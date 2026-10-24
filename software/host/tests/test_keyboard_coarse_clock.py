import copy
import math
import threading
import time
from types import SimpleNamespace

import pytest

from pa_host import emm_motion
from pa_host.emm_motion import MotionError
from test_keyboard_responsiveness import responsive_bus


def freeze_feedback_clock(monkeypatch, bus):
    observed = time.monotonic()
    for state in bus._state.values():
        state["updated_monotonic"] = observed
    monkeypatch.setattr(emm_motion, "time", SimpleNamespace(monotonic=lambda: observed,
                                                        time=time.time, sleep=time.sleep))
    return observed


def test_keyboard_preflight_accepts_distinct_replies_within_one_clock_tick(monkeypatch):
    bus, serial = responsive_bus()
    observed = freeze_feedback_clock(monkeypatch, bus)
    states = bus._keyboard_start_states("X", generation=bus._stop_generation)
    assert all(state["updated_monotonic"] == observed for state in states.values())
    assert not any(frame[1] in (0xFD, 0xF3) for frame in serial.writes)


def test_keyboard_stop_handoff_accepts_new_reply_within_same_clock_tick(monkeypatch):
    bus, serial = responsive_bus()
    observed = freeze_feedback_clock(monkeypatch, bus)
    samples = {axis: bus._read_axis(axis) for axis in bus.axes}
    bus._last_stop_samples = copy.deepcopy(samples)
    bus._last_stop = {"stop_confirmed": True}
    bus._last_stop_generation = bus._stop_generation
    bus._keyboard_segment_started = observed
    bus._last_stop_completed = observed
    states = bus._keyboard_stopped_states(generation=bus._stop_generation)
    assert all(state["updated_monotonic"] == observed for state in states.values())
    assert not any(frame[1] in (0xFD, 0xF3) for frame in serial.writes)


def test_keyboard_coarse_clock_never_reuses_same_cached_sample(monkeypatch):
    bus, serial = responsive_bus()
    freeze_feedback_clock(monkeypatch, bus)
    sample = bus._read_axis("X")
    assert not bus._keyboard_jog_pair(sample, copy.deepcopy(sample))


@pytest.mark.parametrize("change", [
    {"speed_rpm": 1}, {"enabled": False}, {"status": 1},
    {"input_target_count": 0}, {"position_error_count": 1000}, {"telemetry_valid": False},
])
def test_keyboard_same_tick_does_not_hide_unhealthy_or_changed_target_feedback(monkeypatch, change):
    bus, serial = responsive_bus()
    freeze_feedback_clock(monkeypatch, bus)
    previous = bus._read_axis("X")
    current = bus._read_axis("X")
    current.update(change)
    assert not bus._keyboard_jog_pair(previous, current)


@pytest.mark.parametrize("elapsed", [-0.1, 2.1])
def test_new_feedback_sequence_does_not_bypass_timestamp_order_or_expiry(monkeypatch, elapsed):
    bus, serial = responsive_bus()
    freeze_feedback_clock(monkeypatch, bus)
    previous = bus._read_axis("X")
    current = bus._read_axis("X")
    current["updated_monotonic"] += elapsed
    assert not bus._keyboard_jog_pair(previous, current)


@pytest.mark.parametrize("axis,direction", [("X", 0), ("X", 1), ("Z", 0), ("Z", 1)])
def test_keyboard_hold_and_stop_with_windows_sized_monotonic_ticks(monkeypatch, axis, direction):
    bus, serial = responsive_bus()
    monotonic = time.monotonic
    monkeypatch.setattr(emm_motion, "time", SimpleNamespace(
        monotonic=lambda: math.floor(monotonic() * 64) / 64, time=time.time, sleep=time.sleep))
    cancellation = threading.Event()
    serial.on_fast_move = lambda address: cancellation.set()
    with pytest.raises(MotionError) as caught:
        bus.alignment_fast_hold(axis, direction, 20, 20, None, None, cancel_event=cancellation)
    assert caught.value.code == "cancelled"
    assert caught.value.stop_result["stop_confirmed"] is True
    assert not bus.snapshot()["faulted"]
    assert len([frame for frame in serial.writes if frame[1] == 0xFD]) == 1
    assert not any(frame[1] in (0xF3, 0xF6, 0x0A, 0x9A) for frame in serial.writes)
