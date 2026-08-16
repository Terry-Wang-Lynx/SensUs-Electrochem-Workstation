"""History registry and transactional workspace recovery tests."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pytest

from pa_host.gui_server import AppState
from pa_host.workspace_history import WorkspaceHistory


def _point(point_id: str, concentration: float, current: float) -> dict[str, object]:
    return {
        "point_id": point_id, "label": point_id,
        "concentration_um": concentration, "current_nA": current,
    }


def _isolated_app(root: Path) -> tuple[AppState, WorkspaceHistory]:
    registry = WorkspaceHistory(root / "history.json", root)
    app = AppState()
    app.history = registry
    app.save_dir = root / "workspace-a"
    app._load_workspace()
    return app, registry


def test_history_persists_across_registry_and_app_restart_without_absolute_paths() -> None:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        app, registry = _isolated_app(root)
        app.fit({"points": [_point("zero", 0, 10), _point("ten", 10, 30)],
                 "selected_point_ids": ["zero", "ten"]})
        entry = app.register_history({"label": "第一批次"})
        raw = (root / "history.json").read_text(encoding="utf-8")
        assert str(root) not in raw
        assert entry["label"] == "第一批次"

        restarted = WorkspaceHistory(root / "history.json", root)
        listed = restarted.list(root / "workspace-a")
        assert listed["entries"][0]["workspace_id"] == entry["workspace_id"]
        assert listed["entries"][0]["summary"]["points_count"] == 2


def test_external_unix_workspace_uses_non_absolute_filesystem_locator() -> None:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        registry = WorkspaceHistory(root / "registry" / "history.json", root / "project")
        workspace = root / "external-workspace"
        entry = registry.register(workspace, {})
        raw = (root / "registry" / "history.json").read_text(encoding="utf-8")
        expected_anchor = (
            "home" if workspace.resolve().is_relative_to(Path.home().resolve())
            else "filesystem"
        )
        assert entry["location_anchor"] == expected_anchor
        assert '"path": "/' not in raw
        assert registry.resolve(entry["workspace_id"])[1] == workspace.resolve()


def test_import_summary_reads_existing_measurements_without_marker_write() -> None:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        workspace = root / "old-data"
        workspace.mkdir()
        (workspace / "calibration-points.csv").write_text(
            "point_id,concentration_um,current_nA\np1,1,2\n",
            encoding="utf-8",
        )
        (workspace / "calibration-selection.json").write_text(
            '{"selected_point_ids":["p1"]}', encoding="utf-8"
        )
        (workspace / "measurement-index.csv").write_text(
            "finished_at,run_id,sample_name,sample_role,state,steady_current_nA\n"
            "10,r1,旧样品,calibration,completed,2\n",
            encoding="utf-8",
        )
        (workspace / "calibration-model.json").write_text(
            '{"r2":0.99}', encoding="utf-8"
        )
        registry = WorkspaceHistory(root / "registry" / "history.json", root)

        entry = registry.register(
            workspace, WorkspaceHistory.summarize(workspace), create_marker=False
        )

        assert not (workspace / ".sensus-workspace.json").exists()
        assert entry["summary"]["points_count"] == 1
        assert entry["summary"]["completed_count"] == 1
        assert entry["summary"]["calibration_count"] == 1
        assert entry["summary"]["has_model"] is True


def test_duplicate_registration_and_favorite_remove_preserve_source_files() -> None:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        app, registry = _isolated_app(root)
        source = app.save_dir / "measurement.csv"
        source.write_text("time_s,current_nA\n0,1\n", encoding="utf-8")
        first = app.register_history()
        second = app.register_history()
        assert first["workspace_id"] == second["workspace_id"]
        assert len(registry.list()["entries"]) == 1
        registry.favorite(first["workspace_id"], True)
        assert registry.list()["entries"][0]["favorite"] is True
        registry.remove(first["workspace_id"])
        assert source.exists()
        assert registry.list()["entries"] == []


def test_renamed_workspace_is_relocated_by_stable_marker() -> None:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        app, registry = _isolated_app(root)
        entry = app.register_history()
        renamed = root / "workspace-renamed"
        app.save_dir.rename(renamed)
        listed = registry.list()
        assert listed["entries"][0]["status"] == "available"
        assert listed["entries"][0]["location"] == "workspace-renamed"
        registry.resolve(entry["workspace_id"])


def test_missing_and_corrupt_workspaces_are_visible_but_cannot_open() -> None:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        app, registry = _isolated_app(root)
        missing = app.register_history({"label": "将丢失"})
        app2, registry2 = _isolated_app(root / "other")
        corrupt = app2.register_history({"label": "将损坏"})
        (app2.save_dir / "calibration-model.json").write_text("{broken", encoding="utf-8")
        listed = registry2.list()
        assert listed["entries"][0]["status"] == "corrupt"
        with pytest.raises(ValueError, match="无法读取|损坏|无效"):
            registry2.resolve(corrupt["workspace_id"])
        (app.save_dir / ".sensus-workspace.json").unlink()
        app.save_dir.rename(root / "removed")
        # The marker moved with the directory, while the registry still points
        # to the old path and parent no longer contains it.
        (root / "removed").rename(root / "outside")
        assert registry.list()["entries"][0]["status"] == "missing"
        with pytest.raises(FileNotFoundError):
            registry.resolve(missing["workspace_id"])


def test_open_history_restores_full_state_and_marks_hardware_parameters_unapplied() -> None:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        app, registry = _isolated_app(root)
        app.fit({"points": [_point("zero", 0, 10), _point("ten", 10, 30)],
                 "selected_point_ids": ["zero", "ten"]})
        app._append_record({"finished_at": "20", "run_id": "test-1", "sample_name": "样品",
                            "sample_role": "test", "state": "completed",
                            "steady_current_nA": "22", "known_concentration_um": "5"})
        app.drift = {**app._empty_drift(), "enabled": True, "bias_nA": 1.5,
                     "record_ids": ["stable-1"]}
        app._save_drift()
        app._persist_workspace_runtime()
        entry = app.register_history()

        restored = AppState()
        restored.history = WorkspaceHistory(root / "history.json", root)
        result = restored.open_history({"workspace_id": entry["workspace_id"]})
        assert [p["point_id"] for p in result["calibration"]["points"]] == ["zero", "ten"]
        assert result["calibration"]["selected_point_ids"] == ["zero", "ten"]
        assert restored.records[0]["run_id"] == "test-1"
        assert restored.drift["bias_nA"] == 1.5
        assert restored.settings.snapshot()["applied"] is False
        assert restored.measurement.settings["method"] == "it"


def test_open_history_rejects_busy_or_unsaved_switch_and_preserves_current_state() -> None:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        app, registry = _isolated_app(root)
        first = app.register_history({"label": "A"})
        other = root / "workspace-b"
        app.save_dir = other
        app._load_workspace()
        second = app.register_history({"label": "B"})
        app.save_dir = root / "workspace-a"
        app._load_workspace()
        with app.measurement.lock:
            app.measurement.state = "running"
        with pytest.raises(RuntimeError, match="测量"):
            app.open_history({"workspace_id": second["workspace_id"]})
        with app.measurement.lock:
            app.measurement.state = "idle"
        with pytest.raises(RuntimeError, match="未保存"):
            app.open_history({"workspace_id": second["workspace_id"], "unsaved_changes": True})
        assert app.save_dir == root / "workspace-a"


def test_open_history_atomic_failure_rolls_back_workspace_memory_and_workflow_file() -> None:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        app, registry = _isolated_app(root)
        entry = app.register_history({"label": "目标"})
        before_dir = app.save_dir
        original_atomic = app._atomic_json_file

        def fail_target(path: Path, payload: dict[str, object]) -> None:
            if path.name == "gui_workflow.json":
                raise OSError("模拟原子写失败")
            original_atomic(path, payload)

        with patch.object(app, "_atomic_json_file", side_effect=fail_target), pytest.raises(OSError):
            app.open_history({"workspace_id": entry["workspace_id"]})
        assert app.save_dir == before_dir
        assert app.workflow_snapshot()["save_dir"] == str(before_dir)


def test_concurrent_open_requests_serialize_and_leave_a_complete_workspace() -> None:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        app, registry = _isolated_app(root)
        a = app.register_history({"label": "A"})
        app.save_dir = root / "workspace-b"
        app._load_workspace()
        b = app.register_history({"label": "B"})
        errors: list[Exception] = []

        def open_entry(entry: dict[str, object]) -> None:
            try:
                app.open_history({"workspace_id": entry["workspace_id"]})
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=open_entry, args=(entry,)) for entry in (a, b)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2)
        assert errors == []
        assert app.save_dir.resolve() in {(root / "workspace-a").resolve(), (root / "workspace-b").resolve()}
        assert app.workflow_snapshot()["save_dir"] == str(app.save_dir)


def test_new_calibration_batch_uses_child_directory_and_preserves_root_history() -> None:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        app, registry = _isolated_app(root)
        app._append_record({
            "finished_at": "20", "run_id": "root-run", "sample_name": "根目录样品",
            "sample_role": "calibration", "state": "completed",
            "steady_current_nA": "22", "known_concentration_um": "5",
        })
        root_dir = app.save_dir

        first = app.reset_calibration()
        first_dir = app.save_dir
        assert first_dir.parent.resolve() == root_dir.resolve()
        assert first_dir.resolve() != root_dir.resolve()
        assert first["workspace_root"] == str(root_dir.resolve())
        assert first["batch_id"]
        assert first["batch_label"].startswith("批次 ")
        assert (root_dir / "measurement-index.csv").exists()
        assert not (first_dir / "measurement-index.csv").exists()

        second = app.reset_calibration()
        second_dir = app.save_dir
        assert second_dir.parent.resolve() == root_dir.resolve()
        assert second_dir.resolve() != first_dir.resolve()
        assert first_dir.exists()

        history = app.history_snapshot()
        root_entry = next(
            entry for entry in history["entries"]
            if entry["workspace_id"] == history["active_workspace_id"]
        )
        batch_entries = [
            entry for entry in history["batches"]
            if entry["workspace_root_id"] == root_entry["workspace_id"]
        ]
        assert root_entry["kind"] == "workspace"
        assert len(batch_entries) == 2

        restored = AppState()
        restored.history = WorkspaceHistory(root / "history.json", root)
        result = restored.open_history({"workspace_id": root_entry["workspace_id"]})
        assert restored.save_dir.resolve() == root_dir.resolve()
        assert restored.records[0]["run_id"] == "root-run"
        assert result["workflow"]["batch_id"] == ""

        restored.open_history({"workspace_id": first["batch_id"]})
        assert restored.save_dir.resolve() == first_dir.resolve()
        assert restored.workspace_root.resolve() == root_dir.resolve()


def test_configured_workspace_creates_a_named_child_batch_and_history_curves_stay_scoped() -> None:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        app, _ = _isolated_app(root)
        workspace = root / "实验工作区"
        app.configure_workflow({"save_dir": str(workspace), "batch_name": "第一批"})
        assert app.workspace_root == workspace.resolve()
        assert app.save_dir == (workspace / "第一批").resolve()
        assert app.history.marker_info(app.save_dir)["kind"] == "batch"

        trace = app.save_dir / "trace.csv"
        trace.write_text(
            "time_s,current_nA,valid\n0,1,1\n1,2,1\n2,3,1\n",
            encoding="utf-8",
        )
        app._append_record({
            "finished_at": 10, "run_id": "run-1", "sample_name": "样品 1",
            "sample_role": "calibration", "state": "completed",
            "steady_current_nA": 3, "data_path": str(trace),
            "measurement_settings": {"method": "it"},
        })
        assert app.history_curves_snapshot()["curves"][0]["run_id"] == "run-1"
        loaded = app.load_history_curves({"run_ids": ["run-1"]})["curves"]
        assert loaded[0]["time_s"] == [0.0, 1.0, 2.0]
        with pytest.raises(ValueError, match="当前批次"):
            app.load_history_curves({"run_ids": ["other-batch"]})
