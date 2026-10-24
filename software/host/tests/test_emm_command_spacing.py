import threading
import time
from types import SimpleNamespace

import pytest

from pa_host.emm_motion import MotionError, SharedEmmBus
from test_emm_motion import AXES
from test_powered_manual_driver import PoweredManualSerial, powered_bus


def spaced_bus(monkeypatch):
    bus, serial, events = powered_bus(enabled=True)
    clock = SimpleNamespace(now=100.0, sleeping=None)

    def sleep(seconds):
        clock.now += seconds
        if clock.sleeping:
            clock.sleeping()

    monkeypatch.setattr("pa_host.emm_motion.time", SimpleNamespace(
        monotonic=lambda: clock.now, time=lambda: 1700000000 + clock.now, sleep=sleep))
    bus._command_gap_s = 0.02
    bus._last_exchange_monotonic = clock.now
    return bus, serial, clock


def test_normal_commands_wait_for_bus_silence_without_consuming_wire_timeout(monkeypatch):
    bus, serial, clock = spaced_bus(monkeypatch)
    bus._timeout = 0.01
    assert bus._transact(bytes([1, 0x3A]), 4) == bytes.fromhex("013a036b")
    assert clock.now == pytest.approx(100.02)
    assert bus._transact(bytes([2, 0x3A]), 4) == bytes.fromhex("023a036b")
    assert clock.now == pytest.approx(100.04)


def test_gap_never_extends_an_explicit_operation_deadline(monkeypatch):
    bus, serial, clock = spaced_bus(monkeypatch)
    with pytest.raises(MotionError) as caught:
        bus._transact(bytes([1, 0x3A]), 4, deadline=clock.now + 0.01)
    assert caught.value.code == "transaction_timeout"
    assert not serial.writes


def test_cancel_during_gap_prevents_the_command_write(monkeypatch):
    bus, serial, clock = spaced_bus(monkeypatch)
    cancelled = threading.Event()
    clock.sleeping = cancelled.set
    with pytest.raises(MotionError) as caught:
        bus._transact(bytes([1, 0x3A]), 4, cancel_event=cancelled, interruptible=True)
    assert caught.value.code == "cancelled"
    assert not serial.writes


def test_late_bytes_arriving_during_gap_are_not_discarded(monkeypatch):
    bus, serial, clock = spaced_bus(monkeypatch)
    clock.sleeping = lambda: serial.buffer.extend(b"\x01\x00\xee\x6b")
    with pytest.raises(MotionError) as caught:
        bus._transact(bytes([2, 0x3A]), 4)
    assert caught.value.code == "unexpected_input"
    assert not serial.writes


def test_explicit_safety_stop_bypasses_gap_but_segment_fe_does_not(monkeypatch):
    bus, serial, clock = spaced_bus(monkeypatch)
    assert bus._transact(bytes([1, 0xFE, 0x98, 0]), 4, ack=True, stop_command=True)[2] == 2
    assert clock.now == 100
    assert bus._transact(bytes([1, 0xFE, 0x98, 0]), 4, ack=True)[2] == 2
    assert clock.now == pytest.approx(100.02)


def test_elapsed_gap_adds_no_wait_and_closed_port_discards_old_pacing(monkeypatch):
    bus, serial, clock = spaced_bus(monkeypatch)
    clock.now += 0.03
    bus._transact(bytes([1, 0x3A]), 4)
    assert clock.now == pytest.approx(100.03)
    bus._close_port()
    assert bus._last_exchange_monotonic is None


@pytest.mark.parametrize("transaction_latency", [0, 0.03])
def test_real_default_spacing_supports_two_axis_stationary_confirmation(transaction_latency):
    serial = PoweredManualSerial(timeout=0.2, write_timeout=0.2)
    serial.statuses = {1: 3, 2: 3}
    bus = SharedEmmBus("fake-default-cadence", 115200, AXES, serial_factory=lambda **options: serial)
    assert bus._command_gap_s == 0.02 and bus._timeout == 1.0
    bus.connect()
    original_write = serial.write

    def write(frame):
        time.sleep(transaction_latency)
        return original_write(frame)

    serial.write = write
    assert set(bus._stationary_states(enabled=True)) == {"X", "Z"}


def test_verified_keyboard_read_phase_keeps_normal_gap_and_blocks_control_commands(monkeypatch):
    bus, serial, clock = spaced_bus(monkeypatch)
    bus._responsive_keyboard = True
    bus._manual.update(active=True, control_mode="keyboard")
    bus._transact(bytes([1, 0x3A]), 4, keyboard_read=True)
    assert clock.now == pytest.approx(100.02)
    bus._transact(bytes([2, 0x3A]), 4, keyboard_read=True)
    assert clock.now == pytest.approx(100.04)
    with pytest.raises(MotionError) as caught:
        bus._transact(bytes([1, 0xFD, 0]), 4, keyboard_read=True)
    assert caught.value.code == "keyboard_read_phase"
    assert all(frame[1] != 0xFD for frame in serial.writes)
    bus._manual["polling_axis"] = "X"
    bus._keyboard_segment_started = clock.now
    for address in (1, 2):
        with pytest.raises(MotionError) as caught:
            bus._transact(bytes([address, 0x3A]), 4, keyboard_read=True)
        assert caught.value.code == "keyboard_read_phase"
    assert len(serial.writes) == 2
    bus._transact(bytes([1, 0xFE, 0x98, 0]), 4, ack=True, stop_command=True)
    assert clock.now == pytest.approx(100.04)
    assert serial.writes[-1][1] == 0xFE
