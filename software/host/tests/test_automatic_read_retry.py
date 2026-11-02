import threading

import pytest

from pa_host.emm_motion import MotionError
from test_automatic_arrival_settling import arrival_bus
from test_emm_motion import MotionTestClock
from test_powered_manual_driver import powered_bus


def test_one_lost_read_packet_does_not_replay_finite_move(tmp_path, monkeypatch):
    bus, serial, record = arrival_bus(tmp_path, monkeypatch)
    bus._timeout = .03
    write = serial.write
    dropped = []
    def drop_once(frame):
        if frame[1] == 0x43 and serial.trace_started and not dropped:
            dropped.append(True)
            serial.writes.append(frame)
            return len(frame)
        return write(frame)
    monkeypatch.setattr(serial, 'write', drop_once)
    result = bus.move_to('X', record['x_first_mm'], 20)
    assert result['axes']['X']['speed_rpm'] == 0
    assert dropped
    assert len([f for f in serial.writes if f[1] == 0xFD]) == 1
    assert not any(f[1] == 0xFE for f in serial.writes)
    assert len([e for e in serial.events if e['kind'] == 'read_retry']) == 1


@pytest.mark.parametrize('failure,expected_writes', [
    ('missing', 2), ('partial', 1), ('wrong_binding', 1), ('ack', 1),
    ('deadline', 1), ('idle', 1), ('cancelled', 1), ('late_bytes', 1)])
def test_retry_is_bounded_and_never_hides_ambiguous_reply_or_cancel(monkeypatch, failure, expected_writes):
    bus, serial, events = powered_bus(enabled=True)
    clock = MotionTestClock()
    clock.attach(monkeypatch, serial)
    bus._timeout = .05
    bus._automatic_motion_axis = None if failure == 'idle' else 'X'
    cancelled = threading.Event()
    opcode = 0xF3 if failure == 'ack' else 0x3A
    def missing(frame):
        if failure == 'cancelled':
            cancelled.set()
        return b'\x01' if failure == 'partial' else b'\x02\x3a\x03\x6b' if failure == 'wrong_binding' else b''
    serial.overrides[1, opcode] = missing
    serial.writes.clear()
    if failure == 'late_bytes':
        event = bus._event
        def late_input(kind, **fields):
            event(kind, **fields)
            if kind == 'read_retry':
                serial.buffer.extend(bytes.fromhex('013a036b'))
        bus._event = late_input
    body = bytes([1, opcode]) if opcode != 0xF3 else bytes.fromhex('01f3ab0100')
    with pytest.raises(MotionError) as caught:
        bus._transact(body, 4, interruptible=True, cancel_event=cancelled,
                      ack=failure == 'ack', deadline=clock.monotonic() + (.025 if failure == 'deadline' else 2))
    assert len(serial.writes) == expected_writes
    if failure == 'cancelled':
        assert caught.value.code == 'cancelled'
    if failure == 'late_bytes':
        assert caught.value.code == 'unexpected_input'
    assert clock.monotonic() - bus._last_exchange_monotonic < 1.8
