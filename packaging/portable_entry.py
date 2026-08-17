#!/usr/bin/env python3
"""Single PyInstaller entry point for GUI and acquisition subprocesses."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path


WINDOWS_COLD_START_TIMEOUT_S = 180


def _wait_for_server(url: str, expected_project: str, timeout_s: float = 25) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{url}api/health", timeout=0.7) as response:
                payload = json.load(response)
                if (
                    response.status == 200
                    and os.path.normcase(str(payload.get("project", "")))
                    == os.path.normcase(expected_project)
                ):
                    return
        except OSError:
            time.sleep(0.2)
    raise RuntimeError("SensUs backend did not become ready")


def _fallback_to_system_browser(url: str, child: subprocess.Popen[bytes] | None,
                                reason: BaseException | None = None) -> int:
    """Keep the portable server usable when the optional WebView2 runtime is absent."""
    if reason is not None:
        print(
            f"[app] WebView2 unavailable ({reason}); opening the system browser.",
            file=sys.stderr,
            flush=True,
        )
    webbrowser.open(url)
    if child is not None:
        return child.wait()
    return 0


def _windows_app() -> int:
    from pa_host.runtime import project_dir

    url = "http://127.0.0.1:8765/"
    expected_project = str(project_dir())
    child: subprocess.Popen[bytes] | None = None
    try:
        try:
            _wait_for_server(url, expected_project, timeout_s=0.8)
        except RuntimeError:
            child_environment = {
                **os.environ,
                "SENSUS_PORTABLE_CHILD": "1",
                "SENSUS_APP_ROOT": str(Path(sys.executable).resolve().parent),
                "SENSUS_APP_PID": str(os.getpid()),
            }
            child = subprocess.Popen(
                [sys.executable, "gui", "--host", "127.0.0.1", "--port", "8765"],
                env=child_environment,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
            )
            # Windows Defender may inspect every native dependency on the
            # first launch of an unsigned portable ZIP. The verified clean
            # Windows machine needed about 55 seconds before /api/health.
            _wait_for_server(
                url, expected_project, timeout_s=WINDOWS_COLD_START_TIMEOUT_S,
            )
        try:
            import webview

            webview.create_window(
                "SensUs 电化学工作站", url, width=1440, height=920, min_size=(1080, 720)
            )
            webview.start(gui="edgechromium")
        except ImportError as exc:
            return _fallback_to_system_browser(url, child, exc)
        except Exception as exc:
            return _fallback_to_system_browser(url, child, exc)
        return 0
    finally:
        if child is not None and child.poll() is None:
            child.send_signal(getattr(signal, "CTRL_BREAK_EVENT", signal.SIGTERM))
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    commands = {"gui", "it-tool", "collect", "smpmgr"}
    command = argv.pop(0) if argv and argv[0] in commands else ""
    if not command and sys.platform == "win32" and not os.environ.get("SENSUS_PORTABLE_CHILD"):
        return _windows_app()
    if command in {"", "gui"}:
        from pa_host.gui_server import main as target
    elif command == "it-tool":
        from pa_host.it_tool import main as target
    elif command == "collect":
        from pa_host.collect import main as target
    else:
        from smpmgr.main import app

        app(args=argv, prog_name="smpmgr")
        return 0
    return int(target(argv) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
