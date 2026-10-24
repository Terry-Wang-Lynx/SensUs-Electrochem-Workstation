import threading

import pytest

from pa_host.emm_motion import MotionError
from test_powered_manual_driver import powered_bus


def test_cancellation_after_write_finishes_the_current_response_before_yielding_the_bus():
    bus, serial, events = powered_bus(enabled=True)
    events.clear()
    cancelled = threading.Event()
    original_read = serial.read
    first_read = True

    def cancel_during_first_read(count):
        nonlocal first_read
        if first_read:
            first_read = False
            cancelled.set()
            return b""
        return original_read(count)

    serial.read = cancel_during_first_read
    with pytest.raises(MotionError) as caught:
        bus._transact(bytes([1, 0x3A]), 4, cancel_event=cancelled, interruptible=True)
    assert caught.value.code == "cancelled"
    received = [event for event in events if event["kind"] == "wire" and event["direction"] == "rx"]
    assert [event["frame_hex"] for event in received] == ["013a036b"]
    assert serial.in_waiting == 0
    assert serial.writes == [bytes.fromhex("013a6b")]


def test_cancellation_before_write_never_sends_a_new_command():
    bus, serial, events = powered_bus(enabled=True)
    cancelled = threading.Event()
    cancelled.set()
    with pytest.raises(MotionError) as caught:
        bus._transact(bytes([1, 0x3A]), 4, cancel_event=cancelled, interruptible=True)
    assert caught.value.code == "cancelled"
    assert serial.writes == []


def test_cancellation_does_not_extend_the_deadline_for_a_missing_response():
    bus, serial, events = powered_bus(enabled=True)
    bus._timeout = 0.01
    cancelled = threading.Event()
    serial.overrides[1, 0x3A] = b""
    original_read = serial.read

    def cancel_while_response_missing(count):
        cancelled.set()
        return original_read(count)

    serial.read = cancel_while_response_missing
    with pytest.raises(MotionError) as caught:
        bus._transact(bytes([1, 0x3A]), 4, cancel_event=cancelled, interruptible=True)
    assert caught.value.code == "transaction_timeout"
