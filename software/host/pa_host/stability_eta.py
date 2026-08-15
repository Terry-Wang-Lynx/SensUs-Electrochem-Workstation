"""Display-only stability ETA for adaptive I-T measurements.

The estimator deliberately has no access to a collector, command file, or
stop callback.  It predicts when the existing plateau gate may pass and emits
strict-JSON-friendly telemetry; the measurement controller remains the sole
owner of stop decisions.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from numbers import Real
from typing import Any, Mapping, Sequence

import numpy as np

from .it import PlateauConfig, PlateauEvaluation, detect_isolated_spikes


@dataclass(frozen=True)
class StabilityEtaConfig:
    """Tunable estimator timings and model-quality limits."""

    minimum_stage_s: float = 45.0
    history_window_s: float = 120.0
    tau_min_s: float = 5.0
    tau_max_s: float = 3600.0
    tau_candidates: int = 96
    update_interval_s: float = 1.0
    maximum_projection_s: float = 7200.0
    short_slope_window_s: float = 10.0
    reversal_confirmation_s: float = 9.0
    smoothing_old_weight: float = 0.7
    huber_k: float = 1.345
    irls_iterations: int = 8
    minimum_samples: int = 24
    minimum_model_r2: float = 0.20
    profile_loss_fraction: float = 0.08
    maximum_tau_span_ratio: float = 12.0
    identifiability_sigma: float = 3.0
    direction_noise_multiplier: float = 3.0
    maximum_extrapolation_factor: float = 20.0

    def __post_init__(self) -> None:
        positive = (
            "minimum_stage_s", "history_window_s", "tau_min_s", "tau_max_s",
            "update_interval_s", "maximum_projection_s", "short_slope_window_s",
            "reversal_confirmation_s", "huber_k", "maximum_tau_span_ratio",
            "identifiability_sigma", "direction_noise_multiplier",
            "maximum_extrapolation_factor",
        )
        for name in positive:
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, Real):
                raise ValueError(f"ETA parameter {name} must be numeric")
            number = float(value)
            if not math.isfinite(number) or number <= 0:
                raise ValueError(f"ETA parameter {name} must be positive and finite")
            object.__setattr__(self, name, number)
        if self.tau_max_s <= self.tau_min_s:
            raise ValueError("ETA tau_max_s must exceed tau_min_s")
        if self.history_window_s < self.minimum_stage_s:
            raise ValueError("ETA history_window_s must cover minimum_stage_s")
        for name, minimum in (("tau_candidates", 8), ("irls_iterations", 1),
                              ("minimum_samples", 4)):
            value = getattr(self, name)
            if isinstance(value, bool) or int(value) != value or int(value) < minimum:
                raise ValueError(f"ETA parameter {name} must be an integer >= {minimum}")
            object.__setattr__(self, name, int(value))
        bounded = (
            ("smoothing_old_weight", 0.0, 1.0),
            ("minimum_model_r2", -1.0, 1.0),
            ("profile_loss_fraction", 0.0, 1.0),
        )
        for name, lower, upper in bounded:
            value = float(getattr(self, name))
            if not math.isfinite(value) or not lower <= value <= upper:
                raise ValueError(
                    f"ETA parameter {name} must be in [{lower:g}, {upper:g}]"
                )
            object.__setattr__(self, name, value)


@dataclass(frozen=True)
class _LineFit:
    intercept: float
    slope: float
    residuals: np.ndarray
    sigma: float
    loss: float


@dataclass(frozen=True)
class _ExpFit:
    tau_s: float
    i_inf_nA: float
    amplitude_nA: float
    noise_sigma_nA: float
    r2: float
    confidence: float
    direction: str

    def predict(self, time_s: float | np.ndarray) -> float | np.ndarray:
        return self.i_inf_nA + self.amplitude_nA * np.exp(
            -np.asarray(time_s) / self.tau_s
        )

    def mean_between(self, start_s: float, end_s: float) -> float:
        duration = end_s - start_s
        if duration <= 0:
            return float(self.predict(end_s))
        exponential_mean = self.tau_s / duration * (
            math.exp(-start_s / self.tau_s)
            - math.exp(-end_s / self.tau_s)
        )
        return self.i_inf_nA + self.amplitude_nA * exponential_mean


@dataclass(frozen=True)
class _StageData:
    time_s: np.ndarray
    current_nA: np.ndarray
    age_s: float
    expected_sample_rate_hz: float | None
    coordinate_origin_s: float


def _mad_sigma(values: np.ndarray) -> float:
    finite = values[np.isfinite(values)]
    if not len(finite):
        return 0.0
    centre = float(np.median(finite))
    return 1.4826 * float(np.median(np.abs(finite - centre)))


def _irls_line(
    x: np.ndarray, y: np.ndarray, *, huber_k: float, iterations: int,
) -> _LineFit | None:
    if len(x) < 3 or len(x) != len(y):
        return None
    design = np.column_stack((np.ones(len(x)), x))
    if not np.all(np.isfinite(design)) or not np.all(np.isfinite(y)):
        return None
    weights = np.ones(len(x), dtype=float)
    beta: np.ndarray | None = None
    for _ in range(iterations):
        root_weights = np.sqrt(weights)
        weighted_design = design * root_weights[:, None]
        if np.linalg.cond(weighted_design) > 1e10:
            return None
        candidate, _, rank, _ = np.linalg.lstsq(
            weighted_design, y * root_weights, rcond=None
        )
        if rank < 2 or not np.all(np.isfinite(candidate)):
            return None
        residuals = y - design @ candidate
        sigma = _mad_sigma(residuals)
        beta = candidate
        if sigma <= np.finfo(float).eps:
            break
        cutoff = huber_k * sigma
        absolute = np.abs(residuals)
        new_weights = np.ones(len(x), dtype=float)
        outside = absolute > cutoff
        new_weights[outside] = cutoff / absolute[outside]
        if np.max(np.abs(new_weights - weights)) < 1e-4:
            weights = new_weights
            break
        weights = new_weights
    if beta is None:
        return None
    residuals = y - design @ beta
    sigma = _mad_sigma(residuals)
    scale = max(sigma, np.finfo(float).eps)
    cutoff = huber_k * scale
    absolute = np.abs(residuals)
    loss = np.where(
        absolute <= cutoff,
        0.5 * residuals * residuals,
        cutoff * (absolute - 0.5 * cutoff),
    )
    return _LineFit(
        intercept=float(beta[0]), slope=float(beta[1]),
        residuals=residuals, sigma=float(sigma), loss=float(np.mean(loss)),
    )


def _stable_key(value: Any) -> Any:
    if isinstance(value, Mapping):
        return tuple(sorted((str(key), _stable_key(item)) for key, item in value.items()))
    if isinstance(value, (list, tuple)):
        return tuple(_stable_key(item) for item in value)
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, (str, int, float, bool, type(None))):
        return value
    return repr(value)


def _evaluation_value(evaluation: Any, name: str, default: Any = None) -> Any:
    if isinstance(evaluation, Mapping):
        return evaluation.get(name, default)
    return getattr(evaluation, name, default) if evaluation is not None else default


def _stage_value(stage: object, name: str, default: Any = None) -> Any:
    """Read both integration mappings and ``PreparedLiveStage`` objects."""
    if isinstance(stage, Mapping):
        return stage.get(name, default)
    return getattr(stage, name, default)


def _finite_or_none(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _filter_meta_key(value: Any) -> Any:
    if not isinstance(value, Mapping):
        return None
    mode = str(value.get("mode") or "off")
    if mode != "analysis":
        return ("off", False, None)
    cutoff = _finite_or_none(value.get("lowpass_cutoff_hz"))
    return (
        mode,
        bool(value.get("applied")),
        round(cutoff, 6) if cutoff is not None else None,
    )


def _analysis_filter_key(value: Any) -> Any:
    if not isinstance(value, Mapping) or value.get("mode") != "analysis":
        return ("off",)
    return _stable_key(dict(value))


def _eta_display(seconds: int) -> str:
    return "0 秒" if seconds <= 0 else f"约 {seconds} 秒"


class StabilityEtaEstimator:
    """Stateful, rate-limited estimator for display telemetry."""

    def __init__(self, config: StabilityEtaConfig | None = None) -> None:
        self.config = config or StabilityEtaConfig()
        self.reset()

    def reset(self) -> None:
        self._context_key: Any = None
        self._last_compute_s: float | None = None
        self._last_stage_end_s: float | None = None
        self._last_payload: dict[str, Any] | None = None
        self._smoothed_seconds: float | None = None
        self._trend_direction: str | None = None
        self._pending_direction: str | None = None
        self._pending_extremum_s: float | None = None
        self._internal_stage_start_s: float | None = None
        self._last_noise_sigma_nA: float | None = None

    @staticmethod
    def _base_payload(status: str, display_text: str, reason: str) -> dict[str, Any]:
        return {
            "status": status,
            "display_text": display_text,
            "seconds": None,
            "direction": "flat",
            "confidence": None,
            "tau_s": None,
            "i_inf_nA": None,
            "amplitude_nA": None,
            "noise_sigma_nA": None,
            "reason": reason,
            "reset_consecutive": False,
            "suggested_stage_start_s": None,
        }

    def _clear_for_context(self, context_key: Any) -> None:
        self.reset()
        self._context_key = context_key

    def _prepare_stage(
        self,
        *,
        stage: Mapping[str, Any] | object | None,
        time_s: Sequence[float] | None,
        current_nA: Sequence[float] | None,
        valid: Sequence[bool] | None,
        filter_config: Mapping[str, Any] | None,
        plateau_config: PlateauConfig,
    ) -> _StageData | None:
        source: object = stage if stage is not None else {}
        stage_times = _stage_value(source, "time_s", time_s)
        stage_raw = _stage_value(source, "raw_nA", current_nA)
        stage_valid = (
            _stage_value(source, "valid", valid)
            if isinstance(source, Mapping)
            else getattr(source, "valid", None)
        )
        if stage_times is None or stage_raw is None:
            return None
        try:
            t = np.asarray(stage_times, dtype=float)
            raw = np.asarray(stage_raw, dtype=float)
            valid_arr = (
                np.ones(len(t), dtype=bool)
                if stage_valid is None else np.asarray(stage_valid, dtype=bool)
            )
        except (TypeError, ValueError):
            return None
        if any(array.ndim != 1 for array in (t, raw, valid_arr)):
            return None
        if len({len(t), len(raw), len(valid_arr)}) != 1 or not len(t):
            return None
        finite_source_times = t[np.isfinite(t)]
        if not len(finite_source_times):
            return None
        source_min = float(np.min(finite_source_times))
        usable = valid_arr & np.isfinite(t) & np.isfinite(raw)
        # ETA and reversal detection intentionally use native current values.
        # ``PreparedLiveStage`` already delimits invalid/epoch/gap/spike
        # boundaries; the second conservative spike pass also protects direct
        # callers that supply raw arrays instead of a prepared stage.
        spikes = detect_isolated_spikes(
            raw, usable,
            plateau_config.spike_scale_multiplier,
            plateau_config.spike_neighbor_multiplier,
        )
        usable &= ~spikes
        selected = np.flatnonzero(usable)
        if not len(selected):
            return None
        order = selected[np.argsort(t[selected], kind="stable")]
        t = t[order]
        native_current = raw[order]
        usable = np.ones(len(t), dtype=bool)
        coordinate_start = _finite_or_none(
            _stage_value(
                source,
                "start_s",
                _stage_value(source, "stage_start_s"),
            )
        )
        coordinate_offset = (
            coordinate_start - source_min if coordinate_start is not None else 0.0
        )
        if self._internal_stage_start_s is not None:
            keep = t + coordinate_offset >= self._internal_stage_start_s - 1e-12
            t = t[keep]
            native_current = native_current[keep]
            usable = usable[keep]
            if not len(t):
                return None
        raw_start = float(t[0])
        normalized_t = t - raw_start
        supplied_age = _finite_or_none(
            _stage_value(
                source,
                "age_s",
                _stage_value(source, "stage_age_s"),
            )
        )
        age = float(normalized_t[-1])
        if self._internal_stage_start_s is None and supplied_age is not None:
            age = max(age, supplied_age)
        expected_rate = _finite_or_none(
            _stage_value(
                source,
                "expected_sample_rate_hz",
                _stage_value(source, "nominal_sample_rate_hz"),
            )
        )
        return _StageData(
            time_s=normalized_t,
            current_nA=native_current.astype(float),
            age_s=age,
            expected_sample_rate_hz=(
                expected_rate if expected_rate is not None and expected_rate > 0 else None
            ),
            coordinate_origin_s=coordinate_offset + raw_start,
        )

    def _short_direction(
        self, data: _StageData, absolute_tolerance_nA: float,
    ) -> tuple[str, float, float, np.ndarray, np.ndarray]:
        end = float(data.time_s[-1])
        selected = data.time_s >= end - self.config.short_slope_window_s
        t = data.time_s[selected]
        y = data.current_nA[selected]
        if len(t) < 4 or float(t[-1] - t[0]) <= 0:
            return "flat", 0.0, 0.0, t, y
        centred = t - float(np.mean(t))
        fit = _irls_line(
            centred, y, huber_k=self.config.huber_k,
            iterations=self.config.irls_iterations,
        )
        if fit is None:
            return "flat", 0.0, 0.0, t, y
        span = float(t[-1] - t[0])
        displacement = fit.slope * span
        threshold = max(
            self.config.direction_noise_multiplier * fit.sigma,
            absolute_tolerance_nA,
        )
        direction = (
            "flat" if abs(displacement) <= threshold
            else "rising" if displacement > 0 else "falling"
        )
        return direction, float(fit.slope), float(fit.sigma), t, y

    def _check_reversal(
        self, data: _StageData, short_direction: str, short_sigma: float,
        absolute_tolerance_nA: float,
    ) -> dict[str, Any] | None:
        baseline = self._trend_direction
        opposite = (
            (baseline == "rising" and short_direction == "falling")
            or (baseline == "falling" and short_direction == "rising")
        )
        if baseline not in {"rising", "falling"}:
            if short_direction in {"rising", "falling"}:
                self._trend_direction = short_direction
            return None
        if not opposite:
            self._pending_direction = None
            self._pending_extremum_s = None
            return None

        end = float(data.time_s[-1])
        selected = data.time_s >= end - self.config.short_slope_window_s
        recent_t = data.time_s[selected]
        recent_y = data.current_nA[selected]
        if not len(recent_t):
            return None
        extremum_index = (
            int(np.argmax(recent_y)) if baseline == "rising"
            else int(np.argmin(recent_y))
        )
        extremum_s = float(recent_t[extremum_index])
        tail = recent_y[recent_t >= max(extremum_s, end - 1.0)]
        current_level = float(np.median(tail)) if len(tail) else float(recent_y[-1])
        displacement = abs(current_level - float(recent_y[extremum_index]))
        noise = max(
            short_sigma,
            self._last_noise_sigma_nA or 0.0,
        )
        required_displacement = max(
            self.config.direction_noise_multiplier * noise,
            absolute_tolerance_nA,
        )
        self._pending_direction = short_direction
        self._pending_extremum_s = extremum_s
        persisted_s = end - extremum_s
        if (
            persisted_s + 1e-12 < self.config.reversal_confirmation_s
            or displacement <= required_displacement
        ):
            payload = self._base_payload(
                "reestimating", "曲线变化，正在重新估计", "reversal_pending"
            )
            payload.update({
                "direction": "reversal_pending",
                "noise_sigma_nA": _finite_or_none(noise),
            })
            return payload

        source_extremum_s = data.coordinate_origin_s + extremum_s
        self._internal_stage_start_s = source_extremum_s
        self._smoothed_seconds = None
        self._last_noise_sigma_nA = None
        self._trend_direction = short_direction
        self._pending_direction = None
        self._pending_extremum_s = None
        payload = self._base_payload(
            "reestimating", "曲线变化，正在重新估计", "reversal_confirmed"
        )
        payload.update({
            "direction": short_direction,
            "noise_sigma_nA": _finite_or_none(noise),
            "reset_consecutive": True,
            "suggested_stage_start_s": _finite_or_none(source_extremum_s),
        })
        return payload

    def _fit_exponential(
        self, data: _StageData, plateau_config: PlateauConfig,
    ) -> tuple[_ExpFit | None, str]:
        end = float(data.time_s[-1])
        start = (
            float(data.time_s[0])
            if data.age_s <= self.config.history_window_s
            else end - self.config.history_window_s
        )
        selected = data.time_s >= start - 1e-12
        t = data.time_s[selected]
        y = data.current_nA[selected]
        if len(t) < self.config.minimum_samples:
            return None, "insufficient_samples"
        span = float(t[-1] - t[0])
        if span < self.config.minimum_stage_s * 0.75:
            return None, "insufficient_fit_span"
        reference_s = float(t[0])
        relative_t = t - reference_s
        candidates: list[tuple[float, _LineFit]] = []
        taus = np.geomspace(
            self.config.tau_min_s, self.config.tau_max_s,
            self.config.tau_candidates,
        )
        for tau in taus:
            regressor = np.exp(-relative_t / float(tau))
            fit = _irls_line(
                regressor, y, huber_k=self.config.huber_k,
                iterations=self.config.irls_iterations,
            )
            if fit is not None:
                candidates.append((float(tau), fit))
        if len(candidates) < 3:
            return None, "model_fit_failed"
        best_index = min(range(len(candidates)), key=lambda index: candidates[index][1].loss)
        if best_index in {0, len(candidates) - 1}:
            return None, "tau_at_search_boundary"
        tau, linear = candidates[best_index]
        best_loss = linear.loss
        slack = max(
            best_loss * self.config.profile_loss_fraction,
            max(linear.sigma, np.finfo(float).eps) ** 2 * 1e-3,
        )
        profile = [
            (candidate_tau, candidate_fit)
            for candidate_tau, candidate_fit in candidates
            if candidate_fit.loss <= best_loss + slack
        ]
        profile_taus = [item[0] for item in profile]
        if max(profile_taus) / min(profile_taus) > self.config.maximum_tau_span_ratio:
            return None, "tau_not_identifiable"
        i_inf_values = np.asarray([item[1].intercept for item in profile], dtype=float)
        parameter_scale = max(
            self.config.identifiability_sigma * linear.sigma,
            plateau_config.absolute_tolerance_nA,
        )
        if float(np.ptp(i_inf_values)) > 2.0 * parameter_scale:
            return None, "asymptote_unstable"
        amplitude_at_reference = linear.slope
        log_multiplier = reference_s / tau
        if log_multiplier > 700:
            return None, "amplitude_not_identifiable"
        amplitude = amplitude_at_reference * math.exp(log_multiplier)
        if not math.isfinite(amplitude):
            return None, "amplitude_not_identifiable"
        modeled_change = abs(amplitude_at_reference) * (1.0 - math.exp(-span / tau))
        if modeled_change <= max(
            self.config.identifiability_sigma * linear.sigma,
            plateau_config.absolute_tolerance_nA * 0.25,
        ):
            return None, "amplitude_not_identifiable"
        total = float(np.sum((y - float(np.median(y))) ** 2))
        residual_sum = float(np.sum(linear.residuals ** 2))
        r2 = 1.0 - residual_sum / total if total > np.finfo(float).eps else -math.inf
        if not math.isfinite(r2) or r2 < self.config.minimum_model_r2:
            return None, "model_not_trustworthy"
        observed_scale = max(
            float(np.ptp(y)), linear.sigma,
            plateau_config.absolute_tolerance_nA,
        )
        if abs(linear.intercept - float(y[-1])) > (
            self.config.maximum_extrapolation_factor * observed_scale
        ):
            return None, "asymptote_extrapolation_unstable"
        tau_profile_confidence = max(
            0.0,
            1.0 - math.log(max(profile_taus) / min(profile_taus))
            / math.log(self.config.maximum_tau_span_ratio),
        )
        signal_confidence = modeled_change / (
            modeled_change + self.config.identifiability_sigma
            * max(linear.sigma, np.finfo(float).eps)
        )
        duration_confidence = min(1.0, span / self.config.history_window_s)
        confidence = max(0.0, min(1.0,
            0.4 * max(0.0, min(1.0, r2))
            + 0.35 * tau_profile_confidence
            + 0.15 * signal_confidence
            + 0.10 * duration_confidence
        ))
        direction = "falling" if amplitude > 0 else "rising"
        return _ExpFit(
            tau_s=tau, i_inf_nA=linear.intercept,
            amplitude_nA=float(amplitude), noise_sigma_nA=linear.sigma,
            r2=r2, confidence=confidence, direction=direction,
        ), "ok"

    def _simulate_gate(
        self, model: _ExpFit, latest_s: float, stage_origin_s: float,
        minimum_gate_age_s: float, plateau_config: PlateauConfig,
        consecutive_passes: int, expected_sample_rate_hz: float | None,
    ) -> float | None:
        segment_s = plateau_config.segment_duration_s
        segment_count = plateau_config.segment_count
        required = plateau_config.required_consecutive_windows
        streak = max(0, min(int(consecutive_passes), required))
        if streak >= required:
            return 0.0
        latest_global_s = stage_origin_s + latest_s
        first_eligible_segment = int(math.ceil(
            (stage_origin_s + minimum_gate_age_s) / segment_s - 1e-12
        ))
        first_decision = max(
            segment_count,
            int(math.floor(latest_global_s / segment_s)) + 1,
            first_eligible_segment,
        )
        maximum_decisions = max(
            1, int(math.ceil(self.config.maximum_projection_s / segment_s))
        )
        segment_noise = model.noise_sigma_nA
        if expected_sample_rate_hz is not None:
            segment_noise /= math.sqrt(max(1.0, expected_sample_rate_hz * segment_s))
        for offset in range(maximum_decisions):
            complete_segment = first_decision + offset
            window_end_global = complete_segment * segment_s
            projected_seconds = window_end_global - latest_global_s
            if projected_seconds > self.config.maximum_projection_s + 1e-12:
                return None
            window_start_global = (
                window_end_global - plateau_config.window_duration_s
            )
            window_start = window_start_global - stage_origin_s
            means = np.asarray([
                model.mean_between(
                    window_start + index * segment_s,
                    window_start + (index + 1) * segment_s,
                )
                for index in range(segment_count)
            ], dtype=float)
            if not np.all(np.isfinite(means)):
                return None
            centres = window_start + (np.arange(segment_count) + 0.5) * segment_s
            slope, intercept = np.polyfit(centres, means, 1)
            residuals = means - (slope * centres + intercept)
            scatter = max(_mad_sigma(residuals), segment_noise)
            median_current = float(np.median(means))
            tolerance = max(
                plateau_config.absolute_tolerance_nA,
                abs(median_current) * plateau_config.relative_tolerance,
                plateau_config.scatter_multiplier * scatter,
            )
            trend_delta = abs(float(slope) * plateau_config.window_duration_s)
            half = segment_count // 2
            half_delta = abs(
                float(np.mean(means[half:]) - np.mean(means[:half]))
            )
            if trend_delta <= tolerance and half_delta <= tolerance:
                streak += 1
            else:
                streak = 0
            if streak >= required:
                return max(0.0, projected_seconds)
        return None

    def _plateau_fallback(
        self, *, latest_s: float, stage_origin_s: float,
        minimum_gate_age_s: float, plateau_config: PlateauConfig,
        plateau_evaluation: PlateauEvaluation | Mapping[str, Any] | None,
        consecutive_passes: int, direction: str, noise_sigma_nA: float,
    ) -> dict[str, Any] | None:
        stable = bool(_evaluation_value(plateau_evaluation, "stable", False))
        if consecutive_passes <= 0 and not stable:
            return None
        observed_passes = max(int(consecutive_passes), 1 if stable else 0)
        remaining = max(
            0,
            plateau_config.required_consecutive_windows - observed_passes,
        )
        if remaining == 0:
            seconds = 0
        else:
            segment_s = plateau_config.segment_duration_s
            latest_global_s = stage_origin_s + latest_s
            first_eligible_segment = int(math.ceil(
                (stage_origin_s + minimum_gate_age_s) / segment_s - 1e-12
            ))
            next_segment = max(
                int(math.floor(latest_global_s / segment_s)) + 1,
                first_eligible_segment,
                plateau_config.segment_count,
            )
            next_boundary = next_segment * segment_s
            seconds = int(math.ceil(
                max(0.0, next_boundary - latest_global_s)
                + max(0, remaining - 1) * segment_s
            ))
        payload = self._base_payload(
            "ready", _eta_display(seconds), "plateau_confirmation"
        )
        payload.update({
            "seconds": seconds,
            "direction": direction,
            "confidence": 0.7,
            "noise_sigma_nA": _finite_or_none(noise_sigma_nA),
        })
        return payload

    def _smooth(self, seconds: float) -> int:
        raw = max(0.0, float(seconds))
        if self._smoothed_seconds is None:
            self._smoothed_seconds = raw
        else:
            old_weight = self.config.smoothing_old_weight
            self._smoothed_seconds = (
                old_weight * self._smoothed_seconds + (1.0 - old_weight) * raw
            )
        return max(0, int(round(self._smoothed_seconds)))

    def update(
        self,
        *,
        stage: Mapping[str, Any] | object | None = None,
        time_s: Sequence[float] | None = None,
        current_nA: Sequence[float] | None = None,
        valid: Sequence[bool] | None = None,
        plateau_config: PlateauConfig | Mapping[str, Any] | None = None,
        filter_config: Mapping[str, Any] | None = None,
        plateau_evaluation: PlateauEvaluation | Mapping[str, Any] | None = None,
        consecutive_passes: int = 0,
        live_metrics: Mapping[str, Any] | None = None,
        stage_key: Any = None,
        minimum_gate_age_s: float | None = None,
        enabled: bool = True,
        frozen: bool = False,
        force: bool = False,
        now_s: float | None = None,
    ) -> dict[str, Any]:
        """Update and return strict-JSON-compatible ``stability_eta`` telemetry."""
        if not enabled:
            self.reset()
            payload = self._base_payload(
                "disabled", "自动停止未启用", "adaptive_stop_disabled"
            )
            self._last_payload = payload
            return dict(payload)
        if frozen:
            payload = dict(self._last_payload or self._base_payload(
                "estimating", "正在估计", "insufficient_data"
            ))
            payload.update({"status": "frozen", "reason": "frozen"})
            return payload

        try:
            plateau = PlateauConfig.validate(plateau_config)
        except (TypeError, ValueError):
            return self._base_payload(
                "unavailable", "暂无法估计", "invalid_plateau_config"
            )
        source: object = stage if stage is not None else {}
        raw_minimum_gate_age = (
            minimum_gate_age_s
            if minimum_gate_age_s is not None
            else _stage_value(source, "minimum_gate_age_s")
        )
        if raw_minimum_gate_age is None:
            raw_minimum_gate_age = _stage_value(source, "fit_window_s")
        parsed_gate_age = _finite_or_none(raw_minimum_gate_age)
        if raw_minimum_gate_age is not None and (
            parsed_gate_age is None or parsed_gate_age < 0.0
        ):
            return self._base_payload(
                "unavailable", "暂无法估计", "invalid_minimum_gate_age"
            )
        gate_age_s = max(
            plateau.window_duration_s,
            parsed_gate_age if parsed_gate_age is not None else 0.0,
        )
        raw_stage_key = _stage_value(source, "stage_key", stage_key)
        source_filter_meta = _stage_value(source, "filter_meta")
        context_key = (
            _stable_key(raw_stage_key),
            _stable_key(plateau.to_dict()),
            _analysis_filter_key(filter_config),
            _filter_meta_key(source_filter_meta),
            gate_age_s,
        )
        context_changed = context_key != self._context_key
        if context_changed:
            self._clear_for_context(context_key)
        data = self._prepare_stage(
            stage=stage, time_s=time_s, current_nA=current_nA, valid=valid,
            filter_config=filter_config, plateau_config=plateau,
        )
        if data is None:
            payload = self._base_payload(
                "estimating", "正在估计", "insufficient_data"
            )
            self._last_payload = payload
            return dict(payload)
        stage_end_coordinate_s = (
            data.coordinate_origin_s + float(data.time_s[-1])
        )
        if (
            self._last_stage_end_s is not None
            and stage_end_coordinate_s + 1e-9 < self._last_stage_end_s
        ):
            self._clear_for_context(context_key)
        self._last_stage_end_s = stage_end_coordinate_s
        clock = time.monotonic() if now_s is None else float(now_s)
        if (
            not force
            and not context_changed
            and self._last_compute_s is not None
            and clock - self._last_compute_s < self.config.update_interval_s
            and self._last_payload is not None
        ):
            cached = dict(self._last_payload)
            cached["reset_consecutive"] = False
            return cached
        self._last_compute_s = clock

        passes = max(0, int(consecutive_passes))
        rolling_ready = True
        if isinstance(live_metrics, Mapping):
            rolling_steady = _finite_or_none(
                live_metrics.get("steady_current_nA")
            )
            rolling_ready = rolling_steady is not None
        if passes >= plateau.required_consecutive_windows and rolling_ready:
            payload = self._base_payload(
                "complete", _eta_display(0), "plateau_gate_complete"
            )
            payload.update({"seconds": 0, "confidence": 1.0})
            self._last_payload = payload
            return dict(payload)
        if not rolling_ready:
            passes = min(
                passes, max(0, plateau.required_consecutive_windows - 1),
            )

        short_direction, _, short_sigma, _, _ = self._short_direction(
            data, plateau.absolute_tolerance_nA
        )
        reversal = self._check_reversal(
            data, short_direction, short_sigma, plateau.absolute_tolerance_nA
        )
        if reversal is not None:
            self._last_payload = dict(reversal)
            self._last_payload["reset_consecutive"] = False
            return dict(reversal)
        if data.age_s < self.config.minimum_stage_s:
            payload = self._base_payload(
                "estimating", "正在估计",
                "minimum_stage_not_reached",
            )
            payload.update({
                "direction": short_direction,
                "noise_sigma_nA": _finite_or_none(short_sigma),
            })
            self._last_payload = payload
            return dict(payload)

        model, reason = self._fit_exponential(data, plateau)
        if model is None:
            noise = max(short_sigma, self._last_noise_sigma_nA or 0.0)
            fallback = self._plateau_fallback(
                latest_s=float(data.time_s[-1]),
                stage_origin_s=data.coordinate_origin_s,
                minimum_gate_age_s=gate_age_s, plateau_config=plateau,
                plateau_evaluation=plateau_evaluation,
                consecutive_passes=passes, direction=short_direction,
                noise_sigma_nA=noise,
            )
            payload = fallback or self._base_payload(
                "unavailable", "暂无法估计", reason
            )
            payload["direction"] = short_direction
            payload["noise_sigma_nA"] = _finite_or_none(noise)
            self._last_payload = payload
            return dict(payload)

        self._last_noise_sigma_nA = model.noise_sigma_nA
        if short_direction in {"rising", "falling"}:
            self._trend_direction = short_direction
        else:
            self._trend_direction = model.direction
        eta = self._simulate_gate(
            model, float(data.time_s[-1]), data.coordinate_origin_s,
            gate_age_s, plateau, passes,
            data.expected_sample_rate_hz,
        )
        if eta is None:
            fallback = self._plateau_fallback(
                latest_s=float(data.time_s[-1]),
                stage_origin_s=data.coordinate_origin_s,
                minimum_gate_age_s=gate_age_s, plateau_config=plateau,
                plateau_evaluation=plateau_evaluation,
                consecutive_passes=passes, direction=self._trend_direction,
                noise_sigma_nA=model.noise_sigma_nA,
            )
            payload = fallback or self._base_payload(
                "unavailable", "暂无法估计",
                "plateau_not_reached_within_projection",
            )
            payload.update({
                "direction": self._trend_direction,
                "confidence": _finite_or_none(model.confidence),
                "tau_s": _finite_or_none(model.tau_s),
                "i_inf_nA": _finite_or_none(model.i_inf_nA),
                "amplitude_nA": _finite_or_none(model.amplitude_nA),
                "noise_sigma_nA": _finite_or_none(model.noise_sigma_nA),
            })
            self._last_payload = payload
            return dict(payload)

        seconds = self._smooth(eta)
        payload = self._base_payload(
            "ready", _eta_display(seconds), "model_projection"
        )
        payload.update({
            "seconds": seconds,
            "direction": self._trend_direction,
            "confidence": _finite_or_none(model.confidence),
            "tau_s": _finite_or_none(model.tau_s),
            "i_inf_nA": _finite_or_none(model.i_inf_nA),
            "amplitude_nA": _finite_or_none(model.amplitude_nA),
            "noise_sigma_nA": _finite_or_none(model.noise_sigma_nA),
        })
        if isinstance(live_metrics, Mapping):
            payload["metrics_timestamp_s"] = _finite_or_none(
                live_metrics.get("timestamp_s")
            )
        self._last_payload = payload
        return dict(payload)


def estimate_stability_eta(**kwargs: Any) -> dict[str, Any]:
    """One-shot convenience wrapper; controllers should retain an estimator."""
    return StabilityEtaEstimator().update(**kwargs)


__all__ = [
    "StabilityEtaConfig", "StabilityEtaEstimator", "estimate_stability_eta",
]
