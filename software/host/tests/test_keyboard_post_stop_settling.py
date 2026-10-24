import threading

import pytest

from pa_host.emm_motion import MotionError
from test_keyboard_responsiveness import responsive_bus


def stopped_tail_bus(axis="X", direction=0, offsets=(23, 23, 9, 7, 7), mutate=None):
    bus, serial = responsive_bus()
    bus._timeout = 0.05
    cancellation = threading.Event()
    address = bus.axes[axis]["address"]
    sign = -1 if direction else 1
    original_write = serial.write
    tail = {"goal": None, "samples": [], "stop_count": 0}

    def released(moving_address):
        cancellation.set()

    def write(frame):
        current_address, opcode = frame[:2]
        if opcode == 0xFE and current_address == address:
            tail["stop_count"] += 1
            if tail["goal"] is None:
                tail["goal"] = serial.positions[address]
        if opcode == 0x43 and current_address == address and tail["goal"] is not None:
            sample = min(len(tail["samples"]), len(offsets) - 1)
            serial.positions[address] = tail["goal"] + sign * offsets[sample]
            tail["samples"].append(serial.positions[address])
            if mutate is not None:
                mutate(bus, serial, address, len(tail["samples"]))
        return original_write(frame)

    serial.on_fast_move = released
    serial.write = write
    return bus, serial, cancellation, tail


@pytest.mark.parametrize("axis,direction", [("X", 0), ("X", 1), ("Z", 0), ("Z", 1)])
def test_confirmed_stop_plateau_then_small_target_convergence_does_not_fault(axis, direction):
    bus, serial, cancellation, tail = stopped_tail_bus(axis, direction)
    with pytest.raises(MotionError) as caught:
        bus.alignment_fast_hold(axis, direction, 20, 20, None, None, cancel_event=cancellation)
    assert caught.value.code == "cancelled"
    assert caught.value.stop_result["stop_confirmed"] is True
    assert not bus.snapshot()["faulted"]
    assert bus.snapshot()["manual"]["active"]
    assert len(tail["samples"]) == 3
    assert caught.value.stop_result["axes"][axis]["stop_basis"] == "keyboard_position_window"
    assert tail["stop_count"] == 1
    assert len([frame for frame in serial.writes if frame[1] == 0xFD]) == 1
    assert not any(frame[1] in (0xF3, 0xF6, 0x0A, 0x9A) for frame in serial.writes)


@pytest.mark.parametrize("failure", ["rpm", "disabled", "fault", "power", "target", "instantaneous", "held_axis"])
@pytest.mark.parametrize("sample", [1, 2, 3])
def test_stop_tail_never_hides_changed_targets_or_unhealthy_feedback(failure, sample):
    def mutate(bus, serial, address, observed):
        if observed != sample:
            return
        if failure == "rpm":
            serial.speeds[address] = 1
        elif failure == "disabled":
            serial.statuses[address] = 2
        elif failure == "fault":
            serial.statuses[address] = 7
        elif failure == "power":
            serial.statuses[address] |= 0x80
        elif failure == "target":
            serial.targets[0x33][address] += 20
        elif failure == "instantaneous":
            serial.targets[0x34][address] += 20
        else:
            serial.positions[2] += 1000

    bus, serial, cancellation, tail = stopped_tail_bus(mutate=mutate)
    with pytest.raises(MotionError) as caught:
        bus.alignment_fast_hold("X", 0, 20, 20, None, None, cancel_event=cancellation)
    assert caught.value.code != "cancelled" or caught.value.stop_result["stop_confirmed"] is False
    assert bus.snapshot()["faulted"]
    assert len([frame for frame in serial.writes if frame[1] == 0xFD]) == 1


@pytest.mark.parametrize("offsets", [(23, 23, 200, 200), (23, 23, -200, -200), (200, 200, 200),
                                      (23, 23, *range(200, 240, 2))])
def test_stop_tail_rejects_divergence_overshoot_or_failure_to_settle(offsets):
    bus, serial, cancellation, tail = stopped_tail_bus(offsets=offsets)
    with pytest.raises(MotionError) as caught:
        bus.alignment_fast_hold("X", 0, 20, 20, None, None, cancel_event=cancellation)
    assert caught.value.code != "cancelled" or caught.value.stop_result["stop_confirmed"] is False
    assert bus.snapshot()["faulted"]
    assert len([frame for frame in serial.writes if frame[1] == 0xFD]) == 1
