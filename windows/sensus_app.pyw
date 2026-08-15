r"""
SensUs Electrochem Workstation -- Windows Native App.

Uses Edge WebView2 to give a native-window experience:
- No browser needed -- the workstation UI loads directly in this window.
- Title bar shows "SensUs Electrochem Workstation".
- Closes cleanly: the background Python server shuts down when you close the window.

Run:
    .venv/Scripts/pythonw windows/sensus_app.pyw
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import webview

PROJECT_DIR = Path(__file__).resolve().parent.parent
VENV_PYTHON = PROJECT_DIR / ".venv" / "Scripts" / "python.exe"
HOST = "127.0.0.1"
PORT = 8765
URL = f"http://{HOST}:{PORT}/"

_server_process: subprocess.Popen | None = None


def _server_ready() -> bool:
    try:
        req = urllib.request.Request(
            f"http://{HOST}:{PORT}/api/health", method="GET"
        )
        with urllib.request.urlopen(req, timeout=2) as resp:
            return resp.status == 200
    except Exception:
        return False


def start_server() -> None:
    global _server_process
    env = os.environ.copy()
    env["SENSUS_PROJECT_DIR"] = str(PROJECT_DIR)
    env["PYTHONPATH"] = str(PROJECT_DIR / "software" / "host")
    env["PYTHONUNBUFFERED"] = "1"

    _server_process = subprocess.Popen(
        [str(VENV_PYTHON), "-m", "pa_host.gui_server",
         "--host", HOST, "--port", str(PORT)],
        cwd=PROJECT_DIR,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def wait_for_server(timeout: float = 30) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _server_ready():
            return True
        time.sleep(0.3)
    return False


def stop_server() -> None:
    if _server_process is not None and _server_process.poll() is None:
        _server_process.terminate()
        try:
            _server_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _server_process.kill()


def start_gui() -> None:
    # 1 — Ensure the workstation server is running
    if _server_ready():
        print("[app] Server already running — reusing.")
    else:
        print("[app] Starting server...")
        start_server()
        if not wait_for_server():
            print("[app] ERROR: Server did not start in time.")
            stop_server()
            sys.exit(1)
        print("[app] Server ready.")

    # 2 — Create the native window
    window = webview.create_window(
        title="SensUs Electrochem Workstation",
        url=URL,
        width=1240,
        height=820,
        min_size=(940, 620),
        text_select=True,
        confirm_close=True,
    )

    # 3 — Clean up on close
    def on_closed():
        stop_server()

    window.events.closed += on_closed

    webview.start(gui="edgechromium", debug=False)


if __name__ == "__main__":
    start_gui()
