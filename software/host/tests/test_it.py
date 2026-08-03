#!/usr/bin/env python3
"""Unit tests for the 10 Hz i-t calibration workflow."""

from __future__ import annotations

import csv
import json
import sys
import tempfile
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pa_host.it import (
    CalibrationPoint,
    detect_isolated_spikes,
    fit_calibration,
    load_model,
    load_run_csv,
    resample_run_10hz,
    save_model,
    summarize_run,
)


def test_linear_fit_and_inverse() -> None:
    points = [CalibrationPoint(x, 2.0 * x + 1.0) for x in (0.0, 1.0, 2.0, 4.0)]
    model = fit_calibration(points)
    assert model.degree == 1
    assert abs(model.predict_concentration(7.0) - 3.0) < 1e-9
    assert model.r2 > 0.999999


def test_model_round_trip() -> None:
    model = fit_calibration([CalibrationPoint(0, 1), CalibrationPoint(1, 3)])
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "model.json"
        save_model(model, path)
        loaded = load_model(path)
        assert loaded.to_json() == json.loads(path.read_text())
    assert abs(loaded.predict_concentration(5) - 2.0) < 1e-9


def test_final_window_summary_ignores_invalid_rows() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "run.csv"
        with path.open("w", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["time_s", "current_nA", "valid", "sat"])
            for i in range(1200):
                t = i / 10.0
                writer.writerow([t, 2.0 if t < 100 else 4.0, 0 if i == 1190 else 1, 0])
        summary = summarize_run(path, window_s=20.0)
    assert summary.sample_count == 1200
    assert summary.valid_count == 1199
    assert summary.fit_count == 200
    assert abs(summary.steady_current_nA - ((2.0 + 199.0 * 4.0) / 200.0)) < 1e-12


def test_firmware_fa_column_is_signed_nA() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "run.csv"
        with path.open("w", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["dev_ms", "fa_fw", "sat"])
            for i in range(30):
                writer.writerow([i * 100, -2_000_000, 0])
        t, current, valid = load_run_csv(path)
    assert np.allclose(current, -2.0)
    assert np.all(valid)
    assert abs(t[-1] - 2.9) < 1e-12


def test_loader_keeps_final_segment_after_firmware_reset() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "run.csv"
        with path.open("w", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["seq", "dev_ms", "fa_fw", "sat"])
            for seq in range(4):
                writer.writerow([seq, seq * 100, -1_000_000, 0])
            for seq in range(30):
                writer.writerow([seq, seq * 100, -2_000_000, 0])
        t, current, valid = load_run_csv(path)
    assert len(t) == 30
    assert abs(t[-1] - 2.9) < 1e-12
    assert np.allclose(current, -2.0)
    assert np.all(valid)


def test_resample_has_fixed_point_count_and_invalid_mask() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / "source.csv"
        target = Path(tmp) / "target.csv"
        with source.open("w", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["time_s", "current_nA", "valid", "sat"])
            for i in range(100):
                writer.writerow([i / 8.0, 3.0, int(i >= 2), int(i < 2)])
        resample_run_10hz(source, target, 12.0, 10.0)
        rows = list(csv.DictReader(line for line in target.open()
                                   if not line.startswith("#")))
    assert len(rows) == 120
    assert rows[0]["valid"] == "0"
    assert rows[1]["valid"] == "0"
    assert rows[-1]["valid"] == "1"
    assert abs(float(rows[-1]["time_s"]) - 11.9) < 1e-12


def test_isolated_spike_is_excluded_but_raw_value_is_preserved() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "run.csv"
        with path.open("w", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["time_s", "current_nA", "valid", "sat"])
            for i in range(100):
                writer.writerow([i / 10.0, 900.0 if i == 50 else 2.0, 1, 0])
        _, current, valid = load_run_csv(path)
        summary = summarize_run(path, window_s=10.0)
    assert current[50] == 900.0
    assert not valid[50]
    assert summary.valid_count == 99
    assert any("isolated single-sample" in warning for warning in summary.warnings)


def test_sustained_current_step_is_not_classified_as_spike() -> None:
    current = np.asarray([0.0] * 25 + [10.0] * 25)
    mask = detect_isolated_spikes(current, np.ones(len(current), dtype=bool))
    assert not np.any(mask)


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            try:
                fn()
                print(f"  ✓ {name}")
            except AssertionError as exc:
                failures += 1
                print(f"  ✗ {name}: {exc}")
    print(f"\n{'✅ 全部通过' if failures == 0 else f'❌ {failures} 项失败'}")
    raise SystemExit(1 if failures else 0)
