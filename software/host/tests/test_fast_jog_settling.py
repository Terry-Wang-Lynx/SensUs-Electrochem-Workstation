import copy
import threading
import time

import pytest

from pa_host.emm_motion import MotionError, SharedEmmBus
from test_emm_motion import AXES
from test_keyboard_fast_jog import FastSerial


class SettlingFastSerial(FastSerial):
    def __init__(self, overshoot_counts=511, **options):
        super().__init__(**options)
        self.overshoot_counts = overshoot_counts
        self.pending = {}
        self.peaks = []

    def write(self, frame):
        address, opcode = frame[:2]
        if opcode == 0xFD:
            result = super().write(frame)
            self.pending[address] = [self.positions[address], -1 if frame[2] else 1, 0]
            self.statuses[address] = 1
            return result
        if opcode == 0xFE:
            self.pending.pop(address, None)
        if opcode == 0x36 and address in self.pending:
            target, direction, sample = self.pending[address]
            offset = self.overshoot_counts if sample == 0 else self.overshoot_counts // 2 if sample == 1 else 0
            self.positions[address] = target + direction * offset
            self.statuses[address] = 1 if offset else 3
            self.pending[address][2] += 1
            self.peaks.append(self.positions[address])
        return super().write(frame)


def settling_bus(overshoot_counts=511):
    serial = SettlingFastSerial(overshoot_counts=overshoot_counts, timeout=0.2, write_timeout=0.2)
    axes = copy.deepcopy(AXES)
    for settings in axes.values():
        settings["max_speed_mm_s"] = 20
    bus = SharedEmmBus("fake-fast-settling", 115200, axes, serial_factory=lambda **options: serial)
    bus._command_gap_s = 0
    bus.connect()
    bus.begin_keyboard_alignment(confirmed=True)
    serial.writes.clear()
    return bus, serial


@pytest.mark.parametrize("direction", [0, 1])
def test_observed_terminal_overshoot_settles_inside_the_original_hold_budget(direction):
    bus, serial = settling_bus()
    start = serial.positions[1]
    sign = -1 if direction else 1
    result = bus.alignment_fast_hold("X", direction, 20, 20, 10, 30)
    submitted = sum(int.from_bytes(frame[6:10], "big") for frame in serial.writes if frame[1] == 0xFD) * 2 / 3200
    assert submitted <= 9.9
    assert max(sign * (position - start) * 2 / 65536 for position in serial.peaks) <= 10
    assert abs(serial.positions[1] - serial.targets[0x33][1]) <= 1
    assert result["hold_exhausted"] is True
    assert bus.snapshot()["faulted"] is False


def test_encoder_escape_beyond_reserved_settling_room_still_stops_and_faults():
    bus, serial = settling_bus(overshoot_counts=5000)
    with pytest.raises(MotionError) as caught:
        bus.alignment_fast_hold("X", 0, 20, 20, 10, 30)
    assert caught.value.code == "calibration_motion"
    assert bus.snapshot()["faulted"] is True
    assert len([frame for frame in serial.writes if frame[1] == 0xFD]) == 1


@pytest.mark.parametrize("direction", [0, 1])
@pytest.mark.parametrize("exit_reason", ["release", "time_limit"])
@pytest.mark.parametrize("boundary", ["committed", "hard"])
def test_stop_exit_rechecks_active_axis_inside_its_reserved_envelope(direction, exit_reason, boundary):
    from test_keyboard_fast_jog import fast_bus

    bus, serial = fast_bus()
    cancellation = threading.Event()
    start = serial.positions[1]
    sign = -1 if direction else 1

    def escape_and_stop(address):
        if boundary == "hard" and sign * (serial.targets[0x33][address] - start) * 2 / 65536 < 9:
            return
        serial.positions[address] += sign * 5000
        if exit_reason == "release":
            cancellation.set()
        else:
            time.sleep(0.55)

    serial.on_fast_move = escape_and_stop
    with pytest.raises(MotionError) as caught:
        bus.alignment_fast_hold("X", direction, 20, 20, 10, 0.5 if exit_reason == "time_limit" else 30,
                                cancel_event=cancellation)
    assert caught.value.code == "calibration_motion"
    assert caught.value.stop_result["stop_confirmed"] is True
    assert bus.snapshot()["faulted"] is True
