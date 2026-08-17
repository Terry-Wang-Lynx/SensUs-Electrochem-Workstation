#!/usr/bin/env python3
"""Tests for isolated GUI workspaces and explicit calibration selection."""

from __future__ import annotations

import csv
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from unittest.mock import Mock, call, patch

import numpy as np
import pytest

from pa_host import gui_server
from pa_host.diagnostics import DiagnosticStore
from pa_host.gui_server import (
    AppState,
    MeasurementController,
    PlateauController,
    RequestHandler,
    SettingsController,
    _browse_workspace_directory,
    _escape_applescript,
    _notify_measurement_completion,
    _send_system_notification,
    _existing_toolchain_path,
    serve,
)
from pa_host.it import PlateauConfig


def _hex_record(address: int, record_type: int, data: bytes = b"") -> str:
    body = bytes((len(data), address >> 8, address & 0xFF, record_type)) + data
    checksum = (-sum(body)) & 0xFF
    return ":" + (body + bytes((checksum,))).hex().upper()


def _write_hex(path: Path, *records: tuple[int, int, bytes]) -> None:
    lines = [_hex_record(address, record_type, data)
             for address, record_type, data in records]
    path.write_text("\n".join(lines) + "\n", encoding="ascii")


def _point(point_id: str, concentration: float, current: float) -> dict[str, object]:
    return {
        "point_id": point_id,
        "label": point_id,
        "concentration_um": concentration,
        "current_nA": current,
    }


def _complete_adaptive_calibration_point(
    app: AppState,
    *,
    point_id: str,
    concentration: float,
    current: float,
    plateau: PlateauConfig,
) -> None:
    settings = SettingsController.validate({"adaptive_stop": True})
    with patch("pa_host.gui_server._send_system_notification"):
        app._measurement_completed({
            "run_id": point_id,
            "state": "completed",
            "finished_at": float(len(app.point_records) + 1),
            "metadata": {
                "sample_name": point_id,
                "sample_role": "calibration",
                "known_concentration_um": concentration,
            },
            "summary": {
                "steady_current_nA": current,
                "adaptive_stop": {
                    "enabled": True,
                    "config": plateau.to_dict(),
                },
            },
            "settings": settings,
            "raw_path": str(app.save_dir / f"missing-{point_id}-raw.csv"),
            "resampled_path": str(app.save_dir / f"missing-{point_id}.csv"),
            "filtered_path": str(app.save_dir / f"missing-{point_id}-filtered.csv"),
        })


def _fit_adaptive_workspace(workspace: Path, plateau: PlateauConfig) -> AppState:
    app = AppState()
    app.save_dir = workspace
    app._load_workspace()
    app.settings.settings = SettingsController.validate({"adaptive_stop": True})
    app.plateau.settings = plateau
    _complete_adaptive_calibration_point(
        app, point_id="adaptive-zero", concentration=0, current=1, plateau=plateau
    )
    _complete_adaptive_calibration_point(
        app, point_id="adaptive-ten", concentration=10, current=3, plateau=plateau
    )
    app.fit({"points": app.points_payload()["points"]})
    return app


def _request_body(
    payload: bytes, *, content_type: str = "application/json",
    host: str = "127.0.0.1:8769", origin: str | None = None,
) -> dict[str, object]:
    handler = RequestHandler.__new__(RequestHandler)
    handler.headers = {
        "Content-Type": content_type,
        "Content-Length": str(len(payload)),
        "Host": host,
    }
    if origin is not None:
        handler.headers["Origin"] = origin
    handler.rfile = io.BytesIO(payload)
    return handler._body()


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
        assert payload["points_revision"] == app.points_payload()["points_revision"]


@pytest.mark.parametrize("activity", ["measurement", "schedule"])
def test_busy_fit_uses_only_persisted_candidates_and_completed_tests(
    activity: str,
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        app = AppState()
        app.save_dir = Path(tmp) / f"busy-{activity}"
        app._load_workspace()
        with patch("pa_host.gui_server.time.time", return_value=100):
            app.fit({
                "points": [_point("zero", 0, 10), _point("ten", 10, 30)],
                "selected_point_ids": ["zero", "ten"],
            })
        candidate_payload = app.points_payload()
        browser_points = [dict(point) for point in candidate_payload["points"]]
        browser_points[0]["current_nA"] = 999
        for record in (
            {
                "finished_at": 110, "run_id": "test-completed",
                "sample_name": "已完成", "sample_role": "test",
                "known_concentration_um": 5, "steady_current_nA": 20,
                "state": "completed",
            },
            {
                "finished_at": 111, "run_id": "test-running",
                "sample_name": "采集中", "sample_role": "test",
                "known_concentration_um": 5, "steady_current_nA": 21,
                "state": "running",
            },
            {
                "finished_at": 112, "run_id": "test-no-summary",
                "sample_name": "无稳态摘要", "sample_role": "test",
                "known_concentration_um": 5, "steady_current_nA": None,
                "state": "completed",
            },
        ):
            app._append_record(record)

        if activity == "measurement":
            with app.measurement.lock:
                app.measurement.state = "running"
                app.measurement.run_id = "active-run"
                app.measurement.metadata = {"sample_name": "活动测量"}
            before = app.measurement.snapshot()
        else:
            with app.schedule.lock:
                app.schedule.active = True
                app.schedule.message = "自动任务进行中"
            before = app.schedule.snapshot()

        with patch("pa_host.gui_server.time.time", return_value=120):
            result = app.fit({
                "points": browser_points,
                "points_revision": candidate_payload["points_revision"],
                "selected_point_ids": ["zero", "ten"],
            })

        assert result["model"]["coefficients"] == pytest.approx([2, 10])
        assert result["model_created_at"] == 120
        assert result["validation_started_at"] == 100
        assert [point["run_id"] for point in result["validation_points"]] == [
            "test-completed"
        ]
        assert [point["point_id"] for point in result["points"]] == [
            "zero", "ten"
        ]
        after = (
            app.measurement.snapshot()
            if activity == "measurement" else app.schedule.snapshot()
        )
        assert after == before

        reloaded = AppState()
        reloaded.save_dir = app.save_dir
        reloaded._load_workspace()
        assert reloaded.model_created_at == 120
        assert reloaded.validation_started_at == 100
        assert [
            point["run_id"] for point in reloaded.model_payload()["validation_points"]
        ] == ["test-completed"]


def test_busy_fit_preserves_a_candidate_added_after_the_browser_snapshot() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        app = AppState()
        app.save_dir = Path(tmp) / "busy-stale-browser"
        app._load_workspace()
        app.fit({
            "points": [_point("zero", 0, 10), _point("ten", 10, 30)],
            "selected_point_ids": ["zero", "ten"],
        })
        stale = app.points_payload()
        with patch("pa_host.gui_server._send_system_notification"):
            app._measurement_completed({
                "run_id": "new-completed-point",
                "state": "completed",
                "finished_at": 50,
                "metadata": {
                    "sample_name": "后台新点",
                    "sample_role": "calibration",
                    "known_concentration_um": 20,
                },
                "summary": {"steady_current_nA": 50},
                "settings": app.settings.snapshot()["settings"],
                "raw_path": str(app.save_dir / "missing-raw.csv"),
                "resampled_path": str(app.save_dir / "missing.csv"),
                "filtered_path": str(app.save_dir / "missing-filtered.csv"),
            })
        assert stale["points_revision"] != app.points_payload()["points_revision"]

        with pytest.raises(RuntimeError, match="后台更新"):
            app.fit({
                "points": stale["points"],
                "points_revision": stale["points_revision"],
                "selected_point_ids": ["zero", "ten"],
            })
        assert [record["point_id"] for record in app.point_records] == [
            "zero", "ten", "new-completed-point"
        ]

        with app.measurement.lock:
            app.measurement.state = "running"
        result = app.fit({
            "points": stale["points"],
            "points_revision": stale["points_revision"],
            "selected_point_ids": ["zero", "ten"],
        })

        assert [point["point_id"] for point in result["points"]] == [
            "zero", "ten", "new-completed-point"
        ]
        assert result["model"]["n_points"] == 2
        assert result["selected_point_ids"] == ["zero", "ten"]
        with (app.save_dir / "calibration-points.csv").open(
            encoding="utf-8", newline=""
        ) as points_file:
            rows = list(csv.DictReader(points_file))
        assert [row["point_id"] for row in rows] == [
            "zero", "ten", "new-completed-point"
        ]


def test_busy_fit_rejects_an_unpersisted_active_run_point() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        app = AppState()
        app.save_dir = Path(tmp) / "busy-active-point"
        app._load_workspace()
        app.fit({
            "points": [_point("zero", 0, 10), _point("ten", 10, 30)],
            "selected_point_ids": ["zero", "ten"],
        })
        saved = app.points_payload()
        model_before = (app.save_dir / "calibration-model.json").read_text()
        with app.measurement.lock:
            app.measurement.state = "running"
            app.measurement.run_id = "active-run"

        with pytest.raises(RuntimeError, match="已完成并保存"):
            app.fit({
                "points": [*saved["points"], _point("active-run", 20, 999)],
                "points_revision": saved["points_revision"],
                "selected_point_ids": ["zero", "ten", "active-run"],
            })

        assert [record["point_id"] for record in app.point_records] == [
            "zero", "ten"
        ]
        assert (app.save_dir / "calibration-model.json").read_text() == model_before


def test_fit_waits_for_measurement_state_without_holding_the_workspace_lock() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        app = AppState()
        app.save_dir = Path(tmp) / "fit-lock-order"
        app._load_workspace()
        app.fit({
            "points": [_point("zero", 0, 10), _point("ten", 10, 30)],
            "selected_point_ids": ["zero", "ten"],
        })
        saved = app.points_payload()
        measurement_locked = threading.Event()
        measurement_check_started = threading.Event()
        try_workspace_lock = threading.Event()
        workspace_lock_acquired = threading.Event()
        fit_finished = threading.Event()
        fit_errors: list[Exception] = []
        original_is_busy = app.measurement.is_busy

        def observed_is_busy() -> bool:
            measurement_check_started.set()
            return original_is_busy()

        def terminal_callback_order() -> None:
            with app.measurement.lock:
                measurement_locked.set()
                assert try_workspace_lock.wait(1)
                with app.lock:
                    workspace_lock_acquired.set()

        def run_fit() -> None:
            try:
                app.fit({
                    "points": saved["points"],
                    "points_revision": saved["points_revision"],
                    "selected_point_ids": ["zero", "ten"],
                })
            except Exception as exc:  # surfaced by the assertion below
                fit_errors.append(exc)
            finally:
                fit_finished.set()

        app.measurement.is_busy = observed_is_busy  # type: ignore[method-assign]
        terminal_thread = threading.Thread(
            target=terminal_callback_order, daemon=True
        )
        fit_thread = threading.Thread(target=run_fit, daemon=True)
        terminal_thread.start()
        assert measurement_locked.wait(1)
        fit_thread.start()
        assert measurement_check_started.wait(1)
        try_workspace_lock.set()

        assert workspace_lock_acquired.wait(1), "fit 与测量收尾发生锁顺序反转"
        assert fit_finished.wait(1)
        terminal_thread.join(timeout=1)
        fit_thread.join(timeout=1)
        assert fit_errors == []


def test_measurement_start_and_fit_share_one_operation_boundary() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        app = AppState()
        app.save_dir = Path(tmp) / "fit-start-boundary"
        app._load_workspace()
        app.fit({
            "points": [_point("zero", 0, 10), _point("ten", 10, 30)],
            "selected_point_ids": ["zero", "ten"],
        })
        saved = app.points_payload()
        tampered = [dict(point) for point in saved["points"]]
        tampered[1]["current_nA"] = 40
        start_has_lock = threading.Event()
        allow_start = threading.Event()
        fit_finished = threading.Event()
        fit_results: list[dict[str, object]] = []
        fit_errors: list[Exception] = []

        def start_measurement() -> None:
            with app.operation_lock:
                start_has_lock.set()
                assert allow_start.wait(1)
                with app.measurement.lock:
                    app.measurement.state = "running"

        def run_fit() -> None:
            try:
                fit_results.append(app.fit({
                    "points": tampered,
                    "points_revision": saved["points_revision"],
                    "selected_point_ids": ["zero", "ten"],
                }))
            except Exception as exc:  # surfaced by the assertion below
                fit_errors.append(exc)
            finally:
                fit_finished.set()

        start_thread = threading.Thread(target=start_measurement, daemon=True)
        fit_thread = threading.Thread(target=run_fit, daemon=True)
        start_thread.start()
        assert start_has_lock.wait(1)
        fit_thread.start()
        assert not fit_finished.wait(0.05)
        allow_start.set()

        start_thread.join(timeout=1)
        fit_thread.join(timeout=1)
        assert fit_finished.is_set()
        assert fit_errors == []
        assert fit_results[0]["model"]["coefficients"] == pytest.approx([2, 10])
        assert [record["current_nA"] for record in app.point_records] == [10, 30]


def test_fit_rolls_back_all_workspace_files_when_commit_fails() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        app = AppState()
        app.save_dir = Path(tmp) / "fit-rollback"
        app._load_workspace()
        app.fit({
            "points": [_point("zero", 0, 10), _point("ten", 10, 30)],
            "selected_point_ids": ["zero", "ten"],
        })
        paths = app._workspace_paths()
        tracked_paths = [
            paths[key] for key in (
                "model", "settings", "plateau", "filter", "selection", "points"
            )
        ]
        before = {
            path: path.read_bytes() if path.exists() else None
            for path in tracked_paths
        }
        records_before = [dict(record) for record in app.point_records]
        coefficients_before = app.model.coefficients if app.model is not None else None
        payload = app.points_payload()
        changed_points = [dict(point) for point in payload["points"]]
        changed_points[1]["current_nA"] = 40
        real_replace = os.replace

        def fail_filter_commit(source: object, destination: object) -> None:
            source_path = Path(source)
            destination_path = Path(destination)
            if (
                ".stage-" in source_path.name
                and destination_path == paths["filter"]
            ):
                raise OSError("injected filter commit failure")
            real_replace(source, destination)

        with patch(
            "pa_host.gui_server.os.replace", side_effect=fail_filter_commit
        ):
            with pytest.raises(OSError, match="injected filter commit failure"):
                app.fit({
                    "points": changed_points,
                    "points_revision": payload["points_revision"],
                    "selected_point_ids": ["zero", "ten"],
                })

        after = {
            path: path.read_bytes() if path.exists() else None
            for path in tracked_paths
        }
        assert after == before
        assert app.point_records == records_before
        assert (
            app.model.coefficients if app.model is not None else None
        ) == coefficients_before
        assert list(app.save_dir.glob(".*.stage-*")) == []
        assert list(app.save_dir.glob(".*.backup-*")) == []


def test_completion_adds_only_finite_completed_calibration_summaries() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        app = AppState()
        app.save_dir = Path(tmp) / "completion-summary-gate"
        app._load_workspace()

        def complete(run_id: str, state: str, steady: object) -> None:
            app._measurement_completed({
                "run_id": run_id,
                "state": state,
                "finished_at": 10,
                "metadata": {
                    "sample_name": run_id,
                    "sample_role": "calibration",
                    "known_concentration_um": 5,
                },
                "summary": {"steady_current_nA": steady},
                "settings": app.settings.snapshot()["settings"],
                "raw_path": str(app.save_dir / f"missing-{run_id}-raw.csv"),
                "resampled_path": str(app.save_dir / f"missing-{run_id}.csv"),
                "filtered_path": str(
                    app.save_dir / f"missing-{run_id}-filtered.csv"
                ),
            })

        with patch("pa_host.gui_server._send_system_notification"):
            complete("still-running", "running", 20)
            complete("missing-summary", "completed", None)
            complete("nan-summary", "completed", float("nan"))
            complete("valid-summary", "completed", 20)

        assert [record["point_id"] for record in app.point_records] == [
            "valid-summary"
        ]
        assert app.point_records[0]["current_nA"] == 20


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


def test_adaptive_plateau_signature_persists_and_gates_the_calibration_model() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        workspace = Path(tmp) / "adaptive-signature"
        original = PlateauConfig()
        changed = PlateauConfig(absolute_tolerance_nA=0.2)
        app = _fit_adaptive_workspace(workspace, original)

        signature_path = workspace / "calibration-plateau.json"
        assert json.loads(signature_path.read_text())["settings"] == original.to_dict()
        assert app.workflow_snapshot()["settings_match"] is True
        assert app.workflow_snapshot()["calibration_ready"] is True

        app.plateau.settings = changed
        assert app.workflow_snapshot()["settings_match"] is False
        assert app.workflow_snapshot()["calibration_ready"] is False
        assert app.model_payload()["model_compatible"] is False
        with pytest.raises(RuntimeError, match="标定点"):
            app.start_measurement({
                "sample_name": "new-calibration",
                "sample_role": "calibration",
                "known_concentration_um": 20,
            })
        with pytest.raises(RuntimeError, match="生成当前 IT 条件"):
            app.start_measurement({"sample_name": "test", "sample_role": "test"})
        with pytest.raises(ValueError, match="候选标定点不一致"):
            app.fit({"points": app.points_payload()["points"]})
        with pytest.raises(ValueError, match="标定模型不一致"):
            app.predict({"current_nA": 2})

        reloaded = AppState()
        reloaded.settings.settings = SettingsController.validate({"adaptive_stop": True})
        reloaded.plateau.settings = original
        reloaded.save_dir = workspace
        reloaded._load_workspace()
        assert reloaded.calibration_plateau == original.to_dict()
        assert reloaded.model_plateau == original.to_dict()
        assert reloaded.workflow_snapshot()["calibration_ready"] is True

        reloaded.save_dir = Path(tmp) / "empty-workspace"
        reloaded._load_workspace()
        assert reloaded.calibration_plateau is None
        assert reloaded.model_plateau is None
        reloaded.save_dir = workspace
        reloaded._load_workspace()
        reloaded.reset_calibration()
        assert reloaded.calibration_plateau is None
        assert reloaded.model_plateau is None
        assert reloaded.save_dir.parent.resolve() == workspace.resolve()
        assert reloaded.save_dir.resolve() != workspace.resolve()
        assert signature_path.exists()
        assert (reloaded.save_dir / ".sensus-workspace.json").exists()


def test_legacy_adaptive_model_without_plateau_signature_requires_recalibration() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        workspace = Path(tmp) / "legacy-adaptive"
        plateau = PlateauConfig()
        app = _fit_adaptive_workspace(workspace, plateau)
        (workspace / "calibration-plateau.json").unlink()

        reloaded = AppState()
        reloaded.settings.settings = SettingsController.validate({"adaptive_stop": True})
        reloaded.plateau.settings = plateau
        reloaded.save_dir = workspace
        reloaded._load_workspace()

        assert reloaded.model is not None
        assert reloaded.calibration_plateau is None
        assert reloaded.workflow_snapshot()["settings_match"] is False
        assert reloaded.workflow_snapshot()["calibration_ready"] is False
        with pytest.raises(ValueError, match="候选标定点不一致"):
            reloaded.fit({"points": reloaded.points_payload()["points"]})


def test_nonadaptive_model_ignores_plateau_config_and_needs_no_signature() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        workspace = Path(tmp) / "fixed-duration"
        app = AppState()
        app.save_dir = workspace
        app._load_workspace()
        app.settings.settings = SettingsController.validate({"adaptive_stop": False})
        app.fit({"points": [_point("zero", 0, 1), _point("ten", 10, 3)]})

        assert not (workspace / "calibration-plateau.json").exists()
        app.plateau.settings = PlateauConfig(absolute_tolerance_nA=0.2)
        assert app.workflow_snapshot()["settings_match"] is True
        assert app.workflow_snapshot()["calibration_ready"] is True
        assert app.model_payload()["model_compatible"] is True

        reloaded = AppState()
        reloaded.settings.settings = SettingsController.validate({"adaptive_stop": False})
        reloaded.plateau.settings = PlateauConfig(scatter_multiplier=4)
        reloaded.save_dir = workspace
        reloaded._load_workspace()
        assert reloaded.workflow_snapshot()["calibration_ready"] is True


def test_calibration_allows_mixed_analysis_filters() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        app = AppState()
        app.save_dir = Path(tmp) / "filter-workspace"
        app._load_workspace()
        app.filter.settings = {
            "mode": "analysis", "lowpass_enabled": True,
            "lowpass_cutoff_hz": 0.8, "lowpass_auto": False,
            "lowpass_order": 2,
        }
        app.fit({
            "points": [_point("a", 0, 1), _point("b", 1, 3)],
            "selected_point_ids": ["a", "b"],
        })
        assert app.workflow_snapshot()["calibration_ready"] is True
        app.filter.settings["lowpass_cutoff_hz"] = 0.6
        assert app.workflow_snapshot()["calibration_ready"] is True
        assert app.model_payload()["model_compatible"] is True
        refit = app.fit({
            "points": [_point("a", 0, 1), _point("b", 1, 3)],
            "selected_point_ids": ["a", "b"],
        })
        assert refit["model"]["n_points"] == 2

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


def test_analysis_protocol_ignores_snapshot_only_metadata() -> None:
    settings = SettingsController.validate({"adaptive_stop": True})
    snapshot_settings = {
        **settings,
        "native_rate_note": "MAX30131 原生采样说明",
    }

    assert SettingsController.same_analysis_protocol(settings, snapshot_settings)
    assert not SettingsController.same_analysis_protocol(
        settings, {**snapshot_settings, "potential_v": settings["potential_v"] - 0.1}
    )


def test_it_working_electrode_common_mode_is_configurable() -> None:
    legacy = SettingsController.validate({
        "initial_potential_v": 0.2,
        "potential_v": 0.2,
    })
    configured = SettingsController.validate({
        "initial_potential_v": -0.2,
        "potential_v": -0.2,
        "working_electrode_v": 0.25,
    })

    assert SettingsController.working_electrode_mv(legacy) == 1200
    assert configured["working_electrode_v"] == 0.25
    assert SettingsController.working_electrode_mv(configured) == 250


def test_toolchain_path_falls_back_when_configured_location_is_missing() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        legacy = root / "legacy"
        (legacy / "zephyr").mkdir(parents=True)
        (legacy / "zephyr/zephyr-env.sh").touch()

        selected = _existing_toolchain_path(
            str(root / "missing"), (str(legacy),), "zephyr/zephyr-env.sh"
        )

        assert selected == legacy


def test_it_rejects_working_electrode_and_re_values_outside_dac_range() -> None:
    for payload in (
        {"working_electrode_v": 0.2},
        {"working_electrode_v": 1.536},
        {"working_electrode_v": 0.25, "potential_v": 0.3},
        {"working_electrode_v": 1.4, "potential_v": -0.2},
    ):
        try:
            SettingsController.validate(payload)
        except ValueError:
            pass
        else:
            raise AssertionError(f"expected invalid DAC combination: {payload}")


def test_apply_writes_configured_working_electrode_to_firmware_header() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        config = root / "measurement_config.h"
        settings_path = root / "gui_settings.json"
        prebuilt_dir = root / "prebuilt"
        prebuilt_dir.mkdir()
        payload = {
            "initial_potential_v": -0.2,
            "potential_v": -0.2,
            "working_electrode_v": 0.25,
            "adaptive_stop": True,
        }
        (prebuilt_dir / "firmware.json").write_text(json.dumps({
            "settings": SettingsController.validate(payload),
        }))
        (prebuilt_dir / "zephyr.hex").write_text("prebuilt")

        with (
            patch("pa_host.gui_server.FIRMWARE_CONFIG", config),
            patch("pa_host.gui_server.SETTINGS_PATH", settings_path),
            patch("pa_host.gui_server.FIRMWARE_PREBUILT_DIR", prebuilt_dir),
            patch("pa_host.gui_server._release_stale_measurement_bridge"),
            patch.object(SettingsController, "_flash_firmware"),
            patch.object(SettingsController, "_firmware_hash", return_value="test-hash"),
        ):
            result = SettingsController().apply(payload)

        header = config.read_text()
        assert "#define GUI_WP_V_WE_MV 250\n" in header
        assert "#define GUI_IT_ADAPTIVE_STOP 1\n" in header
        assert result["settings"]["working_electrode_v"] == 0.25


def test_runtime_configurable_custom_apply_reuses_verified_firmware_without_flash() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        config = root / "measurement_config.h"
        settings_path = root / "gui_settings.json"
        prebuilt_dir = root / "prebuilt"
        prebuilt_dir.mkdir()
        firmware = prebuilt_dir / "zephyr.hex"
        firmware.write_bytes(b"runtime-configurable")
        digest = SettingsController._firmware_hash(firmware)
        (prebuilt_dir / "firmware.json").write_text(json.dumps({
            "settings": SettingsController.validate({}),
            "runtime_configurable": True,
            "runtime_protocol": {"name": "MEAS", "version": 1},
            "sha256": {"zephyr.hex": digest},
        }))
        with (
            patch("pa_host.gui_server.FIRMWARE_CONFIG", config),
            patch("pa_host.gui_server.SETTINGS_PATH", settings_path),
            patch("pa_host.gui_server.FIRMWARE_PREBUILT_DIR", prebuilt_dir),
            patch("pa_host.gui_server.HARDWARE_TRANSPORT", "rtt"),
            patch("pa_host.gui_server._refresh_usb_transport"),
            patch("pa_host.gui_server._release_stale_measurement_bridge"),
            patch("pa_host.gui_server._probe_jlink_runtime_firmware",
                  return_value=("ready", "verified")),
            patch.object(SettingsController, "_run_build") as build,
            patch.object(SettingsController, "_flash_firmware") as flash,
        ):
            controller = SettingsController()
            result = controller.apply({"potential_v": 0.1, "duration_s": 321})

        build.assert_not_called()
        flash.assert_not_called()
        saved = json.loads(settings_path.read_text())
        assert saved["firmware_source"] == "prebuilt"
        assert saved["firmware_sha256"] == digest
        assert result["settings"]["potential_v"] == 0.1
        assert result["applied"] is True
        assert result["firmware_source"] == "prebuilt"
        assert result["firmware_sha256"] == digest
        assert result["firmware_transport"] == "rtt"
        assert result["message"].startswith("通用固件已确认")


def test_usb_runtime_apply_reuses_data_firmware_without_smp_upgrade() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        v51 = root / "v51"
        images = v51 / "images"
        images.mkdir(parents=True)
        image = images / "app.signed.bin"
        image.write_bytes(b"runtime-v51")
        digest = SettingsController._firmware_hash(image)
        (v51 / "firmware.json").write_text(json.dumps({
            "settings": SettingsController.validate({}),
            "runtime_configurable": True,
            "runtime_protocol": {"name": "MEAS", "version": 1},
            "artifacts_sha256": {"app.signed.bin": digest},
        }), encoding="utf-8")
        settings_path = root / "settings.json"

        with (
            patch("pa_host.gui_server.SETTINGS_PATH", settings_path),
            patch("pa_host.gui_server.V51_PREBUILT_IMAGE", image),
            patch("pa_host.gui_server.HARDWARE_TRANSPORT", "serial"),
            patch("pa_host.gui_server.SERIAL_DATA_PORT", "/dev/data"),
            patch("pa_host.gui_server.SERIAL_SMP_PORT", ""),
            patch("pa_host.gui_server._refresh_usb_transport"),
            patch("pa_host.gui_server._probe_serial_runtime_firmware",
                  return_value=("ready", "verified")),
            patch.object(SettingsController, "_upgrade_v51_firmware") as upgrade,
        ):
            result = SettingsController().apply({"potential_v": -0.1})

        upgrade.assert_not_called()
        assert result["applied"] is True
        assert result["firmware_transport"] == "serial"


def test_runtime_apply_updates_once_then_requires_post_flash_handshake() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        prebuilt = root / "prebuilt"
        prebuilt.mkdir()
        firmware = prebuilt / "zephyr.hex"
        firmware.write_bytes(b"runtime")
        digest = SettingsController._firmware_hash(firmware)
        (prebuilt / "firmware.json").write_text(json.dumps({
            "settings": SettingsController.validate({}),
            "rtt_address": "0x20001100",
            "runtime_configurable": True,
            "runtime_protocol": {"name": "MEAS", "version": 1},
            "sha256": {"zephyr.hex": digest},
        }), encoding="utf-8")
        settings_path = root / "settings.json"

        with (
            patch("pa_host.gui_server.SETTINGS_PATH", settings_path),
            patch("pa_host.gui_server.FIRMWARE_PREBUILT_DIR", prebuilt),
            patch("pa_host.gui_server.HARDWARE_TRANSPORT", "rtt"),
            patch("pa_host.gui_server._refresh_usb_transport"),
            patch("pa_host.gui_server._release_stale_measurement_bridge"),
            patch("pa_host.gui_server._probe_jlink_runtime_firmware",
                  side_effect=[("missing", "old firmware"),
                               ("missing", "old firmware"),
                               ("ready", "verified")]),
            patch.object(SettingsController, "_flash_firmware") as flash,
        ):
            result = SettingsController().apply({"potential_v": 0.05})

        flash.assert_called_once_with(firmware)
        assert result["applied"] is True
        assert result["message"].startswith("通用固件已更新并确认")


def test_post_flash_handshake_failure_does_not_overwrite_saved_settings() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        prebuilt = root / "prebuilt"
        prebuilt.mkdir()
        firmware = prebuilt / "zephyr.hex"
        firmware.write_bytes(b"runtime")
        digest = SettingsController._firmware_hash(firmware)
        (prebuilt / "firmware.json").write_text(json.dumps({
            "settings": SettingsController.validate({}),
            "rtt_address": "0x20001100",
            "runtime_configurable": True,
            "runtime_protocol": {"name": "MEAS", "version": 1},
            "sha256": {"zephyr.hex": digest},
        }), encoding="utf-8")
        settings_path = root / "settings.json"
        previous = {"settings": SettingsController.validate({}), "keep": True}
        settings_path.write_text(json.dumps(previous), encoding="utf-8")

        with (
            patch("pa_host.gui_server.SETTINGS_PATH", settings_path),
            patch("pa_host.gui_server.FIRMWARE_PREBUILT_DIR", prebuilt),
            patch("pa_host.gui_server.HARDWARE_TRANSPORT", "rtt"),
            patch("pa_host.gui_server._refresh_usb_transport"),
            patch("pa_host.gui_server._release_stale_measurement_bridge"),
            patch("pa_host.gui_server._probe_jlink_runtime_firmware",
                  side_effect=[("missing", "old"), ("missing", "old"),
                               ("missing", "no reply"), ("missing", "no reply"),
                               ("missing", "no reply")]),
            patch.object(SettingsController, "_flash_firmware") as flash,
            patch("pa_host.gui_server.time.sleep"),
        ):
            controller = SettingsController()
            with pytest.raises(RuntimeError, match="更新后未通过"):
                controller.apply({"potential_v": 0.05})

        flash.assert_called_once_with(firmware)
        assert settings_path.read_text(encoding="utf-8") == json.dumps(previous)
        assert controller.snapshot()["state"] == "error"
        assert controller.snapshot()["applied"] is False


def test_corrupt_runtime_prebuilt_is_rejected_before_flash() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        config = root / "measurement_config.h"
        settings_path = root / "gui_settings.json"
        prebuilt_dir = root / "prebuilt"
        prebuilt_dir.mkdir()
        firmware = prebuilt_dir / "zephyr.hex"
        firmware.write_bytes(b"corrupt")
        (prebuilt_dir / "firmware.json").write_text(json.dumps({
            "settings": SettingsController.validate({}),
            "runtime_configurable": True,
            "runtime_protocol": {"name": "MEAS", "version": 1},
            "sha256": {"zephyr.hex": "0" * 64},
        }))

        with (
            patch("pa_host.gui_server.FIRMWARE_CONFIG", config),
            patch("pa_host.gui_server.SETTINGS_PATH", settings_path),
            patch("pa_host.gui_server.FIRMWARE_PREBUILT_DIR", prebuilt_dir),
            patch("pa_host.gui_server.HARDWARE_TRANSPORT", "rtt"),
            patch("pa_host.gui_server._refresh_usb_transport"),
            patch("pa_host.gui_server._release_stale_measurement_bridge"),
            patch.object(SettingsController, "_run_build") as build,
            patch.object(SettingsController, "_flash_firmware") as flash,
        ):
            controller = SettingsController()
            with pytest.raises(RuntimeError, match="SHA-256 不匹配"):
                controller.apply({"potential_v": 0.1})

        build.assert_not_called()
        flash.assert_not_called()
        snapshot = controller.snapshot()
        assert snapshot["settings"]["potential_v"] == 0.1
        assert snapshot["state"] == "error"
        assert snapshot["applied"] is False


def test_custom_apply_uses_the_new_build_after_a_prebuilt_run() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        config = root / "measurement_config.h"
        settings_path = root / "gui_settings.json"
        prebuilt_dir = root / "prebuilt"
        build_dir = root / "build"
        prebuilt_dir.mkdir()
        build_dir.mkdir()
        default_settings = SettingsController.validate({})
        custom_payload = {"potential_v": 0.1}
        (prebuilt_dir / "firmware.json").write_text(json.dumps({
            "settings": default_settings,
        }))
        (prebuilt_dir / "zephyr.hex").write_text("prebuilt")
        built_hex = build_dir / "zephyr.hex"
        built_hex.write_text("custom")
        settings_path.write_text(json.dumps({
            "settings": default_settings, "firmware_source": "prebuilt",
        }))

        with (
            patch("pa_host.gui_server.FIRMWARE_CONFIG", config),
            patch("pa_host.gui_server.SETTINGS_PATH", settings_path),
            patch("pa_host.gui_server.FIRMWARE_PREBUILT_DIR", prebuilt_dir),
            patch("pa_host.gui_server.FIRMWARE_BUILD_DIR", build_dir),
            patch("pa_host.gui_server._IS_WIN", True),
            patch("pa_host.gui_server._release_stale_measurement_bridge"),
            patch.object(SettingsController, "_run_build"),
            patch.object(SettingsController, "_flash_firmware") as flash,
            patch.object(SettingsController, "_firmware_hash", return_value="build-hash"),
        ):
            result = SettingsController().apply(custom_payload)

        flash.assert_called_once_with(built_hex)
        saved = json.loads(settings_path.read_text())
        assert saved["firmware_source"] == "build"
        assert saved["firmware_sha256"] == "build-hash"
        assert result["applied"] is True
        assert result["firmware_source"] == "build"
        assert result["firmware_sha256"] == "build-hash"


def test_failed_flash_revokes_the_previous_applied_state() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        config = root / "measurement_config.h"
        settings_path = root / "gui_settings.json"
        prebuilt_dir = root / "prebuilt"
        prebuilt_dir.mkdir()
        payload = SettingsController.validate({})
        (prebuilt_dir / "firmware.json").write_text(json.dumps({"settings": payload}))
        (prebuilt_dir / "zephyr.hex").write_text("prebuilt")
        old_settings = {"settings": payload, "firmware_source": "prebuilt"}
        settings_path.write_text(json.dumps(old_settings))

        with (
            patch("pa_host.gui_server.FIRMWARE_CONFIG", config),
            patch("pa_host.gui_server.SETTINGS_PATH", settings_path),
            patch("pa_host.gui_server.FIRMWARE_PREBUILT_DIR", prebuilt_dir),
            patch("pa_host.gui_server._release_stale_measurement_bridge"),
            patch.object(SettingsController, "_flash_firmware",
                         side_effect=RuntimeError("verify failed")),
        ):
            controller = SettingsController()
            controller.applied = True
            with pytest.raises(RuntimeError, match="verify failed"):
                controller.apply(payload)

        assert controller.applied is False
        assert controller.state == "error"
        assert json.loads(settings_path.read_text()) == old_settings


def test_build_timeout_terminates_the_complete_process_group() -> None:
    process = Mock(pid=4321, returncode=None)
    process.communicate.side_effect = [
        subprocess.TimeoutExpired(["build"], 0.01),
        ("partial output", "partial error"),
    ]

    with (
        patch("pa_host.gui_server._IS_WIN", False),
        patch("pa_host.gui_server.subprocess.Popen", return_value=process),
        patch("pa_host.gui_server.os.killpg", create=True) as kill_group,
        pytest.raises(subprocess.TimeoutExpired),
    ):
        SettingsController._run_build(["build"], timeout_s=0.01)

    kill_group.assert_called_once_with(4321, signal.SIGTERM)


def test_windows_build_timeout_is_bounded_when_taskkill_fails() -> None:
    process = Mock(pid=4321, returncode=None)
    process.communicate.side_effect = [
        subprocess.TimeoutExpired(["build"], 0.01, output="started"),
        subprocess.TimeoutExpired(["build"], 5, output="still running"),
        subprocess.TimeoutExpired(
            ["build"], 5, output="forced", stderr="pipe still open",
        ),
    ]
    process.wait.side_effect = [
        subprocess.TimeoutExpired(["build"], 2),
        1,
    ]
    taskkill_failure = subprocess.CompletedProcess(
        ["taskkill"], returncode=1, stderr="Access is denied",
    )

    with (
        patch("pa_host.gui_server._IS_WIN", True),
        patch(
            "pa_host.gui_server.subprocess.CREATE_NEW_PROCESS_GROUP", 0x200,
            create=True,
        ),
        patch("pa_host.gui_server.subprocess.Popen", return_value=process),
        patch(
            "pa_host.gui_server.subprocess.run", return_value=taskkill_failure,
        ) as taskkill,
        pytest.raises(subprocess.TimeoutExpired) as exc_info,
    ):
        SettingsController._run_build(["build"], timeout_s=0.01)

    assert taskkill.call_count == 2
    assert process.kill.call_count == 3
    assert [call.kwargs["timeout"] for call in process.communicate.call_args_list] == [
        0.01, 5, 5,
    ]
    assert [call.kwargs["timeout"] for call in process.wait.call_args_list] == [2, 2]
    process.stdout.close.assert_called_once_with()
    process.stderr.close.assert_called_once_with()
    assert exc_info.value.output == "forced"
    assert exc_info.value.stderr == "pipe still open"


def test_default_it_method_matches_archived_180_second_protocol() -> None:
    settings = SettingsController.validate({})

    assert settings["method"] == "it"
    assert settings["potential_v"] == 0.2
    assert settings["working_electrode_v"] == 1.2
    assert settings["duration_s"] == 180.0
    assert settings["adaptive_stop"] is False
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


def test_live_data_reader_skips_non_finite_hardware_rows() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        raw = Path(tmp) / "live.csv"
        raw.write_text(
            "dev_ms,fa_fw,sat,ovf\n"
            "nan,1000000,0,0\n"
            "1000,nan,0,0\n"
            "1100,2000000,0,0\n",
            encoding="utf-8",
        )
        controller = MeasurementController()
        controller.raw_path = raw

        data = controller._data()

        assert data["time_s"] == [0.0]
        assert data["current_nA"] == [2.0]


def test_drift_bias_shifts_curve_and_prediction_only_when_enabled() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        app = AppState()
        app.save_dir = Path(tmp) / "drift"
        app._load_workspace()
        app.fit({
            "points": [_point("zero", 0, 10), _point("ten", 10, 30)],
            "selected_point_ids": ["zero", "ten"],
        })
        created_at = float(app.model_created_at or 0)
        settings_json = json.dumps(app.measurement.snapshot()["settings"])
        app.records = [
            {
                "finished_at": str(created_at + 1000), "run_id": "s1", "sample_name": "transition 1",
                "sample_role": "stabilization", "known_concentration_um": "5",
                "steady_current_nA": "20", "state": "completed",
                "measurement_settings_json": settings_json,
            },
            {
                "finished_at": str(created_at + 4600), "run_id": "s2", "sample_name": "transition 2",
                "sample_role": "stabilization", "known_concentration_um": "5",
                "steady_current_nA": "24", "state": "completed",
                "measurement_settings_json": settings_json,
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


def test_drift_rejects_mixed_concentrations_and_old_protocol_records() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        app = AppState()
        app.save_dir = Path(tmp) / "drift-guard"
        app._load_workspace()
        app.fit({
            "points": [_point("zero", 0, 10), _point("ten", 10, 30)],
            "selected_point_ids": ["zero", "ten"],
        })
        created_at = float(app.model_created_at or 0)
        settings_json = json.dumps(app.model_settings)
        app.records = [
            {"finished_at": str(created_at + 1), "run_id": "s1",
             "sample_name": "zero", "sample_role": "stabilization",
             "known_concentration_um": "0", "steady_current_nA": "10",
             "state": "completed", "measurement_settings_json": settings_json},
            {"finished_at": str(created_at + 2), "run_id": "s2",
             "sample_name": "ten", "sample_role": "stabilization",
             "known_concentration_um": "10", "steady_current_nA": "30",
             "state": "completed", "measurement_settings_json": settings_json},
            {"finished_at": str(created_at - 1), "run_id": "old",
             "sample_name": "old", "sample_role": "stabilization",
             "known_concentration_um": "0", "steady_current_nA": "9",
             "state": "completed", "measurement_settings_json": settings_json},
        ]

        assert [record["run_id"] for record in app._stabilization_records()] == ["s1", "s2"]
        with pytest.raises(ValueError, match="填写浓度一致"):
            app.calculate_drift({
                "known_concentration_um": 0,
                "start_run_id": "s1", "end_run_id": "s2",
            })


def test_completed_known_concentration_tests_are_charted_without_refitting() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        app = AppState()
        app.save_dir = Path(tmp) / "validation"
        app._load_workspace()
        app.fit({
            "points": [_point("zero", 0, 10), _point("ten", 10, 30)],
            "selected_point_ids": ["zero", "ten"],
        })
        model_created_at = float(app.model_created_at or 0)
        app.records = [
            {
                "finished_at": str(model_created_at + 10), "run_id": "test-1", "sample_name": "验证样品",
                "sample_role": "test", "known_concentration_um": "5",
                "steady_current_nA": "22", "state": "completed",
            },
            {
                "finished_at": str(model_created_at + 20), "run_id": "test-unknown", "sample_name": "未知样品",
                "sample_role": "test", "known_concentration_um": "",
                "steady_current_nA": "20", "state": "completed",
            },
            {
                "finished_at": str(model_created_at + 30), "run_id": "test-running", "sample_name": "未完成",
                "sample_role": "test", "known_concentration_um": "5",
                "steady_current_nA": "22", "state": "running",
            },
            {
                "finished_at": str(model_created_at + 40), "run_id": "cal-1", "sample_name": "标定样品",
                "sample_role": "calibration", "known_concentration_um": "5",
                "steady_current_nA": "20", "state": "completed",
            },
            {
                "finished_at": str(model_created_at - 10), "run_id": "test-before-model",
                "sample_name": "上一轮测试", "sample_role": "test",
                "known_concentration_um": "5", "steady_current_nA": "22",
                "state": "completed",
            },
        ]

        payload = app.model_payload()
        assert len(payload["points"]) == 2
        assert [point["run_id"] for point in payload["validation_points"]] == [
            "test-1", "test-unknown",
        ]
        point = payload["validation_points"][0]
        assert point["concentration_um"] == 5
        assert point["current_nA"] == 22
        assert point["expected_current_nA"] == pytest.approx(20)
        assert point["predicted_concentration_um"] == pytest.approx(6)
        assert point["error_nA"] == pytest.approx(2)
        assert point["error_um"] == pytest.approx(1)
        unknown = payload["validation_points"][1]
        assert unknown["concentration_um"] is None
        assert unknown["current_nA"] == 20
        assert unknown["predicted_concentration_um"] == pytest.approx(5)
        assert unknown["expected_current_nA"] is None
        assert unknown["error_nA"] is None
        assert unknown["error_um"] is None
        assert unknown["zone"] is None
        assert payload["ap_score"]["stats"]["measured_count"] == 1

        app.drift = {**app._empty_drift(), "enabled": True, "bias_nA": 4}
        biased = app.model_payload()["validation_points"][0]
        assert biased["expected_current_nA"] == pytest.approx(24)
        assert biased["predicted_concentration_um"] == pytest.approx(4)
        assert biased["error_nA"] == pytest.approx(-2)
        assert biased["error_um"] == pytest.approx(-1)


def test_unknown_validation_concentration_can_be_saved_then_filled() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        workspace = Path(tmp) / "unknown-validation"
        app = AppState()
        app.save_dir = workspace
        app._load_workspace()
        app.fit({
            "points": [_point("zero", 0, 10), _point("ten", 10, 30)],
            "selected_point_ids": ["zero", "ten"],
        })
        app._append_record({
            "finished_at": float(app.model_created_at or 0) + 10,
            "run_id": "test-unknown", "sample_name": "待补浓度",
            "sample_role": "test", "known_concentration_um": None,
            "steady_current_nA": 20, "predicted_concentration_um": 5,
            "state": "completed", "data_path": "test.csv", "raw_path": "test-raw.csv",
        })

        unfilled = app.update_validation_points({"points": [{
            "point_id": "test-unknown", "sample_name": "待补浓度",
            "concentration_um": None, "current_nA": 20,
        }]})
        assert unfilled["validation_points"][0]["concentration_um"] is None
        assert unfilled["ap_score"]["stats"]["measured_count"] == 0

        reloaded = AppState()
        reloaded.save_dir = workspace
        reloaded._load_workspace()
        restored = reloaded.model_payload()["validation_points"][0]
        assert restored["concentration_um"] is None
        assert restored["predicted_concentration_um"] == pytest.approx(5)

        filled = reloaded.update_validation_points({"points": [{
            "point_id": "test-unknown", "sample_name": "已补浓度",
            "concentration_um": 5, "current_nA": 20,
        }]})
        point = filled["validation_points"][0]
        assert point["concentration_um"] == 5
        assert point["predicted_concentration_um"] == pytest.approx(5)
        assert point["zone"] == "blue"
        assert point["sample_score"] == 1
        assert filled["ap_score"]["stats"]["measured_count"] == 1
        assert filled["ap_score"]["stats"]["blue_count"] == 1


def test_validation_edits_persist_without_rewriting_the_measurement_index() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        workspace = Path(tmp) / "validation-edits"
        app = AppState()
        app.save_dir = workspace
        app._load_workspace()
        app.fit({
            "points": [_point("zero", 0, 10), _point("ten", 10, 30)],
            "selected_point_ids": ["zero", "ten"],
        })
        app._append_record({
            "finished_at": float(app.model_created_at or 0) + 10,
            "run_id": "test-edit", "sample_name": "原始样品",
            "sample_role": "test", "known_concentration_um": 5,
            "steady_current_nA": 22, "predicted_concentration_um": 6,
            "state": "completed", "data_path": "test.csv", "raw_path": "test-raw.csv",
        })
        index_path = workspace / "measurement-index.csv"
        original_index = index_path.read_text(encoding="utf-8")

        payload = app.update_validation_points({"points": [{
            "point_id": "test-edit", "sample_name": "修改后样品",
            "concentration_um": 6, "current_nA": 24,
        }]})

        point = payload["validation_points"][0]
        assert point["sample_name"] == "修改后样品"
        assert point["concentration_um"] == 6
        assert point["current_nA"] == 24
        assert point["edited"] is True
        assert app.records[0]["known_concentration_um"] == "5"
        assert app.records[0]["steady_current_nA"] == "22"
        assert index_path.read_text(encoding="utf-8") == original_index

        reloaded = AppState()
        reloaded.save_dir = workspace
        reloaded._load_workspace()
        restored = reloaded.model_payload()["validation_points"][0]
        assert restored["sample_name"] == "修改后样品"
        assert restored["concentration_um"] == 6
        assert restored["current_nA"] == 24


def test_calibration_and_test_points_can_be_copied_between_lists_and_persisted() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        workspace = Path(tmp) / "cross-add"
        app = AppState()
        app.save_dir = workspace
        app._load_workspace()
        app.fit({
            "points": [_point("zero", 0, 10), _point("ten", 10, 30)],
            "selected_point_ids": ["zero", "ten"],
        })
        app._append_record({
            "finished_at": float(app.model_created_at or 0) + 10,
            "run_id": "test-copy", "sample_name": "测试样品",
            "sample_role": "test", "known_concentration_um": 5,
            "steady_current_nA": 20, "state": "completed",
            "data_path": "test.csv", "raw_path": "test-raw.csv",
        })

        promoted = app.add_validation_to_calibration({"point_id": "test-copy"})
        assert len(promoted["points"]) == 3
        assert promoted["points"][-1]["run_id"] == "test-copy"

        copied = app.add_calibration_to_validation({"point_id": "zero"})
        assert {point["point_id"] for point in copied["validation_points"]} == {
            "test-copy", "manual-test-zero",
        }

        reloaded = AppState()
        reloaded.save_dir = workspace
        reloaded._load_workspace()
        assert any(point.get("source_point_id") == "zero"
                   for point in reloaded.manual_validation_points)


def test_validation_points_can_be_deleted_without_rewriting_raw_records() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        workspace = Path(tmp) / "delete-validation"
        app = AppState()
        app.save_dir = workspace
        app._load_workspace()
        app.fit({
            "points": [_point("zero", 0, 10), _point("ten", 10, 30)],
            "selected_point_ids": ["zero", "ten"],
        })
        app._append_record({
            "finished_at": float(app.model_created_at or 0) + 10,
            "run_id": "test-delete", "sample_name": "保留原始数据",
            "sample_role": "test", "known_concentration_um": 5,
            "steady_current_nA": 20, "state": "completed",
            "data_path": "test.csv", "raw_path": "test-raw.csv",
        })
        app.add_calibration_to_validation({"point_id": "zero"})
        index_path = workspace / "measurement-index.csv"
        original_index = index_path.read_text(encoding="utf-8")

        result = app.delete_validation_point({"point_id": "test-delete"})
        assert {point["point_id"] for point in result["validation_points"]} == {
            "manual-test-zero"
        }
        assert index_path.read_text(encoding="utf-8") == original_index
        assert app.records[0]["data_path"] == "test.csv"

        result = app.delete_validation_point({"point_id": "manual-test-zero"})
        assert result["validation_points"] == []

        reloaded = AppState()
        reloaded.save_dir = workspace
        reloaded._load_workspace()
        assert reloaded.model_payload()["validation_points"] == []
        saved = json.loads(
            (workspace / "calibration-validation.json").read_text(encoding="utf-8")
        )
        assert saved["deleted_point_ids"] == ["test-delete"]
        assert saved["manual_points"] == []


def test_ap_scoring_keeps_the_complete_index_beyond_one_hundred_rows() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        workspace = Path(tmp) / "complete-index"
        app = AppState()
        app.save_dir = workspace
        app._load_workspace()
        app.fit({
            "points": [_point("zero", 0, 10), _point("ten", 10, 30)],
            "selected_point_ids": ["zero", "ten"],
        })
        created_at = float(app.model_created_at or 0)
        settings = app.settings.snapshot()["settings"]
        for index in range(101):
            is_test = index < 25
            app._append_record({
                "finished_at": created_at + index + 1,
                "run_id": f"run-{index:03d}",
                "sample_name": f"sample-{index:03d}",
                "sample_role": "test" if is_test else "stabilization",
                "known_concentration_um": 5,
                "steady_current_nA": 100 if index == 0 else 20,
                "state": "completed",
                "measurement_settings": settings,
            })

        reloaded = AppState()
        reloaded.save_dir = workspace
        reloaded._load_workspace()
        score = reloaded.model_payload()["ap_score"]

        assert len(reloaded.records) == 101
        assert score["stats"]["grey_count"] == 1
        assert score["stats"]["blue_count"] == 24
        assert score["final_score"] < 200


def test_appending_to_a_legacy_measurement_index_migrates_its_header() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        workspace = Path(tmp) / "legacy-index"
        workspace.mkdir()
        index_path = workspace / "measurement-index.csv"
        legacy_fields = [
            "finished_at", "run_id", "sample_name", "sample_role",
            "known_concentration_um", "steady_current_nA",
            "predicted_concentration_um", "state", "data_path", "raw_path",
        ]
        recovered_settings = SettingsController.validate({})
        with index_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=legacy_fields)
            writer.writeheader()
            writer.writerow({
                "finished_at": "1", "run_id": "legacy", "sample_name": "旧记录",
                "sample_role": "test", "known_concentration_um": "5",
                "steady_current_nA": "20", "predicted_concentration_um": "5",
                "state": "completed", "data_path": "legacy.csv", "raw_path": "",
            })
            csv.writer(handle).writerow([
                "1.5", "partially-migrated", "中断升级记录", "stabilization",
                "5", "20.5", "", "completed", "partial.csv", "",
                json.dumps(recovered_settings),
            ])

        app = AppState()
        app.save_dir = workspace
        app._load_workspace()
        settings = app.settings.snapshot()["settings"]
        app._append_record({
            "finished_at": 2, "run_id": "new", "sample_name": "新记录",
            "sample_role": "stabilization", "known_concentration_um": 5,
            "steady_current_nA": 21, "predicted_concentration_um": None,
            "state": "completed", "data_path": "new.csv", "raw_path": "",
            "measurement_settings": settings,
        })

        with index_path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            rows = list(reader)
        assert reader.fieldnames == [*legacy_fields, "measurement_settings_json"]
        assert len(rows) == 3
        assert all(None not in row for row in rows)
        assert json.loads(rows[1]["measurement_settings_json"])["method"] == "it"
        assert json.loads(rows[2]["measurement_settings_json"])["method"] == "it"
        assert len(app.records) == 3
        assert all(None not in row for row in app.records)
        assert json.loads(app.records[1]["measurement_settings_json"])["method"] == "it"

        reloaded = AppState()
        reloaded.save_dir = workspace
        reloaded._load_workspace()
        assert len(reloaded.records) == 3
        assert "measurement_settings_json" in reloaded.records[-1]


def test_outlying_quadratic_test_still_has_a_chart_point_when_not_invertible() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        app = AppState()
        app.save_dir = Path(tmp) / "quadratic-validation"
        app._load_workspace()
        app.fit({
            "points": [_point("p0", 0, 0), _point("p1", 1, 1), _point("p2", 2, 4)],
            "selected_point_ids": ["p0", "p1", "p2"],
            "degree": 2,
        })
        app.records = [{
            "finished_at": str(float(app.model_created_at or 0) + 1),
            "run_id": "test-outlier", "sample_name": "异常验证",
            "sample_role": "test", "known_concentration_um": "1",
            "steady_current_nA": "10", "state": "completed",
        }]

        point = app.model_payload()["validation_points"][0]
        assert point["current_nA"] == 10
        assert point["expected_current_nA"] == pytest.approx(1)
        assert point["predicted_concentration_um"] is None
        assert point["error_um"] is None
        assert point["error_nA"] == pytest.approx(9)


def test_completed_quadratic_outlier_is_indexed_when_prediction_has_no_inverse() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        app = AppState()
        app.save_dir = root / "quadratic-completion"
        app._load_workspace()
        app.fit({
            "points": [_point("p0", 0, 0), _point("p1", 1, 1), _point("p2", 2, 4)],
            "selected_point_ids": ["p0", "p1", "p2"],
            "degree": 2,
        })
        finished_at = float(app.model_created_at or 0) + 1

        with patch("pa_host.gui_server._send_system_notification"):
            app._measurement_completed({
                "finished_at": finished_at,
                "run_id": "test-outlier-completed",
                "state": "completed",
                "metadata": {
                    "sample_name": "完成异常点", "sample_role": "test",
                    "known_concentration_um": 1,
                },
                "summary": {"steady_current_nA": 10},
                "settings": app.settings.snapshot()["settings"],
                "raw_path": str(root / "missing-raw.csv"),
                "resampled_path": str(root / "missing-data.csv"),
                "filtered_path": str(root / "missing-filtered.csv"),
            })

        result = app.measurement.snapshot()["workflow_result"]
        assert result is not None
        assert "export_error" not in result
        assert result["predicted_concentration_um"] is None
        assert len(app.records) == 1
        assert app.records[0]["run_id"] == "test-outlier-completed"
        assert app.records[0]["predicted_concentration_um"] == "None"
        index_rows = list(csv.DictReader(
            (app.save_dir / "measurement-index.csv").open(newline="")
        ))
        assert len(index_rows) == 1
        assert index_rows[0]["run_id"] == "test-outlier-completed"

        payload = app.model_payload()
        point = payload["validation_points"][0]
        assert point["predicted_concentration_um"] is None
        assert point["zone"] == "grey"
        assert point["sample_score"] == 0
        assert payload["ap_score"]["stats"]["grey_count"] == 1


def test_new_calibration_batch_starts_with_no_previous_validation_points() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        app = AppState()
        app.save_dir = Path(tmp) / "new-batch"
        app._load_workspace()
        points = [_point("zero", 0, 10), _point("ten", 10, 30)]
        with patch("pa_host.gui_server.time.time", side_effect=[1000.0, 2000.0]):
            app.fit({"points": points, "selected_point_ids": ["zero", "ten"]})
            app.records = [{
                "finished_at": "1500", "run_id": "old-test", "sample_name": "上一轮测试",
                "sample_role": "test", "known_concentration_um": "5",
                "steady_current_nA": "22", "state": "completed",
            }]
            assert len(app.model_payload()["validation_points"]) == 1

        previous_dir = app.save_dir
        app.reset_calibration()
        assert app.save_dir.parent.resolve() == previous_dir.resolve()
        assert app.save_dir.resolve() != previous_dir.resolve()
        assert app.model_payload()["validation_points"] == []
        app.fit({"points": points, "selected_point_ids": ["zero", "ten"]})
        assert app.model_payload()["validation_points"] == []


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

        with patch("pa_host.gui_server._send_system_notification") as notify:
            app._measurement_completed({
                "run_id": "it_debug", "state": "completed",
                "finished_at": 1.0, "run_dir": "/tmp/x", "raw_path": "/tmp/x/raw.csv",
                "metadata": {"debug": True, "sample_name": "hw-debug",
                             "sample_role": "test"},
                "summary": {"steady_current_nA": -8.6},
            })
        notify.assert_called_once()

        after = sorted(p.name for p in app.save_dir.glob("*")) \
            if app.save_dir.exists() else []
        assert after == before, f"debug 轮写了文件:{set(after) - set(before)}"
        assert app.point_records == [], "debug 轮不该产生标定点"
        result = app.measurement.snapshot()["workflow_result"]
        assert result is not None and result["debug"] is True
        assert result.get("predicted_concentration_um") is None


def test_system_notification_escapes_applescript_and_uses_argument_vector() -> None:
    assert _escape_applescript('a\\b"c\n') == 'a\\\\b\\"c '
    with (
        patch("pa_host.gui_server.sys.platform", "darwin"),
        patch("pa_host.gui_server.shutil.which", return_value="/usr/bin/osascript"),
        patch("pa_host.gui_server.subprocess.run") as run,
    ):
        _send_system_notification('title "quoted"', 'body\\path\nnext')

    command = run.call_args.args[0]
    assert command[:2] == ["/usr/bin/osascript", "-e"]
    assert 'display notification "body\\\\path next"' in command[2]
    assert 'with title "title \\"quoted\\""' in command[2]


def test_completed_measurement_notification_contains_result_details() -> None:
    with patch("pa_host.gui_server._send_system_notification") as notify:
        _notify_measurement_completion({
            "run_id": "run-1", "state": "completed",
            "metadata": {"sample_name": 'sample "A"'},
        }, {
            "sample_name": 'sample "A"', "state": "completed",
            "steady_current_nA": 12.5,
            "predicted_concentration_um": 3.25,
        })
    notify.assert_called_once()
    title, body = notify.call_args.args
    assert "测试完成" in title
    assert 'sample "A"' in body
    assert "12.5" in body and "3.25" in body


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


def test_stop_is_forwarded_through_the_collectors_command_file() -> None:
    """STOP 必须走 collector 已持有的 RTT 连接，不能另开无效 telnet 连接。"""
    with tempfile.TemporaryDirectory() as tmp:
        ctrl = MeasurementController()
        ctrl.state = "running"
        ctrl.process = Mock()
        ctrl.cmd_path = Path(tmp) / "cmd.txt"
        ctrl._last_reject = {"kind": "CFG_REJECT", "reason": "perturb_during_run"}

        with patch("pa_host.gui_server.threading.Thread") as thread_cls:
            first = ctrl.stop()
            second = ctrl.stop()

        assert ctrl.cmd_path.read_text(encoding="utf-8") == "STOP\n"
        assert first["state"] == "running"
        assert ctrl.user_stop_requested is True
        assert ctrl._last_reject == {}
        assert second["state"] == "running"
        thread_cls.assert_called_once_with(
            target=ctrl._terminate_if_running,
            args=(ctrl.process, 1.5), daemon=True,
        )
        thread_cls.return_value.start.assert_called_once_with()


def test_adaptive_platform_requires_two_new_windows_before_stop() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ctrl = MeasurementController()
        ctrl.settings = SettingsController.validate({"adaptive_stop": True})
        ctrl.state = "running"
        ctrl.process = Mock()
        ctrl.cmd_path = Path(tmp) / "cmd.txt"
        ctrl.metadata = {}
        first_t = np.arange(0.0, 34.9, 0.1)
        second_t = np.arange(0.0, 39.9, 0.1)
        traces = iter([
            {"time_s": first_t.tolist(), "current_nA": np.full(len(first_t), 4.0).tolist(),
             "valid": np.ones(len(first_t), dtype=bool).tolist()},
            {"time_s": second_t.tolist(), "current_nA": np.full(len(second_t), 4.0).tolist(),
             "valid": np.ones(len(second_t), dtype=bool).tolist()},
        ])

        with patch.object(ctrl, "_data", side_effect=lambda: next(traces)), \
                patch("pa_host.gui_server.threading.Thread") as thread_cls:
            ctrl._maybe_auto_stop()
            assert ctrl._plateau_consecutive_passes == 1
            assert not ctrl.cmd_path.exists()
            ctrl._maybe_auto_stop()

        assert ctrl.auto_stop_requested is True
        assert ctrl.user_stop_requested is False
        assert ctrl.cmd_path.read_text(encoding="utf-8") == "STOP\n"
        thread_cls.assert_called_once()


def test_adaptive_platform_backfills_skipped_windows_in_order() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ctrl = MeasurementController()
        ctrl.settings = SettingsController.validate({"adaptive_stop": True})
        ctrl.state = "running"
        ctrl.process = Mock()
        ctrl.cmd_path = Path(tmp) / "cmd.txt"
        ctrl.metadata = {}
        first_t = np.arange(0.0, 30.1, 0.1)
        skipped_t = np.arange(0.0, 45.1, 0.1)
        traces = iter([
            {
                "time_s": first_t.tolist(),
                "current_nA": np.full(len(first_t), 4.0).tolist(),
                "valid": np.ones(len(first_t), dtype=bool).tolist(),
            },
            {
                "time_s": skipped_t.tolist(),
                "current_nA": np.full(len(skipped_t), 4.0).tolist(),
                "valid": np.ones(len(skipped_t), dtype=bool).tolist(),
            },
        ])

        with patch.object(ctrl, "_data", side_effect=lambda: next(traces)), \
                patch("pa_host.gui_server.threading.Thread") as thread_cls:
            ctrl._maybe_auto_stop()
            assert ctrl._plateau_last_segment == 6
            assert ctrl._plateau_consecutive_passes == 1
            ctrl._maybe_auto_stop()

        assert ctrl._plateau_last_segment == 7
        assert ctrl._plateau_consecutive_passes == 2
        assert ctrl.auto_stop_requested is True
        assert ctrl.cmd_path.read_text(encoding="utf-8") == "STOP\n"
        thread_cls.assert_called_once()


def test_half_second_segments_can_accumulate_within_one_watcher_poll() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ctrl = MeasurementController()
        ctrl.settings = SettingsController.validate({"adaptive_stop": True})
        ctrl.plateau_config = PlateauConfig(
            segment_duration_s=0.5,
            segment_count=40,
            required_consecutive_windows=4,
        )
        ctrl.state = "running"
        ctrl.process = Mock()
        ctrl.cmd_path = Path(tmp) / "cmd.txt"
        ctrl.metadata = {}
        time_s = np.arange(0.0, 22.0, 0.1)
        trace = {
            "time_s": time_s.tolist(),
            "current_nA": np.full(len(time_s), 4.0).tolist(),
            "valid": np.ones(len(time_s), dtype=bool).tolist(),
        }

        with patch.object(ctrl, "_data", return_value=trace), \
                patch("pa_host.gui_server.threading.Thread") as thread_cls:
            ctrl._maybe_auto_stop()

        assert ctrl._plateau_last_segment == 43
        assert ctrl._plateau_consecutive_passes == 4
        assert ctrl.auto_stop_requested is True
        assert ctrl.cmd_path.read_text(encoding="utf-8") == "STOP\n"
        thread_cls.assert_called_once()


def test_adaptive_settings_ignore_fixed_duration_and_keep_final_window() -> None:
    settings = SettingsController.validate({
        "adaptive_stop": True,
        "duration_s": 1,
        "fit_window_s": 1,
    })

    assert settings["adaptive_stop"] is True
    assert settings["duration_s"] == 1
    assert settings["fit_window_s"] == 1


def test_display_only_filter_is_excluded_from_platform_analysis() -> None:
    ctrl = MeasurementController()
    ctrl.settings = SettingsController.validate({"adaptive_stop": True})
    ctrl.state = "running"
    ctrl.metadata = {}
    ctrl.filter_config = {
        **ctrl.filter_config,
        "mode": "display", "lowpass_enabled": True,
    }
    time_s = np.arange(0.0, 30.1, 0.1)
    data = {
        "time_s": time_s.tolist(),
        "current_nA": np.ones(len(time_s)).tolist(),
        "valid": np.ones(len(time_s), dtype=bool).tolist(),
    }

    with patch.object(ctrl, "_data", return_value=data), \
            patch("pa_host.gui_server.evaluate_platform", return_value=None) as evaluate:
        ctrl._maybe_auto_stop()

    assert evaluate.call_args.args[3]["mode"] == "off"


@pytest.mark.parametrize("next_mode", ["display", "off"])
def test_display_filter_changes_preserve_formal_state_and_raw_window(
    next_mode: str,
) -> None:
    ctrl = MeasurementController()
    ctrl.state = "running"
    ctrl.filter_config = {
        **ctrl.filter_config,
        "mode": "display", "lowpass_enabled": True,
        "lowpass_auto": False, "lowpass_cutoff_hz": 0.5,
    }
    ctrl._plateau_consecutive_passes = 1
    ctrl._stability_eta = {"status": "ready", "seconds": 12}
    ctrl._last_complete_rolling_metrics = {
        **ctrl._empty_rolling_metrics("ready", "末端窗口指标已就绪"),
        "steady_current_nA": 4.25,
        "noise_nA": 0.12,
        "filter_effective": True,
        "filter_meta": {"mode": "display", "applied": True},
    }

    with patch.object(ctrl, "_reset_plateau_monitor_locked") as reset:
        ctrl.set_filter_config({
            **ctrl.filter_config,
            "mode": next_mode,
            "lowpass_cutoff_hz": 0.8,
        })

    reset.assert_not_called()
    assert ctrl._plateau_consecutive_passes == 1
    assert ctrl._stability_eta == {"status": "ready", "seconds": 12}
    assert ctrl._last_complete_rolling_metrics["steady_current_nA"] == 4.25
    assert ctrl._last_complete_rolling_metrics["noise_nA"] is None
    assert ctrl._last_complete_rolling_metrics["filter_effective"] is False
    assert ctrl._last_complete_rolling_metrics["filter_meta"] == {}


def test_new_measurement_waits_for_previous_watcher_callbacks() -> None:
    ctrl = MeasurementController()
    ctrl.state = "completed"
    ctrl.thread = Mock()
    ctrl.thread.is_alive.return_value = True

    assert ctrl.is_busy() is True
    with pytest.raises(RuntimeError, match="正在保存结果"):
        ctrl.start()


def test_failed_scheduled_run_does_not_consume_the_success_target() -> None:
    app = AppState()
    schedule = app.schedule
    schedule.active = True
    schedule.max_runs = 1
    schedule.attempted_runs = 1

    schedule._completed({
        "run_id": "failed-1", "state": "error", "error": "probe disconnected",
    })

    assert schedule.completed_runs == 0
    assert schedule.failed_runs == 1
    assert schedule.active is False
    assert "失败" in schedule.message


def test_failed_export_does_not_consume_the_schedule_success_target() -> None:
    app = AppState()
    schedule = app.schedule
    schedule.active = True
    schedule.max_runs = 1
    schedule.attempted_runs = 1

    schedule._completed({
        "run_id": "export-failed", "state": "completed",
        "workflow_result": {"export_error": "disk full"},
    })

    assert schedule.completed_runs == 0
    assert schedule.failed_runs == 1
    assert schedule.history[0]["state"] == "error"
    assert "disk full" in schedule.message


def test_stale_schedule_callback_cannot_stop_a_new_generation() -> None:
    app = AppState()
    schedule = app.schedule
    schedule.active = True
    schedule.generation = 2

    schedule._completed({"state": "completed", "run_id": "old"}, generation=1)

    assert schedule.active is True
    assert schedule.completed_runs == 0
    assert schedule.history == []


def test_workflow_write_probe_never_replaces_a_user_file() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        workspace = root / "workspace"
        workspace.mkdir()
        existing = workspace / ".sensus-write-test"
        existing.write_text("user data", encoding="utf-8")
        app = AppState()

        with patch("pa_host.gui_server.WORKFLOW_PATH", root / "workflow.json"):
            app.configure_workflow({"save_dir": str(workspace)})

        assert existing.read_text(encoding="utf-8") == "user data"


def test_startup_without_saved_workspace_stays_blank_and_creates_nothing(
    tmp_path: Path,
) -> None:
    workflow_path = tmp_path / "state" / "workflow.json"
    history_path = tmp_path / "state" / "history.json"
    with (
        patch("pa_host.gui_server.WORKFLOW_PATH", workflow_path),
        patch("pa_host.gui_server.HISTORY_PATH", history_path),
    ):
        app = AppState()

    workflow = app.workflow_snapshot()
    assert app.save_dir is None
    assert app.workspace_root is None
    assert workflow["save_dir"] == ""
    assert workflow["workspace_root"] == ""
    assert workflow["workspace_configured"] is False
    assert workflow["workspace_available"] is False
    assert not workflow_path.exists()
    assert not history_path.exists()


def test_every_measurement_entry_point_requires_a_workspace(tmp_path: Path) -> None:
    with (
        patch("pa_host.gui_server.WORKFLOW_PATH", tmp_path / "workflow.json"),
        patch("pa_host.gui_server.HISTORY_PATH", tmp_path / "history.json"),
    ):
        app = AppState()

    with pytest.raises(RuntimeError, match="请先选择工作区"):
        app.start_measurement({"sample_name": "sample"})
    with pytest.raises(RuntimeError, match="请先选择工作区"):
        app.start_debug_run({"note": "debug"})
    with pytest.raises(RuntimeError, match="请先选择工作区"):
        app.start_schedule({"sample_role": "calibration"})
    with pytest.raises(RuntimeError, match="请先选择工作区"):
        app.reset_calibration({"batch_name": "blocked"})


def test_measurement_payload_cannot_override_the_selected_workspace(
    tmp_path: Path,
) -> None:
    app = AppState()
    selected = tmp_path / "selected"
    unselected = tmp_path / "unselected"
    app.save_dir = selected
    app._load_workspace()
    with patch.object(
        app.measurement, "start_verified", return_value={"state": "running"},
    ) as start:
        app.start_measurement({
            "sample_name": "sample",
            "sample_role": "calibration",
            "known_concentration_um": 1,
            "save_dir": str(unselected),
        })

    metadata = start.call_args.kwargs["metadata"]
    assert metadata["save_dir"] == str(selected.resolve())
    assert Path(metadata["live_raw_path"]).parent == selected.resolve()
    assert not unselected.exists()


def test_selected_workspace_persists_across_restart(tmp_path: Path) -> None:
    root = tmp_path / "measurements"
    root.mkdir()
    workflow_path = tmp_path / "state" / "workflow.json"
    history_path = tmp_path / "state" / "history.json"
    with (
        patch("pa_host.gui_server.WORKFLOW_PATH", workflow_path),
        patch("pa_host.gui_server.HISTORY_PATH", history_path),
    ):
        first = AppState()
        configured = first.configure_workflow({
            "save_dir": str(root),
            "batch_name": "first-batch",
        })
        batch_dir = Path(configured["save_dir"])
        second = AppState()

    assert batch_dir.parent == root.resolve()
    assert second.save_dir == batch_dir
    assert second.workspace_root == root.resolve()
    assert second.workflow_snapshot()["workspace_available"] is True


def test_missing_saved_workspace_is_not_recreated(tmp_path: Path) -> None:
    root = tmp_path / "detached-drive"
    batch = root / "saved-batch"
    workflow_path = tmp_path / "workflow.json"
    workflow_path.write_text(json.dumps({
        "save_dir": str(batch),
        "workspace_root": str(root),
    }), encoding="utf-8")
    with (
        patch("pa_host.gui_server.WORKFLOW_PATH", workflow_path),
        patch("pa_host.gui_server.HISTORY_PATH", tmp_path / "history.json"),
    ):
        app = AppState()

    workflow = app.workflow_snapshot()
    assert app.save_dir == batch.resolve()
    assert workflow["workspace_configured"] is True
    assert workflow["workspace_available"] is False
    assert workflow["workspace_root"] == str(root.resolve())
    assert not root.exists()


@pytest.mark.parametrize("platform, executable", [
    ("darwin", "/usr/bin/osascript"),
    ("win32", "C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"),
])
def test_native_workspace_picker_passes_unicode_path_without_interpolation(
    tmp_path: Path, platform: str, executable: str,
) -> None:
    initial = tmp_path / "个人数据"
    initial.mkdir()
    completed = subprocess.CompletedProcess(
        [executable], 0, stdout=f"{initial}\n", stderr="",
    )
    with (
        patch("pa_host.gui_server.sys.platform", platform),
        patch("pa_host.gui_server.shutil.which", return_value=executable),
        patch("pa_host.gui_server.subprocess.run", return_value=completed) as run,
    ):
        result = _browse_workspace_directory(str(initial))

    assert result == {"selected": True, "path": str(initial.resolve())}
    command = run.call_args.args[0]
    if platform == "darwin":
        assert command[-2:] == ["--", str(initial.resolve())]
        assert "on run argv" in command[2]
        assert "system attribute" not in command[2]
        assert "set initialFolder to missing value" in command[2]
        assert run.call_args.kwargs["env"] is None
    else:
        assert str(initial) not in command[-1]
        assert run.call_args.kwargs["env"]["SENSUS_INITIAL_FOLDER"] == str(
            initial.resolve()
        )
        assert "-STA" in command


def test_native_workspace_picker_cancel_keeps_the_current_path(
    tmp_path: Path,
) -> None:
    completed = subprocess.CompletedProcess(
        ["/usr/bin/osascript"], 0, stdout="", stderr="",
    )
    with (
        patch("pa_host.gui_server.sys.platform", "darwin"),
        patch("pa_host.gui_server.shutil.which", return_value="/usr/bin/osascript"),
        patch("pa_host.gui_server.subprocess.run", return_value=completed),
    ):
        result = _browse_workspace_directory(str(tmp_path))

    assert result == {"selected": False, "path": ""}


def test_jlink_flash_uses_one_backend_and_independent_readback() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        diagnostics = DiagnosticStore(root / "logs")
        firmware = root / "firmware.hex"
        _write_hex(firmware, (0, 0, b"\x00"), (0, 1, b""))
        commander = root / "JLinkExe"
        commander.write_text("commander", encoding="ascii")
        commander.chmod(0o755)
        completed = subprocess.CompletedProcess(
            [str(commander)], 0,
            stdout=("10000100 = 00052833\nDownloading file...O.K.\n"
                    "Script processing completed.\n"),
            stderr="",
        )

        with (
            patch("pa_host.gui_server.JLINK_EXE", commander),
            patch("pa_host.gui_server.JLINK_SERIAL", "29734569"),
            patch("pa_host.gui_server.DIAGNOSTICS", diagnostics),
            patch("pa_host.gui_server.probe_jlink_target",
                  return_value=(True, "10000100 = 00052833")),
            patch("pa_host.gui_server._openocd_target_probe",
                  return_value=(False, "OpenOCD unavailable")),
            patch.object(SettingsController, "_read_jlink_flash_image",
                         side_effect=[b"\xFF", b"\x00"]) as readback,
            patch.object(SettingsController, "_run_jlink_application") as run_app,
            patch("pa_host.gui_server.run_jlink_script",
                  return_value=completed) as commander_run,
            patch("pa_host.gui_server.subprocess.run") as openocd_run,
        ):
            SettingsController._flash_firmware(firmware)

        script = commander_run.call_args.args[0]
        assert "si SWD\nspeed 100\ndevice nRF52833_xxAA\nconnect\n" in script
        assert f"loadfile \"{firmware.resolve()}\", noreset" in script
        assert "0x4001E" not in script
        assert "recover" not in script.lower()
        assert readback.call_count == 2
        run_app.assert_called_once_with()
        openocd_run.assert_not_called()
        assert [event["event"] for event in diagnostics.snapshot()["events"]] == [
            "firmware.flash.started", "firmware.flash.completed",
        ]


def test_jlink_matching_image_skips_programming_but_restarts_target() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        firmware = root / "firmware.hex"
        _write_hex(firmware, (0, 0, b"\xA5"), (0, 1, b""))
        commander = root / "JLinkExe"
        commander.write_text("commander", encoding="ascii")
        commander.chmod(0o755)
        with (
            patch("pa_host.gui_server.JLINK_EXE", commander),
            patch("pa_host.gui_server.probe_jlink_target",
                  return_value=(True, "reachable")),
            patch("pa_host.gui_server._openocd_target_probe",
                  return_value=(False, "OpenOCD unavailable")),
            patch.object(SettingsController, "_read_jlink_flash_image",
                         return_value=b"\xA5"),
            patch.object(SettingsController, "_run_jlink_application") as run_app,
            patch("pa_host.gui_server.run_jlink_script") as program,
        ):
            SettingsController._flash_firmware(firmware)

        program.assert_not_called()
        run_app.assert_called_once_with()


def test_read_only_preflight_can_select_openocd_before_any_write() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        firmware = root / "firmware.hex"
        _write_hex(firmware, (0, 0, b"\x00"), (0, 1, b""))
        commander = root / "JLinkExe"
        commander.touch()
        openocd = root / "openocd"
        openocd.touch()
        scripts = root / "scripts"
        (scripts / "interface").mkdir(parents=True)
        (scripts / "target").mkdir()
        (scripts / "interface/jlink.cfg").touch()
        (scripts / "target/nrf52.cfg").touch()
        with (
            patch("pa_host.gui_server.JLINK_EXE", commander),
            patch("pa_host.gui_server.OPENOCD_EXE", openocd),
            patch("pa_host.gui_server.OPENOCD_SCRIPTS", scripts),
            patch("pa_host.gui_server.probe_jlink_target",
                  return_value=(False, "unsupported Commander")),
            patch("pa_host.gui_server._openocd_target_probe",
                  return_value=(True, "0x10000100: 00052833")),
            patch.object(SettingsController, "_flash_with_jlink") as jlink_write,
            patch.object(SettingsController, "_flash_with_openocd") as openocd_write,
        ):
            SettingsController._flash_firmware(firmware)

        jlink_write.assert_not_called()
        openocd_write.assert_called_once()


def test_unreachable_swd_backends_never_begin_flash() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        firmware = root / "firmware.hex"
        _write_hex(firmware, (0, 0, b"\x00"), (0, 1, b""))
        commander = root / "JLinkExe"
        commander.write_text("commander", encoding="ascii")
        commander.chmod(0o755)
        openocd = root / "openocd"
        openocd.touch()
        scripts = root / "scripts"
        (scripts / "interface").mkdir(parents=True)
        (scripts / "target").mkdir()
        (scripts / "interface/jlink.cfg").touch()
        (scripts / "target/nrf52.cfg").touch()
        with (
            patch("pa_host.gui_server.JLINK_EXE", commander),
            patch("pa_host.gui_server.OPENOCD_EXE", openocd),
            patch("pa_host.gui_server.OPENOCD_SCRIPTS", scripts),
            patch("pa_host.gui_server.probe_jlink_target",
                  return_value=(False, "cannot read IDR")),
            patch("pa_host.gui_server._openocd_target_probe",
                  return_value=(False, "cannot read IDR")) as openocd_probe,
            patch.object(SettingsController, "_flash_with_openocd") as openocd_write,
            pytest.raises(RuntimeError, match="目标无响应"),
        ):
            SettingsController._flash_firmware(firmware)

        openocd_probe.assert_called_once()
        openocd_write.assert_not_called()


def test_jlink_operation_preflight_keeps_tool_output_in_diagnostics(
    tmp_path: Path,
) -> None:
    diagnostics = DiagnosticStore(tmp_path / "logs")
    status = {
        "target_state": "unreachable",
        "target_detail": "J-Link 探针在线，但 nRF52833 未响应",
        "target_diagnostics": "SEGGER: cannot connect; OpenOCD: cannot read IDR",
    }
    with (
        patch("pa_host.gui_server.DIAGNOSTICS", diagnostics),
        patch("pa_host.gui_server._probe_jlink_target_status",
              return_value=status) as probe,
        pytest.raises(RuntimeError, match="nRF52833 未响应") as exc_info,
    ):
        gui_server._require_jlink_target("29734569")

    probe.assert_called_once_with("29734569", force=True)
    event = diagnostics.snapshot()["events"][-1]
    assert event["event"] == "device.jlink.target_unreachable"
    assert event["context"]["tool_output"].endswith("cannot read IDR")
    assert exc_info.value.diagnostic_id == event["event_id"]


def test_jlink_readback_mismatch_is_a_hard_failure() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        firmware = root / "firmware.hex"
        _write_hex(firmware, (0, 0, b"\x12"), (0, 1, b""))
        commander = root / "JLinkExe"
        commander.write_text("commander", encoding="ascii")
        commander.chmod(0o755)
        completed = subprocess.CompletedProcess(
            [str(commander)], 0,
            stdout=("10000100 = 00052833\nDownloading file...O.K.\n"
                    "Script processing completed.\n"), stderr="",
        )
        with (
            patch("pa_host.gui_server.JLINK_EXE", commander),
            patch("pa_host.gui_server.probe_jlink_target",
                  return_value=(True, "reachable")),
            patch.object(SettingsController, "_read_jlink_flash_image",
                         side_effect=[b"\xFF", b"\x34"]),
            patch.object(SettingsController, "_run_jlink_application") as run_app,
            patch("pa_host.gui_server.run_jlink_script", return_value=completed),
            patch.object(SettingsController, "_flash_with_openocd") as openocd_write,
            pytest.raises(RuntimeError, match="独立回读不一致"),
        ):
            SettingsController._flash_firmware(firmware)

        run_app.assert_called_once_with()
        openocd_write.assert_not_called()


def test_runtime_response_requires_one_complete_matching_request() -> None:
    request_id = "ready-current"
    complete = "\n".join((
        f"CFG_APPLIED req={request_id} src=get",
        f"CFG_DERIVED req={request_id}",
        f"CFG_CONFIRMED req={request_id} src=get verify_ok=1 "
        "invalid_cfg=0 vdd_oor=0",
    ))
    assert gui_server._runtime_response_state(complete, request_id) == "ready"
    assert gui_server._runtime_response_state(
        f"CFG_APPLIED req={request_id} src=get", request_id
    ) == "incomplete"
    assert gui_server._runtime_response_state(
        f"CFG_CONFIRMED req={request_id} src=get verify_ok=0",
        request_id,
    ) == "invalid"
    mixed = complete.replace(request_id, "ready-old", 1)
    assert gui_server._runtime_response_state(mixed, request_id) == "incomplete"


def _fake_openocd_layout(tmp_path: Path) -> tuple[Path, Path]:
    openocd = tmp_path / "openocd"
    scripts = tmp_path / "scripts"
    (scripts / "interface").mkdir(parents=True)
    (scripts / "target").mkdir(parents=True)
    (scripts / "interface/jlink.cfg").touch()
    (scripts / "target/nrf52.cfg").touch()
    openocd.touch()
    return openocd, scripts


def test_openocd_target_probe_accepts_explicit_read_memory_identity(
    tmp_path: Path,
) -> None:
    openocd, scripts = _fake_openocd_layout(tmp_path)
    completed = subprocess.CompletedProcess(
        [str(openocd)], 0,
        stdout=(
            "Info : [nrf52.cpu] Cortex-M4 r0p1 processor detected\n"
            "SENSUS_INFO_PART=0x00052833\n"
        ),
        stderr="shutdown command invoked\n",
    )
    with (
        patch("pa_host.gui_server.OPENOCD_EXE", openocd),
        patch("pa_host.gui_server.OPENOCD_SCRIPTS", scripts),
        patch(
            "pa_host.gui_server.runtime.hidden_subprocess_kwargs",
            return_value={"creationflags": 0x08000000},
        ),
        patch("pa_host.gui_server.subprocess.run", return_value=completed) as run,
    ):
        reachable, output = gui_server._openocd_target_probe("29734569")

    assert reachable is True
    assert "SENSUS_INFO_PART=0x00052833" in output
    command = run.call_args.args[0]
    assert any("read_memory 0x10000100 32 1" in arg for arg in command)
    assert not any("mdw " in arg for arg in command)
    assert run.call_args.kwargs["creationflags"] == 0x08000000


def test_openocd_target_probe_rejects_connection_log_without_identity(
    tmp_path: Path,
) -> None:
    openocd, scripts = _fake_openocd_layout(tmp_path)
    completed = subprocess.CompletedProcess(
        [str(openocd)], 0,
        stdout=(
            "Info : [nrf52.cpu] Cortex-M4 r0p1 processor detected\n"
            "Info : [nrf52.cpu] target has 6 breakpoints, 4 watchpoints\n"
        ),
        stderr="shutdown command invoked\n",
    )
    with (
        patch("pa_host.gui_server.OPENOCD_EXE", openocd),
        patch("pa_host.gui_server.OPENOCD_SCRIPTS", scripts),
        patch("pa_host.gui_server.subprocess.run", return_value=completed),
    ):
        reachable, _output = gui_server._openocd_target_probe("29734569")

    assert reachable is False


def test_openocd_rtt_layout_probe_accepts_unpadded_signature_words(
    tmp_path: Path,
) -> None:
    openocd, scripts = _fake_openocd_layout(tmp_path)
    completed = subprocess.CompletedProcess(
        [str(openocd)], 0,
        stdout=(
            "SENSUS_INFO_PART=0x52833\n"
            "SENSUS_RTT_SIGNATURE=0x47474553 0x52205245 0x5454\n"
        ),
        stderr="shutdown command invoked\n",
    )
    with (
        patch("pa_host.gui_server.OPENOCD_EXE", openocd),
        patch("pa_host.gui_server.OPENOCD_SCRIPTS", scripts),
        patch("pa_host.gui_server.subprocess.run", return_value=completed) as run,
    ):
        reachable, layout_ready, output = gui_server._openocd_rtt_layout_probe(
            0x20001100, "29734569",
        )

    assert (reachable, layout_ready) == (True, True)
    assert "SENSUS_RTT_SIGNATURE" in output
    command = run.call_args.args[0]
    assert any("read_memory 0x20001100 32 3" in arg for arg in command)


def test_openocd_runtime_probe_retries_tagged_get_until_verified() -> None:
    process = Mock()
    process.poll.return_value = None
    client = Mock()
    response = "\n".join((
        "CFG_APPLIED req=probe-1 src=get",
        "CFG_DERIVED req=probe-1",
        "CFG_CONFIRMED req=probe-1 src=get verify_ok=1 "
        "invalid_cfg=0 vdd_oor=0",
    )).encode("ascii")
    client.recv.side_effect = [gui_server.socket.timeout, response]
    clock = iter([0.0, 0.0, 0.0, 0.0, 0.0, 1.1, 1.1])
    with (
        patch("pa_host.gui_server._runtime_probe_request_id",
              return_value="probe-1"),
        patch("pa_host.gui_server.start_jlink_rtt", return_value=process),
        patch("pa_host.gui_server.socket.create_connection",
              return_value=client),
        patch("pa_host.gui_server.time.monotonic",
              side_effect=lambda: next(clock, 1.1)),
    ):
        state, detail = gui_server._probe_openocd_runtime_firmware(
            0x20001100, "29734569", timeout_s=5.0,
        )

    assert state == "ready"
    assert "CFG_CONFIRMED" in detail
    assert client.sendall.call_args_list == [
        call(b"GET req=probe-1\n"), call(b"GET req=probe-1\n"),
    ]
    client.close.assert_called_once_with()
    process.terminate.assert_called_once_with()


def test_missing_jlink_rtt_layout_requires_readable_nrf52833() -> None:
    metadata = {"rtt_address": "0x20001000"}
    unavailable = gui_server.RTTControlBlockUnavailable("no control block")
    with (
        patch("pa_host.gui_server.JLinkMemoryRTT", side_effect=unavailable),
        patch("pa_host.gui_server.JLINK_EXE") as commander,
        patch("pa_host.gui_server.probe_jlink_target",
              return_value=(True, "10000100 = 00052833")) as target,
    ):
        commander.is_file.return_value = True
        state, detail = gui_server._probe_jlink_runtime_firmware(metadata)

    assert state == "missing"
    assert "control block" in detail
    target.assert_called_once()


def test_jlink_transport_failure_never_authorizes_firmware_update() -> None:
    metadata = {"rtt_address": "0x20001000"}
    with (
        patch("pa_host.gui_server.JLinkMemoryRTT",
              side_effect=RuntimeError("SWD timeout")),
        patch("pa_host.gui_server.JLINK_EXE") as commander,
        patch("pa_host.gui_server.probe_jlink_target",
              return_value=(True, "10000100 = 00052833")) as target,
    ):
        commander.is_file.return_value = True
        state, detail = gui_server._probe_jlink_runtime_firmware(metadata)

    assert state == "transport_error"
    assert detail == "SWD timeout"
    target.assert_called_once()


def test_runtime_probe_uses_openocd_after_read_only_jlink_preflight() -> None:
    metadata = {"rtt_address": "0x20001000"}
    with (
        patch("pa_host.gui_server.JLINK_EXE") as commander,
        patch("pa_host.gui_server.OPENOCD_EXE") as openocd,
        patch("pa_host.gui_server.OPENOCD_SCRIPTS") as scripts,
        patch("pa_host.gui_server.probe_jlink_target",
              return_value=(False, "unsupported Commander")),
        patch("pa_host.gui_server._openocd_rtt_layout_probe",
              return_value=(True, True, "SEGGER RTT")),
        patch("pa_host.gui_server._probe_openocd_runtime_firmware",
              return_value=("ready", "verified")) as probe,
    ):
        commander.is_file.return_value = True
        openocd.is_file.return_value = True
        scripts.__truediv__.return_value.is_file.return_value = True
        state, detail = gui_server._probe_jlink_runtime_firmware(metadata)

    assert (state, detail) == ("ready", "verified")
    probe.assert_called_once_with(0x20001000, gui_server.JLINK_SERIAL,
                                  timeout_s=7.0)


def test_openocd_missing_rtt_layout_is_recoverable_only_after_chip_identity() -> None:
    metadata = {"rtt_address": "0x20001000"}
    with (
        patch("pa_host.gui_server.JLINK_EXE") as commander,
        patch("pa_host.gui_server._openocd_jlink_available", return_value=True),
        patch("pa_host.gui_server._openocd_rtt_layout_probe",
              return_value=(True, False, "10000100 = 00052833")) as layout,
        patch("pa_host.gui_server._probe_openocd_runtime_firmware") as runtime_probe,
    ):
        commander.is_file.return_value = False
        state, detail = gui_server._probe_jlink_runtime_firmware(metadata)

    assert state == "missing"
    assert "00052833" in detail
    layout.assert_called_once_with(0x20001000, gui_server.JLINK_SERIAL)
    runtime_probe.assert_not_called()


def test_schedule_start_requires_a_fresh_jlink_identity(tmp_path: Path) -> None:
    app = AppState()
    app.save_dir = tmp_path
    app.workspace_root = tmp_path
    app.workspace_available = True
    settings = {"applied": True, "settings": SettingsController.validate({})}
    with (
        patch.object(app.settings, "snapshot", return_value=settings),
        patch("pa_host.gui_server.HARDWARE_TRANSPORT_REQUESTED", "rtt"),
        patch("pa_host.gui_server.HARDWARE_TRANSPORT", "rtt"),
        patch("pa_host.gui_server._require_jlink_target",
              side_effect=RuntimeError("目标板无响应")) as require_target,
        pytest.raises(RuntimeError, match="目标板无响应"),
    ):
        app.start_schedule({"sample_role": "calibration"})

    require_target.assert_called_once()
    assert app.schedule.snapshot()["active"] is False


def test_background_discovery_does_not_probe_while_operation_is_busy(
    monkeypatch,
) -> None:
    class BusyLock:
        def __init__(self) -> None:
            self.released = False

        def acquire(self, *, blocking: bool) -> bool:
            assert blocking is False
            return False

        def release(self) -> None:
            self.released = True

    lock = BusyLock()
    app = Mock(operation_lock=lock)
    cached = [{"id": "jlink:1", "kind": "jlink", "probe_serial": "1"}]
    discover = Mock(return_value=cached)
    full_probe = Mock(side_effect=AssertionError("active hardware was probed"))
    annotate = Mock(side_effect=AssertionError("active target was probed"))
    remember = Mock()
    monkeypatch.setattr(gui_server, "APP", app)
    monkeypatch.setattr(gui_server, "_discover_devices", discover)
    monkeypatch.setattr(gui_server, "_discover_devices_with_probe", full_probe)
    monkeypatch.setattr(gui_server, "_annotate_target_states", annotate)
    monkeypatch.setattr(gui_server, "_remember_device_discovery", remember)
    monkeypatch.setattr(gui_server, "DEVICE_DISCOVERY_LOG_SIGNATURE", None)
    monkeypatch.setattr(gui_server, "DEVICE_DISCOVERY_LOG_ERROR", "")
    monkeypatch.setattr(gui_server, "DEVICE_DISCOVERY_THREAD", Mock())

    gui_server._run_device_discovery()

    discover.assert_called_once_with(probe=False)
    full_probe.assert_not_called()
    annotate.assert_not_called()
    remember.assert_called_once_with(cached, "")
    assert lock.released is False
    assert gui_server.DEVICE_DISCOVERY_THREAD is None


def test_device_discovery_forgets_target_cache_after_probe_unplug(
    monkeypatch,
) -> None:
    cache = {"29734569": {"target_state": "reachable"}}
    monkeypatch.setattr(gui_server, "JLINK_TARGET_CACHE", cache)
    monkeypatch.setattr(gui_server, "DEVICE_DISCOVERY_CACHE", [])
    monkeypatch.setattr(gui_server, "DEVICE_DISCOVERY_AT", 0.0)
    monkeypatch.setattr(gui_server, "DEVICE_DISCOVERY_ERROR", "")

    gui_server._remember_device_discovery([])

    assert cache == {}


def test_jlink_readback_error_survives_failed_restart_cleanup() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        firmware = root / "firmware.hex"
        _write_hex(firmware, (0, 0, b"\x12"), (0, 1, b""))
        commander = root / "JLinkExe"
        commander.touch()
        with (
            patch("pa_host.gui_server.JLINK_EXE", commander),
            patch("pa_host.gui_server.probe_jlink_target",
                  return_value=(True, "reachable")),
            patch.object(SettingsController, "_read_jlink_flash_image",
                         side_effect=RuntimeError("readback failed")),
            patch.object(SettingsController, "_run_jlink_application",
                         side_effect=RuntimeError("restart failed")),
            pytest.raises(RuntimeError, match="readback failed"),
        ):
            SettingsController._flash_firmware(firmware)


def test_openocd_fallback_uses_one_bounded_write_verify_run_session() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        firmware = root / "firmware.hex"
        _write_hex(firmware, (0, 0, b"\x00"), (0, 1, b""))
        openocd = root / "openocd"
        scripts = root / "scripts"
        (scripts / "interface").mkdir(parents=True)
        (scripts / "target").mkdir()
        (scripts / "interface/jlink.cfg").write_text("adapter driver jlink")
        (scripts / "target/nrf52.cfg").write_text("target create")
        openocd.write_text("openocd", encoding="ascii")
        openocd.chmod(0o755)
        output = (
            "SENSUS_INFO_PART=0x00052833\n"
            "wrote 1 bytes from file\nverified 1 bytes in 0.1s\n"
        )
        with (
            patch("pa_host.gui_server.JLINK_EXE", root / "missing-jlink"),
            patch("pa_host.gui_server.OPENOCD_EXE", openocd),
            patch("pa_host.gui_server.OPENOCD_SCRIPTS", scripts),
            patch("pa_host.gui_server._openocd_target_probe",
                  return_value=(True, "SENSUS_INFO_PART=0x00052833")),
            patch.object(SettingsController, "_run_openocd_once",
                         return_value=(0, output)) as run,
        ):
            SettingsController._flash_firmware(firmware)

        assert run.call_count == 1
        commands = run.call_args.args[1:]
        assert any(command.startswith("flash write_image erase {")
                   for command in commands)
        assert any(command.startswith("verify_image {") for command in commands)
        assert commands[-2:] == ("reset run", "shutdown")
        assert not any("erase_sector" in command or "0x4001E" in command
                       for command in commands)


def test_openocd_retry_is_bounded_and_never_switches_backend() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        firmware = root / "firmware.hex"
        _write_hex(firmware, (0, 0, b"\x00"), (0, 1, b""))
        openocd = root / "openocd"
        scripts = root / "scripts"
        (scripts / "interface").mkdir(parents=True)
        (scripts / "target").mkdir()
        (scripts / "interface/jlink.cfg").touch()
        (scripts / "target/nrf52.cfg").touch()
        openocd.touch()
        with (
            patch("pa_host.gui_server.JLINK_EXE", root / "missing-jlink"),
            patch("pa_host.gui_server.OPENOCD_EXE", openocd),
            patch("pa_host.gui_server.OPENOCD_SCRIPTS", scripts),
            patch("pa_host.gui_server._openocd_target_probe",
                  return_value=(True, "0x10000100: 00052833")),
            patch.object(SettingsController, "_run_openocd_once",
                         return_value=(1, "failed to read memory")) as run,
            patch("pa_host.gui_server.time.sleep"),
            pytest.raises(RuntimeError, match="写入/校验/复位失败"),
        ):
            SettingsController._flash_firmware(firmware)

        assert run.call_count == 3
        assert run.call_args.args[1:] == ("init", "reset run", "shutdown")


def test_intel_hex_parser_rejects_corrupt_checksum() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        firmware = Path(tmp) / "firmware.hex"
        firmware.write_text(":0100000000FE\n", encoding="ascii")
        with pytest.raises(RuntimeError, match="Intel HEX 校验和错误"):
            SettingsController._intel_hex_image(firmware)


def test_intel_hex_parser_rejects_uicr_overlap_and_records_after_eof() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        uicr = root / "uicr.hex"
        _write_hex(uicr, (0, 4, b"\x10\x00"), (0x1014, 0, b"\x05"),
                   (0, 1, b""))
        with pytest.raises(RuntimeError, match="应用 Flash 之外"):
            SettingsController._intel_hex_image(uicr)

        overlap = root / "overlap.hex"
        _write_hex(overlap, (0, 0, b"\x01\x02"), (1, 0, b"\x02"),
                   (0, 1, b""))
        with pytest.raises(RuntimeError, match="重叠数据地址"):
            SettingsController._intel_hex_image(overlap)

        trailing = root / "trailing.hex"
        _write_hex(trailing, (0, 0, b"\x01"), (0, 1, b""),
                   (2, 0, b"\x03"))
        with pytest.raises(RuntimeError, match="EOF 后仍有记录"):
            SettingsController._intel_hex_image(trailing)


def test_control_api_requires_json_and_rejects_cross_origin_posts() -> None:
    assert _request_body(
        b'{"enabled":true}', origin="http://127.0.0.1:8769"
    ) == {"enabled": True}
    with pytest.raises(ValueError, match="application/json"):
        _request_body(b"enabled=true", content_type="text/plain")
    with pytest.raises(ValueError, match="跨站"):
        _request_body(b"{}", origin="http://malicious.example")


def test_filter_apply_updates_future_scheduled_run_configuration() -> None:
    app = AppState()
    updated = {
        "mode": "analysis", "lowpass_enabled": True,
        "lowpass_cutoff_hz": 0.5, "lowpass_auto": False,
        "lowpass_order": 3,
    }
    app.schedule.active = True
    app.schedule.filter_config = {**updated, "mode": "off"}
    handler = RequestHandler.__new__(RequestHandler)
    handler.path = "/api/filter/apply"
    handler._body = Mock(return_value=updated)
    handler._send_json = Mock()

    with (
        patch("pa_host.gui_server.APP", app),
        patch.object(app.filter, "apply", return_value={"settings": updated}),
        patch.object(app.measurement, "snapshot", return_value={"state": "idle"}),
    ):
        handler.do_POST()

    assert app.schedule.filter_config == updated
    handler._send_json.assert_called_once_with({"settings": updated})


def test_debug_command_rejects_non_ascii_before_reaching_collector() -> None:
    with pytest.raises(ValueError, match="ASCII"):
        MeasurementController._validate_command_line("SET 量程=1")


@pytest.mark.parametrize(
    ("return_code", "auto_stop_requested"),
    [(0, False), (3, True), (-15, True)],
)
def test_measurement_accepts_natural_or_known_adaptive_stop_exits(
    return_code: int, auto_stop_requested: bool,
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        ctrl = MeasurementController()
        ctrl.settings = SettingsController.validate({"adaptive_stop": True})
        ctrl.filter_config = {**ctrl.filter_config, "mode": "off"}
        ctrl.state = "running"
        ctrl.auto_stop_requested = auto_stop_requested
        ctrl._plateau_consecutive_passes = 2
        ctrl._plateau_evaluation = {"stable": True, "window_end_s": 35.0}
        ctrl.run_dir = root
        ctrl.raw_path = root / "raw.csv"
        ctrl.resampled_path = root / "resampled.csv"
        ctrl.filtered_path = root / "filtered.csv"
        ctrl.summary_path = root / "summary.json"
        ctrl.process = Mock()
        ctrl.process.poll.return_value = return_code
        ctrl.process.wait.return_value = return_code
        with ctrl.raw_path.open("w", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["seq", "dev_ms", "fa_fw", "sat", "ovf"])
            for index in range(351):
                writer.writerow([index, index * 100, 4_000_000, 0, 0])

        log_handle = Mock()
        ctrl._watch(log_handle)

        assert ctrl.state == "completed"
        assert ctrl.summary is not None
        assert ctrl.summary["steady_current_nA"] == pytest.approx(4.0)
        assert ctrl.summary["adaptive_stop"]["auto_stopped"] is auto_stop_requested
        assert (
            json.loads(ctrl.summary_path.read_text())["adaptive_stop"]["auto_stopped"]
            is auto_stop_requested
        )
        log_handle.close.assert_called_once_with()


@pytest.mark.parametrize("return_code", [0, 1, 2])
def test_adaptive_stop_rejects_unexpected_exit(return_code: int) -> None:
    ctrl = MeasurementController()
    ctrl.state = "running"
    ctrl.auto_stop_requested = True
    ctrl.run_dir = Path("failed-run")
    ctrl.process = Mock()
    ctrl.process.poll.return_value = return_code
    ctrl.process.wait.return_value = return_code
    completed: list[dict[str, object]] = []
    ctrl.on_complete = completed.append

    log_handle = Mock()
    ctrl._watch(log_handle)

    assert ctrl.state == "error"
    assert ctrl.summary is None
    assert completed and completed[0]["state"] == "error"
    assert str(return_code) in ctrl.error
    log_handle.close.assert_called_once_with()


@pytest.mark.parametrize(
    ("return_code", "bridge_stop_forced"),
    [(3, False), (0, True), (1, True)],
)
def test_manual_known_stop_is_not_promoted_to_a_completed_measurement(
    return_code: int, bridge_stop_forced: bool,
) -> None:
    ctrl = MeasurementController()
    ctrl.state = "running"
    ctrl.user_stop_requested = True
    ctrl._bridge_stop_forced = bridge_stop_forced
    ctrl.process = Mock()
    ctrl.process.poll.return_value = return_code
    ctrl.process.wait.return_value = return_code
    completed: list[dict[str, object]] = []
    ctrl.on_complete = completed.append

    log_handle = Mock()
    ctrl._watch(log_handle)

    assert ctrl.state == "idle"
    assert ctrl.summary is None
    assert completed and completed[0]["state"] == "idle"
    log_handle.close.assert_called_once_with()


def test_stop_timeout_marks_bridge_shutdown_before_termination() -> None:
    ctrl = MeasurementController()
    ctrl.user_stop_requested = True
    process = Mock()
    process.poll.return_value = None
    process.wait.return_value = 0

    with (
        patch("pa_host.gui_server.time.sleep"),
        patch.object(ctrl, "_terminate_tree") as terminate,
    ):
        ctrl._terminate_if_running(process, 1.5)

    assert ctrl._bridge_stop_forced is True
    terminate.assert_called_once_with(process)
    process.wait.assert_called_once_with(timeout=6)


def test_natural_finish_falls_back_to_last_complete_rolling_window() -> None:
    ctrl = MeasurementController()
    ctrl.state = "running"
    ctrl._last_complete_rolling_metrics = {
        **ctrl._empty_rolling_metrics("ready", "末端窗口指标已就绪"),
        "steady_current_nA": 4.25,
        "native_point_count": 100,
        "valid_native_point_count": 100,
        "stage_key": "epoch-1@0",
    }
    ctrl._last_complete_rolling_epoch = 1
    terminal_metrics = {
        **ctrl._empty_rolling_metrics("accumulating", "孤立尖峰后等待新数据"),
        "native_point_count": 105,
        "valid_native_point_count": 104,
        "progress_percent": 99.5,
    }

    def refresh_terminal(_data: dict[str, list[Any]], *, force: bool) -> None:
        assert force is True
        ctrl._rolling_metrics = dict(terminal_metrics)

    with patch.object(
        ctrl, "_refresh_live_analysis_locked", side_effect=refresh_terminal,
    ):
        ctrl._freeze_live_analysis_locked(
            {"time_s": [], "current_nA": [], "valid": [], "epoch": []},
            completed=True,
        )

    assert ctrl._rolling_metrics["steady_current_nA"] == pytest.approx(4.25)
    assert ctrl._rolling_metrics["native_point_count"] == 105
    assert ctrl._rolling_metrics["valid_native_point_count"] == 104
    assert ctrl._rolling_metrics["progress_percent"] == 100.0
    assert ctrl._rolling_metrics["status"] == "frozen"


def test_manual_stop_freezes_complete_window_at_request_time(tmp_path: Path) -> None:
    ctrl = MeasurementController()
    ctrl.state = "running"
    ctrl.process = Mock()
    ctrl.cmd_path = tmp_path / "cmd.txt"
    ctrl._rolling_metrics = {
        **ctrl._empty_rolling_metrics("accumulating", "新阶段正在累积"),
        "native_point_count": 105,
        "valid_native_point_count": 104,
    }
    ctrl._last_complete_rolling_metrics = {
        **ctrl._empty_rolling_metrics("ready", "末端窗口指标已就绪"),
        "steady_current_nA": 4.25,
        "native_point_count": 100,
        "valid_native_point_count": 100,
    }

    assert ctrl._request_stop_locked(automatic=False)
    ctrl._rolling_metrics["steady_current_nA"] = 99.0
    ctrl._last_complete_rolling_metrics["steady_current_nA"] = 88.0

    with patch.object(ctrl, "_refresh_live_analysis_locked") as refresh:
        ctrl._freeze_live_analysis_locked(
            {"time_s": [], "current_nA": [], "valid": [], "epoch": []},
            completed=False,
        )

    refresh.assert_not_called()
    assert ctrl._rolling_metrics["steady_current_nA"] == pytest.approx(4.25)
    assert ctrl._rolling_metrics["native_point_count"] == 105
    assert ctrl._rolling_metrics["status"] == "frozen"


def test_automatic_stop_freeze_prefers_trigger_evidence() -> None:
    ctrl = MeasurementController()
    ctrl.state = "running"
    ctrl.auto_stop_requested = True
    ctrl._rolling_metrics = {
        **ctrl._empty_rolling_metrics("ready", "后续刷新"),
        "steady_current_nA": 99.0,
    }
    ctrl._last_complete_rolling_metrics = {
        **ctrl._empty_rolling_metrics("ready", "最近完整窗口"),
        "steady_current_nA": 88.0,
    }
    ctrl._stop_requested_rolling_metrics = {
        **ctrl._empty_rolling_metrics("ready", "手动停止快照"),
        "steady_current_nA": 77.0,
    }
    ctrl._auto_stop_evidence = {
        "rolling_metrics": {
            **ctrl._empty_rolling_metrics("ready", "自动停止触发证据"),
            "steady_current_nA": 4.25,
        },
        "stability_eta": {"status": "ready", "seconds": 0},
    }

    ctrl._freeze_live_analysis_locked(
        {"time_s": [], "current_nA": [], "valid": [], "epoch": []},
        completed=True,
    )

    assert ctrl._rolling_metrics["steady_current_nA"] == pytest.approx(4.25)
    assert ctrl._rolling_metrics["status"] == "frozen"


def test_live_analysis_reset_clears_terminal_window_caches() -> None:
    ctrl = MeasurementController()
    ctrl._last_complete_rolling_metrics = {"steady_current_nA": 4.25}
    ctrl._last_complete_rolling_epoch = 3
    ctrl._stop_requested_rolling_metrics = {"steady_current_nA": 4.25}

    ctrl._reset_plateau_monitor_locked(hardware_context_changed=True)

    assert ctrl._last_complete_rolling_metrics is None
    assert ctrl._last_complete_rolling_epoch is None
    assert ctrl._stop_requested_rolling_metrics is None

    ctrl.settings["method"] = "cv"
    ctrl._last_complete_rolling_metrics = {"steady_current_nA": 8.5}
    ctrl._last_complete_rolling_epoch = 4
    ctrl._data_context_reset_pending = True
    ctrl._refresh_live_analysis_locked(
        {"time_s": [], "current_nA": [], "valid": [], "epoch": []},
        force=True,
    )

    assert ctrl._data_context_reset_pending is False
    assert ctrl._last_complete_rolling_metrics is None
    assert ctrl._last_complete_rolling_epoch is None


def test_confirmed_reversal_invalidates_current_window_before_manual_stop(
    tmp_path: Path,
) -> None:
    ctrl = MeasurementController()
    ctrl.state = "running"
    ctrl.process = Mock()
    ctrl.cmd_path = tmp_path / "cmd.txt"
    ctrl._prepared_live_stage = Mock()
    ctrl._rolling_metrics = {
        **ctrl._empty_rolling_metrics("ready", "反转前窗口"),
        "steady_current_nA": 4.25,
    }
    ctrl._last_complete_rolling_metrics = dict(ctrl._rolling_metrics)
    ctrl._last_complete_rolling_epoch = 3
    ctrl._stability_eta = {
        "reset_consecutive": True,
        "suggested_stage_start_s": 42.0,
    }

    ctrl._apply_confirmed_reversal_to_plateau_locked()

    assert ctrl._prepared_live_stage is None
    assert ctrl._rolling_metrics["status"] == "accumulating"
    assert ctrl._rolling_metrics["steady_current_nA"] is None
    assert ctrl._last_complete_rolling_metrics is None
    with patch("pa_host.gui_server.threading.Thread"):
        assert ctrl._request_stop_locked(automatic=False)
    assert ctrl._stop_requested_rolling_metrics is None


def test_measurement_watcher_is_non_daemon(tmp_path: Path) -> None:
    ctrl = MeasurementController()
    process = Mock()
    watcher = Mock()
    watcher.is_alive.return_value = False

    with (
        patch("pa_host.gui_server.RUNS_DIR", tmp_path),
        patch("pa_host.gui_server._require_jlink_target"),
        patch("pa_host.gui_server.subprocess.Popen", return_value=process),
        patch("pa_host.gui_server.threading.Thread", return_value=watcher) as thread_cls,
    ):
        ctrl.start()

    assert thread_cls.call_args.kwargs["daemon"] is False
    watcher.start.assert_called_once_with()
    thread_cls.call_args.kwargs["args"][0].close()


def test_wait_for_completion_joins_watcher_without_a_deadline() -> None:
    ctrl = MeasurementController()
    watcher = Mock()
    watcher.is_alive.return_value = True
    ctrl.thread = watcher

    with patch("pa_host.gui_server.threading.current_thread", return_value=object()):
        ctrl.wait_for_completion()

    watcher.join.assert_called_once_with()


def test_watcher_start_failure_reclaims_collector_and_gets_diagnostic_id(
    tmp_path: Path,
) -> None:
    ctrl = MeasurementController()
    diagnostics = DiagnosticStore(tmp_path / "logs")
    process = Mock(pid=4321)
    process.poll.return_value = None
    watcher = Mock()
    watcher.start.side_effect = RuntimeError("thread unavailable")

    with (
        patch("pa_host.gui_server.RUNS_DIR", tmp_path / "runs"),
        patch("pa_host.gui_server.DIAGNOSTICS", diagnostics),
        patch("pa_host.gui_server.HARDWARE_TRANSPORT", "rtt"),
        patch("pa_host.gui_server.HARDWARE_TRANSPORT_REQUESTED", "rtt"),
        patch("pa_host.gui_server._require_jlink_target"),
        patch("pa_host.gui_server.subprocess.Popen", return_value=process),
        patch("pa_host.gui_server.threading.Thread", return_value=watcher),
        patch.object(MeasurementController, "_terminate_tree") as terminate,
        pytest.raises(RuntimeError, match="thread unavailable") as exc_info,
    ):
        ctrl.start()

    terminate.assert_called_once_with(process)
    assert ctrl.state == "error"
    assert ctrl.error == "无法启动采集收尾线程"
    event = diagnostics.snapshot()["events"][-1]
    assert event["event"] == "measurement.watcher_start_failed"
    assert exc_info.value.diagnostic_id == event["event_id"]


def test_windows_terminate_tree_releases_openocd_before_forcing() -> None:
    process = Mock(pid=4321)
    process.poll.return_value = None
    process.wait.return_value = 0

    with (
        patch("pa_host.gui_server._IS_WIN", True),
        patch("pa_host.gui_server._port_accepts_connections", return_value=True),
        patch("pa_host.gui_server._release_stale_measurement_bridge") as release,
        patch("pa_host.gui_server.subprocess.run") as taskkill,
    ):
        MeasurementController._terminate_tree(process)

    release.assert_called_once_with()
    process.wait.assert_called_once_with(timeout=6)
    taskkill.assert_not_called()


def test_windows_terminate_tree_forces_after_bridge_release_failure() -> None:
    process = Mock(pid=4321)
    process.poll.return_value = None

    with (
        patch("pa_host.gui_server._IS_WIN", True),
        patch("pa_host.gui_server._port_accepts_connections", return_value=True),
        patch(
            "pa_host.gui_server._release_stale_measurement_bridge",
            side_effect=RuntimeError("bridge stuck"),
        ),
        patch("pa_host.gui_server.subprocess.run") as taskkill,
    ):
        MeasurementController._terminate_tree(process)

    taskkill.assert_called_once_with(
        ["taskkill", "/F", "/T", "/PID", "4321"],
        capture_output=True,
        timeout=10,
        **gui_server.runtime.hidden_subprocess_kwargs(),
    )


def test_windows_terminate_tree_forces_after_bridge_wait_timeout() -> None:
    process = Mock(pid=4321)
    process.poll.return_value = None
    process.wait.side_effect = subprocess.TimeoutExpired(["collector"], 6)
    taskkill_result = subprocess.CompletedProcess(["taskkill"], returncode=0)

    with (
        patch("pa_host.gui_server._IS_WIN", True),
        patch("pa_host.gui_server._port_accepts_connections", return_value=True),
        patch("pa_host.gui_server._release_stale_measurement_bridge") as release,
        patch(
            "pa_host.gui_server.subprocess.run", return_value=taskkill_result,
        ) as taskkill,
    ):
        MeasurementController._terminate_tree(process)

    release.assert_called_once_with()
    taskkill.assert_called_once()
    process.kill.assert_not_called()


def test_windows_terminate_tree_falls_back_when_taskkill_fails() -> None:
    process = Mock(pid=4321)
    process.poll.return_value = None
    taskkill_result = subprocess.CompletedProcess(["taskkill"], returncode=1)

    with (
        patch("pa_host.gui_server._IS_WIN", True),
        patch("pa_host.gui_server._port_accepts_connections", return_value=False),
        patch("pa_host.gui_server._release_stale_measurement_bridge") as release,
        patch(
            "pa_host.gui_server.subprocess.run", return_value=taskkill_result,
        ) as taskkill,
    ):
        MeasurementController._terminate_tree(process)

    release.assert_not_called()
    taskkill.assert_called_once()
    process.kill.assert_called_once_with()


def test_windows_kill_tree_falls_back_when_taskkill_fails() -> None:
    process = Mock(pid=4321)
    process.poll.return_value = None
    taskkill_result = subprocess.CompletedProcess(["taskkill"], returncode=1)

    with (
        patch("pa_host.gui_server._IS_WIN", True),
        patch(
            "pa_host.gui_server.subprocess.run", return_value=taskkill_result,
        ) as taskkill,
    ):
        MeasurementController._kill_tree(process)

    taskkill.assert_called_once()
    process.kill.assert_called_once_with()


def test_server_shutdown_waits_for_measurement_finalization() -> None:
    server = Mock(server_port=8769)
    app = Mock()
    app.measurement.process = None

    with (
        patch("pa_host.gui_server.DiagnosticHTTPServer", return_value=server),
        patch("pa_host.gui_server.APP", app),
        patch("pa_host.gui_server.signal.signal"),
    ):
        serve(port=8769)

    app.schedule.stop.assert_called_once_with()
    app.measurement.stop.assert_called_once_with()
    app.measurement.wait_for_completion.assert_called_once_with()
    server.server_close.assert_called_once_with()


def test_debug_probe_arms_rtt_without_starting_and_begin_queues_set_first(
    tmp_path: Path,
) -> None:
    app = AppState()
    app.save_dir = tmp_path / "workspace"
    app._load_workspace()
    line = "SET fsr=2 off=4 conv=auto period=4 e=200 vwe=1200 idle=2 sysper=2 cellv=1 ioc=0"
    with patch.object(app.schedule, "snapshot", return_value={"active": False}), \
            patch.object(app.measurement, "snapshot", return_value={"state": "idle"}), \
            patch.object(app.measurement, "start", return_value={"state": "running"}) as start:
        app.start_debug_run({"note": "probe", "probe_only": True})
    assert start.call_args.kwargs["trigger"] == "ARMED"

    with tempfile.TemporaryDirectory() as tmp:
        ctrl = MeasurementController()
        ctrl.state = "running"
        ctrl.process = Mock()
        ctrl.cmd_path = Path(tmp) / "cmd.txt"
        ctrl.debug_waiting_for_start = True
        ctrl._cfg_confirmed_this_session = True
        result = ctrl.begin_debug_measurement(line)
        assert ctrl.cmd_path.read_text(encoding="utf-8") == f"{line}\n"
        assert result["sent"] == [line]
        assert ctrl.debug_waiting_for_start is True

        ctrl._cfg_live = {
            "ep": 2, "confirmed_ep": 2, "fsr": 2, "off": 4,
            "conv": 4, "conv_src": "auto", "period": 4,
            "e_mv": 200, "vwe_mv": 1200, "idle": 2, "sysper": 2,
            "cellv": 1, "ioc": 0,
        }
        ctrl._maybe_start_confirmed_debug()
        assert ctrl.cmd_path.read_text(encoding="utf-8") == f"{line}\nSTART\n"
        assert ctrl.debug_waiting_for_start is False


def test_rejected_debug_config_never_starts_with_the_old_values() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ctrl = MeasurementController()
        ctrl.state = "running"
        ctrl.process = Mock()
        ctrl.cmd_path = Path(tmp) / "cmd.txt"
        ctrl.debug_waiting_for_start = True
        ctrl._cfg_confirmed_this_session = True
        ctrl._cfg_live = {"ep": 1, "confirmed_ep": 1, "fsr": 0, "off": 2,
                          "conv": 0, "conv_src": "auto", "period": 0,
                          "e_mv": -200, "vwe_mv": 1200, "idle": 2,
                          "sysper": 3, "cellv": 1, "ioc": 0}
        bad = "SET fsr=0 off=7 conv=auto period=0 e=-200 vwe=250 idle=2 sysper=3 cellv=1 ioc=0"
        ctrl.begin_debug_measurement(bad)
        ctrl._last_reject = {"kind": "CFG_REJECT", "reason": "offset_gt_fsr"}
        ctrl._maybe_start_confirmed_debug()
        assert ctrl.cmd_path.read_text(encoding="utf-8") == bad + "\n"
        assert ctrl.debug_waiting_for_start is True


def test_debug_config_already_confirmed_starts_without_waiting_for_new_epoch() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ctrl = MeasurementController()
        ctrl.state = "running"
        ctrl.process = Mock()
        ctrl.cmd_path = Path(tmp) / "cmd.txt"
        ctrl.debug_waiting_for_start = True
        ctrl._cfg_confirmed_this_session = True
        ctrl._cfg_live = {"ep": 2, "confirmed_ep": 2, "fsr": 0, "off": 6,
                          "conv": 0, "conv_src": "auto", "period": 0,
                          "e_mv": -200, "vwe_mv": 250, "idle": 2,
                          "sysper": 3, "cellv": 1, "ioc": 0}
        line = "SET fsr=0 off=6 conv=auto period=0 e=-200 vwe=250 idle=2 sysper=3 cellv=1 ioc=0"
        result = ctrl.begin_debug_measurement(line)
        assert result["already_applied"] is True
        assert ctrl.cmd_path.read_text(encoding="utf-8") == "START\n"
        assert ctrl.debug_waiting_for_start is False


def test_previous_run_confirmation_cannot_skip_set_after_reconnect() -> None:
    """Stop -> reconnect may reboot the MCU; cached V_WE=250 must not authorize START."""
    with tempfile.TemporaryDirectory() as tmp:
        ctrl = MeasurementController()
        ctrl.state = "running"
        ctrl.process = Mock()
        ctrl.cmd_path = Path(tmp) / "cmd.txt"
        ctrl.debug_waiting_for_start = True
        # This is deliberately a stale snapshot from the previous RTT session.
        ctrl._cfg_live = {"ep": 2, "confirmed_ep": 2, "fsr": 0, "off": 6,
                          "conv": 0, "conv_src": "auto", "period": 0,
                          "e_mv": -200, "vwe_mv": 250, "idle": 2,
                          "sysper": 3, "cellv": 1, "ioc": 0}
        ctrl._cfg_confirmed_this_session = False
        line = "SET fsr=0 off=6 conv=auto period=0 e=-200 vwe=250 idle=2 sysper=3 cellv=1 ioc=0"

        try:
            ctrl.begin_debug_measurement(line)
        except RuntimeError as exc:
            assert "本次连接" in str(exc)
        else:
            raise AssertionError("上一轮的 CFG_CONFIRMED 不应允许本轮直接 START")
        assert not ctrl.cmd_path.exists()


def test_debug_incremental_read_keeps_partial_jsonl_and_csv_lines() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        ctrl = MeasurementController()
        ctrl.audit_path = root / "audit.jsonl"
        ctrl.raw_path = root / "current.csv"
        ctrl.cell_v_path = root / "cellv.csv"
        ctrl.audit_path.write_text('{"kind":"CFG_BOOT",', encoding="utf-8")
        ctrl.raw_path.write_text("dev_ms,fa_fw,sat,epoch\n100,1000000,0,1\n", encoding="utf-8")
        ctrl.cell_v_path.write_text("dev_ms,e_mv,we_mv,re_mv,epoch,we_code,re_code\n", encoding="utf-8")

        assert ctrl._audit_events() == []
        assert len(ctrl._debug_series()["current"]["t"]) == 1
        with ctrl.audit_path.open("a", encoding="utf-8") as handle:
            handle.write('"ms":1}\n')
        with ctrl.raw_path.open("a", encoding="utf-8") as handle:
            handle.write("200,2000000,0,1")
        assert ctrl._audit_events()[-1]["kind"] == "CFG_BOOT"
        assert len(ctrl._debug_series()["current"]["t"]) == 1
        with ctrl.raw_path.open("a", encoding="utf-8") as handle:
            handle.write("\n")
        assert len(ctrl._debug_series()["current"]["t"]) == 2


def test_live_data_device_clock_rollback_resets_platform_monitor() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ctrl = MeasurementController()
        ctrl.raw_path = Path(tmp) / "raw.csv"
        ctrl.raw_path.write_text(
            "dev_ms,fa_fw,sat,ovf\n1000,1000000,0,0\n",
            encoding="utf-8",
        )
        ctrl._data()
        ctrl._plateau_last_segment = 8
        ctrl._plateau_consecutive_passes = 2
        ctrl._plateau_evaluation = {"stable": True}
        ctrl._plateau_progress = {"elapsed_s": 40.0}

        with ctrl.raw_path.open("a", encoding="utf-8") as handle:
            handle.write("500,2000000,0,0\n")
        data = ctrl._data()

        assert data["time_s"] == [0.0]
        assert ctrl._plateau_last_segment == 0
        assert ctrl._plateau_consecutive_passes == 0
        assert ctrl._plateau_evaluation is None
        assert ctrl._plateau_progress == {}


def test_new_cfg_epoch_resets_platform_monitor_only_once() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ctrl = MeasurementController()
        ctrl.audit_path = Path(tmp) / "audit.jsonl"
        ctrl.audit_path.write_text(
            json.dumps({"kind": "CFG_APPLIED", "ep": 3}) + "\n",
            encoding="utf-8",
        )
        ctrl._audit_events()
        ctrl._plateau_last_segment = 6
        ctrl._plateau_consecutive_passes = 2
        ctrl._plateau_evaluation = {"stable": True}
        ctrl._plateau_progress = {"elapsed_s": 30.0}

        with ctrl.audit_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"kind": "CFG_DERIVED", "ep": 3}) + "\n")
            handle.write(json.dumps({"kind": "CFG_CONFIRMED", "ep": 3}) + "\n")
        ctrl._audit_events()

        assert ctrl._plateau_last_segment == 6
        assert ctrl._plateau_consecutive_passes == 2
        assert ctrl._plateau_evaluation == {"stable": True}
        assert ctrl._plateau_progress == {"elapsed_s": 30.0}

        with ctrl.audit_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"kind": "CFG_APPLIED", "ep": 4}) + "\n")
        ctrl._audit_events()

        assert ctrl._plateau_cfg_epoch == 4
        assert ctrl._plateau_last_segment == 0
        assert ctrl._plateau_consecutive_passes == 0
        assert ctrl._plateau_evaluation is None
        assert ctrl._plateau_progress == {}


def test_new_cfg_epoch_excludes_unread_old_epoch_tail_from_platform() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        ctrl = MeasurementController()
        ctrl.settings = SettingsController.validate({"adaptive_stop": True})
        ctrl.state = "running"
        ctrl.metadata = {}
        ctrl.process = Mock()
        ctrl.cmd_path = root / "cmd.txt"
        ctrl.audit_path = root / "audit.jsonl"
        ctrl.raw_path = root / "raw.csv"
        ctrl.audit_path.write_text(
            json.dumps({"kind": "CFG_APPLIED", "ep": 1}) + "\n",
            encoding="utf-8",
        )
        initial = np.arange(0.0, 10.01, 0.1)
        ctrl.raw_path.write_text(
            "dev_ms,fa_fw,sat,ovf,epoch\n" + "".join(
                f"{round(value * 1000)},99000000,0,0,1\n" for value in initial
            ),
            encoding="utf-8",
        )
        ctrl._audit_events()
        ctrl._maybe_auto_stop()
        assert ctrl._plateau_context_epoch == 1
        assert max(ctrl._data_cache["time_s"]) == pytest.approx(10.0)

        # The cache still ends at 10.0 s. Rows through 10.7 s were produced by
        # the old hardware context but have not been parsed when ep=2 arrives.
        old_tail = np.arange(10.1, 10.71, 0.1)
        new_context = np.arange(10.8, 45.11, 0.1)
        with ctrl.raw_path.open("a", encoding="utf-8") as handle:
            handle.write("".join(
                f"{round(value * 1000)},99000000,0,0,1\n" for value in old_tail
            ))
            handle.write("".join(
                f"{round(value * 1000)},4000000,0,0,2\n"
                for value in new_context
            ))
        with ctrl.audit_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"kind": "CFG_APPLIED", "ep": 2}) + "\n")
        ctrl._audit_events()

        with patch(
            "pa_host.gui_server.evaluate_platform",
            wraps=__import__("pa_host.it", fromlist=["evaluate_platform"]).evaluate_platform,
        ) as evaluate:
            ctrl._maybe_auto_stop()

        assert evaluate.called
        evaluated_times, evaluated_currents = evaluate.call_args.args[:2]
        assert min(evaluated_times) == pytest.approx(10.8)
        assert set(evaluated_currents) == {4.0}
        assert ctrl._plateau_context_epoch == 2
        assert ctrl._plateau_context_start_s == pytest.approx(10.8)
        assert ctrl._data_cache["epoch"][-1] == 2


def test_new_cfg_epoch_clears_stale_derived_fields_and_requests_replay() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ctrl = MeasurementController()
        root = Path(tmp)
        ctrl.audit_path = root / "audit.jsonl"
        ctrl.cmd_path = root / "cmd.txt"
        ctrl.audit_path.write_text(
            "".join(json.dumps(event) + "\n" for event in (
                {"kind": "CFG_APPLIED", "ep": 3, "period": 4},
                {"kind": "CFG_DERIVED", "ep": 3, "bits": 18,
                 "period_ms": 124},
                {"kind": "CFG_CONFIRMED", "ep": 3, "verify_ok": 1},
            )),
            encoding="utf-8",
        )
        ctrl._audit_events()
        assert ctrl._cfg_live["bits"] == 18
        assert ctrl._cfg_live["confirmed_ep"] == 3

        with ctrl.audit_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "kind": "CFG_APPLIED", "ep": 4, "period": 5,
            }) + "\n")
        ctrl._audit_events()

        assert ctrl._cfg_live["ep"] == 4
        assert ctrl._cfg_live["period"] == 5
        assert "bits" not in ctrl._cfg_live
        assert "period_ms" not in ctrl._cfg_live
        assert "confirmed_ep" not in ctrl._cfg_live
        assert ctrl._cfg_confirmed_this_session is False

        ctrl.state = "running"
        ctrl.metadata = {"debug": True}
        ctrl._auto_get_at = 0.0
        ctrl.debug_snapshot()

        assert ctrl.cmd_path.read_text(encoding="utf-8") == "GET\n"


def test_range_applied_resets_platform_monitor() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ctrl = MeasurementController()
        ctrl.raw_log = Path(tmp) / "rtt.log"
        ctrl.raw_log.write_text(
            "RANGE_APPLIED fsr_pa=250000 off_pa=9000\n", encoding="utf-8"
        )
        ctrl._plateau_last_segment = 6
        ctrl._plateau_consecutive_passes = 2
        ctrl._plateau_evaluation = {"stable": True}
        ctrl._plateau_progress = {"elapsed_s": 30.0}

        ctrl._scan_range_events()

        assert ctrl.range_runtime["applied"]["fsr_pa"] == 250000
        assert ctrl._plateau_last_segment == 0
        assert ctrl._plateau_consecutive_passes == 0
        assert ctrl._plateau_evaluation is None
        assert ctrl._plateau_progress == {}
        assert ctrl._plateau_context_pending is True


def test_range_applied_boundary_waits_for_first_new_epoch_sample() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        ctrl = MeasurementController()
        ctrl.raw_path = root / "raw.csv"
        ctrl.raw_log = root / "rtt.log"
        initial = np.arange(0.0, 10.01, 0.1)
        ctrl.raw_path.write_text(
            "dev_ms,fa_fw,sat,ovf,epoch\n" + "".join(
                f"{round(value * 1000)},99000000,0,0,1\n" for value in initial
            ),
            encoding="utf-8",
        )
        ctrl._plateau_cfg_epoch = 1
        with ctrl.lock:
            first = ctrl._plateau_context_data_locked(ctrl._data())
        assert first["epoch"][-1] == 1

        with ctrl.raw_path.open("a", encoding="utf-8") as handle:
            handle.write("10100,99000000,0,0,1\n10700,99000000,0,0,1\n")
            handle.write("10800,4000000,0,0,2\n10900,4000000,0,0,2\n")
        ctrl._cfg_live = {"fsr": 2, "off": 4}
        ctrl.raw_log.write_text(
            "RANGE_APPLIED fsr_code=1 offset_sel=4\n", encoding="utf-8"
        )
        ctrl._scan_range_events()
        with ctrl.lock:
            current = ctrl._plateau_context_data_locked(ctrl._data())

        assert current["time_s"] == pytest.approx([10.8, 10.9])
        assert current["current_nA"] == [4.0, 4.0]
        assert current["epoch"] == [2, 2]
        assert ctrl._plateau_context_epoch == 2
        assert ctrl._plateau_context_pending is False


def test_debug_series_rejects_saturated_and_overflow_samples() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ctrl = MeasurementController()
        ctrl.raw_path = Path(tmp) / "raw.csv"
        ctrl.raw_path.write_text(
            "dev_ms,fa_fw,sat,ovf,epoch\n"
            "100,1000000,0,0,1\n"
            "200,2000000,1,0,1\n"
            "300,3000000,0,1,1\n",
            encoding="utf-8",
        )

        assert ctrl._debug_series()["current"]["valid"] == [True, False, False]


@pytest.mark.parametrize(
    ("path", "method_name", "payload"),
    [
        ("/api/debug/stop", "stop", {}),
        ("/api/debug/begin", "begin_debug_measurement", {"line": "SET fsr=2"}),
        ("/api/debug/cmd", "send_command", {"line": "SET fsr=2 FORCE"}),
    ],
)
def test_debug_mutation_endpoints_reject_formal_measurement(
    path: str, method_name: str, payload: dict[str, object],
) -> None:
    app = AppState()
    app.measurement.state = "running"
    app.measurement.metadata = {"debug": False}
    handler = RequestHandler.__new__(RequestHandler)
    handler.path = path
    handler._body = Mock(return_value=payload)
    handler._send_json = Mock()

    with patch("pa_host.gui_server.APP", app), \
            patch.object(app.measurement, method_name) as mutation:
        handler.do_POST()

    mutation.assert_not_called()
    response, status = handler._send_json.call_args.args
    assert "不是硬件 DEBUG 测量" in response["error"]
    assert status == 409


@pytest.mark.parametrize(
    ("path", "method_name", "payload"),
    [
        ("/api/debug/stop", "stop", {}),
        ("/api/debug/begin", "begin_debug_measurement", {"line": "SET fsr=2"}),
        ("/api/debug/cmd", "send_command", {"line": "GET"}),
    ],
)
def test_debug_mutation_endpoints_allow_debug_measurement(
    path: str, method_name: str, payload: dict[str, object],
) -> None:
    app = AppState()
    app.measurement.state = "running"
    app.measurement.metadata = {"debug": True}
    handler = RequestHandler.__new__(RequestHandler)
    handler.path = path
    handler._body = Mock(return_value=payload)
    handler._send_json = Mock()
    expected = {"sent": path}

    with patch("pa_host.gui_server.APP", app), \
            patch.object(app.measurement, method_name, return_value=expected) as mutation:
        handler.do_POST()

    mutation.assert_called_once()
    handler._send_json.assert_called_once_with(expected)


def test_debug_snapshot_exposes_read_only_formal_run_state() -> None:
    ctrl = MeasurementController()
    ctrl.state = "running"
    ctrl.metadata = {"debug": False}

    snapshot = ctrl.debug_snapshot()

    assert snapshot["debug_run"] is False
    assert snapshot["mutations_allowed"] is False


def test_it_tainted_audit_marks_formal_run_error_before_analysis() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ctrl = MeasurementController()
        ctrl.state = "running"
        ctrl.metadata = {"debug": False}
        ctrl.audit_path = Path(tmp) / "audit.jsonl"
        ctrl.audit_path.write_text(
            json.dumps({
                "kind": "IT_TAINTED", "ep": 4,
                "reason": "perturb_during_run",
            }) + "\n",
            encoding="utf-8",
        )
        ctrl.process = Mock()
        ctrl.process.poll.return_value = 0
        ctrl.process.wait.return_value = 0
        completed: list[dict[str, object]] = []
        ctrl.on_complete = completed.append
        log_handle = Mock()

        ctrl._watch(log_handle)

        assert ctrl.state == "error"
        assert ctrl.summary is None
        assert ctrl._hardware_taint is not None
        assert ctrl._hardware_taint["reason"] == "perturb_during_run"
        assert completed[0]["hardware_taint"]["kind"] == "IT_TAINTED"
        assert "IT_TAINTED" in ctrl.error
        log_handle.close.assert_called_once_with()


def test_it_done_final_marker_recovers_a_dropped_taint_event() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ctrl = MeasurementController()
        ctrl.raw_log = Path(tmp) / "rtt.log"
        ctrl.raw_log.write_text(
            "IT_DONE native=240 expected=240 elapsed_ms=30000 ep=4 tainted=1\n",
            encoding="utf-8",
        )

        ctrl._scan_range_events()

        assert ctrl._hardware_taint is not None
        assert ctrl._hardware_taint["kind"] == "IT_DONE"
        assert ctrl._hardware_taint["tainted"] == 1
        assert ctrl._hardware_taint["reason"] == "firmware_final_marker"


def test_tainted_formal_run_never_enters_calibration_or_measurement_index() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        app = AppState()
        app.save_dir = Path(tmp) / "workspace"
        app._load_workspace()
        raw_path = Path(tmp) / "tainted-raw.csv"
        raw_path.write_text("dev_ms,fa_fw,sat,ovf\n", encoding="utf-8")
        run = {
            "run_id": "tainted-run",
            "state": "error",
            "finished_at": 123.0,
            "run_dir": str(Path(tmp) / "run"),
            "raw_path": str(raw_path),
            "hardware_taint": {
                "kind": "IT_TAINTED", "reason": "perturb_during_run",
            },
            "metadata": {
                "sample_name": "bad-calibration",
                "sample_role": "calibration",
                "known_concentration_um": 10.0,
            },
        }

        with patch("pa_host.gui_server._notify_measurement_completion"):
            app._measurement_completed(run)

        result = app.measurement.snapshot()["workflow_result"]
        assert result["tainted"] is True
        assert result["state"] == "error"
        assert app.points == []
        assert app.records == []
        assert not (app.save_dir / "measurement-index.csv").exists()
        assert not (app.save_dir / "calibration-points.csv").exists()


def _cfg_gate_events(expected: dict[str, object], *, request_id: str,
                     epoch: int = 7, **overrides: object) -> list[dict[str, object]]:
    actual = {**expected, **overrides}
    actual.setdefault("sel", 1)
    actual.setdefault("amps", 1)
    applied_keys = {
        "fsr", "off", "conv_src", "period", "sysper", "clk40", "ioc",
        "e_mv", "vwe_mv", "idle", "cellv", "chop", "rs", "ios", "satpct",
        "sel", "amps",
    }
    applied = {key: actual[key] for key in applied_keys}
    return [
        {"kind": "CFG_APPLIED", "ep": epoch, "src": "get", "req": request_id,
         **applied},
        {"kind": "CFG_DERIVED", "ep": epoch, "req": request_id, "bits": 18},
        {"kind": "CFG_CONFIRMED", "ep": epoch, "src": "get", "req": request_id,
         "verify_ok": actual.get("verify_ok", 1),
         "invalid_cfg": actual.get("invalid_cfg", 0),
         "vdd_oor": actual.get("vdd_oor", 0)},
    ]


def _legacy_cfg_gate_events(
    expected: dict[str, object], *, epoch: int = 7, **overrides: object,
) -> list[dict[str, object]]:
    events = _cfg_gate_events(
        expected, request_id="", epoch=epoch, **overrides,
    )
    for event in events:
        event.pop("req", None)
    events[-1].pop("verify_ok", None)
    return events


def test_formal_config_gate_requires_one_confirmed_request_snapshot() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ctrl = MeasurementController()
        ctrl.state = "running"
        ctrl.cmd_path = Path(tmp) / "cmd.txt"
        ctrl.audit_path = Path(tmp) / "audit.jsonl"
        expected = SettingsController.runtime_afe_contract({})
        ctrl._config_gate = {
            "state": "checking", "expected": expected, "request_id": "abc123",
            "legacy_fallback_sent": False, "mismatches": [],
        }
        events = _cfg_gate_events(expected, request_id="abc123")
        ctrl.audit_path.write_text(
            "".join(json.dumps(event) + "\n" for event in events), encoding="utf-8"
        )

        ctrl._audit_events()

        assert ctrl.cmd_path.read_text(encoding="utf-8") == "START\n"
        assert ctrl._config_gate["state"] == "matched"
        assert ctrl.metadata["hardware_config"]["epoch"] == 7
        assert ctrl.metadata["hardware_config"]["verification_level"] == "physical_registers"


def test_formal_gate_waits_for_runtime_measurement_confirmation() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ctrl = MeasurementController()
        ctrl.state = "running"
        ctrl.cmd_path = Path(tmp) / "cmd.txt"
        ctrl.audit_path = Path(tmp) / "audit.jsonl"
        expected = SettingsController.runtime_afe_contract({})
        measurement = SettingsController.runtime_measurement_contract({})
        ctrl._config_gate = {
            "state": "checking", "expected": expected, "request_id": "runtime1",
            "measurement_expected": measurement,
            "measurement_command": "MEAS staged runtime1",
            "measurement_sent": False,
            "measurement_confirmed": False, "measurement_actual": {},
            "legacy_fallback_sent": False, "mismatches": [],
        }
        ctrl.audit_path.write_text(
            "".join(
                json.dumps(event) + "\n"
                for event in _cfg_gate_events(expected, request_id="runtime1")
            ),
            encoding="utf-8",
        )

        ctrl._audit_events()
        assert ctrl._config_gate["state"] == "checking"
        assert ctrl.cmd_path.read_text(encoding="utf-8") == "MEAS staged runtime1\n"

        with ctrl.audit_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "kind": "MEAS_CONFIRMED", "req": "runtime1", **measurement,
            }) + "\n")
        ctrl._audit_events()

        assert ctrl._config_gate["state"] == "matched"
        assert ctrl.cmd_path.read_text(encoding="utf-8") == (
            "MEAS staged runtime1\nSTART\n"
        )
        assert ctrl.metadata["hardware_config"]["measurement_actual"] == measurement


def test_formal_gate_blocks_runtime_measurement_mismatch() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ctrl = MeasurementController()
        ctrl.state = "running"
        ctrl.cmd_path = Path(tmp) / "cmd.txt"
        ctrl.audit_path = Path(tmp) / "audit.jsonl"
        expected = SettingsController.runtime_afe_contract({})
        measurement = SettingsController.runtime_measurement_contract({})
        ctrl._config_gate = {
            "state": "checking", "expected": expected, "request_id": "runtime2",
            "measurement_expected": measurement,
            "measurement_command": "MEAS staged runtime2",
            "measurement_sent": False,
            "measurement_confirmed": False, "measurement_actual": {},
            "legacy_fallback_sent": False, "mismatches": [],
        }
        ctrl.audit_path.write_text(
            "".join(
                json.dumps(event) + "\n"
                for event in _cfg_gate_events(expected, request_id="runtime2")
            ),
            encoding="utf-8",
        )

        ctrl._audit_events()
        assert ctrl.cmd_path.read_text(encoding="utf-8") == "MEAS staged runtime2\n"

        with ctrl.audit_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "kind": "MEAS_CONFIRMED", "req": "runtime2", **measurement,
                "duration_ms": int(measurement["duration_ms"]) + 1,
            }) + "\n")
        ctrl._audit_events()

        assert ctrl._config_gate["state"] == "mismatch"
        assert ctrl.cmd_path.read_text(encoding="utf-8") == "MEAS staged runtime2\n"
        assert {item["field"] for item in ctrl._config_gate["mismatches"]} == {
            "measurement.duration_ms",
        }


def test_runtime_commands_cover_custom_it_and_cv_conditions() -> None:
    request_id = "abc123def456"
    custom_it = {
        "potential_v": -0.2, "initial_potential_v": 0.2,
        "prestep_s": 180, "duration_s": 600, "target_rate_hz": 5,
        "sens_period_code": 1, "fsr_nA": 40000,
        "offset_mode": "80nA",
    }
    afe = SettingsController.runtime_afe_command(custom_it)
    measurement = SettingsController.runtime_measurement_command(custom_it, request_id)
    assert len(afe) < 128
    assert len(measurement) < 128
    assert "period=1" in afe and "e=-200" in afe
    assert measurement == (
        "MEAS 0 200 -200 180000 600000 0 200 -600 600 50 30 1 3 1 3 "
        + request_id
    )

    cv_contract = SettingsController.runtime_afe_contract({"method": "cv"})
    assert cv_contract["e_mv"] == -600
    assert cv_contract["vwe_mv"] == 800
    cv_measurement = SettingsController.runtime_measurement_contract({
        "method": "cv", "cv_quiet_s": 9,
    })
    assert cv_measurement["mode"] == 1
    assert cv_measurement["quiet_ms"] == 9000


def test_formal_config_gate_never_mixes_epochs_or_request_ids() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ctrl = MeasurementController()
        ctrl.state = "running"
        ctrl.cmd_path = Path(tmp) / "cmd.txt"
        ctrl.audit_path = Path(tmp) / "audit.jsonl"
        expected = SettingsController.runtime_afe_contract({})
        ctrl._config_gate = {
            "state": "checking", "expected": expected, "request_id": "fresh",
            "legacy_fallback_sent": False, "mismatches": [],
        }
        old = _cfg_gate_events(expected, request_id="old", epoch=2)
        split = _cfg_gate_events(expected, request_id="fresh", epoch=3)
        # The requested APPLIED cannot borrow DERIVED/CONFIRMED from another req.
        events = old + [split[0], {**split[1], "req": "other"}, {**split[2], "req": "other"}]
        ctrl.audit_path.write_text(
            "".join(json.dumps(event) + "\n" for event in events), encoding="utf-8"
        )

        ctrl._audit_events()

        assert not ctrl.cmd_path.exists()
        assert ctrl._config_gate["state"] == "checking"


def test_formal_config_gate_blocks_every_runtime_mismatch() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ctrl = MeasurementController()
        ctrl.state = "running"
        ctrl.cmd_path = Path(tmp) / "cmd.txt"
        ctrl.audit_path = Path(tmp) / "audit.jsonl"
        expected = SettingsController.runtime_afe_contract({})
        ctrl._config_gate = {
            "state": "checking", "expected": expected, "request_id": "gate1",
            "legacy_fallback_sent": False, "mismatches": [],
        }
        events = _cfg_gate_events(
            expected, request_id="gate1", idle=1, sysper=2, ioc=1,
            cellv=0, conv_src="pin", sel=0, amps=0,
        )
        ctrl.audit_path.write_text(
            "".join(json.dumps(event) + "\n" for event in events), encoding="utf-8"
        )

        ctrl._audit_events()

        assert not ctrl.cmd_path.exists()
        assert ctrl._config_gate["state"] == "mismatch"
        assert {item["field"] for item in ctrl._config_gate["mismatches"]} == {
            "conv_src", "sysper", "ioc", "idle", "cellv",
        }


@pytest.mark.parametrize(
    ("operation", "payload"),
    [
        ("range", {"fsr_code": 2, "offset_sel": 4}),
        ("command", "SET idle=1"),
        ("command", "POKE 0x68 0 FORCE"),
        ("command", "OCP 1000"),
    ],
)
def test_formal_config_gate_rejects_runtime_mutations_before_start(
    tmp_path: Path, operation: str, payload: object,
) -> None:
    ctrl = MeasurementController()
    ctrl.state = "running"
    ctrl.process = Mock()
    ctrl.cmd_path = tmp_path / "cmd.txt"
    ctrl._config_gate = {"state": "checking", "mismatches": []}

    with pytest.raises(RuntimeError, match="配置核对期间"):
        if operation == "range":
            assert isinstance(payload, dict)
            ctrl.send_range(payload)
        else:
            assert isinstance(payload, str)
            ctrl.send_command(payload)

    assert not ctrl.cmd_path.exists()


def test_formal_config_gate_allows_read_only_diagnostics(tmp_path: Path) -> None:
    ctrl = MeasurementController()
    ctrl.state = "running"
    ctrl.process = Mock()
    ctrl.cmd_path = tmp_path / "cmd.txt"
    ctrl._config_gate = {"state": "checking", "mismatches": []}

    for line in ("GET", "STATUS", "PEEK 0x23"):
        ctrl.send_command(line)

    assert ctrl.cmd_path.read_text(encoding="utf-8") == (
        "GET\nSTATUS\nPEEK 0x23\n"
    )


def test_stop_during_config_gate_wakes_waiter_without_completion_callback() -> None:
    ctrl = MeasurementController()
    ctrl.state = "running"
    ctrl.process = Mock()
    ctrl.process.poll.return_value = None
    ctrl._config_gate = {"state": "checking", "mismatches": []}
    completed: list[dict[str, object]] = []
    ctrl.on_complete = completed.append

    with patch("pa_host.gui_server.threading.Thread") as thread_cls:
        ctrl.stop()

    assert ctrl._config_gate_event.wait(0)
    assert ctrl._config_gate["state"] == "aborted"
    assert ctrl.user_stop_requested is True
    thread_cls.assert_called_once_with(
        target=ctrl._terminate_if_running,
        args=(ctrl.process, 0.0), daemon=True,
    )

    ctrl.process.poll.return_value = 3
    ctrl.process.wait.return_value = 3
    log_handle = Mock()
    ctrl._watch(log_handle)
    assert ctrl.state == "idle"
    assert completed == []
    log_handle.close.assert_called_once_with()


def test_process_exit_during_config_gate_wakes_waiter_without_callback() -> None:
    ctrl = MeasurementController()
    ctrl.state = "running"
    ctrl.process = Mock()
    ctrl.process.poll.return_value = 1
    ctrl.process.wait.return_value = 1
    ctrl._config_gate = {"state": "checking", "mismatches": []}
    completed: list[dict[str, object]] = []
    ctrl.on_complete = completed.append

    log_handle = Mock()
    ctrl._watch(log_handle)

    assert ctrl._config_gate_event.wait(0)
    assert ctrl._config_gate["state"] == "process_exit"
    assert ctrl.state == "error"
    assert completed == []
    log_handle.close.assert_called_once_with()


def test_initial_gate_get_write_failure_still_starts_cleanup_watcher(
    tmp_path: Path,
) -> None:
    ctrl = MeasurementController()
    diagnostics = DiagnosticStore(tmp_path / "logs")
    process = Mock()
    process.poll.return_value = None
    terminator = Mock()
    watcher = Mock()
    real_open = Path.open

    def fail_command_append(path: Path, mode: str = "r", *args, **kwargs):
        if path.name == "cmd.txt" and "a" in mode:
            raise OSError("injected command write failure")
        return real_open(path, mode, *args, **kwargs)

    with (
        patch("pa_host.gui_server.RUNS_DIR", tmp_path),
        patch("pa_host.gui_server.DIAGNOSTICS", diagnostics),
        patch("pa_host.gui_server._require_jlink_target"),
        patch("pa_host.gui_server.subprocess.Popen", return_value=process) as popen,
        patch("pa_host.gui_server.Path.open", autospec=True,
              side_effect=fail_command_append),
        patch("pa_host.gui_server.threading.Thread",
              side_effect=[terminator, watcher]) as thread_cls,
        pytest.raises(RuntimeError, match="无法下发硬件配置回读命令") as exc_info,
    ):
        ctrl.start_verified()

    assert ctrl._config_gate_event.wait(0)
    assert ctrl._config_gate["state"] == "io_error"
    assert exc_info.value.diagnostic_id == ctrl._config_gate["diagnostic_id"]
    event = diagnostics.snapshot()["events"][-1]
    assert event["event"] == "measurement.config_gate_failed"
    assert event["event_id"] == exc_info.value.diagnostic_id
    assert event["context"]["gate_state"] == "io_error"
    assert thread_cls.call_args_list[0].kwargs == {
        "target": ctrl._terminate_if_running,
        "args": (process, 0.0),
        "daemon": True,
    }
    assert thread_cls.call_args_list[1].kwargs["target"] == ctrl._watch
    terminator.start.assert_called_once_with()
    watcher.start.assert_called_once_with()
    popen.call_args.kwargs["stdout"].close()


def test_tagged_gate_get_retries_before_legacy_probe(tmp_path: Path) -> None:
    ctrl = MeasurementController()
    ctrl.state = "running"
    ctrl.cmd_path = tmp_path / "cmd.txt"
    ctrl.cmd_path.write_text(
        "SET fsr=1\nGET req=retry1\n", encoding="utf-8",
    )
    ctrl._config_gate = {
        "state": "checking", "request_id": "retry1", "started_at": 100.0,
        "last_tagged_get_at": 100.0, "tagged_get_attempts": 1,
        "afe_command": "SET fsr=1", "afe_command_sent": True,
        "legacy_fallback_sent": False, "mismatches": [],
    }

    with patch("pa_host.gui_server.time.time", return_value=100.5):
        ctrl._maybe_retry_tagged_gate_get_locked()
    assert ctrl.cmd_path.read_text(encoding="utf-8") == (
        "SET fsr=1\nGET req=retry1\n"
    )

    with patch("pa_host.gui_server.time.time", return_value=100.8):
        ctrl._maybe_retry_tagged_gate_get_locked()
    assert ctrl._config_gate["tagged_get_attempts"] == 2
    assert ctrl.cmd_path.read_text(encoding="utf-8") == (
        "SET fsr=1\nGET req=retry1\nGET req=retry1\n"
    )

    with patch("pa_host.gui_server.time.time", return_value=106.1):
        ctrl._maybe_retry_tagged_gate_get_locked()
        ctrl._maybe_send_legacy_gate_get_locked()
    assert ctrl.cmd_path.read_text(encoding="utf-8") == (
        "SET fsr=1\nGET req=retry1\nGET req=retry1\nGET\n"
    )
    assert ctrl._config_gate["legacy_fallback_sent"] is True


def test_formal_gate_never_sends_meas_before_physical_afe_confirmation(
    tmp_path: Path,
) -> None:
    ctrl = MeasurementController()
    ctrl.state = "running"
    ctrl.cmd_path = tmp_path / "cmd.txt"
    ctrl.audit_path = tmp_path / "audit.jsonl"
    ctrl.audit_path.write_text("", encoding="utf-8")
    expected = SettingsController.runtime_afe_contract({})
    measurement = SettingsController.runtime_measurement_contract({})
    ctrl._config_gate = {
        "state": "checking", "expected": expected, "request_id": "staged1",
        "afe_command": "SET fsr=1 e=400", "afe_command_sent": False,
        "link_ready": False,
        "measurement_expected": measurement,
        "measurement_command": "MEAS staged staged1",
        "measurement_sent": False, "measurement_confirmed": False,
        "measurement_actual": {}, "legacy_fallback_sent": False,
        "last_tagged_get_at": 0.0, "tagged_get_attempts": 0,
        "mismatches": [],
    }

    ctrl._send_runtime_gate_commands_locked()

    assert ctrl.cmd_path.read_text(encoding="utf-8") == "GET req=staged1\n"
    assert ctrl._config_gate["measurement_sent"] is False

    ctrl.audit_path.write_text("".join(
        json.dumps(event) + "\n"
        for event in _cfg_gate_events(expected, request_id="staged1")
    ), encoding="utf-8")
    ctrl._audit_events()

    assert ctrl.cmd_path.read_text(encoding="utf-8") == (
        "GET req=staged1\nSET fsr=1 e=400\n"
    )
    assert ctrl._config_gate["link_ready"] is True
    assert ctrl._config_gate["measurement_sent"] is False

    last_get = float(ctrl._config_gate["last_tagged_get_at"])
    with patch("pa_host.gui_server.time.time", return_value=last_get + 0.8):
        ctrl._maybe_retry_tagged_gate_get_locked()
    assert ctrl.cmd_path.read_text(encoding="utf-8") == (
        "GET req=staged1\nSET fsr=1 e=400\nGET req=staged1\n"
    )

    with ctrl.audit_path.open("a", encoding="utf-8") as handle:
        handle.write("".join(
            json.dumps(event) + "\n"
            for event in _cfg_gate_events(expected, request_id="staged1")
        ))
    ctrl._audit_events()

    assert ctrl.cmd_path.read_text(encoding="utf-8") == (
        "GET req=staged1\nSET fsr=1 e=400\n"
        "GET req=staged1\nMEAS staged staged1\n"
    )
    assert ctrl._config_gate["measurement_sent"] is True
    assert ctrl._config_gate["state"] == "checking"


def test_formal_gate_ignores_inflight_probe_reply_after_afe_command(
    tmp_path: Path,
) -> None:
    ctrl = MeasurementController()
    ctrl.state = "running"
    ctrl.cmd_path = tmp_path / "cmd.txt"
    ctrl.audit_path = tmp_path / "audit.jsonl"
    ctrl.audit_path.write_text("", encoding="utf-8")
    expected = SettingsController.runtime_afe_contract({"fsr_nA": 2000})
    stale = {**expected, "fsr": 0, "off": 4, "e_mv": 400}
    ctrl._config_gate = {
        "state": "checking", "expected": expected, "request_id": "race1",
        "afe_command": "SET fsr=5 off=1 e=200", "afe_command_sent": False,
        "link_ready": False, "measurement_expected": {},
        "measurement_sent": False, "measurement_confirmed": False,
        "measurement_actual": {}, "legacy_fallback_sent": False,
        "last_tagged_get_at": 0.0, "tagged_get_attempts": 1,
        "mismatches": [],
    }

    with ctrl.audit_path.open("a", encoding="utf-8") as handle:
        handle.write("".join(
            json.dumps(event) + "\n"
            for event in _cfg_gate_events(stale, request_id="race1", epoch=2)
        ))
    ctrl._audit_events()

    assert ctrl._config_gate["link_probe_epoch"] == 2
    assert ctrl._config_gate["require_post_set_epoch"] is True
    assert ctrl.cmd_path.read_text(encoding="utf-8") == "SET fsr=5 off=1 e=200\n"

    with ctrl.audit_path.open("a", encoding="utf-8") as handle:
        handle.write("".join(
            json.dumps(event) + "\n"
            for event in _cfg_gate_events(stale, request_id="race1", epoch=2)
        ))
    ctrl._audit_events()

    assert ctrl._config_gate["state"] == "checking"
    assert ctrl._config_gate["mismatches"] == []

    with ctrl.audit_path.open("a", encoding="utf-8") as handle:
        handle.write("".join(
            json.dumps(event) + "\n"
            for event in _cfg_gate_events(expected, request_id="race1", epoch=3)
        ))
    ctrl._audit_events()

    assert ctrl._config_gate["state"] == "matched"
    assert ctrl.cmd_path.read_text(encoding="utf-8") == (
        "SET fsr=5 off=1 e=200\nSTART\n"
    )


def test_formal_gate_retries_transient_afe_status_before_sending_meas(
    tmp_path: Path,
) -> None:
    ctrl = MeasurementController()
    ctrl.state = "running"
    ctrl.cmd_path = tmp_path / "cmd.txt"
    ctrl.audit_path = tmp_path / "audit.jsonl"
    expected = SettingsController.runtime_afe_contract({})
    measurement = SettingsController.runtime_measurement_contract({})
    ctrl._config_gate = {
        "state": "checking", "expected": expected, "request_id": "settle1",
        "afe_command": "SET fsr=1", "afe_command_sent": False,
        "link_ready": True,
        "measurement_expected": measurement,
        "measurement_command": "MEAS staged settle1",
        "measurement_sent": False, "measurement_confirmed": False,
        "measurement_actual": {}, "legacy_fallback_sent": False,
        "exact_response_seen": False, "started_at": 100.0,
        "last_tagged_get_at": 0.0, "tagged_get_attempts": 0,
        "mismatches": [],
    }
    with patch("pa_host.gui_server.time.time", return_value=100.0):
        ctrl._send_runtime_gate_commands_locked()
    ctrl.audit_path.write_text("".join(
        json.dumps(event) + "\n"
        for event in _cfg_gate_events(
            expected, request_id="settle1", invalid_cfg=1,
        )
    ), encoding="utf-8")

    ctrl._audit_events()

    assert ctrl._config_gate["state"] == "checking"
    assert ctrl._config_gate["phase"] == "waiting_for_clean_afe"
    assert ctrl._config_gate["measurement_sent"] is False
    assert "MEAS" not in ctrl.cmd_path.read_text(encoding="utf-8")

    with patch("pa_host.gui_server.time.time", return_value=100.8):
        ctrl._maybe_retry_tagged_gate_get_locked()
    assert ctrl.cmd_path.read_text(encoding="utf-8").count("SET ") == 1
    assert ctrl.cmd_path.read_text(encoding="utf-8").count("GET req=settle1") == 2

    with ctrl.audit_path.open("a", encoding="utf-8") as handle:
        handle.write("".join(
            json.dumps(event) + "\n"
            for event in _cfg_gate_events(expected, request_id="settle1")
        ))
    ctrl._audit_events()

    assert ctrl._config_gate["measurement_sent"] is True
    assert ctrl.cmd_path.read_text(encoding="utf-8").endswith(
        "MEAS staged settle1\n"
    )


def test_start_write_failure_fails_gate_without_escaping_audit_poll(
    tmp_path: Path,
) -> None:
    ctrl = MeasurementController()
    ctrl.state = "running"
    ctrl.process = Mock()
    ctrl.process.poll.return_value = None
    ctrl.cmd_path = tmp_path / "cmd.txt"
    ctrl.cmd_path.mkdir()
    ctrl.audit_path = tmp_path / "audit.jsonl"
    expected = SettingsController.runtime_afe_contract({})
    ctrl._config_gate = {
        "state": "checking", "expected": expected, "request_id": "gate-start",
        "legacy_fallback_sent": False, "mismatches": [],
    }
    ctrl.audit_path.write_text("".join(
        json.dumps(event) + "\n"
        for event in _cfg_gate_events(expected, request_id="gate-start")
    ), encoding="utf-8")

    with patch("pa_host.gui_server.threading.Thread") as thread_cls:
        ctrl._audit_events()

    assert ctrl._config_gate_event.wait(0)
    assert ctrl._config_gate["state"] == "io_error"
    assert "hardware_config" not in ctrl.metadata
    thread_cls.assert_called_once_with(
        target=ctrl._terminate_if_running,
        args=(ctrl.process, 0.0), daemon=True,
    )


def test_legacy_get_write_failure_fails_gate(tmp_path: Path) -> None:
    ctrl = MeasurementController()
    ctrl.state = "running"
    ctrl.process = Mock()
    ctrl.process.poll.return_value = None
    ctrl.cmd_path = tmp_path / "cmd.txt"
    ctrl.cmd_path.mkdir()
    ctrl._config_gate = {
        "state": "checking", "started_at": 0.0,
        "legacy_fallback_sent": False, "mismatches": [],
    }

    with patch("pa_host.gui_server.threading.Thread") as thread_cls:
        ctrl._maybe_send_legacy_gate_get_locked()

    assert ctrl._config_gate_event.wait(0)
    assert ctrl._config_gate["state"] == "io_error"
    assert ctrl._config_gate["legacy_fallback_sent"] is False
    thread_cls.assert_called_once_with(
        target=ctrl._terminate_if_running,
        args=(ctrl.process, 0.0), daemon=True,
    )


def test_legacy_fallback_rejects_a_new_no_req_snapshot(tmp_path: Path) -> None:
    ctrl = MeasurementController()
    ctrl.state = "running"
    ctrl.process = Mock()
    ctrl.process.poll.return_value = None
    ctrl.cmd_path = tmp_path / "cmd.txt"
    ctrl.audit_path = tmp_path / "audit.jsonl"
    expected = SettingsController.runtime_afe_contract({})
    ctrl._config_gate = {
        "state": "checking", "expected": expected, "request_id": "fresh",
        "started_at": 0.0, "legacy_fallback_sent": False, "mismatches": [],
    }
    stale = _legacy_cfg_gate_events(expected, epoch=5)
    ctrl.audit_path.write_text(
        "".join(json.dumps(event) + "\n" for event in stale), encoding="utf-8",
    )
    ctrl._audit_events()
    assert ctrl._config_gate["state"] == "checking"

    ctrl._maybe_send_legacy_gate_get_locked()
    ctrl._advance_config_gate_locked()
    assert ctrl.cmd_path.read_text(encoding="utf-8") == "GET\n"
    assert ctrl._config_gate["state"] == "checking"

    fresh = _legacy_cfg_gate_events(expected, epoch=5)
    with ctrl.audit_path.open("a", encoding="utf-8") as handle:
        handle.write("".join(json.dumps(event) + "\n" for event in fresh))
    with patch("pa_host.gui_server.threading.Thread") as thread_cls:
        ctrl._audit_events()
    assert ctrl.cmd_path.read_text(encoding="utf-8") == "GET\n"
    assert ctrl._config_gate["state"] == "unsupported_firmware"
    assert ctrl._config_gate["verification_level"] == "reported_config"
    assert ctrl._config_gate_event.wait(0)
    assert ctrl._config_gate["message"] == (
        "固件不支持完整物理配置核验，请重新应用条件并烧录硬件"
    )
    thread_cls.assert_called_once_with(
        target=ctrl._terminate_if_running,
        args=(ctrl.process, 0.0), daemon=True,
    )


def test_exact_gate_response_without_verify_ok_is_unsupported(
    tmp_path: Path,
) -> None:
    ctrl = MeasurementController()
    ctrl.state = "running"
    ctrl.process = Mock()
    ctrl.process.poll.return_value = None
    ctrl.cmd_path = tmp_path / "cmd.txt"
    ctrl.audit_path = tmp_path / "audit.jsonl"
    expected = SettingsController.runtime_afe_contract({})
    ctrl._config_gate = {
        "state": "checking", "expected": expected, "request_id": "exact-old",
        "legacy_fallback_sent": False, "mismatches": [],
    }
    events = _cfg_gate_events(expected, request_id="exact-old", epoch=8)
    events[-1].pop("verify_ok")
    ctrl.audit_path.write_text(
        "".join(json.dumps(event) + "\n" for event in events), encoding="utf-8",
    )

    with patch("pa_host.gui_server.threading.Thread"):
        ctrl._audit_events()

    assert not ctrl.cmd_path.exists()
    assert ctrl._config_gate["state"] == "unsupported_firmware"
    assert ctrl._config_gate["verification_level"] == "reported_config"
    assert ctrl._config_gate_event.wait(0)


def test_exact_gate_response_beats_newer_legacy_candidate(tmp_path: Path) -> None:
    ctrl = MeasurementController()
    ctrl.state = "running"
    ctrl.process = Mock()
    ctrl.process.poll.return_value = None
    ctrl.cmd_path = tmp_path / "cmd.txt"
    ctrl.audit_path = tmp_path / "audit.jsonl"
    ctrl.audit_path.write_text("", encoding="utf-8")
    expected = SettingsController.runtime_afe_contract({})
    ctrl._config_gate = {
        "state": "checking", "expected": expected, "request_id": "exact",
        "started_at": 0.0, "legacy_fallback_sent": False, "mismatches": [],
    }
    ctrl._maybe_send_legacy_gate_get_locked()
    events = [
        *_legacy_cfg_gate_events(expected, epoch=12, idle=1),
        *_cfg_gate_events(expected, request_id="exact", epoch=7),
    ]
    ctrl.audit_path.write_text(
        "".join(json.dumps(event) + "\n" for event in events), encoding="utf-8",
    )

    ctrl._audit_events()

    assert ctrl._config_gate["state"] == "matched"
    assert ctrl._config_gate["ep"] == 7
    assert ctrl._config_gate["verification_level"] == "physical_registers"
    assert ctrl.cmd_path.read_text(encoding="utf-8") == "GET\nSTART\n"


def test_formal_gate_blocks_physical_verify_failure(tmp_path: Path) -> None:
    ctrl = MeasurementController()
    ctrl.state = "running"
    ctrl.process = Mock()
    ctrl.process.poll.return_value = None
    ctrl.cmd_path = tmp_path / "cmd.txt"
    ctrl.audit_path = tmp_path / "audit.jsonl"
    expected = SettingsController.runtime_afe_contract({})
    ctrl._config_gate = {
        "state": "checking", "expected": expected, "request_id": "bad-verify",
        "legacy_fallback_sent": False, "mismatches": [],
    }
    events = [
        {"kind": "CFG_FAULT", "ep": 7, "req": "bad-verify",
         "cause": "verify_mismatch"},
        *_cfg_gate_events(
            expected, request_id="bad-verify", epoch=7, verify_ok=0,
        ),
    ]
    ctrl.audit_path.write_text(
        "".join(json.dumps(event) + "\n" for event in events), encoding="utf-8",
    )

    with patch("pa_host.gui_server.threading.Thread"):
        ctrl._audit_events()

    assert ctrl._config_gate_event.wait(0)
    assert ctrl._config_gate["state"] == "mismatch"
    assert not ctrl.cmd_path.exists()
    assert {item["field"] for item in ctrl._config_gate["mismatches"]} >= {
        "verify_ok", "config_integrity",
    }


def test_debug_platform_monitor_uses_live_period_and_never_stops() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ctrl = MeasurementController()
        ctrl.settings = SettingsController.validate({"adaptive_stop": False})
        ctrl.metadata = {"debug": True}
        ctrl.state = "running"
        ctrl.process = Mock()
        ctrl.cmd_path = Path(tmp) / "cmd.txt"
        ctrl._cfg_live = {"period_ms": 1882}
        t = np.arange(0.0, 39.9, 0.1)
        traces = iter([
            {"time_s": t[:350].tolist(), "current_nA": np.full(350, 4.0).tolist(),
             "valid": np.ones(350, dtype=bool).tolist()},
            {"time_s": t.tolist(), "current_nA": np.full(len(t), 4.0).tolist(),
             "valid": np.ones(len(t), dtype=bool).tolist()},
        ])
        with patch.object(ctrl, "_data", side_effect=lambda: next(traces)), \
                patch("pa_host.gui_server.evaluate_platform", wraps=__import__(
                    "pa_host.it", fromlist=["evaluate_platform"]
                ).evaluate_platform) as evaluate:
            ctrl._maybe_auto_stop()
            ctrl._maybe_auto_stop()

        assert ctrl._plateau_evaluation is not None
        assert ctrl.auto_stop_requested is False
        assert not ctrl.cmd_path.exists()
        assert evaluate.call_args.kwargs["expected_sample_rate_hz"] == pytest.approx(
            1000 / 1882
        )


def test_plateau_controller_persists_validated_settings() -> None:
    with tempfile.TemporaryDirectory() as tmp, patch(
        "pa_host.gui_server.PLATEAU_SETTINGS_PATH", Path(tmp) / "plateau.json"
    ):
        controller = PlateauController()
        result = controller.apply({
            **PlateauConfig().to_dict(),
            "segment_duration_s": 4,
            "segment_count": 8,
            "required_consecutive_windows": 3,
        })
        restored = PlateauController().snapshot()

        assert result["window_duration_s"] == 32
        assert restored["settings"]["required_consecutive_windows"] == 3
        assert restored["window_duration_s"] == 32


def test_plateau_controller_write_failure_keeps_previous_settings() -> None:
    with tempfile.TemporaryDirectory() as tmp, patch(
        "pa_host.gui_server.PLATEAU_SETTINGS_PATH", Path(tmp) / "plateau.json"
    ) as settings_path:
        controller = PlateauController()
        original = controller.settings
        settings_path.write_text(
            json.dumps({"settings": original.to_dict()}), encoding="utf-8",
        )
        changed = {
            **original.to_dict(),
            "segment_duration_s": 4,
            "segment_count": 8,
        }

        with patch("pa_host.gui_server.os.replace", side_effect=OSError("disk full")), \
                pytest.raises(OSError, match="disk full"):
            controller.apply(changed)

        assert controller.settings == original
        assert json.loads(settings_path.read_text(encoding="utf-8")) == {
            "settings": original.to_dict(),
        }
        assert list(Path(tmp).glob(".plateau.json.*.tmp")) == []


def test_plateau_apply_rejects_formal_run_before_mutating_settings() -> None:
    with tempfile.TemporaryDirectory() as tmp, patch(
        "pa_host.gui_server.PLATEAU_SETTINGS_PATH", Path(tmp) / "plateau.json"
    ) as settings_path:
        app = AppState()
        app.measurement.state = "running"
        app.measurement.metadata = {"debug": False}
        app.measurement._plateau_last_segment = 6
        payload = {
            **PlateauConfig().to_dict(),
            "segment_duration_s": 4,
            "segment_count": 8,
        }
        handler = RequestHandler.__new__(RequestHandler)
        handler.path = "/api/plateau/apply"
        handler._body = Mock(return_value=payload)
        handler._send_json = Mock()

        with patch("pa_host.gui_server.APP", app):
            handler.do_POST()

        assert app.plateau.settings == PlateauConfig()
        assert app.schedule.plateau_config == PlateauConfig()
        assert app.measurement.plateau_config == PlateauConfig()
        assert app.measurement._plateau_last_segment == 6
        assert not settings_path.exists()
        response, status = handler._send_json.call_args.args
        assert "正式测量" in response["error"]
        assert status == 409


def test_plateau_apply_updates_running_debug_and_resets_monitor() -> None:
    with tempfile.TemporaryDirectory() as tmp, patch(
        "pa_host.gui_server.PLATEAU_SETTINGS_PATH", Path(tmp) / "plateau.json"
    ) as settings_path:
        app = AppState()
        app.measurement.state = "running"
        app.measurement.metadata = {"debug": True}
        app.measurement._plateau_last_segment = 6
        app.measurement._plateau_consecutive_passes = 2
        app.measurement._plateau_evaluation = {"stable": True}
        app.measurement._plateau_progress = {"elapsed_s": 30.0}
        payload = {
            **PlateauConfig().to_dict(),
            "segment_duration_s": 4,
            "segment_count": 8,
        }
        handler = RequestHandler.__new__(RequestHandler)
        handler.path = "/api/plateau/apply"
        handler._body = Mock(return_value=payload)
        handler._send_json = Mock()

        with patch("pa_host.gui_server.APP", app):
            handler.do_POST()

        expected = PlateauConfig.validate(payload)
        assert app.plateau.settings == expected
        assert app.schedule.plateau_config == expected
        assert app.measurement.plateau_config == expected
        assert app.measurement._plateau_last_segment == 0
        assert app.measurement._plateau_consecutive_passes == 0
        assert app.measurement._plateau_evaluation is None
        assert app.measurement._plateau_progress == {}
        assert settings_path.exists()
        handler._send_json.assert_called_once()


@pytest.mark.parametrize("stop_flag", ["user_stop_requested", "auto_stop_requested"])
def test_plateau_apply_rejects_stopping_debug_before_persisting(
    stop_flag: str,
) -> None:
    with tempfile.TemporaryDirectory() as tmp, patch(
        "pa_host.gui_server.PLATEAU_SETTINGS_PATH", Path(tmp) / "plateau.json"
    ) as settings_path:
        app = AppState()
        app.measurement.state = "running"
        app.measurement.metadata = {"debug": True}
        setattr(app.measurement, stop_flag, True)
        app.measurement._plateau_last_segment = 6
        payload = {
            **PlateauConfig().to_dict(),
            "segment_duration_s": 4,
            "segment_count": 8,
        }
        handler = RequestHandler.__new__(RequestHandler)
        handler.path = "/api/plateau/apply"
        handler._body = Mock(return_value=payload)
        handler._send_json = Mock()

        with patch("pa_host.gui_server.APP", app):
            handler.do_POST()

        assert app.plateau.settings == PlateauConfig()
        assert app.schedule.plateau_config == PlateauConfig()
        assert app.measurement.plateau_config == PlateauConfig()
        assert app.measurement._plateau_last_segment == 6
        assert not settings_path.exists()
        response, status = handler._send_json.call_args.args
        assert response["error"] == "测量正在停止，不能修改自动停止参数"
        assert status == 409


def test_plateau_apply_rejects_active_schedule_before_persisting() -> None:
    with tempfile.TemporaryDirectory() as tmp, patch(
        "pa_host.gui_server.PLATEAU_SETTINGS_PATH", Path(tmp) / "plateau.json"
    ) as settings_path:
        app = AppState()
        app.schedule.active = True
        payload = {
            **PlateauConfig().to_dict(),
            "segment_duration_s": 4,
            "segment_count": 8,
        }
        handler = RequestHandler.__new__(RequestHandler)
        handler.path = "/api/plateau/apply"
        handler._body = Mock(return_value=payload)
        handler._send_json = Mock()

        with patch("pa_host.gui_server.APP", app):
            handler.do_POST()

        assert app.plateau.settings == PlateauConfig()
        assert not settings_path.exists()
        response, status = handler._send_json.call_args.args
        assert "自动任务" in response["error"]
        assert status == 409


def test_debug_run_forces_it_settings_when_formal_method_is_cv(
    tmp_path: Path,
) -> None:
    app = AppState()
    app.save_dir = tmp_path / "workspace"
    app._load_workspace()
    app.settings.settings = SettingsController.validate({"method": "cv"})
    with patch.object(app.schedule, "snapshot", return_value={"active": False}), \
            patch.object(app.measurement, "start", return_value={"state": "running"}) as start:
        app.start_debug_run({"note": "cv-formal-settings", "probe_only": True})

    assert start.call_args.kwargs["settings"]["method"] == "it"


def test_settings_and_schedule_reject_non_finite_values() -> None:
    with pytest.raises(ValueError, match="NaN"):
        SettingsController.validate({"potential_v": float("nan")})
    app = AppState()
    with pytest.raises(ValueError, match="间隔"):
        app.schedule.start({"interval_minutes": float("nan")})
    with pytest.raises(ValueError, match="整数"):
        SettingsController.validate({"cv_cycles": 1.5})


def test_history_curve_select_all_keeps_total_point_budget_bounded(tmp_path: Path) -> None:
    app = AppState()
    app.save_dir = tmp_path / "history-curves"
    app._load_workspace()
    app.records = [
        {
            "run_id": f"run-{index}",
            "state": "completed",
            "data_path": f"curve-{index}.csv",
        }
        for index in range(20)
    ]
    def curve(record: dict[str, object], *, maximum_points: int = 3000):
        del record
        maximum_points_seen.append(maximum_points)
        return {"method": "it", "time_s": [0], "current_nA": [0], "valid": [True]}

    maximum_points_seen: list[int] = []
    with patch.object(app, "_curve_from_record", side_effect=curve):
        result = app.load_history_curves({
            "run_ids": [f"run-{index}" for index in range(20)],
        })

    assert len(result["curves"]) == 20
    assert maximum_points_seen == [1800] * 20
    assert sum(maximum_points_seen) == 36_000
    with pytest.raises(ValueError, match="最多叠加 80 条"):
        app.load_history_curves({"run_ids": [f"run-{index}" for index in range(81)]})
