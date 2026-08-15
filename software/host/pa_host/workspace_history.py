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
            seen.add(workspace_id)
            entries.append({
                "workspace_id": workspace_id,
                "locator": {"anchor": anchor, "path": relative},
                "label": str(item.get("label") or Path(relative).name or "未命名工作区"),
                "created_at": self._number(item.get("created_at"), time.time()),
                "updated_at": self._number(item.get("updated_at"), 0.0),
                "favorite": bool(item.get("favorite", False)),
                "summary": dict(item.get("summary") or {}),
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
        marker = workspace / MARKER_NAME
        try:
            payload = json.loads(marker.read_text(encoding="utf-8"))
            value = str(payload.get("workspace_id") or "")
            return value or None
        except (OSError, TypeError, json.JSONDecodeError, UnicodeError):
            return None

    def _ensure_marker(self, workspace: Path, workspace_id: str | None = None) -> str:
        workspace.mkdir(parents=True, exist_ok=True)
        existing = self._marker_id(workspace)
        if existing:
            return existing
        workspace_id = workspace_id or uuid.uuid4().hex
        self._atomic_json(workspace / MARKER_NAME, {
            "version": 1,
            "workspace_id": workspace_id,
            "created_at": time.time(),
        })
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

    def register(
        self, workspace: Path, summary: dict[str, Any], label: str = "",
    ) -> dict[str, Any]:
        with self.lock:
            data = self._read()
            workspace = workspace.resolve()
            locator = self._locator(workspace)
            workspace_id = self._ensure_marker(workspace)
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
            })
            self._atomic_json(self.registry_path, {
                "version": REGISTRY_VERSION, "entries": data["entries"],
            })
            return self._public(entry, workspace, "available", "")

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
            "created_at": entry["created_at"],
            "updated_at": entry["updated_at"],
            "favorite": bool(entry["favorite"]),
            "status": status,
            "status_detail": detail,
            "summary": dict(entry.get("summary") or {}),
        }
