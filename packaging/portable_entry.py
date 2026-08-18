#!/usr/bin/env python3
"""Single PyInstaller entry point for GUI and acquisition subprocesses."""

from __future__ import annotations

import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path


WINDOWS_COLD_START_TIMEOUT_S = 180
WINDOWS_SHUTDOWN_TIMEOUT_S = 900
WINDOWS_ALREADY_EXISTS = 183
WINDOWS_MUTEX_NAME = r"Local\SensUsElectrochemWorkstation.Portable.v1"
WINDOWS_CONFIRM_YES = 6
SENSUS_PRODUCT = "SensUs-Electrochem-Workstation"
_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")


class ExistingSensusBackend(RuntimeError):
    """A verified SensUs backend must be reused instead of duplicated."""

    def __init__(self, port: int, version: str, detail: str) -> None:
        super().__init__(
            f"检测到 SensUs {version} 仍在后台运行，{detail}。"
            "已打开原界面，本次不会启动第二个硬件后台。"
        )
        self.port = int(port)
        self.version = version


def _wait_for_server(
    url: str,
    expected_project: str,
    expected_version: str,
    expected_launcher_pid: int,
    expected_launch_token: str = "",
    timeout_s: float = 25,
) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{url}api/health", timeout=0.7) as response:
                payload = json.load(response)
                if (
                    response.status == 200
                    and payload.get("product") == SENSUS_PRODUCT
                    and os.path.normcase(str(payload.get("project", "")))
                    == os.path.normcase(expected_project)
                    and str(payload.get("version", "")) == expected_version
                    and str(payload.get("launcher_pid", ""))
                    == str(expected_launcher_pid)
                    and (
                        not expected_launch_token
                        or str(payload.get("launch_token", ""))
                        == expected_launch_token
                    )
                ):
                    return
        except OSError:
            time.sleep(0.2)
    raise RuntimeError("SensUs backend did not become ready")


def _available_port(preferred: int = 8765) -> int:
    for port in (preferred, 0):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            try:
                listener.bind(("127.0.0.1", port))
            except OSError:
                continue
            return int(listener.getsockname()[1])
    raise RuntimeError("No local server port is available")


def _configured_server_port(default: int = 8765) -> int:
    try:
        configured = int(os.environ.get("SENSUS_SERVER_PORT", "") or default)
    except ValueError:
        return default
    return configured if 1 <= configured <= 65535 else default


def _read_local_json(
    port: int, path: str, *, timeout_s: float = 1.0,
) -> dict[str, object] | None:
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}{path}", timeout=timeout_s,
        ) as response:
            payload = json.load(response)
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def _sensus_health(port: int) -> dict[str, object] | None:
    payload = _read_local_json(port, "/api/health")
    if payload is None or payload.get("ok") is not True:
        return None
    if payload.get("product") == SENSUS_PRODUCT:
        return payload
    # v0.4.7-v0.4.9 predate the product/schema fields. Confirm their exact
    # health shape here; runtime endpoints are checked separately before any
    # control request is sent to the process.
    project = str(payload.get("project") or "").strip()
    version = str(payload.get("version") or "").strip()
    session = str(payload.get("diagnostic_session") or "").strip()
    normalised_project = project.replace("\\", "/").rstrip("/").lower()
    if (
        not normalised_project.endswith("/workstation")
        or not re.fullmatch(r"[0-9A-Za-z._-]{6,160}", session)
        or _SEMVER_RE.fullmatch(version) is None
    ):
        return None
    return payload


def _existing_sensus_is_idle(
    port: int, health: dict[str, object],
) -> bool | None:
    if health.get("product") == SENSUS_PRODUCT:
        busy = health.get("hardware_busy")
        update_busy = health.get("app_update_busy", False)
        if not isinstance(busy, bool) or not isinstance(update_busy, bool):
            return None
        return not busy and not update_busy
    status = _read_local_json(port, "/api/status")
    schedule = _read_local_json(port, "/api/schedule")
    settings = _read_local_json(port, "/api/settings")
    if status is None or schedule is None or settings is None:
        return None
    if "state" not in status or "active" not in schedule or "state" not in settings:
        return None
    return not bool(
        status.get("busy")
        or str(status.get("state") or "") == "running"
        or schedule.get("active")
        or str(settings.get("state") or "") == "applying"
    )


def _port_is_available(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        try:
            listener.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def _resolve_windows_start_port(preferred: int) -> int:
    if _port_is_available(preferred):
        return preferred
    health = _sensus_health(preferred)
    if health is None:
        if os.environ.get("SENSUS_LAUNCH_TOKEN", "").strip():
            raise RuntimeError("更新指定的本地端口已被其他程序占用")
        return _available_port(0)
    idle = _existing_sensus_is_idle(preferred, health)
    version = str(health.get("version") or "未知")
    if idle is not True:
        detail = (
            "旧版正在测量或执行硬件操作"
            if idle is False else "无法确认旧版已处于空闲状态"
        )
        raise ExistingSensusBackend(preferred, version, detail)
    if not _windows_confirm(
        "SensUs 电化学工作站",
        f"检测到空闲的 SensUs {version} 后台。\n\n"
        "为避免两个程序同时占用 J-Link，是否安全关闭旧后台并继续？",
    ):
        raise ExistingSensusBackend(
            preferred, version, "用户选择保留当前空闲后台"
        )
    _request_shutdown_once(f"http://127.0.0.1:{preferred}/")
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        if _port_is_available(preferred):
            return preferred
        time.sleep(0.2)
    raise RuntimeError("旧版 SensUs 后台未能安全退出，请重启软件后再试")


def _request_shutdown_once(url: str, timeout_s: float = 10.0) -> None:
    request = urllib.request.Request(
        f"{url}api/shutdown",
        data=b"{}",
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            if response.status != 200:
                raise RuntimeError(
                    f"旧版后台拒绝安全退出（HTTP {response.status}）"
                )
    except urllib.error.HTTPError as exc:
        if exc.code in {409, 423}:
            raise RuntimeError(
                "旧版后台正在执行硬件任务，本次不会强制关闭"
            ) from exc
        raise RuntimeError(
            f"旧版后台无法安全退出（HTTP {exc.code}）"
        ) from exc


def _request_safe_shutdown(
    url: str, timeout_s: float = WINDOWS_SHUTDOWN_TIMEOUT_S,
) -> None:
    """Keep retrying a protected shutdown while a driver task finishes."""
    deadline = time.monotonic() + max(1.0, timeout_s)
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError("Timed out waiting for a safe hardware shutdown")
        request = urllib.request.Request(
            f"{url}api/shutdown",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=remaining) as response:
                if response.status == 200:
                    return
                raise RuntimeError(
                    f"Safe shutdown failed with HTTP {response.status}"
                )
        except urllib.error.HTTPError as exc:
            if exc.code not in {409, 423}:
                raise RuntimeError(
                    f"Safe shutdown failed with HTTP {exc.code}"
                ) from exc
            if deadline - time.monotonic() <= 0:
                raise RuntimeError(
                    "Timed out waiting for a safe hardware shutdown"
                ) from exc
            time.sleep(0.5)


def _windows_message(title: str, message: str) -> None:
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, message, title, 0x40)
    except (AttributeError, OSError):
        print(f"{title}: {message}", file=sys.stderr, flush=True)


def _windows_confirm(title: str, message: str) -> bool:
    try:
        import ctypes

        # MB_YESNO | MB_ICONWARNING | MB_DEFBUTTON2 keeps cancellation safe.
        return int(ctypes.windll.user32.MessageBoxW(
            None, message, title, 0x04 | 0x30 | 0x100,
        )) == WINDOWS_CONFIRM_YES
    except (AttributeError, OSError):
        return False


def _open_windows_single_instance(*, reject_existing: bool) -> int | None:
    """Keep the launcher mutex alive in both the window and backend processes."""
    import ctypes

    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW.argtypes = [
        ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p,
    ]
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_bool
    handle = int(kernel32.CreateMutexW(None, False, WINDOWS_MUTEX_NAME) or 0)
    if not handle:
        raise OSError("Unable to create the SensUs single-instance mutex")
    if reject_existing and int(kernel32.GetLastError()) == WINDOWS_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        return None
    return handle


def _acquire_windows_single_instance() -> int | None:
    """Prevent two portable launchers from probing or preparing one J-Link."""
    return _open_windows_single_instance(reject_existing=True)


def _hold_windows_single_instance() -> int:
    """Keep the mutex valid if a crashed native window leaves its backend alive."""
    handle = _open_windows_single_instance(reject_existing=False)
    if handle is None:  # pragma: no cover - reject_existing=False always returns a handle
        raise OSError("Unable to retain the SensUs single-instance mutex")
    return handle


def _release_windows_single_instance(handle: int) -> None:
    import ctypes

    kernel32 = ctypes.windll.kernel32
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_bool
    kernel32.CloseHandle(ctypes.c_void_p(handle))


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
    if sys.platform == "win32":
        _windows_message(
            "SensUs 电化学工作站",
            "软件已在系统浏览器中打开。\n\n"
            "关闭本提示不会退出软件；使用完成后，"
            "请点击页面右上角的“退出”安全关闭后台。",
        )
    if child is not None:
        return child.wait()
    return 0


def _windows_app() -> int:
    from pa_host.runtime import project_dir
    from pa_host import __version__

    expected_project = str(project_dir())
    child: subprocess.Popen[bytes] | None = None
    try:
        port = _resolve_windows_start_port(_configured_server_port())
    except ExistingSensusBackend as existing:
        existing_url = f"http://127.0.0.1:{existing.port}/"
        webbrowser.open(existing_url)
        _windows_message("SensUs 电化学工作站", str(existing))
        return 0
    url = f"http://127.0.0.1:{port}/"
    try:
        # Always own the backend that serves this window. Reusing a matching
        # process can attach to an orphan from an older launcher, after which
        # closing the new window would leave that process behind again.
        child_environment = {
            **os.environ,
            "SENSUS_PORTABLE_CHILD": "1",
            "SENSUS_APP_ROOT": str(Path(sys.executable).resolve().parent),
            "SENSUS_APP_PID": str(os.getpid()),
            "SENSUS_SERVER_PORT": str(port),
        }
        child = subprocess.Popen(
            [
                sys.executable, "gui", "--host", "127.0.0.1",
                "--port", str(port),
            ],
            env=child_environment,
            creationflags=(
                subprocess.CREATE_NEW_PROCESS_GROUP
                | getattr(subprocess, "CREATE_NO_WINDOW", 0)
            ),
        )
        # Windows Defender may inspect every native dependency on the first
        # launch of an unsigned portable ZIP. A clean machine can need close to
        # a minute before /api/health becomes responsive.
        _wait_for_server(
            url, expected_project, __version__,
            expected_launcher_pid=os.getpid(),
            expected_launch_token=os.environ.get(
                "SENSUS_LAUNCH_TOKEN", ""
            ).strip(),
            timeout_s=WINDOWS_COLD_START_TIMEOUT_S,
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
            try:
                _request_safe_shutdown(url)
                child.wait(timeout=30)
            except (OSError, RuntimeError, subprocess.TimeoutExpired):
                # A hardware operation may still be in its protected section.
                # Give the backend one final grace period before signalling it.
                try:
                    child.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    child.send_signal(
                        getattr(signal, "CTRL_BREAK_EVENT", signal.SIGTERM)
                    )
                    try:
                        child.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        child.kill()


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    commands = {"gui", "it-tool", "collect", "smpmgr"}
    command = argv.pop(0) if argv and argv[0] in commands else ""
    if not command and sys.platform == "win32" and not os.environ.get("SENSUS_PORTABLE_CHILD"):
        mutex = _acquire_windows_single_instance()
        if mutex is None:
            _windows_message(
                "SensUs 电化学工作站",
                "软件已经打开。请切换到现有窗口，不要重复启动。",
            )
            return 0
        try:
            try:
                return _windows_app()
            except (OSError, RuntimeError) as exc:
                _windows_message(
                    "SensUs 电化学工作站无法启动",
                    str(exc),
                )
                return 1
        finally:
            _release_windows_single_instance(mutex)
    child_mutex: int | None = None
    if (
        command == "gui"
        and sys.platform == "win32"
        and os.environ.get("SENSUS_PORTABLE_CHILD")
    ):
        # The parent owns the same named object first. Holding a second handle
        # here keeps a newly launched copy from racing an orphan backend that is
        # still releasing hardware after the native window was force-closed.
        child_mutex = _hold_windows_single_instance()
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
    try:
        return int(target(argv) or 0)
    finally:
        if child_mutex is not None:
            _release_windows_single_instance(child_mutex)


if __name__ == "__main__":
    raise SystemExit(main())
