import copy
import threading
import time

import pytest

from pa_host.emm_motion import MotionError, SharedEmmBus
from test_emm_motion import AXES, signed
from test_keyboard_fast_jog import FastSerial


class SummarySerial(FastSerial):
    def write(self, frame):
        address, opcode = frame[:2]
        if opcode == 0x43 and (address, opcode) not in self.overrides:
            assert frame == bytes([address, 0x43, 0x7A, 0x6B])
            self.writes.append(frame)
            self.buffer.extend(bytes([address, opcode, 31, 9]) + (24000).to_bytes(2, "big")
                               + (3).to_bytes(2, "big") + (self.positions[address] & 0xFFFF).to_bytes(2, "big")
                               + signed(self.targets[0x33][address], 4) + signed(self.speeds[address], 2)
                               + signed(self.positions[address], 4)
                               + signed(self.targets[0x34][address] - self.positions[address], 4)
                               + bytes([3, self.statuses[address], 0x6B]))
            return len(frame)
        return super().write(frame)


def responsive_bus():
    serial = SummarySerial(timeout=0.2, write_timeout=0.2)
    axes = copy.deepcopy(AXES)
    for settings in axes.values():
        settings["max_speed_mm_s"] = 20
    bus = SharedEmmBus("fake-responsive", 115200, axes, serial_factory=lambda **options: serial)
    bus._command_gap_s = 0
    bus.connect()
    bus._responsive_keyboard = True
    bus.begin_keyboard_alignment(confirmed=True)
    serial.writes.clear()
    return bus, serial


def test_warm_keyboard_start_does_not_repeat_full_initialization():
    bus, serial = responsive_bus()
    cancellation = threading.Event()
    serial.on_fast_move = lambda address: cancellation.set()
    with pytest.raises(MotionError) as caught:
        bus.alignment_fast_hold("X", 0, 20, 20, None, None, cancel_event=cancellation)
    assert caught.value.code == "cancelled"
    first_move = next(index for index, frame in enumerate(serial.writes) if frame[1] == 0xFD)
    assert first_move <= 10
    assert caught.value.stop_result["stop_confirmed"] is True


def test_release_uses_compact_fresh_stop_and_audit_instead_of_reinitializing():
    bus, serial = responsive_bus()
    cancellation = threading.Event()
    serial.on_fast_move = lambda address: cancellation.set()
    with pytest.raises(MotionError) as caught:
        bus.alignment_fast_hold("X", 0, 20, 20, None, None, cancel_event=cancellation)
    first_stop = next(index for index, frame in enumerate(serial.writes) if frame[1] == 0xFE)
    assert len(serial.writes[first_stop:]) <= 16
    assert caught.value.stop_result["stop_confirmed"] is True


@pytest.mark.parametrize("index", [2, 3, 10, 15, 18, 23])
def test_compact_feedback_rejects_invalid_layout_and_signs(index):
    bus, serial = responsive_bus()
    frame = bytearray(bus._transact(bytes([1, 0x43, 0x7A]), 31))
    frame[index] = 2
    serial.overrides[1, 0x43] = bytes(frame)
    with pytest.raises(MotionError) as caught:
        bus._read_axis("X")
    assert caught.value.code == "invalid_telemetry"
    assert not any(frame[1] == 0xFD for frame in serial.writes)


@pytest.mark.parametrize("changed", ["target", "instantaneous", "moving", "fault", "profile", "power"])
@pytest.mark.parametrize("changed_axis,address", [("X", 1), ("Z", 2)])
def test_responsive_start_still_rejects_unhealthy_or_changed_hardware(changed, changed_axis, address):
    bus, serial = responsive_bus()
    if changed in ("target", "instantaneous"):
        serial.targets[0x33 if changed == "target" else 0x34][address] += 1000
    elif changed == "moving":
        serial.speeds[address] = 10
    elif changed == "fault":
        serial.statuses[address] = 7
    elif changed == "power":
        serial.statuses[address] |= 0x80
    else:
        profile = bytearray.fromhex(bus.snapshot()["axes"][changed_axis]["config"]["raw_hex"])
        profile[9] = 32
        serial.overrides[address, 0x42] = bytes(profile)
    with pytest.raises(MotionError):
        bus.alignment_fast_hold("X", 0, 20, 20, None, None, cancel_event=threading.Event())
    assert not any(frame[1] == 0xFD for frame in serial.writes)


def test_compact_mode_probe_checks_separate_feedback_before_enabling():
    bus, serial = responsive_bus()
    bus._responsive_keyboard = False
    bus.enable_responsive_keyboard()
    assert bus._responsive_keyboard is True
    assert {0x43, 0x36, 0x35, 0x3A, 0x33} <= {frame[1] for frame in serial.writes}


def test_keydown_preempts_idle_refresh_without_fault_or_extra_stop():
    from test_alignment_jog import jogging, start

    manager, motion, clock = jogging()
    entered = threading.Event()

    def refresh_keyboard_alignment(cancel_event=None):
        entered.set()
        assert cancel_event.wait(1)
        raise MotionError("cancelled", "Idle read yielded to keydown")

    motion.refresh_keyboard_alignment = refresh_keyboard_alignment
    clock.now = 0.5
    worker = threading.Thread(target=manager._refresh_idle_once)
    worker.start()
    try:
        assert entered.wait(0.5)
        start(manager)
        worker.join(0.2)
        assert not worker.is_alive()
        manager._step_once()
        assert len(motion.steps) == 1
        assert motion.stops == 0
        assert manager.snapshot()["status"] == "holding"
    finally:
        worker.join(2)


@pytest.mark.parametrize("fault", ["sign", "layout", "status", "power"])
def test_idle_preemption_does_not_hide_a_semantically_invalid_completed_reply(fault):
    bus, serial = responsive_bus()
    cancellation = threading.Event()
    frame = bytearray(bus._transact(bytes([1, 0x43, 0x7A]), 31))
    if fault == "sign":
        frame[18] = 2
    elif fault == "layout":
        frame[3] = 8
    elif fault == "status":
        frame[29] = 7
    else:
        frame[29] |= 0x80

    def interrupted_reply(command):
        cancellation.set()
        return bytes(frame)

    serial.overrides[1, 0x43] = interrupted_reply
    with pytest.raises(MotionError) as caught:
        bus.refresh_keyboard_alignment(cancel_event=cancellation)
    assert caught.value.code in ("invalid_telemetry", "axis_fault", "power_flag_changed")
    assert bus.snapshot()["faulted"]
    assert {value[0] for value in serial.writes if value[1] == 0xFE} == {1, 2}
    assert not any(value[1] == 0xFD for value in serial.writes)
