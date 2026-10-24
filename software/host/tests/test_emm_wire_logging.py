import time

import pytest

from pa_host.emm_motion import MotionError
from test_powered_manual_driver import powered_bus


def test_wire_logging_never_delays_a_sent_command_response():
    bus, serial, events = powered_bus(enabled=True)
    bus._timeout = 0.02
    original_event = bus._event

    def slow_transmit_log(kind, **fields):
        if kind == "wire" and fields.get("direction") == "tx":
            time.sleep(0.03)
        original_event(kind, **fields)

    bus._event = slow_transmit_log
    assert bus._transact(bytes([1, 0x3A]), 4) == bytes.fromhex("013a036b")
    assert serial.in_waiting == 0


def test_idle_axis_feedback_has_independent_bounded_command_timeouts():
    bus, serial, events = powered_bus(enabled=True)
    bus._timeout = 0.02
    original_event = bus._event

    def slow_receive_log(kind, **fields):
        if kind == "wire" and fields.get("direction") == "rx":
            time.sleep(0.03)
        original_event(kind, **fields)

    bus._event = slow_receive_log
    assert bus._read_axis("X")["speed_rpm"] == 0
    assert len(serial.writes) == 3


def test_explicit_axis_deadline_is_not_extended_by_logging():
    bus, serial, events = powered_bus(enabled=True)
    original_event = bus._event

    def slow_receive_log(kind, **fields):
        if kind == "wire" and fields.get("direction") == "rx":
            time.sleep(0.03)
        original_event(kind, **fields)

    bus._event = slow_receive_log
    with pytest.raises(MotionError) as caught:
        bus._read_axis("X", deadline=time.monotonic() + 0.02)
    assert caught.value.code == "transaction_timeout"
    assert serial.writes == [bytes.fromhex("01366b")]


def test_expired_position_sample_is_not_relabelled_fresh_after_slow_logging():
    bus, serial, events = powered_bus(enabled=True)
    original_event = bus._event

    def stalled_position_log(kind, **fields):
        if kind == "wire" and fields.get("direction") == "rx" and fields.get("opcode") == 0x36:
            time.sleep(2.01)
        original_event(kind, **fields)

    bus._event = stalled_position_log
    with pytest.raises(MotionError) as caught:
        bus._read_axis("X")
    assert caught.value.code == "invalid_telemetry"
    assert bus.snapshot()["axes"]["X"]["telemetry_valid"] is False
