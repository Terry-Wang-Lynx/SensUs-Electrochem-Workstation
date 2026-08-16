"""Portable, crash-safe registry for saved GUI workspaces."""

from __future__ import annotations

import csv
import json
import os
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any


REGISTRY_VERSION = 1
MARKER_NAME = ".sensus-workspace.json"
WORKSPACE_KIND = "workspace"
BATCH_KIND = "batch"
_KNOWN_KINDS = {WORKSPACE_KIND, BATCH_KIND}


class WorkspaceHistory:
    """Index workspaces without owning or deleting their measurement data."""

    def __init__(self, registry_path: Path, project_dir: Path) -> None:
        self.registry_path = registry_path
        self.project_dir = project_dir.resolve()
        self.home_dir = Path.home().resolve()
        self.registry_dir = registry_path.parent.resolve()
        self.lock = threading.RLock()

    @staticmethod
    def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", prefix=f".{path.name}.",
                suffix=".tmp", dir=path.parent, delete=False,
            ) as handle:
                temporary = Path(handle.name)
                json.dump(payload, handle, indent=2, ensure_ascii=False)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            temporary = None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _empty(self, error: str = "") -> dict[str, Any]:
        return {"version": REGISTRY_VERSION, "entries": [], "registry_error": error}

    def _read(self) -> dict[str, Any]:
        if not self.registry_path.exists():
            return self._empty()
        try:
            raw = json.loads(self.registry_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeError) as exc:
            return self._empty(f"历史注册表已损坏：{exc}")
        if isinstance(raw, list):
            raw = {"version": 0, "entries": raw}
        if not isinstance(raw, dict) or not isinstance(raw.get("entries"), list):
            return self._empty("历史注册表格式无效")
        entries: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in raw["entries"]:
            if not isinstance(item, dict):
                continue
            workspace_id = str(item.get("workspace_id") or item.get("id") or "")
            locator = item.get("locator")
            if not workspace_id or workspace_id in seen or not isinstance(locator, dict):
                continue
            anchor = str(locator.get("anchor") or "")
            relative = str(locator.get("path") or "")
            if anchor not in {"project", "home", "registry", "filesystem"} or not self._safe_relative(relative):
                continue
            kind = str(item.get("kind") or WORKSPACE_KIND)
            if kind not in _KNOWN_KINDS:
                kind = WORKSPACE_KIND
            workspace_root_id = str(item.get("workspace_root_id") or "")
            if kind == WORKSPACE_KIND:
                workspace_root_id = workspace_id
            seen.add(workspace_id)
            entries.append({
                "workspace_id": workspace_id,
                "locator": {"anchor": anchor, "path": relative},
                "label": str(item.get("label") or Path(relative).name or "未命名工作区"),
                "created_at": self._number(item.get("created_at"), time.time()),
                "updated_at": self._number(item.get("updated_at"), 0.0),
                "favorite": bool(item.get("favorite", False)),
                "summary": dict(item.get("summary") or {}),
                "kind": kind,
                "workspace_root_id": workspace_root_id,
            })
        return {
            "version": REGISTRY_VERSION,
            "entries": entries,
            "registry_error": (
                "" if raw.get("version") in (None, 0, REGISTRY_VERSION)
                else f"已忽略不支持的注册表版本 {raw.get('version')}"
            ),
        }

    @staticmethod
    def _number(value: Any, default: float) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _safe_relative(value: str) -> bool:
        path = Path(value)
        return bool(value) and not path.is_absolute() and ".." not in path.parts

    def _locator(self, workspace: Path) -> dict[str, str]:
        resolved = workspace.resolve()
        for anchor, root in (
            ("project", self.project_dir), ("home", self.home_dir),
            ("registry", self.registry_dir),
        ):
            try:
                relative = resolved.relative_to(root)
            except ValueError:
                continue
            if relative.parts:
                return {"anchor": anchor, "path": relative.as_posix()}
        if os.name != "nt":
            try:
                relative = resolved.relative_to(Path("/"))
                if relative.parts:
                    return {"anchor": "filesystem", "path": relative.as_posix()}
            except ValueError:
                pass
        raise ValueError("历史记录路径无法用相对定位保存")

    def _path(self, locator: dict[str, Any]) -> Path:
        anchor = str(locator.get("anchor") or "")
        relative = str(locator.get("path") or "")
        if anchor not in {"project", "home", "registry", "filesystem"} or not self._safe_relative(relative):
            raise ValueError("历史记录路径无效")
        root = {
            "project": self.project_dir, "home": self.home_dir,
            "registry": self.registry_dir,
            "filesystem": Path("/"),
        }[anchor]
        candidate = (root / relative).resolve()
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise ValueError("历史记录路径越界") from exc
        return candidate

    def _marker_id(self, workspace: Path) -> str | None:
        value = self.marker_info(workspace).get("workspace_id")
        return str(value) if value else None

    @staticmethod
    def _valid_marker_kind(value: Any) -> str:
        kind = str(value or WORKSPACE_KIND)
        return kind if kind in _KNOWN_KINDS else WORKSPACE_KIND

    def marker_info(self, workspace: Path) -> dict[str, Any]:
        """Read the directory marker without requiring a registry entry."""
        marker = workspace / MARKER_NAME
        try:
            payload = json.loads(marker.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                return {}
            workspace_id = str(payload.get("workspace_id") or "")
            if not workspace_id:
                return {}
            return {
                **payload,
                "workspace_id": workspace_id,
                "kind": self._valid_marker_kind(payload.get("kind")),
                "workspace_root_id": str(
                    payload.get("workspace_root_id") or ""
                ),
            }
        except (OSError, TypeError, json.JSONDecodeError, UnicodeError):
            return {}

    def _ensure_marker(
        self,
        workspace: Path,
        workspace_id: str | None = None,
        *,
        kind: str = WORKSPACE_KIND,
        workspace_root_id: str = "",
        label: str = "",
    ) -> str:
        workspace.mkdir(parents=True, exist_ok=True)
        kind = self._valid_marker_kind(kind)
        existing_payload = self.marker_info(workspace)
        existing = str(existing_payload.get("workspace_id") or "")
        if existing:
            updates: dict[str, Any] = {}
            if existing_payload.get("kind") != kind:
                updates["kind"] = kind
            if kind == BATCH_KIND and workspace_root_id:
                if existing_payload.get("workspace_root_id") != workspace_root_id:
                    updates["workspace_root_id"] = workspace_root_id
            if label and not existing_payload.get("label"):
                updates["label"] = label
            if updates:
                self._atomic_json(workspace / MARKER_NAME, {
                    **existing_payload, **updates,
                })
            return existing
        workspace_id = workspace_id or uuid.uuid4().hex
        payload: dict[str, Any] = {
            "version": 1,
            "workspace_id": workspace_id,
            "created_at": time.time(),
        }
        if kind != WORKSPACE_KIND:
            payload["kind"] = kind
        if workspace_root_id:
            payload["workspace_root_id"] = workspace_root_id
        if label:
            payload["label"] = label
        self._atomic_json(workspace / MARKER_NAME, payload)
        return workspace_id

    def _relocate(self, entry: dict[str, Any]) -> Path | None:
        expected = self._path(entry["locator"])
        if expected.is_dir():
            return expected
        workspace_id = entry["workspace_id"]
        parent = expected.parent
        if parent.is_dir():
            for marker in parent.glob(f"*/{MARKER_NAME}"):
                if self._marker_id(marker.parent) == workspace_id:
                    entry["locator"] = self._locator(marker.parent)
                    return marker.parent.resolve()
        return None

    @staticmethod
    def _health(workspace: Path) -> tuple[str, str]:
        if not workspace.is_dir():
            return "missing", "工作区目录不存在"
        json_files = (
            "calibration-model.json", "calibration-settings.json",
            "calibration-plateau.json", "calibration-filter.json",
            "calibration-selection.json", "calibration-validation.json",
            "calibration-drift.json",
        )
        for name in json_files:
            path = workspace / name
            if not path.exists():
                continue
            try:
                if not isinstance(json.loads(path.read_text(encoding="utf-8")), dict):
                    raise ValueError("JSON 顶层不是对象")
            except (OSError, ValueError, json.JSONDecodeError, UnicodeError) as exc:
                return "corrupt", f"{name} 无法读取：{exc}"
        csv_schemas = {
            "calibration-points.csv": {"concentration_um", "current_nA"},
            "measurement-index.csv": {"run_id", "state"},
        }
        for name, required in csv_schemas.items():
            path = workspace / name
            if not path.exists():
                continue
            try:
                with path.open(newline="", encoding="utf-8") as handle:
                    fields = set(csv.DictReader(handle).fieldnames or ())
                if not required.issubset(fields):
                    return "corrupt", f"{name} 缺少必需列"
            except (OSError, UnicodeError, csv.Error) as exc:
                return "corrupt", f"{name} 无法读取：{exc}"
        return "available", ""

    @staticmethod
    def summarize(workspace: Path) -> dict[str, Any]:
        """Build a display summary from an existing workspace directory."""
        workspace = workspace.resolve()
        point_count = 0
        selected_count = 0
        points_path = workspace / "calibration-points.csv"
        if points_path.exists():
            try:
                with points_path.open(newline="", encoding="utf-8") as handle:
                    point_count = sum(
                        1 for row in csv.DictReader(handle)
                        if row.get("concentration_um") not in (None, "")
                        and row.get("current_nA") not in (None, "")
                    )
            except (OSError, UnicodeError, csv.Error):
                point_count = 0

        selection_path = workspace / "calibration-selection.json"
        if selection_path.exists():
            try:
                selection = json.loads(selection_path.read_text(encoding="utf-8"))
                selected = selection.get("selected_point_ids", [])
                selected_count = len(selected) if isinstance(selected, list) else 0
            except (OSError, UnicodeError, TypeError, json.JSONDecodeError):
                selected_count = 0

        records: list[dict[str, Any]] = []
        index_path = workspace / "measurement-index.csv"
        if index_path.exists():
            try:
                with index_path.open(newline="", encoding="utf-8") as handle:
                    records = [dict(row) for row in csv.DictReader(handle)]
            except (OSError, UnicodeError, csv.Error):
                records = []
        if not records:
            # Older exports may contain summaries without the workspace index.
            for summary_path in sorted(workspace.glob("*-summary.json")):
                try:
                    payload = json.loads(summary_path.read_text(encoding="utf-8"))
                except (OSError, UnicodeError, TypeError, json.JSONDecodeError):
                    continue
                if not isinstance(payload, dict):
                    continue
                records.append({
                    "finished_at": payload.get("finished_at"),
                    "sample_name": payload.get("sample_name"),
                    "sample_role": payload.get("sample_role"),
                    "state": payload.get("state") or "completed",
                    "steady_current_nA": payload.get("steady_current_nA"),
                    "measurement_settings_json": json.dumps(
                        payload.get("measurement_settings") or {}
                    ),
                })

        completed = [row for row in records if row.get("state") == "completed"]
        role_counts = {
            role: sum(row.get("sample_role") == role for row in completed)
            for role in ("calibration", "test", "stabilization", "cv")
        }
        latest = max(
            completed,
            key=lambda row: WorkspaceHistory._number(row.get("finished_at"), 0.0),
            default=None,
        )
        model_r2: float | None = None
        model_path = workspace / "calibration-model.json"
        if model_path.exists():
            try:
                model = json.loads(model_path.read_text(encoding="utf-8"))
                raw_r2 = model.get("r2") if isinstance(model, dict) else None
                model_r2 = float(raw_r2) if raw_r2 is not None else None
            except (OSError, UnicodeError, TypeError, ValueError, json.JSONDecodeError):
                model_r2 = None
        settings: dict[str, Any] = {}
        for settings_name in (
            "workspace-state.json", "calibration-settings.json",
        ):
            settings_path = workspace / settings_name
            if not settings_path.exists():
                continue
            try:
                payload = json.loads(settings_path.read_text(encoding="utf-8"))
                candidate = payload.get("settings", payload) if isinstance(payload, dict) else {}
                if isinstance(candidate, dict):
                    settings = candidate
                    break
            except (OSError, UnicodeError, TypeError, json.JSONDecodeError):
                continue

        return {
            "points_count": point_count,
            "selected_points_count": selected_count,
            "records_count": len(records),
            "completed_count": len(completed),
            "calibration_count": role_counts["calibration"],
            "test_count": role_counts["test"],
            "stabilization_count": role_counts["stabilization"],
            "cv_count": role_counts["cv"],
            "has_model": model_path.exists(),
            "model_r2": model_r2,
            "method": settings.get("method", "it"),
            "latest_result_at": (
                WorkspaceHistory._number(latest.get("finished_at"), 0.0)
                if latest else None
            ),
            "latest_sample_name": str((latest or {}).get("sample_name") or ""),
            "latest_sample_role": str((latest or {}).get("sample_role") or ""),
        }

    def register(
        self, workspace: Path, summary: dict[str, Any], label: str = "",
        *, create_marker: bool = True, kind: str | None = None,
        workspace_root_id: str | None = None,
    ) -> dict[str, Any]:
        with self.lock:
            data = self._read()
            workspace = workspace.resolve()
            locator = self._locator(workspace)
            existing = next(
                (item for item in data["entries"] if item["locator"] == locator),
                None,
            )
            marker = self.marker_info(workspace)
            resolved_kind = self._valid_marker_kind(
                kind
                or (existing or {}).get("kind")
                or marker.get("kind")
                or WORKSPACE_KIND
            )
            resolved_root_id = str(
                workspace_root_id
                or (existing or {}).get("workspace_root_id")
                or marker.get("workspace_root_id")
                or ""
            )
            if existing is not None:
                workspace_id = existing["workspace_id"]
            elif create_marker:
                workspace_id = self._ensure_marker(
                    workspace, kind=resolved_kind,
                    workspace_root_id=resolved_root_id,
                    label=label.strip(),
                )
            else:
                workspace_id = self._marker_id(workspace) or uuid.uuid4().hex
            if resolved_kind == WORKSPACE_KIND:
                resolved_root_id = workspace_id
            elif not resolved_root_id:
                resolved_root_id = workspace_id
            if create_marker:
                self._ensure_marker(
                    workspace, workspace_id=workspace_id,
                    kind=resolved_kind,
                    workspace_root_id=(
                        resolved_root_id if resolved_kind == BATCH_KIND else ""
                    ),
                    label=label.strip(),
                )
            now = time.time()
            entry = next(
                (item for item in data["entries"] if item["workspace_id"] == workspace_id),
                None,
            )
            if entry is None:
                entry = {
                    "workspace_id": workspace_id,
                    "created_at": now,
                    "favorite": False,
                }
                data["entries"].append(entry)
            entry.update({
                "locator": locator,
                "label": label.strip() or workspace.name or "未命名工作区",
                "updated_at": now,
                "summary": dict(summary),
                "kind": resolved_kind,
                "workspace_root_id": resolved_root_id,
            })
            self._atomic_json(self.registry_path, {
                "version": REGISTRY_VERSION, "entries": data["entries"],
            })
            return self._public(entry, workspace, "available", "")

    def register_batch(
        self,
        workspace: Path,
        workspace_root: Path,
        summary: dict[str, Any],
        label: str = "",
    ) -> dict[str, Any]:
        """Register a batch directory below a selected workspace root."""
        root = workspace_root.resolve()
        root_id = self._marker_id(root)
        if not root_id:
            root_entry = self.register(root, self.summarize(root))
            root_id = root_entry["workspace_id"]
        return self.register(
            workspace, summary, label, kind=BATCH_KIND,
            workspace_root_id=root_id,
        )

    def list(self, current_workspace: Path | None = None) -> dict[str, Any]:
        with self.lock:
            data = self._read()
            relocated = False
            entries = []
            current = current_workspace.resolve() if current_workspace else None
            for entry in data["entries"]:
                old_locator = dict(entry["locator"])
                workspace = self._relocate(entry)
                relocated = relocated or entry["locator"] != old_locator
                if workspace is None:
                    workspace = self._path(entry["locator"])
                status, detail = self._health(workspace)
                public = self._public(entry, workspace, status, detail)
                public["current"] = bool(current is not None and workspace == current)
                entries.append(public)
            if relocated:
                self._atomic_json(self.registry_path, {
                    "version": REGISTRY_VERSION, "entries": data["entries"],
                })
            entries.sort(key=lambda item: (
                not item["favorite"], -float(item["updated_at"]), item["label"]
            ))
            return {"version": REGISTRY_VERSION, "entries": entries,
                    "registry_error": data["registry_error"]}

    def resolve(self, workspace_id: str) -> tuple[dict[str, Any], Path]:
        with self.lock:
            data = self._read()
            entry = next(
                (item for item in data["entries"] if item["workspace_id"] == workspace_id),
                None,
            )
            if entry is None:
                raise ValueError("历史记录不存在")
            old_locator = dict(entry["locator"])
            workspace = self._relocate(entry)
            if workspace is None:
                raise FileNotFoundError("历史工作区目录不存在")
            status, detail = self._health(workspace)
            if status != "available":
                raise ValueError(detail or "历史工作区不可用")
            if entry["locator"] != old_locator:
                self._atomic_json(self.registry_path, {
                    "version": REGISTRY_VERSION, "entries": data["entries"],
                })
            return entry, workspace

    def favorite(self, workspace_id: str, favorite: bool | None = None) -> dict[str, Any]:
        with self.lock:
            data = self._read()
            entry = next(
                (item for item in data["entries"] if item["workspace_id"] == workspace_id),
                None,
            )
            if entry is None:
                raise ValueError("历史记录不存在")
            entry["favorite"] = not entry["favorite"] if favorite is None else bool(favorite)
            self._atomic_json(self.registry_path, {
                "version": REGISTRY_VERSION, "entries": data["entries"],
            })
            workspace = self._relocate(entry) or self._path(entry["locator"])
            status, detail = self._health(workspace)
            return self._public(entry, workspace, status, detail)

    def remove(self, workspace_id: str) -> None:
        with self.lock:
            data = self._read()
            retained = [
                entry for entry in data["entries"]
                if entry["workspace_id"] != workspace_id
            ]
            if len(retained) == len(data["entries"]):
                raise ValueError("历史记录不存在")
            self._atomic_json(self.registry_path, {
                "version": REGISTRY_VERSION, "entries": retained,
            })

    @staticmethod
    def _public(
        entry: dict[str, Any], workspace: Path, status: str, detail: str,
    ) -> dict[str, Any]:
        locator = entry["locator"]
        return {
            "workspace_id": entry["workspace_id"],
            "label": entry["label"],
            "location": locator["path"],
            "location_anchor": locator["anchor"],
            "kind": entry.get("kind", WORKSPACE_KIND),
            "workspace_root_id": entry.get(
                "workspace_root_id", entry["workspace_id"]
            ),
            "created_at": entry["created_at"],
            "updated_at": entry["updated_at"],
            "favorite": bool(entry["favorite"]),
            "status": status,
            "status_detail": detail,
            "summary": dict(entry.get("summary") or {}),
        }
