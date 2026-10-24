import threading

import pytest

from pa_host.emm_motion import MotionError
from test_keyboard_alignment_driver import keyboard_bus
from test_keyboard_responsiveness import responsive_bus


@pytest.mark.parametrize("axis,direction", [("X", 0), ("X", 1), ("Z", 0), ("Z", 1)])
def test_unknown_mechanical_origin_has_no_invented_directional_boundary(axis, direction):
    bus, serial = responsive_bus()
    address = bus.axes[axis]["address"]
    sign = -1 if direction else 1
    serial.positions[address] += sign * round((bus.axes[axis]["travel_mm"] + 5) * 65536 / 2)
    for targets in serial.targets.values():
        targets.update(serial.positions)
    bus._state[axis]["raw_count"] = serial.positions[address]
    assert bus._keyboard_boundaries() == []
    cancellation = threading.Event()
    serial.on_fast_move = lambda moving_address: cancellation.set()
    with pytest.raises(MotionError) as caught:
        bus.alignment_fast_hold(axis, direction, 20, 20, None, None, cancel_event=cancellation)
    assert caught.value.code == "cancelled"
    assert caught.value.stop_result["stop_confirmed"]
    assert not bus.snapshot()["faulted"]
    moves = [frame for frame in serial.writes if frame[1] == 0xFD]
    assert len(moves) == 1 and moves[0][0] == address and moves[0][2] == direction
    assert max(serial.lookahead) <= 6


@pytest.mark.parametrize("axis,direction", [("X", 0), ("X", 1), ("Z", 0), ("Z", 1)])
def test_fine_alignment_does_not_stop_at_nominal_session_travel(axis, direction):
    bus, serial, _, _ = keyboard_bus()
    address = bus.axes[axis]["address"]
    sign = -1 if direction else 1
    serial.positions[address] += sign * round((bus.axes[axis]["travel_mm"] + 1) * 65536 / 2)
    for targets in serial.targets.values():
        targets.update(serial.positions)
    result = bus.alignment_step(axis, direction, .5, .12, 1)
    assert result["segment_mm"] > 0
    assert not bus.snapshot()["faulted"]


def test_explicit_upper_z_guard_does_not_invent_other_three_limits():
    bus, serial = responsive_bus()
    bus.cancel_keyboard_alignment()
    bus.begin_keyboard_alignment(confirmed=True, boundary_at_current={"axis": "Z", "direction": "up"})
    boundaries = bus._keyboard_boundaries()
    assert len(boundaries) == 1
    assert boundaries[0]["axis"] == "Z" and boundaries[0]["direction"] == "up"
    assert boundaries[0]["kind"] == "operator"
    assert boundaries[0]["at_limit"]


def test_controller_encoding_range_remains_checked_without_calling_it_mechanical_limit():
    bus, serial = responsive_bus()
    bus._state["X"]["raw_count"] = 0xFFFFFFFF - 100
    boundaries = bus._keyboard_boundaries()
    assert len(boundaries) == 1
    assert boundaries[0]["kind"] == "protocol"
    assert boundaries[0]["at_limit"]
