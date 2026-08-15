"""Pure rolling metrics for native I-T acquisition data.

The browser must not infer scientific values from its decimated display trace.
This module prepares the newest continuous native-data stage once, applies any
analysis filter to that whole stage, and only then crops the final fit window.
The prepared stage can also be reused by the automatic-stop ETA estimator.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, replace
from numbers import Real
from typing import Any, Sequence

import numpy as np

from .filtering import apply_filter, validate_filter_config
from .it import PlateauConfig, detect_isolated_spikes


_EMPTY_FLOATS = np.asarray([], dtype=float)
_EMPTY_MASK = np.asarray([], dtype=bool)


@dataclass(frozen=True)
class PreparedLiveStage:
    """Latest continuous native stage and its whole-stage filtered view."""

    stage_key: str | None
    time_s: np.ndarray
    raw_nA: np.ndarray
    filtered_nA: np.ndarray
    analysis_nA: np.ndarray
    window_mask: np.ndarray
    filter_meta: dict[str, object]
    filter_effective: bool
    plateau_config: PlateauConfig
    fit_window_s: float
    nominal_sample_rate_hz: float | None
    maximum_gap_s: float | None
    native_point_count: int
    valid_native_point_count: int
    isolated_spike_count: int
    stage_start_s: float | None
    stage_end_s: float | None
    stage_age_s: float | None
    coverage_s: float
    window_point_count: int
    window_complete: bool
    reason: str

    @property
    def window_time_s(self) -> np.ndarray:
        return self.time_s[self.window_mask]

    @property
    def window_raw_nA(self) -> np.ndarray:
        return self.raw_nA[self.window_mask]

    @property
    def window_filtered_nA(self) -> np.ndarray:
        return self.filtered_nA[self.window_mask]

    @property
    def window_analysis_nA(self) -> np.ndarray:
        return self.analysis_nA[self.window_mask]


def _readonly(values: Sequence[Any], *, dtype: Any) -> np.ndarray:
    result = np.asarray(values, dtype=dtype).copy()
    result.setflags(write=False)
    return result


def _finite_positive(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{name} 必须是正的有限数")
    number = float(value)
    if not math.isfinite(number) or number <= 0.0:
        raise ValueError(f"{name} 必须是正的有限数")
    return number


def _epoch_token(value: object) -> tuple[str, object] | None:
    """Return a comparable, JSON-safe epoch identity; non-finite is invalid."""

    if isinstance(value, np.generic):
        value = value.item()
    if value is None:
        return ("none", None)
    if isinstance(value, bool):
        return ("bool", value)
    if isinstance(value, Real):
        number = float(value)
        if not math.isfinite(number):
            return None
        if number.is_integer():
            return ("number", int(number))
        return ("number", number)
    if isinstance(value, str):
        return ("string", value)
    return None


def _stage_key(epoch: tuple[str, object], start_s: float) -> str:
    epoch_text = json.dumps(
        [epoch[0], epoch[1]], ensure_ascii=True, separators=(",", ":"),
    )
    # float.hex() is stable across polls and distinguishes -0.0 from +0.0.
    return f"{epoch_text}@{start_s.hex()}"


def _nominal_rate(
    time_s: np.ndarray,
    usable: np.ndarray,
    epochs: Sequence[tuple[str, object] | None],
    expected_sample_rate_hz: float | None,
) -> float | None:
    if expected_sample_rate_hz is not None:
        return _finite_positive(
            expected_sample_rate_hz, "实时指标额定采样率",
        )
    intervals = [
        float(time_s[index] - time_s[index - 1])
        for index in range(1, len(time_s))
        if usable[index]
        and usable[index - 1]
        and epochs[index] == epochs[index - 1]
        and time_s[index] > time_s[index - 1]
    ]
    if not intervals:
        return None
    median_interval = float(np.median(np.asarray(intervals, dtype=float)))
    return 1.0 / median_interval if median_interval > 0.0 else None


def _latest_stage_indices(
    time_s: np.ndarray,
    usable: np.ndarray,
    epochs: Sequence[tuple[str, object] | None],
    maximum_gap_s: float | None,
) -> np.ndarray:
    latest: list[int] = []
    current: list[int] = []
    previous: int | None = None
    for index in range(len(time_s)):
        if not usable[index]:
            current = []
            latest = []
            previous = None
            continue
        boundary = previous is None
        if previous is not None:
            interval = float(time_s[index] - time_s[previous])
            boundary = (
                epochs[index] != epochs[previous]
                or interval <= 0.0
                or (
                    maximum_gap_s is not None
                    and interval > maximum_gap_s + 1e-12
                )
            )
        if boundary:
            current = [index]
        else:
            current.append(index)
        latest = current
        previous = index
    return np.asarray(latest, dtype=int)


def _empty_stage(
    *,
    filter_config: dict[str, object],
    plateau_config: PlateauConfig,
    fit_window_s: float,
    nominal_sample_rate_hz: float | None,
    maximum_gap_s: float | None,
    native_point_count: int,
    valid_native_point_count: int,
    reason: str,
) -> PreparedLiveStage:
    _, meta = apply_filter(
        _EMPTY_FLOATS, _EMPTY_FLOATS, _EMPTY_MASK, filter_config,
    )
    return PreparedLiveStage(
        stage_key=None,
        time_s=_readonly(_EMPTY_FLOATS, dtype=float),
        raw_nA=_readonly(_EMPTY_FLOATS, dtype=float),
        filtered_nA=_readonly(_EMPTY_FLOATS, dtype=float),
        analysis_nA=_readonly(_EMPTY_FLOATS, dtype=float),
        window_mask=_readonly(_EMPTY_MASK, dtype=bool),
        filter_meta=_json_safe_dict(meta),
        filter_effective=False,
        plateau_config=plateau_config,
        fit_window_s=fit_window_s,
        nominal_sample_rate_hz=nominal_sample_rate_hz,
        maximum_gap_s=maximum_gap_s,
        native_point_count=native_point_count,
        valid_native_point_count=valid_native_point_count,
        isolated_spike_count=0,
        stage_start_s=None,
        stage_end_s=None,
        stage_age_s=None,
        coverage_s=0.0,
        window_point_count=0,
        window_complete=False,
        reason=reason,
    )


def prepare_live_stage(
    time_s: Sequence[float],
    current_nA: Sequence[float],
    valid: Sequence[bool],
    epoch: Sequence[object] | None,
    *,
    fit_window_s: float,
    filter_config: dict[str, object] | None = None,
    plateau_config: PlateauConfig | dict[str, object] | None = None,
    expected_sample_rate_hz: float | None = None,
) -> PreparedLiveStage:
    """Prepare the newest uninterrupted native stage for live calculations.

    Invalid/non-finite rows, epoch changes, timestamp rollback and excessive
    sampling gaps delimit stages. Isolated one-point impulses are conservative
    stage boundaries too: they remain in the raw acquisition but a new live
    window must accumulate after the impulse.
    """

    window_s = _finite_positive(fit_window_s, "末端拟合窗口")
    config = PlateauConfig.validate(plateau_config)
    normalized_filter = validate_filter_config(filter_config)
    try:
        t = np.asarray(time_s, dtype=float)
        raw = np.asarray(current_nA, dtype=float)
        valid_arr = np.asarray(valid, dtype=bool)
    except (TypeError, ValueError) as exc:
        raise ValueError("实时指标输入必须是一维序列") from exc
    if any(array.ndim != 1 for array in (t, raw, valid_arr)):
        raise ValueError("实时指标输入必须是一维序列")
    if epoch is None:
        raw_epochs: list[object] = [None] * len(t)
    else:
        try:
            epoch_array = np.asarray(epoch, dtype=object)
        except (TypeError, ValueError) as exc:
            raise ValueError("实时指标 epoch 必须是一维序列") from exc
        if epoch_array.ndim != 1:
            raise ValueError("实时指标 epoch 必须是一维序列")
        raw_epochs = list(epoch_array)
    lengths = (len(t), len(raw), len(valid_arr), len(raw_epochs))
    if len(set(lengths)) != 1:
        raise ValueError("实时指标 time/current/valid/epoch 长度不一致")

    epochs = [_epoch_token(value) for value in raw_epochs]
    epoch_valid = np.asarray([value is not None for value in epochs], dtype=bool)
    usable = valid_arr & np.isfinite(t) & np.isfinite(raw) & epoch_valid
    native_count = len(t)
    valid_native_count = int(usable.sum())
    nominal_rate = _nominal_rate(
        t, usable, epochs, expected_sample_rate_hz,
    )
    maximum_gap_s = (
        config.maximum_gap_periods / nominal_rate
        if nominal_rate is not None else None
    )
    indices = _latest_stage_indices(t, usable, epochs, maximum_gap_s)
    if not len(indices):
        return _empty_stage(
            filter_config=normalized_filter,
            plateau_config=config,
            fit_window_s=window_s,
            nominal_sample_rate_hz=nominal_rate,
            maximum_gap_s=maximum_gap_s,
            native_point_count=native_count,
            valid_native_point_count=valid_native_count,
            reason="等待有效原生数据",
        )

    physical_t = t[indices]
    physical_raw = raw[indices]
    spike_mask = detect_isolated_spikes(
        physical_raw,
        np.ones(len(physical_raw), dtype=bool),
        config.spike_scale_multiplier,
        config.spike_neighbor_multiplier,
    )
    spike_count = int(spike_mask.sum())
    spike_free_indices = _latest_stage_indices(
        physical_t,
        ~spike_mask,
        [epochs[index] for index in indices],
        maximum_gap_s,
    )
    if not len(spike_free_indices):
        empty = _empty_stage(
            filter_config=normalized_filter,
            plateau_config=config,
            fit_window_s=window_s,
            nominal_sample_rate_hz=nominal_rate,
            maximum_gap_s=maximum_gap_s,
            native_point_count=native_count,
            valid_native_point_count=valid_native_count,
            reason="孤立尖峰后等待新数据",
        )
        return replace(empty, isolated_spike_count=spike_count)

    stage_indices = indices[spike_free_indices]
    stage_t = t[stage_indices]
    stage_raw = raw[stage_indices]
    filtered, raw_filter_meta = apply_filter(
        stage_t,
        stage_raw,
        np.ones(len(stage_t), dtype=bool),
        normalized_filter,
    )
    filter_meta = _json_safe_dict(raw_filter_meta)
    filter_effective = bool(filter_meta.get("applied")) and len(stage_t) >= 5
    if bool(filter_meta.get("applied")) and not filter_effective:
        filter_meta["applied"] = False
        filter_meta["note"] = "有效数据不足，尚未应用低通滤波"
    use_filtered = (
        normalized_filter["mode"] == "analysis" and filter_effective
    )
    analysis = filtered if use_filtered else stage_raw

    start_s = float(stage_t[0])
    end_s = float(stage_t[-1])
    age_s = max(0.0, end_s - start_s)
    window_start_s = end_s - window_s
    window_mask = stage_t >= window_start_s - 1e-12
    window_t = stage_t[window_mask]
    window_count = int(window_mask.sum())
    coverage_s = (
        max(0.0, float(window_t[-1] - window_t[0]))
        if window_count >= 2 else 0.0
    )

    sample_period_s = 1.0 / nominal_rate if nominal_rate is not None else 0.0
    full_window = age_s + sample_period_s + 1e-12 >= window_s
    minimum_count = 3
    if nominal_rate is not None:
        minimum_count = max(
            minimum_count,
            int(math.ceil(
                window_s * nominal_rate * config.minimum_coverage_ratio
            )),
        )
    coverage_complete = (
        coverage_s + sample_period_s + 1e-12
        >= window_s * config.minimum_coverage_ratio
    )
    gaps_complete = True
    if maximum_gap_s is not None and window_count >= 2:
        gaps_complete = bool(
            np.max(np.diff(window_t)) <= maximum_gap_s + 1e-12
        )
    independent_times = (
        window_count >= 2
        and float(np.max(window_t) - np.min(window_t)) > 0.0
    )
    complete = bool(
        full_window
        and window_count >= minimum_count
        and coverage_complete
        and gaps_complete
        and independent_times
    )
    if not full_window:
        reason = f"末端窗口正在累积（{age_s:.1f}/{window_s:g} s）"
    elif window_count < minimum_count:
        reason = f"末端窗口有效点不足（{window_count}/{minimum_count}）"
    elif not coverage_complete:
        reason = "末端窗口时间覆盖不足"
    elif not gaps_complete:
        reason = "末窗采样间隔过大"
    elif not independent_times:
        reason = "末窗缺少独立时间点"
    else:
        reason = "末端窗口指标已就绪"

    stage_epoch = epochs[int(stage_indices[0])]
    assert stage_epoch is not None
    return PreparedLiveStage(
        stage_key=_stage_key(stage_epoch, start_s),
        time_s=_readonly(stage_t, dtype=float),
        raw_nA=_readonly(stage_raw, dtype=float),
        filtered_nA=_readonly(filtered, dtype=float),
        analysis_nA=_readonly(analysis, dtype=float),
        window_mask=_readonly(window_mask, dtype=bool),
        filter_meta=filter_meta,
        filter_effective=filter_effective,
        plateau_config=config,
        fit_window_s=window_s,
        nominal_sample_rate_hz=nominal_rate,
        maximum_gap_s=maximum_gap_s,
        native_point_count=native_count,
        valid_native_point_count=valid_native_count,
        isolated_spike_count=spike_count,
        stage_start_s=start_s,
        stage_end_s=end_s,
        stage_age_s=age_s,
        coverage_s=coverage_s,
        window_point_count=window_count,
        window_complete=complete,
        reason=reason,
    )


def _json_number(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    # Do not use a truthiness shortcut here: float(-0.0) preserves its sign.
    return number if math.isfinite(number) else None


def _json_safe_dict(value: dict[str, object]) -> dict[str, object]:
    def safe(item: object) -> object:
        if isinstance(item, dict):
            return {str(key): safe(nested) for key, nested in item.items()}
        if isinstance(item, (list, tuple)):
            return [safe(nested) for nested in item]
        if isinstance(item, (np.bool_, bool)):
            return bool(item)
        if isinstance(item, (np.integer, int)) and not isinstance(item, bool):
            return int(item)
        if isinstance(item, (np.floating, float)):
            return _json_number(item)
        if item is None or isinstance(item, str):
            return item
        return str(item)

    return {str(key): safe(item) for key, item in value.items()}


def _progress_percent(
    stage: PreparedLiveStage,
    fixed_duration_s: float | None,
    run_elapsed_s: float | None,
) -> float | None:
    if fixed_duration_s is None:
        return None
    duration = _finite_positive(fixed_duration_s, "定时测量时长")
    if run_elapsed_s is None:
        elapsed = stage.stage_age_s or 0.0
    else:
        if isinstance(run_elapsed_s, bool) or not isinstance(run_elapsed_s, Real):
            raise ValueError("测量进度时间必须是有限数")
        elapsed = float(run_elapsed_s)
        if not math.isfinite(elapsed):
            raise ValueError("测量进度时间必须是有限数")
    return float(min(100.0, max(0.0, elapsed / duration * 100.0)))


def _zero_sign_aware_mean(values: np.ndarray) -> float:
    result = float(np.mean(values))
    if result == 0.0 and len(values) and np.all(values == 0.0):
        if bool(np.all(np.signbit(values))):
            return -0.0
    return result


def metrics_from_stage(
    stage: PreparedLiveStage,
    *,
    run_state: str | None = None,
    fixed_duration_s: float | None = None,
    run_elapsed_s: float | None = None,
) -> dict[str, object]:
    """Return the strict-JSON rolling metric payload for a prepared stage."""

    state = str(run_state or "").strip().lower()
    progress = _progress_percent(stage, fixed_duration_s, run_elapsed_s)
    status = "accumulating"
    if not stage.native_point_count and state in {"", "idle", "ready"}:
        status = "idle"

    steady: float | None = None
    noise: float | None = None
    slope: float | None = None
    scatter: float | None = None
    tolerance: float | None = None
    slope_limit: float | None = None
    trend = "insufficient"
    reason = stage.reason

    if stage.window_complete:
        x = stage.window_time_s
        y = stage.window_analysis_nA
        steady = _zero_sign_aware_mean(y)
        centred_x = x - float(np.mean(x))
        centred_y = y - steady
        denominator = float(np.dot(centred_x, centred_x))
        slope = float(np.dot(centred_x, centred_y) / denominator)
        intercept = steady - slope * float(np.mean(x))
        residuals = y - (slope * x + intercept)
        residual_centre = float(np.median(residuals))
        scatter = 1.4826 * float(
            np.median(np.abs(residuals - residual_centre))
        )
        tolerance = max(
            stage.plateau_config.absolute_tolerance_nA,
            abs(steady) * stage.plateau_config.relative_tolerance,
            stage.plateau_config.scatter_multiplier * scatter,
        )
        slope_limit = tolerance / stage.plateau_config.window_duration_s
        if slope > slope_limit:
            trend = "rising"
        elif slope < -slope_limit:
            trend = "falling"
        else:
            trend = "flat"

        filter_mode = str(stage.filter_meta.get("mode") or "off")
        if (
            filter_mode != "off"
            and stage.filter_effective
            and stage.filter_meta.get("lowpass_cutoff_hz") is not None
        ):
            differences = stage.window_raw_nA - stage.window_filtered_nA
            if len(differences) >= 2:
                noise = float(np.std(differences, ddof=1))

        status = (
            "complete"
            if state in {"complete", "completed", "finished"}
            else "ready"
        )
        reason = "测量已完成" if status == "complete" else stage.reason

    result: dict[str, object] = {
        "status": status,
        "reason": reason,
        "window_s": _json_number(stage.fit_window_s),
        "coverage_s": _json_number(stage.coverage_s),
        "native_point_count": int(stage.native_point_count),
        "valid_native_point_count": int(stage.valid_native_point_count),
        "window_point_count": int(stage.window_point_count),
        "progress_percent": _json_number(progress),
        "steady_current_nA": _json_number(steady),
        "noise_nA": _json_number(noise),
        "slope_nA_per_s": _json_number(slope),
        "trend_state": trend,
        "tolerance_nA": _json_number(tolerance),
        "robust_scatter_nA": _json_number(scatter),
        "slope_limit_nA_per_s": _json_number(slope_limit),
        "filter_effective": bool(stage.filter_effective),
        "filter_meta": _json_safe_dict(stage.filter_meta),
        "stage_key": stage.stage_key,
        "stage_start_s": _json_number(stage.stage_start_s),
        "stage_end_s": _json_number(stage.stage_end_s),
        "stage_age_s": _json_number(stage.stage_age_s),
    }
    # This is a hard assertion at the module boundary: NaN/Infinity must never
    # leak into BaseHTTPRequestHandler's permissive JSON encoder.
    json.dumps(result, allow_nan=False, ensure_ascii=False)
    return result


def compute_rolling_metrics(
    time_s: Sequence[float],
    current_nA: Sequence[float],
    valid: Sequence[bool],
    epoch: Sequence[object] | None,
    *,
    fit_window_s: float,
    filter_config: dict[str, object] | None = None,
    plateau_config: PlateauConfig | dict[str, object] | None = None,
    expected_sample_rate_hz: float | None = None,
    run_state: str | None = None,
    fixed_duration_s: float | None = None,
    run_elapsed_s: float | None = None,
) -> dict[str, object]:
    """Prepare native samples and compute one stateless rolling snapshot."""

    stage = prepare_live_stage(
        time_s,
        current_nA,
        valid,
        epoch,
        fit_window_s=fit_window_s,
        filter_config=filter_config,
        plateau_config=plateau_config,
        expected_sample_rate_hz=expected_sample_rate_hz,
    )
    return metrics_from_stage(
        stage,
        run_state=run_state,
        fixed_duration_s=fixed_duration_s,
        run_elapsed_s=run_elapsed_s,
    )
