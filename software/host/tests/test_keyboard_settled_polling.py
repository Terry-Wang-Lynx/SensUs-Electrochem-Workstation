import copy
import time

import pytest

from pa_host.emm_motion import MotionError, SharedEmmBus
from test_emm_motion import AXES, config_reply, signed
from test_powered_manual_driver import PoweredManualSerial, powered_bus


class SettlingSerial(PoweredManualSerial):
    def __init__(self, **options):
        super().__init__(**options)
        self.statuses = {1: 3, 2: 3}
        self.move_completes = False
        self.active_address = None
        self.feedback_reads = 0
        self.quiet_reads = 0
        self.target = None
        self.on_settled = None
        self.require_quiesce = False

    def write(self, frame):
        address, opcode = frame[:2]
        if opcode == 0xFD:
            self.active_address = address
            self.feedback_reads = self.quiet_reads = 0
            delta = round(int.from_bytes(frame[6:10], "big") * 65536 / 3200)
            self.target = self.positions[address] + (-delta if frame[2] else delta)
            self.motion_sign = -1 if frame[2] else 1
        elif opcode == 0xFE:
            self.active_address = None
            self.statuses[address] |= 2
        if self.active_address is not None and opcode == 0x36 and address == self.active_address:
            self.feedback_reads += 1
            remaining = max(0, 140 - 20 * self.feedback_reads)
            self.positions[address] = self.target - self.motion_sign * remaining
            self.statuses[address] = 3
            if remaining == 0:
                self.quiet_reads += 1
                for targets in self.targets.values():
                    targets[address] = self.target
                if self.quiet_reads >= 3:
                    if not self.require_quiesce:
                        self.active_address = None
                    if self.on_settled is not None:
                        self.on_settled(address)
                        self.on_settled = None
        reject_peer = self.active_address is not None and address != self.active_address
        rejection_address = self.active_address
        result = super().write(frame)
        if reject_peer:
            self.buffer.extend(bytes([rejection_address, 0, 0xEE, 0x6B]))
        return result


def settled_bus():
    serial = SettlingSerial(timeout=0.05, write_timeout=0.05)
    bus = SharedEmmBus("fake-settled-polling", 115200, copy.deepcopy(AXES),
                       serial_factory=lambda **options: serial, transaction_timeout_s=0.05)
    bus._command_gap_s = 0
    bus.connect()
    return bus, serial


def test_stationary_preflight_waits_for_actual_quiet_instead_of_faulting_on_the_first_pair():
    bus, serial, events = powered_bus(enabled=True)
    counts = iter((65536, 65560, 65560, 65560))
    observed = []

    def position_reply(frame):
        count = next(counts, 65560)
        observed.append(count)
        return bytes([1, 0x36]) + signed(count, 4) + b"\x6b"

    serial.overrides[1, 0x36] = position_reply
    result = bus._stationary_states(enabled=True)
    assert observed[:3] == [65536, 65560, 65560]
    assert result["X"]["raw_count"] == 65560
    assert not any(frame[1] in (0xF3, 0xFD, 0xF6) for frame in serial.writes)


@pytest.mark.parametrize("jitter_counts,allowed", [(2, True), (21, False)])
def test_preflight_quantization_band_accepts_subpulse_jitter_but_not_a_whole_microstep(jitter_counts, allowed):
    bus, serial, events = powered_bus(enabled=True)
    bus.begin_keyboard_alignment(confirmed=True)
    sample = 0

    def position_reply(frame):
        nonlocal sample
        sample += 1
        count = 65536 + (jitter_counts if sample % 2 else 0)
        return bytes([1, 0x36]) + signed(count, 4) + b"\x6b"

    serial.overrides[1, 0x36] = position_reply
    serial.writes.clear()
    if allowed:
        result = bus.capture_keyboard_alignment()
        assert result["raw_counts"]["X"] in (65536, 65538)
    else:
        with pytest.raises(MotionError) as caught:
            bus.capture_keyboard_alignment()
        assert caught.value.code in {"axis_moving", "transaction_timeout"}
    assert not any(frame[1] in (0xF3, 0xFD, 0xF6) for frame in serial.writes)


def test_quiet_window_does_not_reset_forever_on_two_count_encoder_quantization():
    bus, serial = settled_bus()
    bus.begin_keyboard_alignment(confirmed=True)
    sample = 0

    def position_reply(frame):
        nonlocal sample
        sample += 1
        count = serial.positions[1] + (1 if sample % 2 else -1)
        return bytes([1, 0x36]) + signed(count, 4) + b"\x6b"

    serial.overrides[1, 0x36] = position_reply
    result = bus.alignment_step("X", 0, 0.5, 0.12, 1)
    assert abs(result["raw_counts"]["X"] - serial.target) <= 1
    assert bus.snapshot()["faulted"] is False


def test_gentle_short_segment_has_time_for_acceleration_and_encoder_settling():
    bus, serial, events = powered_bus(enabled=True)
    bus.begin_keyboard_alignment(confirmed=True)
    serial.move_completes = False
    motion_started = None
    target = serial.positions[1] + 1966

    def started():
        nonlocal motion_started
        motion_started = time.monotonic()

    def position_reply(frame):
        if motion_started is not None and time.monotonic() - motion_started >= 1.25:
            serial.positions[1] = target
            serial.statuses[1] = 3
            for targets in serial.targets.values():
                targets[1] = target
        return bytes([1, 0x36]) + signed(serial.positions[1], 4) + b"\x6b"

    serial.on_move = started
    serial.overrides[1, 0x36] = position_reply
    result = bus.alignment_step("X", 0, 0.5, 0.12, 1)
    assert result["raw_counts"]["X"] == target
    assert len([frame for frame in serial.writes if frame[1] == 0xFD]) == 1


def test_completed_segment_quiesces_commanded_motor_before_full_peer_audit():
    bus, serial = settled_bus()
    bus.begin_keyboard_alignment(confirmed=True)
    serial.require_quiesce = True
    serial.writes.clear()
    result = bus.alignment_step("X", 0, 0.5, 0.12, 1)
    assert result["segment_mm"] == pytest.approx(0.06)
    move_index = next(index for index, frame in enumerate(serial.writes) if frame[1] == 0xFD)
    stop_index = next(index for index, frame in enumerate(serial.writes[move_index + 1:], move_index + 1)
                      if frame[:2] == bytes([1, 0xFE]))
    assert all(frame[0] == 1 for frame in serial.writes[move_index:stop_index])
    assert bus.snapshot()["manual"]["active"] is True
    assert bus.snapshot()["faulted"] is False


@pytest.mark.parametrize("axis,direction", [("X", 0), ("X", 1), ("Z", 0), ("Z", 1)])
def test_keyboard_waits_for_quiet_encoder_before_releasing_the_bus_to_peer_queries(axis, direction):
    bus, serial = settled_bus()
    bus.begin_keyboard_alignment(confirmed=True)
    serial.writes.clear()
    started = time.monotonic()
    result = bus.alignment_step(axis, direction, 0.5, 0.12, 1)
    assert serial.quiet_reads >= 3
    assert result["raw_counts"][axis] == serial.target
    assert result["segment_mm"] == pytest.approx(0.06)
    assert time.monotonic() - started >= 0.1
    assert bus.snapshot()["faulted"] is False
    assert len([frame for frame in serial.writes if frame[1] == 0xFD]) == 1
    assert not any(frame[1] in (0xF3, 0xF6, 0x0A, 0x9A) for frame in serial.writes)


@pytest.mark.parametrize("failure", ["held_axis", "profile"])
def test_post_quiet_verification_still_rejects_changed_held_axis_and_configuration(failure):
    bus, serial = settled_bus()
    bus.begin_keyboard_alignment(confirmed=True)

    def changed_after_settling(address):
        if failure == "held_axis":
            serial.positions[2] += 1000
        else:
            response = bytearray(config_reply(2))
            response[9] = 32
            serial.overrides[2, 0x42] = bytes(response)

    serial.on_settled = changed_after_settling
    with pytest.raises(MotionError) as caught:
        bus.alignment_step("X", 0, 0.5, 0.12, 1)
    assert caught.value.code != "response_length"
    assert bus.snapshot()["faulted"] is True
    assert caught.value.stop_result["stop_confirmed"] is True
