"""10 Hz i-t workflow: steady-current extraction, calibration and prediction.

The firmware emits timestamped native samples at about 8.06 Hz for the current
180 s run; the host can resample that trace to exactly 10 Hz/1800 rows.  This
module keeps the
scientific workflow deliberately small and explicit:

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
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Sequence

import numpy as np


DEFAULT_WINDOW_S = 20.0
DEFAULT_DURATION_S = 180.0
DEFAULT_SAMPLE_RATE_HZ = 10.0


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

        roots = np.roots(coeff)
        lo, hi = self.concentration_min_um, self.concentration_max_um
        candidates = [float(r.real) for r in roots
                      if abs(float(r.imag)) < 1e-8 and lo - 1e-9 <= r.real <= hi + 1e-9]
        if not candidates:
            raise ValueError(
                f"current {current:g} nA has no real concentration root in "
                f"[{lo:g}, {hi:g}] umol/L"
            )
        centre = (lo + hi) / 2.0
        return min(candidates, key=lambda value: abs(value - centre))

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


def detect_isolated_spikes(current_nA: Sequence[float],
                           valid: Sequence[bool]) -> np.ndarray:
    """Return a conservative mask for isolated one-sample current impulses.

    A point is flagged only when it is far from both neighbours on a robust,
    whole-trace scale *and* the two neighbours agree substantially better with
    each other.  A sustained physical step therefore remains valid.  The raw
    CSV is never modified; this mask is analysis metadata only.
    """

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
        & (residual > 7.0 * robust_scale)
        & (residual > 3.0 * (neighbour_gap + robust_scale))
    )
    flagged[1:-1] = local_spike
    return flagged


def _load_run_csv_with_quality(
        path: str | Path) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Load ``time_s``, signed current in nA, and validity from a run CSV.

    Accepted current columns are ``current_nA``, ``reduction_current_nA`` and
    firmware ``fa_fw`` (fA).  The latter is converted to nA.  ``sat != 0`` or
    an explicit ``valid == 0`` excludes a row.
    """

    path = Path(path)
    with path.open(newline="") as handle:
        rows = list(csv.DictReader(line for line in handle if not line.startswith("#")))
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
        row_valid = int(float(row.get("valid", "1") or 1)) != 0
        times.append(t)
        currents.append(current)
        valid.append(row_valid and sat == 0)
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
                      duration_s: float = DEFAULT_DURATION_S,
                      target_rate_hz: float = DEFAULT_SAMPLE_RATE_HZ) -> Path:
    """Resample a native run to a fixed-rate CSV for the requested workflow.

    The MAX30131 native period is about 124 ms on the fastest supported setting,
    so the board can provide roughly 8.06 Hz rather than ten independent samples
    per second.  This function interpolates the timestamped trace to exactly
    ``duration_s * target_rate_hz`` rows.  Saturated source rows remain marked
    invalid; the output is never silently promoted to a valid calibration point.
    """
    if duration_s <= 0 or target_rate_hz <= 0:
        raise ValueError("duration_s and target_rate_hz must be positive")
    t, current, valid, _ = _load_run_csv_with_quality(path)
    n = int(round(duration_s * target_rate_hz))
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

    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", newline="") as handle:
        handle.write("# Fixed-rate host resampling; native hardware timestamps retained in source run\n")
        handle.write(f"# source_rate_hz: {source_rate:.9f}\n")
        handle.write(f"# target_rate_hz: {target_rate_hz:.9f}\n")
        writer = csv.writer(handle)
        writer.writerow(["time_s", "current_nA", "valid", "sat", "source_rate_hz"])
        for ti, yi, ok in zip(target_t, resampled_current, valid_domain):
            writer.writerow([f"{ti:.9f}", f"{yi:.12g}", int(ok), 0 if ok else 3,
                             f"{source_rate:.9f}"])
    return output


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
    with path.open(newline="") as handle:
        reader = csv.DictReader(line for line in handle if not line.startswith("#"))
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
    if degree < 1:
        raise ValueError("degree must be at least 1")
    if len(points) < degree + 1:
        raise ValueError(f"degree {degree} needs at least {degree + 1} points")
    x = np.asarray([p.concentration_um for p in points], dtype=float)
    y = np.asarray([p.current_nA for p in points], dtype=float)
    if not np.all(np.isfinite(x)) or not np.all(np.isfinite(y)):
        raise ValueError("calibration data must be finite")
    if np.ptp(x) <= 0:
        raise ValueError("calibration concentrations must span a nonzero range")
    coeff = np.polyfit(x, y, degree)
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
    path.write_text(json.dumps(model.to_json(), indent=2) + "\n")


def load_model(path: str | Path) -> CalibrationModel:
    return CalibrationModel.from_json(json.loads(Path(path).read_text()))


def save_summary(summary: RunSummary, path: str | Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(summary), indent=2) + "\n")
