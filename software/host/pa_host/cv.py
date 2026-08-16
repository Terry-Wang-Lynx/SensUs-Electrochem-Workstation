"""CV raw-data loading, export, summary, and plotting."""

from __future__ import annotations

import csv
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np


@dataclass(frozen=True)
class CVSummary:
    path: str
    sample_count: int
    valid_count: int
    saturated_count: int
    cycles_requested: int
    cycles_observed: int
    potential_min_v: float | None
    potential_max_v: float | None
    current_min_nA: float | None
    current_max_nA: float | None
    last_cycle_forward_peak_nA: float | None
    last_cycle_reverse_peak_nA: float | None
    duration_s: float
    warnings: tuple[str, ...]


def load_cv_run(path: str | Path) -> dict[str, np.ndarray]:
    path = Path(path)
    rows: list[dict[str, str]] = []
    with path.open(newline="") as handle:
        reader = csv.DictReader(line for line in handle if not line.startswith("#"))
        if reader.fieldnames is None or "potential_mv" not in reader.fieldnames:
            raise ValueError("文件不包含 CV 电位列")
        rows = list(reader)
    if not rows:
        empty = np.array([], dtype=float)
        return {
            "time_s": empty,
            "potential_v": empty,
            "current_nA": empty,
            "cycle": np.array([], dtype=int),
            "direction": np.array([], dtype=int),
            "valid": np.array([], dtype=bool),
        }
    dev_ms = np.array([float(row["dev_ms"]) for row in rows], dtype=float)
    potential = np.array([float(row["potential_mv"]) / 1000 for row in rows])
    current = np.array([float(row["fa_fw"]) / 1_000_000 for row in rows])
    cycle = np.array([int(row["cycle"]) for row in rows], dtype=int)
    direction = np.array([int(row["direction"]) for row in rows], dtype=int)
    valid = np.array([
        int(row.get("sat") or 0) == 0 and int(row.get("ovf") or 0) == 0
        for row in rows
    ], dtype=bool)
    return {
        "time_s": (dev_ms - dev_ms[0]) / 1000,
        "potential_v": potential,
        "current_nA": current,
        "cycle": cycle,
        "direction": direction,
        "valid": valid,
    }


def summarize_cv(path: str | Path, settings: dict[str, Any]) -> CVSummary:
    data = load_cv_run(path)
    valid = data["valid"]
    cycles_observed = int(data["cycle"].max()) if len(data["cycle"]) else 0
    warnings: list[str] = []
    saturated = int((~valid).sum())
    if saturated:
        warnings.append(f"{saturated} 个点饱和或 FIFO 溢出")
    requested = int(settings.get("cv_cycles", 0))
    if cycles_observed < requested:
        warnings.append(f"仅观察到 {cycles_observed}/{requested} 圈")
    selected = valid
    last_forward = valid & (data["cycle"] == cycles_observed) & (data["direction"] == 1)
    last_reverse = valid & (data["cycle"] == cycles_observed) & (data["direction"] == -1)

    def min_or_none(values: np.ndarray, mask: np.ndarray) -> float | None:
        return float(values[mask].min()) if mask.any() else None

    def max_or_none(values: np.ndarray, mask: np.ndarray) -> float | None:
        return float(values[mask].max()) if mask.any() else None

    return CVSummary(
        path=str(path),
        sample_count=len(valid),
        valid_count=int(valid.sum()),
        saturated_count=saturated,
        cycles_requested=requested,
        cycles_observed=cycles_observed,
        potential_min_v=min_or_none(data["potential_v"], selected),
        potential_max_v=max_or_none(data["potential_v"], selected),
        current_min_nA=min_or_none(data["current_nA"], selected),
        current_max_nA=max_or_none(data["current_nA"], selected),
        last_cycle_forward_peak_nA=max_or_none(data["current_nA"], last_forward),
        last_cycle_reverse_peak_nA=min_or_none(data["current_nA"], last_reverse),
        duration_s=float(data["time_s"][-1]) if len(data["time_s"]) else 0.0,
        warnings=tuple(warnings),
    )


def export_cv_csv(raw_path: str | Path, output_path: str | Path,
                  settings: dict[str, Any]) -> Path:
    data = load_cv_run(raw_path)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="") as handle:
        handle.write("SensUs Electrochemistry Workstation\n")
        handle.write("Cyclic Voltammetry\n")
        handle.write(f"Init E (V) = {settings['cv_low_v']:.3f}\n")
        handle.write(f"High E (V) = {settings['cv_high_v']:.3f}\n")
        handle.write(f"Low E (V) = {settings['cv_low_v']:.3f}\n")
        handle.write("Init P/N = P\n")
        handle.write(f"Scan Rate (V/s) = {settings['cv_scan_rate_v_s']:.4g}\n")
        handle.write(f"Cycle = {settings['cv_cycles']}\n")
        handle.write(f"Segment = {settings['cv_cycles'] * 2}\n")
        handle.write(f"Potential Step (V) = {settings['cv_step_v']:.4g}\n")
        handle.write(f"Quiet Time (sec) = {settings['cv_quiet_s']:.4g}\n\n")
        writer = csv.writer(handle)
        writer.writerow([
            "Potential (V)", "Current (A)", "Current (uA)", "Current (nA)", "Time (s)",
            "Cycle", "Direction", "Valid",
        ])
        for potential, current, time_s, cycle, direction, valid in zip(
                data["potential_v"], data["current_nA"], data["time_s"],
                data["cycle"], data["direction"], data["valid"]):
            writer.writerow([
                f"{potential:.6f}", f"{current * 1e-9:.9e}",
                f"{current / 1000:.9f}", f"{current:.6f}",
                f"{time_s:.3f}", int(cycle), "forward" if direction > 0 else "reverse",
                int(valid),
            ])
    return output_path


def save_cv_summary(summary: CVSummary, path: str | Path) -> None:
    Path(path).write_text(
        json.dumps(asdict(summary), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def plot_cv(path: str | Path, output: str | Path) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    data = load_cv_run(path)
    fig, ax = plt.subplots(figsize=(8.8, 5.4))
    cycles = sorted(set(int(value) for value in data["cycle"]))
    for cycle in cycles:
        mask = data["valid"] & (data["cycle"] == cycle)
        if not mask.any():
            continue
        is_last = cycle == cycles[-1]
        ax.plot(data["potential_v"][mask], data["current_nA"][mask] / 1000,
                color="#117a65" if is_last else "#9eaaad",
                alpha=1 if is_last else 0.28, linewidth=1.4 if is_last else 0.6,
                label=f"Cycle {cycle}" if is_last else None)
    invalid = ~data["valid"]
    if invalid.any():
        ax.scatter(data["potential_v"][invalid], data["current_nA"][invalid] / 1000,
                   s=8, color="#b94444", label="Invalid")
    ax.set(xlabel="Potential vs RE (V)", ylabel="Signed current (uA)")
    ax.grid(alpha=0.2)
    if cycles:
        ax.legend(frameon=False)
    fig.tight_layout()
    fig.savefig(output, dpi=180)
    plt.close(fig)
