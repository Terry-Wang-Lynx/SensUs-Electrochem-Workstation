#!/usr/bin/env python3
"""Tests for the two-phase (reduction transient → oxidation steady state) tracker.

Sign convention under test: ``currents`` are firmware **reduction** currents in
nA, so ``> 0`` is the offset-limited direction and ``< 0`` is the device-native
oxidation direction that carries our signal at E = +200 mV.
"""

from __future__ import annotations

from pa_host.gui_server import SETTLE_WINDOW_S, _transient_phase


def _ramp(n: int, start: float, end: float, dt: float = 0.124,
          valid: list[bool] | None = None):
    times = [i * dt for i in range(n)]
    currents = [start + (end - start) * i / (n - 1) for i in range(n)]
    return times, currents, valid if valid is not None else [True] * n


def test_empty_input_is_idle() -> None:
    assert _transient_phase([], [], [])["phase"] == "idle"


def test_still_positive_is_the_reduction_transient() -> None:
    phase = _transient_phase(*_ramp(400, 500.0, 25.0))
    assert phase["phase"] == "reduction"
    assert phase["crossed_at_s"] is None
    assert phase["since_cross_s"] is None
    assert phase["ready"] is False


def test_crossing_is_taken_after_the_last_non_negative_sample() -> None:
    """Not the *first* crossing: noise near zero re-crosses many times.

    Real run r12 straddled zero 25 times; anchoring on the first crossing
    reported settling about 7 s early.
    """
    times = [0.0, 1.0, 2.0, 3.0, 4.0, 5.0]
    currents = [5.0, -1.0, 2.0, -3.0, -4.0, -5.0]   # 早期来回穿零
    phase = _transient_phase(times, currents, [True] * 6)
    assert phase["phase"] == "oxidation"
    assert phase["crossed_at_s"] == 3.0              # 最后一个非负样本(t=2)之后
    assert phase["since_cross_s"] == 2.0


def test_settled_oxidation_is_ready() -> None:
    n = 400
    times, currents, valid = _ramp(n, -8.0, -8.05)   # ~0.4 pA/s
    phase = _transient_phase(times, currents, valid)
    assert phase["phase"] == "oxidation"
    assert abs(phase["drift_pa_s"]) < phase["drift_threshold_pa_s"]
    assert phase["ready"] is True


def test_fast_drift_blocks_ready() -> None:
    times, currents, valid = _ramp(400, -1.0, -8.0)  # 数 nA / 数十秒
    phase = _transient_phase(times, currents, valid)
    assert phase["phase"] == "oxidation"
    assert abs(phase["drift_pa_s"]) > phase["drift_threshold_pa_s"]
    assert phase["ready"] is False


def test_railed_sample_inside_the_settle_window_blocks_ready() -> None:
    """A rail inside the window means potential control lapsed there.

    Regression from real run r10: 59% of its samples were railed, yet its tail
    was quiet enough that a drift-only criterion called it ready.
    """
    n = 400
    times, currents, valid = _ramp(n, -8.0, -8.05)
    last_in_window = next(i for i in range(n) if times[i] >= times[-1] - SETTLE_WINDOW_S)
    valid[last_in_window + 1] = False
    phase = _transient_phase(times, currents, valid)
    assert phase["window_railed"] == 1
    assert phase["ready"] is False


def test_rails_outside_the_window_are_reported_but_do_not_block() -> None:
    n = 400
    times, currents, valid = _ramp(n, -8.0, -8.05)
    for i in range(60):                              # 早段撞轨,末窗干净
        valid[i] = False
    phase = _transient_phase(times, currents, valid)
    assert phase["window_railed"] == 0
    assert phase["railed_samples"] == 60
    assert phase["ready"] is True
