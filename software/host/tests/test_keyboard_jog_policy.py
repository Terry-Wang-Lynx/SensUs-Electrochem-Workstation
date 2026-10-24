import pytest

from pa_host.emm_motion import MotionError
from test_keyboard_post_stop_settling import stopped_tail_bus
from test_keyboard_fast_jog import WireClock


def test_keyboard_handoff_does_not_wait_for_single_count_precision():
    bus, serial, cancellation, tail = stopped_tail_bus(offsets=(139, 107, 77, 55, 25, 13, 13, 5, 5))
    stop_samples = []
    bus._callback = lambda event: stop_samples.append(len(tail["samples"])) if event["kind"] == "stopped" else None
    with pytest.raises(MotionError) as caught:
        bus.alignment_fast_hold("X", 0, 20, 20, None, None, cancel_event=cancellation)
    assert caught.value.code == "cancelled"
    assert stop_samples == [2]
    assert len(tail["samples"]) == 3
    assert caught.value.stop_result["axes"]["X"]["stop_basis"] == "keyboard_position_window"
    assert not bus.snapshot()["faulted"]


@pytest.mark.parametrize("transaction_latency", [0, 0.04])
def test_global_stop_keeps_single_count_confirmation_even_with_keyboard_session(monkeypatch, transaction_latency):
    bus, serial, cancellation, tail = stopped_tail_bus(offsets=(139, 107, 77, 55, 25, 13, 13, 5, 5))
    bus._timeout = 1.0
    clock = WireClock()
    monkeypatch.setattr("pa_host.emm_motion.time", clock)
    original_write = serial.write

    def write(frame):
        clock.sleep(transaction_latency)
        return original_write(frame)

    serial.write = write
    result = bus.stop_all()
    assert result["stop_confirmed"]
    assert len(tail["samples"]) == 7
    assert abs(tail["samples"][-1] - tail["samples"][-2]) <= 1
    assert result["axes"]["X"]["stop_basis"] == "acknowledged_stop"
    assert not any(frame[1] == 0xFD for frame in serial.writes)
