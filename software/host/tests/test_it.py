#!/usr/bin/env python3
"""Unit tests for the 10 Hz i-t calibration workflow."""

from __future__ import annotations

import csv
import json
import sys
import tempfile
import warnings
from dataclasses import asdict
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pa_host.it import (
    CalibrationModel,
    CalibrationPoint,
    PlateauConfig,
    detect_isolated_spikes,
    evaluate_ap_score,
    evaluate_platform,
    fit_calibration,
    load_model,
    load_run_csv,
    resample_run_10hz,
    save_model,
    summarize_run,
)


def test_ap_score_reaches_200_for_24_blue_samples() -> None:
    points = [
        {"concentration_um": float(index), "predicted_concentration_um": float(index)}
        for index in range(24)
    ]
    score = evaluate_ap_score(points)
    assert score["ms"] == pytest.approx(10)
    assert score["sc"] == pytest.approx(10)
    assert score["final_score"] == pytest.approx(200)
    assert score["stats"]["blue_count"] == 24


def test_ap_score_uses_fixed_24_denominator_and_breaks_grey_streak() -> None:
    points = [
        {"concentration_um": 5, "predicted_concentration_um": 7},  # blue edge
        {"concentration_um": 5, "predicted_concentration_um": 8},  # green midpoint
        {"concentration_um": 5, "predicted_concentration_um": 20},  # grey break
        {"concentration_um": 10, "predicted_concentration_um": 12},  # blue edge
    ]
    score = evaluate_ap_score(points)
    assert score["ms"] == pytest.approx(10 * 2.5 / 24)
    assert score["longest_weighted_streak"] == pytest.approx(1.5)
    assert score["sc"] == 0
    assert score["stats"]["green_count"] == 1
    assert score["stats"]["grey_count"] == 1


def test_ap_uninvertible_measured_point_is_grey_and_breaks_streak() -> None:
    score = evaluate_ap_score([
        {"concentration_um": 5, "predicted_concentration_um": 5},
        {"concentration_um": 5, "predicted_concentration_um": None},
        {"concentration_um": 5, "predicted_concentration_um": 5},
    ])
    assert [point["zone"] for point in score["points"]] == ["blue", "grey", "blue"]
    assert score["longest_weighted_streak"] == pytest.approx(1)


def test_inverse_root_selection_is_deterministic_for_ambiguous_quadratic() -> None:
    model = CalibrationModel(
        degree=2, coefficients=(-1.0, 10.0, -25.0),
        concentration_min_um=0.0, concentration_max_um=10.0,
        r2=1.0, rmse_nA=0.0, n_points=3,
    )
    assert model.predict_concentration(-4.0) == pytest.approx(7.0)


def test_inverse_keeps_a_small_but_nonzero_linear_slope() -> None:
    model = CalibrationModel(
        degree=1, coefficients=(1e-14, 0.0),
        concentration_min_um=0.0, concentration_max_um=10.0,
        r2=1.0, rmse_nA=0.0, n_points=2,
    )
    assert model.predict_concentration(2e-14) == pytest.approx(2.0)


def test_linear_fit_and_inverse() -> None:
    points = [CalibrationPoint(x, 2.0 * x + 1.0) for x in (0.0, 1.0, 2.0, 4.0)]
    model = fit_calibration(points)
    assert model.degree == 1
    assert abs(model.predict_concentration(7.0) - 3.0) < 1e-9
    assert model.r2 > 0.999999


def test_calibration_rejects_negative_or_near_zero_response() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        fit_calibration([CalibrationPoint(-1, 1), CalibrationPoint(1, 3)])
    with pytest.raises(ValueError, match="too small"):
        fit_calibration([CalibrationPoint(0, 5), CalibrationPoint(10, 5)])


def test_quadratic_fit_requires_distinct_concentrations_and_monotonicity() -> None:
    with pytest.raises(ValueError, match="3 distinct"):
        fit_calibration([
            CalibrationPoint(0, 0), CalibrationPoint(0, 0.1),
            CalibrationPoint(1, 1),
        ], degree=2)
    with pytest.raises(ValueError, match="monotonic"):
        fit_calibration([
            CalibrationPoint(0, 0), CalibrationPoint(5, 25),
            CalibrationPoint(10, 0),
        ], degree=2)

    monotonic = fit_calibration([
        CalibrationPoint(0, 0), CalibrationPoint(1, 1),
        CalibrationPoint(2, 4),
    ], degree=2)
    assert monotonic.predict_concentration(1) == pytest.approx(1)


def test_higher_order_fit_also_rejects_a_curve_that_reverses_direction() -> None:
    with pytest.raises(ValueError, match="monotonic"):
        fit_calibration([
            CalibrationPoint(x, (x - 1) * (x - 2) * (x - 3))
            for x in (0, 1, 2, 3, 4)
        ], degree=3)

    model = fit_calibration([
        CalibrationPoint(x, x ** 3) for x in (0, 1, 2, 3)
    ], degree=3)
    assert model.predict_concentration(8) == pytest.approx(2)


def test_polyfit_rank_warning_is_rejected(monkeypatch) -> None:
    rank_warning = getattr(getattr(np, "exceptions", np), "RankWarning", None)
    if rank_warning is None:
        rank_warning = np.RankWarning

    def warn_polyfit(*_args, **_kwargs):
        warnings.warn("poorly conditioned", rank_warning)
        return np.asarray([1.0, 0.0])

    monkeypatch.setattr(np, "polyfit", warn_polyfit)
    with pytest.raises(ValueError, match="rank-deficient"):
        fit_calibration([CalibrationPoint(0, 0), CalibrationPoint(1, 1)])


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


@pytest.mark.parametrize("encoding", ["utf-8", "gb18030"])
def test_run_loader_supports_utf8_and_legacy_windows_csv(
    tmp_path: Path, encoding: str,
) -> None:
    path = tmp_path / f"run-{encoding}.csv"
    path.write_bytes((
        "# 固件状态 ⇒ 已应用\n"
        "dev_ms,fa_fw,sat\n"
        "0,-1000000,0\n100,-2000000,0\n200,-3000000,0\n"
    ).encode(encoding))

    time_s, current_nA, valid = load_run_csv(path)

    assert time_s.tolist() == pytest.approx([0.0, 0.1, 0.2])
    assert current_nA.tolist() == pytest.approx([-1.0, -2.0, -3.0])
    assert valid.tolist() == [True, True, True]


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


def test_fifo_overflow_row_is_invalid_in_it_loader_and_resampling() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / "source.csv"
        target = Path(tmp) / "target.csv"
        with source.open("w", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["seq", "dev_ms", "fa_fw", "sat", "ovf"])
            for index in range(40):
                writer.writerow([index, index * 100, 2_000_000, 0,
                                 int(index == 20)])
        _, _, valid = load_run_csv(source)
        resample_run_10hz(source, target, 3.9, 10.0)
        rows = list(csv.DictReader(line for line in target.open()
                                   if not line.startswith("#")))

    assert not valid[20]
    assert rows[20]["valid"] == "0"


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


def test_plateau_config_defaults_validate_and_serialize() -> None:
    defaults = PlateauConfig()

    assert PlateauConfig.validate() == defaults
    assert PlateauConfig.validate(defaults) is defaults
    assert PlateauConfig.validate(defaults.to_dict()) == defaults
    assert defaults.to_dict() == asdict(defaults)
    assert defaults.window_duration_s == pytest.approx(30.0)


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"unknown": 1}, "未知"),
        ({"segment_duration_s": True}, "必须是数值"),
        ({"segment_duration_s": float("nan")}, "有限数"),
        ({"absolute_tolerance_nA": float("inf")}, "有限数"),
        ({"segment_count": 3}, "必须是偶数"),
        ({"segment_count": 6.5}, "必须是整数"),
        ({"required_consecutive_windows": False}, "必须是整数"),
        ({"minimum_coverage_ratio": 0}, "范围内"),
        ({"relative_tolerance": 1.01}, "范围内"),
        ({"maximum_gap_periods": -1}, "范围内"),
        ({"spike_scale_multiplier": 0}, "范围内"),
        ({
            "segment_duration_s": 0.5,
            "segment_count": 2,
            "required_consecutive_windows": 1,
        }, "末段拟合窗口"),
    ],
)
def test_plateau_config_strict_validation(
    payload: dict[str, object], message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        PlateauConfig.validate(payload)


def test_plateau_config_accepts_documented_boundaries() -> None:
    config = PlateauConfig(
        segment_duration_s=0.5,
        segment_count=60,
        absolute_tolerance_nA=0,
        relative_tolerance=1,
        scatter_multiplier=0,
        minimum_coverage_ratio=1,
        maximum_gap_periods=100,
        required_consecutive_windows=100,
        spike_scale_multiplier=1000,
        spike_neighbor_multiplier=1000,
    )

    assert config.window_duration_s == pytest.approx(30.0)
    with pytest.raises(ValueError, match="JSON 对象"):
        PlateauConfig.validate([])  # type: ignore[arg-type]


def test_spike_detector_multipliers_are_configurable() -> None:
    current = np.asarray([
        0.0, 0.1, 0.2, 0.1, 0.0, 0.1, 0.2, 1.0,
        0.2, 0.1, 0.0, 0.1, 0.2, 0.1, 0.0,
    ])
    valid = np.ones(len(current), dtype=bool)

    assert not np.any(detect_isolated_spikes(current, valid))
    assert detect_isolated_spikes(current, valid, 2.0, 1.0)[7]
    assert not np.any(detect_isolated_spikes(current, valid, 100.0, 100.0))
    with pytest.raises(ValueError, match="spike_scale_multiplier"):
        detect_isolated_spikes(current, valid, True, 1.0)


def test_platform_detection_accepts_flat_trace_and_removes_isolated_spike() -> None:
    time_s = np.arange(0.0, 35.1, 0.1)
    current = np.full(len(time_s), 12.0)
    current[200] = 900.0
    result = evaluate_platform(time_s, current, np.ones(len(time_s), dtype=bool))

    assert result is not None
    assert result.complete_segment == 7
    assert result.stable
    assert result.isolated_spikes_removed == 1
    assert result.delta_30s_nA is not None and result.delta_30s_nA < 1e-9


def test_platform_detection_default_config_is_backward_compatible() -> None:
    time_s = np.arange(0.0, 35.1, 0.1)
    current = 8.0 + 0.001 * time_s
    valid = np.ones(len(time_s), dtype=bool)

    legacy = evaluate_platform(time_s, current, valid)
    configured = evaluate_platform(
        time_s, current, valid, config=PlateauConfig().to_dict(),
    )

    assert configured == legacy


def test_platform_detection_uses_custom_window_geometry() -> None:
    config = PlateauConfig(segment_duration_s=2.0, segment_count=10)
    time_s = np.arange(0.0, 24.1, 0.1)
    result = evaluate_platform(
        time_s, np.full(len(time_s), 4.0), np.ones(len(time_s), dtype=bool),
        config=config,
    )

    assert result is not None
    assert result.stable
    assert result.complete_segment == 12
    assert result.window_start_s == pytest.approx(4.0)
    assert result.window_end_s == pytest.approx(24.0)
    assert result.segment_centres_s == pytest.approx(
        (5.0, 7.0, 9.0, 11.0, 13.0, 15.0, 17.0, 19.0, 21.0, 23.0)
    )
    assert result.config is config


def test_completed_window_does_not_use_future_filter_samples() -> None:
    time_s = np.arange(0.0, 35.0, 0.1)
    current = np.where(time_s < 30.0, 0.0, 1.0)
    valid = np.ones(len(time_s), dtype=bool)
    filter_config = {
        "mode": "analysis",
        "lowpass_enabled": True,
        "lowpass_auto": False,
        "lowpass_cutoff_hz": 0.1,
        "lowpass_order": 2,
    }
    plateau_config = PlateauConfig(
        absolute_tolerance_nA=0.1,
        relative_tolerance=0.0,
        scatter_multiplier=0.0,
    )

    just_completed = evaluate_platform(
        time_s[:302], current[:302], valid[:302], filter_config,
        expected_sample_rate_hz=10.0, config=plateau_config,
    )
    polled_late = evaluate_platform(
        time_s, current, valid, filter_config,
        expected_sample_rate_hz=10.0, config=plateau_config,
        decision_segment=6,
    )

    assert just_completed is not None and polled_late is not None
    assert just_completed.complete_segment == polled_late.complete_segment == 6
    assert polled_late.segment_means_nA == pytest.approx(
        just_completed.segment_means_nA
    )
    assert polled_late.delta_30s_nA == pytest.approx(just_completed.delta_30s_nA)
    assert polled_late.stable is just_completed.stable


def test_platform_detection_uses_custom_tolerance() -> None:
    time_s = np.arange(0.0, 35.1, 0.1)
    current = 5.0 + 0.02 * time_s
    valid = np.ones(len(time_s), dtype=bool)

    default_result = evaluate_platform(time_s, current, valid)
    relaxed_result = evaluate_platform(
        time_s, current, valid,
        config={"absolute_tolerance_nA": 1.0},
    )

    assert default_result is not None and not default_result.stable
    assert relaxed_result is not None and relaxed_result.stable
    assert relaxed_result.tolerance_nA == pytest.approx(1.0)


def test_platform_evaluation_exposes_signed_fit_diagnostics() -> None:
    time_s = np.arange(0.0, 35.1, 0.1)
    current = 5.0 - 0.001 * time_s
    result = evaluate_platform(
        time_s, current, np.ones(len(time_s), dtype=bool),
        filter_config={"mode": "off"},
    )

    assert result is not None
    assert result.status == "stable"
    assert result.fit_intercept_nA == pytest.approx(5.0, abs=1e-4)
    assert result.slope_nA_per_s == pytest.approx(-0.001)
    assert result.trend_delta_nA == pytest.approx(-0.03)
    assert result.delta_30s_nA == pytest.approx(0.03)
    assert result.first_half_mean_nA is not None
    assert result.second_half_mean_nA is not None
    assert result.half_delta_signed_nA is not None
    assert result.half_delta_signed_nA < 0
    assert result.delta_half_nA == pytest.approx(abs(result.half_delta_signed_nA))
    assert result.segment_centres_s == pytest.approx((7.5, 12.5, 17.5, 22.5, 27.5, 32.5))
    assert result.config == PlateauConfig()


def test_platform_detection_rejects_drift_and_invalid_window() -> None:
    time_s = np.arange(0.0, 35.1, 0.1)
    drifting = 5.0 + 0.02 * time_s
    valid = np.ones(len(time_s), dtype=bool)
    result = evaluate_platform(time_s, drifting, valid)

    assert result is not None
    assert not result.stable
    assert result.delta_30s_nA is not None and result.delta_30s_nA > result.tolerance_nA

    valid[200] = False
    invalid = evaluate_platform(time_s, drifting, valid)
    assert invalid is not None
    assert not invalid.stable
    assert "无效点" in invalid.reason


def test_platform_detection_rejects_insufficient_nominal_sample_coverage() -> None:
    time_s = np.arange(0.0, 35.0, 0.5)
    result = evaluate_platform(
        time_s, np.full(len(time_s), 4.0), np.ones(len(time_s), dtype=bool),
        expected_sample_rate_hz=10.0,
    )

    assert result is not None
    assert not result.stable
    assert "有效点不足" in result.reason


@pytest.mark.parametrize("period_ms", [124, 242, 476, 945, 1882, 3757])
def test_platform_detection_supports_every_hardware_sample_period(
    period_ms: int,
) -> None:
    rate_hz = 1000.0 / period_ms
    time_s = np.arange(0.0, 36.0, 1.0 / rate_hz)
    result = evaluate_platform(
        time_s, np.full(len(time_s), 4.0), np.ones(len(time_s), dtype=bool),
        expected_sample_rate_hz=rate_hz,
    )

    assert result is not None
    assert result.stable, result.reason


def test_platform_detection_rejects_excessive_sample_gap() -> None:
    time_s = np.arange(0.0, 35.1, 0.1)
    # The gap straddles a five-second boundary, so checking each segment in
    # isolation would miss it even though the continuous stream lost samples.
    keep = ~np.isin(np.round(time_s, 1), [9.9, 10.0, 10.1])
    time_s = time_s[keep]
    result = evaluate_platform(
        time_s, np.full(len(time_s), 4.0), np.ones(len(time_s), dtype=bool),
        expected_sample_rate_hz=10.0,
    )

    assert result is not None
    assert not result.stable
    assert "采样间隔过大" in result.reason


def test_adaptive_resampling_uses_actual_trace_end() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / "source.csv"
        target = Path(tmp) / "target.csv"
        with source.open("w", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["time_s", "current_nA", "valid", "sat"])
            for i in range(282):
                writer.writerow([i / 8.0, 3.0, 1, 0])
        resample_run_10hz(source, target, None, 10.0)
        rows = list(csv.DictReader(line for line in target.open()
                                   if not line.startswith("#")))

    assert float(rows[-1]["time_s"]) <= 281 / 8.0
    assert float(rows[-1]["time_s"]) > 35.0


def test_resample_never_overwrites_source() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "run.csv"
        path.write_text(
            "time_s,current_nA,valid,sat\n0,1,1,0\n1,1,1,0\n2,1,1,0\n",
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="不能覆盖原始"):
            resample_run_10hz(path, path)


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
