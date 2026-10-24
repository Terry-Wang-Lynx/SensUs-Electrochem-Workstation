import copy
import math
import threading

import pytest

from pa_host.emm_motion import MotionError, SharedEmmBus
from test_emm_motion import AXES
from test_keyboard_responsiveness import SummarySerial


@pytest.mark.parametrize("sign", [-1, 1])
def test_signed_integer_representation_budget_covers_zero_crossing(sign):
    from pa_host.keyboard_fast_jog import _target_representation_budget

    counts_per_pulse = 65536 / 3200
    internal_start = sign * 10.25
    represented_start = math.trunc(internal_start)
    quantized_start = round(internal_start / counts_per_pulse) * counts_per_pulse
    signed_pulses = -sign * 3
    represented_target = math.trunc(quantized_start + signed_pulses * counts_per_pulse)
    expected_target = represented_start + signed_pulses * counts_per_pulse
    error = abs(represented_target - expected_target)
    assert error > counts_per_pulse / 2 + 1
    assert error <= _target_representation_budget(counts_per_pulse)


def test_fixed_budget_covers_starting_count_phases_under_signed_truncation_model():
    from pa_host.keyboard_fast_jog import _target_representation_budget

    counts_per_pulse = 65536 / 3200
    for whole_count in range(-250, 251):
        for fraction in (.01, .25, .49, .75, .99):
            internal_start = whole_count + fraction
            baseline = math.trunc(internal_start)
            pulse_start = round(internal_start / counts_per_pulse) * counts_per_pulse
            for pulse_delta in (-9431, -3, 3, 9431):
                returned = math.trunc(pulse_start + pulse_delta * counts_per_pulse)
                expected = baseline + pulse_delta * counts_per_pulse
                assert abs(returned - expected) <= _target_representation_budget(counts_per_pulse)


class IncidentSerial(SummarySerial):
    def __init__(self, *, extra_error=0):
        super().__init__(timeout=.2, write_timeout=.2)
        self.positions = {1: -4099549, 2: 2950249}
        for targets in self.targets.values():
            targets.update({1: -4099549, 2: 2950251})
        self.replay = False
        self.commands = []
        self.progress = iter([(2946634, -59), (2940782, -151), (2932380, -245),
                              (2921612, -331), (2907928, -423), (2891536, -513), (2874114, -576)])
        self.extra_error = extra_error

    def write(self, frame):
        address, opcode = frame[:2]
        if self.replay and opcode == 0xFD:
            assert address == 2 and frame[2] == 1
            self.commands.append(frame)
            self.writes.append(frame)
            target = [2757094, 2680949 - self.extra_error][len(self.commands) - 1]
            self.targets[0x33][2] = target
            self.buffer.extend(bytes.fromhex("02fd026b"))
            return len(frame)
        if self.replay and address == 2 and opcode == 0x43 and len(self.commands) == 1:
            self.positions[2], self.speeds[2] = next(self.progress)
            self.statuses[2] = 1
        if self.replay and opcode == 0xFE:
            self.replay = False
            self.speeds = {1: 0, 2: 0}
            self.statuses = {1: 3, 2: 3}
            for targets in self.targets.values():
                targets.update(self.positions)
        return super().write(frame)


@pytest.mark.parametrize("extra_error,expected", [(0, "cancelled"), (2, "manual_target_mismatch"), (1000, "manual_target_mismatch")])
def test_real_z_up_second_segment_checks_integer_target_rounding_without_masking_wrong_targets(extra_error, expected):
    serial = IncidentSerial(extra_error=extra_error)
    axes = copy.deepcopy(AXES)
    for config in axes.values():
        config.update(positive_direction=0, max_speed_mm_s=20)
    bus = SharedEmmBus("fake-target-rounding", 115200, axes, serial_factory=lambda **options: serial)
    bus._command_gap_s = 0
    bus.connect()
    bus._responsive_keyboard = True
    bus.begin_keyboard_alignment(confirmed=True)
    serial.writes.clear()
    serial.replay = True
    cancelled = threading.Event()
    events = []
    original = bus._event

    def event(kind, **fields):
        events.append({"kind": kind, **fields})
        original(kind, **fields)
        if kind == "keyboard_fast_target" and len(serial.commands) == 2:
            cancelled.set()

    bus._event = event
    with pytest.raises(MotionError) as caught:
        bus.alignment_fast_hold("Z", 1, 20, 20, None, None, cancel_event=cancelled)
    assert caught.value.code == expected
    assert [frame.hex() for frame in serial.commands] == ["02fd010258fa000024d700006b", "02fd010258fa00000e8600006b"]
    assert caught.value.stop_result["stop_confirmed"]
    assert bus.snapshot()["faulted"] is (expected != "cancelled")
    assert not any(frame[1] in (0xF3, 0xF6, 0x0A, 0x9A) for frame in serial.writes)
