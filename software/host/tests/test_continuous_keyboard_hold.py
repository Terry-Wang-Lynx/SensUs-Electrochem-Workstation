import threading
from types import SimpleNamespace

import pytest

from pa_host import keyboard_fast_jog
from pa_host.emm_motion import MotionError
from test_alignment_jog import command, jogging, start
from test_keyboard_fast_jog import fast_bus


def test_fine_hold_keeps_running_past_old_time_and_distance_limits_with_heartbeats():
    manager, motion, clock = jogging()
    start(manager)
    for sequence in range(1, 601):
        clock.now = sequence / 10
        manager.heartbeat(command(sequence=sequence))
        manager._step_once()
    assert manager.snapshot()["status"] == "holding"
    assert manager.snapshot()["distance_mm"] > 10
    assert motion.stops == 0
    assert manager.snapshot()["settings"]["max_hold_s"] is None
    assert manager.snapshot()["settings"]["max_hold_mm"] is None
    manager.stop(command())
    assert motion.stops == 1
    assert manager.snapshot()["status"] == "idle"


@pytest.mark.parametrize("direction", [0, 1])
def test_continuous_fast_hold_crosses_10mm_and_30s_without_intermediate_stop(monkeypatch, direction):
    bus, serial = fast_bus()
    cancellation = threading.Event()
    elapsed = [0.0]
    origin = serial.positions[1]

    def moved(address):
        assert bus.snapshot()["manual"]["polling_axis"] == "X"
        elapsed[0] += 11
        if abs(serial.positions[address] - origin) * 2 / 65536 >= 40:
            cancellation.set()

    serial.on_fast_move = moved
    monkeypatch.setattr(keyboard_fast_jog, "time", SimpleNamespace(monotonic=lambda: elapsed[0]))
    with pytest.raises(MotionError) as caught:
        bus.alignment_fast_hold("X", direction, 20, 20, None, None, cancel_event=cancellation)
    assert caught.value.code == "cancelled"
    assert caught.value.stop_result["stop_confirmed"] is True
    assert elapsed[0] > 30
    assert abs(serial.positions[1] - origin) * 2 / 65536 >= 40
    assert max(serial.lookahead) <= 6
    last_move = max(index for index, frame in enumerate(serial.writes) if frame[1] == 0xFD)
    assert not any(frame[1] == 0xFE for frame in serial.writes[:last_move])
    assert not any(frame[1] in (0xF3, 0xF6, 0x0A, 0x9A) for frame in serial.writes)
    assert bus.snapshot()["faulted"] is False
    assert "polling_axis" not in bus.snapshot()["manual"]


def test_continuous_fast_hold_requires_an_owner_cancellation_signal():
    bus, serial = fast_bus()
    with pytest.raises(MotionError) as caught:
        bus.alignment_fast_hold("X", 0, 20, 20, None, None)
    assert caught.value.code == "alignment_cancellation_required"
    assert not any(frame[1] == 0xFD for frame in serial.writes)


@pytest.mark.parametrize("direction", [0, 1])
def test_continuous_fast_hold_retains_the_existing_directional_boundary(direction):
    bus, serial = fast_bus()
    sign = -1 if direction else 1
    origin = serial.positions[1]
    remaining = 3 * 65536 / 2
    bus._manual["direction_bounds"] = {"X": {"lower" if direction else "upper": origin + sign * round(remaining)}}
    cancellation = threading.Event()
    deadline = threading.Timer(2, cancellation.set)
    deadline.start()
    try:
        result = bus.alignment_fast_hold("X", direction, 20, 20, None, None, cancel_event=cancellation)
    finally:
        deadline.cancel()
    assert result["stop_reason"] == "travel_limit"
    assert 2.8 <= sign * (serial.positions[1] - origin) * 2 / 65536 <= 3
    assert bus.snapshot()["faulted"] is False
