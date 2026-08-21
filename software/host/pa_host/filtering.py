"""Host-side, reproducible digital filtering for electrochemical traces.

The acquisition CSV is deliberately never modified.  Filtering is an analysis
view layered on top of the signed current samples.  The repeated one-pole
forward/backward implementation deliberately matches the browser preview so a
saved analysis uses the same transfer function the operator saw during a run.
"""

from __future__ import annotations

import csv
import json
import math
from pathlib import Path
from typing import Any, Sequence

import numpy as np


def read_csv_lines(path: str | Path) -> tuple[list[str], str]:
    """读取 CSV 文本行,并如实返回真正用于解码的编码名。

    🔴 中文 Windows 的 locale 默认编码是 cp936(GBK)。历史版本这里漏写了
    ``encoding=`` ⇒ 带中文的注释行以 GBK 落盘,而这些文件现在还躺在现场
    的磁盘上。所以光把写入端锁成 UTF-8 救不了他们,读取端必须容错:

    1. 先按 UTF-8 读(``utf-8-sig`` 顺手吃掉 Excel 往复后留下的 BOM);
    2. ``UnicodeDecodeError`` 时退回 ``gb18030`` —— 能把中文**正确**还原,
       比 ``errors="replace"`` 直接把中文烧成 U+FFFD 好得多;
    3. 两者都不成才用 ``errors="replace"`` 兜底。

    目标是让一个被污染的工作区变成"能用但有警告",而不是整个工作区
    直接不可用(现场实例:`已保存的工作区当前不可用:'utf-8' codec ...`)。

    与 ``it.py::_read_csv_lines`` 是同一套回退策略;本次改动被限制在
    filtering/cv 两个模块内,未做合并。
    """

    path = Path(path)
    try:
        return path.read_text(encoding="utf-8-sig").splitlines(), "utf-8"
    except UnicodeDecodeError:
        pass
    try:
        return path.read_text(encoding="gb18030").splitlines(), "gb18030"
    except UnicodeDecodeError:
        pass
    # gb18030 覆盖面极广,走到这里说明文件既不是 UTF-8 也不是 GBK 系;
    # 这时宁可让几个字符变成 U+FFFD,也不能让整个工作区打不开。
    return (
        path.read_text(encoding="utf-8", errors="replace").splitlines(),
        "utf-8/replace",
    )


FILTER_DEFAULTS: dict[str, Any] = {
    "mode": "analysis",  # off, display, analysis
    "lowpass_enabled": True,
    "lowpass_cutoff_hz": 0.3,
    "lowpass_auto": False,
    "lowpass_order": 4,
}


def validate_filter_config(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return a complete, JSON-safe filter configuration.

    This validation intentionally does not use the hardware sampling-rate
    limits.  The final Nyquist check is made against the actual trace because
    Debug, DC and EIS/CV can have different native rates.
    """

    raw = {**FILTER_DEFAULTS, **(payload or {})}
    mode = str(raw.get("mode", FILTER_DEFAULTS["mode"])).lower()
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

    lowpass_enabled = boolean("lowpass_enabled", FILTER_DEFAULTS["lowpass_enabled"])
    lowpass_auto = boolean("lowpass_auto", FILTER_DEFAULTS["lowpass_auto"])
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
    lines, source_encoding = read_csv_lines(source)
    rows: list[dict[str, str]] = list(
        csv.DictReader(line for line in lines if not line.startswith("#"))
    )
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
    # 解码用的编码是可复现性的一部分,一律记进 meta;不是 UTF-8 时还要把
    # 警告顶到 note 里,否则操作员看不出这份数据来自一个被污染的文件。
    meta["source_encoding"] = source_encoding
    if source_encoding != "utf-8":
        warning = f"源文件不是 UTF-8，已按 {source_encoding} 回退解码，建议重新导出"
        meta["note"] = f"{meta['note']} · {warning}" if meta["note"] else warning
    output.parent.mkdir(parents=True, exist_ok=True)
    # 🔴 必须显式 encoding="utf-8"。meta["note"] 带中文(如"低通截止频率已限制
    #    为奈奎斯特频率的 90%"),漏写 encoding 时中文 Windows 会按 cp936(GBK)
    #    落盘,之后任何按 UTF-8 读这个文件的代码都在那串中文上抛
    #    UnicodeDecodeError。macOS 默认 UTF-8 所以本地永远测不出来。
    with output.open("w", newline="", encoding="utf-8") as handle:
        handle.write("# Host-side filtered view; raw source is preserved\n")
        # 原来直接把 dict 的 repr 塞进注释行(中文 + 单引号混在一起,既难解析
        # 也难排错)。改用 JSON:ensure_ascii=False 保持中文可读(文件编码已锁
        # UTF-8),default=str 兜住将来可能漏进 meta 的 numpy 标量。
        handle.write(
            f"# filter: {json.dumps(meta, ensure_ascii=False, default=str)}\n"
        )
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
