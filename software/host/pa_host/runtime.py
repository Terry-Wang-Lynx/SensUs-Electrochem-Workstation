"""Runtime paths and subprocess commands for source and portable builds."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


APP_DIR_NAME = "SensUs Workstation"


def hidden_subprocess_kwargs(*, new_process_group: bool = False) -> dict[str, int]:
    """Keep command-line helpers off the Windows desktop."""
    if sys.platform != "win32":
        return {}
    flags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if new_process_group:
        flags |= int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    return {"creationflags": flags}


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _configured_path(name: str) -> Path | None:
    value = os.environ.get(name, "").strip()
    return Path(value).expanduser().resolve() if value else None


def _source_project_dir() -> Path:
    package_dir = Path(__file__).resolve().parent
    candidates = [Path.cwd(), *Path.cwd().parents, package_dir, *package_dir.parents]
    for candidate in candidates:
        if (candidate / "software" / "firmware" / "CMakeLists.txt").exists():
            return candidate.resolve()
    return package_dir.parents[2]


def project_dir() -> Path:
    configured = _configured_path("SENSUS_RESOURCE_DIR") or _configured_path(
        "SENSUS_PROJECT_DIR"
    )
    if configured is not None:
        return configured
    if is_frozen():
        executable = Path(sys.executable).resolve()
        bundle_dir = Path(getattr(sys, "_MEIPASS", executable.parent))
        candidates = [
            bundle_dir / "workstation",
            bundle_dir.parent / "workstation",
            bundle_dir.parent.parent / "workstation",
            bundle_dir.parent.parent.parent / "workstation",
            executable.parent / "workstation",
            executable.parent.parent / "workstation",
            executable.parent.parent.parent / "workstation",
        ]
        for candidate in candidates:
            if candidate.exists():
                return candidate.resolve()
        # A frozen app must never discover and use a developer's checkout
        # through the current working directory. Returning the expected
        # bundled location keeps a broken package obvious to its caller.
        return (bundle_dir / "workstation").resolve()
    return _source_project_dir()


def state_dir() -> Path:
    configured = _configured_path("SENSUS_STATE_DIR")
    if configured is not None:
        return configured
    if not is_frozen():
        return project_dir() / "measurements"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_DIR_NAME
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return base / APP_DIR_NAME
    base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return base / "sensus-workstation"


def logs_dir() -> Path:
    configured = _configured_path("SENSUS_LOG_DIR")
    if configured is not None:
        return configured
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Logs" / APP_DIR_NAME
    return state_dir() / "logs"


def default_measurements_dir() -> Path:
    configured = _configured_path("SENSUS_MEASUREMENTS_DIR")
    if configured is not None:
        return configured
    if not is_frozen():
        return state_dir() / "experiment_data"
    return Path.home() / "Documents" / "SensUs Measurements"


def module_command(module: str, *args: object) -> list[str]:
    """Return a child command that works both under Python and PyInstaller."""
    if is_frozen():
        aliases = {
            "pa_host.gui_server": "gui",
            "pa_host.it_tool": "it-tool",
            "pa_host.collect": "collect",
            "smpmgr": "smpmgr",
        }
        try:
            command = aliases[module]
        except KeyError as exc:
            raise ValueError(f"unsupported frozen child module: {module}") from exc
        return [sys.executable, command, *(str(value) for value in args)]
    return [sys.executable, "-m", module, *(str(value) for value in args)]
