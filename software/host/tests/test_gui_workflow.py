#!/usr/bin/env python3
"""Tests for isolated GUI workspaces and explicit calibration selection."""

from __future__ import annotations

import csv
import json
import tempfile
from pathlib import Path

from pa_host.gui_server import AppState, MeasurementController, SettingsController


def _point(point_id: str, concentration: float, current: float) -> dict[str, object]:
    return {
        "point_id": point_id,
        "label": point_id,
        "concentration_um": concentration,
        "current_nA": current,
    }


def test_workspace_keeps_all_candidates_but_fits_only_selected_range() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        workspace = Path(tmp) / "experiment-a"
        app = AppState()
        app.save_dir = workspace
        app._load_workspace()
        points = [
            _point("p1", 0, 100),
            _point("p2", 0, 10),
            _point("p3", 5, 20),
            _point("p4", 10, 30),
        ]
        payload = app.fit({
            "points": points,
            "selected_point_ids": ["p2", "p3", "p4"],
            "degree": 1,
        })

        assert payload["model"]["n_points"] == 3
        assert abs(app.model.predict_concentration(15) - 2.5) < 1e-9
        rows = list(csv.DictReader((workspace / "calibration-points.csv").open()))
        assert len(rows) == 4
        assert [row["selected"] for row in rows] == ["0", "1", "1", "1"]
        selection = json.loads((workspace / "calibration-selection.json").read_text())
        assert selection["selected_point_ids"] == ["p2", "p3", "p4"]


def test_switching_workspace_loads_an_independent_model() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        first = AppState()
        first.save_dir = root / "first"
        first._load_workspace()
        first.fit({
            "points": [_point("a", 0, 1), _point("b", 1, 3)],
            "selected_point_ids": ["a", "b"],
        })

        second = AppState()
        second.save_dir = root / "second"
        second._load_workspace()
        assert second.model is None
        assert second.point_records == []

        reloaded = AppState()
        reloaded.save_dir = root / "first"
        reloaded._load_workspace()
        assert reloaded.model is not None
        assert reloaded.selected_point_ids == ["a", "b"]
        assert len(reloaded.point_records) == 2


def test_raw_points_are_targeted_to_the_workspace_during_acquisition() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        app = AppState()
        app.save_dir = Path(tmp) / "live"
        app._load_workspace()
        metadata = app._prepare_export_metadata({
            "sample_name": "sample/one",
            "known_concentration_um": 5,
        })
        assert metadata["export_stem"] == "sample_one-5uM"
        assert Path(metadata["live_raw_path"]).parent == app.save_dir
        assert Path(metadata["live_raw_path"]).name == "sample_one-5uM-raw.csv"


def test_percentage_offset_tracks_selected_fsr() -> None:
    settings = SettingsController.validate({
        "fsr_nA": 1000,
        "offset_mode": "50pct",
    })
    assert settings["offset_mode"] == "50pct"
    assert settings["offset_nA"] == 500

    legacy = SettingsController.validate({"fsr_nA": 1000, "offset_nA": 80})
    assert legacy["offset_mode"] == "80nA"
    assert legacy["offset_nA"] == 80


def test_it_step_settings_keep_initial_and_target_potentials_distinct() -> None:
    settings = SettingsController.validate({
        "initial_potential_v": 0.3,
        "potential_v": 0.2,
        "prestep_s": 2.5,
    })
    assert settings["initial_potential_v"] == 0.3
    assert settings["potential_v"] == 0.2
    assert settings["prestep_s"] == 2.5

    continuously_held = SettingsController.validate({
        "initial_potential_v": 0.2,
        "potential_v": 0.2,
        "prestep_s": 0,
    })
    assert SettingsController.same_analysis_protocol(settings, continuously_held)


def test_it_uses_verified_1200mv_common_mode_across_supported_potentials() -> None:
    legacy = SettingsController.validate({
        "initial_potential_v": 0.2,
        "potential_v": 0.2,
    })
    near_rail = SettingsController.validate({
        "initial_potential_v": 0.4,
        "potential_v": 0.4,
    })

    assert SettingsController.working_electrode_mv(legacy) == 1200
    assert SettingsController.working_electrode_mv(near_rail) == 1200


def test_default_it_method_matches_archived_180_second_protocol() -> None:
    settings = SettingsController.validate({})

    assert settings["method"] == "it"
    assert settings["potential_v"] == 0.2
    assert settings["duration_s"] == 180.0
    assert settings["target_rate_hz"] == 10.0
    assert settings["fit_window_s"] == 20.0
    assert settings["fsr_nA"] == 2000
    assert settings["offset_mode"] == "10pct"
    assert settings["offset_nA"] == 200


def test_live_cv_data_reader_only_appends_new_complete_rows() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        raw = Path(tmp) / "live.csv"
        raw.write_text(
            "# live CV\n"
            "host_unix_s,seq,dev_ms,counts,fa_fw,tag,auto,ovf,sat,potential_mv,cycle,direction\n"
            "1,0,1000,1,2500000,4,0,0,0,-600,1,1\n"
            "1,1,1020,2,"
        )
        controller = MeasurementController()
        controller.settings = SettingsController.validate({"method": "cv", "cv_cycles": 1})
        controller.raw_path = raw

        first = controller._data()
        assert first["time_s"] == [0.0]
        assert first["current_nA"] == [2.5]

        with raw.open("a") as handle:
            handle.write(
                "3000000,4,0,0,0,-599,1,1\n"
                "1,2,1040,3,3500000,4,0,0,1,-598,1,1\n"
            )
        second = controller._data()
        assert second["time_s"] == [0.0, 0.02, 0.04]
        assert second["current_nA"] == [2.5, 3.0, 3.5]
        assert second["potential_v"] == [-0.6, -0.599, -0.598]
        assert second["valid"] == [True, True, False]


def test_drift_bias_shifts_curve_and_prediction_only_when_enabled() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        app = AppState()
        app.save_dir = Path(tmp) / "drift"
        app._load_workspace()
        app.fit({
            "points": [_point("zero", 0, 10), _point("ten", 10, 30)],
            "selected_point_ids": ["zero", "ten"],
        })
        app.records = [
            {
                "finished_at": "1000", "run_id": "s1", "sample_name": "transition 1",
                "sample_role": "stabilization", "known_concentration_um": "5",
                "steady_current_nA": "20", "state": "completed",
            },
            {
                "finished_at": "4600", "run_id": "s2", "sample_name": "transition 2",
                "sample_role": "stabilization", "known_concentration_um": "5",
                "steady_current_nA": "24", "state": "completed",
            },
        ]
        drift = app.calculate_drift({
            "solution_name": "5 uM transition",
            "known_concentration_um": 5,
            "start_run_id": "s1",
            "end_run_id": "s2",
            "enabled": True,
        })
        assert drift["bias_nA"] == 4
        assert drift["slope_nA_per_hour"] == 4
        assert abs(app.model_payload()["curve"]["current_nA"][0] - 14) < 1e-9
        prediction = app.predict({"current_nA": 24})
        assert abs(prediction["predicted_concentration_um"] - 5) < 1e-9
        assert prediction["bias_corrected_current_nA"] == 20

        app.toggle_drift({"enabled": False})
        prediction = app.predict({"current_nA": 24})
        assert abs(prediction["predicted_concentration_um"] - 7) < 1e-9


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            try:
                fn()
                print(f"  PASS {name}")
            except Exception as exc:
                failures += 1
                print(f"  FAIL {name}: {exc}")
    raise SystemExit(1 if failures else 0)


def test_debug_run_never_touches_the_calibration_workspace() -> None:
    """🔴 硬件 DEBUG 轮必须完全不进标定工作区。

    为什么值得一个测试:调参数时会随手跑很多轮,若它们进了
    measurement-index.csv 与标定点集合,污染要等到下次拟合曲线时才暴露,
    那时已经分不清哪几行是调试轮。这条断言把"不污染"从约定变成机器保证。
    """
    with tempfile.TemporaryDirectory() as tmp:
        app = AppState()
        app.save_dir = Path(tmp) / "ws"
        app._load_workspace()
        before = sorted(p.name for p in app.save_dir.glob("*")) \
            if app.save_dir.exists() else []

        app._measurement_completed({
            "run_id": "it_debug", "state": "completed",
            "finished_at": 1.0, "run_dir": "/tmp/x", "raw_path": "/tmp/x/raw.csv",
            "metadata": {"debug": True, "sample_name": "hw-debug",
                         "sample_role": "test"},
            "summary": {"steady_current_nA": -8.6},
        })

        after = sorted(p.name for p in app.save_dir.glob("*")) \
            if app.save_dir.exists() else []
        assert after == before, f"debug 轮写了文件:{set(after) - set(before)}"
        assert app.point_records == [], "debug 轮不该产生标定点"
        result = app.measurement.snapshot()["workflow_result"]
        assert result is not None and result["debug"] is True
        assert result.get("predicted_concentration_um") is None


def test_debug_command_line_is_validated_before_reaching_the_firmware() -> None:
    """超长/多行命令在上位机就挡掉 —— 固件侧只会回一条 too_long,不如这里说清。"""
    app = AppState()
    ctrl = app.measurement
    ctrl.state = "running"
    ctrl.cmd_path = Path(tempfile.mkdtemp()) / "cmd.txt"
    ctrl.send_command("SET fsr=2 off=4")
    assert ctrl.cmd_path.read_text(encoding="utf-8") == "SET fsr=2 off=4\n"
    for bad in ("", "   ", "SET a=1\nSTART", "SET " + "x" * 200):
        try:
            ctrl.send_command(bad)
        except ValueError:
            continue
        raise AssertionError(f"应当拒绝:{bad[:30]!r}")
    # 拒绝的命令一条都不许落进命令文件(否则固件会真的执行它)
    assert ctrl.cmd_path.read_text(encoding="utf-8") == "SET fsr=2 off=4\n"
