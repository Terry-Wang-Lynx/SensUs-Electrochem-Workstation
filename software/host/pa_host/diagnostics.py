"""Fault-tolerant structured diagnostics for the workstation runtime."""

from __future__ import annotations

import io
import json
import os
import platform
import secrets
import sys
import threading
import time
import traceback
import zipfile
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


_SENSITIVE_KEYS = {
    "authorization", "cookie", "password", "secret", "token", "api_key",
    "access_key", "private_key",
}
_MAX_STRING_LENGTH = 8_000
_MAX_COLLECTION_LENGTH = 100
_MAX_BUNDLE_FILE_BYTES = 4 * 1024 * 1024


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")


def _safe_value(value: Any, *, key: str = "", depth: int = 0) -> Any:
    """Return bounded JSON-safe context without credentials."""
    lowered = key.lower()
    if any(marker in lowered for marker in _SENSITIVE_KEYS):
        return "[redacted]"
    if depth >= 6:
        return "[max-depth]"
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if value == value and abs(value) != float("inf") else None
    if isinstance(value, Path):
        value = str(value)
    if isinstance(value, str):
        if len(value) <= _MAX_STRING_LENGTH:
            return value
        return value[:_MAX_STRING_LENGTH] + "...[truncated]"
    if isinstance(value, dict):
        items = list(value.items())[:_MAX_COLLECTION_LENGTH]
        result = {
            str(item_key): _safe_value(
                item_value, key=str(item_key), depth=depth + 1
            )
            for item_key, item_value in items
        }
        if len(value) > len(items):
            result["_truncated_items"] = len(value) - len(items)
        return result
    if isinstance(value, (list, tuple, set)):
        items = list(value)[:_MAX_COLLECTION_LENGTH]
        result = [_safe_value(item, depth=depth + 1) for item in items]
        if len(value) > len(items):
            result.append(f"...[{len(value) - len(items)} more]")
        return result
    return _safe_value(str(value), key=key, depth=depth + 1)


class DiagnosticStore:
    """Keep recent events in memory and append them to bounded JSONL files."""

    def __init__(
        self,
        log_dir: Path,
        *,
        max_bytes: int = 2 * 1024 * 1024,
        backup_count: int = 4,
        memory_events: int = 300,
    ) -> None:
        self.log_dir = Path(log_dir)
        self.log_path = self.log_dir / "workstation.jsonl"
        self.max_bytes = max(1024, int(max_bytes))
        self.backup_count = max(1, int(backup_count))
        self.session_id = (
            time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)
        )
        self.started_at = time.time()
        self._events: deque[dict[str, Any]] = deque(maxlen=memory_events)
        self._lock = threading.RLock()
        self.last_write_error = ""
        self._hooks_installed = False

    def _rotate_locked(self, incoming_bytes: int) -> None:
        current_size = self.log_path.stat().st_size if self.log_path.exists() else 0
        if current_size + incoming_bytes <= self.max_bytes:
            return
        oldest = self.log_path.with_name(
            f"{self.log_path.name}.{self.backup_count}"
        )
        oldest.unlink(missing_ok=True)
        for index in range(self.backup_count - 1, 0, -1):
            source = self.log_path.with_name(f"{self.log_path.name}.{index}")
            target = self.log_path.with_name(f"{self.log_path.name}.{index + 1}")
            if source.exists():
                os.replace(source, target)
        if self.log_path.exists():
            os.replace(
                self.log_path,
                self.log_path.with_name(f"{self.log_path.name}.1"),
            )

    def _append_locked(self, payload: dict[str, Any]) -> None:
        line = (
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._rotate_locked(len(line))
        with self.log_path.open("ab") as handle:
            handle.write(line)

    def record(
        self,
        level: str,
        event: str,
        message: str,
        **context: Any,
    ) -> str:
        level = str(level or "info").lower()
        if level not in {"debug", "info", "warning", "error", "critical"}:
            level = "info"
        event_id = "D-" + time.strftime("%H%M%S") + "-" + secrets.token_hex(3)
        payload = {
            "timestamp": _utc_timestamp(),
            "epoch": time.time(),
            "level": level,
            "event": str(event or "application.event"),
            "message": str(message or ""),
            "event_id": event_id,
            "session_id": self.session_id,
            "pid": os.getpid(),
            "thread": threading.current_thread().name,
            "context": _safe_value(context),
        }
        with self._lock:
            self._events.append(payload)
            try:
                self._append_locked(payload)
                self.last_write_error = ""
            except Exception as exc:  # diagnostics must never break acquisition
                self.last_write_error = f"{type(exc).__name__}: {exc}"
        return event_id

    def exception(
        self,
        event: str,
        message: str,
        exc: BaseException,
        **context: Any,
    ) -> str:
        return self.record(
            "error",
            event,
            message,
            exception_type=type(exc).__name__,
            exception_message=str(exc),
            traceback="".join(
                traceback.format_exception(type(exc), exc, exc.__traceback__)
            ),
            **context,
        )

    def snapshot(self, *, limit: int = 80) -> dict[str, Any]:
        with self._lock:
            events = list(self._events)[-max(1, min(int(limit), 300)):]
            return {
                "session_id": self.session_id,
                "started_at": self.started_at,
                "log_dir": str(self.log_dir),
                "log_file": str(self.log_path),
                "write_error": self.last_write_error,
                "events": events,
            }

    def log_files(self) -> list[Path]:
        paths = [self.log_path]
        paths.extend(
            self.log_path.with_name(f"{self.log_path.name}.{index}")
            for index in range(1, self.backup_count + 1)
        )
        return [path for path in paths if path.is_file()]

    def bundle(
        self,
        *,
        context: dict[str, Any] | None = None,
        extra_files: Iterable[tuple[str, Path]] = (),
    ) -> bytes:
        """Build a bounded local support bundle without raw measurement CSVs."""
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            manifest = {
                **self.snapshot(limit=300),
                "system": {
                    "platform": platform.platform(),
                    "python": sys.version,
                    "executable": sys.executable,
                    "frozen": bool(getattr(sys, "frozen", False)),
                },
                "context": _safe_value(context or {}),
            }
            archive.writestr(
                "diagnostics.json",
                json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
            )
            for path in self.log_files():
                try:
                    data = path.read_bytes()
                except OSError:
                    continue
                archive.writestr(f"logs/{path.name}", data[-_MAX_BUNDLE_FILE_BYTES:])
            used_names: set[str] = set()
            for requested_name, path in extra_files:
                path = Path(path)
                if not path.is_file():
                    continue
                safe_name = Path(str(requested_name)).name or path.name
                if safe_name in used_names:
                    continue
                try:
                    data = path.read_bytes()
                except OSError:
                    continue
                used_names.add(safe_name)
                archive.writestr(
                    f"current-run/{safe_name}", data[-_MAX_BUNDLE_FILE_BYTES:]
                )
        return output.getvalue()

    def install_exception_hooks(self) -> None:
        """Record otherwise-unhandled main-thread and background-thread errors."""
        with self._lock:
            if self._hooks_installed:
                return
            previous_sys_hook = sys.excepthook
            previous_thread_hook = threading.excepthook

            def sys_hook(exc_type: type[BaseException], exc: BaseException, tb: Any) -> None:
                if exc.__traceback__ is None:
                    exc = exc.with_traceback(tb)
                self.exception("runtime.unhandled", "Unhandled application exception", exc)
                previous_sys_hook(exc_type, exc, tb)

            def thread_hook(args: threading.ExceptHookArgs) -> None:
                exc = args.exc_value
                if exc is not None:
                    self.exception(
                        "thread.unhandled",
                        "Unhandled background thread exception",
                        exc,
                        thread=getattr(args.thread, "name", ""),
                    )
                previous_thread_hook(args)

            sys.excepthook = sys_hook
            threading.excepthook = thread_hook
            self._hooks_installed = True
