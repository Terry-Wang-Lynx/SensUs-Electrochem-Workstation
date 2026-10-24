import threading

import pytest

from pa_host.alignment_jog import AlignmentJog
from pa_host.emm_motion import MotionError
from test_keyboard_responsiveness import responsive_bus


@pytest.mark.parametrize("speed", [0.5, 20])
@pytest.mark.parametrize("axis,direction,label,reverse", [("X", 0, "left", "right"), ("X", 1, "right", "left"),
                                                         ("Z", 0, "down", "up"), ("Z", 1, "up", "down")])
def test_boundary_blocks_only_outward_and_keeps_reverse_and_other_axis_available(speed, axis, direction, label, reverse):
    bus, serial = responsive_bus()
    address = bus.axes[axis]["address"]
    sign = -1 if direction else 1
    bus._manual["direction_bounds"] = {axis: {"lower" if direction else "upper":
        serial.positions[address] + sign * round(0.05 * 65536 / 2)}}
    manager = AlignmentJog(bus, bus.snapshot()["manual"]["session_token"], start_threads=False)
    token = manager.snapshot()["session_token"]
    manager.configure({"session_token": token, "speed_mm_s": speed, "max_speed_mm_s": speed,
                       "fast_motion_confirmed": speed > 2})
    manager.start({"session_token": token, "press_id": "edge", "axis": axis, "direction": label})
    manager._step_once()
    stopped = manager.snapshot()
    assert stopped["status"] == "idle"
    assert stopped["last_stop_reason"] == "travel_limit"
    assert stopped["error"] is None
    assert not any(frame[1] == 0xFD for frame in serial.writes)
    assert any(boundary["axis"] == axis and boundary["direction"] == label and boundary["at_limit"]
               for boundary in stopped["boundaries"])
    assert manager.heartbeat({"session_token": token, "press_id": "edge", "sequence": 1})["status"] == "idle"
    manager.configure({"session_token": token, "speed_mm_s": 20, "max_speed_mm_s": 20,
                       "fast_motion_confirmed": True})
    peer_axis, peer_direction = ("X", "left") if axis == "Z" else ("Z", "down")
    for press, move_axis, move_direction, expected_direction in [("reverse", axis, reverse, 1 - direction),
                                                               ("peer", peer_axis, peer_direction, 0)]:
        serial.writes.clear()
        manager.start({"session_token": token, "press_id": press, "axis": move_axis, "direction": move_direction})
        serial.on_fast_move = lambda moving_address: manager._active["cancel_event"].set()
        manager._step_once()
        manager.stop({"session_token": token, "press_id": press})
        moves = [frame for frame in serial.writes if frame[1] == 0xFD]
        assert len(moves) == 1 and moves[0][0] == bus.axes[move_axis]["address"] and moves[0][2] == expected_direction
        assert not bus.snapshot()["faulted"]
        assert manager.snapshot()["status"] == "idle"


def test_repeated_up_at_reserved_boundary_never_creeps_closer():
    bus, serial = responsive_bus()
    bus._manual["direction_bounds"] = {"Z": {"lower": serial.positions[2] - round(0.09 * 65536 / 2)}}
    for attempt in range(5):
        result = bus.alignment_fast_hold("Z", 1, 20, 20, None, None, cancel_event=threading.Event())
        assert result["stop_reason"] == "travel_limit"
    assert not any(frame[1] == 0xFD for frame in serial.writes)
    assert not bus.snapshot()["faulted"]


@pytest.mark.parametrize("delta,allowed", [(12, True), (1000, False)])
def test_cross_axis_hold_uses_drive_position_window_without_hiding_real_displacement(delta, allowed):
    bus, serial = responsive_bus()
    cancellation = threading.Event()

    def moved(address):
        serial.positions[1] += delta
        serial.targets[0x33][1] = serial.targets[0x34][1] = serial.positions[1]
        cancellation.set()

    serial.on_fast_move = moved
    with pytest.raises(MotionError) as caught:
        bus.alignment_fast_hold("Z", 1, 20, 20, None, None, cancel_event=cancellation)
    assert (caught.value.code == "cancelled") is allowed
    assert bus.snapshot()["faulted"] is not allowed


@pytest.mark.parametrize("axis,direction,label", [("X", 0, "left"), ("X", 1, "right"), ("Z", 0, "down"), ("Z", 1, "up")])
def test_operator_boundary_preserves_stop_and_reverse_behavior(axis, direction, label):
    bus, serial = responsive_bus()
    bus.cancel_keyboard_alignment()
    serial.writes.clear()
    bus.begin_keyboard_alignment(confirmed=True, boundary_at_current={"axis": axis, "direction": label})
    before = serial.positions[bus.axes[axis]["address"]]
    result = bus.alignment_fast_hold(axis, direction, 20, 20, None, None, cancel_event=threading.Event())
    assert result["stop_reason"] == "travel_limit"
    assert serial.positions[bus.axes[axis]["address"]] == before
    assert not any(frame[1] in (0xFD, 0xF3, 0xF6, 0x0A, 0x9A) for frame in serial.writes)
