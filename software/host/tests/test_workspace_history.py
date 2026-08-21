"""History registry and transactional workspace recovery tests."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path, PureWindowsPath
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pytest

from pa_host.gui_server import AppState
from pa_host.workspace_history import WorkspaceHistory


class _FakeWindowsPath(PureWindowsPath):
    """在 macOS/Linux 上冒充"另一个盘上的工作区"。

    _locator() 对入参只用到 resolve() 与 relative_to():纯路径已经有 relative_to
    (跨 flavour 比较会正常抛 ValueError),只缺 resolve() —— 补上返回自身即可,
    不必真去访问 D:\\,也不必在 CI 上拉一台 Windows。
    """

    def resolve(self) -> "_FakeWindowsPath":  # pragma: no cover - 单行替身
        return self


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


def test_windows_workspace_on_another_drive_round_trips_via_absolute_anchor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 🔴 Windows 独有缺陷:D: 盘既不在家目录/应用目录之下,也无法 relative_to("/"),
    # 老实现直接抛"历史记录路径无法用相对定位保存"(接口层 400)。
    with TemporaryDirectory() as tmp, monkeypatch.context() as env:
        root = Path(tmp)
        registry = WorkspaceHistory(root / "registry" / "history.json", root / "project")
        env.setattr(os, "name", "nt")
        workspace = _FakeWindowsPath(r"D:\data\ws")

        locator = registry._locator(workspace)

        assert locator["anchor"] == "absolute"
        assert str(registry._path(locator)) == r"D:\data\ws"

        # 🔴 到这里 os.name 的伪装必须撤掉,而且不是为了让测试变松:
        #    `_read()`/`_valid_locator()`/`_safe_absolute()` 一个都不看 os.name
        #    (它们用 PurePosixPath+PureWindowsPath 两种 flavour 各解析一遍),
        #    所以下面这段断言与 os.name 无关。但 os.name 是**进程级全局量**:
        #    `pathlib.Path(x)` 会照它挑 flavour,伪装成 nt 时 Path("/var/...") 变成
        #    WindowsPath 并把分隔符改写成 "\var\..." ⇒ 在 macOS 上读真实临时文件
        #    直接 FileNotFoundError。这是伪装带来的假象,真 Windows 上 Path 本来就是
        #    WindowsPath、重新包一次是空操作。断言一条没减,只是把伪装收回到真正
        #    需要它的 `_locator()` 那两行。
        env.undo()

        # 落盘再读回:_read() 不能把绝对锚点条目当非法条目静默丢掉。
        registry.registry_path.parent.mkdir(parents=True, exist_ok=True)
        registry.registry_path.write_text(
            json.dumps({
                "version": 1,
                "entries": [{
                    "workspace_id": "d-drive", "locator": locator,
                    "label": "D 盘工作区",
                }],
            }),
            encoding="utf-8",
        )
        reloaded = registry._read()["entries"]
        assert [item["locator"] for item in reloaded] == [locator]


def test_relative_anchors_keep_priority_over_the_new_absolute_anchor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 相对锚点是"应用/家目录整体搬动后仍能解析"的唯一依靠,绝对锚点只能垫底。
    # 三个目录都只参与路径运算,不创建、不读写:注册表目录特意放在家目录之外,
    # 否则 home 锚点会先命中,registry 锚点根本轮不到。
    home = Path.home().resolve()
    project = home / "sensus-project"
    registry = WorkspaceHistory(
        Path("/sensus-registry-fixture/history.json"), project
    )

    for name in ("posix", "nt"):
        with monkeypatch.context() as env:
            env.setattr(os, "name", name)
            assert registry._locator(home / "ws-under-home") == {
                "anchor": "home", "path": "ws-under-home",
            }
            assert registry._locator(project / "ws-under-project") == {
                "anchor": "project", "path": "ws-under-project",
            }
            assert registry._locator(
                registry.registry_dir / "ws-under-registry"
            ) == {"anchor": "registry", "path": "ws-under-registry"}


def test_legacy_registry_entries_of_every_anchor_remain_readable() -> None:
    with TemporaryDirectory() as tmp:
        root = Path(tmp).resolve()
        project = root / "project"
        registry_dir = root / "registry"
        for existing in (project / "ws-p", registry_dir / "ws-r", root / "ws-f"):
            existing.mkdir(parents=True)
        legacy = {
            "project": "ws-p",
            "registry": "ws-r",
            # 家目录条目不落盘(不往用户真实家目录写东西),只验证它没被丢弃。
            "home": "sensus-legacy-home/ws-h",
        }
        # 🔴 `filesystem` 锚点**本质上是 POSIX 专有**的:它把绝对路径存成相对于 "/"
        #    的路径,而 Windows 上带盘符的路径不在 "\" 之下 —— 生产代码的写入侧本来
        #    就有 `if os.name != "nt"` 挡着,解析侧也不可能把 `\Users\...` 还原成
        #    `C:\Users\...`。所以这一段只在 POSIX 上构造与断言。
        #    (CI 的 Windows job 就是被这条测试自身的 `relative_to(Path("/"))` 拦下的:
        #     ValueError: 'C:\\...\\ws-f' is not in the subpath of '\\')
        if os.name != "nt":
            legacy["filesystem"] = (root / "ws-f").relative_to(Path("/")).as_posix()
        (registry_dir / "history.json").write_text(
            json.dumps({
                "version": 1,
                "entries": [
                    {"workspace_id": anchor, "label": anchor,
                     "locator": {"anchor": anchor, "path": path}}
                    for anchor, path in legacy.items()
                ],
            }),
            encoding="utf-8",
        )
        registry = WorkspaceHistory(registry_dir / "history.json", project)

        entries = {item["workspace_id"]: item for item in registry._read()["entries"]}
        assert set(entries) == set(legacy)
        assert registry._path(entries["project"]["locator"]) == project / "ws-p"
        assert registry._path(entries["registry"]["locator"]) == registry_dir / "ws-r"
        if os.name != "nt":
            assert registry._path(entries["filesystem"]["locator"]) == root / "ws-f"
        assert registry._path(entries["home"]["locator"]) == (
            Path.home().resolve() / "sensus-legacy-home" / "ws-h"
        )

        listed = {item["workspace_id"]: item for item in registry.list()["entries"]}
        assert set(listed) == set(legacy)
        assert listed["project"]["status"] == "available"
        if os.name != "nt":
            assert listed["filesystem"]["status"] == "available"
        assert listed["home"]["status"] == "missing"


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


def test_history_discovers_matching_batch_directories_copied_into_workspace() -> None:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        app, _ = _isolated_app(root)
        workspace = root / "workspace"
        app.configure_workflow({"save_dir": str(workspace), "batch_name": "第一批"})
        root_id = app.history.marker_info(workspace)["workspace_id"]
        copied = workspace / "外部导入批次"
        app.history._ensure_marker(
            copied,
            kind="batch",
            workspace_root_id=root_id,
            label="外部导入批次",
        )

        history = app.history_snapshot()

        assert any(
            entry["workspace_id"] == root_id
            for entry in history["workspaces"]
        )
        discovered = next(
            entry for entry in history["current_batches"]
            if entry["label"] == "外部导入批次"
        )
        assert discovered["status"] == "available"
        assert discovered["workspace_root_id"] == root_id


# ==========================================================================
# 旧编码(中文 Windows 的 cp936/GBK)落盘的历史记录必须还能读
#
# 现场(2026-08-21,同事的中文 Windows):写盘当时一切正常,**重启后**加载带中文的
# 批次历史报 `'utf-8' codec can't decode bytes in position 320-321`。
# "重启才坏"是因为同一会话里 records 一直在内存里、文件从没被读回来过;重启后才
# 第一次真的去读 —— 那些文件是**旧版本**写的(漏写 encoding,中文按 cp936 落盘)。
# 写入端现已全部锁 UTF-8,但旧文件还在用户磁盘上、量很大 ⇒ 只能在读取端修,
# 且不能要求用户转换文件。
# ==========================================================================

_LEGACY_INDEX_CSV = (
    "finished_at,run_id,sample_name,sample_role,known_concentration_um,"
    "steady_current_nA,state,data_path\n"
    "1787000000,it_1,左旋多巴 6.25µM,calibration,6.25,-18.9,completed,"
    "数据/左旋多巴-6.25.csv\n"
    "1787000600,it_2,空白对照,calibration,0,-2.1,completed,数据/空白.csv\n"
)
_LEGACY_POINTS_CSV = (
    "point_id,label,concentration_um,current_nA\n"
    "p1,左旋多巴 6.25µM,6.25,-18.9\n"
    "p2,空白对照,0,-2.1\n"
)


def _legacy_workspace(root: Path, *, encoding: str) -> Path:
    """造一份"旧版本写下的"批次目录:目录名中文,内容按 `encoding` 落盘。"""
    workspace = root / "左旋多巴 第一批"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "measurement-index.csv").write_bytes(
        _LEGACY_INDEX_CSV.encode(encoding)
    )
    (workspace / "calibration-points.csv").write_bytes(
        _LEGACY_POINTS_CSV.encode(encoding)
    )
    # 工作区里的 JSON 同样会带中文(样品名/标签),旧版本一样按 locale 落盘。
    (workspace / "calibration-settings.json").write_bytes(
        json.dumps({"settings": {"method": "it", "sample_name": "左旋多巴 6.25µM"}},
                   ensure_ascii=False).encode(encoding)
    )
    return workspace


def _legacy_registry_bytes(encoding: str) -> bytes:
    payload = {
        "version": 1,
        "entries": [{
            "workspace_id": "legacy-batch",
            "locator": {"anchor": "project", "path": "左旋多巴 第一批"},
            "label": "左旋多巴 第一批",
            "created_at": 1787000000.0,
            "updated_at": 1787000600.0,
            "favorite": False,
            "summary": {},
        }],
    }
    # ensure_ascii=False 是关键:中文必须真的以该编码的**字节**存在文件里,
    # 否则 json.dumps 会转义成 \uXXXX,整个文件退化成 ASCII,测不出编码问题。
    return json.dumps(payload, ensure_ascii=False).encode(encoding)


def test_legacy_gbk_history_loads_with_chinese_intact_and_a_notice() -> None:
    """🔴 旧 GBK 编码的注册表 + 批次目录要能加载,中文要**正确还原**并给出提示。

    正确还原是这条测试的重点,不是"能不能加载":上一轮这几处 CSV 读被加过
    `errors="replace"`,加载确实不再报错,但历史列表里的中文样品名会显示成
    `'������� 6.25�0�8M'` —— 加载得上却看不懂,等于没修。
    """
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        workspace = _legacy_workspace(root, encoding="gb18030")
        (root / "history.json").write_bytes(_legacy_registry_bytes("gb18030"))
        registry = WorkspaceHistory(root / "history.json", root)

        listed = registry.list()
        entry = listed["entries"][0]

        # ① 条目没被"注册表已损坏"整份丢掉,中文标签正确还原
        assert entry["workspace_id"] == "legacy-batch"
        assert entry["label"] == "左旋多巴 第一批"
        assert "�" not in entry["label"]
        # ② 旧编码不是错误:工作区照常可用
        assert entry["status"] == "available"
        # ③ 用户可见提示,两条通道都给到:已有的 status_detail(前端已在显示)
        #    + 新增可选键 encoding
        assert "旧版本" in entry["status_detail"]
        assert "重新导出" in entry["status_detail"]
        assert entry["encoding"] == "gb18030"
        # ④ 注册表自身的旧编码也要说出来(前端把 registry_error 当提示条显示)
        assert "历史注册表" in listed["registry_error"]
        assert "gb18030" in listed["registry_error"]

        # ⑤ 摘要里的中文样品名必须正确还原 —— 它直接显示在历史列表上
        summary = WorkspaceHistory.summarize(workspace)
        assert summary["latest_sample_name"] == "空白对照"
        assert summary["records_count"] == 2
        assert summary["points_count"] == 2
        assert summary["method"] == "it"

        # ⑥ _health 的二元返回必须保住:gui_server.import_history 这样解包
        status, detail = WorkspaceHistory._health(workspace)
        assert status == "available"
        assert "旧版本" in detail


def test_clean_utf8_history_is_not_flagged_as_legacy() -> None:
    """反面样本:干净 UTF-8 不许被误报旧编码,否则提示变噪声、用户学会忽略它。"""
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        workspace = _legacy_workspace(root, encoding="utf-8")
        (root / "history.json").write_bytes(_legacy_registry_bytes("utf-8"))
        registry = WorkspaceHistory(root / "history.json", root)

        listed = registry.list()
        entry = listed["entries"][0]

        assert entry["label"] == "左旋多巴 第一批"
        assert entry["status"] == "available"
        assert entry["status_detail"] == "", entry["status_detail"]
        assert entry["encoding"] == "utf-8"
        assert listed["registry_error"] == "", listed["registry_error"]
        assert WorkspaceHistory.summarize(workspace)["latest_sample_name"] == "空白对照"


# ==========================================================================
# 机器门禁:用户数据的读不许再是严格 utf-8
# ==========================================================================

#: 🔴 作用域只限本轮改的这四个文件。别扩到全包 —— 随包资源(index.html /
#   styles.css / prebuilt 元数据)本来就该保持严格 UTF-8,扫全包会把它们全报成
#   违规,门禁立刻变成噪声。
_GATED_MODULES = ("workspace_history.py", "analyze.py", "it.py", "collect.py")

#: 明示豁免:这两处读的是**我们自己刚产出的文件**,不是用户数据。
#  坏了该立刻暴露,不该被编码容错掩盖(判据见 textio.py 模块 docstring 末段)。
_STRICT_READS_ON_PURPOSE = {
    # 随包 prebuilt firmware.json:构建产物,内容只有 rtt_address 这类 ASCII
    ("collect.py", "find_rtt_address"),
    # 上位机自己写的命令文件:_encode_firmware_command 本就拒收非 ASCII
    ("collect.py", "read_serial_lines"),
}


def _strict_reads(module_path: Path) -> set[tuple[str, str]]:
    """扫出"以严格编码读文件"的调用,返回 {(文件名, 所在函数名)}。

    判定:`.read_text(...)` 或读模式的 `.open(...)`,且**没带** `errors=`
    (带 errors= 至少不会因为一个坏字节把整个工作区弄没)。写模式一律跳过 ——
    写入端锁 UTF-8 是对的,本门禁管的是读取端。
    """
    import ast

    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    enclosing: dict[ast.AST, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for child in ast.walk(node):
                # 嵌套函数:外层先写、内层后写 ⇒ 最终留下最内层的名字
                enclosing[child] = node.name
    found: set[tuple[str, str]] = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr not in ("read_text", "open"):
            continue
        keywords = {kw.arg for kw in node.keywords}
        if "errors" in keywords:
            continue
        if node.func.attr == "open":
            mode = ""
            if node.args and isinstance(node.args[0], ast.Constant):
                mode = str(node.args[0].value)
            for kw in node.keywords:
                if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
                    mode = str(kw.value.value)
            if any(flag in mode for flag in ("w", "a", "x", "+")):
                continue
        found.add((module_path.name, enclosing.get(node, "<module>")))
    return found


def test_user_data_reads_no_longer_use_strict_utf8() -> None:
    """门禁:这四个文件里读用户数据的地方不许再是严格 utf-8。

    为什么要机器扫而不是靠人复核:这次的报错文本
    (`'utf-8' codec can't decode bytes in position 320-321`)**分辨不出是哪个文件**
    —— 抛它的 handler 同时包着一整批读。漏掉任何一处,现场就重演同一句报错,
    而我们会以为已经修好了。
    """
    package = Path(__file__).resolve().parents[1] / "pa_host"
    strict: set[tuple[str, str]] = set()
    for name in _GATED_MODULES:
        strict |= _strict_reads(package / name)

    assert strict == _STRICT_READS_ON_PURPOSE, (
        f"新增/遗漏的严格读:多出 {strict - _STRICT_READS_ON_PURPOSE}、"
        f"少了 {_STRICT_READS_ON_PURPOSE - strict}"
    )

    # 读取端确实走了共用模块,而不是又各写一份三级回退
    for name in ("workspace_history.py", "analyze.py", "it.py"):
        source = (package / name).read_text(encoding="utf-8")
        assert "from .textio import" in source, name
    # it.py 里那份只有两级回退、且不返回编码名的重复实现必须已经删掉
    # (查 `def `,不查裸名字 —— 删除说明里还引用着这个名字)
    assert "def _read_csv_lines" not in (package / "it.py").read_text(encoding="utf-8")
