"""I-T workflow: platform detection, steady-current extraction and calibration.

The firmware emits timestamped native samples at about 8.06 Hz and the host can
resample that trace to a fixed output rate.  This module keeps the scientific
workflow deliberately small and explicit:

1. discard rows marked saturated/invalid;
2. average the final 20 s of a run to obtain one steady-current point;
3. fit current as a function of concentration;
4. invert the fitted curve for unknown-sample prediction.

The current sign is preserved.  In particular, ``fa_fw`` is converted from fA
to nA without taking an absolute value, so the same model can be used for
oxidation or reduction measurements.
"""

from __future__ import annotations

import csv
import json
import math
import warnings
from dataclasses import asdict, dataclass
from numbers import Real
from pathlib import Path
from typing import Iterable, Sequence

import numpy as np

from .filtering import apply_filter
from .textio import read_csv_lines, read_text_tolerant


DEFAULT_WINDOW_S = 20.0
DEFAULT_DURATION_S = 180.0
DEFAULT_SAMPLE_RATE_HZ = 10.0


@dataclass(frozen=True)
class PlateauConfig:
    """Validated parameters for automatic platform detection."""

    segment_duration_s: float = 5.0
    segment_count: int = 6
    absolute_tolerance_nA: float = 0.10
    relative_tolerance: float = 0.01
    scatter_multiplier: float = 3.0
    minimum_coverage_ratio: float = 0.60
    maximum_gap_periods: float = 2.5
    required_consecutive_windows: int = 2
    spike_scale_multiplier: float = 7.0
    spike_neighbor_multiplier: float = 3.0

    def __post_init__(self) -> None:
        numeric_ranges = {
            "segment_duration_s": (0.5, 60.0, True),
            "absolute_tolerance_nA": (0.0, 1000.0, True),
            "relative_tolerance": (0.0, 1.0, True),
            "scatter_multiplier": (0.0, 100.0, True),
            "minimum_coverage_ratio": (0.0, 1.0, False),
            "maximum_gap_periods": (0.0, 100.0, False),
            "spike_scale_multiplier": (0.0, 1000.0, False),
            "spike_neighbor_multiplier": (0.0, 1000.0, False),
        }
        for name, (lower, upper, include_lower) in numeric_ranges.items():
            raw = getattr(self, name)
            if isinstance(raw, bool) or not isinstance(raw, Real):
                raise ValueError(f"平台参数 {name} 必须是数值")
            value = float(raw)
            if not math.isfinite(value):
                raise ValueError(f"平台参数 {name} 必须是有限数")
            in_range = lower <= value <= upper if include_lower else lower < value <= upper
            if not in_range:
                opening = "[" if include_lower else "("
                raise ValueError(
                    f"平台参数 {name} 必须在 {opening}{lower:g}, {upper:g}] 范围内"
                )
            object.__setattr__(self, name, value)

        for name, lower, upper in (
            ("segment_count", 2, 60),
            ("required_consecutive_windows", 1, 100),
        ):
            raw = getattr(self, name)
            if (
                isinstance(raw, bool)
                or not isinstance(raw, Real)
                or not math.isfinite(float(raw))
                or float(raw) != math.trunc(float(raw))
            ):
                raise ValueError(f"平台参数 {name} 必须是整数")
            value = int(raw)
            if not lower <= value <= upper:
                raise ValueError(
                    f"平台参数 {name} 必须在 [{lower}, {upper}] 范围内"
                )
            object.__setattr__(self, name, value)

        if self.segment_count % 2:
            raise ValueError("平台参数 segment_count 必须是偶数")
        if self.minimum_stop_duration_s + 1e-12 < DEFAULT_WINDOW_S:
            raise ValueError(
                f"自动停止最短数据时长不得少于末段拟合窗口 "
                f"{DEFAULT_WINDOW_S:g} 秒；请增大分段时长、分段数量或连续通过窗"
            )

    @property
    def window_duration_s(self) -> float:
        return self.segment_duration_s * self.segment_count

    @property
    def minimum_stop_duration_s(self) -> float:
        return self.window_duration_s + (
            self.required_consecutive_windows - 1
        ) * self.segment_duration_s

    def to_dict(self) -> dict[str, float | int]:
        return asdict(self)

    @classmethod
    def validate(
        cls, value: PlateauConfig | dict[str, object] | None = None,
    ) -> PlateauConfig:
        if value is None:
            return cls()
        if isinstance(value, cls):
            return value
        if not isinstance(value, dict):
            raise ValueError("平台参数必须是 JSON 对象")
        allowed = set(cls.__dataclass_fields__)
        unknown = set(value) - allowed
        if unknown:
            names = ", ".join(sorted(str(name) for name in unknown))
            raise ValueError(f"未知平台参数: {names}")
        return cls(**value)


# Compatibility constants for callers that still import the historical names.
PLATEAU_SEGMENT_S = PlateauConfig.segment_duration_s
PLATEAU_SEGMENTS = PlateauConfig.segment_count
PLATEAU_WINDOW_S = PLATEAU_SEGMENT_S * PLATEAU_SEGMENTS
PLATEAU_ABSOLUTE_TOLERANCE_NA = PlateauConfig.absolute_tolerance_nA
PLATEAU_RELATIVE_TOLERANCE = PlateauConfig.relative_tolerance


@dataclass(frozen=True)
class CalibrationPoint:
    concentration_um: float
    current_nA: float
    label: str = ""


@dataclass(frozen=True)
class RunSummary:
    path: str
    sample_count: int
    valid_count: int
    fit_count: int
    sample_rate_hz: float
    duration_s: float
    window_s: float
    steady_current_nA: float | None
    steady_sd_nA: float | None
    first_fit_time_s: float | None
    last_fit_time_s: float | None
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class PlateauEvaluation:
    """One completed platform decision window."""

    complete_segment: int
    window_start_s: float
    window_end_s: float
    stable: bool
    reason: str
    segment_means_nA: tuple[float, ...] = ()
    slope_nA_per_s: float | None = None
    delta_30s_nA: float | None = None
    delta_half_nA: float | None = None
    median_current_nA: float | None = None
    segment_scatter_nA: float | None = None
    tolerance_nA: float | None = None
    isolated_spikes_removed: int = 0
    filter_meta: dict[str, object] | None = None
    status: str = "unstable"
    fit_intercept_nA: float | None = None
    trend_delta_nA: float | None = None
    first_half_mean_nA: float | None = None
    second_half_mean_nA: float | None = None
    half_delta_signed_nA: float | None = None
    segment_centres_s: tuple[float, ...] = ()
    config: PlateauConfig | None = None


@dataclass(frozen=True)
class CalibrationModel:
    """Polynomial current-vs-concentration calibration model.

    ``coefficients`` follow ``numpy.polyval`` order (highest power first).
    Linear degree 1 is the default and is normally the safest choice for a
    small number of electrochemical calibration points.
    """

    degree: int
    coefficients: tuple[float, ...]
    concentration_min_um: float
    concentration_max_um: float
    r2: float
    rmse_nA: float
    n_points: int

    def current_from_concentration(self, concentration_um: float | Sequence[float]):
        return np.polyval(np.asarray(self.coefficients), concentration_um)

    def predict_concentration(self, current_nA: float) -> float:
        """Invert the calibration curve for one current value.

        For a linear model this is analytic.  For a polynomial model, real
        roots within the calibration concentration interval are considered;
        if there are multiple candidates, the one nearest the interval centre
        is selected and the caller should treat the result as ambiguous.
        """

        current = float(current_nA)
        if not math.isfinite(current):
            raise ValueError("current_nA must be finite")
        coeff = np.asarray(self.coefficients, dtype=float).copy()
        coeff[-1] -= current
        if self.degree == 1:
            if abs(coeff[0]) < 1e-15:
                raise ValueError("calibration slope is zero; prediction is undefined")
            return float(-coeff[1] / coeff[0])

        lo, hi = self.concentration_min_um, self.concentration_max_um
        if self.degree == 2 and len(coeff) == 3:
            a, b, c = (float(value) for value in coeff)
            if abs(a) < 1e-15:
                roots = [] if abs(b) < 1e-15 else [-c / b]
            else:
                discriminant = b * b - 4.0 * a * c
                roots = (
                    [] if discriminant < 0.0
                    else [
                        (-b + math.sqrt(discriminant)) / (2.0 * a),
                        (-b - math.sqrt(discriminant)) / (2.0 * a),
                    ]
                )
            candidates = [
                float(root) for root in roots
                if lo - 1e-9 <= root <= hi + 1e-9
            ]
        else:
            roots = np.roots(coeff)
            candidates = [
                float(root.real) for root in roots
                if abs(float(root.imag)) < 1e-8
                and lo - 1e-9 <= root.real <= hi + 1e-9
            ]
        if not candidates:
            raise ValueError(
                f"current {current:g} nA has no real concentration root in "
                f"[{lo:g}, {hi:g}] umol/L"
            )
        centre = (lo + hi) / 2.0
        return min(candidates, key=lambda value: (abs(value - centre), -value))

    def to_json(self) -> dict[str, object]:
        return {
            "model": "current_vs_concentration",
            "degree": self.degree,
            "coefficients": list(self.coefficients),
            "concentration_min_um": self.concentration_min_um,
            "concentration_max_um": self.concentration_max_um,
            "r2": self.r2,
            "rmse_nA": self.rmse_nA,
            "n_points": self.n_points,
            "current_unit": "nA",
            "concentration_unit": "umol/L",
        }

    @classmethod
    def from_json(cls, payload: dict[str, object]) -> "CalibrationModel":
        if payload.get("model") != "current_vs_concentration":
            raise ValueError("unsupported calibration model")
        return cls(
            degree=int(payload["degree"]),
            coefficients=tuple(float(x) for x in payload["coefficients"]),
            concentration_min_um=float(payload["concentration_min_um"]),
            concentration_max_um=float(payload["concentration_max_um"]),
            r2=float(payload["r2"]),
            rmse_nA=float(payload["rmse_nA"]),
            n_points=int(payload["n_points"]),
        )


AP_SAMPLE_COUNT = 24
AP_STREAK_THRESHOLDS = (
    (24.0, 10.0), (21.0, 8.0), (18.0, 6.0), (15.0, 4.5),
    (12.0, 3.0), (10.0, 2.0), (8.0, 1.5), (6.0, 1.0),
    (4.0, 0.5),
)


def _ap_point_score(true_um: float, measured_um: float) -> dict[str, float | str | None]:
    """Classify one ETE sample and return its score details.

    Green-zone points receive a linear score from 1 at the Blue boundary to
    0 at the Green boundary.  The ETE specification defines the endpoints and
    ordering, but not a different interpolation rule.
    """
    if true_um < 10.0:
        error = abs(measured_um - true_um)
        blue_limit, green_limit = 2.0, 4.0
        in_green = max(0.0, true_um - green_limit) <= measured_um <= true_um + green_limit
        relative_error = None if true_um == 0 else error / true_um * 100.0
    else:
        relative = abs(measured_um - true_um) / true_um
        error = abs(measured_um - true_um)
        blue_limit, green_limit = 0.20, 0.40
        in_green = 0.60 * true_um <= measured_um <= 1.40 * true_um
        relative_error = relative * 100.0

    if (true_um < 10.0 and error <= blue_limit) or (
        true_um >= 10.0 and error / true_um <= blue_limit
    ):
        zone, score = "blue", 1.0
    elif in_green:
        distance = error if true_um < 10.0 else error / true_um
        score = max(0.0, min(1.0, 1.0 - (distance - blue_limit) / (green_limit - blue_limit)))
        zone = "green"
    else:
        zone, score = "grey", 0.0

    return {
        "zone": zone,
        "score": float(score),
        "absolute_error_um": float(error),
        "error_percent": relative_error,
    }


def evaluate_ap_score(points: Sequence[dict[str, object]],
                      sample_count: int = AP_SAMPLE_COUNT) -> dict[str, object]:
    """Calculate the July IP ETE score for ordered test points.

    The denominator for MS is always 24, even when fewer samples have been
    measured.  Points beyond the first 24 remain useful for the chart/statistics
    but do not change the ETE score.
    """
    if sample_count <= 0:
        raise ValueError("AP 样品总数必须为正数")
    prepared: list[dict[str, object]] = []
    for index, point in enumerate(points, 1):
        try:
            true_um = float(point.get("concentration_um"))
        except (AttributeError, TypeError, ValueError):
            continue
        if not math.isfinite(true_um) or true_um < 0:
            continue
        try:
            measured_value = point.get(
                "predicted_concentration_um",
                point.get("measured_concentration_um"),
            )
            measured_um = float(measured_value)
        except (AttributeError, TypeError, ValueError):
            measured_um = math.nan
        if math.isfinite(measured_um):
            detail = _ap_point_score(true_um, measured_um)
            prepared.append({"sequence": index, "concentration_um": true_um,
                             "measured_concentration_um": measured_um, **detail})
        else:
            # A completed sample with no real inverse (for example, an
            # outlying quadratic current) is still a measured sample. It
            # scores zero and breaks serial consistency rather than silently
            # disappearing from the 24-sample sequence.
            prepared.append({"sequence": index, "concentration_um": true_um,
                             "measured_concentration_um": None, "zone": "grey",
                             "score": 0.0, "absolute_error_um": None,
                             "error_percent": None})

    scoring_points = prepared[:sample_count]
    score_sum = sum(float(point["score"]) for point in scoring_points)
    current_streak = 0.0
    longest_streak = 0.0
    for point in scoring_points:
        weight = 1.0 if point["zone"] == "blue" else 0.5 if point["zone"] == "green" else 0.0
        if weight == 0.0:
            current_streak = 0.0
        else:
            current_streak += weight
            longest_streak = max(longest_streak, current_streak)
    serial_score = next((score for threshold, score in AP_STREAK_THRESHOLDS
                         if longest_streak >= threshold), 0.0)

    abs_errors = [float(point["absolute_error_um"]) for point in prepared
                  if point["absolute_error_um"] is not None]
    percent_errors = [float(point["error_percent"]) for point in prepared
                      if point["error_percent"] is not None]
    signed_errors = [float(point["measured_concentration_um"])
                     - float(point["concentration_um"]) for point in prepared
                     if point["measured_concentration_um"] is not None]
    stats = {
        "measured_count": len(prepared),
        "scored_count": len(scoring_points),
        "blue_count": sum(point["zone"] == "blue" for point in prepared),
        "green_count": sum(point["zone"] == "green" for point in prepared),
        "grey_count": sum(point["zone"] == "grey" for point in prepared),
        "mean_absolute_error_um": sum(abs_errors) / len(abs_errors) if abs_errors else None,
        "rmse_um": math.sqrt(sum(error * error for error in abs_errors) / len(abs_errors)) if abs_errors else None,
        "mean_absolute_error_percent": sum(percent_errors) / len(percent_errors) if percent_errors else None,
        "max_absolute_error_um": max(abs_errors) if abs_errors else None,
        "max_absolute_error_percent": max(percent_errors) if percent_errors else None,
        "mean_signed_error_um": sum(signed_errors) / len(signed_errors) if signed_errors else None,
    }
    ms = 10.0 * score_sum / sample_count
    final_score = 100.0 + 5.0 * (ms + serial_score)
    return {
        "sample_count": sample_count,
        "points": prepared,
        "stats": stats,
        "longest_weighted_streak": longest_streak,
        "ms": ms,
        "sc": serial_score,
        "final_score": final_score,
    }


def _finite(value: str | float | int | None) -> float | None:
    if value is None or value == "":
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _pick(row: dict[str, str], names: Iterable[str]) -> str | None:
    for name in names:
        if name in row and row[name] != "":
            return row[name]
    return None


def detect_isolated_spikes(
    current_nA: Sequence[float], valid: Sequence[bool],
    spike_scale_multiplier: float = 7.0,
    spike_neighbor_multiplier: float = 3.0,
) -> np.ndarray:
    """Return a conservative mask for isolated one-sample current impulses.

    A point is flagged only when it is far from both neighbours on a robust,
    whole-trace scale *and* the two neighbours agree substantially better with
    each other.  A sustained physical step therefore remains valid.  The raw
    CSV is never modified; this mask is analysis metadata only.
    """

    for name, raw in (
        ("spike_scale_multiplier", spike_scale_multiplier),
        ("spike_neighbor_multiplier", spike_neighbor_multiplier),
    ):
        if (
            isinstance(raw, bool)
            or not isinstance(raw, Real)
            or not math.isfinite(float(raw))
            or not 0.0 < float(raw) <= 1000.0
        ):
            raise ValueError(f"平台参数 {name} 必须是 (0, 1000] 内的有限数")

    current = np.asarray(current_nA, dtype=float)
    usable = np.asarray(valid, dtype=bool) & np.isfinite(current)
    flagged = np.zeros(len(current), dtype=bool)
    if len(current) < 5 or int(usable.sum()) < 5:
        return flagged

    adjacent = usable[:-1] & usable[1:]
    differences = np.diff(current)[adjacent]
    if not len(differences):
        return flagged
    difference_centre = float(np.median(differences))
    robust_scale = 1.4826 * float(np.median(np.abs(differences - difference_centre)))
    robust_scale = max(robust_scale, np.finfo(float).eps)

    neighbours_valid = usable[:-2] & usable[1:-1] & usable[2:]
    neighbour_midpoint = (current[:-2] + current[2:]) / 2.0
    residual = np.abs(current[1:-1] - neighbour_midpoint)
    neighbour_gap = np.abs(current[2:] - current[:-2])
    local_spike = (
        neighbours_valid
        & (residual > float(spike_scale_multiplier) * robust_scale)
        & (
            residual
            > float(spike_neighbor_multiplier) * (neighbour_gap + robust_scale)
        )
    )
    flagged[1:-1] = local_spike
    return flagged


def evaluate_platform(
    time_s: Sequence[float], current_nA: Sequence[float], valid: Sequence[bool],
    filter_config: dict[str, object] | None = None,
    expected_sample_rate_hz: float | None = None,
    config: PlateauConfig | dict[str, object] | None = None,
    decision_segment: int | None = None,
) -> PlateauEvaluation | None:
    """Evaluate one complete configured platform window.

    By default the newest complete window is used. ``decision_segment`` lets a
    polling caller replay missed windows in order without exposing later data
    to an earlier decision. ``None`` means the requested window is unavailable.
    Saturated or otherwise invalid samples reject the whole decision window.
    Isolated one-sample impulses are removed conservatively before filtering
    and do not by themselves reject an otherwise healthy window.
    """

    plateau_config = PlateauConfig.validate(config)
    t = np.asarray(time_s, dtype=float)
    current = np.asarray(current_nA, dtype=float)
    valid_arr = np.asarray(valid, dtype=bool)
    if any(array.ndim != 1 for array in (t, current, valid_arr)):
        raise ValueError("平台判定输入必须是一维序列")
    if len({len(t), len(current), len(valid_arr)}) != 1:
        raise ValueError("平台判定输入 time/current/valid 长度不一致")
    if expected_sample_rate_hz is not None:
        expected_sample_rate_hz = float(expected_sample_rate_hz)
        if not math.isfinite(expected_sample_rate_hz) or expected_sample_rate_hz <= 0:
            raise ValueError("平台判定额定采样率必须是正的有限数")
    finite = np.isfinite(t) & np.isfinite(current)
    if not np.any(finite):
        return None

    segment_duration_s = plateau_config.segment_duration_s
    segment_count = plateau_config.segment_count
    window_duration_s = plateau_config.window_duration_s
    latest_complete_segment = int(math.floor(
        float(np.max(t[finite])) / segment_duration_s
    ))
    if decision_segment is None:
        complete_segment = latest_complete_segment
    else:
        if (
            isinstance(decision_segment, bool)
            or not isinstance(decision_segment, Real)
            or not math.isfinite(float(decision_segment))
            or float(decision_segment) != math.trunc(float(decision_segment))
            or int(decision_segment) < 0
        ):
            raise ValueError("平台判定 decision_segment 必须是非负整数")
        complete_segment = int(decision_segment)
        if complete_segment > latest_complete_segment:
            return None
    window_end = (
        complete_segment * segment_duration_s
    )
    if window_end < window_duration_s:
        return None
    window_start = window_end - window_duration_s
    in_window = finite & (t >= window_start) & (t < window_end)
    if not np.any(in_window):
        return PlateauEvaluation(
            complete_segment, window_start, window_end, False,
            "判定窗口内没有数据", config=plateau_config,
        )
    if np.any(in_window & ~valid_arr):
        return PlateauEvaluation(
            complete_segment, window_start, window_end, False,
            "判定窗口含饱和或无效点", config=plateau_config,
        )

    # A completed decision window must have one deterministic result. The
    # zero-phase analysis filter and robust spike scale may use earlier history,
    # but must never see samples after this window's end; otherwise scheduler
    # jitter changes an already-completed window retroactively.
    history = finite & (t < window_end)
    history_t = t[history]
    history_current = current[history]
    history_valid = valid_arr[history]
    history_in_window = history_t >= window_start
    nominal_rate_hz = expected_sample_rate_hz
    if nominal_rate_hz is None:
        ordered_times = np.sort(history_t)
        positive_intervals = np.diff(ordered_times)
        positive_intervals = positive_intervals[positive_intervals > 0]
        if len(positive_intervals):
            nominal_rate_hz = 1.0 / float(np.median(positive_intervals))

    spike_mask = detect_isolated_spikes(
        history_current, history_valid,
        plateau_config.spike_scale_multiplier,
        plateau_config.spike_neighbor_multiplier,
    )
    analysis_valid = history_valid & ~spike_mask
    filtered, filter_meta = apply_filter(
        history_t, history_current, analysis_valid, filter_config,
    )
    spikes_removed = int((spike_mask & history_in_window).sum())
    minimum_segment_samples = (
        max(1, int(math.ceil(
            segment_duration_s
            * nominal_rate_hz
            * plateau_config.minimum_coverage_ratio
        )))
        if nominal_rate_hz is not None else 2
    )
    maximum_interval_s = (
        plateau_config.maximum_gap_periods / nominal_rate_hz
        if nominal_rate_hz is not None else None
    )
    segment_means: list[float] = []
    for index in range(segment_count):
        start = window_start + index * segment_duration_s
        stop = start + segment_duration_s
        selected = analysis_valid & (history_t >= start) & (history_t < stop)
        selected_count = int(selected.sum())
        if selected_count < minimum_segment_samples:
            return PlateauEvaluation(
                complete_segment, window_start, window_end, False,
                f"第 {index + 1} 个 {segment_duration_s:g} 秒段有效点不足（"
                f"{selected_count}/{minimum_segment_samples}）",
                isolated_spikes_removed=spikes_removed,
                filter_meta=filter_meta,
                config=plateau_config,
            )
        if maximum_interval_s is not None and selected_count >= 2:
            selected_times = np.sort(history_t[selected])
            largest_interval = float(np.max(np.diff(selected_times)))
            if largest_interval > maximum_interval_s + 1e-12:
                return PlateauEvaluation(
                    complete_segment, window_start, window_end, False,
                    f"第 {index + 1} 个 {segment_duration_s:g} 秒段采样间隔过大（"
                    f"{largest_interval:.4g} s > {maximum_interval_s:.4g} s）",
                    isolated_spikes_removed=spikes_removed,
                    filter_meta=filter_meta,
                    config=plateau_config,
                )
        segment_means.append(float(np.mean(filtered[selected])))

    if maximum_interval_s is not None:
        window_times = np.sort(history_t[analysis_valid & history_in_window])
        largest_interval = float(np.max(np.diff(window_times)))
        if largest_interval > maximum_interval_s + 1e-12:
            return PlateauEvaluation(
                complete_segment, window_start, window_end, False,
                f"判定窗口采样间隔过大（{largest_interval:.4g} s > "
                f"{maximum_interval_s:.4g} s）",
                isolated_spikes_removed=spikes_removed,
                filter_meta=filter_meta,
                config=plateau_config,
            )

    means = np.asarray(segment_means, dtype=float)
    centres = (
        window_start
        + (np.arange(segment_count) + 0.5) * segment_duration_s
    )
    slope, intercept = np.polyfit(centres, means, 1)
    residuals = means - (slope * centres + intercept)
    residual_centre = float(np.median(residuals))
    scatter = 1.4826 * float(np.median(np.abs(residuals - residual_centre)))
    median_current = float(np.median(means))
    tolerance = max(
        plateau_config.absolute_tolerance_nA,
        abs(median_current) * plateau_config.relative_tolerance,
        plateau_config.scatter_multiplier * scatter,
    )
    trend_delta = float(slope) * window_duration_s
    half_index = segment_count // 2
    first_half_mean = float(np.mean(means[:half_index]))
    second_half_mean = float(np.mean(means[half_index:]))
    half_delta_signed = second_half_mean - first_half_mean
    delta_30s = abs(trend_delta)
    delta_half = abs(half_delta_signed)
    stable = delta_30s <= tolerance and delta_half <= tolerance
    return PlateauEvaluation(
        complete_segment=complete_segment,
        window_start_s=window_start,
        window_end_s=window_end,
        stable=stable,
        reason="平台判定通过" if stable else "末段仍有趋势",
        status="stable" if stable else "unstable",
        segment_means_nA=tuple(segment_means),
        slope_nA_per_s=float(slope),
        delta_30s_nA=delta_30s,
        delta_half_nA=delta_half,
        median_current_nA=median_current,
        segment_scatter_nA=scatter,
        tolerance_nA=tolerance,
        isolated_spikes_removed=spikes_removed,
        filter_meta=filter_meta,
        fit_intercept_nA=float(intercept),
        trend_delta_nA=trend_delta,
        first_half_mean_nA=first_half_mean,
        second_half_mean_nA=second_half_mean,
        half_delta_signed_nA=half_delta_signed,
        segment_centres_s=tuple(float(value) for value in centres),
        config=plateau_config,
    )


# 🔴 原先这里有个本地 `_read_csv_lines`:只有两级回退(缺 errors="replace" 兜底 ⇒
#    既非 UTF-8 也非 GBK 系的文件仍会抛),而且不返回真正用到的编码 ⇒ 调用方无法
#    提示用户"这份是旧编码"。已删除,统一走 `textio.read_csv_lines`
#    (filtering.py 里那份同源副本本轮不动,见交接说明)。


def _load_run_csv_with_quality(
        path: str | Path) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Load ``time_s``, signed current in nA, and validity from a run CSV.

    Accepted current columns are ``current_nA``, ``reduction_current_nA`` and
    firmware ``fa_fw`` (fA).  The latter is converted to nA.  ``sat != 0``,
    ``ovf != 0`` or an explicit ``valid == 0`` excludes a row.
    """

    path = Path(path)
    # ✅ 用户数据:run CSV 落在用户自己的批次目录里。
    rows = list(csv.DictReader(
        line for line in read_csv_lines(path)[0] if not line.startswith("#")
    ))
    if not rows:
        raise ValueError(f"run CSV has no data rows: {path}")

    times: list[float] = []
    currents: list[float] = []
    valid: list[bool] = []
    sequence: list[int | None] = []
    for row in rows:
        t = _finite(_pick(row, ("time_s", "dev_ms", "host_unix_s")))
        if t is None:
            continue
        if "dev_ms" in row and "time_s" not in row:
            t /= 1000.0
        current = _finite(_pick(row, ("current_nA", "reduction_current_nA")))
        if current is None:
            fa = _finite(row.get("fa_fw"))
            current = None if fa is None else fa / 1_000_000.0
        if current is None:
            continue
        sat = int(float(row.get("sat", "0") or 0))
        ovf = int(float(row.get("ovf", "0") or 0))
        row_valid = int(float(row.get("valid", "1") or 1)) != 0
        times.append(t)
        currents.append(current)
        valid.append(row_valid and sat == 0 and ovf == 0)
        try:
            sequence.append(int(float(row["seq"])))
        except (KeyError, TypeError, ValueError):
            sequence.append(None)

    if len(times) < 3:
        raise ValueError(f"run CSV has fewer than three usable rows: {path}")
    time_arr = np.asarray(times, dtype=float)
    # A single RTT file can contain a short pre-reset tail followed by the
    # requested run.  Firmware sequence numbers and device uptime both reset;
    # keep the final monotonic segment instead of mixing two measurements.
    breaks = [i for i in range(1, len(time_arr))
              if time_arr[i] < time_arr[i - 1]
              or (sequence[i] is not None and sequence[i - 1] is not None
                  and sequence[i] < sequence[i - 1])]
    if breaks:
        start = breaks[-1]
        time_arr = time_arr[start:]
        current_arr = np.asarray(currents, dtype=float)[start:]
        valid_arr = np.asarray(valid, dtype=bool)[start:]
    else:
        current_arr = np.asarray(currents, dtype=float)
        valid_arr = np.asarray(valid, dtype=bool)
    # Firmware timestamps may start after calibration/boot.  Use elapsed time.
    time_arr -= time_arr[0]
    if len(time_arr) < 3:
        raise ValueError(f"run CSV's final measurement segment has fewer than three rows: {path}")
    spike_mask = detect_isolated_spikes(current_arr, valid_arr)
    valid_arr &= ~spike_mask
    return time_arr, current_arr, valid_arr, spike_mask


def load_run_csv(path: str | Path) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Load elapsed time, signed current in nA, and the analysis-valid mask."""

    time_arr, current_arr, valid_arr, _ = _load_run_csv_with_quality(path)
    return time_arr, current_arr, valid_arr


def resample_run_10hz(path: str | Path, output: str | Path,
                      duration_s: float | None = DEFAULT_DURATION_S,
                      target_rate_hz: float = DEFAULT_SAMPLE_RATE_HZ) -> Path:
    """Resample a native run to a fixed-rate CSV for the requested workflow.

    The MAX30131 native period is about 124 ms on the fastest supported setting,
    so the board can provide roughly 8.06 Hz rather than ten independent samples
    per second.  This function interpolates the timestamped trace to exactly
    ``duration_s * target_rate_hz`` rows.  Saturated source rows remain marked
    invalid; the output is never silently promoted to a valid calibration point.
    """
    if (duration_s is not None and duration_s <= 0) or target_rate_hz <= 0:
        raise ValueError("duration_s and target_rate_hz must be positive")
    source_path = Path(path)
    output_path = Path(output)
    if source_path.resolve() == output_path.resolve():
        raise ValueError("重采样输出必须是新文件，不能覆盖原始采集文件")
    t, current, valid, _ = _load_run_csv_with_quality(source_path)
    n = (
        int(round(duration_s * target_rate_hz))
        if duration_s is not None
        else max(3, int(math.floor(float(t[-1]) * target_rate_hz)) + 1)
    )
    target_t = np.arange(n, dtype=float) / target_rate_hz
    source_rate = 1.0 / float(np.median(np.diff(t))) if len(t) > 1 else 0.0

    # Interpolate the measured signal where possible.  If every source sample
    # is invalid (e.g. a saturated zero sample), preserve the trace but mark all
    # resampled rows invalid so downstream fitting fails loudly.
    source_mask = np.isfinite(current)
    if not np.any(source_mask):
        raise ValueError("run CSV contains no finite current values")
    interp_t = t[source_mask]
    interp_current = current[source_mask]
    order = np.argsort(interp_t, kind="stable")
    interp_t = interp_t[order]
    interp_current = interp_current[order]
    resampled_current = np.interp(target_t, interp_t, interp_current)

    if np.any(valid):
        # A target point is valid only when both source samples bracketing its
        # interpolation interval are valid.  This preserves short saturation
        # gaps instead of filling them and accidentally enabling calibration.
        right = np.searchsorted(t, target_t, side="left")
        exact = (right < len(t)) & np.isclose(t[np.minimum(right, len(t) - 1)],
                                               target_t, rtol=0.0, atol=1e-12)
        left = np.clip(right - 1, 0, len(t) - 1)
        right_clipped = np.clip(right, 0, len(t) - 1)
        valid_domain = ((target_t >= t[0]) & (target_t <= t[-1]) &
                        valid[left] & valid[right_clipped])
        valid_domain |= exact & valid[right_clipped]
    else:
        valid_domain = np.zeros(n, dtype=bool)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8") as handle:
        handle.write("# Fixed-rate host resampling; native hardware timestamps retained in source run\n")
        handle.write(f"# source_rate_hz: {source_rate:.9f}\n")
        handle.write(f"# target_rate_hz: {target_rate_hz:.9f}\n")
        writer = csv.writer(handle)
        writer.writerow(["time_s", "current_nA", "valid", "sat", "source_rate_hz"])
        for ti, yi, ok in zip(target_t, resampled_current, valid_domain):
            writer.writerow([f"{ti:.9f}", f"{yi:.12g}", int(ok), 0 if ok else 3,
                             f"{source_rate:.9f}"])
    return output_path


def summarize_run(path: str | Path, window_s: float = DEFAULT_WINDOW_S,
                  expected_rate_hz: float = DEFAULT_SAMPLE_RATE_HZ) -> RunSummary:
    if window_s <= 0:
        raise ValueError("window_s must be positive")
    t, current, valid, spike_mask = _load_run_csv_with_quality(path)
    dt = np.diff(t)
    median_dt = float(np.median(dt))
    rate = 1.0 / median_dt if median_dt > 0 else 0.0
    warnings: list[str] = []
    if rate and abs(rate - expected_rate_hz) / expected_rate_hz > 0.05:
        warnings.append(f"sample rate {rate:.3f} Hz differs from requested "
                        f"{expected_rate_hz:.3f} Hz by >5%")
    if np.std(dt) / median_dt > 0.05:
        warnings.append("sample interval jitter exceeds 5%")
    invalid_count = len(valid) - int(valid.sum())
    if invalid_count:
        warnings.append(f"{invalid_count} samples marked invalid or saturated")
    spike_count = int(spike_mask.sum())
    if spike_count:
        warnings.append(
            f"{spike_count} isolated single-sample current impulses excluded; raw data preserved"
        )
    end = float(t[-1])
    start = max(float(t[0]), end - window_s)
    fit_mask = valid & (t >= start)
    fit_count = int(fit_mask.sum())
    if fit_count < 3:
        warnings.append("fewer than three valid samples in the final fitting window; "
                        "steady current is undefined")
        return RunSummary(
            path=str(path), sample_count=len(t), valid_count=int(valid.sum()),
            fit_count=fit_count, sample_rate_hz=rate, duration_s=end,
            window_s=window_s, steady_current_nA=None, steady_sd_nA=None,
            first_fit_time_s=None, last_fit_time_s=None, warnings=tuple(warnings),
        )
    selected = current[fit_mask]
    return RunSummary(
        path=str(path), sample_count=len(t), valid_count=int(valid.sum()),
        fit_count=len(selected), sample_rate_hz=rate, duration_s=end,
        window_s=window_s, steady_current_nA=float(np.mean(selected)),
        steady_sd_nA=float(np.std(selected, ddof=1)) if len(selected) > 1 else 0.0,
        first_fit_time_s=float(t[fit_mask][0]), last_fit_time_s=float(t[fit_mask][-1]),
        warnings=tuple(warnings),
    )


def load_calibration_points(path: str | Path) -> list[CalibrationPoint]:
    path = Path(path)
    # ✅ 用户数据:标定点 CSV,注释行/标签列可能带中文样品名。
    reader = csv.DictReader(
        line for line in read_csv_lines(path)[0] if not line.startswith("#")
    )
    if reader.fieldnames is None:
        raise ValueError("calibration CSV has no header")
    points: list[CalibrationPoint] = []
    for row in reader:
        concentration = _finite(_pick(row, (
            "concentration_um", "concentration_uM", "concentration", "ldopa_concentration_um")))
        current = _finite(_pick(row, (
            "current_nA", "current_na", "endpoint_current_nA", "endpoint_current_na",
            "steady_current_nA")))
        if concentration is None or current is None:
            raise ValueError("each calibration row needs concentration and current")
        points.append(CalibrationPoint(concentration, current, row.get("label", "")))
    if not points:
        raise ValueError("calibration CSV has no points")
    return points


def fit_calibration(points: Sequence[CalibrationPoint], degree: int = 1) -> CalibrationModel:
    if not 1 <= degree <= 10:
        raise ValueError("degree must be between 1 and 10")
    if len(points) < degree + 1:
        raise ValueError(f"degree {degree} needs at least {degree + 1} points")
    x = np.asarray([p.concentration_um for p in points], dtype=float)
    y = np.asarray([p.current_nA for p in points], dtype=float)
    if not np.all(np.isfinite(x)) or not np.all(np.isfinite(y)):
        raise ValueError("calibration data must be finite")
    if np.any(x < 0):
        raise ValueError("calibration concentrations must be non-negative")
    if np.ptp(x) <= 0:
        raise ValueError("calibration concentrations must span a nonzero range")
    if len(np.unique(x)) < degree + 1:
        raise ValueError(
            f"degree {degree} needs at least {degree + 1} distinct concentrations"
        )
    response_scale = max(1.0, float(np.max(np.abs(y))))
    if float(np.ptp(y)) <= 1e-12 * response_scale:
        raise ValueError("calibration current response is too small to invert reliably")

    rank_warning = getattr(getattr(np, "exceptions", np), "RankWarning", None)
    if rank_warning is None:  # NumPy < 2.0
        rank_warning = np.RankWarning
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", rank_warning)
            coeff = np.polyfit(x, y, degree)
    except rank_warning as exc:
        raise ValueError(
            "calibration fit is rank-deficient or poorly conditioned"
        ) from exc

    if degree >= 2:
        lo, hi = float(np.min(x)), float(np.max(x))
        boundary_tolerance = max(1.0, abs(lo), abs(hi)) * 1e-10
        derivative_roots = np.roots(np.polyder(coeff))
        critical = [lo]
        critical.extend(sorted(
            float(root.real) for root in derivative_roots
            if abs(float(root.imag)) < 1e-8
            and lo + boundary_tolerance < root.real < hi - boundary_tolerance
        ))
        critical.append(hi)
        distinct_critical = [critical[0]]
        for value in critical[1:]:
            if abs(value - distinct_critical[-1]) > boundary_tolerance:
                distinct_critical.append(value)
        critical_response = np.polyval(coeff, distinct_critical)
        response_deltas = np.diff(critical_response)
        monotonic_tolerance = max(
            1.0, float(np.max(np.abs(critical_response)))
        ) * 1e-12
        meaningful_deltas = response_deltas[
            np.abs(response_deltas) > monotonic_tolerance
        ]
        if not (
            len(meaningful_deltas)
            and (
                np.all(meaningful_deltas > 0)
                or np.all(meaningful_deltas < 0)
            )
        ):
            raise ValueError(
                "calibration curve must be monotonic over the calibration range"
            )
    prediction = np.polyval(coeff, x)
    residual = y - prediction
    ss_tot = float(np.sum((y - np.mean(y)) ** 2))
    r2 = 1.0 - float(np.sum(residual ** 2)) / ss_tot if ss_tot else 0.0
    return CalibrationModel(
        degree=degree, coefficients=tuple(float(v) for v in coeff),
        concentration_min_um=float(np.min(x)), concentration_max_um=float(np.max(x)),
        r2=r2, rmse_nA=float(np.sqrt(np.mean(residual ** 2))), n_points=len(points),
    )


def save_model(model: CalibrationModel, path: str | Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(model.to_json(), indent=2) + "\n", encoding="utf-8")


def load_model(path: str | Path) -> CalibrationModel:
    # ✅ 用户数据:calibration-model.json 就在用户的工作区目录里,`_load_workspace()`
    #    加载工作区时会读它(gui_server 的 paths["model"])。
    # 🔴 这一处的失败样子特别隐蔽:UnicodeDecodeError 是 ValueError 的子类,而调用方
    #    的 handler 恰好写着 `except (OSError, ValueError, json.JSONDecodeError)` ⇒
    #    不报错、不崩,只是把 self.model 置 None ——用户**悄无声息地丢掉整条标定曲线**,
    #    还以为是自己没拟合过。
    return CalibrationModel.from_json(
        json.loads(read_text_tolerant(Path(path))[0])
    )


def save_summary(summary: RunSummary, path: str | Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(summary), indent=2) + "\n", encoding="utf-8")
