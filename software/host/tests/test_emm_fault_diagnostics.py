import pytest

from pa_host.emm_motion import MotionError
from test_emm_motion import make_bus


@pytest.mark.parametrize("before_command", [False, True])
def test_fault_records_buffered_bytes_without_accepting_them_as_telemetry(before_command):
    events = []
    bus, opened = make_bus(event_callback=events.append)
    bus.connect()
    serial = opened[0]
    serial.writes.clear()
    extra = bytes.fromhex("01fd9f6b")
    if before_command:
        serial.buffer.extend(extra)
    else:
        serial.overrides[1, 0x36] = bytes.fromhex("013600000100006b") + extra
    with pytest.raises(MotionError) as caught:
        bus.refresh()
    assert caught.value.code == ("unexpected_input" if before_command else "response_length")
    diagnostic = next((event for event in events if event["kind"] == "wire_error"), None)
    assert diagnostic is not None
    assert diagnostic["pending_hex"] == "01fd9f6b"
    assert diagnostic["expected_length"] == 8
    assert diagnostic["response_hex"] == ("" if before_command else "013600000100006b")
    assert caught.value.wire_diagnostic["pending_hex"] == "01fd9f6b"
    assert not bus.snapshot()["axes"]["X"]["telemetry_valid"]
    assert not any(frame[1] in (0xF3, 0xFD) for frame in serial.writes)
