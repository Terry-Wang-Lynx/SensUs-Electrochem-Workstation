"""Host-side, reproducible digital filtering for electrochemical traces.

The acquisition CSV is deliberately never modified.  Filtering is an analysis
view layered on top of the signed current samples.  The repeated one-pole
forward/backward implementation deliberately matches the browser preview so a
saved analysis uses the same transfer function the operator saw during a run.
"""

from __future__ import annotations

import csv
import math
from pathlib import Path
from typing import Any, Sequence

import numpy as np


FILTER_DEFAULTS: dict[str, Any] = {
    "mode": "display",  # off, display, analysis
    "lowpass_enabled": False,
    "lowpass_cutoff_hz": 1.0,
    "lowpass_auto": True,
    "lowpass_order": 2,
}


def validate_filter_config(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return a complete, JSON-safe filter configuration.

    This validation intentionally does not use the hardware sampling-rate
    limits.  The final Nyquist check is made against the actual trace because
    Debug, DC and EIS/CV can have different native rates.
    """

    raw = {**FILTER_DEFAULTS, **(payload or {})}
    mode = str(raw.get("mode", "display")).lower()
    if mode not in {"off", "display", "analysis"}:
        raise ValueError("滤波模式必须是 off、display 或 analysis")

    def boolean(name: str, default: bool) -> bool:
        value = raw.get(name, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)) and value in (0, 1):
            return bool(value)
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"true", "1", "yes", "on"}:
                return True
            if normalized in {"false", "0", "no", "off"}:
                return False
        raise ValueError(f"滤波参数 {name} 必须是布尔值")

    def number(
        name: str, lo: float, hi: float, integer: bool = False,
        *, required: bool = True,
    ) -> float | int:
        fallback = FILTER_DEFAULTS[name]
        try:
            value = float(raw[name])
        except (KeyError, TypeError, ValueError) as exc:
            if not required:
                value = float(fallback)
            else:
                raise ValueError(f"滤波参数 {name} 不是数字") from exc
        if not math.isfinite(value) or not lo <= value <= hi:
            if not required:
                value = float(fallback)
            else:
                raise ValueError(f"滤波参数 {name} 必须在 {lo:g} 至 {hi:g} 之间")
        return int(round(value)) if integer else round(value, 6)

    lowpass_enabled = boolean("lowpass_enabled", False)
    lowpass_auto = boolean("lowpass_auto", True)
    return {
        "mode": mode,
        "lowpass_enabled": lowpass_enabled,
        "lowpass_cutoff_hz": number(
            "lowpass_cutoff_hz", 0.01, 1000,
            required=lowpass_enabled and not lowpass_auto,
        ),
        "lowpass_auto": lowpass_auto,
        "lowpass_order": number(
            "lowpass_order", 1, 4, integer=True, required=lowpass_enabled,
        ),
    }


def _sample_rate(time_s: Sequence[float]) -> float:
    t = np.asarray(time_s, dtype=float)
    if len(t) < 2:
        return 0.0
    dt = np.diff(t)
    dt = dt[np.isfinite(dt) & (dt > 0)]
    return 1.0 / float(np.median(dt)) if len(dt) else 0.0


def _repeated_one_pole_lowpass(
    x: np.ndarray, fs: float, cutoff: float, order: int,
) -> np.ndarray:
    """Match ``gui/app.js::filterLowpass`` operation-for-operation."""

    alpha = (2.0 * math.pi * cutoff / fs) / (1.0 + 2.0 * math.pi * cutoff / fs)

    def one_pass(values: np.ndarray) -> np.ndarray:
        out = np.empty_like(values, dtype=float)
        state = float(values[0])
        for i, value in enumerate(values):
            state += alpha * (float(value) - state)
            out[i] = state
        return out

    result = x.astype(float, copy=True)
    # JavaScript's Math.round is half-up for these positive values, whereas
    # Python round() uses bankers' rounding.  The browser also mirrors the edge
    # samples themselves, rather than NumPy's ``reflect`` convention.
    rounded = int(math.floor(fs / max(cutoff, 0.05) + 0.5))
    pad = min(max(3, rounded), len(result) - 1)
    padded = (
        np.concatenate((result[:pad][::-1], result, result[-pad:][::-1]))
        if pad else result
    )
    for _ in range(order):
        padded = one_pass(padded)
        padded = one_pass(padded[::-1])[::-1]
    return padded[pad:-pad] if pad else padded


def _filter_segment(
    values: np.ndarray,
    fs: float,
    config: dict[str, Any],
    cutoff: float | None,
) -> np.ndarray:
    result = values.astype(float, copy=True)
    if cutoff is not None:
        result = _repeated_one_pole_lowpass(
            result, fs, cutoff, int(config["lowpass_order"]),
        )
    return result


def apply_filter(
    time_s: Sequence[float], current_nA: Sequence[float], valid: Sequence[bool],
    config: dict[str, Any] | None = None,
) -> tuple[np.ndarray, dict[str, Any]]:
    """Apply a validated filter to each contiguous valid segment."""

    cfg = validate_filter_config(config)
    t = np.asarray(time_s, dtype=float)
    current = np.asarray(current_nA, dtype=float)
    try:
        valid_arr = np.asarray(valid, dtype=bool)
        lengths = (len(t), len(current), len(valid_arr))
    except TypeError as exc:
        raise ValueError("滤波输入必须是一维序列") from exc
    if any(array.ndim != 1 for array in (t, current, valid_arr)):
        raise ValueError("滤波输入必须是一维序列")
    if len(set(lengths)) != 1:
        raise ValueError("滤波输入 time/current/valid 长度不一致")
    # A malformed timestamp must not turn a valid current sample into a
    # filter state update.  It is retained in the output as a raw point, but
    # excluded from detection and each contiguous filtering segment.
    valid_arr = valid_arr & np.isfinite(t) & np.isfinite(current)
    fs = _sample_rate(t)
    output = current.astype(float, copy=True)
    meta: dict[str, Any] = {
        "mode": cfg["mode"], "sample_rate_hz": fs,
        "applied": False, "lowpass_cutoff_hz": None,
        "note": "滤波关闭",
    }
    if cfg["mode"] == "off" or not len(output) or fs <= 0:
        return output, meta
    nyquist = fs / 2.0
    cutoff: float | None = None
    notes: list[str] = []
    if cfg["lowpass_enabled"]:
        requested_cutoff = (
            max(0.05, min(2.0, fs * 0.20)) if cfg["lowpass_auto"]
            else float(cfg["lowpass_cutoff_hz"])
        )
        cutoff = min(
            requested_cutoff,
            nyquist * 0.90,
        )
        if not cfg["lowpass_auto"] and requested_cutoff >= nyquist * 0.90:
            notes.append(f"低通截止频率已限制为奈奎斯特频率的 90%（{cutoff:.4g} Hz）")
        if cutoff <= 0:
            cutoff = None
    meta.update({
        "applied": bool(cutoff is not None),
        "lowpass_cutoff_hz": cutoff,
        "note": " · ".join(notes),
    })
    if not meta["applied"]:
        return output, meta

    # Never filter through saturated rows.  A step either side of a rail is a
    # separate segment; this prevents recovery transients from contaminating
    # otherwise valid electrochemical data.
    index = 0
    while index < len(output):
        while index < len(output) and not valid_arr[index]:
            index += 1
        start = index
        while index < len(output) and valid_arr[index]:
            index += 1
        stop = index
        if stop - start >= 5:
            output[start:stop] = _filter_segment(
                output[start:stop], fs, cfg, cutoff
            )
    return output, meta


def write_filtered_csv(
    source: str | Path, output: str | Path, config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Filter a CSV with ``time_s``/``current_nA`` or firmware columns."""

    source = Path(source)
    output = Path(output)
    if source.resolve() == output.resolve():
        raise ValueError("滤波输出必须是新文件，不能覆盖原始采集文件")
    rows: list[dict[str, str]] = []
    with source.open(newline="") as handle:
        for row in csv.DictReader(line for line in handle if not line.startswith("#")):
            rows.append(row)
    if not rows:
        raise ValueError(f"run CSV has no data rows: {source}")
    time_s = np.asarray([float(row.get("time_s", row.get("dev_ms", 0)))
                         / (1000.0 if "time_s" not in row else 1.0) for row in rows])
    current = np.asarray([
        float(row.get("current_nA", row.get("fa_fw", 0)))
        / (1_000_000.0 if "current_nA" not in row else 1.0) for row in rows
    ])
    raw_sat = np.asarray([int(float(row.get("sat", 0) or 0)) for row in rows])
    raw_ovf = np.asarray([int(float(row.get("ovf", 0) or 0)) for row in rows])
    valid = np.asarray([
        int(float(row.get("valid", 1) or 1)) != 0
        and sat == 0 and ovf == 0
        for row, sat, ovf in zip(rows, raw_sat, raw_ovf)
    ]) & np.isfinite(time_s) & np.isfinite(current)
    filtered, meta = apply_filter(time_s, current, valid, config)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", newline="") as handle:
        handle.write("# Host-side filtered view; raw source is preserved\n")
        handle.write(f"# filter: {meta}\n")
        writer = csv.writer(handle)
        writer.writerow([
            "time_s", "current_nA", "valid", "sat", "ovf", "raw_current_nA",
        ])
        for t, value, ok, sat, ovf, raw in zip(
            time_s, filtered, valid, raw_sat, raw_ovf, current
        ):
            writer.writerow([
                f"{t:.9f}", f"{value:.12g}", int(ok), int(sat), int(ovf),
                f"{raw:.12g}",
            ])
    return meta
