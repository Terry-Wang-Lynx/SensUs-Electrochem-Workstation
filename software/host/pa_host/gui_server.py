"""Local browser GUI for the 10 Hz electrochemical i-t workflow.

The GUI deliberately uses only Python's standard library on the server side;
the browser renders the plots with a small canvas-based frontend.  This keeps
the one-click tool usable on the lab Mac without installing a desktop GUI
toolkit. Hardware acquisition is delegated to ``pa_host.it_tool``. V4.0 uses
RTT/J-Link; V5.1 uses its DATA USB CDC while retaining the same line protocol,
parser and analysis pipeline.
"""

from __future__ import annotations

import argparse
import copy
import csv
import hashlib
import io
import json
import math
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from .it import (
    CalibrationPoint,
    CalibrationModel,
    PlateauConfig,
    evaluate_ap_score,
    evaluate_platform,
    fit_calibration,
    load_calibration_points,
    load_model,
    load_run_csv,
    resample_run_10hz,
    save_summary,
    summarize_run,
)
from .collect import (
    BRIDGE_SHUTDOWN_COMMAND,
    JLINK_EXE,
    JLinkMemoryRTT,
    RTTControlBlockUnavailable,
    NRF52833_INFO_PART_ADDRESS,
    NRF52833_INFO_PART_VALUE,
    OPENOCD_EXE,
    OPENOCD_SCRIPTS,
    SPEED_KHZ as JLINK_SPEED_KHZ,
    _jlink_file_argument,
    _parse_jlink_mem32,
    find_rtt_address,
    jlink_connection_script,
    parse_audit,
    probe_jlink_target,
    run_jlink_script,
    start_jlink_rtt,
    stop_jlink_rtt,
)
from .cv import (
    export_cv_csv,
    load_cv_run,
    plot_cv,
    save_cv_summary,
    summarize_cv,
)
from .filtering import (
    FILTER_DEFAULTS,
    validate_filter_config,
    write_filtered_csv,
)
from .live_metrics import (
    PreparedLiveStage,
    metrics_from_stage,
    prepare_live_stage,
)
from .stability_eta import StabilityEtaEstimator
from .workspace_history import BATCH_KIND, WORKSPACE_KIND, WorkspaceHistory
from .frontend_update import FrontendUpdater
from .app_update import AppUpdateError, AppUpdateManager
from .diagnostics import DiagnosticStore
from . import __version__
from . import runtime
from .windows_jlink import (
    JLINK_VENDOR_ID,
    commander_reports_probe_connected,
    install_winusb_driver,
    jlink_bindings,
    openocd_reports_missing_driver,
    openocd_reports_probe_communication_error,
    reports_probe_busy,
    resolve_helper as resolve_winusb_helper,
)
from .jlink_usb import discover_jlink_usb_devices

_IS_WIN = sys.platform == "win32"


PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = runtime.project_dir()
STATE_DIR = runtime.state_dir()
WINUSB_HELPER = resolve_winusb_helper(PROJECT_DIR)
DIAGNOSTICS = DiagnosticStore(runtime.logs_dir())
FRONTEND_UPDATER = FrontendUpdater(
    PACKAGE_DIR / "gui", STATE_DIR, PROJECT_DIR
)


def _app_update_event(event: str, message: str, context: dict[str, Any]) -> None:
    DIAGNOSTICS.record(
        "warning" if event.endswith("failed") else "info",
        event, message, **context,
    )


APP_UPDATER = AppUpdateManager(
    __version__, STATE_DIR, event_callback=_app_update_event
)
GUI_DIR = FRONTEND_UPDATER.prepare_startup()
RUNS_DIR = STATE_DIR / "gui_runs"
DEFAULT_PORT = 8765
MEASUREMENT_DURATION_S = 180.0
COLLECTOR_DURATION_S = 190.0
TARGET_RATE_HZ = 10.0
FIT_WINDOW_S = 20.0
MAX_PLATEAU_BACKFILL_WINDOWS = 32
LIVE_ANALYSIS_REFRESH_S = 0.9
CLIENT_DIAGNOSTIC_MAX_LENGTH = 8_000
CONFIG_GATE_GET_RETRY_S = 0.75
CONFIG_GATE_LEGACY_PROBE_DELAY_S = 6.0
FIRMWARE_BUILD_DIR = PROJECT_DIR / "software" / "firmware" / "build" / "firmware" / "zephyr"
FIRMWARE_PREBUILT_DIR = PROJECT_DIR / "software" / "firmware" / "prebuilt"
FIRMWARE_CONFIG = PROJECT_DIR / "software" / "firmware" / "src" / "measurement_config.h"
SETTINGS_PATH = STATE_DIR / "gui_settings.json"
FILTER_SETTINGS_PATH = STATE_DIR / "filter_settings.json"
PLATEAU_SETTINGS_PATH = STATE_DIR / "plateau_settings.json"
WORKFLOW_PATH = STATE_DIR / "gui_workflow.json"
HISTORY_PATH = STATE_DIR / "workspace_history.json"
JLINK_SERIAL = os.environ.get("SENSUS_JLINK_SERIAL", "").strip()
CONFIGURED_JLINK_SERIAL = JLINK_SERIAL
SERIAL_DATA_PORT = os.environ.get("SENSUS_SERIAL_PORT", "").strip()
SERIAL_SMP_PORT = os.environ.get("SENSUS_SMP_PORT", "").strip()
HARDWARE_TRANSPORT = os.environ.get("SENSUS_TRANSPORT", "auto").lower()
HARDWARE_TRANSPORT_REQUESTED = HARDWARE_TRANSPORT
JLINK_CDC_SERIAL = os.environ.get("SENSUS_JLINK_CDC_SERIAL", "0000297345691")
SENSUS_USB_IDS = {(0x2FE3, 0x0100)}
# A manual selection is intentionally process-local. It is cleared by the
# "自动检测" choice and is never changed while a hardware operation is busy.
DEVICE_SELECTION_LOCK = threading.RLock()
SELECTED_DEVICE: dict[str, Any] | None = None
# Serial probing is deliberately kept out of the HTTP request path. A USB
# device can take several seconds to answer while macOS is re-enumerating it;
# the browser should keep rendering and receive the previous snapshot instead.
DEVICE_DISCOVERY_LOCK = threading.RLock()
DEVICE_DISCOVERY_CACHE: list[dict[str, Any]] = []
DEVICE_DISCOVERY_AT = 0.0
DEVICE_DISCOVERY_THREAD: threading.Thread | None = None
DEVICE_DISCOVERY_ERROR = ""
DEVICE_DISCOVERY_LOG_SIGNATURE: tuple[Any, ...] | None = None
DEVICE_DISCOVERY_LOG_ERROR = ""
DEVICE_DISCOVERY_TTL_S = 1.0
DEVICE_DISCOVERY_CANCEL = threading.Event()
DEVICE_PROBE_LOCK = threading.Lock()
JLINK_TARGET_PROBE_LOCK = threading.Lock()
JLINK_TARGET_CACHE_LOCK = threading.RLock()
JLINK_TARGET_CACHE: dict[str, dict[str, Any]] = {}
JLINK_TARGET_CACHE_TTL_S = 5.0
JLINK_DRIVER_INSTALL_LOCK = threading.Lock()
SHUTDOWN_INTENT = threading.Event()
JLINK_DRIVER_TASK_LOCK = threading.RLock()
JLINK_DRIVER_TASK: dict[str, Any] = {
    "state": "idle",
    "device_id": "",
    "message": "",
    "error": "",
    "diagnostic_id": "",
    "started_at": None,
    "finished_at": None,
}
SMPMGR_EXE = Path(
    os.environ.get("SENSUS_SMPMGR")
    or shutil.which("smpmgr")
    or "/tmp/smpvenv/bin/smpmgr"
)
_V51_RESOURCE_CANDIDATES = (
    PROJECT_DIR / "software" / "ver5.1",
    PROJECT_DIR / "packaging" / "resources" / "v51",
)
V51_RESOURCE_DIR = next(
    (path for path in _V51_RESOURCE_CANDIDATES if path.exists()),
    _V51_RESOURCE_CANDIDATES[0],
)
V51_UPLOAD_SCRIPT = V51_RESOURCE_DIR / "scripts" / "03-usb-upload.sh"
V51_PREBUILT_IMAGE = V51_RESOURCE_DIR / "images" / "app.signed.bin"


def _ensure_not_shutting_down() -> None:
    if SHUTDOWN_INTENT.is_set():
        raise RuntimeError("应用正在安全退出，不能再启动新的硬件任务")


def _transport_status(transport: str | None = None) -> dict[str, Any]:
    """Expose the transport selected for the next/current acquisition."""
    actual = str(transport or HARDWARE_TRANSPORT or "unknown").lower()
    labels = {
        "serial": "USB DATA CDC",
        "rtt": "RTT / J-Link",
        "auto": "自动检测",
    }
    payload: dict[str, Any] = {
        "transport": actual,
        "transport_label": labels.get(actual, "连接方式未知"),
        "transport_requested": str(HARDWARE_TRANSPORT_REQUESTED or "auto").lower(),
    }
    with DEVICE_SELECTION_LOCK:
        if SELECTED_DEVICE is not None:
            payload.update({
                "device_id": SELECTED_DEVICE.get("id", ""),
                "device_name": SELECTED_DEVICE.get("name", ""),
                "device_selection": "manual",
            })
    return payload


def _escape_applescript(value: object) -> str:
    """Escape a value for an AppleScript double-quoted string literal."""
    return (str(value)
            .replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("\r", " ")
            .replace("\n", " "))


def _browse_workspace_directory(initial_path: str = "") -> dict[str, Any]:
    """Open the platform folder picker without interpolating user input."""
    initial = Path(os.path.expandvars(os.path.expanduser(initial_path.strip())))
    if not initial_path.strip() or not initial.is_dir():
        initial_value = ""
    else:
        initial_value = str(initial.resolve())
    environment: dict[str, str] | None = None

    if sys.platform == "darwin":
        executable = shutil.which("osascript") or "/usr/bin/osascript"
        script = """
use scripting additions

on run argv
set initialPath to ""
if (count of argv) > 0 then set initialPath to item 1 of argv
set initialFolder to missing value
if initialPath is not "" then
    try
        set initialFolder to POSIX file initialPath as alias
    end try
end if
try
    if initialFolder is missing value then
        set chosenFolder to choose folder with prompt "选择 SensUs 数据工作区"
    else
        set chosenFolder to choose folder with prompt "选择 SensUs 数据工作区" default location initialFolder
    end if
    return POSIX path of chosenFolder
on error number -128
    return ""
end try
end run
"""
        command = [executable, "-e", script, "--", initial_value]
    elif sys.platform == "win32":
        executable = shutil.which("powershell.exe") or "powershell.exe"
        environment = {**os.environ, "SENSUS_INITIAL_FOLDER": initial_value}
        script = r"""
Add-Type -AssemblyName System.Windows.Forms
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$dialog = New-Object System.Windows.Forms.FolderBrowserDialog
$dialog.Description = '选择 SensUs 数据工作区'
$dialog.ShowNewFolderButton = $true
if ($env:SENSUS_INITIAL_FOLDER -and [IO.Directory]::Exists($env:SENSUS_INITIAL_FOLDER)) {
    $dialog.SelectedPath = $env:SENSUS_INITIAL_FOLDER
}
if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {
    [Console]::Write($dialog.SelectedPath)
}
"""
        command = [
            executable, "-NoProfile", "-STA", "-NonInteractive",
            "-Command", script,
        ]
    else:
        raise RuntimeError("当前系统暂不支持原生目录浏览，请直接填写绝对路径")

    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            env=environment,
            **runtime.hidden_subprocess_kwargs(),
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("目录选择窗口等待超时，请重试") from exc
    except OSError as exc:
        raise RuntimeError(f"无法打开目录选择窗口：{exc}") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise RuntimeError(f"目录选择失败：{detail or '系统选择器返回错误'}")
    selected = completed.stdout.strip()
    if not selected:
        return {"selected": False, "path": ""}
    selected_path = Path(selected).expanduser().resolve()
    if not selected_path.is_dir():
        raise RuntimeError("所选工作区目录不存在")
    return {"selected": True, "path": str(selected_path)}


def _send_system_notification(title: str, body: str) -> None:
    """Send a best-effort native notification without invoking a shell."""
    if sys.platform == "darwin":
        executable = shutil.which("osascript") or "/usr/bin/osascript"
        script = (
            f'display notification "{_escape_applescript(body)}" '
            f'with title "{_escape_applescript(title)}"'
        )
        try:
            subprocess.run(
                [executable, "-e", script],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=3,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
    elif sys.platform.startswith("linux"):
        executable = shutil.which("notify-send")
        if executable:
            try:
                subprocess.run(
                    [executable, title, body],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=3,
                )
            except (OSError, subprocess.TimeoutExpired):
                pass


def _notify_measurement_completion(
    run: dict[str, Any], result: dict[str, Any]
) -> None:
    """Turn every acquisition terminal state into one concise system notice."""
    metadata = run.get("metadata") or {}
    state = str(run.get("state") or result.get("state") or "")
    sample = str(result.get("sample_name") or metadata.get("sample_name")
                 or run.get("run_id") or "本轮测量")
    debug = bool(metadata.get("debug"))
    if state == "completed":
        title = "电化学工作站 · Debug 完成" if debug else "电化学工作站 · 测试完成"
        details = [sample]
        steady = result.get("steady_current_nA")
        if steady is not None:
            try:
                details.append(f"稳态电流 {float(steady):.3g} nA")
            except (TypeError, ValueError):
                pass
        predicted = result.get("predicted_concentration_um")
        if predicted is not None:
            try:
                details.append(f"预测浓度 {float(predicted):.3g} µM")
            except (TypeError, ValueError):
                pass
        body = " · ".join(details)
    elif state == "idle":
        title = "电化学工作站 · 测试已停止"
        body = f"{sample} · 本轮未完成，原始数据已保留"
    else:
        title = "电化学工作站 · 测试失败"
        detail = str(run.get("error") or result.get("export_error") or "请查看工作站日志")
        body = f"{sample} · {detail}"
    _send_system_notification(title, body)


def _existing_toolchain_path(
    configured: str | None, fallbacks: tuple[str, ...], marker: str
) -> Path:
    """Use a configured toolchain path when valid, then known legacy locations."""
    candidates = ([configured] if configured else []) + list(fallbacks)
    paths = [Path(value).expanduser() for value in candidates if value]
    for path in paths:
        if (path / marker).exists():
            return path
    return paths[0]


NCS_DIR = _existing_toolchain_path(
    os.environ.get("SENSUS_NCS_DIR"),
    ("~/sensus-toolchains/ncs", "~/ncs"),
    "zephyr/zephyr-env.sh",
)
ZEPHYR_SDK_DIR = _existing_toolchain_path(
    os.environ.get("SENSUS_ZEPHYR_SDK_DIR"),
    ("~/sensus-toolchains/zephyr-sdk-1.0.1", "~/zephyr-sdk-1.0.1"),
    "gnu/arm-zephyr-eabi/bin/arm-zephyr-eabi-gcc",
)
_configured_activate = os.environ.get("SENSUS_NCS_VENV_ACTIVATE")
NCS_VENV_ACTIVATE = Path(_configured_activate).expanduser() if (
    _configured_activate and Path(_configured_activate).expanduser().exists()
) else NCS_DIR / ".venv/bin/activate"


def _firmware_source() -> str:
    try:
        payload = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        source = str(payload.get("firmware_source") or "")
        return source if source in {"build", "prebuilt"} else ""
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return ""


def _firmware_artifact(name: str) -> Path:
    built = FIRMWARE_BUILD_DIR / name
    prebuilt = FIRMWARE_PREBUILT_DIR / name
    if _firmware_source() == "prebuilt" and prebuilt.exists():
        return prebuilt
    if _firmware_source() == "build" and built.exists():
        return built
    return built if built.exists() else prebuilt

# ── 两相测量:还原瞬态 → 过零 → 氧化稳态 ────────────────────────────────
# 工作点 E=+200mV 驱动**氧化**,所以稳态电流走器件的原生方向(流出 WE),
# 不受 offset 天花板约束。但复位会把恒电位仪关掉、电极被放生漂到开路电位,
# 而实测 OCP 比 +200mV 更正 ⇒ 重新加上 +200mV 是一次**向下**阶跃 ⇒ 起始瞬态
# 是**还原**方向、实测起点 ≥500nA。还原方向的可测上限就是 offset 本身
# (datasheet p41),所以两个相位要的 offset 恰好相反:
#   瞬态期:offset 必须大(否则撞轨 ⇒ 电极根本不在 +200mV,那段数据条件是错的)
#   测量期:offset 必须小(它只是白占量程 + 白加绝对容差)
# 复位会重新制造瞬态,所以"改 offset 必须重烧"曾让这两件事无法同时满足;
# 方案 C 的 RANGE 命令在线切档打破了这个循环。详见
# docs/troubleshooting/electrochem-workstation-烧录与rtt取数.md §14。
MEAS_FSR_CODE = 2        # 250nA:增益 max ±1%(trimmed 档)、慢钟组、天花板 240nA
MEAS_OFFSET_SEL = 4      # SEL4 = 9nA 绝对档,容差 7–11nA(±2nA,对比 50%FS 的 ±50nA)
SETTLE_WINDOW_S = 20.0   # 漂移速率的拟合窗口,与 FIT_WINDOW_S 取数窗口一致
SETTLE_DRIFT_PA_S = 20.0 # 建议阈值,仅作提示;真正的判据交给人看数字


def _transient_phase(times: list[float], currents: list[float],
                     valid: list[bool]) -> dict[str, Any]:
    """判断当前处于还原瞬态还是氧化稳态,并给出末窗漂移速率。

    符号约定:``currents`` 是固件换算出的**还原电流**(nA),>0 = 还原
    (非原生方向,受 offset 天花板限制),<0 = 氧化(原生方向)。

    过零判据刻意取"**最后一个非负样本之后**",而不是"第一次过零" ——
    实测 r12 在零附近来回穿了 25 次,取首次会早报 ~7s。
    """
    n = len(times)
    if n == 0:
        return {"phase": "idle", "n": 0}
    last_nonneg = -1
    for i in range(n - 1, -1, -1):
        if currents[i] >= 0.0:
            last_nonneg = i
            break
    if last_nonneg == n - 1:
        phase = "reduction"            # 末点仍在还原侧,瞬态未结束
        crossed_at = None
    else:
        phase = "oxidation"
        crossed_at = times[last_nonneg + 1] if last_nonneg >= 0 else times[0]

    # 末窗最小二乘斜率(pA/s)。纯 python:这里不值得为 5 行拟合引 numpy。
    t_end = times[-1]
    win_idx = [i for i in range(n) if times[i] >= t_end - SETTLE_WINDOW_S]
    win = [(times[i], currents[i]) for i in win_idx if valid[i]]
    # 末窗里只要还有撞轨样本,就说明电位控制在这段时间内失过效 ⇒ 不许算"已稳定"。
    # (实测 r10:全程 59% 撞轨,末段却已平静 —— 只看斜率会把它判成可切档。)
    win_railed = sum(1 for i in win_idx if not valid[i])
    drift_pa_s: float | None = None
    if len(win) >= 4:
        m = len(win)
        sx = sum(p[0] for p in win)
        sy = sum(p[1] for p in win)
        sxx = sum(p[0] * p[0] for p in win)
        sxy = sum(p[0] * p[1] for p in win)
        den = m * sxx - sx * sx
        if den > 0:
            # nA/s → pA/s;再取负号,让"氧化电流在长大"显示为正的漂移量级
            drift_pa_s = -(m * sxy - sx * sy) / den * 1000.0
    railed = sum(1 for v in valid if not v)
    return {
        "phase": phase,
        "n": n,
        "crossed_at_s": crossed_at,
        "since_cross_s": (t_end - crossed_at) if crossed_at is not None else None,
        "elapsed_s": t_end,
        "drift_pa_s": drift_pa_s,
        "drift_threshold_pa_s": SETTLE_DRIFT_PA_S,
        "railed_samples": railed,
        "railed_frac": railed / n,
        "window_railed": win_railed,
        # ready 只是"这几个提示条件都满足",不是"数据一定可信" —— 20pA/s 是我拍的,
        # 真正该看的是 drift_pa_s 本身相对你信号大小的占比。
        "ready": bool(phase == "oxidation" and drift_pa_s is not None
                      and abs(drift_pa_s) <= SETTLE_DRIFT_PA_S
                      and win_railed == 0),
    }


def ncs_venv_prefix() -> str:
    """返回激活 NCS venv 的 shell 前缀,venv 不存在时返回空串。

    west 不在系统 PATH 上而在 NCS 的 venv 内;返回空串是为了兼容 west 已在
    PATH 上的机器(以及 CI),此时让 west 自己去报错,而不是先报 activate 缺失。

    Windows: 返回用于 cmd /c 的前缀;macOS/Linux: 返回 source ... && 前缀。
    """
    if not NCS_VENV_ACTIVATE.exists():
        return ""
    if _IS_WIN:
        # Windows: 在 cmd /c 里用 call 激活
        return f"call {shlex.quote(str(NCS_VENV_ACTIVATE))} && "
    return f"source {shlex.quote(str(NCS_VENV_ACTIVATE))} && "
FSR_OPTIONS = {
    50: "MAX30131_FSR_50NA",
    100: "MAX30131_FSR_100NA",
    250: "MAX30131_FSR_250NA",
    500: "MAX30131_FSR_500NA",
    1000: "MAX30131_FSR_1000NA",
    2000: "MAX30131_FSR_2000NA",
}
CV_EIS_FSR_OPTIONS = {
    4: "MAX30131_EIS_FSR_4UA",
    8: "MAX30131_EIS_FSR_8UA",
    20: "MAX30131_EIS_FSR_20UA",
    40: "MAX30131_EIS_FSR_40UA",
}
IT_WIDE_FSR_OPTIONS = {
    4000: "MAX30131_EIS_FSR_4UA",
    8000: "MAX30131_EIS_FSR_8UA",
    20000: "MAX30131_EIS_FSR_20UA",
    40000: "MAX30131_EIS_FSR_40UA",
}
OFFSET_OPTIONS = {
    "9nA": ("MAX30131_OFFSET_SEL4_9NA", 9),
    "19nA": ("MAX30131_OFFSET_SEL5_19NA", 19),
    "40nA": ("MAX30131_OFFSET_SEL6_40NA", 40),
    "80nA": ("MAX30131_OFFSET_SEL7_80NA", 80),
    "10pct": ("MAX30131_OFFSET_10PCT_FSR", 0.10),
    "20pct": ("MAX30131_OFFSET_20PCT_FSR", 0.20),
    "50pct": ("MAX30131_OFFSET_50PCT_FSR", 0.50),
}
SENS_PERIOD_MS = {0: 124, 1: 242, 2: 476, 3: 945, 4: 1882, 5: 3757}


def _port_accepts_connections(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
            return True
    except OSError:
        return False


def _all_serial_port_infos() -> list[Any]:
    """Return USB serial candidates plus J-Links without a CDC interface."""
    try:
        from serial.tools import list_ports
    except ImportError:
        list_ports = None

    def is_candidate(info: Any) -> bool:
        device = str(getattr(info, "device", "") or "")
        if not device:
            return False
        if device in {SERIAL_DATA_PORT, SERIAL_SMP_PORT}:
            return True
        descriptor = _port_descriptor(info).lower()
        vid = getattr(info, "vid", None)
        pid = getattr(info, "pid", None)
        return bool(
            (vid, pid) in SENSUS_USB_IDS
            or vid == JLINK_VENDOR_ID
            or "pa-converter" in descriptor
            or "sensus" in descriptor
            or "segger" in descriptor
            or "j-link" in descriptor
            or "jlink" in descriptor
        )

    infos = (
        [info for info in list_ports.comports() if is_candidate(info)]
        if list_ports is not None else []
    )
    try:
        native_jlinks = discover_jlink_usb_devices()
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        DIAGNOSTICS.record(
            "warning", "device.jlink.usb_discovery_failed",
            "Native J-Link USB discovery failed", error=str(exc),
        )
        native_jlinks = []
    existing_counts: dict[str, int] = {}
    for info in infos:
        if _is_jlink_port(info):
            serial = _normalise_probe_serial(getattr(info, "serial_number", ""))
            if serial:
                existing_counts[serial] = existing_counts.get(serial, 0) + 1
    native_groups: dict[str, list[Any]] = {}
    for info in native_jlinks:
        native_groups.setdefault(
            _normalise_probe_serial(info.serial_number), []
        ).append(info)
    for serial, native_group in native_groups.items():
        if not serial:
            infos.extend(native_group)
            continue
        missing_count = max(0, len(native_group) - existing_counts.get(serial, 0))
        infos.extend(native_group[:missing_count])
    return infos


def _port_descriptor(info: Any) -> str:
    return " ".join(
        str(getattr(info, field, "") or "")
        for field in (
            "device", "description", "hwid", "manufacturer", "product",
            "serial_number", "location",
        )
    )


def _is_jlink_port(info: Any) -> bool:
    """Identify every SEGGER CDC interface, not only the historical serial."""
    descriptor = _port_descriptor(info).lower()
    manufacturer = str(getattr(info, "manufacturer", "") or "").lower()
    product = str(getattr(info, "product", "") or "").lower()
    vid = getattr(info, "vid", None)
    if vid == 0x1366:
        return True
    if "segger" in manufacturer or "j-link" in product or "jlink" in product:
        return True
    if JLINK_CDC_SERIAL:
        configured = re.sub(r"[^0-9a-f]", "", str(JLINK_CDC_SERIAL).lower())
        if configured and configured in re.sub(r"[^0-9a-f]", "", descriptor):
            return True
    return "j-link" in descriptor or "jlink" in descriptor


def _serial_port_infos() -> list[Any]:
    """Return non-J-Link USB CDC candidates for V5.1 DATA/SMP discovery."""
    return [info for info in _all_serial_port_infos() if not _is_jlink_port(info)]


def _normalise_probe_serial(value: object) -> str:
    """Map a J-Link CDC serial such as 000029734569 to probe SN 29734569."""
    raw = str(value or "").strip()
    digits = re.sub(r"\D", "", raw)
    if not digits:
        return ""
    try:
        return str(int(digits, 10))
    except ValueError:
        return digits.lstrip("0") or "0"


def _jlink_probe_serial(info: Any) -> str:
    serial = _normalise_probe_serial(getattr(info, "serial_number", ""))
    if serial:
        return serial
    # Some macOS descriptors only expose the serial in the device path.
    device = str(getattr(info, "device", "") or "")
    match = re.search(r"usbmodem(\d+)", device, flags=re.IGNORECASE)
    return _normalise_probe_serial(match.group(1) if match else "")


def _meaningful_process_tail(*outputs: object, limit: int = 6) -> list[str]:
    """Return useful process diagnostics without OpenOCD's normal shutdown line."""
    generic = {"shutdown command invoked"}
    lines: list[str] = []
    for output in outputs:
        if output is None:
            continue
        for raw_line in str(output).splitlines():
            line = raw_line.strip()
            if not line or line.lower() in generic:
                continue
            if not lines or lines[-1] != line:
                lines.append(line)
    return lines[-limit:]


def _unknown_jlink_target_status(probe_serial: str) -> dict[str, Any]:
    return {
        "target_state": "unknown",
        "target_detail": "目标板连接尚未确认",
        "target_backend": "",
        "target_checked_at": 0.0,
        "probe_serial": probe_serial,
    }


def _cached_jlink_target_status(probe_serial: str) -> dict[str, Any]:
    if not probe_serial:
        return _unknown_jlink_target_status("")
    with JLINK_TARGET_CACHE_LOCK:
        cached = JLINK_TARGET_CACHE.get(probe_serial)
        return copy.deepcopy(
            cached or _unknown_jlink_target_status(probe_serial)
        )


def _openocd_jlink_available() -> bool:
    return bool(
        OPENOCD_EXE.is_file()
        and (OPENOCD_SCRIPTS / "interface/jlink.cfg").is_file()
        and (OPENOCD_SCRIPTS / "target/nrf52.cfg").is_file()
    )


def _openocd_identity_command() -> str:
    return (
        "set sensus_info [read_memory "
        f"0x{NRF52833_INFO_PART_ADDRESS:08X} 32 1]; "
        "echo \"SENSUS_INFO_PART=[format 0x%08X "
        "[lindex $sensus_info 0]]\""
    )


def _openocd_identity_verified(output: str) -> bool:
    return bool(re.search(
        rf"\bSENSUS_INFO_PART\s*=\s*(?:0x)?0*"
        rf"{NRF52833_INFO_PART_VALUE:X}\b",
        output,
        flags=re.IGNORECASE,
    ))


def _run_openocd_bounded(
    command: list[str], *, timeout_s: float,
) -> subprocess.CompletedProcess[str]:
    """Run one-shot OpenOCD with a graceful shutdown path before termination."""
    control_port = _free_local_tcp_port()
    controlled = [
        command[0],
        "-c", "gdb_port disabled",
        "-c", "tcl_port disabled",
        "-c", f"telnet_port {control_port}",
        *command[1:],
    ]
    process = subprocess.Popen(
        controlled,
        cwd=PROJECT_DIR,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        **runtime.hidden_subprocess_kwargs(new_process_group=True),
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired as exc:
        stdout = str(exc.stdout or "")
        stderr = str(exc.stderr or "")
        try:
            with socket.create_connection(
                ("127.0.0.1", control_port), timeout=0.7,
            ) as control:
                control.sendall(b"shutdown\n")
        except OSError:
            pass
        try:
            final_stdout, final_stderr = process.communicate(timeout=2.0)
            stdout += final_stdout or ""
            stderr += final_stderr or ""
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                final_stdout, final_stderr = process.communicate(timeout=2.0)
                stdout += final_stdout or ""
                stderr += final_stderr or ""
            except subprocess.TimeoutExpired:
                process.kill()
                final_stdout, final_stderr = process.communicate()
                stdout += final_stdout or ""
                stderr += final_stderr or ""
        raise subprocess.TimeoutExpired(
            controlled, timeout_s, output=stdout, stderr=stderr,
        )
    return subprocess.CompletedProcess(
        controlled, process.returncode, stdout, stderr,
    )


def _openocd_target_probe(probe_serial: str) -> tuple[bool, str]:
    if not _openocd_jlink_available():
        return False, "随包 OpenOCD 不可用"
    command = [
        str(OPENOCD_EXE), "-s", str(OPENOCD_SCRIPTS),
        "-f", "interface/jlink.cfg", "-c", "transport select swd",
        "-f", "target/nrf52.cfg",
    ]
    if probe_serial:
        command += ["-c", f"adapter serial {probe_serial}"]
    command += [
        "-c", f"adapter speed {JLINK_SPEED_KHZ}",
        "-c", f"init; {_openocd_identity_command()}; shutdown",
    ]
    try:
        done = _run_openocd_bounded(command, timeout_s=10)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    output = f"{done.stdout}\n{done.stderr}"
    return done.returncode == 0 and _openocd_identity_verified(output), output


def _openocd_rtt_layout_probe(
    rtt_address: int, probe_serial: str,
) -> tuple[bool, bool, str]:
    """Read chip identity and the RTT signature without resetting or writing."""
    if not _openocd_jlink_available():
        return False, False, "随包 OpenOCD 不可用"
    command = [
        str(OPENOCD_EXE), "-s", str(OPENOCD_SCRIPTS),
        "-f", "interface/jlink.cfg", "-c", "transport select swd",
        "-f", "target/nrf52.cfg",
    ]
    if probe_serial:
        command += ["-c", f"adapter serial {probe_serial}"]
    command += [
        "-c", f"adapter speed {JLINK_SPEED_KHZ}",
        "-c", (
            "init; set sensus_info [read_memory "
            f"0x{NRF52833_INFO_PART_ADDRESS:08X} 32 1]; "
            "set sensus_rtt [read_memory "
            f"0x{rtt_address:08X} 32 3]; "
            "echo \"SENSUS_INFO_PART=[format 0x%08X "
            "[lindex $sensus_info 0]]\"; "
            "echo \"SENSUS_RTT_SIGNATURE=[format 0x%08X "
            "[lindex $sensus_rtt 0]] [format 0x%08X "
            "[lindex $sensus_rtt 1]] [format 0x%08X "
            "[lindex $sensus_rtt 2]]\"; shutdown"
        ),
    ]
    try:
        done = _run_openocd_bounded(command, timeout_s=10)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, False, str(exc)
    output = f"{done.stdout}\n{done.stderr}"
    target_ready = done.returncode == 0 and _openocd_identity_verified(output)
    rtt_pattern = re.compile(
        r"\bSENSUS_RTT_SIGNATURE\s*=\s*"
        r"(?:0x)?0*47474553\s+(?:0x)?0*52205245\s+(?:0x)?0*5454\b",
        flags=re.IGNORECASE,
    )
    return target_ready, target_ready and bool(rtt_pattern.search(output)), output


def _probe_jlink_target_status(
    probe_serial: str,
    *,
    force: bool = False,
    cancel_event: threading.Event | None = None,
) -> dict[str, Any]:
    """Check the target without reset/write and cache the result briefly."""
    now = time.monotonic()
    with JLINK_TARGET_CACHE_LOCK:
        cached = JLINK_TARGET_CACHE.get(probe_serial)
        if (
            cached
            and not force
            and now - float(cached.get("target_checked_at", 0.0))
            < JLINK_TARGET_CACHE_TTL_S
        ):
            return copy.deepcopy(cached)

    with JLINK_TARGET_PROBE_LOCK:
        now = time.monotonic()
        with JLINK_TARGET_CACHE_LOCK:
            cached = JLINK_TARGET_CACHE.get(probe_serial)
            if (
                cached
                and not force
                and now - float(cached.get("target_checked_at", 0.0))
                < JLINK_TARGET_CACHE_TTL_S
            ):
                return copy.deepcopy(cached)

        attempts: list[str] = []
        reachable = False
        backend = ""
        openocd_driver_missing = False
        openocd_probe_communication_error = False
        commander_probe_connected = False
        probe_busy = False
        if cancel_event is not None and cancel_event.is_set():
            return _unknown_jlink_target_status(probe_serial)
        openocd_available = _openocd_jlink_available()
        openocd_output = ""
        if openocd_available:
            reachable, openocd_output = _openocd_target_probe(probe_serial)
            probe_busy = reports_probe_busy(openocd_output)
            openocd_probe_communication_error = bool(
                openocd_reports_probe_communication_error(openocd_output)
            )
            if reachable:
                backend = "OpenOCD / libjaylink"
            else:
                tail = _meaningful_process_tail(openocd_output, limit=3)
                attempts.append("OpenOCD: " + (" | ".join(tail) or "目标无响应"))

        # Portable packages own a tested OpenOCD build. Use it first so an
        # unrelated or incompatible system Commander cannot delay discovery or
        # hide the WinUSB action. Commander is still the compatibility path for
        # computers whose probe remains on SEGGER's official Windows driver.
        commander_needed = bool(
            not reachable
            and JLINK_EXE.is_file()
            and (
                not openocd_available
                or openocd_reports_missing_driver(openocd_output)
                or openocd_probe_communication_error
            )
            and not probe_busy
            and not (
                cancel_event is not None and cancel_event.is_set()
            )
        )
        if commander_needed:
            reachable, output = probe_jlink_target(
                probe_serial,
                executable=JLINK_EXE,
                timeout_s=10,
            )
            if reachable:
                backend = "SEGGER J-Link Commander"
            else:
                probe_busy = probe_busy or reports_probe_busy(output)
                commander_probe_connected = commander_reports_probe_connected(
                    output
                )
                tail = _meaningful_process_tail(output, limit=3)
                attempts.append("SEGGER: " + (" | ".join(tail) or "目标无响应"))

        winusb_binding_available = bool(
            _IS_WIN
            and openocd_reports_missing_driver(openocd_output)
            and _jlink_requires_winusb(probe_serial)
            and not reachable
            and not probe_busy
        )
        openocd_driver_missing = bool(
            winusb_binding_available and not commander_probe_connected
        )
        probe_communication_failed = bool(
            openocd_probe_communication_error
            and not commander_probe_connected
        )

        if reachable:
            result = {
                "target_state": "reachable",
                "target_detail": f"nRF52833 已响应（{backend}）",
                "target_backend": backend,
                "target_failure": "",
                "target_diagnostics": "",
                "driver_state": "ready",
                "driver_action": "",
                "driver_message": "Windows J-Link 接口已就绪",
            }
        else:
            diagnostics = "；".join(attempts)
            helper_available = WINUSB_HELPER.is_file()
            target_failure = (
                "driver_missing"
                if openocd_driver_missing else (
                    "probe_busy"
                    if probe_busy else (
                        "probe_communication"
                        if probe_communication_failed else (
                            "target_unreachable" if attempts else "tool_unavailable"
                        )
                    )
                )
            )
            result = {
                "target_state": "unreachable" if attempts else "unknown",
                "target_detail": (
                    "J-Link 已识别，但 Windows 调试接口驱动尚未准备"
                    if openocd_driver_missing else (
                        "J-Link 正被其他软件占用；请关闭 Ozone、IDE、"
                        "J-Link Commander 或其他调试工具后刷新"
                        if probe_busy else (
                            "J-Link USB 通信超时；请断开 J-Link、目标板供电及 "
                            "3V3/VTref 10 秒后重插"
                            if probe_communication_failed else (
                                "J-Link 探针在线，但 nRF52833 未响应；"
                                "请检查板卡供电、SWD 排线和接口方向"
                                if attempts else "没有可用的 SWD 核对工具"
                            )
                        )
                    )
                ),
                "target_backend": (
                    "SEGGER J-Link Commander"
                    if commander_probe_connected else ""
                ),
                "target_failure": target_failure,
                "target_diagnostics": diagnostics,
                "driver_state": (
                    "missing" if openocd_driver_missing else (
                        "ready" if commander_probe_connected else "unknown"
                    )
                ),
                "driver_action": (
                    "install_winusb"
                    if winusb_binding_available and helper_available else ""
                ),
                "driver_message": (
                    "系统 SEGGER J-Link 驱动可访问探针；如确认板卡供电正常，"
                    "可在设备栏手动切换到随包 OpenOCD"
                    if commander_probe_connected and winusb_binding_available else (
                        "请点击右上角“选择设备”，再点击该 J-Link 的“准备 J-Link”"
                        if openocd_driver_missing and helper_available else (
                            "当前便携包缺少 WinUSB 准备工具，请更新软件"
                            if openocd_driver_missing else ""
                        )
                    )
                ),
            }
        result.update({
            "target_checked_at": time.monotonic(),
            "probe_serial": probe_serial,
        })
        with JLINK_TARGET_CACHE_LOCK:
            JLINK_TARGET_CACHE[probe_serial] = copy.deepcopy(result)
        return result


def _require_jlink_target(probe_serial: str) -> dict[str, Any]:
    """Require a fresh, read-only nRF52833 identity check before an operation."""
    status = _probe_jlink_target_status(probe_serial, force=True)
    if status.get("target_state") == "reachable":
        return status
    diagnostic_id = DIAGNOSTICS.record(
        "error", "device.jlink.target_unreachable",
        "J-Link probe is present but the nRF52833 target did not respond",
        probe_serial=probe_serial,
        target_detail=status.get("target_detail"),
        tool_output=status.get("target_diagnostics"),
    )
    error = RuntimeError(str(status.get("target_detail") or "J-Link 目标板无响应"))
    setattr(error, "diagnostic_id", diagnostic_id)
    raise error


def _annotate_target_states(
    devices: list[dict[str, Any]],
    *,
    refresh_jlink: bool,
    cancel_event: threading.Event | None = None,
) -> list[dict[str, Any]]:
    for device in devices:
        if device.get("kind") == "jlink":
            if device.get("target_failure") == "duplicate_probe_serial":
                continue
            probe_serial = str(device.get("probe_serial") or "")
            status = (
                (
                    _probe_jlink_target_status(probe_serial)
                    if cancel_event is None
                    else _probe_jlink_target_status(
                        probe_serial, cancel_event=cancel_event,
                    )
                )
                if refresh_jlink and probe_serial
                else _cached_jlink_target_status(probe_serial)
            )
            device.update({
                key: value for key, value in status.items()
                if key.startswith("target_") or key.startswith("driver_")
            })
        else:
            ready = bool(device.get("selectable"))
            device.update({
                "target_state": "reachable" if ready else "unknown",
                "target_detail": (
                    "USB DATA 与 SMP 已响应"
                    if ready else "USB 接口尚未核对完成"
                ),
                "target_backend": "USB CDC" if ready else "",
            })
    return devices


def _runtime_probe_request_id() -> str:
    """Return a firmware-safe request id that cannot match buffered output."""
    return f"ready-{time.monotonic_ns():x}"[-32:]


def _runtime_response_state(text: str, request_id: str) -> str:
    """Classify one tagged GET response without trusting buffered RTT output."""
    seen: set[str] = set()
    tagged = False
    confirmation_failed = False
    for raw_line in text.splitlines():
        event = parse_audit(raw_line)
        if event is None or str(event.get("req") or "") != request_id:
            continue
        tagged = True
        kind = str(event.get("kind") or "")
        if kind == "CFG_APPLIED":
            if event.get("src") == "get":
                seen.add(kind)
        elif kind == "CFG_DERIVED":
            seen.add(kind)
        elif kind == "CFG_CONFIRMED":
            if (
                event.get("src") == "get"
                and event.get("verify_ok") == 1
                and int(event.get("invalid_cfg") or 0) == 0
                and int(event.get("vdd_oor") or 0) == 0
            ):
                seen.add(kind)
            elif (
                event.get("verify_ok") == 0
                or int(event.get("invalid_cfg") or 0) != 0
                or int(event.get("vdd_oor") or 0) != 0
            ):
                confirmation_failed = True
    if {"CFG_APPLIED", "CFG_DERIVED", "CFG_CONFIRMED"}.issubset(seen):
        return "ready"
    if confirmation_failed:
        return "invalid"
    return "incomplete" if tagged else "missing"


def _runtime_response_verified(text: str, request_id: str) -> bool:
    """Return whether a complete tagged GET physically verified the AFE."""
    return _runtime_response_state(text, request_id) == "ready"


def _probe_serial_runtime_firmware(
    candidate: str, *, timeout_s: float = 4.0,
) -> tuple[str, str]:
    """Verify the tagged runtime protocol over DATA CDC without changing it."""
    try:
        import serial
    except ImportError:
        return "transport_error", "pyserial 不可用"
    request_id = _runtime_probe_request_id()
    received = bytearray()
    try:
        with serial.Serial(candidate, 115200, timeout=0.1,
                           write_timeout=0.5) as stream:
            stream.dtr = True
            try:
                stream.reset_input_buffer()
            except (AttributeError, OSError, ValueError):
                pass
            stream.write(f"GET req={request_id}\n".encode("ascii"))
            stream.flush()
            deadline = time.monotonic() + timeout_s
            while time.monotonic() < deadline:
                chunk = stream.read(512)
                if chunk:
                    received.extend(chunk)
                    text = received.decode("utf-8", "replace")
                    if _runtime_response_verified(text, request_id):
                        return "ready", text
    except (OSError, ValueError, serial.SerialException) as exc:
        return "transport_error", str(exc)
    text = received.decode("utf-8", "replace")
    detail = " | ".join(_meaningful_process_tail(text, limit=4))
    return _runtime_response_state(text, request_id), detail


def _free_local_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _stop_runtime_probe_bridge(process: Any) -> None:
    stop_jlink_rtt(process)


def _probe_openocd_runtime_firmware(
    rtt_address: int, probe_serial: str, *, timeout_s: float = 7.0,
) -> tuple[str, str]:
    """Verify runtime firmware through the bundled OpenOCD RTT server."""
    port = _free_local_tcp_port()
    process: Any | None = None
    client: socket.socket | None = None
    request_id = _runtime_probe_request_id()
    received = bytearray()
    try:
        process = start_jlink_rtt(rtt_address, probe_serial or None, port)
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline and client is None:
            if process.poll() is not None:
                return "transport_error", "OpenOCD RTT 进程在建立连接前退出"
            try:
                client = socket.create_connection(("127.0.0.1", port), timeout=0.3)
            except OSError:
                time.sleep(0.08)
        if client is None:
            return "transport_error", "OpenOCD RTT 服务未就绪"
        client.settimeout(0.2)
        request = f"GET req={request_id}\n".encode("ascii")
        next_request_at = 0.0
        while time.monotonic() < deadline:
            now = time.monotonic()
            if now >= next_request_at:
                # OpenOCD can expose its TCP listener before the RTT down
                # channel is ready. Reusing the tagged, read-only GET also
                # survives a full telemetry ring without changing hardware.
                client.sendall(request)
                next_request_at = now + 1.0
            try:
                chunk = client.recv(4096)
            except socket.timeout:
                continue
            if not chunk:
                break
            received.extend(chunk)
            text = received.decode("utf-8", "replace")
            if _runtime_response_verified(text, request_id):
                return "ready", text
    except (OSError, RuntimeError, subprocess.SubprocessError, SystemExit) as exc:
        return "transport_error", str(exc)
    finally:
        if client is not None:
            try:
                client.close()
            except OSError:
                pass
        if process is not None:
            _stop_runtime_probe_bridge(process)
    text = received.decode("utf-8", "replace")
    detail = " | ".join(_meaningful_process_tail(text, limit=4))
    return _runtime_response_state(text, request_id), detail


def _probe_jlink_runtime_firmware(
    metadata: dict[str, Any], *, timeout_s: float = 7.0,
) -> tuple[str, str]:
    """Verify the V4 runtime protocol before considering a Flash operation."""
    raw_address = metadata.get("rtt_address")
    try:
        rtt_address = (
            int(raw_address, 0) if isinstance(raw_address, str) else int(raw_address)
        )
    except (TypeError, ValueError):
        return "transport_error", "固件元数据缺少有效 RTT 地址"

    # The portable package owns this OpenOCD build and uses the same backend
    # for collection and flashing. Prefer it even when a system Commander is
    # installed so an unrelated SEGGER upgrade cannot change old-firmware
    # detection. A readable nRF52833 identity plus a missing RTT signature is
    # the only state that authorizes recovery flashing.
    openocd_failure = ""
    if _openocd_jlink_available():
        reachable, layout_ready, output = _openocd_rtt_layout_probe(
            rtt_address, JLINK_SERIAL,
        )
        detail = " | ".join(_meaningful_process_tail(output, limit=5))
        if reachable:
            if not layout_ready:
                return "missing", detail or "未找到通用固件 RTT 控制块"
            return _probe_openocd_runtime_firmware(
                rtt_address, JLINK_SERIAL, timeout_s=timeout_s,
            )
        openocd_failure = detail or "OpenOCD 无法读取 nRF52833 身份"

    if not JLINK_EXE.is_file():
        return "transport_error", openocd_failure or "没有可用的 J-Link 读取后端"

    reachable, probe_output = probe_jlink_target(
        JLINK_SERIAL or None, executable=JLINK_EXE, timeout_s=10,
    )
    if not reachable:
        detail = " | ".join(_meaningful_process_tail(probe_output, limit=5))
        failures = [item for item in (
            openocd_failure,
            detail or "J-Link 无法读取 nRF52833 身份",
        ) if item]
        return "transport_error", " || ".join(failures)

    request_id = _runtime_probe_request_id()
    transport: JLinkMemoryRTT | None = None
    received = bytearray()
    try:
        transport = JLinkMemoryRTT(
            rtt_address,
            JLINK_SERIAL or None,
            executable=JLINK_EXE,
        )
        transport.discard_pending_up()
        transport.sendall(f"GET req={request_id}\n".encode("ascii"))
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            chunk = transport.recv()
            if chunk:
                received.extend(chunk)
                text = received.decode("utf-8", "replace")
                if _runtime_response_verified(text, request_id):
                    return "ready", text
            else:
                time.sleep(0.05)
    except RTTControlBlockUnavailable as exc:
        return "missing", str(exc)
    except (OSError, RuntimeError, TimeoutError) as exc:
        return "transport_error", str(exc)
    finally:
        if transport is not None:
            transport.close()
    text = received.decode("utf-8", "replace")
    detail = " | ".join(_meaningful_process_tail(text, limit=4))
    return _runtime_response_state(text, request_id), detail


def _probe_serial_data_candidate(
    candidate: str, *, cancel_event: threading.Event | None = None,
) -> bool:
    """Read-only probe for one CDC candidate; never resets or writes firmware."""
    if cancel_event is not None and cancel_event.is_set():
        return False
    try:
        import serial
    except ImportError:
        return False
    try:
        with serial.Serial(candidate, 115200, timeout=0.1,
                           write_timeout=0.5) as stream:
            stream.dtr = True
            # USB1 firmware predates request IDs and only understands a bare
            # GET. Both forms are read-only and do not alter hardware state.
            for probe in (b"GET req=workstation-probe\n", b"GET\n"):
                if cancel_event is not None and cancel_event.is_set():
                    return False
                try:
                    stream.reset_input_buffer()
                except (AttributeError, OSError, ValueError):
                    pass
                stream.write(probe)
                stream.flush()
                deadline = time.monotonic() + 1.5
                received = bytearray()
                while time.monotonic() < deadline:
                    if cancel_event is not None and cancel_event.is_set():
                        return False
                    chunk = stream.read(256)
                    if chunk:
                        received.extend(chunk)
                        text = received.decode("utf-8", "replace")
                        if "CFG_CONFIRMED" in text:
                            return True
    except (OSError, ValueError, serial.SerialException):
        return False
    return False


def _discover_serial_data_port(*, force: bool = False) -> str | None:
    """Find the V5.1 DATA CDC without confusing it with SMP or J-Link CDC."""
    if SERIAL_DATA_PORT and not force:
        return SERIAL_DATA_PORT
    candidates = sorted(str(info.device) for info in _serial_port_infos())
    for candidate in candidates:
        if _probe_serial_data_candidate(candidate):
            return candidate
    return None


def _usb_physical_location(info: Any) -> str:
    """Normalize interface-qualified locations to one physical USB port."""
    return re.sub(
        r":\d+(?:\.\d+)?$", "",
        str(getattr(info, "location", "") or "").strip(),
    )


def _same_usb_device(left: Any, right: Any) -> bool:
    """Match CDC interfaces by stable USB identity, never by device suffix."""
    # VID/PID alone cannot distinguish two identical boards. Prefer a shared
    # serial or physical location; without either, keep interfaces separate
    # rather than accidentally merging two USB devices into one choice.
    shared_identity = False
    for field in ("serial_number", "location"):
        if field == "location":
            left_value = _usb_physical_location(left)
            right_value = _usb_physical_location(right)
        else:
            left_value = getattr(left, field, None)
            right_value = getattr(right, field, None)
        if left_value not in (None, "") and right_value not in (None, ""):
            shared_identity = True
            if left_value != right_value:
                return False
    if not shared_identity:
        return str(getattr(left, "device", "")) == str(getattr(right, "device", ""))
    return (
        getattr(left, "vid", None) == getattr(right, "vid", None)
        and getattr(left, "pid", None) == getattr(right, "pid", None)
    )


def _discover_serial_smp_port(
    data_port: str, *, force: bool = False
) -> str | None:
    """Find the sibling SMP CDC exposed by the same V5.1 USB device."""
    if SERIAL_SMP_PORT and not force:
        return SERIAL_SMP_PORT
    infos = _serial_port_infos()
    data_info = next(
        (info for info in infos if str(getattr(info, "device", "")) == data_port),
        None,
    )
    if data_info is None:
        return None
    siblings = [
        str(info.device) for info in infos
        if str(getattr(info, "device", "")) != data_port
        and _same_usb_device(info, data_info)
    ]
    return siblings[0] if len(siblings) == 1 else None


def _usb_identity(info: Any) -> str:
    """Build an ID that survives CDC interface renumbering and reboots."""
    # Linux may append an interface suffix (for example :1.0/:1.1) to the
    # same physical USB location; it must not split DATA and SMP into devices.
    location = _usb_physical_location(info)
    fields = (
        str(getattr(info, "vid", "") or "").lower(),
        str(getattr(info, "pid", "") or "").lower(),
        str(getattr(info, "serial_number", "") or "").strip(),
        location,
    )
    stable = "|".join(fields)
    if not any(fields[2:]):
        stable = f"{stable}|{getattr(info, 'device', '')}"
    digest = hashlib.sha256(stable.encode("utf-8")).hexdigest()[:16]
    return f"usb:{digest}"


def _usb_display_name(info: Any, *, data_port: str = "") -> str:
    serial = str(getattr(info, "serial_number", "") or "").strip()
    identifier = re.sub(r"[^0-9a-z]", "", serial, flags=re.IGNORECASE)[-4:]
    if not identifier and data_port:
        port_name = re.split(r"[/\\\\]", data_port)[-1]
        identifier = re.sub(
            r"[^0-9a-z]", "", port_name, flags=re.IGNORECASE
        )[-4:]
    return f"USB {identifier.upper()}" if identifier else "USB"


def _jlink_device_id(info: Any) -> str:
    serial = _jlink_probe_serial(info)
    if serial:
        return f"jlink:{serial}"
    digest = hashlib.sha256(_port_descriptor(info).encode("utf-8")).hexdigest()[:16]
    return f"jlink:{digest}"


def _jlink_display_name(info: Any) -> str:
    serial = _jlink_probe_serial(info)
    return f"J-Link · SN {serial}" if serial else "J-Link · 未读取序列号"


def _device_sort_key(device: dict[str, Any]) -> tuple[int, str]:
    return (0 if device.get("kind") == "usb" else 1, str(device.get("name", "")))


def _discover_devices(
    *, probe: bool = True, cancel_event: threading.Event | None = None,
) -> list[dict[str, Any]]:
    """Enumerate J-Link probes and V5.1 USB boards without flashing/resetting."""
    devices: list[dict[str, Any]] = []
    all_infos = _all_serial_port_infos()
    for info in all_infos:
        if not _is_jlink_port(info):
            continue
        probe_serial = _jlink_probe_serial(info)
        devices.append({
            "id": _jlink_device_id(info),
            "kind": "jlink",
            "transport": "rtt",
            "transport_label": "RTT / J-Link",
            "name": _jlink_display_name(info),
            "probe_serial": probe_serial,
            "cdc_port": str(getattr(info, "device", "") or ""),
            "serial_number": str(getattr(info, "serial_number", "") or ""),
            "vid": getattr(info, "vid", None),
            "pid": getattr(info, "pid", None),
            "location": str(getattr(info, "location", "") or ""),
            "selectable": bool(probe_serial),
        })

    duplicate_jlink_ids = {
        device["id"] for device in devices
        if sum(candidate["id"] == device["id"] for candidate in devices) > 1
    }
    for device in devices:
        if device["id"] not in duplicate_jlink_ids:
            continue
        physical = str(device.get("location") or device.get("cdc_port") or "")
        suffix = hashlib.sha256(physical.encode("utf-8")).hexdigest()[:8]
        device["id"] = f"{device['id']}:{suffix}"
        device["selectable"] = False
        device["target_state"] = "unreachable"
        device["target_failure"] = "duplicate_probe_serial"
        device["target_detail"] = (
            "检测到重复的 J-Link 序列号，无法可靠选择；"
            "请只保留一只探头"
        )

    candidates = [info for info in all_infos if not _is_jlink_port(info)]
    groups: list[list[Any]] = []
    for info in candidates:
        group = next((items for items in groups if _same_usb_device(items[0], info)), None)
        if group is None:
            groups.append([info])
        else:
            group.append(info)
    for group in groups:
        data_info: Any | None = None
        if probe:
            # Once a DATA interface has been verified, probe it first. macOS
            # currently enumerates the sibling SMP interface before DATA; trying
            # SMP first adds a three-second timeout to every device refresh.
            ordered_group = sorted(
                group,
                key=lambda info: (
                    str(getattr(info, "device", "") or "")
                    != SERIAL_DATA_PORT,
                    str(getattr(info, "device", "") or ""),
                ),
            )
            for info in ordered_group:
                if cancel_event is not None and cancel_event.is_set():
                    break
                candidate = str(getattr(info, "device", "") or "")
                responsive = (
                    _probe_serial_data_candidate(candidate)
                    if cancel_event is None
                    else _probe_serial_data_candidate(
                        candidate, cancel_event=cancel_event,
                    )
                )
                if responsive:
                    data_info = info
                    break
        elif SERIAL_DATA_PORT:
            # A previously verified DATA path is safe to describe without
            # opening the port again. This keeps a hot-plug refresh instant.
            data_info = next(
                (
                    info for info in group
                    if str(getattr(info, "device", "") or "") == SERIAL_DATA_PORT
                ),
                None,
            )
        data_port = str(getattr(data_info, "device", "") or "") if data_info else ""
        representative = data_info or group[0]
        sibling_ports = [
            str(getattr(info, "device", "") or "")
            for info in group if info is not data_info
        ]
        smp_port = sibling_ports[0] if data_port and len(sibling_ports) == 1 else ""
        identity = _usb_identity(representative)
        devices.append({
            "id": identity,
            "kind": "usb",
            "transport": "serial",
            "transport_label": "USB DATA CDC",
            "name": _usb_display_name(representative, data_port=data_port),
            "serial_number": str(getattr(representative, "serial_number", "") or ""),
            "vid": getattr(representative, "vid", None),
            "pid": getattr(representative, "pid", None),
            "location": str(getattr(representative, "location", "") or ""),
            "data_port": data_port,
            "smp_port": smp_port or "",
            "interfaces": [str(getattr(info, "device", "") or "") for info in group],
            "selectable": bool(data_port and smp_port),
            "probe_required": not probe and not data_port,
        })
    return _annotate_target_states(
        sorted(devices, key=_device_sort_key), refresh_jlink=False
    )


def _discover_devices_with_probe(
    *, cancel_event: threading.Event | None = None,
) -> list[dict[str, Any]]:
    """Serialize port probes so a hot-plug refresh cannot race selection."""
    with DEVICE_PROBE_LOCK:
        return _discover_devices(probe=True, cancel_event=cancel_event)


def _selected_device_copy() -> dict[str, Any] | None:
    with DEVICE_SELECTION_LOCK:
        return copy.deepcopy(SELECTED_DEVICE) if SELECTED_DEVICE else None


def _ports_for_identity(identity: str) -> list[Any]:
    """Return every CDC interface belonging to one physical USB board."""
    return [
        info for info in _serial_port_infos()
        if _usb_identity(info) == identity
    ]


def _ordered_usb_interfaces(
    infos: list[Any], preferred_data_port: str,
) -> list[Any]:
    """Try the previously verified DATA interface before sibling CDC ports."""
    return sorted(
        infos,
        key=lambda info: (
            str(getattr(info, "device", "") or "") != preferred_data_port,
            str(getattr(info, "device", "") or ""),
        ),
    )


def _find_jlink_for_id(device_id: str) -> Any | None:
    return next(
        (info for info in _all_serial_port_infos()
         if _is_jlink_port(info) and _jlink_device_id(info) == device_id),
        None,
    )


def _jlink_usb_ids(info: Any) -> tuple[int, int]:
    vid = getattr(info, "vid", None)
    pid = getattr(info, "pid", None)
    if vid is None or pid is None:
        descriptor = _port_descriptor(info)
        match = re.search(
            r"VID(?:_|:PID=)([0-9A-F]{4})(?:&PID_|:)([0-9A-F]{4})",
            descriptor,
            flags=re.IGNORECASE,
        )
        if match is not None:
            vid, pid = int(match.group(1), 16), int(match.group(2), 16)
    if vid is None or pid is None:
        raise RuntimeError("无法读取该 J-Link 的 USB VID/PID")
    if int(vid) != JLINK_VENDOR_ID:
        raise RuntimeError("只能准备 SEGGER J-Link 的 Windows 驱动")
    return int(vid), int(pid)


def _jlink_requires_winusb(probe_serial: str) -> bool:
    """Confirm that OpenOCD can repair the exact supported PnP interface."""
    def has_target_binding(vid: int, pid: int) -> bool:
        bindings = jlink_bindings(vid, pid)
        exact = [
            binding for binding in bindings
            if binding.probe_serial == probe_serial
        ]
        if exact:
            return any(not binding.ready for binding in exact)
        # With one physical interface there is no ambiguity even when Windows
        # omitted its serial property. Multiple anonymous interfaces are never
        # eligible for an automatic driver action.
        return bool(
            len(bindings) == 1
            and not bindings[0].probe_serial
            and not bindings[0].ready
        )

    with DEVICE_DISCOVERY_LOCK:
        cached = next(
            (
                copy.deepcopy(candidate) for candidate in DEVICE_DISCOVERY_CACHE
                if candidate.get("kind") == "jlink"
                and str(candidate.get("probe_serial") or "") == probe_serial
            ),
            None,
        )
    if cached is not None:
        try:
            vid, pid = int(cached["vid"]), int(cached["pid"])
            return has_target_binding(vid, pid)
        except (KeyError, OSError, RuntimeError, TypeError, ValueError):
            pass
    info = next(
        (
            candidate for candidate in _all_serial_port_infos()
            if _is_jlink_port(candidate)
            and _jlink_probe_serial(candidate) == probe_serial
        ),
        None,
    )
    if info is None:
        return False
    try:
        vid, pid = _jlink_usb_ids(info)
        return has_target_binding(vid, pid)
    except (OSError, RuntimeError, ValueError):
        return False


def _prepare_jlink_winusb(
    device_id: str,
    *,
    status_callback: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    if not _IS_WIN:
        raise RuntimeError("WinUSB 准备仅适用于 Windows")
    if not WINUSB_HELPER.is_file():
        raise RuntimeError("当前便携包缺少 WinUSB 准备工具，请更新软件")
    if status_callback is not None:
        status_callback("正在核对所选 J-Link 调试接口")
    with DEVICE_DISCOVERY_LOCK:
        cached_device = next(
            (
                copy.deepcopy(candidate) for candidate in DEVICE_DISCOVERY_CACHE
                if candidate.get("id") == device_id
                and candidate.get("kind") == "jlink"
            ),
            None,
        )
    info = cached_device or _find_jlink_for_id(device_id)
    if info is None:
        raise ValueError("J-Link 已断开，请重新插入后刷新")
    probe_serial = (
        str(info.get("probe_serial") or "")
        if isinstance(info, dict) else _jlink_probe_serial(info)
    )
    if not probe_serial:
        raise RuntimeError("该 J-Link 没有可用的探针序列号")
    if isinstance(info, dict):
        try:
            vid, pid = int(info["vid"]), int(info["pid"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("无法读取该 J-Link 的 USB VID/PID") from exc
    else:
        vid, pid = _jlink_usb_ids(info)
    bindings = jlink_bindings(vid, pid)
    if not bindings:
        raise RuntimeError(
            "没有找到可准备的 J-Link 调试接口；请重新插拔后刷新"
        )
    if len(bindings) > 1:
        raise RuntimeError(
            "检测到多只同型号 J-Link；准备驱动时请只保留目标探头，"
            "完成后可重新插回其他探头"
        )
    binding = bindings[0]
    if binding.probe_serial and binding.probe_serial != probe_serial:
        raise RuntimeError(
            "Windows 调试接口与所选 J-Link 身份不一致；"
            "请只保留目标探头后刷新"
        )
    interfaces = [binding.interface]

    DIAGNOSTICS.record(
        "info", "device.jlink.driver_install.started",
        "J-Link WinUSB preparation started",
        device_id=device_id, probe_serial=probe_serial,
        vid=f"{vid:04x}", pid=f"{pid:04x}", helper=WINUSB_HELPER,
    )
    started_at = time.monotonic()
    try:
        if status_callback is not None:
            status_callback(
                "接口核对完成，即将弹出 Windows 管理员权限提示"
            )
        installation = install_winusb_driver(
            WINUSB_HELPER, vid=vid, pid=pid, interfaces=interfaces,
            status_callback=status_callback,
        )
    except Exception as exc:
        diagnostic_id = DIAGNOSTICS.exception(
            "device.jlink.driver_install.failed",
            "J-Link WinUSB preparation failed",
            exc, device_id=device_id, probe_serial=probe_serial,
            vid=f"{vid:04x}", pid=f"{pid:04x}",
            duration_ms=round((time.monotonic() - started_at) * 1000, 1),
        )
        try:
            setattr(exc, "diagnostic_id", diagnostic_id)
        except (AttributeError, TypeError):
            pass
        raise

    with JLINK_TARGET_CACHE_LOCK:
        JLINK_TARGET_CACHE.pop(probe_serial, None)
    if status_callback is not None:
        status_callback("驱动已处理，正在等待 J-Link 重新连接")
    # wdi-simple returns before every Windows PnP view has converged. It also
    # runs elevated, so the helper restarts the exact interface and scans for
    # devices before returning. Verify the service binding directly here;
    # requiring the CDC descriptor would incorrectly reject legacy ARM-OBs.
    stable_ready = 0
    seen_after_install = False
    last_binding_detail = "设备正在重新枚举"
    for _ in range(60):
        try:
            current_bindings = jlink_bindings(vid, pid)
        except (OSError, RuntimeError, ValueError) as exc:
            current_bindings = []
            last_binding_detail = str(exc)
        matching = next(
            (
                current for current in current_bindings
                if current.interface.instance_id.lower()
                == binding.interface.instance_id.lower()
            ),
            None,
        )
        if matching is None and len(current_bindings) == 1:
            candidate = current_bindings[0]
            if (
                not binding.probe_serial
                or not candidate.probe_serial
                or candidate.probe_serial == binding.probe_serial
            ):
                matching = candidate
        if matching is not None:
            seen_after_install = True
            last_binding_detail = (
                f"Status={matching.status or 'unknown'}, "
                f"Problem={matching.problem_code}, "
                f"Service={matching.service or 'none'}"
            )
        if matching is not None and matching.ready:
            stable_ready += 1
            if stable_ready >= 2:
                break
        else:
            stable_ready = 0
        time.sleep(0.5)
    if stable_ready < 2:
        action = (
            "请断开 J-Link 10 秒后重新插入，再点击刷新"
            if not seen_after_install
            else "请点击刷新；若仍未就绪，请断开 J-Link 10 秒后重插"
        )
        raise RuntimeError(
            "WinUSB 已安装，但 Windows 未确认调试接口稳定恢复"
            f"（{last_binding_detail}）；{action}"
        )

    status: dict[str, Any] = _unknown_jlink_target_status(probe_serial)
    if status_callback is not None:
        status_callback("J-Link 已重新枚举，正在核对目标板")
    for attempt in range(3):
        if attempt:
            time.sleep(1.0)
        status = _probe_jlink_target_status(probe_serial, force=True)
        if status.get("target_state") == "reachable":
            break
        if status.get("target_failure") not in {
            "driver_missing", "probe_communication",
        }:
            break

    with DEVICE_DISCOVERY_LOCK:
        devices = copy.deepcopy(DEVICE_DISCOVERY_CACHE)
    prepared = next(
        (device for device in devices if device.get("probe_serial") == probe_serial),
        None,
    )
    if prepared is None:
        prepared = {
            "id": device_id,
            "kind": "jlink",
            "transport": "rtt",
            "transport_label": "RTT / J-Link",
            "name": f"J-Link · SN {probe_serial}",
            "probe_serial": probe_serial,
            "serial_number": probe_serial,
            "vid": vid,
            "pid": pid,
            "selectable": True,
        }
        devices.append(prepared)
    prepared.update(status)
    _remember_device_discovery(devices)
    _start_device_discovery()
    DIAGNOSTICS.record(
        "info", "device.jlink.driver_install.completed",
        "J-Link WinUSB preparation completed",
        device_id=device_id, probe_serial=probe_serial,
        installation=installation,
        target_state=prepared.get("target_state"),
        target_detail=prepared.get("target_detail"),
        duration_ms=round((time.monotonic() - started_at) * 1000, 1),
    )
    result = _devices_payload_from_devices(devices)
    result["message"] = (
        "J-Link 已准备并连上 nRF52833"
        if prepared.get("target_state") == "reachable"
        else "WinUSB 已准备；请检查目标板供电和 SWD 连线"
    )
    return result


def _jlink_driver_task_snapshot() -> dict[str, Any]:
    with JLINK_DRIVER_TASK_LOCK:
        snapshot = copy.deepcopy(JLINK_DRIVER_TASK)
    snapshot["running"] = JLINK_DRIVER_INSTALL_LOCK.locked()
    return snapshot


def _set_jlink_driver_task(**values: Any) -> dict[str, Any]:
    with JLINK_DRIVER_TASK_LOCK:
        JLINK_DRIVER_TASK.update(values)
        return copy.deepcopy(JLINK_DRIVER_TASK)


def _run_jlink_driver_task(device_id: str) -> None:
    try:
        _set_jlink_driver_task(
            message="正在等待后台设备核对结束",
        )
        with APP.operation_lock:
            _ensure_not_shutting_down()
            if not APP.hardware_idle():
                raise RuntimeError("测量或硬件参数更新期间不能准备 J-Link 驱动")
            _set_jlink_driver_task(
                message="正在核对 Windows 中的 J-Link 调试接口",
            )
            result = _prepare_jlink_winusb(
                device_id,
                status_callback=lambda message: _set_jlink_driver_task(
                    message=message,
                ),
            )
        _set_jlink_driver_task(
            state="succeeded",
            message=str(result.get("message") or "J-Link Windows 驱动已准备"),
            error="",
            diagnostic_id="",
            finished_at=time.time(),
        )
    except Exception as exc:
        diagnostic_id = str(getattr(exc, "diagnostic_id", "") or "")
        if not diagnostic_id:
            diagnostic_id = DIAGNOSTICS.exception(
                "device.jlink.driver_task.failed",
                "Background J-Link driver preparation failed",
                exc,
                device_id=device_id,
            )
        _set_jlink_driver_task(
            state="error",
            message="",
            error=str(exc),
            diagnostic_id=diagnostic_id,
            finished_at=time.time(),
        )
    finally:
        JLINK_DRIVER_INSTALL_LOCK.release()
        DEVICE_DISCOVERY_CANCEL.clear()
        _start_device_discovery()


def _start_jlink_driver_task(device_id: str) -> dict[str, Any]:
    _ensure_not_shutting_down()
    if not APP.hardware_idle():
        raise RuntimeError("测量或硬件参数更新期间不能准备 J-Link 驱动")
    if not JLINK_DRIVER_INSTALL_LOCK.acquire(blocking=False):
        return _jlink_driver_task_snapshot()
    if SHUTDOWN_INTENT.is_set():
        JLINK_DRIVER_INSTALL_LOCK.release()
        raise RuntimeError("应用正在安全退出，不能再启动 J-Link 驱动准备")
    DEVICE_DISCOVERY_CANCEL.set()
    _set_jlink_driver_task(
        state="running",
        device_id=device_id,
        message="正在等待后台设备核对结束",
        error="",
        diagnostic_id="",
        started_at=time.time(),
        finished_at=None,
    )
    worker = threading.Thread(
        target=_run_jlink_driver_task,
        args=(device_id,),
        name="jlink-driver-preparation",
        daemon=True,
    )
    try:
        worker.start()
    except Exception:
        JLINK_DRIVER_INSTALL_LOCK.release()
        DEVICE_DISCOVERY_CANCEL.clear()
        raise
    return _jlink_driver_task_snapshot()


def _set_device_selection(device: dict[str, Any] | None) -> None:
    """Apply a manual device to the transport globals while the app is idle."""
    global HARDWARE_TRANSPORT, SERIAL_DATA_PORT, SERIAL_SMP_PORT, JLINK_SERIAL
    with DEVICE_SELECTION_LOCK:
        global SELECTED_DEVICE
        SELECTED_DEVICE = copy.deepcopy(device) if device else None
    if device is None:
        JLINK_SERIAL = CONFIGURED_JLINK_SERIAL
        if HARDWARE_TRANSPORT_REQUESTED == "rtt":
            HARDWARE_TRANSPORT = "rtt"
        else:
            _refresh_usb_transport()
        DIAGNOSTICS.record(
            "info", "device.selection.auto", "Device selection returned to auto",
            transport=HARDWARE_TRANSPORT,
        )
        return
    if device.get("kind") == "usb":
        SERIAL_DATA_PORT = str(device.get("data_port") or "")
        SERIAL_SMP_PORT = str(device.get("smp_port") or "")
        HARDWARE_TRANSPORT = "serial"
        DIAGNOSTICS.record(
            "info", "device.selection.changed", "USB device selected",
            device_id=device.get("id"), device_name=device.get("name"),
            transport=HARDWARE_TRANSPORT,
        )
        return
    JLINK_SERIAL = str(device.get("probe_serial") or "")
    SERIAL_DATA_PORT = ""
    SERIAL_SMP_PORT = ""
    HARDWARE_TRANSPORT = "rtt"
    DIAGNOSTICS.record(
        "info", "device.selection.changed", "J-Link device selected",
        device_id=device.get("id"), device_name=device.get("name"),
        transport=HARDWARE_TRANSPORT,
    )


def _devices_payload_from_devices(
    devices: list[dict[str, Any]],
    *,
    busy: bool = False,
    probing: bool = False,
    error: str = "",
) -> dict[str, Any]:
    selected = _selected_device_copy()
    selected_id = selected.get("id") if selected else None
    if selected_id:
        present = next(
            (device for device in devices if device.get("id") == selected_id), None,
        )
        if present is not None:
            selected = {**present, "present": True}
        else:
            selected = {
                **(selected or {}),
                "present": False,
                "selectable": False,
                "target_state": "unreachable",
                "target_detail": "所选设备已断开",
            }
    payload = {
        "devices": devices,
        "selected_device_id": selected_id,
        "selected_device": selected,
        "selection_mode": "manual" if selected_id else "auto",
        "busy": busy,
        "driver_preparing": JLINK_DRIVER_INSTALL_LOCK.locked(),
        "driver_task": _jlink_driver_task_snapshot(),
        "probing": probing,
        **_transport_status(),
    }
    if error:
        payload["error"] = error
    return payload


def _remember_device_discovery(
    devices: list[dict[str, Any]], error: str = ""
) -> None:
    global DEVICE_DISCOVERY_CACHE, DEVICE_DISCOVERY_AT, DEVICE_DISCOVERY_ERROR
    with DEVICE_DISCOVERY_LOCK:
        DEVICE_DISCOVERY_CACHE = copy.deepcopy(devices)
        DEVICE_DISCOVERY_AT = time.monotonic()
        DEVICE_DISCOVERY_ERROR = error
    if not error:
        live_serials = {
            str(device.get("probe_serial") or "")
            for device in devices if device.get("kind") == "jlink"
        }
        with JLINK_TARGET_CACHE_LOCK:
            for serial in list(JLINK_TARGET_CACHE):
                if serial not in live_serials:
                    JLINK_TARGET_CACHE.pop(serial, None)


def _run_device_discovery() -> None:
    global DEVICE_DISCOVERY_THREAD
    global DEVICE_DISCOVERY_LOG_SIGNATURE, DEVICE_DISCOVERY_LOG_ERROR
    error = ""
    devices: list[dict[str, Any]] = []
    cancelled = False
    app = globals().get("APP")
    operation_lock = getattr(app, "operation_lock", None)
    driver_preparing = JLINK_DRIVER_INSTALL_LOCK.locked()
    operation_acquired = bool(
        not driver_preparing
        and operation_lock is not None
        and operation_lock.acquire(blocking=False)
    )
    try:
        if driver_preparing or JLINK_DRIVER_INSTALL_LOCK.locked():
            with DEVICE_DISCOVERY_LOCK:
                devices = copy.deepcopy(DEVICE_DISCOVERY_CACHE)
        elif operation_lock is not None and not operation_acquired:
            with DEVICE_DISCOVERY_LOCK:
                devices = copy.deepcopy(DEVICE_DISCOVERY_CACHE)
        elif app is not None and not app.hardware_idle():
            with DEVICE_DISCOVERY_LOCK:
                devices = copy.deepcopy(DEVICE_DISCOVERY_CACHE)
        else:
            devices = _discover_devices_with_probe(
                cancel_event=DEVICE_DISCOVERY_CANCEL,
            )
            cancelled = DEVICE_DISCOVERY_CANCEL.is_set()
            if cancelled:
                with DEVICE_DISCOVERY_LOCK:
                    devices = copy.deepcopy(DEVICE_DISCOVERY_CACHE)
            # A driver request may arrive while CDC probing is in progress.
            # Skip the slower OpenOCD target check so the UAC request can take
            # ownership of the operation lock as soon as enumeration finishes.
            _annotate_target_states(
                devices,
                refresh_jlink=not JLINK_DRIVER_INSTALL_LOCK.locked(),
                cancel_event=DEVICE_DISCOVERY_CANCEL,
            )
            cancelled = cancelled or DEVICE_DISCOVERY_CANCEL.is_set()
            if cancelled:
                with DEVICE_DISCOVERY_LOCK:
                    devices = copy.deepcopy(DEVICE_DISCOVERY_CACHE)
    except (OSError, RuntimeError, ValueError) as exc:
        error = str(exc)
        # Preserve the last known list during a transient USB re-enumeration;
        # a short unplug/replug should not make the dialog lose the J-Link.
        with DEVICE_DISCOVERY_LOCK:
            devices = copy.deepcopy(DEVICE_DISCOVERY_CACHE)
    finally:
        if operation_acquired:
            operation_lock.release()
        if not cancelled:
            _remember_device_discovery(devices, error)
        signature = tuple(sorted(
            (
                str(device.get("id") or ""),
                bool(device.get("selectable")),
                str(device.get("data_port") or ""),
                str(device.get("probe_serial") or ""),
                str(device.get("target_state") or ""),
            )
            for device in devices
        ))
        if error and error != DEVICE_DISCOVERY_LOG_ERROR:
            DIAGNOSTICS.record(
                "warning", "device.discovery.failed", "Device discovery failed",
                error=error, cached_device_count=len(devices),
            )
        if not error and signature != DEVICE_DISCOVERY_LOG_SIGNATURE:
            DIAGNOSTICS.record(
                "info", "device.discovery.changed", "Detected hardware changed",
                devices=[{
                    "id": device.get("id"),
                    "name": device.get("name"),
                    "kind": device.get("kind"),
                    "selectable": device.get("selectable"),
                    "target_state": device.get("target_state"),
                    "target_detail": device.get("target_detail"),
                    "target_diagnostics": device.get("target_diagnostics"),
                } for device in devices],
            )
        DEVICE_DISCOVERY_LOG_SIGNATURE = signature
        DEVICE_DISCOVERY_LOG_ERROR = error
        with DEVICE_DISCOVERY_LOCK:
            DEVICE_DISCOVERY_THREAD = None


def _start_device_discovery() -> bool:
    global DEVICE_DISCOVERY_THREAD
    with DEVICE_DISCOVERY_LOCK:
        if DEVICE_DISCOVERY_THREAD is not None and DEVICE_DISCOVERY_THREAD.is_alive():
            return False
        if JLINK_DRIVER_INSTALL_LOCK.locked():
            return False
        DEVICE_DISCOVERY_CANCEL.clear()
        worker = threading.Thread(
            target=_run_device_discovery,
            name="device-discovery",
            daemon=True,
        )
        DEVICE_DISCOVERY_THREAD = worker
        worker.start()
        return True


def _cached_devices_payload(*, busy: bool = False) -> dict[str, Any]:
    """Return a fast device snapshot while refreshing CDC probes in background."""
    global DEVICE_DISCOVERY_CACHE, DEVICE_DISCOVERY_AT, DEVICE_DISCOVERY_ERROR
    now = time.monotonic()
    with DEVICE_DISCOVERY_LOCK:
        devices = copy.deepcopy(DEVICE_DISCOVERY_CACHE)
        updated_at = DEVICE_DISCOVERY_AT
        error = DEVICE_DISCOVERY_ERROR
        probing = (
            DEVICE_DISCOVERY_THREAD is not None
            and DEVICE_DISCOVERY_THREAD.is_alive()
        )
    had_cache = bool(devices)
    stale = now - updated_at >= DEVICE_DISCOVERY_TTL_S
    if not busy and (stale or not had_cache):
        probing = _start_device_discovery() or probing
    return _devices_payload_from_devices(
        devices, busy=busy, probing=probing, error=error
    )


def _devices_payload(*, probe: bool = True) -> dict[str, Any]:
    devices = (
        _discover_devices_with_probe()
        if probe
        else _discover_devices(probe=False)
    )
    if probe:
        _annotate_target_states(devices, refresh_jlink=True)
        _remember_device_discovery(devices)
    return _devices_payload_from_devices(devices)


def _refresh_usb_transport() -> None:
    """Refresh USB CDC paths immediately before an idle hardware operation."""
    global HARDWARE_TRANSPORT, SERIAL_DATA_PORT, SERIAL_SMP_PORT, JLINK_SERIAL
    selected = _selected_device_copy()
    if HARDWARE_TRANSPORT_REQUESTED == "rtt" and selected is None:
        return
    if selected is not None:
        if selected.get("kind") == "jlink":
            info = _find_jlink_for_id(str(selected.get("id") or ""))
            if info is None:
                raise RuntimeError(f"手动选择的 {selected.get('name', 'J-Link')} 已断开")
            JLINK_SERIAL = _jlink_probe_serial(info)
            if not JLINK_SERIAL:
                raise RuntimeError("手动选择的 J-Link 没有可用序列号")
            HARDWARE_TRANSPORT = "rtt"
            return
        identity = str(selected.get("id") or "")
        preferred_data_port = str(selected.get("data_port") or "")
        attempted_ports: list[str] = []
        data_port = ""
        smp_port = ""
        # Device discovery also opens CDC ports. Serialize the final validation
        # so a background refresh cannot make a healthy selected board appear
        # unresponsive immediately before flashing or measurement.
        with DEVICE_PROBE_LOCK:
            infos = _ports_for_identity(identity)
            if not infos:
                raise RuntimeError(
                    f"手动选择的 {selected.get('name', 'USB 设备')} 已断开"
                )
            for info in _ordered_usb_interfaces(infos, preferred_data_port):
                candidate = str(getattr(info, "device", "") or "")
                if not candidate:
                    continue
                attempted_ports.append(candidate)
                if _probe_serial_data_candidate(candidate):
                    data_port = candidate
                    break
            if data_port:
                smp_port = _discover_serial_smp_port(data_port, force=True) or ""
        if not data_port:
            DIAGNOSTICS.record(
                "warning", "device.selection.validation_failed",
                "Selected USB device did not expose a responsive DATA interface",
                device_id=identity, device_name=selected.get("name"),
                preferred_data_port=preferred_data_port,
                attempted_ports=attempted_ports,
            )
            raise RuntimeError(
                f"手动选择的 {selected.get('name', 'USB 设备')} 未响应 DATA CDC"
            )
        if not smp_port:
            raise RuntimeError("手动选择的 USB 设备缺少同一设备的 SMP CDC")
        SERIAL_DATA_PORT = data_port
        SERIAL_SMP_PORT = smp_port
        HARDWARE_TRANSPORT = "serial"
        DIAGNOSTICS.record(
            "info", "device.selection.validated",
            "Selected USB DATA and SMP interfaces validated",
            device_id=identity, device_name=selected.get("name"),
            data_port=data_port, smp_port=smp_port,
            attempted_ports=attempted_ports,
        )
        return
    if HARDWARE_TRANSPORT_REQUESTED == "auto":
        candidates = [
            device for device in _discover_devices_with_probe()
            if device.get("selectable")
        ]
        if len(candidates) > 1:
            raise RuntimeError("检测到多个可用设备，请先点击“选择设备”")
        if len(candidates) == 1:
            device = candidates[0]
            if device.get("kind") == "jlink":
                JLINK_SERIAL = str(device.get("probe_serial") or "")
                SERIAL_DATA_PORT = ""
                SERIAL_SMP_PORT = ""
                HARDWARE_TRANSPORT = "rtt"
            else:
                SERIAL_DATA_PORT = str(device.get("data_port") or "")
                SERIAL_SMP_PORT = str(device.get("smp_port") or "")
                HARDWARE_TRANSPORT = "serial"
            return
    discovered = _discover_serial_data_port(force=True)
    if discovered:
        SERIAL_DATA_PORT = discovered
        SERIAL_SMP_PORT = _discover_serial_smp_port(discovered, force=True) or ""
        HARDWARE_TRANSPORT = "serial"
        return
    if HARDWARE_TRANSPORT_REQUESTED == "serial" or HARDWARE_TRANSPORT == "serial":
        raise RuntimeError(
            "USB 模式未找到 V5.1 DATA CDC，请重新插拔 USB 后重试"
        )
    if _serial_port_infos():
        raise RuntimeError(
            "检测到 USB CDC，但未找到可用的 V5.1 DATA CDC；"
            "请确认固件已更新并重新插拔 USB"
        )
    SERIAL_DATA_PORT = ""
    SERIAL_SMP_PORT = ""
    HARDWARE_TRANSPORT = "rtt"


def _wait_for_usb_transport_ready(timeout_s: float = 12.0) -> None:
    """Wait for the application CDC interfaces after an MCUboot update."""
    deadline = time.monotonic() + max(0.0, timeout_s)
    last_error: RuntimeError | None = None
    while True:
        try:
            _refresh_usb_transport()
            return
        except RuntimeError as exc:
            last_error = exc
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(0.2, remaining))
    detail = str(last_error or "DATA CDC 未出现")
    raise RuntimeError(f"USB 固件已上传，但应用 DATA CDC 未恢复：{detail}")


def _usb_physical_snapshot_for_port(port: str) -> dict[str, Any]:
    infos = _serial_port_infos()
    info = next(
        (
            candidate for candidate in infos
            if str(getattr(candidate, "device", "") or "") == port
        ),
        None,
    )
    if info is None:
        return {}
    identity = _usb_identity(info)
    return {
        "serial_number": str(
            getattr(info, "serial_number", "") or ""
        ).strip(),
        "location": _usb_physical_location(info),
        "ports": sorted(
            str(getattr(candidate, "device", "") or "")
            for candidate in infos
            if _usb_identity(candidate) == identity
        ),
    }


def _wait_for_bootloader_smp_port(
    previous_port: str,
    physical: dict[str, Any],
    *,
    timeout_s: float = 20.0,
) -> str:
    """Follow one board's CDC path across application-to-MCUboot re-enumeration."""
    stable_identity = bool(
        physical.get("serial_number") or physical.get("location")
    )
    if not stable_identity:
        return previous_port
    try:
        from serial.tools import list_ports
    except ImportError:
        return previous_port
    started_at = time.monotonic()
    deadline = started_at + max(0.0, timeout_s)
    original_ports = {
        str(port) for port in physical.get("ports", []) if str(port)
    }
    observed_transition = not original_ports
    stable_port = ""
    stable_count = 0
    while True:
        candidates: list[Any] = []
        for info in list_ports.comports():
            if _is_jlink_port(info):
                continue
            serial = str(getattr(info, "serial_number", "") or "").strip()
            location = _usb_physical_location(info)
            serial_match = bool(
                physical.get("serial_number")
                and serial == physical["serial_number"]
            )
            location_match = bool(
                physical.get("location")
                and location == physical["location"]
            )
            # Physical USB location wins when available. Clone boards may ship
            # with the same serial number, so an OR match could upload firmware
            # to the wrong board while the selected one is re-enumerating.
            same_board = (
                location_match
                if physical.get("location")
                else serial_match
            )
            if same_board:
                candidates.append(info)
        live_ports = {
            str(getattr(info, "device", "") or "") for info in candidates
        }
        if (
            any("mcuboot" in _port_descriptor(info).lower() for info in candidates)
            or any(port not in original_ports for port in live_ports)
            or bool(original_ports - live_ports)
        ):
            observed_transition = True
        candidates.sort(key=lambda info: (
            "mcuboot" not in _port_descriptor(info).lower(),
            str(getattr(info, "device", "") or "") in original_ports,
            str(getattr(info, "device", "") or "") == SERIAL_DATA_PORT,
            str(getattr(info, "device", "") or "") != previous_port,
        ))
        port = (
            str(getattr(candidates[0], "device", "") or "")
            if candidates else ""
        )
        port_is_bootloader = bool(
            candidates
            and "mcuboot" in _port_descriptor(candidates[0]).lower()
        )
        if port and port == stable_port:
            stable_count += 1
        else:
            stable_port, stable_count = port, 1 if port else 0
        settled_same_path = time.monotonic() - started_at >= 1.0
        if (
            stable_port and stable_count >= 2
            and (
                port_is_bootloader
                or stable_port not in original_ports
                or (observed_transition and settled_same_path)
            )
        ):
            return stable_port
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(0.2, remaining))
    raise RuntimeError(
        "进入 MCUboot 后未找到同一块板的 SMP 串口；请重新插拔 USB 后重试"
    )


def _resolve_hardware_transport(requested: str, serial_port: str) -> str:
    """Resolve auto mode once when the GUI starts, preserving explicit modes."""
    global SERIAL_DATA_PORT, SERIAL_SMP_PORT
    mode = requested.lower()
    if mode not in {"auto", "rtt", "serial"}:
        raise ValueError(f"未知硬件传输模式:{requested}")
    if serial_port:
        SERIAL_DATA_PORT = serial_port
    if mode == "serial":
        if not SERIAL_DATA_PORT:
            SERIAL_DATA_PORT = _discover_serial_data_port() or ""
        if not SERIAL_DATA_PORT:
            raise ValueError("USB 模式未找到 V5.1 DATA CDC，请指定 --serial-port")
        SERIAL_SMP_PORT = (
            _discover_serial_smp_port(SERIAL_DATA_PORT) or SERIAL_SMP_PORT
        )
        return "serial"
    if mode == "rtt":
        return "rtt"
    discovered = _discover_serial_data_port()
    if discovered:
        SERIAL_DATA_PORT = discovered
        SERIAL_SMP_PORT = (
            _discover_serial_smp_port(discovered) or SERIAL_SMP_PORT
        )
        return "serial"
    # Auto mode must keep the workstation reachable when a board enumerates
    # its CDC interfaces but fails before its DATA command loop starts.  The
    # device list still exposes that USB board as unavailable, while an
    # attached J-Link remains usable for diagnosis and recovery.  Explicit
    # serial mode above continues to fail fast with a concrete DATA CDC error.
    SERIAL_DATA_PORT = ""
    SERIAL_SMP_PORT = ""
    return "rtt"


def _release_stale_measurement_bridge() -> None:
    """Gracefully release an orphaned OpenOCD or memory RTT bridge."""
    if not _port_accepts_connections(19021):
        return
    try:
        with socket.create_connection(("127.0.0.1", 19022), timeout=1) as connection:
            connection.sendall(BRIDGE_SHUTDOWN_COMMAND)
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            if not _port_accepts_connections(19021):
                return
            time.sleep(0.1)
    except OSError:
        pass
    try:
        with socket.create_connection(("127.0.0.1", 4444), timeout=1) as connection:
            connection.sendall(b"shutdown\n")
    except OSError as exc:
        raise RuntimeError("检测到残留硬件连接，但无法释放 J-Link") from exc
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        if not _port_accepts_connections(19021):
            return
        time.sleep(0.1)
    raise RuntimeError("残留 J-Link 连接未能在 4 秒内退出")


def _json_safe(value: Any) -> Any:
    """Convert numpy/scalar values into strict JSON-compatible primitives."""
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return value if value == value and abs(value) != float("inf") else None
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if hasattr(value, "item"):
        return _json_safe(value.item())
    return str(value)


def _split_complete_lines(text: str, pending: str = "") -> tuple[list[str], str]:
    """Split an append-only text stream without losing a partial final line."""
    combined = pending + text
    if not combined:
        return [], ""
    chunks = combined.splitlines(keepends=True)
    if chunks and not chunks[-1].endswith(("\n", "\r")):
        pending = chunks.pop()
    else:
        pending = ""
    return [chunk.rstrip("\r\n") for chunk in chunks], pending


def _now_id(prefix: str) -> str:
    return f"{prefix}_{time.strftime('%Y%m%d_%H%M%S')}"


class FilterController:
    """Persist analysis/display filter choices independently of firmware."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.settings = dict(FILTER_DEFAULTS)
        if FILTER_SETTINGS_PATH.exists():
            try:
                loaded = json.loads(FILTER_SETTINGS_PATH.read_text(encoding="utf-8"))
                self.settings = validate_filter_config(
                    loaded.get("settings", loaded) if isinstance(loaded, dict) else {}
                )
            except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
                DIAGNOSTICS.record(
                    "warning", "settings.filter.restore_failed",
                    "Saved filter settings could not be restored",
                    path=FILTER_SETTINGS_PATH, error=str(exc),
                )

    def apply(self, payload: dict[str, Any]) -> dict[str, Any]:
        settings = validate_filter_config(payload)
        with self.lock:
            self.settings = settings
            FILTER_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
            FILTER_SETTINGS_PATH.write_text(
                json.dumps({"settings": settings}, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        return self.snapshot()

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return _json_safe({
                "settings": dict(self.settings),
                "scopes": {
                    "off": "关闭",
                    "display": "仅显示",
                    "analysis": "显示并用于稳态分析/标定",
                },
            })


class PlateauController:
    """Persist host-side automatic-stop parameters independently of firmware."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.settings = PlateauConfig.validate(None)
        if PLATEAU_SETTINGS_PATH.exists():
            try:
                loaded = json.loads(PLATEAU_SETTINGS_PATH.read_text(encoding="utf-8"))
                raw = loaded.get("settings", loaded) if isinstance(loaded, dict) else {}
                self.settings = PlateauConfig.validate(raw)
            except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
                DIAGNOSTICS.record(
                    "warning", "settings.plateau.restore_failed",
                    "Saved plateau settings could not be restored",
                    path=PLATEAU_SETTINGS_PATH, error=str(exc),
                )

    def apply(self, payload: dict[str, Any]) -> dict[str, Any]:
        settings = PlateauConfig.validate(payload)
        with self.lock:
            PLATEAU_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
            temporary_path: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8",
                    prefix=f".{PLATEAU_SETTINGS_PATH.name}.", suffix=".tmp",
                    dir=PLATEAU_SETTINGS_PATH.parent, delete=False,
                ) as handle:
                    temporary_path = Path(handle.name)
                    json.dump(
                        {"settings": settings.to_dict()}, handle,
                        indent=2, ensure_ascii=False,
                    )
                    handle.write("\n")
                os.replace(temporary_path, PLATEAU_SETTINGS_PATH)
                temporary_path = None
            finally:
                if temporary_path is not None:
                    temporary_path.unlink(missing_ok=True)
            self.settings = settings
        return self.snapshot()

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return _json_safe({
                "settings": self.settings.to_dict(),
                "window_duration_s": self.settings.window_duration_s,
            })


class SettingsController:
    """Validate method parameters and build/flash matching firmware."""

    DEFAULTS = {
        "method": "it",
        "initial_potential_v": 0.2,
        "potential_v": 0.2,
        "working_electrode_v": 1.2,
        "prestep_s": 0.0,
        "duration_s": 180.0,
        "adaptive_stop": False,
        "target_rate_hz": 10.0,
        "sens_period_code": 0,
        "fit_window_s": 20.0,
        "fsr_nA": 2000,
        "offset_nA": 200,
        "offset_mode": "10pct",
        "cv_low_v": -0.6,
        "cv_high_v": 0.6,
        "cv_scan_rate_v_s": 0.05,
        "cv_cycles": 30,
        "cv_step_v": 0.001,
        "cv_quiet_s": 2.0,
        "cv_eis_fsr_uA": 40,
    }

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.apply_lock = threading.Lock()
        self.settings = dict(self.DEFAULTS)
        loaded_saved = False
        self._loaded_saved = False
        self._saved_firmware_hash = ""
        self._saved_transport = "rtt"
        self._saved_firmware_source = "build"
        if SETTINGS_PATH.exists():
            try:
                saved = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
                if not isinstance(saved, dict):
                    raise ValueError("settings file must contain an object")
                saved_settings = saved.get("settings", saved)
                self.settings = self.validate(saved_settings)
                loaded_saved = True
                self._loaded_saved = True
                self._saved_firmware_hash = str(saved.get("firmware_sha256") or "")
                self._saved_transport = str(saved.get("transport") or "rtt")
                self._saved_firmware_source = str(saved.get("firmware_source") or "build")
            except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
                DIAGNOSTICS.record(
                    "warning", "settings.hardware.restore_failed",
                    "Saved hardware settings could not be restored",
                    path=SETTINGS_PATH, error=str(exc),
                )
        self.applied = False
        self.state = "not_applied"
        if loaded_saved:
            self.message = "已恢复参数，但当前固件尚未烧录确认"
        else:
            self.message = "参数尚未应用到硬件"
        self.error = ""
        if HARDWARE_TRANSPORT in {"rtt", "serial"}:
            self.restore_for_transport(HARDWARE_TRANSPORT)

    @staticmethod
    def _firmware_hash(firmware_hex: Path | None = None) -> str:
        firmware_hex = firmware_hex or _firmware_artifact("zephyr.hex")
        if not firmware_hex.exists():
            return ""
        return hashlib.sha256(firmware_hex.read_bytes()).hexdigest()

    @classmethod
    def _verify_prebuilt_artifact(
        cls, artifact: Path, metadata: dict[str, Any]
    ) -> str:
        if not artifact.exists():
            raise RuntimeError(f"找不到内置固件:{artifact}")
        digest = cls._firmware_hash(artifact)
        expected = ""
        sha256 = metadata.get("sha256")
        if isinstance(sha256, dict):
            expected = str(sha256.get(artifact.name) or "").lower()
        elif isinstance(sha256, str):
            expected = sha256.lower()
        artifact_hashes = metadata.get("artifacts_sha256")
        if not expected and isinstance(artifact_hashes, dict):
            expected = str(artifact_hashes.get(artifact.name) or "").lower()
        if expected and digest.lower() != expected:
            raise RuntimeError(
                f"内置固件校验失败:{artifact.name} SHA-256 不匹配"
            )
        return digest

    @staticmethod
    def _supports_runtime_settings(metadata: dict[str, Any]) -> bool:
        protocol = metadata.get("runtime_protocol")
        return bool(
            metadata.get("runtime_configurable") is True
            and isinstance(protocol, dict)
            and protocol.get("name") == "MEAS"
            and protocol.get("version") == 1
        )

    def _set_apply_message(self, message: str) -> None:
        with self.lock:
            self.message = message

    @staticmethod
    def _probe_runtime_firmware(
        metadata: dict[str, Any], *, usb_transport: bool,
    ) -> tuple[str, str]:
        if usb_transport:
            if not SERIAL_DATA_PORT:
                return "transport_error", "未找到已选设备的 USB DATA CDC"
            return _probe_serial_runtime_firmware(SERIAL_DATA_PORT)
        return _probe_jlink_runtime_firmware(metadata)

    @classmethod
    def _wait_for_runtime_firmware(
        cls, metadata: dict[str, Any], *, usb_transport: bool,
        attempts: int = 3,
    ) -> tuple[str, str]:
        details: list[str] = []
        last_state = "missing"
        for attempt in range(1, max(1, attempts) + 1):
            state, detail = cls._probe_runtime_firmware(
                metadata, usb_transport=usb_transport,
            )
            last_state = state
            if state == "ready":
                return state, detail
            details.append(f"{attempt}:{state}:{detail or '-'}")
            if state == "invalid":
                break
            if attempt < attempts:
                time.sleep(0.35 * attempt)
        return last_state, " || ".join(details)

    def restore_for_transport(self, transport: str) -> None:
        if not self._loaded_saved:
            return
        if self._saved_firmware_source == "prebuilt":
            artifact = (
                V51_PREBUILT_IMAGE if self._saved_transport == "serial"
                else FIRMWARE_PREBUILT_DIR / "zephyr.hex"
            )
        else:
            artifact = FIRMWARE_BUILD_DIR / (
                "zephyr.signed.bin" if self._saved_transport == "serial" else "zephyr.hex"
            )
        verified = bool(
            self._saved_firmware_hash
            and self._saved_transport == transport
            and self._saved_firmware_hash == self._firmware_hash(artifact)
        )
        with self.lock:
            self.applied = verified
            self.state = "applied" if verified else "not_applied"
            self.message = (
                "已恢复与当前固件一致的硬件参数"
                if verified else "已恢复参数，但当前硬件尚未烧录确认"
            )

    @staticmethod
    def _run_build(command: list[str], timeout_s: float = 600) -> None:
        """Run a build in its own process group and reclaim every descendant."""
        started_at = time.monotonic()
        DIAGNOSTICS.record(
            "info", "firmware.build.started", "Firmware build command started",
            command=command, timeout_s=timeout_s,
        )
        process = subprocess.Popen(
            command, cwd=PROJECT_DIR, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
            **(
                runtime.hidden_subprocess_kwargs(new_process_group=True)
                if _IS_WIN else {"start_new_session": True}
            ),
        )
        stdout = ""
        stderr = ""

        def remember_timeout(error: subprocess.TimeoutExpired) -> None:
            nonlocal stdout, stderr

            def output_text(value: Any) -> str:
                if value is None:
                    return ""
                if isinstance(value, bytes):
                    return value.decode(errors="replace")
                return str(value)

            if error.output is not None:
                stdout = output_text(error.output)
            if error.stderr is not None:
                stderr = output_text(error.stderr)

        def kill_windows_tree() -> None:
            killed = False
            try:
                result = subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                    capture_output=True, text=True,
                    encoding="utf-8", errors="replace", timeout=10,
                    **runtime.hidden_subprocess_kwargs(),
                )
                killed = result.returncode == 0
            except (subprocess.TimeoutExpired, OSError):
                pass
            if not killed:
                try:
                    process.kill()
                except OSError:
                    pass

        def signal_posix_tree(signum: int) -> None:
            try:
                os.killpg(process.pid, signum)
            except (ProcessLookupError, PermissionError, OSError):
                try:
                    if signum == signal.SIGTERM:
                        process.terminate()
                    else:
                        process.kill()
                except OSError:
                    pass

        def close_pipes_and_reap() -> None:
            # A descendant outside the process group can keep inherited pipe
            # handles open after the build shell is dead. Never let that turn a
            # timeout into an unbounded communicate().
            for pipe in (process.stdout, process.stderr):
                if pipe is not None:
                    try:
                        pipe.close()
                    except (OSError, ValueError):
                        pass
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                try:
                    process.kill()
                except OSError:
                    pass
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass

        try:
            stdout, stderr = process.communicate(timeout=timeout_s)
        except subprocess.TimeoutExpired as exc:
            remember_timeout(exc)
            if _IS_WIN:
                kill_windows_tree()
            else:
                signal_posix_tree(signal.SIGTERM)
            try:
                stdout, stderr = process.communicate(timeout=5)
            except subprocess.TimeoutExpired as graceful_timeout:
                remember_timeout(graceful_timeout)
                if _IS_WIN:
                    kill_windows_tree()
                else:
                    signal_posix_tree(signal.SIGKILL)
                try:
                    stdout, stderr = process.communicate(timeout=5)
                except subprocess.TimeoutExpired as forced_timeout:
                    remember_timeout(forced_timeout)
                    close_pipes_and_reap()
            DIAGNOSTICS.record(
                "error", "firmware.build.timeout", "Firmware build timed out",
                command=command,
                timeout_s=timeout_s,
                stdout=stdout,
                stderr=stderr,
                duration_ms=round((time.monotonic() - started_at) * 1000, 1),
            )
            raise subprocess.TimeoutExpired(
                command, timeout_s, output=stdout, stderr=stderr,
            ) from exc
        if process.returncode:
            DIAGNOSTICS.record(
                "error", "firmware.build.failed", "Firmware build command failed",
                command=command,
                return_code=process.returncode,
                stdout=stdout,
                stderr=stderr,
                duration_ms=round((time.monotonic() - started_at) * 1000, 1),
            )
            raise subprocess.CalledProcessError(
                process.returncode, command, output=stdout, stderr=stderr,
            )
        DIAGNOSTICS.record(
            "info", "firmware.build.completed", "Firmware build completed",
            command=command,
            duration_ms=round((time.monotonic() - started_at) * 1000, 1),
        )

    @staticmethod
    def _intel_hex_image(
        firmware_hex: Path, flash_size: int = 512 * 1024
    ) -> dict[int, int]:
        """Parse an Intel HEX image confined to nRF52833 application Flash."""
        base_address = 0
        image: dict[int, int] = {}
        saw_eof = False
        for line_number, raw_line in enumerate(
            firmware_hex.read_text(encoding="ascii").splitlines(), start=1
        ):
            line = raw_line.strip()
            if not line:
                continue
            if saw_eof:
                raise RuntimeError(
                    f"Intel HEX EOF 后仍有记录（第 {line_number} 行）"
                )
            if not line.startswith(":"):
                raise RuntimeError(
                    f"Intel HEX 格式错误（第 {line_number} 行）"
                )
            try:
                record = bytes.fromhex(line[1:])
            except ValueError as exc:
                raise RuntimeError(
                    f"Intel HEX 编码错误（第 {line_number} 行）"
                ) from exc
            if len(record) < 5 or len(record) != record[0] + 5:
                raise RuntimeError(
                    f"Intel HEX 长度错误（第 {line_number} 行）"
                )
            if sum(record) & 0xFF:
                raise RuntimeError(
                    f"Intel HEX 校验和错误（第 {line_number} 行）"
                )
            address = (record[1] << 8) | record[2]
            record_type = record[3]
            data = record[4:-1]
            if address + len(data) > 0x10000:
                raise RuntimeError(
                    f"Intel HEX 记录跨越 16 位地址边界（第 {line_number} 行）"
                )
            if record_type == 0x00:
                if not data:
                    continue
                start = base_address + address
                end = start + len(data) - 1
                if start < 0 or end >= flash_size:
                    raise RuntimeError(
                        "Intel HEX 含应用 Flash 之外的数据，已拒绝烧录:"
                        f"0x{start:08X}..0x{end:08X}（第 {line_number} 行）"
                    )
                for offset, value in enumerate(data):
                    absolute = start + offset
                    if absolute in image:
                        raise RuntimeError(
                            "Intel HEX 含重叠数据地址:"
                            f"0x{absolute:08X}（第 {line_number} 行）"
                        )
                    image[absolute] = value
            elif record_type == 0x01:
                if address != 0 or data:
                    raise RuntimeError(
                        f"Intel HEX EOF 记录非法（第 {line_number} 行）"
                    )
                saw_eof = True
            elif record_type == 0x02:
                if address != 0 or len(data) != 2:
                    raise RuntimeError(
                        f"Intel HEX 段地址记录非法（第 {line_number} 行）"
                    )
                base_address = int.from_bytes(data, "big") << 4
            elif record_type == 0x04:
                if address != 0 or len(data) != 2:
                    raise RuntimeError(
                        f"Intel HEX 线性地址记录非法（第 {line_number} 行）"
                    )
                base_address = int.from_bytes(data, "big") << 16
            elif record_type in (0x03, 0x05):
                if address != 0 or len(data) != 4:
                    raise RuntimeError(
                        f"Intel HEX 启动地址记录非法（第 {line_number} 行）"
                    )
            else:
                raise RuntimeError(
                    f"Intel HEX 不支持的记录类型 0x{record_type:02X}"
                    f"（第 {line_number} 行）"
                )
        if not saw_eof:
            raise RuntimeError("Intel HEX 缺少唯一 EOF 记录")
        if not image:
            raise RuntimeError("Intel HEX 中没有 nRF52833 Flash 数据")
        return image

    @classmethod
    def _intel_hex_flash_sectors(
        cls, firmware_hex: Path, page_size: int = 4096,
        flash_size: int = 512 * 1024,
    ) -> list[int]:
        image = cls._intel_hex_image(firmware_hex, flash_size=flash_size)
        return sorted({address // page_size for address in image})

    @staticmethod
    def _openocd_command(speed: int, *commands: str) -> list[str]:
        command = [
            str(OPENOCD_EXE), "-s", str(OPENOCD_SCRIPTS),
            "-f", "interface/jlink.cfg", "-c", "transport select swd",
            # Avoid a RAM flash algorithm on older compatible probes. OpenOCD
            # still owns erase, write, verify and reset in one bounded session.
            "-c", "set WORKAREASIZE 0", "-f", "target/nrf52.cfg",
        ]
        if JLINK_SERIAL:
            command += ["-c", f"adapter serial {JLINK_SERIAL}"]
        command += ["-c", f"adapter speed {speed}"]
        for openocd_command in commands:
            command += ["-c", openocd_command]
        return command

    @staticmethod
    def _openocd_path(path: Path) -> str:
        text = str(path.resolve()).replace("\\", "/")
        if any(character in text for character in "{}\r\n"):
            raise RuntimeError(f"OpenOCD 无法处理固件路径:{path}")
        return "{" + text + "}"

    @classmethod
    def _parse_openocd_words(
        cls, output: str, address: int, count: int
    ) -> list[int]:
        words: dict[int, int] = {}
        for line in output.splitlines():
            match = re.match(
                r"^\s*(?:0x)?([0-9A-Fa-f]{8}):\s*"
                r"((?:[0-9A-Fa-f]{8}(?:\s+|$))+)",
                line,
            )
            if match is None:
                continue
            line_address = int(match.group(1), 16)
            for index, raw_word in enumerate(match.group(2).split()):
                words[line_address + index * 4] = int(raw_word, 16)
        expected = [address + index * 4 for index in range(count)]
        missing = [item for item in expected if item not in words]
        if missing:
            raise RuntimeError(
                "OpenOCD 内存读取不完整，缺少地址:"
                + ", ".join(f"0x{item:08X}" for item in missing[:4])
            )
        return [words[item] for item in expected]

    @staticmethod
    def _openocd_transient(output: str) -> bool:
        lowered = output.lower()
        return any(marker in lowered for marker in (
            "parity mismatch", "failed to read memory", "failed to write memory",
            "error waiting nvmc_ready", "examination failed",
            "failed to erase reg", "cannot read idr", "error connecting dp",
            "libusb_error_timeout", "unable to connect", "target not examined",
        ))

    @classmethod
    def _run_openocd_once(
        cls, speed: int, *commands: str, timeout_s: float
    ) -> tuple[int, str]:
        command = cls._openocd_command(speed, *commands)
        try:
            done = _run_openocd_bounded(command, timeout_s=timeout_s)
            return done.returncode, f"{done.stdout}\n{done.stderr}"
        except subprocess.TimeoutExpired as exc:
            return 124, f"{exc.stdout or ''}\n{exc.stderr or ''}\nTIMEOUT"
        except OSError as exc:
            return 127, str(exc)

    @staticmethod
    def _jlink_result_error(done: subprocess.CompletedProcess[str]) -> str:
        blob = f"{done.stdout}\n{done.stderr}"
        lowered = blob.lower()
        failed = (
            done.returncode != 0
            or "script processing completed." not in lowered
            or any(marker in lowered for marker in (
                "error:", "failed to", "could not", "cannot connect",
                "script execution aborted", "programming failed",
            ))
        )
        if not failed:
            return ""
        return " | ".join(_meaningful_process_tail(blob, limit=8))

    @staticmethod
    def _image_mismatch(
        image: dict[int, int], readback: bytes,
    ) -> tuple[int, int, int] | None:
        start = min(image)
        for address, expected in sorted(image.items()):
            offset = address - start
            actual = readback[offset] if offset < len(readback) else -1
            if actual != expected:
                return address, expected, actual
        return None

    @classmethod
    def _read_jlink_flash_image(cls, image: dict[int, int]) -> bytes:
        """Read one contiguous application range in a fresh Commander process."""
        start = min(image)
        length = max(image) - start + 1
        failures: list[str] = []
        with tempfile.TemporaryDirectory(prefix="sensus-flash-read-") as temporary:
            for attempt in range(1, 3):
                output_path = Path(temporary) / f"readback-{attempt}.bin"
                script = jlink_connection_script(
                    f"mem32 0x{NRF52833_INFO_PART_ADDRESS:08X} 1",
                    "h",
                    f"savebin {_jlink_file_argument(output_path)}, "
                    f"0x{start:08X}, 0x{length:X}",
                    "q",
                )
                try:
                    done = run_jlink_script(
                        script, JLINK_SERIAL or None,
                        executable=JLINK_EXE, timeout_s=90,
                    )
                    blob = f"{done.stdout}\n{done.stderr}"
                    error = cls._jlink_result_error(done)
                    part = _parse_jlink_mem32(
                        blob, NRF52833_INFO_PART_ADDRESS, 1
                    )[0]
                    readback = output_path.read_bytes()
                    if error:
                        raise RuntimeError(error)
                    if part != NRF52833_INFO_PART_VALUE:
                        raise RuntimeError("J-Link 目标身份核对失败")
                    if len(readback) != length:
                        raise RuntimeError(
                            f"J-Link 回读长度不完整:{len(readback)} != {length}"
                        )
                    return readback
                except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
                    failures.append(str(exc))
                    if attempt < 2:
                        time.sleep(0.15)
        raise RuntimeError("J-Link 独立回读失败:" + " || ".join(failures))

    @classmethod
    def _run_jlink_application(cls) -> None:
        failures: list[str] = []
        for attempt in range(1, 3):
            try:
                done = run_jlink_script(
                    jlink_connection_script(
                        f"mem32 0x{NRF52833_INFO_PART_ADDRESS:08X} 1",
                        "r", "g", "sleep 200", "q",
                    ),
                    JLINK_SERIAL or None,
                    executable=JLINK_EXE, timeout_s=20,
                )
                blob = f"{done.stdout}\n{done.stderr}"
                error = cls._jlink_result_error(done)
                part = _parse_jlink_mem32(
                    blob, NRF52833_INFO_PART_ADDRESS, 1
                )[0]
                if error:
                    raise RuntimeError(error)
                if part != NRF52833_INFO_PART_VALUE:
                    raise RuntimeError("J-Link 目标身份核对失败")
                return
            except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
                failures.append(str(exc))
                if attempt < 2:
                    time.sleep(0.15)
        raise RuntimeError("J-Link 未能复位并运行目标:" + " || ".join(failures))

    @classmethod
    def _select_swd_flash_backend(cls) -> str:
        """Select one backend using read-only chip identity checks."""
        failures: list[str] = []
        if _openocd_jlink_available():
            reachable, output = _openocd_target_probe(JLINK_SERIAL)
            if reachable:
                return "openocd"
            detail = " | ".join(_meaningful_process_tail(output, limit=4))
            failures.append("OpenOCD:" + (detail or "目标无响应"))
        if JLINK_EXE.is_file():
            reachable, output = probe_jlink_target(
                JLINK_SERIAL or None, executable=JLINK_EXE, timeout_s=15,
            )
            if reachable:
                return "jlink"
            detail = " | ".join(_meaningful_process_tail(output, limit=4))
            failures.append("J-Link:" + (detail or "目标无响应"))
        if not failures:
            failures.append("没有可用的 J-Link Commander 或随包 OpenOCD")
        raise RuntimeError(
            "J-Link 探头已连接，但 nRF52833 目标无响应:"
            + " || ".join(failures)
        )

    @classmethod
    def _flash_with_jlink(
        cls,
        firmware_hex: Path,
        image: dict[int, int],
        *,
        target_verified: bool = False,
    ) -> bool:
        if not target_verified:
            reachable, output = probe_jlink_target(
                JLINK_SERIAL or None, executable=JLINK_EXE, timeout_s=15,
            )
            if not reachable:
                raise RuntimeError(
                    "J-Link 探头已连接，但 nRF52833 目标无响应:"
                    + " | ".join(_meaningful_process_tail(output, limit=6))
                )

        try:
            current = cls._read_jlink_flash_image(image)
            changed = cls._image_mismatch(image, current) is not None
            if changed:
                script = jlink_connection_script(
                    f"mem32 0x{NRF52833_INFO_PART_ADDRESS:08X} 1",
                    "h",
                    f"loadfile {_jlink_file_argument(firmware_hex)}, noreset",
                    "q",
                )
                done = run_jlink_script(
                    script, JLINK_SERIAL or None,
                    executable=JLINK_EXE, timeout_s=180,
                )
                blob = f"{done.stdout}\n{done.stderr}"
                error = cls._jlink_result_error(done)
                part = _parse_jlink_mem32(
                    blob, NRF52833_INFO_PART_ADDRESS, 1
                )[0]
                if (
                    error
                    or part != NRF52833_INFO_PART_VALUE
                    or "o.k." not in blob.lower()
                ):
                    raise RuntimeError(
                        "J-Link 写入未确认成功:"
                        + (error or "缺少下载完成标记")
                    )
                verified = cls._read_jlink_flash_image(image)
                mismatch = cls._image_mismatch(image, verified)
                if mismatch is not None:
                    address, expected, actual = mismatch
                    actual_text = "EOF" if actual < 0 else f"0x{actual:02X}"
                    raise RuntimeError(
                        "J-Link 独立回读不一致:"
                        f"0x{address:08X} 期望 0x{expected:02X}，实际 {actual_text}"
                    )
        except Exception:
            try:
                cls._run_jlink_application()
            except Exception:
                pass
            raise
        cls._run_jlink_application()
        return changed

    @classmethod
    def _flash_with_openocd(
        cls, firmware_hex: Path, image: dict[int, int],
    ) -> None:
        required_scripts = (
            OPENOCD_SCRIPTS / "interface/jlink.cfg",
            OPENOCD_SCRIPTS / "target/nrf52.cfg",
        )
        if not OPENOCD_EXE.is_file() or not all(path.is_file() for path in required_scripts):
            raise RuntimeError("找不到完整的随包 OpenOCD 与 nRF52 脚本")
        firmware_path = cls._openocd_path(firmware_hex)
        failures: list[str] = []
        for attempt in range(1, 3):
            return_code, blob = cls._run_openocd_once(
                100,
                "init", "reset halt",
                _openocd_identity_command(),
                f"flash write_image erase {firmware_path}",
                f"verify_image {firmware_path}",
                "reset run", "shutdown",
                timeout_s=300,
            )
            identity_verified = _openocd_identity_verified(blob)
            if not identity_verified:
                failures.append("OpenOCD 未返回 nRF52833 身份标记")
            lowered = blob.lower()
            success = (
                return_code == 0
                and identity_verified
                and "wrote " in lowered
                and "verified " in lowered
                and not any(marker in lowered for marker in (
                    "contents differ", "verification failed", "checksum mismatch",
                ))
            )
            if success:
                return
            failures.append(" | ".join(_meaningful_process_tail(blob, limit=8)))
            if attempt == 2 or not cls._openocd_transient(blob):
                break
            time.sleep(0.15)
        # A failed command may have stopped after ``reset halt``. Use the same
        # backend for a bounded best-effort restart, but never hide the original
        # programming/verification error if this cleanup also fails.
        try:
            cls._run_openocd_once(
                100, "init", "reset run", "shutdown", timeout_s=20,
            )
        except Exception:
            pass
        raise RuntimeError(
            "OpenOCD 单会话写入/校验/复位失败:"
            + " || ".join(item for item in failures if item)
        )

    @classmethod
    def _flash_firmware(cls, firmware_hex: Path | None = None) -> None:
        """Safely update one nRF52833 image without mixing flash backends."""
        firmware_hex = firmware_hex or _firmware_artifact("zephyr.hex")
        if not firmware_hex.exists():
            raise RuntimeError(f"找不到固件: {firmware_hex}")
        image = cls._intel_hex_image(firmware_hex)
        sectors = sorted({address // 4096 for address in image})
        try:
            backend = cls._select_swd_flash_backend()
        except Exception as exc:
            DIAGNOSTICS.record(
                "error", "firmware.flash.preflight_failed",
                "No SWD backend could verify the target identity",
                firmware=firmware_hex, probe_serial=JLINK_SERIAL,
                error=str(exc),
            )
            raise
        DIAGNOSTICS.record(
            "info", "firmware.flash.started", "Firmware flashing started",
            backend=backend, firmware=firmware_hex, probe_serial=JLINK_SERIAL,
            pages=sectors,
        )
        try:
            if backend == "jlink":
                changed = cls._flash_with_jlink(
                    firmware_hex, image, target_verified=True,
                )
            else:
                cls._flash_with_openocd(firmware_hex, image)
                changed = True
        except Exception as exc:
            DIAGNOSTICS.record(
                "error", "firmware.flash.failed",
                "Safe page-scoped firmware flash failed",
                backend=backend, firmware=firmware_hex, error=str(exc),
            )
            raise
        DIAGNOSTICS.record(
            "info", "firmware.flash.completed",
            "Single-backend image verification and run completed",
            backend=backend, firmware=firmware_hex, pages=sectors,
            changed=changed,
        )

    @staticmethod
    def _upgrade_v51_firmware(image: Path | None = None) -> None:
        """Reset the V5.1 app over SMP, then upload its signed image via USB."""
        global SERIAL_SMP_PORT
        if image is None:
            image = FIRMWARE_BUILD_DIR / "zephyr.signed.bin"
            if not image.exists() and V51_PREBUILT_IMAGE.exists():
                image = V51_PREBUILT_IMAGE
        if not image.exists():
            raise RuntimeError(f"找不到 V5.1 签名镜像:{image}")
        if not runtime.is_frozen() and not SMPMGR_EXE.exists():
            raise RuntimeError(f"找不到 smpmgr:{SMPMGR_EXE}")
        if not SERIAL_SMP_PORT:
            raise RuntimeError(
                "USB 固件更新需要 SENSUS_SMP_PORT 指向 SMP CDC"
            )
        if not runtime.is_frozen() and not _IS_WIN and not V51_UPLOAD_SCRIPT.exists():
            raise RuntimeError(f"找不到 USB 上传脚本:{V51_UPLOAD_SCRIPT}")

        smpmgr = (
            runtime.module_command("smpmgr")
            if runtime.is_frozen() else [str(SMPMGR_EXE)]
        )
        previous_smp_port = SERIAL_SMP_PORT
        physical_usb = _usb_physical_snapshot_for_port(previous_smp_port)
        DIAGNOSTICS.record(
            "info", "firmware.usb_upgrade.started", "USB firmware upgrade started",
            image=image, smp_port=SERIAL_SMP_PORT,
        )
        reset_output = ""
        try:
            reset = subprocess.run(
                [*smpmgr, "--port", SERIAL_SMP_PORT, "--timeout", "5", "os", "reset"],
                check=True, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=15,
                **runtime.hidden_subprocess_kwargs(),
            )
            reset_output = f"{reset.stdout}\n{reset.stderr}"
            SERIAL_SMP_PORT = _wait_for_bootloader_smp_port(
                previous_smp_port, physical_usb,
            )
            if runtime.is_frozen() or _IS_WIN:
                done = subprocess.run(
                    [*smpmgr, "--port", SERIAL_SMP_PORT, "--timeout", "10", "upgrade",
                     str(image)],
                    check=True, capture_output=True, text=True,
                    encoding="utf-8", errors="replace", timeout=150,
                    **runtime.hidden_subprocess_kwargs(),
                )
            else:
                upload_environment = {
                    **os.environ,
                    "SENSUS_SMP_PORT": SERIAL_SMP_PORT,
                }
                done = subprocess.run(
                    ["/bin/bash", str(V51_UPLOAD_SCRIPT), str(image)],
                    cwd=PROJECT_DIR,
                    check=True, capture_output=True, text=True,
                    encoding="utf-8", errors="replace", timeout=150,
                    env=upload_environment,
                )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired,
                OSError, RuntimeError) as exc:
            DIAGNOSTICS.exception(
                "firmware.usb_upgrade.failed", "USB firmware upgrade tool failed",
                exc,
                image=image, smp_port=SERIAL_SMP_PORT,
                reset_output=reset_output,
                stdout=getattr(exc, "stdout", ""),
                stderr=getattr(exc, "stderr", ""),
            )
            raise
        blob = f"{done.stdout}\n{done.stderr}"
        if "Upgrade complete." not in blob:
            DIAGNOSTICS.record(
                "error", "firmware.usb_upgrade.verification_missing",
                "USB firmware tool did not confirm completion",
                image=image, smp_port=SERIAL_SMP_PORT,
                reset_output=reset_output, output=blob,
            )
            tail = [line for line in blob.strip().splitlines() if line.strip()][-3:]
            raise RuntimeError("V5.1 USB 更新未确认成功:" + " | ".join(tail))
        try:
            _wait_for_usb_transport_ready()
        except RuntimeError as exc:
            DIAGNOSTICS.exception(
                "firmware.usb_upgrade.application_recovery_failed",
                "USB firmware uploaded but application CDC did not recover",
                exc,
                image=image, data_port=SERIAL_DATA_PORT,
                smp_port=SERIAL_SMP_PORT,
            )
            raise
        DIAGNOSTICS.record(
            "info", "firmware.usb_upgrade.completed",
            "USB firmware upgrade completed and application CDC recovered",
            image=image, data_port=SERIAL_DATA_PORT,
            smp_port=SERIAL_SMP_PORT,
        )

    @classmethod
    def same_analysis_protocol(
        cls, first: dict[str, Any], second: dict[str, Any]
    ) -> bool:
        """Compare settings that determine sampled IT data and its analysis.

        Startup potential/hold are not sampled. They remain in run metadata for
        traceability but do not invalidate a curve whose sampled potential,
        duration, rate, fit window and current range are unchanged.
        """
        try:
            first = cls.validate(first)
            second = cls.validate(second)
        except (TypeError, ValueError):
            return False
        if first["method"] != second["method"]:
            return False
        ignored = {
            "initial_potential_v", "prestep_s", "cv_low_v", "cv_high_v",
            "cv_scan_rate_v_s", "cv_cycles", "cv_step_v", "cv_quiet_s",
        }
        if first.get("adaptive_stop") and second.get("adaptive_stop"):
            ignored.add("duration_s")
        return (
            {key: value for key, value in first.items() if key not in ignored}
            == {key: value for key, value in second.items() if key not in ignored}
        )

    @classmethod
    def validate(cls, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("检测条件必须是 JSON 对象")
        merged = {**cls.DEFAULTS, **payload}
        method = str(merged["method"]).lower()
        raw_adaptive_stop = merged.get("adaptive_stop", False)
        if isinstance(raw_adaptive_stop, bool):
            adaptive_stop = raw_adaptive_stop
        elif isinstance(raw_adaptive_stop, (int, float)) and raw_adaptive_stop in (0, 1):
            adaptive_stop = bool(raw_adaptive_stop)
        elif isinstance(raw_adaptive_stop, str) and raw_adaptive_stop.strip().lower() in {
            "true", "1", "yes", "on", "false", "0", "no", "off",
        }:
            adaptive_stop = raw_adaptive_stop.strip().lower() in {"true", "1", "yes", "on"}
        else:
            raise ValueError("自动停止必须是开或关")
        def integer(name: str) -> int:
            try:
                value = float(merged[name])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"检测条件 {name} 必须是整数") from exc
            if not math.isfinite(value) or value != math.trunc(value):
                raise ValueError(f"检测条件 {name} 必须是整数")
            return int(value)

        initial_potential_v = float(merged["initial_potential_v"])
        potential_v = float(merged["potential_v"])
        working_electrode_v = float(merged["working_electrode_v"])
        prestep_s = float(merged["prestep_s"])
        duration_s = float(merged["duration_s"])
        target_rate_hz = float(merged["target_rate_hz"])
        sens_period_code = integer("sens_period_code")
        fit_window_s = float(merged["fit_window_s"])
        cv_low_v = float(merged["cv_low_v"])
        cv_high_v = float(merged["cv_high_v"])
        cv_scan_rate_v_s = float(merged["cv_scan_rate_v_s"])
        cv_cycles = integer("cv_cycles")
        cv_step_v = float(merged["cv_step_v"])
        cv_quiet_s = float(merged["cv_quiet_s"])
        cv_eis_fsr_uA = integer("cv_eis_fsr_uA")
        fsr_nA = integer("fsr_nA")
        if not all(math.isfinite(value) for value in (
                initial_potential_v, potential_v, working_electrode_v,
                prestep_s, duration_s, target_rate_hz, fit_window_s,
                cv_low_v, cv_high_v, cv_scan_rate_v_s, cv_step_v, cv_quiet_s
        )):
            raise ValueError("检测条件不能包含 NaN 或无穷大")
        raw_offset_mode = payload.get("offset_mode")
        if raw_offset_mode in (None, ""):
            raw_offset_mode = (
                f"{int(payload['offset_nA'])}nA"
                if "offset_nA" in payload else cls.DEFAULTS["offset_mode"]
            )
        offset_mode = str(raw_offset_mode)
        if method not in {"it", "cv"}:
            raise ValueError("检测方法必须是 I-T 或 CV")
        if method == "it":
            if not -0.4 <= initial_potential_v <= 0.4:
                raise ValueError("I-T 起始电位必须在 -0.4 至 +0.4 V 之间")
            if not -0.4 <= potential_v <= 0.4:
                raise ValueError("I-T 测试电位必须在 -0.4 至 +0.4 V 之间")
            if not 0.25 <= working_electrode_v <= 1.535:
                raise ValueError("I-T 的 WE 电位必须在 0.25 至 1.535 V 之间")
            for label, value in (
                ("起始", initial_potential_v), ("测试", potential_v)
            ):
                reference_electrode_v = working_electrode_v - value
                if not 0.008 <= reference_electrode_v <= 1.535:
                    raise ValueError(
                        f"{label}电位对应的 RE={reference_electrode_v:.3f} V，"
                        "超出 DAC 可实现范围 0.008 至 1.535 V；请调整 WE 电位"
                    )
            if not 0 <= prestep_s <= 300:
                raise ValueError("阶跃前保持时间必须在 0 至 300 秒之间")
            if not 0 < duration_s <= 3600:
                raise ValueError("I-T 时长必须在 0 秒以上且不超过 3600 秒")
            if not adaptive_stop and not 10 <= duration_s <= 3600:
                raise ValueError("I-T 时长必须在 10 至 3600 秒之间")
        else:
            adaptive_stop = False
            if not -0.6 <= cv_low_v < cv_high_v <= 0.6:
                raise ValueError("CV 电位范围必须在 -0.6 至 +0.6 V 内，且下限小于上限")
            if not 0.01 <= cv_scan_rate_v_s <= 0.1:
                raise ValueError("CV 扫描速度必须在 0.01 至 0.10 V/s 之间")
            if not 1 <= cv_cycles <= 100:
                raise ValueError("CV 循环圈数必须在 1 至 100 之间")
            if abs(cv_step_v - 0.001) > 1e-9:
                raise ValueError("当前硬件 CV 电位步长固定为 1 mV")
            if not 0 <= cv_quiet_s <= 300:
                raise ValueError("CV 静置时间必须在 0 至 300 秒之间")
            if cv_eis_fsr_uA not in CV_EIS_FSR_OPTIONS:
                raise ValueError("CV EIS ADC 量程必须为 4、8、20 或 40 µA")
            duration_s = 2 * (cv_high_v - cv_low_v) / cv_scan_rate_v_s * cv_cycles
            initial_potential_v = cv_low_v
            potential_v = cv_low_v
            prestep_s = cv_quiet_s
        if not 0.5 <= target_rate_hz <= 10:
            raise ValueError("输出采样频率必须在 0.5 至 10 Hz 之间")
        if sens_period_code not in SENS_PERIOD_MS:
            raise ValueError("不支持该硬件采样周期")
        if method == "it" and not 1 <= fit_window_s <= (
            3600.0 if adaptive_stop else duration_s
        ):
            limit = "3600 秒" if adaptive_stop else "测量时长"
            raise ValueError(f"拟合窗口必须在 1 秒与{limit}之间")
        if method == "it" and fit_window_s * target_rate_hz < 3:
            raise ValueError("拟合窗口内至少需要 3 个输出采样点")
        if fsr_nA not in FSR_OPTIONS and fsr_nA not in IT_WIDE_FSR_OPTIONS:
            raise ValueError("不支持该电流量程")
        if offset_mode not in OFFSET_OPTIONS:
            raise ValueError("不支持该偏置电流档位")
        offset_value = OFFSET_OPTIONS[offset_mode][1]
        offset_nA = (
            int(round(fsr_nA * float(offset_value)))
            if isinstance(offset_value, float) else int(offset_value)
        )
        if offset_nA >= fsr_nA:
            raise ValueError("偏置电流必须小于电流满量程")
        return {
            "method": method,
            "initial_potential_v": round(initial_potential_v, 4),
            "potential_v": round(potential_v, 4),
            "working_electrode_v": round(working_electrode_v, 4),
            "prestep_s": round(prestep_s, 3),
            "duration_s": round(duration_s, 3),
            "adaptive_stop": adaptive_stop,
            "target_rate_hz": round(target_rate_hz, 3),
            "sens_period_code": sens_period_code,
            "fit_window_s": round(fit_window_s, 3),
            "fsr_nA": fsr_nA,
            "offset_nA": offset_nA,
            "offset_mode": offset_mode,
            "cv_low_v": round(cv_low_v, 4),
            "cv_high_v": round(cv_high_v, 4),
            "cv_scan_rate_v_s": round(cv_scan_rate_v_s, 4),
            "cv_cycles": cv_cycles,
            "cv_step_v": round(cv_step_v, 4),
            "cv_quiet_s": round(cv_quiet_s, 3),
            "cv_eis_fsr_uA": cv_eis_fsr_uA,
        }

    @staticmethod
    def working_electrode_mv(settings: dict[str, Any]) -> int:
        return int(round(float(settings["working_electrode_v"]) * 1000))

    @classmethod
    def runtime_afe_contract(cls, payload: dict[str, Any]) -> dict[str, Any]:
        """Return the persistent AFE state required before a measurement."""
        settings = cls.validate(payload)
        fsr_codes = {50: 0, 100: 1, 250: 2, 500: 3, 1000: 4, 2000: 5}
        offset_codes = {
            "10pct": 1, "20pct": 2, "50pct": 3, "9nA": 4,
            "19nA": 5, "40nA": 6, "80nA": 7,
        }
        # Wide-range IT uses the EIS ADC for samples, but the persistent DC AFE
        # baseline still boots at 2 uA and must not inherit Debug changes.
        fsr_nA = settings["fsr_nA"]
        cv = settings["method"] == "cv"
        return {
            "fsr": fsr_codes.get(fsr_nA, 5),
            "off": offset_codes[settings["offset_mode"]],
            "conv_src": "auto",
            "period": settings["sens_period_code"],
            "sysper": 3,
            "clk40": 0,
            "ioc": 0,
            "e_mv": int(round(settings["potential_v"] * 1000)),
            # CV's EIS path has always used an 800 mV common-mode voltage.
            # Keep the DC baseline on the same valid DAC window while the
            # visible IT working-electrode control remains fully adjustable.
            "vwe_mv": 800 if cv else cls.working_electrode_mv(settings),
            "idle": 2,
            "cellv": 1,
            "chop": 1,
            "rs": 0,
            "ios": 1,
            "satpct": 5,
        }

    @classmethod
    def runtime_afe_command(cls, payload: dict[str, Any]) -> str:
        contract = cls.runtime_afe_contract(payload)
        return "SET " + " ".join((
            f"fsr={contract['fsr']}", f"off={contract['off']}",
            "conv=auto", f"period={contract['period']}",
            f"sysper={contract['sysper']}", f"clk40={contract['clk40']}",
            f"ioc={contract['ioc']}", f"chop={contract['chop']}",
            f"rs={contract['rs']}", f"ios={contract['ios']}",
            f"e={contract['e_mv']}", f"vwe={contract['vwe_mv']}",
            f"idle={contract['idle']}", f"cellv={contract['cellv']}",
            f"satpct={contract['satpct']}",
        ))

    @classmethod
    def runtime_measurement_contract(cls, payload: dict[str, Any]) -> dict[str, Any]:
        settings = cls.validate(payload)
        eis_codes = {4: 0, 8: 1, 20: 2, 40: 3}
        wide_eis_codes = {4000: 0, 8000: 1, 20000: 2, 40000: 3}
        return {
            "mode": 1 if settings["method"] == "cv" else 0,
            "start_mv": int(round(settings["initial_potential_v"] * 1000)),
            "target_mv": int(round(settings["potential_v"] * 1000)),
            "quiet_ms": int(round(
                (settings["cv_quiet_s"] if settings["method"] == "cv"
                 else settings["prestep_s"]) * 1000
            )),
            "duration_ms": int(round(settings["duration_s"] * 1000)),
            "adaptive": 1 if settings["adaptive_stop"] else 0,
            "sample_interval_ms": int(round(1000 / settings["target_rate_hz"])),
            "cv_low_mv": int(round(settings["cv_low_v"] * 1000)),
            "cv_high_mv": int(round(settings["cv_high_v"] * 1000)),
            "cv_rate_mv_s": int(round(settings["cv_scan_rate_v_s"] * 1000)),
            "cv_cycles": settings["cv_cycles"],
            "cv_step_mv": int(round(settings["cv_step_v"] * 1000)),
            "cv_eis": eis_codes[settings["cv_eis_fsr_uA"]],
            "it_use_eis": 1 if settings["fsr_nA"] in IT_WIDE_FSR_OPTIONS else 0,
            "it_eis": wide_eis_codes.get(settings["fsr_nA"], 3),
        }

    @classmethod
    def runtime_measurement_command(
        cls, payload: dict[str, Any], request_id: str
    ) -> str:
        contract = cls.runtime_measurement_contract(payload)
        values = (
            "mode", "start_mv", "target_mv", "quiet_ms", "duration_ms",
            "adaptive", "sample_interval_ms", "cv_low_mv", "cv_high_mv",
            "cv_rate_mv_s", "cv_cycles", "cv_step_mv", "cv_eis",
            "it_use_eis", "it_eis",
        )
        return "MEAS " + " ".join(
            [*(str(contract[key]) for key in values), request_id]
        )

    def apply(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.apply_lock.acquire(blocking=False):
            raise RuntimeError("已有另一项参数应用正在进行，请等待完成")
        started_at = time.monotonic()
        DIAGNOSTICS.record(
            "info", "settings.apply.started", "Hardware settings apply started",
            transport=HARDWARE_TRANSPORT,
            device=_selected_device_copy(),
            settings=payload,
        )
        try:
            result = self._apply_locked(payload)
            DIAGNOSTICS.record(
                "info", "settings.apply.completed",
                "Hardware settings apply completed",
                transport=HARDWARE_TRANSPORT,
                firmware_source=self._saved_firmware_source,
                firmware_sha256=self._saved_firmware_hash,
                duration_ms=round((time.monotonic() - started_at) * 1000, 1),
                settings=result.get("settings"),
            )
            return result
        except Exception as exc:
            diagnostic_id = DIAGNOSTICS.exception(
                "settings.apply.failed", "Hardware settings apply failed", exc,
                transport=HARDWARE_TRANSPORT,
                device=_selected_device_copy(),
                duration_ms=round((time.monotonic() - started_at) * 1000, 1),
                settings=payload,
            )
            try:
                setattr(exc, "diagnostic_id", diagnostic_id)
            except (AttributeError, TypeError):
                pass
            raise
        finally:
            self.apply_lock.release()

    def _apply_locked(self, payload: dict[str, Any]) -> dict[str, Any]:
        settings = self.validate(payload)
        with self.lock:
            self.settings = settings
            self.state = "applying"
            self.message = "正在检查设备连接与通用固件"
            self.error = ""
            self.applied = False
        potential_mv = int(round(settings["potential_v"] * 1000))
        initial_potential_mv = int(round(settings["initial_potential_v"] * 1000))
        prestep_ms = int(round(settings["prestep_s"] * 1000))
        duration_ms = int(round(settings["duration_s"] * 1000))
        cv_step_interval_ms = int(round(
            settings["cv_step_v"] / settings["cv_scan_rate_v_s"] * 1000
        ))
        it_sample_interval_ms = int(round(1000 / settings["target_rate_hz"]))
        sens_code = settings["sens_period_code"]
        sens_ms = SENS_PERIOD_MS[sens_code]
        working_electrode_mv = self.working_electrode_mv(settings)
        header = (
            "#ifndef SENSUS_MEASUREMENT_CONFIG_H\n"
            "#define SENSUS_MEASUREMENT_CONFIG_H\n\n"
            "/* Generated by the local electrochemistry workstation. */\n"
            f"#define GUI_MEASUREMENT_MODE_CV {1 if settings['method'] == 'cv' else 0}\n"
            f"#define GUI_WP_V_WE_MV {working_electrode_mv}\n"
            "#define GUI_CV_V_WE_MV 800\n"
            f"#define GUI_WP_FSR {FSR_OPTIONS.get(settings['fsr_nA'], 'MAX30131_FSR_2000NA')}\n"
            f"#define GUI_WP_OFFSET_SEL {OFFSET_OPTIONS[settings['offset_mode']][0]}\n"
            f"#define GUI_IT_USE_EIS {1 if settings['fsr_nA'] in IT_WIDE_FSR_OPTIONS else 0}\n"
            f"#define GUI_IT_EIS_FSR {IT_WIDE_FSR_OPTIONS.get(settings['fsr_nA'], 'MAX30131_EIS_FSR_40UA')}\n"
            f"#define GUI_IT_SAMPLE_INTERVAL_MS {it_sample_interval_ms}U\n"
            f"#define GUI_IT_ADAPTIVE_STOP {1 if settings['adaptive_stop'] else 0}\n"
            f"#define GUI_WP_START_E_MV {initial_potential_mv}\n"
            f"#define GUI_WP_E_MV {potential_mv}\n"
            f"#define GUI_PRESTEP_DURATION_MS {prestep_ms}U\n"
            f"#define GUI_MEASUREMENT_DURATION_MS {duration_ms}U\n\n"
            f"#define GUI_CV_LOW_E_MV {int(round(settings['cv_low_v'] * 1000))}\n"
            f"#define GUI_CV_HIGH_E_MV {int(round(settings['cv_high_v'] * 1000))}\n"
            f"#define GUI_CV_SCAN_RATE_MV_S {int(round(settings['cv_scan_rate_v_s'] * 1000))}U\n"
            f"#define GUI_CV_CYCLES {settings['cv_cycles']}U\n"
            f"#define GUI_CV_STEP_MV {int(round(settings['cv_step_v'] * 1000))}U\n"
            f"#define GUI_CV_STEP_INTERVAL_MS {cv_step_interval_ms}U\n"
            f"#define GUI_CV_QUIET_DURATION_MS {int(round(settings['cv_quiet_s'] * 1000))}U\n\n"
            f"#define GUI_CV_EIS_FSR {CV_EIS_FSR_OPTIONS[settings['cv_eis_fsr_uA']]}\n\n"
            f"#define GUI_SENS_PERIOD_CODE 0x{sens_code:X}U\n"
            f"#define GUI_SENS_PERIOD_MS {sens_ms}U\n\n"
            "#endif\n"
        )
        try:
            # App may have started before the USB cable was connected. Refresh
            # only behind this idle-operation gate so an active measurement
            # never loses its DATA stream.
            _refresh_usb_transport()
            usb_transport = HARDWARE_TRANSPORT == "serial"
            if not usb_transport:
                _release_stale_measurement_bridge()
            firmware_source = "build"
            prebuilt_dir = (
                V51_PREBUILT_IMAGE.parent.parent
                if usb_transport else FIRMWARE_PREBUILT_DIR
            )
            prebuilt_metadata = prebuilt_dir / "firmware.json"
            try:
                metadata = json.loads(prebuilt_metadata.read_text(encoding="utf-8"))
                if not isinstance(metadata, dict):
                    raise TypeError("firmware metadata must be an object")
                prebuilt_settings = self.validate(metadata["settings"])
            except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
                metadata = {}
                prebuilt_settings = None
            # Portable packages ship one validated runtime-configurable image.
            # Every supported UI condition uses that image and is committed
            # transactionally over RTT/CDC before START, so the target machine
            # never needs NCS, Zephyr or a compiler. Source checkouts retain the
            # existing compile path for developer firmware changes.
            can_use_prebuilt = (
                runtime.is_frozen()
                or self._supports_runtime_settings(metadata)
                or (settings == prebuilt_settings and not usb_transport)
            )
            if can_use_prebuilt:
                runtime_supported = self._supports_runtime_settings(metadata)
                if runtime.is_frozen() and not runtime_supported:
                    raise RuntimeError("随包通用固件元数据缺失或版本不兼容")
                firmware_source = "prebuilt"
                if not runtime.is_frozen():
                    FIRMWARE_CONFIG.write_text(header, encoding="utf-8")
                firmware_hex = (
                    V51_PREBUILT_IMAGE if usb_transport
                    else FIRMWARE_PREBUILT_DIR / "zephyr.hex"
                )
                self._set_apply_message("正在校验内置通用固件")
                firmware_hash = self._verify_prebuilt_artifact(
                    firmware_hex, metadata
                )
                updated = False
                if runtime_supported:
                    self._set_apply_message("正在核对目标板与通用固件")
                    runtime_state, runtime_detail = self._wait_for_runtime_firmware(
                        metadata, usb_transport=usb_transport, attempts=2,
                    )
                    if runtime_state == "invalid":
                        raise RuntimeError(
                            "通用固件已响应，但 MAX30131 物理配置核对失败:"
                            + (runtime_detail or "确认信息不完整")
                        )
                    if runtime_state == "transport_error":
                        raise RuntimeError(
                            "无法核对当前硬件连接:"
                            + (runtime_detail or "传输通道不可用")
                        )
                    if runtime_state == "incomplete":
                        raise RuntimeError(
                            "通用固件应答不完整，未执行烧录:"
                            + (runtime_detail or "缺少带标识的配置确认行")
                        )
                    if runtime_state == "ready":
                        DIAGNOSTICS.record(
                            "info", "firmware.runtime.reused",
                            "Existing runtime firmware passed tagged physical verification",
                            transport="serial" if usb_transport else "rtt",
                            firmware=firmware_hex,
                        )
                    else:
                        updated = True
                        DIAGNOSTICS.record(
                            "info", "firmware.runtime.update_required",
                            "Runtime protocol was not found; firmware update is required",
                            transport="serial" if usb_transport else "rtt",
                            detail=runtime_detail,
                        )
                        if usb_transport:
                            if not SERIAL_SMP_PORT:
                                raise RuntimeError(
                                    "当前 USB 固件不支持通用协议，且未找到同一设备的 SMP CDC，无法安全升级"
                                )
                            self._set_apply_message(
                                "固件不兼容，正在通过 USB 更新并校验"
                            )
                            self._upgrade_v51_firmware(firmware_hex)
                        else:
                            self._set_apply_message(
                                "固件不兼容，正在通过 J-Link 更新并校验"
                            )
                            self._flash_firmware(firmware_hex)
                        self._set_apply_message("正在确认更新后的运行时协议")
                        verified_state, verification_detail = self._wait_for_runtime_firmware(
                            metadata, usb_transport=usb_transport,
                        )
                        if verified_state != "ready":
                            raise RuntimeError(
                                "固件更新后未通过运行时核对:"
                                + (verification_detail or "未收到完整应答")
                            )
                else:
                    # Source-only compatibility for an old fixed-condition image.
                    self._set_apply_message("正在更新并校验固定条件固件")
                    self._flash_firmware(firmware_hex)
                    updated = True
                SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
                SETTINGS_PATH.write_text(json.dumps({
                    "settings": settings,
                    "firmware_source": firmware_source,
                    "firmware_sha256": firmware_hash,
                    "transport": "serial" if usb_transport else "rtt",
                }, indent=2, ensure_ascii=False), encoding="utf-8")
                with self.lock:
                    self.settings = settings
                    self._saved_firmware_hash = firmware_hash
                    self._saved_transport = "serial" if usb_transport else "rtt"
                    self._saved_firmware_source = firmware_source
                    self.applied = True
                    self.state = "applied"
                    self.message = (
                        (
                            "通用固件已更新并确认；当前条件已保存，"
                            "测量前会自动下发并核验"
                        )
                        if runtime_supported and updated
                        else (
                            "通用固件已确认；当前条件已保存，"
                            "测量前会自动下发并核验"
                        )
                        if runtime_supported
                        else "推荐条件已使用内置固件应用到硬件"
                    )
                    self.error = ""
                return self.snapshot()
            self._set_apply_message("内置固件不支持当前条件，正在准备开发者构建")
            if (not _IS_WIN and (
                not (NCS_DIR / "zephyr/zephyr-env.sh").exists()
                or not NCS_VENV_ACTIVATE.exists()
                or not (ZEPHYR_SDK_DIR / (
                    "gnu/arm-zephyr-eabi/bin/arm-zephyr-eabi-gcc"
                )).exists()
            )):
                raise RuntimeError(
                    "当前条件与随包固件不同；请先双击“03-安装固件工具链.command”"
                )
            FIRMWARE_CONFIG.write_text(header, encoding="utf-8")
            self._set_apply_message("正在编译开发者固件，请保持设备连接")
            # 🔴 west 装在 NCS 自己的 venv 里(默认 ~/ncs/.venv/bin/west)。
            #    `zephyr-env.sh` 只把 $ZEPHYR_BASE/scripts 塞进 PATH,**不激活该 venv**
            #    ⇒ 不先激活就是 `zsh:1: command not found: west`,按钮看起来"没反应"
            #    (失败 <1s,label 闪一下就弹回去)。2026-08-09 实测确认。
            #    只在这个子 shell 里激活:NCS venv 与本工作站 venv 依赖冲突,不可合并。
            if _IS_WIN:
                # Windows: 使用 cmd /c 执行构建。需要先把 zephyr-env.cmd 跑通再调 west。
                # %CD% 在 cmd /c 里自动随 cwd 设定。
                build = (
                    f"{ncs_venv_prefix()}"
                    f"call {shlex.quote(str(NCS_DIR / 'zephyr/zephyr-env.cmd'))} && "
                    + ("west build -p always -b pa_converter_v51 "
                     "-d software/firmware/build software/firmware -- "
                     "-DSB_CONFIG_BOOTLOADER_MCUBOOT=y "
                     "-DBOARD_ROOT=%CD%/software/firmware "
                     "-DDTS_ROOT=%CD%/software/firmware"
                     if usb_transport else
                     "west build -b pa_converter_v40 -d software/firmware/build "
                     "software/firmware -- -DBOARD_ROOT=%CD%/software/firmware "
                     "-DDTS_ROOT=%CD%/software/firmware")
                )
                self._run_build(["cmd", "/c", build])
            else:
                build = (
                    f"{ncs_venv_prefix()}"
                    f"export ZEPHYR_TOOLCHAIN_VARIANT=zephyr && "
                    f"export ZEPHYR_SDK_INSTALL_DIR={shlex.quote(str(ZEPHYR_SDK_DIR))} && "
                    f"source {shlex.quote(str(NCS_DIR / 'zephyr/zephyr-env.sh'))} && "
                    + ("west build -p always -b pa_converter_v51 "
                     "-d software/firmware/build software/firmware -- "
                     "-DSB_CONFIG_BOOTLOADER_MCUBOOT=y "
                     "-DBOARD_ROOT=$PWD/software/firmware "
                     "-DDTS_ROOT=$PWD/software/firmware"
                     if usb_transport else
                     "west build -b pa_converter_v40 -d software/firmware/build "
                     "software/firmware -- -DBOARD_ROOT=$PWD/software/firmware "
                     "-DDTS_ROOT=$PWD/software/firmware")
                )
                self._run_build(["/bin/zsh", "-lc", build])
            firmware_hex = FIRMWARE_BUILD_DIR / "zephyr.hex"
            firmware_artifact = firmware_hex
            if usb_transport:
                firmware_artifact = FIRMWARE_BUILD_DIR / "zephyr.signed.bin"
                self._set_apply_message("正在通过 USB 烧录并校验开发者固件")
                self._upgrade_v51_firmware(firmware_artifact)
            else:
                self._set_apply_message("正在通过 J-Link 烧录并校验开发者固件")
                self._flash_firmware(firmware_hex)
            SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
            firmware_hash = self._firmware_hash(firmware_artifact)
            SETTINGS_PATH.write_text(json.dumps({
                "settings": settings,
                "firmware_source": firmware_source,
                "firmware_sha256": firmware_hash,
                "transport": "serial" if usb_transport else "rtt",
            }, indent=2, ensure_ascii=False), encoding="utf-8")
        # RuntimeError:_flash_firmware() 的「exit 0 但没烧成」判据会抛它,
        # 不接住的话会变成未处理 500,state 停在 "applying",前端只能看到通用错误。
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired,
                OSError, RuntimeError) as exc:
            detail = _meaningful_process_tail(
                getattr(exc, "stderr", ""),
                getattr(exc, "stdout", ""),
                str(exc),
            )
            with self.lock:
                self.state = "error"
                self.applied = False
                self.error = " | ".join(detail) if detail else "固件编译或烧录失败"
                self.message = "参数应用失败"
            raise RuntimeError(self.error) from exc
        with self.lock:
            self.settings = settings
            self._saved_firmware_hash = firmware_hash
            self._saved_transport = "serial" if usb_transport else "rtt"
            self._saved_firmware_source = firmware_source
            self.applied = True
            self.state = "applied"
            self.message = "参数已写入硬件"
            return self.snapshot()

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return _json_safe({
                "settings": self.settings,
                "applied": self.applied,
                "state": self.state,
                "message": self.message,
                "error": self.error,
                "firmware_source": self._saved_firmware_source,
                "firmware_sha256": self._saved_firmware_hash,
                "firmware_transport": self._saved_transport,
                "native_rate_hz": (
                    self.settings["cv_scan_rate_v_s"] / self.settings["cv_step_v"]
                    if self.settings["method"] == "cv"
                    else (
                        self.settings["target_rate_hz"]
                        if self.settings["fsr_nA"] in IT_WIDE_FSR_OPTIONS
                        else 1000 / SENS_PERIOD_MS[self.settings["sens_period_code"]]
                    )
                ),
                "output_points": (
                    int(round(self.settings["duration_s"]
                              * self.settings["cv_scan_rate_v_s"]
                              / self.settings["cv_step_v"]))
                    if self.settings["method"] == "cv"
                    else (
                        None if self.settings["adaptive_stop"]
                        else int(round(self.settings["duration_s"]
                                       * self.settings["target_rate_hz"]))
                    )
                ),
                "cv_segments": self.settings["cv_cycles"] * 2,
            })


class MeasurementController:
    """Own the one active acquisition and expose a thread-safe snapshot."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.state = "idle"
        self.message = "等待开始测量"
        self.error = ""
        self.run_id = ""
        self.run_dir: Path | None = None
        self.raw_path: Path | None = None
        self.raw_log: Path | None = None
        self.resampled_path: Path | None = None
        self.filtered_path: Path | None = None
        self.filter_meta: dict[str, Any] = {}
        self.summary_path: Path | None = None
        self.plot_path: Path | None = None
        self.started_at: float | None = None
        self.finished_at: float | None = None
        self.process: subprocess.Popen[str] | None = None
        self.cmd_path: Path | None = None   # 方案 C:在线切档命令文件
        self.cell_v_path: Path | None = None  # 电极电压连采 CSV(与电流不同速率)
        self.audit_path: Path | None = None   # 配置变更审计 jsonl(每次改参数留痕)
        # DEBUG 页的增量读状态。全部按"读位置 + 累积列表"做 ⇒ 1Hz 刷新不随
        # 文件增长变慢(一轮 180s 就上千行,全量重读会明显卡)。
        self._audit_pos = 0
        self._audit_pending = ""
        self._auto_get_at = 0.0
        self._audit_cache: list[dict[str, Any]] = []
        self._cfg_live: dict[str, Any] = {}
        self._cfg_epochs: dict[tuple[int, str | None], dict[str, Any]] = {}
        self._afe_status: dict[str, Any] = {}
        self._last_reject: dict[str, Any] = {}
        self._phase: dict[str, Any] = {}
        self._dbg_cur_pos = 0
        self._dbg_cur: list[dict[str, Any]] = []
        self._dbg_cur_hdr: list[str] | None = None
        self._dbg_cur_pending = ""
        self._dbg_cv_pos = 0
        self._dbg_cv: list[dict[str, Any]] = []
        self._dbg_cv_hdr: list[str] | None = None
        self._dbg_cv_pending = ""
        # 方案 C:运行时档位真值。**不能用 SettingsController 的值代替** ——
        # 那是"最后一次烧录进去的编译期默认",而 RANGE 命令会在运行中改掉它,
        # 两者可以不一致。唯一权威来源是固件回的 RANGE_APPLIED 行。
        self.range_runtime: dict[str, Any] = {
            "pending": None, "applied": None, "rejected": None, "at": None,
        }
        self._rtt_pos = 0
        self._rtt_pending = ""
        # 两相测量:过零并稳定后切到测量档。默认**手动** —— 自动切档会往数据里
        # 注入一个跨档直流台阶(§13b 实测 +22.9nA),这一步该由人点。
        self.auto_switch_meas = False
        self._auto_switch_done = False
        self.summary: dict[str, Any] | None = None
        self.workflow_result: dict[str, Any] | None = None
        self.thread: threading.Thread | None = None
        self.metadata: dict[str, Any] = {}
        self.on_complete: Any = None
        self.completion_hook: Any = None
        self.settings = dict(SettingsController.DEFAULTS)
        self.filter_config = dict(FILTER_DEFAULTS)
        self.plateau_config = PlateauConfig.validate(None)
        self.bridge_process: subprocess.Popen[str] | None = None
        self.bridge_log_handle: Any = None
        self.user_stop_requested = False
        self.auto_stop_requested = False
        self._bridge_stop_forced = False
        self._auto_stop_evidence: dict[str, Any] | None = None
        self._plateau_last_segment = 0
        self._plateau_consecutive_passes = 0
        self._plateau_evaluation: dict[str, Any] | None = None
        self._plateau_progress: dict[str, Any] = {}
        self._plateau_cfg_epoch: int | None = None
        self._plateau_context_epoch: int | None = None
        self._plateau_expected_epoch: int | None = None
        self._plateau_context_pending = False
        self._plateau_context_start_s = 0.0
        self._plateau_minimum_gate_until_s = 0.0
        self.debug_waiting_for_start = False
        self._debug_pending_cfg: dict[str, Any] | None = None
        self._config_gate: dict[str, Any] = {"state": "idle"}
        self._config_gate_event = threading.Event()
        self._prestart_gate_failed = False
        # 只能拿本次 RTT 会话里收到的 CFG_CONFIRMED 作为下发依据。
        # OpenOCD 重建连接时可能会让 MCU 重启；上一轮的 cfg 缓存在那之后
        # 已不再代表硬件现状。
        self._cfg_confirmed_this_session = False
        self._cfg_live_epoch: int | None = None
        self._hardware_taint: dict[str, Any] | None = None
        self._stability_eta_estimator = StabilityEtaEstimator()
        self._prepared_live_stage: PreparedLiveStage | None = None
        self._rolling_metrics: dict[str, Any] = {}
        self._stability_eta: dict[str, Any] = {}
        self._last_complete_rolling_metrics: dict[str, Any] | None = None
        self._last_complete_rolling_epoch: object = None
        self._diagnostic_completion_logged_run_id = ""
        self._stop_requested_rolling_metrics: dict[str, Any] | None = None
        self._rolling_metrics_frozen: dict[str, Any] | None = None
        self._stability_eta_frozen: dict[str, Any] | None = None
        self._live_analysis_last_refresh = 0.0
        self._reset_live_analysis_locked()
        self._reset_data_cache()

    @staticmethod
    def _empty_rolling_metrics(
        status: str = "idle", reason: str = "等待测量",
    ) -> dict[str, Any]:
        return {
            "status": status,
            "reason": reason,
            "window_s": None,
            "coverage_s": 0.0,
            "native_point_count": 0,
            "valid_native_point_count": 0,
            "expected_native_point_count": None,
            "window_point_count": 0,
            "progress_percent": 0.0,
            "steady_current_nA": None,
            "noise_nA": None,
            "slope_nA_per_s": None,
            "trend_state": "insufficient",
            "tolerance_nA": None,
            "robust_scatter_nA": None,
            "slope_limit_nA_per_s": None,
            "filter_effective": False,
            "filter_meta": {},
            "stage_key": None,
            "stage_start_s": None,
            "stage_end_s": None,
            "stage_age_s": None,
        }

    @staticmethod
    def _disabled_stability_eta(reason: str = "adaptive_stop_disabled") -> dict[str, Any]:
        return {
            "status": "disabled",
            "display_text": "自动停止未启用",
            "seconds": None,
            "direction": "flat",
            "confidence": None,
            "tau_s": None,
            "i_inf_nA": None,
            "amplitude_nA": None,
            "noise_sigma_nA": None,
            "reason": reason,
            "window_s": None,
            "stage_age_s": None,
            "minimum_stage_s": 45.0,
            "history_window_s": 120.0,
            "platform_window_s": None,
            "reset_consecutive": False,
            "suggested_stage_start_s": None,
        }

    def _reset_live_analysis_locked(self) -> None:
        self._prepared_live_stage = None
        self._rolling_metrics = self._empty_rolling_metrics()
        self._last_complete_rolling_metrics = None
        self._last_complete_rolling_epoch = None
        self._stop_requested_rolling_metrics = None
        if (
            self.settings.get("method") == "it"
            and self.settings.get("adaptive_stop")
        ):
            self._stability_eta = self._disabled_stability_eta(
                "waiting_for_measurement"
            )
            self._stability_eta.update({
                "status": "idle",
                "display_text": "等待测量",
                "platform_window_s": self.plateau_config.window_duration_s,
            })
        else:
            self._stability_eta = self._disabled_stability_eta()
        self._rolling_metrics_frozen = None
        self._stability_eta_frozen = None
        self._live_analysis_last_refresh = 0.0
        self._stability_eta_estimator.reset()

    def _invalidate_rolling_display_locked(self) -> None:
        """Rebuild display/noise metrics without discarding ETA direction state."""

        self._prepared_live_stage = None
        self._rolling_metrics = self._empty_rolling_metrics(
            "accumulating", "滤波显示设置已更新，正在重算",
        )
        if self._last_complete_rolling_metrics is not None:
            # Display-only filtering changes noise, but the formal steady
            # current and slope still use raw data. Preserve those scientific
            # values for an immediate stop while invalidating filter evidence.
            self._last_complete_rolling_metrics = {
                **self._last_complete_rolling_metrics,
                "noise_nA": None,
                "filter_effective": False,
                "filter_meta": {},
            }
        self._stop_requested_rolling_metrics = None
        self._rolling_metrics_frozen = None
        self._live_analysis_last_refresh = 0.0

    @staticmethod
    def _rolling_metric_is_complete(metrics: dict[str, Any] | None) -> bool:
        if not isinstance(metrics, dict):
            return False
        value = metrics.get("steady_current_nA")
        if isinstance(value, bool):
            return False
        try:
            return math.isfinite(float(value))
        except (TypeError, ValueError):
            return False

    def _last_complete_with_bookkeeping_locked(
        self, latest: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        if not self._rolling_metric_is_complete(
            self._last_complete_rolling_metrics
        ):
            return None
        result = dict(self._last_complete_rolling_metrics or {})
        if isinstance(latest, dict):
            for field in (
                "native_point_count", "valid_native_point_count",
                "expected_native_point_count", "progress_percent",
            ):
                if field in latest:
                    result[field] = latest[field]
        return result

    def _reset_data_cache(self) -> None:
        self._data_cache_path: Path | None = None
        self._data_cache_position = 0
        self._data_cache_pending = ""
        self._data_cache_header: list[str] | None = None
        self._data_cache_first_dev_ms: float | None = None
        self._data_last_dev_ms: float | None = None
        self._data_cache: dict[str, list[Any]] = {
            "time_s": [], "current_nA": [], "valid": [], "epoch": [],
        }
        self._data_context_reset_pending = False

    def set_filter_config(self, config: dict[str, Any]) -> None:
        normalized = validate_filter_config(config)
        with self.lock:
            if (
                self.state == "running"
                and (self.user_stop_requested or self.auto_stop_requested)
            ):
                return
            before = self._analysis_filter_config()
            previous = dict(self.filter_config)
            self.filter_config = normalized
            if self.state != "running":
                return
            if before != self._analysis_filter_config():
                self._reset_plateau_monitor_locked(
                    preserve_minimum_gate=True,
                )
            elif previous != self.filter_config:
                self._invalidate_rolling_display_locked()

    def set_plateau_config(self, config: PlateauConfig | dict[str, Any]) -> None:
        normalized = PlateauConfig.validate(config)
        with self.lock:
            if (
                self.state == "running"
                and (self.user_stop_requested or self.auto_stop_requested)
            ):
                return
            if normalized != self.plateau_config:
                self.plateau_config = normalized
                if self.state == "running":
                    self._reset_plateau_monitor_locked()

    def _reset_plateau_monitor_locked(
        self, *, hardware_context_changed: bool = False,
        expected_epoch: int | None = None, clock_reset: bool = False,
        preserve_minimum_gate: bool = False,
    ) -> None:
        self._plateau_last_segment = 0
        self._plateau_consecutive_passes = 0
        self._plateau_evaluation = None
        self._plateau_progress = {}
        if not preserve_minimum_gate:
            self._plateau_minimum_gate_until_s = 0.0
        self._reset_live_analysis_locked()
        if clock_reset:
            self._plateau_context_start_s = 0.0
            self._plateau_context_epoch = None
            self._plateau_expected_epoch = None
            self._plateau_context_pending = False
        elif hardware_context_changed:
            # The incremental cache can lag the collector file. Do not infer a
            # boundary from its last timestamp: unread old-epoch rows would then
            # be mistaken for post-change samples. The first matching CSV epoch
            # establishes the boundary in _plateau_context_data_locked().
            self._plateau_expected_epoch = expected_epoch
            self._plateau_context_pending = True

    def is_busy(self) -> bool:
        """Return true until the acquisition watcher has finished callbacks."""
        with self.lock:
            return self.state == "running" or bool(
                self.thread is not None and self.thread.is_alive()
            )

    def wait_for_completion(self) -> None:
        """Wait until acquisition analysis, exports, and callbacks are complete."""
        with self.lock:
            watcher = self.thread
        if (watcher is not None and watcher.is_alive()
                and watcher is not threading.current_thread()):
            watcher.join()

    def _analysis_filter_config(self) -> dict[str, Any]:
        config = dict(self.filter_config)
        if config.get("mode") != "analysis":
            # Display-only knobs must not participate in the formal-analysis
            # identity; otherwise changing a preview cutoff resets the platform
            # streak and ETA even though both still consume raw current.
            return validate_filter_config({"mode": "off"})
        return config

    def _expected_native_rate_hz_locked(self) -> float | None:
        if self.metadata.get("debug"):
            period_ms = self._cfg_live.get("period_ms")
            if isinstance(period_ms, (int, float)) and period_ms > 0:
                return 1000.0 / float(period_ms)
            return None
        if self.settings.get("method") == "cv":
            step = float(self.settings.get("cv_step_v") or 0.0)
            scan_rate = float(self.settings.get("cv_scan_rate_v_s") or 0.0)
            return scan_rate / step if step > 0 and scan_rate > 0 else None
        if self.settings.get("fsr_nA") in IT_WIDE_FSR_OPTIONS:
            rate = float(self.settings.get("target_rate_hz") or 0.0)
            return rate if rate > 0 else None
        period_code = self.settings.get("sens_period_code")
        period_ms = SENS_PERIOD_MS.get(period_code)
        return 1000.0 / period_ms if period_ms else None

    @staticmethod
    def _native_valid_count(data: dict[str, list[Any]]) -> int:
        return sum(
            bool(is_valid)
            and math.isfinite(float(timestamp))
            and math.isfinite(float(current))
            for timestamp, current, is_valid in zip(
                data.get("time_s", []),
                data.get("current_nA", []),
                data.get("valid", []),
            )
        )

    def _cv_rolling_metrics_locked(
        self, data: dict[str, list[Any]], *, completed: bool = False,
    ) -> dict[str, Any]:
        payload = self._empty_rolling_metrics(
            "complete" if completed else "collecting",
            "CV 指标保持实时电位与当前循环语义",
        )
        count = len(data.get("time_s", []))
        valid_count = self._native_valid_count(data)
        elapsed_s = float(data["time_s"][-1]) if count else 0.0
        duration_s = float(self.settings.get("duration_s") or 0.0)
        progress = (
            100.0 if completed
            else min(100.0, max(0.0, elapsed_s / duration_s * 100.0))
            if duration_s > 0 else 0.0
        )
        native_rate = self._expected_native_rate_hz_locked()
        payload.update({
            "native_point_count": count,
            "valid_native_point_count": valid_count,
            "expected_native_point_count": (
                int(round(duration_s * native_rate))
                if native_rate is not None and duration_s > 0 else None
            ),
            "progress_percent": progress,
            "stage_end_s": elapsed_s if count else None,
            "stage_age_s": elapsed_s if count else None,
        })
        return payload

    def _refresh_live_analysis_locked(
        self, data: dict[str, list[Any]] | None = None, *, force: bool = False,
    ) -> None:
        if self._rolling_metrics_frozen is not None and not force:
            return
        now = time.monotonic()
        if (
            not force
            and self._live_analysis_last_refresh > 0.0
            and now - self._live_analysis_last_refresh < LIVE_ANALYSIS_REFRESH_S
        ):
            return
        data = data if data is not None else self._data()
        if self._data_context_reset_pending:
            self._data_context_reset_pending = False
            self._reset_plateau_monitor_locked(clock_reset=True)
        if self.settings.get("method") == "cv":
            completed = self.state == "completed"
            self._rolling_metrics = self._cv_rolling_metrics_locked(
                data, completed=completed,
            )
            self._stability_eta_estimator.reset()
            self._stability_eta = self._disabled_stability_eta("cv_method")
            self._prepared_live_stage = None
            self._live_analysis_last_refresh = now
            return

        context_data = self._plateau_context_data_locked(
            data, update_state=not force,
        )
        expected_rate = self._expected_native_rate_hz_locked()
        elapsed_s = (
            float(data["time_s"][-1]) if data.get("time_s") else 0.0
        )
        try:
            stage = prepare_live_stage(
                context_data.get("time_s", []),
                context_data.get("current_nA", []),
                context_data.get("valid", []),
                context_data.get("epoch"),
                fit_window_s=float(
                    self.settings.get("fit_window_s") or FIT_WINDOW_S
                ),
                filter_config=self.filter_config,
                plateau_config=self.plateau_config,
                expected_sample_rate_hz=expected_rate,
            )
            metrics = metrics_from_stage(
                stage,
                run_state=self.state,
                fixed_duration_s=(
                    None if self.settings.get("adaptive_stop")
                    else float(self.settings.get("duration_s") or 0.0)
                ),
                run_elapsed_s=elapsed_s,
            )
            metrics.update({
                "native_point_count": len(data.get("time_s", [])),
                "valid_native_point_count": self._native_valid_count(data),
                "expected_native_point_count": (
                    None if self.settings.get("adaptive_stop")
                    or expected_rate is None
                    else int(round(
                        float(self.settings.get("duration_s") or 0.0)
                        * expected_rate
                    ))
                ),
                "timestamp_s": stage.stage_end_s,
            })
            stage_epoch = (
                context_data.get("epoch", [])[-1]
                if context_data.get("epoch") else None
            )
            if (
                self._last_complete_rolling_metrics is not None
                and stage_epoch != self._last_complete_rolling_epoch
            ):
                self._last_complete_rolling_metrics = None
                self._last_complete_rolling_epoch = None
            self._prepared_live_stage = stage
            self._rolling_metrics = metrics
            if stage.window_complete and self._rolling_metric_is_complete(metrics):
                self._last_complete_rolling_metrics = dict(metrics)
                self._last_complete_rolling_epoch = stage_epoch
        except (TypeError, ValueError) as exc:
            self._prepared_live_stage = None
            self._rolling_metrics = self._empty_rolling_metrics(
                "unavailable", f"实时指标不可用：{exc}",
            )
            self._rolling_metrics.update({
                "native_point_count": len(data.get("time_s", [])),
                "valid_native_point_count": self._native_valid_count(data),
            })

        adaptive_enabled = bool(
            self.settings.get("adaptive_stop") and self.state == "running"
        )
        if adaptive_enabled:
            minimum_gate_age_s = max(
                self.plateau_config.window_duration_s,
                float(self.settings.get("fit_window_s") or FIT_WINDOW_S),
            )
            eta = self._stability_eta_estimator.update(
                stage=self._prepared_live_stage,
                plateau_config=self.plateau_config,
                filter_config=self.filter_config,
                plateau_evaluation=self._plateau_evaluation,
                consecutive_passes=self._plateau_consecutive_passes,
                live_metrics=self._rolling_metrics,
                minimum_gate_age_s=minimum_gate_age_s,
                enabled=True,
                force=force,
                now_s=now,
            )
            stage_age = (
                self._prepared_live_stage.stage_age_s
                if self._prepared_live_stage is not None else None
            )
            eta.update({
                "window_s": (
                    min(
                        float(stage_age),
                        self._stability_eta_estimator.config.history_window_s,
                    )
                    if stage_age is not None else None
                ),
                "stage_age_s": stage_age,
                "minimum_stage_s": (
                    self._stability_eta_estimator.config.minimum_stage_s
                ),
                "history_window_s": (
                    self._stability_eta_estimator.config.history_window_s
                ),
                "platform_window_s": (
                    self.plateau_config.window_duration_s
                ),
            })
            self._stability_eta = eta
        else:
            self._stability_eta_estimator.reset()
            self._stability_eta = self._disabled_stability_eta()
        self._live_analysis_last_refresh = now

    def _freeze_live_analysis_locked(
        self, data: dict[str, list[Any]], *, completed: bool,
    ) -> None:
        if self.settings.get("method") == "cv":
            self._rolling_metrics = self._cv_rolling_metrics_locked(
                data, completed=completed,
            )
            self._stability_eta_estimator.reset()
            self._stability_eta = self._disabled_stability_eta("cv_method")
            self._prepared_live_stage = None
            self._live_analysis_last_refresh = time.monotonic()
        elif (
            self.auto_stop_requested
            and isinstance(self._auto_stop_evidence, dict)
            and isinstance(
                self._auto_stop_evidence.get("rolling_metrics"), dict,
            )
        ):
            self._rolling_metrics = dict(
                self._auto_stop_evidence["rolling_metrics"]
            )
            trigger_eta = self._auto_stop_evidence.get("stability_eta")
            if isinstance(trigger_eta, dict):
                self._stability_eta = dict(trigger_eta)
            self._prepared_live_stage = None
            self._live_analysis_last_refresh = time.monotonic()
        elif (
            self.user_stop_requested
            and self._stop_requested_rolling_metrics is not None
        ):
            self._rolling_metrics = dict(
                self._stop_requested_rolling_metrics
            )
            self._prepared_live_stage = None
            self._live_analysis_last_refresh = time.monotonic()
        else:
            self._refresh_live_analysis_locked(data, force=True)
            if not self._rolling_metric_is_complete(self._rolling_metrics):
                cached = self._last_complete_with_bookkeeping_locked(
                    self._rolling_metrics
                )
                if cached is not None:
                    self._rolling_metrics = cached
        rolling = dict(self._rolling_metrics)
        if completed and not self.settings.get("adaptive_stop"):
            rolling["progress_percent"] = 100.0
        if self.settings.get("method") == "cv":
            rolling["status"] = "frozen"
            rolling["reason"] = "CV 原生点数与进度已冻结"
        elif rolling.get("steady_current_nA") is not None:
            rolling["status"] = "frozen"
            rolling["reason"] = "最后一个完整原生窗口已冻结"
        else:
            rolling["status"] = "frozen"
            rolling["reason"] = (
                "测量结束时无完整原生窗口："
                f"{rolling.get('reason') or '数据不足'}"
            )
        self._rolling_metrics = rolling
        self._rolling_metrics_frozen = dict(rolling)

        if self.settings.get("adaptive_stop"):
            eta = dict(self._stability_eta)
            if (
                self.auto_stop_requested
                or self._plateau_consecutive_passes
                >= self.plateau_config.required_consecutive_windows
            ):
                eta.update({
                    "status": "complete",
                    "display_text": "0 秒",
                    "seconds": 0,
                    "confidence": 1.0,
                    "reason": "plateau_gate_complete",
                })
            eta["status"] = "frozen"
            eta["reset_consecutive"] = False
            self._stability_eta = eta
            self._stability_eta_frozen = dict(eta)
        else:
            self._stability_eta = self._disabled_stability_eta()
            self._stability_eta_frozen = dict(self._stability_eta)

    def start(self, metadata: dict[str, Any] | None = None,
              on_complete: Any = None,
              settings: dict[str, Any] | None = None,
              trigger: str = "FRESH_START",
              filter_config: dict[str, Any] | None = None,
              plateau_config: PlateauConfig | dict[str, Any] | None = None,
              verify_runtime_config: bool = False) -> dict[str, Any]:
        # V5.1 re-enumerates both CDC interfaces after firmware upload. Probe
        # immediately before opening the collector so a stale DATA path from
        # the pre-flash device cannot become a misleading exit-code-1 failure.
        if HARDWARE_TRANSPORT_REQUESTED != "rtt" or _selected_device_copy() is not None:
            _refresh_usb_transport()
        with self.lock:
            if self.state == "running" or (
                self.thread is not None and self.thread.is_alive()
            ):
                raise RuntimeError("已有测量正在运行或正在保存结果")
        # External tools may take several seconds on a cold Windows machine.
        # Keep the live status lock free so the UI remains responsive while the
        # start request is in its explicit configuring phase.
        if HARDWARE_TRANSPORT == "rtt":
            _require_jlink_target(JLINK_SERIAL)
        with self.lock:
            if self.state == "running" or (
                self.thread is not None and self.thread.is_alive()
            ):
                raise RuntimeError("已有测量正在运行或正在保存结果")
            self.settings = SettingsController.validate(settings or self.settings)
            self.filter_config = validate_filter_config(filter_config or self.filter_config)
            self.plateau_config = PlateauConfig.validate(
                plateau_config if plateau_config is not None else self.plateau_config
            )
            if verify_runtime_config:
                trigger = "ARMED"
            method = self.settings["method"]
            RUNS_DIR.mkdir(parents=True, exist_ok=True)
            self.run_id = _now_id(method)
            self.run_dir = RUNS_DIR / self.run_id
            self.run_dir.mkdir(parents=True, exist_ok=False)
            self.metadata = dict(metadata or {})
            self._diagnostic_completion_logged_run_id = ""
            DIAGNOSTICS.record(
                "info", "measurement.starting", "Measurement is starting",
                run_id=self.run_id,
                run_dir=self.run_dir,
                method=method,
                transport=HARDWARE_TRANSPORT,
                device=_selected_device_copy(),
                trigger=trigger,
                metadata_keys=sorted(self.metadata),
                settings=self.settings,
            )
            live_raw_path = str(self.metadata.get("live_raw_path") or "")
            self.raw_path = Path(live_raw_path) if live_raw_path else self.run_dir / "raw.csv"
            self.raw_path.parent.mkdir(parents=True, exist_ok=True)
            self.raw_log = self.run_dir / "rtt.log"
            self.cmd_path = self.run_dir / "cmd.txt"
            self.cell_v_path = self.run_dir / "cellv.csv"
            # 🔴 审计与 CSV **同目录同 run_id** —— 用户要的"每次测量都要出 csv 和
            #    对应的操作审计日志"就是靠这个绑定,不靠时间戳事后猜。
            self.audit_path = self.run_dir / "audit.jsonl"
            # 🔴 新一轮必须清缓存与读位置 —— 不清会把上一轮的审计与曲线接在
            #    这一轮后面(文件换了,读位置却没归零 ⇒ 直接读到文件尾之外)。
            self._audit_pos = 0
            self._audit_pending = ""
            self._auto_get_at = 0.0
            self._audit_cache = []
            # OpenOCD 重建 RTT 连接可能会重启 MCU。上一轮缓存若仍是
            # V_WE=250，而新开机的硬件已回到 1200，begin() 就会误判“已一致”
            # 而只发 START。因此新会话必须清除硬件真值，等本轮 CFG_*
            # 重新建立。前端在点击前已保留表单值，不会因此丢参数。
            self._cfg_live = {}
            self._cfg_live_epoch = None
            self._cfg_epochs = {}
            self._afe_status = {}
            self._cfg_confirmed_this_session = False
            self._hardware_taint = None
            self._last_reject = {}
            self._phase = {}          # 阶段是**本轮**的属性,新一轮必须清
            self._dbg_cur_pos = 0
            self._dbg_cur = []
            self._dbg_cur_hdr = None
            self._dbg_cur_pending = ""
            self._dbg_cv_pos = 0
            self._dbg_cv = []
            self._dbg_cv_hdr = None
            self._dbg_cv_pending = ""
            self.range_runtime = {"pending": None, "applied": None,
                                  "rejected": None, "at": None}
            self._rtt_pos = 0
            self._rtt_pending = ""
            self._auto_switch_done = False
            self.resampled_path = self.run_dir / (
                "cv.csv" if method == "cv" else "resampled_10hz.csv"
            )
            self.filtered_path = self.run_dir / (
                "cv-filtered.csv" if method == "cv" else "resampled_10hz-filtered.csv"
            )
            self.filter_meta = {}
            self.summary_path = self.run_dir / "summary.json"
            self.plot_path = self.run_dir / (
                "cv_curve.png" if method == "cv" else "it_curve.png"
            )
            self.started_at = time.time()
            self.finished_at = None
            self.summary = None
            self.workflow_result = None
            self.error = ""
            self.user_stop_requested = False
            self.auto_stop_requested = False
            self._bridge_stop_forced = False
            self._auto_stop_evidence = None
            self._reset_plateau_monitor_locked()
            self._plateau_context_start_s = 0.0
            self._plateau_cfg_epoch = None
            self._plateau_context_epoch = None
            self._plateau_expected_epoch = None
            self._plateau_context_pending = False
            self.debug_waiting_for_start = trigger == "ARMED"
            self._debug_pending_cfg = None
            self._prestart_gate_failed = False
            self._config_gate_event = threading.Event()
            gate_request_id = hashlib.sha256(
                f"{self.run_id}:{time.time_ns()}".encode("ascii")
            ).hexdigest()[:12]
            self._config_gate = (
                {
                    "state": "checking",
                    "expected": SettingsController.runtime_afe_contract(self.settings),
                    "afe_command": SettingsController.runtime_afe_command(self.settings),
                    "measurement_expected": (
                        SettingsController.runtime_measurement_contract(self.settings)
                    ),
                    "measurement_command": (
                        SettingsController.runtime_measurement_command(
                            self.settings, gate_request_id
                        )
                    ),
                    "measurement_confirmed": False,
                    "measurement_actual": {},
                    "measurement_sent": False,
                    "afe_command_sent": False,
                    "afe_confirmed": False,
                    "link_ready": False,
                    "link_probe_epoch": None,
                    "require_post_set_epoch": False,
                    "exact_response_seen": False,
                    "phase": "probing_link",
                    "actual": {},
                    "mismatches": [],
                    "started_at": time.time(),
                    "verified_at": None,
                    "verification_level": None,
                    "request_id": gate_request_id,
                    "last_tagged_get_at": 0.0,
                    "tagged_get_attempts": 0,
                    "legacy_fallback_sent": False,
                }
                if verify_runtime_config else {"state": "idle"}
            )
            self._reset_data_cache()
            self.state = "running"
            if self.settings.get("adaptive_stop") and method == "it":
                self._stability_eta.update({
                    "status": "estimating",
                    "display_text": "正在估计",
                    "reason": "insufficient_data",
                })
            transport_label = (
                "V5.1 USB DATA" if HARDWARE_TRANSPORT == "serial" else "RTT"
            )
            self.message = (
                f"已启动硬件 {method.upper()} 测量，等待 {transport_label} 数据"
            )
            self.on_complete = on_complete

            env = os.environ.copy()
            # Frozen Windows child processes otherwise buffer stderr when it
            # is redirected to collector.log, hiding the only actionable RTT
            # backend details until after a forced stop.
            env["PYTHONUNBUFFERED"] = "1"
            if not runtime.is_frozen():
                host_dir = str(PROJECT_DIR / "software" / "host")
                env["PYTHONPATH"] = host_dir + os.pathsep + env.get("PYTHONPATH", "")
            command = runtime.module_command(
                "pa_host.it_tool",
                "measure",
                # 方案 C:命令文件。外部另开 telnet 连接写下行**无效**
                # (JLinkExe 只转发采集器持有的那个连接)⇒ 必须走这个文件。
                "--cell-v",
                str(self.cell_v_path),
                "--audit",
                str(self.audit_path),
                "--cmd-file",
                str(self.cmd_path),
                "--out",
                str(self.raw_path),
                "--raw-log",
                str(self.raw_log),
                "--trigger",
                trigger,
                "--duration",
                str(
                    0
                    if self.settings["method"] == "it" and self.settings["adaptive_stop"]
                    else self.settings["prestep_s"] + self.settings["duration_s"] + 5
                ),
                "--idle-timeout",
                "25",
            )
            if HARDWARE_TRANSPORT == "serial":
                if not SERIAL_DATA_PORT:
                    self.state = "error"
                    self.error = "V5.1 需要明确指定 DATA CDC 路径"
                    raise RuntimeError(self.error)
                command += ["--serial", SERIAL_DATA_PORT]
            else:
                # collector 持有唯一 RTT 桥并负责完整回收。
                command += [
                    "--start-jlink", "--elf", str(_firmware_artifact("zephyr.elf")),
                ]
                if JLINK_SERIAL:
                    command += ["--probe-serial", JLINK_SERIAL]
            if method == "cv":
                command.append("--cv")
            log_handle = (self.run_dir / "collector.log").open(
                "w", buffering=1, encoding="utf-8", errors="replace"
            )
            try:
                self.process = subprocess.Popen(
                    command,
                    cwd=PROJECT_DIR,
                    env=env,
                    stdout=log_handle,
                    stderr=subprocess.STDOUT,
                    text=True, encoding="utf-8", errors="replace",
                    # 🔴 自成进程组:进程树是
                    #      gui_server → it_tool → pa_host.collect → JLinkExe
                    #    只 terminate 第一层(it_tool)的话,孙进程 collect 与曾孙
                    #    JLinkExe 都活下来,**并且不会因 idle-timeout 自愈**
                    #    (2026-08-09 实测:停止 60s 后两者仍在跑、19021 仍被占,
                    #    下一次烧录/测量就会撞上探头被占)。有了进程组才能整棵收掉。
                    # Windows: CREATE_NEW_PROCESS_GROUP 代替 start_new_session。
                    **(runtime.hidden_subprocess_kwargs(new_process_group=True)
                       if _IS_WIN else dict(start_new_session=True)),
                )
            except Exception as exc:
                log_handle.close()
                self._stop_bridge()
                self.state = "error"
                self.error = "无法启动采集进程"
                diagnostic_id = DIAGNOSTICS.exception(
                    "measurement.collector_start_failed",
                    "Collector process could not be started",
                    exc,
                    run_id=self.run_id,
                    run_dir=self.run_dir,
                    transport=HARDWARE_TRANSPORT,
                )
                try:
                    setattr(exc, "diagnostic_id", diagnostic_id)
                except (AttributeError, TypeError):
                    pass
                raise
            DIAGNOSTICS.record(
                "info", "measurement.collector_started",
                "Collector process started",
                run_id=self.run_id,
                process_id=self.process.pid if self.process is not None else None,
                transport=HARDWARE_TRANSPORT,
                config_gate=self._config_gate,
            )
            if verify_runtime_config:
                # ARMED keeps the firmware idle. The full AFE and measurement
                # snapshots are committed before a tagged physical GET; START
                # is emitted only after both confirmations match the UI.
                if self._send_runtime_gate_commands_locked():
                    self.message = "正在下发并核对硬件测量条件"
            # This thread owns analysis, exports, and completion callbacks after
            # the collector exits. It must keep the process alive until those
            # durable writes finish.
            self.thread = threading.Thread(
                target=self._watch,
                args=(log_handle,),
                name=f"measurement-{self.run_id}",
                daemon=False,
            )
            try:
                self.thread.start()
            except Exception as exc:
                log_handle.close()
                if self.process is not None and self.process.poll() is None:
                    self._terminate_tree(self.process)
                self._stop_bridge()
                self.state = "error"
                self.error = "无法启动采集收尾线程"
                diagnostic_id = DIAGNOSTICS.exception(
                    "measurement.watcher_start_failed",
                    "Measurement completion watcher could not be started",
                    exc,
                    run_id=self.run_id,
                    run_dir=self.run_dir,
                    transport=HARDWARE_TRANSPORT,
                )
                try:
                    setattr(exc, "diagnostic_id", diagnostic_id)
                except (AttributeError, TypeError):
                    pass
                raise
            return self.snapshot()

    def start_verified(self, metadata: dict[str, Any] | None = None,
                       on_complete: Any = None,
                       settings: dict[str, Any] | None = None,
                       filter_config: dict[str, Any] | None = None,
                       plateau_config: PlateauConfig | dict[str, Any] | None = None,
                       timeout_s: float = 30.0) -> dict[str, Any]:
        """Start a formal run only after the MCU confirms the full AFE state."""
        self.start(
            metadata=metadata,
            on_complete=on_complete,
            settings=settings,
            trigger="ARMED",
            filter_config=filter_config,
            plateau_config=plateau_config,
            verify_runtime_config=True,
        )
        if not self._config_gate_event.wait(timeout_s):
            self._fail_config_gate("timeout", "硬件配置回读超时，测量未启动")
        with self.lock:
            gate = dict(self._config_gate)
        if gate.get("state") != "matched":
            if gate.get("state") != "mismatch":
                error = RuntimeError(
                    str(gate.get("message") or "硬件配置回读失败，测量未启动")
                )
            else:
                details = "、".join(
                    str(item.get("field")) for item in gate.get("mismatches", [])
                )
                suffix = f"：{details}" if details else ""
                error = RuntimeError(
                    f"硬件当前配置与实时页条件不一致{suffix}。"
                    "请重新点击“应用条件并烧录硬件”后再测量"
                )
            diagnostic_id = str(gate.get("diagnostic_id") or "")
            if diagnostic_id:
                setattr(error, "diagnostic_id", diagnostic_id)
            raise error
        return self.snapshot()

    def _stop_bridge(self) -> None:
        """收掉硬件桥子进程。

        🔴 2026-08-09 起本类不再自己起桥(collector 用 `--start-jlink` 自己持有
        JLinkExe,见 start() 里的注释),`bridge_process` 恒为 None ⇒ 本方法实际是
        no-op。保留是因为 start()/stop()/_watch() 三处的清理路径都调它,留着比
        删掉三处调用更不容易出错;若将来又需要外部桥,这里是唯一的挂载点。
        """
        process = self.bridge_process
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
        if self.bridge_log_handle is not None:
            self.bridge_log_handle.close()
        self.bridge_process = None
        self.bridge_log_handle = None

    def stop(self) -> dict[str, Any]:
        with self.lock:
            self._request_stop_locked(automatic=False)
            return self.snapshot()

    def _request_stop_locked(self, *, automatic: bool) -> bool:
        process = self.process
        if self.state != "running" or process is None:
            return False
        if self.user_stop_requested or self.auto_stop_requested:
            return False
        if self._config_gate.get("state") == "checking":
            self._fail_config_gate("aborted", "配置核对期间测量已取消")
            return True
        if automatic:
            self.auto_stop_requested = True
            self._auto_stop_evidence = _json_safe({
                "requested_at": time.time(),
                "consecutive_passes": self._plateau_consecutive_passes,
                "required_consecutive_windows": (
                    self.plateau_config.required_consecutive_windows
                ),
                "evaluation": self._plateau_evaluation,
                "context_start_s": self._plateau_context_start_s,
                "minimum_gate_until_s": self._plateau_minimum_gate_until_s,
                "rolling_stage_key": self._rolling_metrics.get("stage_key"),
                "rolling_stage_end_s": self._rolling_metrics.get("stage_end_s"),
                "rolling_metrics": self._rolling_metrics,
                "stability_eta": self._stability_eta,
                "filter_config": self.filter_config,
                "plateau_config": self.plateau_config.to_dict(),
            })
            self.message = "已检测到稳定平台，正在结束测量"
        else:
            self._stop_requested_rolling_metrics = (
                dict(self._rolling_metrics)
                if self._rolling_metric_is_complete(self._rolling_metrics)
                else self._last_complete_with_bookkeeping_locked(
                    self._rolling_metrics
                )
            )
            self.user_stop_requested = True
            self.message = "正在停止硬件测量"
        # 顶部拒绝提示只代表“最近一次命令”；停止后它已不再可行动。
        # 原始 CFG_REJECT 仍完整保留在 audit.jsonl 和界面审计列表中。
        self._last_reject = {}
        try:
            if self.cmd_path is None:
                raise OSError("采集命令文件尚未就绪")
            # collector 持有唯一有效的 RTT 下行连接。另开 telnet 连接写 STOP
            # 不会到达固件,只会让 host 杀掉进程而 MCU 继续处于 acquiring。
            with self.cmd_path.open("a", encoding="utf-8") as fh:
                fh.write("STOP\n")
        except OSError:
            self._terminate_tree(process)
        else:
            threading.Thread(
                target=self._terminate_if_running,
                args=(process, 1.5), daemon=True,
            ).start()
        return True

    def send_range(self, payload: dict[str, Any]) -> dict[str, Any]:
        """方案 C:测量进行中在线切换 FSR / offset 档,**不复位、不中断极化**。

        为什么必须经采集器的命令文件:JLinkExe 的 RTT telnet 只把采集器持有的
        那个连接的输入送进下行通道,另开连接写命令目标端收不到(2026-08-09 实测)。
        """
        with self.lock:
            if (self.state != "running" or self.cmd_path is None
                    or self.user_stop_requested or self.auto_stop_requested):
                raise RuntimeError("只有测量进行中才能在线切档")
            if self._config_gate.get("state") == "checking":
                raise RuntimeError("硬件配置核对期间不能修改运行时参数")
            fsr = int(payload["fsr_code"])
            sel = int(payload["offset_sel"])
            if not (0 <= fsr <= 5) or not (0 <= sel <= 7):
                raise ValueError("fsr_code 需 0–5(50n/100n/250n/500n/1µ/2µ),"
                                 "offset_sel 需 0–7(0/10%/20%/50%FS,9/19/40/80nA)")
            line = f"RANGE {fsr} {sel}"
            with self.cmd_path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
            self.range_runtime = {**self.range_runtime, "pending": line,
                                  "rejected": None, "at": time.time()}
            return _json_safe({"sent": line, "cmd_file": str(self.cmd_path),
                               "note": "固件会回 RANGE_APPLIED / RANGE_REJECT,见 rtt.log"})

    # ------------------------------------------------------------------
    # 硬件 DEBUG 模式
    # ------------------------------------------------------------------
    @staticmethod
    def _validate_command_line(line: str) -> str:
        line = line.strip()
        if not line:
            raise ValueError("命令为空")
        if "\n" in line or "\r" in line:
            raise ValueError("一行一条命令,不许含换行")
        try:
            line.encode("ascii")
        except UnicodeEncodeError as exc:
            raise ValueError("硬件命令只能包含 ASCII 字符") from exc
        if len(line) >= 128:
            raise ValueError(f"命令过长({len(line)} ≥ 128 字符)")
        return line

    def send_command(self, line: str) -> dict[str, Any]:
        """下发任意一行命令。send_range() 是它的一个特例。

        🔴 同样必须经采集器的命令文件 —— JLinkExe 的 RTT telnet 只把**采集器持有
        的那个连接**的输入送进下行通道,另开 telnet 写命令目标端收不到
        (2026-08-09 实测)。所以"没有测量在跑"时无处可发,只能拒绝。
        """
        # 与固件 AFE_CFG_LINE_MAX 同口径:超长在固件侧只会被拒,不如在这里挡
        line = self._validate_command_line(line)
        with self.lock:
            if (self.state != "running" or self.cmd_path is None
                    or self.user_stop_requested or self.auto_stop_requested):
                raise RuntimeError("命令只能在测量进行中下发(RTT 下行通道由采集器持有)")
            verb = line.split(None, 1)[0]
            if (self._config_gate.get("state") == "checking"
                    and verb not in {"GET", "STATUS", "PEEK"}):
                raise RuntimeError("硬件配置核对期间不能修改运行时参数")
            with self.cmd_path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
            if line.startswith(("RANGE ", "SET ")):
                self.range_runtime = {**self.range_runtime, "pending": line,
                                      "rejected": None, "at": time.time()}
            return _json_safe({"sent": line, "cmd_file": str(self.cmd_path)})

    def require_debug_run(self) -> None:
        """Reject Debug mutations unless the active acquisition is a Debug run."""
        with self.lock:
            if self.state != "running" or not self.metadata.get("debug"):
                raise RuntimeError(
                    "当前运行不是硬件 DEBUG 测量，拒绝 DEBUG 控制命令"
                )

    def begin_debug_measurement(self, line: str) -> dict[str, Any]:
        """在 ARMED Debug 连接上应用配置；确认成功后才由 snapshot 发 START。"""
        line = self._validate_command_line(line)
        if not line.startswith("SET ") or "FORCE" in line.split():
            raise ValueError("Debug 待机启动只允许不带 FORCE 的 SET 配置命令")
        desired: dict[str, str] = {}
        for token in line.split()[1:]:
            key, sep, value = token.partition("=")
            if not sep or not key or not value:
                raise ValueError(f"无法解析 Debug 配置项：{token}")
            desired[key] = value
        with self.lock:
            if (self.state != "running" or self.cmd_path is None
                    or self.user_stop_requested or not self.debug_waiting_for_start):
                raise RuntimeError("Debug 设备不在等待启动状态，请重新读取设备配置")
            if not self._cfg_confirmed_this_session:
                raise RuntimeError("Debug 尚未收到本次连接的配置确认，请等待设备状态读回")
            current_ep = int(self._cfg_live.get("ep") or 0)
            self._last_reject = {}
            # 全部字段已经等于设备确认值时，固件只会回 CFG_NOOP（不会新建 epoch）。
            # 此时现有 confirmed_ep 已足够证明配置，直接 START 即可。
            if (int(self._cfg_live.get("confirmed_ep") or -1) == current_ep
                    and self._debug_cfg_matches(desired, self._cfg_live)):
                with self.cmd_path.open("a", encoding="utf-8") as fh:
                    fh.write("START\n")
                self.debug_waiting_for_start = False
                self.message = "设备配置已一致，正在启动硬件测量"
                return _json_safe({"sent": ["START"], "already_applied": True,
                                   "cmd_file": str(self.cmd_path)})
            self._debug_pending_cfg = {
                "desired": desired,
                "min_epoch": current_ep + 1,
                "line": line,
            }
            # 这里只发 SET。必须等审计出现匹配的 CFG_CONFIRMED 后才能发 START；
            # 否则整组 SET 被拒时仍会拿旧配置开跑，看起来像“被默认值覆盖”。
            with self.cmd_path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
            self.message = "配置已下发，等待固件确认"
            return _json_safe({"sent": [line], "waiting_for_confirmation": True,
                               "cmd_file": str(self.cmd_path)})

    @staticmethod
    def _debug_cfg_matches(desired: dict[str, str], cfg: dict[str, Any]) -> bool:
        fields = {
            "fsr": "fsr", "off": "off", "period": "period", "e": "e_mv",
            "vwe": "vwe_mv", "idle": "idle", "sysper": "sysper",
            "cellv": "cellv", "ioc": "ioc",
        }
        for key, cfg_key in fields.items():
            if key in desired and str(cfg.get(cfg_key)) != desired[key]:
                return False
        if "conv" in desired:
            if desired["conv"] == "auto":
                if cfg.get("conv_src") != "auto":
                    return False
            elif str(cfg.get("conv")) != desired["conv"]:
                return False
        return True

    def _maybe_start_confirmed_debug(self) -> None:
        pending = self._debug_pending_cfg
        if not pending or self._last_reject:
            return
        cfg = self._cfg_live
        try:
            confirmed = int(cfg.get("confirmed_ep") or -1)
            epoch = int(cfg.get("ep") or -1)
            min_epoch = int(pending["min_epoch"])
        except (TypeError, ValueError):
            return
        if (epoch < min_epoch or confirmed != epoch
                or not self._debug_cfg_matches(pending["desired"], cfg)):
            return
        if self.cmd_path is None:
            return
        with self.cmd_path.open("a", encoding="utf-8") as fh:
            fh.write("START\n")
        self._debug_pending_cfg = None
        self.debug_waiting_for_start = False
        self.message = "配置已确认，正在启动硬件测量"

    def _fail_config_gate(self, state: str, message: str,
                          mismatches: list[dict[str, Any]] | None = None) -> None:
        """Abort an ARMED formal run without ever queuing START."""
        process: subprocess.Popen[str] | None = None
        with self.lock:
            if self._config_gate.get("state") != "checking":
                self._config_gate_event.set()
                return
            self._config_gate.update({
                "state": state,
                "mismatches": list(mismatches or self._config_gate.get("mismatches", [])),
                "verified_at": time.time(),
                "message": message,
            })
            self._prestart_gate_failed = True
            self.debug_waiting_for_start = False
            self.user_stop_requested = True
            self.message = message
            self.error = message
            process = self.process
            diagnostic_id = DIAGNOSTICS.record(
                "warning" if state == "aborted" else "error",
                "measurement.config_gate_failed",
                "Hardware configuration verification did not complete",
                run_id=self.run_id,
                run_dir=self.run_dir,
                gate_state=state,
                status_message=message,
                mismatches=self._config_gate.get("mismatches", []),
                request_id=self._config_gate.get("request_id"),
                transport=HARDWARE_TRANSPORT,
                device=_selected_device_copy(),
            )
            self._config_gate["diagnostic_id"] = diagnostic_id
            self._config_gate_event.set()
        if process is not None and process.poll() is None:
            threading.Thread(
                target=self._terminate_if_running,
                args=(process, 0.0), daemon=True,
            ).start()

    def _write_config_gate_command_locked(self, line: str, failure: str) -> bool:
        """Append one gate command or atomically fail and tear down the ARMED run."""
        if self._config_gate.get("state") != "checking":
            return False
        try:
            if self.cmd_path is None:
                raise OSError("采集命令文件尚未就绪")
            with self.cmd_path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError as exc:
            self._fail_config_gate("io_error", f"{failure}：{exc}")
            return False
        return True

    def _send_tagged_gate_get_locked(self) -> bool:
        """Send one replay request and remember it for bounded RTT-start retries."""
        request_id = self._config_gate.get("request_id")
        if not request_id:
            self._fail_config_gate("internal_error", "硬件配置回读请求缺少标识")
            return False
        if not self._write_config_gate_command_locked(
            f"GET req={request_id}", "无法下发硬件配置回读命令",
        ):
            return False
        self._config_gate["last_tagged_get_at"] = time.time()
        self._config_gate["tagged_get_attempts"] = (
            int(self._config_gate.get("tagged_get_attempts") or 0) + 1
        )
        return True

    def _send_runtime_gate_commands_locked(self) -> bool:
        """Prove the downlink first; only then apply the AFE exactly once."""
        if self._config_gate.get("link_ready") is False:
            self._config_gate["phase"] = "probing_link"
            return self._send_tagged_gate_get_locked()
        if not self._config_gate.get("afe_command_sent"):
            if not self._apply_afe_after_link_probe_locked():
                return False
        return self._send_tagged_gate_get_locked()

    def _apply_afe_after_link_probe_locked(self) -> bool:
        """Commit SET after one tagged GET has proved RTT/CDC downlink readiness."""
        if self._config_gate.get("afe_command_sent"):
            return True
        command = self._config_gate.get("afe_command")
        if not command:
            self._fail_config_gate("internal_error", "测量门禁缺少 AFE 配置命令")
            return False
        if not self._write_config_gate_command_locked(
            str(command), "无法下发 AFE 配置",
        ):
            return False
        request_id = str(self._config_gate.get("request_id") or "")
        self._cfg_epochs = {
            key: record for key, record in self._cfg_epochs.items()
            if str(key[1] or "") != request_id
        }
        now = time.time()
        self._config_gate.update({
            "link_ready": True,
            "afe_command_sent": True,
            "last_tagged_get_at": now,
            "phase": "applying_afe",
            "actual": {},
            "mismatches": [],
        })
        self.message = "硬件通道已就绪，正在应用 AFE 配置"
        return True

    def _maybe_retry_tagged_gate_get_locked(self) -> None:
        """Retry the exact GET while J-Link may accept TCP before RTT downlink is ready."""
        if (self._config_gate.get("state") != "checking"
                or self._config_gate.get("afe_confirmed")
                or self._config_gate.get("legacy_fallback_sent")
                or self.cmd_path is None or self.user_stop_requested):
            return
        now = time.time()
        started_at = float(self._config_gate.get("started_at") or now)
        last_sent = float(self._config_gate.get("last_tagged_get_at") or 0.0)
        if ((not self._config_gate.get("exact_response_seen")
             and now - started_at >= CONFIG_GATE_LEGACY_PROBE_DELAY_S)
                or now - last_sent < CONFIG_GATE_GET_RETRY_S):
            return
        self._send_tagged_gate_get_locked()

    def _send_measurement_gate_command_locked(self) -> bool:
        """Send MEAS only after a clean, matching physical AFE snapshot."""
        if self._config_gate.get("measurement_sent"):
            return True
        command = self._config_gate.get("measurement_command")
        if not command:
            self._fail_config_gate(
                "internal_error", "测量门禁缺少测量时序命令",
            )
            return False
        if not self._write_config_gate_command_locked(
            str(command), "硬件 AFE 配置已匹配，但无法下发测量时序",
        ):
            return False
        self._config_gate.update({
            "measurement_sent": True,
            "measurement_sent_at": time.time(),
            "phase": "checking_measurement",
        })
        self.message = "AFE 配置已确认，正在核对测量时序"
        return True

    @staticmethod
    def _config_mismatches(expected: dict[str, Any],
                           actual: dict[str, Any]) -> list[dict[str, Any]]:
        mismatches: list[dict[str, Any]] = []
        for field, wanted in expected.items():
            got = actual.get(field)
            if got != wanted:
                mismatches.append({
                    "field": field,
                    "expected": wanted,
                    "actual": got,
                })
        for field in ("invalid_cfg", "vdd_oor"):
            if int(actual.get(field) or 0) != 0:
                mismatches.append({
                    "field": field, "expected": 0, "actual": actual.get(field),
                })
        if actual.get("verify_ok") != 1:
            mismatches.append({
                "field": "verify_ok", "expected": 1,
                "actual": actual.get("verify_ok"),
            })
        return mismatches

    @staticmethod
    def _measurement_mismatches(expected: dict[str, Any],
                                actual: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            {
                "field": f"measurement.{field}",
                "expected": wanted,
                "actual": actual.get(field),
            }
            for field, wanted in expected.items()
            if actual.get(field) != wanted
        ]

    def _advance_config_gate_locked(self) -> None:
        if self._config_gate.get("state") != "checking":
            return
        exact_candidates: list[tuple[int, dict[str, Any]]] = []
        legacy_candidates: list[tuple[int, dict[str, Any]]] = []
        wanted_req = self._config_gate.get("request_id")
        for (epoch, request_id), record in self._cfg_epochs.items():
            if not {"CFG_APPLIED", "CFG_DERIVED", "CFG_CONFIRMED"}.issubset(
                record["seen"]
            ):
                continue
            if record.get("applied_src") != "get":
                continue
            # New firmware identifies the confirming replay as src=get. Older
            # builds also do this; keeping it mandatory rejects boot fragments.
            if record.get("confirmed_src") not in (None, "get"):
                continue
            if (
                self._config_gate.get("afe_command_sent")
                and self._config_gate.get("require_post_set_epoch")
                and epoch == self._config_gate.get("link_probe_epoch")
            ):
                # More than one tagged GET may already be in flight when the
                # first reply proves the downlink. Those replies describe the
                # pre-SET epoch and can arrive after SET was queued. They must
                # not be judged as the requested post-SET configuration.
                continue
            if request_id == wanted_req:
                exact_candidates.append((epoch, record))
            elif (self._config_gate.get("legacy_fallback_sent")
                  and request_id in (None, "", "-")):
                legacy_candidates.append((epoch, record))
        # A tagged response is unambiguous and must always beat a legacy replay,
        # even if the latter carries a numerically newer epoch.
        candidates = exact_candidates or legacy_candidates
        if not candidates:
            return
        epoch, record = max(candidates, key=lambda item: item[0])
        actual = dict(record["data"])
        actual["ep"] = epoch
        exact_physical_response = (
            bool(exact_candidates) and record.get("confirmed_has_verify_ok") is True
        )
        if exact_candidates:
            self._config_gate["exact_response_seen"] = True
        self._config_gate.update({
            "actual": actual,
            "ep": epoch,
            "mismatches": [],
            "verification_level": (
                "physical_registers" if exact_physical_response
                else "reported_config"
            ),
        })
        if not exact_physical_response:
            self._fail_config_gate(
                "unsupported_firmware",
                "固件不支持完整物理配置核验，请重新应用条件并烧录硬件",
            )
            return
        if self._config_gate.get("link_ready") is False:
            probe_mismatches: list[dict[str, Any]] = []
            if actual.get("verify_ok") != 1:
                probe_mismatches.append({
                    "field": "verify_ok", "expected": 1,
                    "actual": actual.get("verify_ok"),
                })
            if record["faults"]:
                probe_mismatches.append({
                    "field": "config_integrity", "expected": "confirmed",
                    "actual": record["faults"][-1].get("kind"),
                })
            if probe_mismatches:
                self._fail_config_gate(
                    "mismatch", "AFE 物理寄存器校验失败，未启动测量",
                    probe_mismatches,
                )
                return
            expected = self._config_gate.get("expected") or {}
            self._config_gate.update({
                "link_probe_epoch": epoch,
                "require_post_set_epoch": any(
                    actual.get(field) != wanted
                    for field, wanted in expected.items()
                ),
            })
            self._apply_afe_after_link_probe_locked()
            return
        mismatches = self._config_mismatches(
            self._config_gate["expected"], actual,
        )
        if record["faults"]:
            mismatches.append({
                "field": "config_integrity",
                "expected": "confirmed",
                "actual": record["faults"][-1].get("kind"),
            })
        transient_fields = {"invalid_cfg", "vdd_oor"}
        hard_mismatches = [
            item for item in mismatches if item["field"] not in transient_fields
        ]
        transient_mismatches = [
            item for item in mismatches if item["field"] in transient_fields
        ]
        self._config_gate["mismatches"] = mismatches
        if hard_mismatches:
            fields = "、".join(str(item["field"]) for item in hard_mismatches)
            self._fail_config_gate(
                "mismatch", f"硬件配置不一致（{fields}），未启动测量",
                mismatches,
            )
            return
        if transient_mismatches:
            self._config_gate["phase"] = "waiting_for_clean_afe"
            self.message = "AFE 状态正在稳定，正在自动复核"
            return
        self._config_gate["afe_confirmed"] = True
        self._config_gate["phase"] = "afe_matched"
        measurement_expected = self._config_gate.get("measurement_expected") or {}
        if measurement_expected and not self._config_gate.get("measurement_sent"):
            self._send_measurement_gate_command_locked()
            return
        if (measurement_expected
                and not self._config_gate.get("measurement_confirmed")):
            return
        if measurement_expected:
            measurement_mismatches = self._measurement_mismatches(
                measurement_expected,
                self._config_gate.get("measurement_actual") or {},
            )
            self._config_gate["mismatches"] = measurement_mismatches
            if measurement_mismatches:
                fields = "、".join(
                    str(item["field"]) for item in measurement_mismatches
                )
                self._fail_config_gate(
                    "mismatch", f"测量时序不一致（{fields}），未启动测量",
                    measurement_mismatches,
                )
                return
        if (self.state != "running" or self.cmd_path is None
                or self.user_stop_requested):
            self._fail_config_gate("aborted", "配置核对期间测量已取消")
            return
        if not self._write_config_gate_command_locked(
            "START", "硬件配置已匹配，但无法下发启动命令",
        ):
            return
        self.debug_waiting_for_start = False
        self._config_gate.update({
            "state": "matched",
            "verified_at": time.time(),
            "message": "硬件配置已完整确认",
        })
        self.metadata["hardware_config"] = {
            "expected": dict(self._config_gate["expected"]),
            "actual": actual,
            "measurement_expected": dict(measurement_expected),
            "measurement_actual": dict(
                self._config_gate.get("measurement_actual") or {}
            ),
            "epoch": epoch,
            "verification_level": self._config_gate["verification_level"],
            "verified_at": self._config_gate["verified_at"],
        }
        self.message = "硬件配置已确认，正在启动测量"
        self._config_gate_event.set()

    def _maybe_send_legacy_gate_get_locked(self) -> None:
        """Fall back to bare GET for already-flashed firmware without req support."""
        if (self._config_gate.get("state") != "checking"
                or self._config_gate.get("afe_confirmed")
                or self._config_gate.get("exact_response_seen")
                or self._config_gate.get("legacy_fallback_sent")
                or time.time() - float(self._config_gate.get("started_at") or 0)
                < CONFIG_GATE_LEGACY_PROBE_DELAY_S
                or self.cmd_path is None or self.user_stop_requested):
            return
        if not self._write_config_gate_command_locked(
            "GET", "无法下发旧版固件兼容回读命令",
        ):
            return
        # Anything already parsed predates this GET and cannot be its response.
        # Keep tagged records so a late exact response can still win.
        self._cfg_epochs = {
            key: record for key, record in self._cfg_epochs.items()
            if key[1] not in (None, "", "-")
        }
        self._config_gate["legacy_fallback_sent"] = True

    def _audit_events(self, limit: int = 60) -> list[dict[str, Any]]:
        """读 audit.jsonl 的尾部。增量读:只从上次位置往后追加,不全量重读。"""
        with self.lock:
            path = self.audit_path
            if path is None or not path.exists():
                return []
            try:
                with path.open("r", encoding="utf-8", errors="replace") as fh:
                    fh.seek(self._audit_pos)
                    fresh = fh.read()
                    self._audit_pos = fh.tell()
            except OSError:
                return self._audit_cache[-limit:]
            lines, self._audit_pending = _split_complete_lines(
                fresh, self._audit_pending
            )
            for raw in lines:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                self._audit_cache.append(event)
                kind = event.get("kind")
                epoch = event.get("ep")
                if (kind == "MEAS_CONFIRMED"
                    and self._config_gate.get("state") == "checking"
                    and self._config_gate.get("measurement_sent")
                    and (
                    str(event.get("req") or "")
                    == str(self._config_gate.get("request_id") or "")
                    )):
                    expected_measurement = (
                        self._config_gate.get("measurement_expected") or {}
                    )
                    self._config_gate["measurement_actual"] = {
                        key: event.get(key) for key in expected_measurement
                    }
                    self._config_gate["measurement_confirmed"] = True
                elif (kind == "MEAS_REJECT"
                      and self._config_gate.get("state") == "checking"
                      and self._config_gate.get("measurement_sent")
                      and str(event.get("req") or "")
                      == str(self._config_gate.get("request_id") or "")):
                    reason = str(event.get("reason") or "unknown")
                    self._fail_config_gate(
                        "measurement_rejected",
                        f"固件拒绝测量条件（{reason}），测量未启动",
                    )
                if (isinstance(epoch, int)
                        and kind in ("CFG_APPLIED", "CFG_DERIVED", "CFG_BOOT",
                                     "CFG_CONFIRMED")):
                    if epoch != self._cfg_live_epoch:
                        self._cfg_live = {}
                        self._cfg_live_epoch = epoch
                        self._cfg_confirmed_this_session = False
                    if (
                        epoch != self._plateau_cfg_epoch
                        and (
                            self._plateau_cfg_epoch is not None
                            or self._plateau_context_epoch is not None
                        )
                    ):
                        self._reset_plateau_monitor_locked(
                            hardware_context_changed=True,
                            expected_epoch=epoch,
                        )
                    self._plateau_cfg_epoch = epoch
                if isinstance(epoch, int) and str(kind).startswith("CFG_"):
                    request_id = event.get("req")
                    request_key = (
                        str(request_id) if request_id not in (None, "") else None
                    )
                    record = self._cfg_epochs.setdefault((epoch, request_key), {
                        "data": {}, "seen": set(), "faults": [],
                        "applied_src": None, "confirmed_src": None,
                        "confirmed_has_verify_ok": False,
                    })
                    record["seen"].add(kind)
                    if kind in ("CFG_APPLIED", "CFG_DERIVED", "CFG_CONFIRMED"):
                        record["data"].update({
                            k: v for k, v in event.items()
                            if k not in ("raw", "kind", "host_unix_s")
                        })
                    if kind == "CFG_APPLIED":
                        record["applied_src"] = event.get("src")
                    elif kind == "CFG_CONFIRMED":
                        record["confirmed_src"] = event.get("src")
                        record["confirmed_has_verify_ok"] = "verify_ok" in event
                    elif kind in ("CFG_FAULT", "CFG_ROLLBACK"):
                        record["faults"].append(dict(event))
                if kind in ("CFG_APPLIED", "CFG_DERIVED", "CFG_BOOT"):
                    self._cfg_live.update({k: v for k, v in event.items()
                                           if k not in ("raw", "kind")})
                elif kind == "CFG_CONFIRMED":
                    self._cfg_live.update({k: v for k, v in event.items()
                                           if k not in ("raw", "kind")})
                    self._cfg_live["confirmed_ep"] = event.get("ep")
                    self._cfg_confirmed_this_session = True
                elif kind == "AFE_STATUS":
                    self._afe_status = {k: v for k, v in event.items() if k != "kind"}
                elif kind == "IT_PHASE":
                    previous_phase = self._phase.get("phase")
                    self._phase = {k: v for k, v in event.items() if k != "kind"}
                    if (
                        self._phase.get("phase") == "acquire"
                        and previous_phase != "acquire"
                    ):
                        self._reset_plateau_monitor_locked(clock_reset=True)
                elif kind == "IT_TAINTED":
                    self._hardware_taint = {
                        k: v for k, v in event.items() if k != "kind"
                    }
                    self._hardware_taint["kind"] = kind
                elif kind in ("CFG_REJECT", "CFG_FAULT", "CFG_ROLLBACK", "OCP_REJECT",
                              "RANGE_REJECT"):
                    self._last_reject = {
                        k: v for k, v in event.items() if k != "kind"
                    }
                    self._last_reject["kind"] = kind
            self._advance_config_gate_locked()
            # 只保留尾部,长跑不无限膨胀
            if len(self._audit_cache) > 400:
                del self._audit_cache[:-400]
            return self._audit_cache[-limit:]

    def _read_kv_csv(self, path: Path | None, pos_attr: str, cache_attr: str,
                     wanted: tuple[str, ...]) -> list[dict[str, Any]]:
        """增量读一个带表头的 CSV,只留 `wanted` 里的列。

        为什么要增量:DEBUG 页 1Hz 刷新,一轮 180s 就是上千行;每次全量重读会
        随时间线性变慢,长跑时界面明显卡顿。
        """
        cache: list[dict[str, Any]] = getattr(self, cache_attr)
        if path is None or not path.exists():
            return cache
        pos = getattr(self, pos_attr)
        try:
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                fh.seek(pos)
                fresh = fh.read()
                setattr(self, pos_attr, fh.tell())
        except OSError:
            return cache
        header = getattr(self, cache_attr + "_hdr", None)
        pending_attr = cache_attr + "_pending"
        lines, pending = _split_complete_lines(fresh, getattr(self, pending_attr))
        setattr(self, pending_attr, pending)
        for raw in lines:
            raw = raw.strip()
            if not raw or raw.startswith("#"):
                continue
            parts = raw.split(",")
            if header is None:
                header = parts
                setattr(self, cache_attr + "_hdr", header)
                continue
            if len(parts) != len(header):
                continue
            row: dict[str, Any] = {}
            for key, value in zip(header, parts):
                if key not in wanted:
                    continue
                try:
                    row[key] = float(value) if "." in value else int(value)
                except ValueError:
                    row[key] = value
            # 🔴 固件复位边界:dev_ms 回退 ⇒ 之前那些行属于上一次开机,丢掉。
            #    不丢的话双轴图会把两次开机首尾相接:t0 取 min(dev_ms) 会落到
            #    上一次开机的时刻,整条曲线的时间轴全错,而且看起来完全正常。
            #    (2026-08-10 实测:RTT 上行缓冲在目标复位后不清空,残留几十行。)
            if (cache and "dev_ms" in row and "dev_ms" in cache[-1]
                    and row["dev_ms"] < cache[-1]["dev_ms"]):
                cache.clear()
            cache.append(row)
        return cache

    def _debug_series(self) -> dict[str, Any]:
        """双轴图的两条流。

        🔴 两条流必须都用**设备时钟 dev_ms** 对齐,不能一条用 load_run_csv 的
        time_s、另一条用 host_unix_s:前者会裁掉静置段(t=0 的定义不同),后者含
        轮询抖动。共用 dev_ms 后,t=0 = 两条流里最早的那个设备时刻,左右轴同轴。
        ⚠️ dev_ms 来自 LFRC(±500ppm),作**相对量**可信,不当绝对时间用。
        """
        cur = self._read_kv_csv(
            self.raw_path, "_dbg_cur_pos", "_dbg_cur",
            ("dev_ms", "fa_fw", "sat", "ovf", "epoch", "counts"),
        )
        cv = self._read_kv_csv(self.cell_v_path, "_dbg_cv_pos", "_dbg_cv",
                               ("dev_ms", "e_mv", "we_mv", "re_mv", "epoch",
                                "ocp", "we_code", "re_code"))
        starts = [r["dev_ms"] for r in (cur[:1] + cv[:1]) if "dev_ms" in r]
        t0 = min(starts) if starts else 0
        return {
            "t0_dev_ms": t0,
            "current": {
                "t": [(r.get("dev_ms", 0) - t0) / 1000.0 for r in cur],
                "nA": [r.get("fa_fw", 0) / 1e6 for r in cur],
                "valid": [
                    int(r.get("sat", 0) or 0) == 0
                    and int(r.get("ovf", 0) or 0) == 0
                    for r in cur
                ],
                "ep": [int(r.get("epoch", 0) or 0) for r in cur],
            },
            "cell_v": {
                "t": [(r.get("dev_ms", 0) - t0) / 1000.0 for r in cv],
                "e_mv": [r.get("e_mv", 0) for r in cv],
                "clipped": [bool(r.get("we_code") in (0, 4095)
                                 or r.get("re_code") in (0, 4095)) for r in cv],
                "ocp": [int(r.get("ocp", 0) or 0) for r in cv],
            },
        }

    def debug_snapshot(self) -> dict[str, Any]:
        events = self._audit_events()
        with self.lock:
            self._maybe_start_confirmed_debug()
        # 🔴 自愈:开机那几行 CFG_BOOT/CFG_APPLIED/CFG_DERIVED 常常收不到 ——
        #   JLinkExe 的 `rtt start` 会把读指针对到当前写指针,**跳过缓冲里已有的
        #   字节**,而那几行在 rtt start 之前(复位后 ~300ms)就写完了。
        #   这正是 GET 幂等重放的用途:它不 ep++、不写任何寄存器,只把设备当前
        #   认知整套重打一遍。检测到"在跑但一条 CFG_* 都没有"就自动补一次。
        # 判据必须用**只有 CFG_DERIVED 才带**的字段(bits)。用 `not self._cfg_live`
        # 不行:CFG_BOOT 只带 ep/ms/fw/reason,一到就让字典非空,GET 反而不发了
        # ——"有几个键"和"有没有派生量"是两件事。
        live_epoch = self._cfg_live.get("ep")
        live_complete = (
            live_epoch is not None
            and self._cfg_live.get("bits") is not None
            and self._cfg_live.get("confirmed_ep") == live_epoch
        )
        if (self.state == "running" and not live_complete
                and time.time() - self._auto_get_at > 3.0):
            self._auto_get_at = time.time()
            try:
                self.send_command("GET")
            except (RuntimeError, ValueError):
                pass
        series = self._debug_series()
        debug_run = bool(self.state == "running" and self.metadata.get("debug"))
        return _json_safe({
            "state": self.state,
            "stop_requested": self.user_stop_requested and self.state == "running",
            "waiting_for_start": self.debug_waiting_for_start,
            "config_pending": self._debug_pending_cfg is not None,
            "config_session_confirmed": self._cfg_confirmed_this_session,
            "message": self.message,
            "error": self.error,
            "run_id": self.run_id,
            "debug_run": debug_run,
            "mutations_allowed": debug_run and not self.user_stop_requested,
            "run_dir": str(self.run_dir) if self.run_dir else "",
            "raw_path": str(self.raw_path) if self.raw_path else "",
            "audit_path": str(self.audit_path) if self.audit_path else "",
            "cell_v_path": str(self.cell_v_path) if self.cell_v_path else "",
            "cfg": self._cfg_live,
            "afe_status": self._afe_status,
            "last_reject": self._last_reject or None,
            "phase": self._phase or None,
            "cell_v": self._cell_voltages(),
            "series": series,
            "adaptive_stop": self._plateau_payload(series),
            # 只送尾部,并且倒序 —— 界面上最新的在最上面
            "audit": list(reversed(events[-40:])),
            "audit_total": len(self._audit_cache),
        })

    def switch_to_measurement_range(self) -> dict[str, Any]:
        """切到测量档(FSR 250nA + offset SEL4=9nA)。

        只在**已过零进入氧化稳态之后**才该调用:小 offset 把还原侧上限压到 9nA,
        此后任何还原方向的摆动都会立刻撞轨失去电位控制。
        """
        return self.send_range({"fsr_code": MEAS_FSR_CODE,
                                "offset_sel": MEAS_OFFSET_SEL})

    def set_auto_switch(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            self.auto_switch_meas = bool(payload.get("enabled"))
            return _json_safe({"enabled": self.auto_switch_meas,
                               "target": {"fsr_code": MEAS_FSR_CODE,
                                          "offset_sel": MEAS_OFFSET_SEL}})

    def _at_measurement_range(self) -> bool:
        applied = (self.range_runtime or {}).get("applied") or {}
        try:
            return (int(applied.get("fsr_pa", -1)) == 250_000
                    and int(applied.get("off_pa", -1)) == 9_000)
        except (TypeError, ValueError):
            return False

    def _maybe_auto_switch(self) -> None:
        """勾了自动切档时,过零并稳定后切一次(且只切一次)。"""
        with self.lock:
            if (not self.auto_switch_meas or self._auto_switch_done
                    or self.state != "running" or self.cmd_path is None):
                return
            if self.range_runtime.get("pending") or self._at_measurement_range():
                return
        try:
            snap = self.snapshot()
        except Exception:                                  # noqa: BLE001
            return
        if not (snap.get("transient") or {}).get("ready"):
            return
        with self.lock:
            if self._auto_switch_done:                     # 双检:snapshot 期间没持锁
                return
            self._auto_switch_done = True
        try:
            self.switch_to_measurement_range()
        except Exception as exc:                           # noqa: BLE001
            with self.lock:
                self._auto_switch_done = False
                self.range_runtime = {**self.range_runtime,
                                      "rejected": f"自动切档失败:{exc}"}

    def _plateau_context_data_locked(
        self, data: dict[str, list[Any]], *, update_state: bool = True,
    ) -> dict[str, list[Any]]:
        """Return samples in the active epoch, optionally advancing gate state."""
        times = data.get("time_s", [])
        currents = data.get("current_nA", [])
        validity = data.get("valid", [])
        epochs = data.get("epoch", [])
        empty = {"time_s": [], "current_nA": [], "valid": [], "epoch": []}
        if not (len(times) == len(currents) == len(validity)):
            return empty

        parsed_epochs: list[int | None] = []
        if len(epochs) == len(times):
            for value in epochs:
                try:
                    parsed_epochs.append(
                        None if value in (None, "") else int(value)
                    )
                except (TypeError, ValueError):
                    parsed_epochs.append(None)
        if not parsed_epochs or not any(value is not None for value in parsed_epochs):
            # Legacy CSVs have no epoch. They remain usable until a live
            # hardware change occurs; after that there is no defensible way to
            # distinguish unread old samples from new-context samples.
            return empty if self._plateau_context_pending else data

        latest_epoch = next(
            value for value in reversed(parsed_epochs) if value is not None
        )
        target_epoch = self._plateau_context_epoch
        context_start_s = self._plateau_context_start_s
        if self._plateau_context_pending:
            expected = self._plateau_expected_epoch
            if expected is not None:
                if latest_epoch != expected:
                    return empty
                target_epoch = expected
            else:
                if target_epoch is not None and latest_epoch == target_epoch:
                    return empty
                target_epoch = latest_epoch
        elif target_epoch is None:
            expected = self._plateau_cfg_epoch
            if expected is not None and latest_epoch != expected:
                return empty
            target_epoch = expected if expected is not None else latest_epoch
        elif latest_epoch != target_epoch:
            # Raw samples are also an epoch authority. This covers a dropped
            # CFG_APPLIED audit line while still preventing cross-epoch mixing.
            if update_state:
                self._reset_plateau_monitor_locked()
            target_epoch = latest_epoch
            if update_state:
                self._plateau_cfg_epoch = latest_epoch

        indices = [
            index for index, epoch in enumerate(parsed_epochs)
            if epoch == target_epoch
        ]
        if not indices:
            return empty
        if (
            self._plateau_context_pending
            or self._plateau_context_epoch != target_epoch
        ):
            context_start_s = float(times[indices[0]])
            if update_state:
                self._plateau_context_epoch = target_epoch
                self._plateau_context_start_s = context_start_s
                self._plateau_expected_epoch = None
                self._plateau_context_pending = False
        indices = [
            index for index in indices
            if float(times[index]) >= context_start_s - 1e-12
        ]
        if not indices:
            return empty
        return {
            "time_s": [times[index] for index in indices],
            "current_nA": [currents[index] for index in indices],
            "valid": [validity[index] for index in indices],
            "epoch": [parsed_epochs[index] for index in indices],
        }

    def _maybe_auto_stop(self) -> None:
        """Update platform telemetry and stop only eligible formal IT runs."""

        with self.lock:
            if (
                self.state != "running"
                or self.settings.get("method") != "it"
                or self.user_stop_requested
                or self.auto_stop_requested
                or str(self.range_runtime.get("pending") or "").startswith(
                    "RANGE "
                )
                or not (self.settings.get("adaptive_stop")
                        or self.metadata.get("debug"))
            ):
                return
            raw_data = self._data()
            # Keep the formal gate self-contained for non-watcher callers. In
            # production this is normally a throttled no-op because the watcher
            # refreshed the same cache immediately before entering this method.
            self._refresh_live_analysis_locked(raw_data)
            self._apply_confirmed_reversal_to_plateau_locked()
            data = self._plateau_context_data_locked(raw_data)
            if not data["time_s"]:
                self._plateau_progress = {
                    "waiting_for_context": self._plateau_context_pending,
                    "expected_epoch": self._plateau_expected_epoch,
                }
                return
            elapsed_s = (
                max(data["time_s"]) if data.get("time_s") else 0.0
            )
            segment_s = self.plateau_config.segment_duration_s
            analysis_warmup_s = max(
                self.plateau_config.window_duration_s,
                float(self.settings.get("fit_window_s") or FIT_WINDOW_S),
            )
            latest_segment = int(math.floor(max(0.0, elapsed_s) / segment_s))
            first_context_segment = max(
                self.plateau_config.segment_count,
                int(math.ceil(
                    max(
                        self._plateau_context_start_s + analysis_warmup_s,
                        self._plateau_minimum_gate_until_s,
                    ) / segment_s - 1e-12
                )),
            )
            next_segment = max(
                self._plateau_last_segment + 1, first_context_segment
            )
            pending_windows = max(0, latest_segment - next_segment + 1)
            self._plateau_progress = {
                "elapsed_s": elapsed_s,
                "required_s": (
                    max(
                        self._plateau_context_start_s + analysis_warmup_s,
                        self._plateau_minimum_gate_until_s,
                    )
                ),
                "complete_segments": latest_segment,
                "pending_windows": pending_windows,
            }
            expected_rate = self._expected_native_rate_hz_locked()
            if pending_windows == 0:
                return
            allow_stop = (
                bool(self.settings.get("adaptive_stop"))
                and not self.metadata.get("debug")
            )
            rolling_ready = self._rolling_window_ready_for_stop_locked()
            self._plateau_progress["rolling_window_ready"] = rolling_ready
            stop_segment = min(
                latest_segment,
                next_segment + MAX_PLATEAU_BACKFILL_WINDOWS - 1,
            )
            for decision_segment in range(next_segment, stop_segment + 1):
                try:
                    evaluation = evaluate_platform(
                        data["time_s"], data["current_nA"], data["valid"],
                        self._analysis_filter_config(),
                        expected_sample_rate_hz=expected_rate,
                        config=self.plateau_config,
                        decision_segment=decision_segment,
                    )
                except (TypeError, ValueError) as exc:
                    self._plateau_consecutive_passes = 0
                    self._plateau_evaluation = {
                        "status": "invalid",
                        "stable": False,
                        "reason": f"平台判定失败：{exc}",
                        "config": self.plateau_config.to_dict(),
                    }
                    return
                if evaluation is None:
                    break
                self._plateau_last_segment = evaluation.complete_segment
                self._plateau_evaluation = _json_safe(asdict(evaluation))
                if evaluation.stable:
                    self._plateau_consecutive_passes += 1
                    if allow_stop and not rolling_ready:
                        self._plateau_consecutive_passes = min(
                            self._plateau_consecutive_passes,
                            max(
                                0,
                                self.plateau_config.required_consecutive_windows
                                - 1,
                            ),
                        )
                        self._plateau_progress[
                            "waiting_for_rolling_window"
                        ] = True
                else:
                    self._plateau_consecutive_passes = 0
                if (allow_stop and self._plateau_consecutive_passes
                        >= self.plateau_config.required_consecutive_windows):
                    self._request_stop_locked(automatic=True)
                    break
            self._plateau_progress["processed_segment"] = self._plateau_last_segment
            self._plateau_progress["pending_windows"] = max(
                0, latest_segment - self._plateau_last_segment
            )

    def _rolling_window_ready_for_stop_locked(self) -> bool:
        stage = self._prepared_live_stage
        if stage is None or not stage.window_complete:
            return False
        if self._rolling_metrics.get("stage_key") != stage.stage_key:
            return False
        value = self._rolling_metrics.get("steady_current_nA")
        if isinstance(value, bool):
            return False
        try:
            return math.isfinite(float(value))
        except (TypeError, ValueError):
            return False

    def _apply_confirmed_reversal_to_plateau_locked(self) -> None:
        """Consume one confirmed reversal from the watcher-owned ETA cache."""

        if not self._stability_eta.get("reset_consecutive"):
            return
        suggested_start = self._stability_eta.get("suggested_stage_start_s")
        self._stability_eta["reset_consecutive"] = False
        if (
            isinstance(suggested_start, bool)
            or not isinstance(suggested_start, (int, float))
            or not math.isfinite(float(suggested_start))
        ):
            return
        self._plateau_last_segment = 0
        self._plateau_consecutive_passes = 0
        self._plateau_evaluation = None
        self._plateau_progress = {}
        self._plateau_context_start_s = max(0.0, float(suggested_start))
        self._prepared_live_stage = None
        self._rolling_metrics = self._empty_rolling_metrics(
            "accumulating", "已确认曲线反转，正在重新累积末端窗口",
        )
        self._last_complete_rolling_metrics = None
        self._last_complete_rolling_epoch = None
        self._stop_requested_rolling_metrics = None
        self._plateau_minimum_gate_until_s = (
            self._plateau_context_start_s
            + self._stability_eta_estimator.config.minimum_stage_s
        )
        # The next watcher pass rebuilds rolling metrics from the new stage.
        self._live_analysis_last_refresh = 0.0

    def _plateau_payload(self, debug_series: dict[str, Any] | None = None) -> dict[str, Any]:
        config = self.plateau_config.to_dict()
        trigger_evidence = (
            dict(self._auto_stop_evidence)
            if self.auto_stop_requested
            and isinstance(self._auto_stop_evidence, dict)
            else None
        )
        source_evaluation = (
            trigger_evidence.get("evaluation")
            if trigger_evidence is not None
            else self._plateau_evaluation
        )
        evaluation = (
            dict(source_evaluation)
            if isinstance(source_evaluation, dict) else None
        )
        if evaluation is not None:
            means = list(evaluation.get("segment_means_nA") or [])
            centres = list(evaluation.get("segment_centres_s") or [])
            original_start = evaluation.get("window_start_s")
            original_end = evaluation.get("window_end_s")
            offset_s = 0.0
            if debug_series is not None:
                current_times = (debug_series.get("current") or {}).get("t") or []
                if current_times:
                    offset_s = float(current_times[0])
            if isinstance(original_start, (int, float)):
                evaluation["window_start_s"] = float(original_start) + offset_s
            if isinstance(original_end, (int, float)):
                evaluation["window_end_s"] = float(original_end) + offset_s
            if (not centres and isinstance(original_start, (int, float))
                    and isinstance(original_end, (int, float))):
                segment_s = self.plateau_config.segment_duration_s
                centres = [
                    float(original_start) + (index + 0.5) * segment_s
                    for index in range(self.plateau_config.segment_count)
                ]
            evaluation["segment_centres_s"] = [
                float(value) + offset_s for value in centres
            ]
            if centres:
                segment_s = self.plateau_config.segment_duration_s
                evaluation["segments"] = [
                    {
                        "index": index + 1,
                        "start_s": float(centre) - segment_s / 2 + offset_s,
                        "end_s": float(centre) + segment_s / 2 + offset_s,
                        "center_s": float(centre) + offset_s,
                        "mean_nA": (
                            float(means[index]) if index < len(means) else None
                        ),
                    }
                    for index, centre in enumerate(centres)
                ]
            if len(means) == len(centres) and centres:
                half = len(means) // 2
                segment_s = self.plateau_config.segment_duration_s
                if isinstance(original_start, (int, float)) and isinstance(
                    original_end, (int, float)
                ):
                    slope = evaluation.get("slope_nA_per_s")
                    intercept = evaluation.get("fit_intercept_nA")
                    if isinstance(slope, (int, float)) and isinstance(
                        intercept, (int, float)
                    ):
                        evaluation["trend_line"] = [
                            {
                                "time_s": float(original_start) + offset_s,
                                "current_nA": float(slope) * float(original_start)
                                + float(intercept),
                            },
                            {
                                "time_s": float(original_end) + offset_s,
                                "current_nA": float(slope) * float(original_end)
                                + float(intercept),
                            },
                        ]
                    split = float(original_start) + half * segment_s
                    evaluation["half_lines"] = [
                        {
                            "start_s": float(original_start) + offset_s,
                            "end_s": split + offset_s,
                            "mean_nA": evaluation.get("first_half_mean_nA"),
                        },
                        {
                            "start_s": split + offset_s,
                            "end_s": float(original_end) + offset_s,
                            "mean_nA": evaluation.get("second_half_mean_nA"),
                        },
                    ]
        preview = None
        elapsed = self._plateau_progress.get("elapsed_s")
        if (
            evaluation is None
            and debug_series is not None
            and isinstance(elapsed, (int, float))
            and not isinstance(elapsed, bool)
            and math.isfinite(float(elapsed))
        ):
            end_s = float(elapsed)
            start_s = max(
                self._plateau_context_start_s,
                end_s - self.plateau_config.window_duration_s,
            )
            if end_s > start_s:
                segment_s = self.plateau_config.segment_duration_s
                first_segment = int(math.floor(start_s / segment_s))
                last_segment = int(math.ceil(end_s / segment_s))
                current_times = (
                    (debug_series.get("current") or {}).get("t") or []
                )
                offset_s = float(current_times[0]) if current_times else 0.0
                preview = {
                    "window_start_s": start_s + offset_s,
                    "window_end_s": end_s + offset_s,
                    "segments": [
                        {
                            "index": segment_index + 1,
                            "start_s": max(
                                start_s, segment_index * segment_s,
                            ) + offset_s,
                            "end_s": min(
                                end_s, (segment_index + 1) * segment_s,
                            ) + offset_s,
                            "center_s": (
                                max(start_s, segment_index * segment_s)
                                + min(end_s, (segment_index + 1) * segment_s)
                            ) / 2 + offset_s,
                            "mean_nA": None,
                        }
                        for segment_index in range(
                            first_segment, last_segment
                        )
                        if min(
                            end_s, (segment_index + 1) * segment_s,
                        ) > max(start_s, segment_index * segment_s)
                    ],
                }
        return _json_safe({
            "enabled": bool(self.settings.get("adaptive_stop")),
            "monitoring": bool(
                self.settings.get("method") == "it"
                and (self.settings.get("adaptive_stop") or self.metadata.get("debug"))
            ),
            "stop_enabled": bool(
                self.settings.get("adaptive_stop") and not self.metadata.get("debug")
            ),
            "auto_stopped": self.auto_stop_requested,
            "consecutive_passes": (
                trigger_evidence.get("consecutive_passes")
                if trigger_evidence is not None
                else self._plateau_consecutive_passes
            ),
            "required_consecutive_windows": config["required_consecutive_windows"],
            "config": config,
            "progress": dict(self._plateau_progress),
            "evaluation": evaluation,
            "preview": preview,
            "trigger_evidence": trigger_evidence,
        })

    @staticmethod
    def _terminate_tree(process: subprocess.Popen[str]) -> None:
        """整棵进程组收掉,而不是只收第一层。

        🔴 只 `process.terminate()` 收不干净:树是
        it_tool → pa_host.collect → JLinkExe,`terminate` 只打到 it_tool,
        collect 与 JLinkExe 会一直活着占住探头和 telnet 19021(实测 60s 不自愈)。

        Windows: 先让 RTT 后端释放探头,超时后才用 taskkill /T 整棵收掉。
        macOS/Linux: 配合 Popen(start_new_session=True) 才能用 killpg 一次收完。
        JLinkExe 本身**不理 SIGTERM**(实测),但它父进程 collect 一退、stdin 管道
        EOF,它就会自己退 —— 所以关键是让 collect 收到信号并跑完它的 finally。
        """
        if process.poll() is not None:
            return
        if _IS_WIN:
            # Older ARM-OB probes can remain in LIBUSB_ERROR_TIMEOUT until a
            # physical replug if OpenOCD is killed while it owns WinUSB.  The
            # collector normally sends this shutdown itself, but GUI timeout
            # and startup-failure paths arrive here before that finally block.
            # Release the current workstation bridge first, then let the
            # wrapper/collector unwind.  Forced taskkill remains bounded as a
            # fallback for a broken child tree.
            if _port_accepts_connections(19021):
                try:
                    _release_stale_measurement_bridge()
                    process.wait(timeout=6)
                    return
                except (RuntimeError, subprocess.TimeoutExpired, OSError):
                    pass
            killed = False
            try:
                result = subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                    capture_output=True, timeout=10,
                    **runtime.hidden_subprocess_kwargs(),
                )
                killed = result.returncode == 0
            except (subprocess.TimeoutExpired, OSError):
                pass
            if not killed:
                try:
                    process.kill()
                except OSError:
                    pass
        else:
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError):
                process.terminate()

    def _terminate_if_running(
        self, process: subprocess.Popen[str], delay_s: float
    ) -> None:
        time.sleep(delay_s)
        if process.poll() is None:
            with self.lock:
                self._bridge_stop_forced = bool(
                    self.user_stop_requested or self.auto_stop_requested
                )
            self._terminate_tree(process)
            try:
                process.wait(timeout=6)
            except subprocess.TimeoutExpired:
                self._kill_tree(process)

    @staticmethod
    def _kill_tree(process: subprocess.Popen[str]) -> None:
        """Force a process group down after graceful shutdown timed out."""
        if process.poll() is not None:
            return
        if _IS_WIN:
            killed = False
            try:
                result = subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                    capture_output=True, timeout=10,
                    **runtime.hidden_subprocess_kwargs(),
                )
                killed = result.returncode == 0
            except (subprocess.TimeoutExpired, OSError):
                pass
            if not killed:
                try:
                    process.kill()
                except OSError:
                    pass
        else:
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                process.kill()

    def _watch(self, log_handle: Any) -> None:
        assert self.process is not None
        process = self.process
        while process.poll() is None:
            self._audit_events()
            with self.lock:
                self._maybe_retry_tagged_gate_get_locked()
                self._maybe_send_legacy_gate_get_locked()
            self._scan_range_events()
            self._maybe_auto_switch()
            with self.lock:
                self._refresh_live_analysis_locked()
            self._maybe_auto_stop()
            with self.lock:
                if self._config_gate.get("state") != "checking":
                    self.message = self._progress_message()
            time.sleep(0.5)
        return_code = process.wait()
        with self.lock:
            gate_checking = self._config_gate.get("state") == "checking"
        if gate_checking:
            self._fail_config_gate(
                "process_exit",
                f"采集进程在配置核对完成前退出（退出码 {return_code}）",
            )
        self._audit_events()
        self._scan_range_events()
        log_handle.close()
        self._stop_bridge()
        with self.lock:
            self.finished_at = time.time()
            self.debug_waiting_for_start = False
            self._debug_pending_cfg = None
            if self._prestart_gate_failed:
                gate_state = self._config_gate.get("state")
                self.state = "idle" if gate_state == "aborted" else "error"
                self.message = str(
                    self._config_gate.get("message") or "硬件配置核对失败"
                )
                self.error = "" if gate_state == "aborted" else self.message
                return
            terminal_data = self._data(update_monitor=False)
            requested_stop_exit = return_code in (3, -15) or (
                self._bridge_stop_forced and return_code in (0, 1)
            )
            if self.user_stop_requested and requested_stop_exit:
                self._freeze_live_analysis_locked(
                    terminal_data, completed=False,
                )
                self.state = "idle"
                self.error = ""
                self.message = "测量已停止"
                self._notify_complete()
                return
            adaptive_completion = self.auto_stop_requested and requested_stop_exit
            natural_completion = (
                return_code == 0
                and not self.user_stop_requested
                and not self.auto_stop_requested
            )
            if not natural_completion and not adaptive_completion:
                self._freeze_live_analysis_locked(
                    terminal_data, completed=False,
                )
                self.state = "error"
                collector_log = self.run_dir / "collector.log"
                try:
                    log_text = collector_log.read_text(
                        encoding="utf-8", errors="replace"
                    )
                except OSError:
                    log_text = ""
                detail = _meaningful_process_tail(log_text)
                transport = (
                    "V5.1 USB DATA CDC"
                    if HARDWARE_TRANSPORT == "serial"
                    else "RTT/J-Link"
                )
                if detail:
                    self.error = (
                        f"{transport} 采集失败（退出码 {return_code}）："
                        f"{' | '.join(detail)}"
                    )
                else:
                    self.error = (
                        f"{transport} 采集失败（退出码 {return_code}）。"
                        f"请查看 {collector_log}"
                    )
                self.message = "测量失败"
                self._notify_complete()
                return
            if self._hardware_taint is not None and not self.metadata.get("debug"):
                self._freeze_live_analysis_locked(
                    terminal_data, completed=False,
                )
                reason = str(self._hardware_taint.get("reason") or "unknown")
                self.state = "error"
                self.error = f"硬件报告 IT_TAINTED（{reason}），本轮结果已隔离"
                self.message = "测量受到运行时配置干扰，原始数据已保留"
                self._notify_complete()
                return
            self._freeze_live_analysis_locked(
                terminal_data, completed=True,
            )
            try:
                assert self.raw_path and self.resampled_path and self.summary_path
                if self.settings["method"] == "cv":
                    export_cv_csv(self.raw_path, self.resampled_path, self.settings)
                    summary = summarize_cv(self.raw_path, self.settings)
                    save_cv_summary(summary, self.summary_path)
                    if self.plot_path is not None:
                        plot_cv(self.raw_path, self.plot_path)
                    self.message = "CV 完成，全部原生电流点已保存"
                else:
                    resample_run_10hz(
                        self.raw_path, self.resampled_path,
                        duration_s=(
                            None if self.settings["adaptive_stop"]
                            else self.settings["duration_s"]
                        ),
                        target_rate_hz=self.settings["target_rate_hz"],
                    )
                    analysis_path = self.resampled_path
                    analysis_filter = self._analysis_filter_config()
                    if analysis_filter.get("mode") == "analysis":
                        self.filter_meta = write_filtered_csv(
                            self.resampled_path, self.filtered_path, analysis_filter
                        )
                        if self.filter_meta.get("applied"):
                            analysis_path = self.filtered_path
                    summary = summarize_run(
                        analysis_path, window_s=self.settings["fit_window_s"]
                    )
                    save_summary(summary, self.summary_path)
                    self.message = "测量完成，已生成 10 Hz 数据和末段汇总"
                self.summary = _json_safe(asdict(summary))
                if self.settings["method"] != "cv":
                    legacy_steady = self.summary.get("steady_current_nA")
                    rolling_steady = self._rolling_metrics.get(
                        "steady_current_nA"
                    )
                    self.summary["legacy_resampled_steady_current_nA"] = (
                        legacy_steady
                    )
                    # Calibration and prediction consume this field. Keep it
                    # identical to the final native rolling metric, including
                    # an explicit null when no complete native window exists.
                    self.summary["steady_current_nA"] = rolling_steady
                    self.summary["steady_current_source"] = (
                        "native_rolling_window"
                        if rolling_steady is not None
                        else "native_rolling_window_unavailable"
                    )
                    self.summary["steady_current_reason"] = (
                        self._rolling_metrics.get("reason")
                    )
                self.summary["rolling_metrics"] = _json_safe(
                    self._rolling_metrics
                )
                self.summary["stability_eta"] = _json_safe(
                    self._stability_eta
                )
                self.summary["filter"] = {
                    "config": self.filter_config,
                    "analysis_config": (
                        analysis_filter if self.settings["method"] != "cv" else None
                    ),
                    "effective": self.filter_meta,
                    "rolling_effective": self._rolling_metrics.get(
                        "filter_meta"
                    ),
                    "analysis_source": str(analysis_path) if self.settings["method"] != "cv" else "raw",
                    "rolling_source": (
                        str(self.raw_path)
                        if self.settings["method"] != "cv" else "raw"
                    ),
                }
                trigger_evidence = (
                    dict(self._auto_stop_evidence)
                    if self.auto_stop_requested
                    and isinstance(self._auto_stop_evidence, dict)
                    else None
                )
                self.summary["adaptive_stop"] = {
                    "enabled": bool(self.settings.get("adaptive_stop")),
                    "auto_stopped": self.auto_stop_requested,
                    "consecutive_passes": (
                        trigger_evidence.get("consecutive_passes")
                        if trigger_evidence is not None
                        else self._plateau_consecutive_passes
                    ),
                    "required_consecutive_windows": (
                        self.plateau_config.required_consecutive_windows
                    ),
                    "config": self.plateau_config.to_dict(),
                    "evaluation": (
                        trigger_evidence.get("evaluation")
                        if trigger_evidence is not None
                        else self._plateau_evaluation
                    ),
                    "trigger_evidence": trigger_evidence,
                }
                self.summary["hardware_config"] = self.metadata.get("hardware_config")
                if self.summary_path is not None:
                    self.summary_path.write_text(
                        json.dumps(self.summary, indent=2, ensure_ascii=False),
                        encoding="utf-8",
                    )
                self.state = "completed"
            except Exception as exc:  # keep the raw run even if analysis fails
                self.state = "error"
                self.error = f"测量已落盘，但分析失败：{exc}"
                self.message = "分析失败，原始数据仍已保存"
        self._notify_complete()

    def _scan_range_events(self) -> None:
        """增量扫 rtt.log,取固件回的 RANGE_APPLIED / RANGE_REJECT。

        为什么读文件而不是解析 collector 的 stdout:这两行走的是 RTT 上行,
        由 collector 的 --raw-log 落到 rtt.log;stdout 里只有它自己的进度信息。
        用读位置增量读 ⇒ 不重复处理、也不受文件增长影响。
        """
        if self.raw_log is None or not self.raw_log.exists():
            return
        try:
            with self.raw_log.open("r", encoding="utf-8", errors="replace") as fh:
                fh.seek(self._rtt_pos)
                fresh = fh.read()
                self._rtt_pos = fh.tell()
        except OSError:
            return
        lines, self._rtt_pending = _split_complete_lines(fresh, self._rtt_pending)
        for line in lines:
            line = line.strip()
            taint: dict[str, Any] | None = None
            if "IT_TAINTED" in line:
                marker = line[line.index("IT_TAINTED"):]
                taint = {"kind": "IT_TAINTED", "raw": marker}
                for token in marker.split()[1:]:
                    key, separator, value = token.partition("=")
                    if not separator:
                        continue
                    try:
                        taint[key] = int(value)
                    except ValueError:
                        taint[key] = value
            elif "IT_DONE" in line and re.search(r"\btainted=1(?:\s|$)", line):
                taint = {
                    "kind": "IT_DONE",
                    "reason": "firmware_final_marker",
                    "tainted": 1,
                    "raw": line[line.index("IT_DONE"):],
                }
            if taint is not None:
                with self.lock:
                    self._hardware_taint = taint
            if "RANGE_APPLIED" in line:
                kv: dict[str, Any] = {}
                for tok in line[line.index("RANGE_APPLIED"):].split()[1:]:
                    if "=" in tok:
                        k, _, v = tok.partition("=")
                        try:
                            kv[k] = int(v)
                        except ValueError:
                            kv[k] = v
                with self.lock:
                    range_matches_live = (
                        kv.get("fsr_code") == self._cfg_live.get("fsr")
                        and kv.get("offset_sel") == self._cfg_live.get("off")
                    )
                    context_already_observed = (
                        self._plateau_cfg_epoch is not None
                        and self._plateau_context_epoch == self._plateau_cfg_epoch
                        and range_matches_live
                    )
                    if not context_already_observed:
                        expected_epoch = (
                            self._plateau_cfg_epoch
                            if self._plateau_cfg_epoch
                            != self._plateau_context_epoch
                            else None
                        )
                        self._reset_plateau_monitor_locked(
                            hardware_context_changed=True,
                            expected_epoch=expected_epoch,
                        )
                    self.range_runtime = {"pending": None, "applied": kv,
                                          "rejected": None, "at": time.time()}
            elif "RANGE_REJECT" in line:
                with self.lock:
                    self.range_runtime = {**self.range_runtime, "pending": None,
                                          "rejected": line[line.index("RANGE_REJECT"):],
                                          "at": time.time()}

    def _notify_complete(self) -> None:
        with self.lock:
            run_id = self.run_id
            should_log = bool(
                run_id and run_id != self._diagnostic_completion_logged_run_id
            )
            if should_log:
                self._diagnostic_completion_logged_run_id = run_id
            completion = {
                "run_id": run_id,
                "state": self.state,
                "status_message": self.message,
                "error": self.error,
                "run_dir": str(self.run_dir or ""),
                "raw_path": str(self.raw_path or ""),
                "summary_path": str(self.summary_path or ""),
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "transport": HARDWARE_TRANSPORT,
                "config_gate": dict(self._config_gate),
            }
        if should_log:
            DIAGNOSTICS.record(
                "info" if completion["state"] == "completed" else "error",
                (
                    "measurement.completed"
                    if completion["state"] == "completed"
                    else "measurement.failed"
                ),
                str(completion["status_message"] or completion["error"]),
                **completion,
            )
        callbacks = (self.completion_hook, self.on_complete)
        for callback in callbacks:
            if callback is not None:
                try:
                    # The workflow hook records export results on the controller.
                    # Refresh before the schedule hook so it can see export errors.
                    callback(self.snapshot())
                except Exception as exc:
                    DIAGNOSTICS.exception(
                        "measurement.completion_callback_failed",
                        "Measurement completion callback failed",
                        exc,
                        run_id=run_id,
                        callback=getattr(callback, "__name__", type(callback).__name__),
                    )

    def set_workflow_result(self, result: dict[str, Any]) -> None:
        with self.lock:
            self.workflow_result = _json_safe(result)
            saved = result.get("data_path") or result.get("raw_path")
            if self.state == "completed" and saved:
                self.message = f"测量完成并已自动保存：{saved}"

    def _progress_message(self) -> str:
        count = len(self._data()["time_s"])
        if self.auto_stop_requested:
            return "已检测到稳定平台，正在结束测量"
        if self.user_stop_requested:
            return "正在停止硬件测量"
        if self.settings.get("method") == "it" and self.settings.get("adaptive_stop"):
            eta_text = str(
                self._stability_eta.get("display_text") or "正在估计"
            )
            return f"正在采集：已收到 {count} 个原生点；{eta_text}"
        return f"正在采集：已收到 {count} 个原生点"

    def _data(self, *, update_monitor: bool = True) -> dict[str, Any]:
        if not self.raw_path or not self.raw_path.exists():
            return {"time_s": [], "current_nA": [], "valid": [], "epoch": []}
        if update_monitor and self._data_context_reset_pending:
            self._data_context_reset_pending = False
            self._reset_plateau_monitor_locked(clock_reset=True)
        if self._data_cache_path != self.raw_path:
            self._reset_data_cache()
            self._data_cache_path = self.raw_path
            if self.settings["method"] == "cv":
                self._data_cache.update({
                    "potential_v": [], "cycle": [], "direction": [],
                })
        try:
            if self.raw_path.stat().st_size < self._data_cache_position:
                self._reset_data_cache()
                self._data_cache_path = self.raw_path
                if update_monitor:
                    self._reset_plateau_monitor_locked(clock_reset=True)
                else:
                    self._data_context_reset_pending = True
                if self.settings["method"] == "cv":
                    self._data_cache.update({
                        "potential_v": [], "cycle": [], "direction": [],
                    })
            with self.raw_path.open(
                newline="", encoding="utf-8", errors="replace"
            ) as handle:
                handle.seek(self._data_cache_position)
                chunk = handle.read()
                self._data_cache_position = handle.tell()
        except OSError:
            return self._data_cache

        text = self._data_cache_pending + chunk
        lines = text.splitlines(keepends=True)
        self._data_cache_pending = ""
        if lines and not lines[-1].endswith(("\n", "\r")):
            self._data_cache_pending = lines.pop()
        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            try:
                values = next(csv.reader([stripped]))
                if self._data_cache_header is None:
                    self._data_cache_header = values
                    continue
                row = dict(zip(self._data_cache_header, values, strict=False))
                dev_ms = float(row["dev_ms"])
                current_nA = float(row["fa_fw"]) / 1_000_000
                if not math.isfinite(dev_ms) or not math.isfinite(current_nA):
                    continue
                if (self._data_last_dev_ms is not None
                        and dev_ms < self._data_last_dev_ms):
                    # A target reset can leave a short tail from the previous
                    # uptime in the same file. Keep the live chart monotonic,
                    # matching the offline loader's final-segment policy.
                    for values in self._data_cache.values():
                        values.clear()
                    self._data_cache_first_dev_ms = None
                    if update_monitor:
                        self._reset_plateau_monitor_locked(clock_reset=True)
                    else:
                        self._data_context_reset_pending = True
                first_dev_ms = (
                    dev_ms if self._data_cache_first_dev_ms is None
                    else self._data_cache_first_dev_ms
                )
                valid = (
                    int(row.get("sat") or 0) == 0
                    and int(row.get("ovf") or 0) == 0
                )
                raw_epoch = row.get("epoch")
                sample_epoch = (
                    None if raw_epoch in (None, "") else int(raw_epoch)
                )
                if self.settings["method"] == "cv":
                    potential_v = float(row["potential_mv"]) / 1000
                    if not math.isfinite(potential_v):
                        continue
                    cycle = int(row["cycle"])
                    direction = int(row["direction"])
                self._data_cache_first_dev_ms = first_dev_ms
                self._data_last_dev_ms = dev_ms
                self._data_cache["time_s"].append(
                    (dev_ms - first_dev_ms) / 1000
                )
                self._data_cache["current_nA"].append(current_nA)
                self._data_cache["valid"].append(valid)
                self._data_cache["epoch"].append(sample_epoch)
                if self.settings["method"] == "cv":
                    self._data_cache["potential_v"].append(potential_v)
                    self._data_cache["cycle"].append(cycle)
                    self._data_cache["direction"].append(direction)
            except (KeyError, TypeError, ValueError, csv.Error):
                continue
        return self._data_cache

    def _cell_voltages(self) -> dict[str, Any] | None:
        """读电极电压连采 CSV 的最后一行。

        为什么单独一个文件而不是并进电流 CSV:System ADC 按 SYS_PERIOD(≈1Hz)走,
        电流按 SENS_PERIOD(8Hz)走,两者**不同步**,塞一张表必然错行。
        为什么只取最后一行:GUI 只需要"现在电极在哪个电位";全序列留给离线分析。
        """
        path = self.cell_v_path
        if path is None or not path.exists():
            return None
        try:
            rows = [
                ln for ln in path.read_text(encoding="utf-8", errors="replace").splitlines()
                if ln and not ln.startswith("#")
            ]
        except OSError:
            return None
        if len(rows) < 2:
            return None
        header = rows[0].split(",")
        last = rows[-1].split(",")
        if len(last) != len(header):
            return None
        out: dict[str, Any] = {}
        for key, raw in zip(header, last):
            try:
                out[key] = float(raw) if "." in raw else int(raw)
            except ValueError:
                out[key] = raw
        # 🔴 两类"撞轨"必须分开报,否则告警会互相淹没(2026-08-11 实测:CE 顶在
        #    下轨让 65/66 行都亮 clipped,真正会让 E 失效的 WE/RE 削顶反而看不见了)。
        #   clipped:WE 或 RE 出界 ⇒ **E 这个数不可信**(它就是这两路之差)
        #   railed :CE 或 WO 撞轨 ⇒ 放大器用尽驱动范围、**环路饱和**,
        #           E 可能仍读得出来但电解池已不在设定电位上(此时 e_mv 会偏离设定值)
        out["clipped"] = any(out.get(k) in (0, 4095) for k in ("we_code", "re_code"))
        # 🔴 2026-08-11 拆分。原来把 ce_code 与 wo_code 一起算 railed,现在不成立:
        #   ① **CE 的规格下限是 0.1V,不是 0**(datasheet p11「CE Output Voltage Range」
        #      CP_EN=0 ⇒ 0.1V ~ VDD−1.1V)。CE 掉到 80mV 时环路已经出规格,
        #      按 code==0 判会漏报。100mV / 0.375mV ≈ 267 码。
        #   ② **WO 出量程现在是设计使然**:V_WE 默认改 1200 后 WO≈V_WE+540≈1745mV
        #      > System ADC 在 1.0× 下的满量程 1536mV ⇒ wo_code 恒 4095。
        #      继续把它算作"撞轨"会让告警灯常红,而常红的灯等于没有灯。
        #      ⇒ 单独报 wo_offscale,措辞是"看不见"而不是"坏了"。
        ce_code = out.get("ce_code")
        out["ce_railed"] = isinstance(ce_code, int) and (ce_code <= 267 or ce_code >= 4095)
        out["wo_offscale"] = out.get("wo_code") in (0, 4095)
        out["railed"] = bool(out["ce_railed"])
        # 🔴 恒电位环用了多少驱动、还剩多少 —— 这两个数把"环路快饱和了"变成可读数字。
        #   ce_drive_mv:C 放大器为了把电流推过电解池,需要把 CE 压到 RE 之下多少。
        #                健康态实测只需 ~60 mV(v1:CE 140 / RE 201)。
        #   ce_headroom_mv:CE 距 0 轨还有多少。它见底 ⇒ 环路钳不住设定电位。
        if isinstance(out.get("ce_mv"), (int, float)) and isinstance(out.get("re_mv"), (int, float)):
            out["ce_drive_mv"] = out["re_mv"] - out["ce_mv"]
            # 规格下限是 0.1V ⇒ 余量按到 100mV 算,不是到 0
            out["ce_headroom_mv"] = out["ce_mv"] - 100
        # 🔴 V_WE 的合法窗口 —— 现在两头都能给实测值,不再靠编译期假定的 VDD=3000:
        #     E + drive + 0.1V  ≤  V_WE  ≤  VDD − 1.1V   (CP_EN=0;=1 时是 −0.7V)
        #   上限来自实测 VDD(tag 0xE0);下限来自实测 drive=RE−CE。
        #   vdd_mv == -1 ⇒ 固件没上报(旧版本或通道没选中)⇒ 这几项都不给,
        #   宁可缺字段也不拿假定值冒充实测(公理 A4:器件是权威)。
        vdd = out.get("vdd_mv")
        if isinstance(vdd, (int, float)) and vdd > 0:
            out["we_max_mv"] = int(vdd) - 1100
            out["we_max_mv_cp"] = int(vdd) - 700   # 开 CP_EN 后的上限
            if isinstance(out.get("we_mv"), (int, float)):
                out["we_headroom_mv"] = out["we_max_mv"] - out["we_mv"]
            if isinstance(out.get("ce_drive_mv"), (int, float)) and \
                    isinstance(out.get("e_mv"), (int, float)):
                out["we_min_mv"] = int(out["e_mv"] + out["ce_drive_mv"] + 100)
        out["rows"] = len(rows) - 1
        return out

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            data = self._data(update_monitor=False)
            operation_phase = (
                "configuring"
                if self.state == "running"
                and self._config_gate.get("state") == "checking"
                else ("running" if self.state == "running" else self.state)
            )
            latest_sample = None
            if data["time_s"]:
                index = len(data["time_s"]) - 1
                latest_sample = {
                    "index": index,
                    "time_s": data["time_s"][index],
                    "current_nA": data["current_nA"][index],
                    "valid": data["valid"][index],
                }
                if self.settings["method"] == "cv":
                    latest_sample.update({
                        "potential_v": data["potential_v"][index],
                        "cycle": data["cycle"][index],
                        "direction": data["direction"][index],
                    })
            payload = {
                **_transport_status(),
                "state": self.state,
                "operation_phase": operation_phase,
                "busy": self.state == "running" or bool(
                    self.thread is not None and self.thread.is_alive()
                ),
                "message": self.message,
                "error": self.error,
                # 方案 C:运行时档位。**与 settings 里的 fsr_nA/offset_nA 不是一回事** ——
                # 那是最后一次烧录的编译期默认值,RANGE 命令能在运行中改掉实际档位。
                "range_runtime": self.range_runtime,
                "cell_v": self._cell_voltages(),
                "transient": _transient_phase(data["time_s"], data["current_nA"],
                                              data["valid"]),
                "auto_switch": {
                    "enabled": self.auto_switch_meas,
                    "done": self._auto_switch_done,
                    "target": {"fsr_code": MEAS_FSR_CODE,
                               "offset_sel": MEAS_OFFSET_SEL},
                },
                "adaptive_stop": self._plateau_payload(),
                "rolling_metrics": dict(self._rolling_metrics),
                "stability_eta": dict(self._stability_eta),
                "config_gate": dict(self._config_gate),
                "run_id": self.run_id,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "run_dir": str(self.run_dir) if self.run_dir else "",
                "raw_path": str(self.raw_path) if self.raw_path else "",
                "resampled_path": str(self.resampled_path) if self.resampled_path else "",
                "filtered_path": str(self.filtered_path) if self.filtered_path else "",
                "summary_path": str(self.summary_path) if self.summary_path else "",
                "plot_path": str(self.plot_path) if self.plot_path else "",
                "summary": self.summary,
                "filter": {
                    "config": self.filter_config,
                    "effective": self.filter_meta,
                },
                "workflow_result": self.workflow_result,
                "metadata": self.metadata,
                "hardware_taint": self._hardware_taint,
                "data": data,
                "latest_sample": latest_sample,
                "settings": {
                    **self.settings,
                    "native_rate_note": (
                        "CV 使用 EIS ADC，按 1 mV 步进；每个原生电流点实时显示并保存"
                        if self.settings["method"] == "cv"
                        else (
                            "宽量程 I-T 使用 EIS ADC；单次电位扰动小于 0.4 mV"
                            if self.settings["fsr_nA"] in IT_WIDE_FSR_OPTIONS
                            else "MAX30131 原生约 8.06 Hz；高于原生的输出频率由主机重采样"
                        )
                    ),
                },
            }
            return _json_safe(payload)


class ScheduleController:
    """Run non-overlapping IT measurements at a fixed start-to-start interval."""

    def __init__(self, measurement: MeasurementController) -> None:
        self.measurement = measurement
        self.lock = threading.RLock()
        self.active = False
        self.interval_s = 300.0
        self.max_runs = 0
        self.total_minutes = 0.0
        self.stop_at: float | None = None
        self.attempted_runs = 0
        self.completed_runs = 0
        self.failed_runs = 0
        self.next_run_at: float | None = None
        self.sample_prefix = "自动样品"
        self.known_concentration_um: float | None = None
        self.sample_role = "test"
        self.save_dir = ""
        self.message = "自动测量未启动"
        self.history: list[dict[str, Any]] = []
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.generation = 0
        self.settings = dict(SettingsController.DEFAULTS)
        self.filter_config = dict(FILTER_DEFAULTS)
        self.plateau_config = PlateauConfig.validate(None)
        self.metadata_hook: Any = None

    def set_filter_config(self, config: dict[str, Any]) -> None:
        normalized = validate_filter_config(config)
        with self.lock:
            self.filter_config = normalized

    def set_plateau_config(self, config: PlateauConfig | dict[str, Any]) -> None:
        normalized = PlateauConfig.validate(config)
        with self.lock:
            self.plateau_config = normalized

    def start(self, payload: dict[str, Any]) -> dict[str, Any]:
        interval_minutes = float(payload.get("interval_minutes", 5))
        total_minutes = float(payload.get("total_minutes", 0))
        if not math.isfinite(interval_minutes) or interval_minutes <= 0:
            raise ValueError("自动任务间隔必须是有限的正数")
        if not math.isfinite(total_minutes) or total_minutes < 0:
            raise ValueError("自动任务总时长必须是有限的非负数")
        settings = SettingsController.validate(payload.get("settings", {}))
        sample_role = (
            "cv" if settings["method"] == "cv"
            else str(payload.get("sample_role") or "test")
        )
        raw_concentration = payload.get("known_concentration_um")
        known_concentration = (
            None if raw_concentration in (None, "") else float(raw_concentration)
        )
        if known_concentration is not None and not math.isfinite(known_concentration):
            raise ValueError("浓度必须是有限数字")
        if known_concentration is not None and known_concentration < 0:
            raise ValueError("浓度不能为负数")
        if sample_role not in {"calibration", "stabilization", "test", "cv"}:
            raise ValueError("自动任务类型必须是标定、稳定化、测试或 CV")
        if sample_role == "calibration" and known_concentration is None:
            raise ValueError("自动标定任务必须填写已知浓度")
        minimum_interval_s = (
            settings["prestep_s"]
            + max(
                self.plateau_config.window_duration_s,
                settings["fit_window_s"],
            )
            + (self.plateau_config.required_consecutive_windows - 1)
            * self.plateau_config.segment_duration_s
            + 10
            if settings.get("adaptive_stop")
            else settings["prestep_s"] + settings["duration_s"] + 10
        )
        if interval_minutes * 60 < minimum_interval_s:
            raise ValueError(
                f"当前测量条件要求间隔至少 {minimum_interval_s / 60:.2f} 分钟"
            )
        with self.lock:
            if self.active:
                raise RuntimeError("自动测量已经在运行")
        if self.measurement.is_busy():
            raise RuntimeError("请等待当前手动测量结束")
        with self.lock:
            if self.active:
                raise RuntimeError("自动测量已经在运行")
            self.active = True
            self.interval_s = interval_minutes * 60
            self.max_runs = max(0, int(payload.get("max_runs", 0)))
            self.total_minutes = total_minutes
            self.attempted_runs = 0
            self.completed_runs = 0
            self.failed_runs = 0
            self.sample_prefix = str(payload.get("sample_prefix") or "自动样品")
            self.known_concentration_um = known_concentration
            self.sample_role = sample_role
            self.save_dir = str(payload.get("save_dir") or "")
            self.settings = settings
            started_at = time.time()
            self.stop_at = (
                started_at + self.total_minutes * 60
                if self.total_minutes > 0 else None
            )
            self.next_run_at = started_at if payload.get("start_now", True) else started_at + self.interval_s
            self.message = "自动测量已启动"
            self.stop_event = threading.Event()
            self.generation += 1
            generation = self.generation
            self.thread = threading.Thread(
                target=self._loop,
                args=(self.stop_event, generation),
                name=f"schedule-{generation}",
                daemon=True,
            )
            self.thread.start()
            DIAGNOSTICS.record(
                "info", "schedule.started", "Automatic measurement schedule started",
                generation=generation,
                interval_s=self.interval_s,
                max_runs=self.max_runs,
                total_minutes=self.total_minutes,
                sample_role=self.sample_role,
                workspace=self.save_dir,
            )
            return self.snapshot()

    def stop(self) -> dict[str, Any]:
        with self.lock:
            self.active = False
            self.next_run_at = None
            self.stop_at = None
            self.message = "自动测量已停止；正在进行的测量会正常完成"
            self.stop_event.set()
            DIAGNOSTICS.record(
                "info", "schedule.stopped", "Automatic measurement schedule stopped",
                generation=self.generation,
                attempted_runs=self.attempted_runs,
                completed_runs=self.completed_runs,
                failed_runs=self.failed_runs,
            )
            return self.snapshot()

    def _loop(self, stop_event: threading.Event, generation: int) -> None:
        while not stop_event.wait(0.25):
            with self.lock:
                if (generation != self.generation
                        or stop_event is not self.stop_event or not self.active):
                    return
                if self.stop_at is not None and time.time() >= self.stop_at:
                    self.active = False
                    self.next_run_at = None
                    self.message = f"稳定化阶段已结束，共完成 {self.completed_runs} 次测量"
                    stop_event.set()
                    return
                due = self.next_run_at is not None and time.time() >= self.next_run_at
            if not due:
                continue
            if self.measurement.is_busy():
                continue
            with self.lock:
                if (self.stop_at is not None
                        and not self.settings.get("adaptive_stop")
                        and time.time() + self.settings["prestep_s"]
                        + self.settings["duration_s"] + 5 > self.stop_at):
                    self.active = False
                    self.next_run_at = None
                    self.message = f"计划时段已结束，共完成 {self.completed_runs} 次测量"
                    stop_event.set()
                    return
                run_number = self.attempted_runs + 1
                self.attempted_runs = run_number
                scheduled_at = self.next_run_at or time.time()
                self.next_run_at = scheduled_at + self.interval_s
                metadata = {
                    "source": "schedule",
                    "sample_name": f"{self.sample_prefix} {run_number}",
                    "known_concentration_um": self.known_concentration_um,
                    "sample_role": self.sample_role,
                    "save_dir": self.save_dir,
                    "scheduled_at": scheduled_at,
                }
                filter_config = dict(self.filter_config)
                plateau_config = self.plateau_config
                self.message = f"正在执行第 {run_number} 次自动测量"
            try:
                if self.metadata_hook is not None:
                    metadata = self.metadata_hook(metadata)
                if stop_event.is_set() or stop_event is not self.stop_event:
                    return
                self.measurement.start_verified(
                    metadata=metadata,
                    on_complete=lambda run, run_generation=generation: self._completed(
                        run, run_generation
                    ),
                    settings=self.settings,
                    filter_config=filter_config,
                    plateau_config=plateau_config,
                )
            except Exception as exc:
                with self.lock:
                    self.message = f"自动测量启动失败：{exc}"
                    self.active = False
                    self.next_run_at = None
                    self.stop_at = None
                    self.failed_runs += 1
                    self.stop_event.set()
                DIAGNOSTICS.exception(
                    "schedule.run_start_failed",
                    "Scheduled measurement could not be started",
                    exc,
                    generation=generation,
                    run_number=run_number,
                    metadata=metadata,
                )
                return

    def _completed(
        self, run: dict[str, Any], generation: int | None = None
    ) -> None:
        with self.lock:
            if generation is not None and generation != self.generation:
                return
            workflow_result = run.get("workflow_result") or {}
            export_error = (
                workflow_result.get("export_error")
                if isinstance(workflow_result, dict) else None
            )
            succeeded = run.get("state") == "completed" and not export_error
            if succeeded:
                self.completed_runs += 1
            else:
                self.failed_runs += 1
            self.history.insert(0, {
                "run_id": run.get("run_id"),
                "finished_at": run.get("finished_at"),
                "state": "error" if export_error else run.get("state"),
                "error": export_error or run.get("error"),
                "summary": run.get("summary"),
                "metadata": run.get("metadata"),
                "run_dir": run.get("run_dir"),
            })
            self.history = self.history[:100]
            if not succeeded:
                self.active = False
                self.next_run_at = None
                self.stop_at = None
                self.message = (
                    f"第 {self.attempted_runs} 次自动测量失败，任务已暂停："
                    f"{export_error or run.get('error') or run.get('state') or '未知错误'}"
                )
                self.stop_event.set()
            elif self.max_runs and self.completed_runs >= self.max_runs:
                self.active = False
                self.next_run_at = None
                self.stop_at = None
                self.message = f"自动测量已完成，共 {self.completed_runs} 次"
                self.stop_event.set()
            elif self.active:
                self.message = f"第 {self.completed_runs} 次完成，等待下一次"

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return _json_safe({
                "active": self.active,
                "interval_minutes": self.interval_s / 60,
                "max_runs": self.max_runs,
                "total_minutes": self.total_minutes,
                "stop_at": self.stop_at,
                "attempted_runs": self.attempted_runs,
                "completed_runs": self.completed_runs,
                "failed_runs": self.failed_runs,
                "next_run_at": self.next_run_at,
                "sample_prefix": self.sample_prefix,
                "known_concentration_um": self.known_concentration_um,
                "sample_role": self.sample_role,
                "save_dir": self.save_dir,
                "message": self.message,
                "history": self.history,
                "settings": self.settings,
                "plateau_config": self.plateau_config.to_dict(),
            })


class AppState:
    @staticmethod
    def _empty_drift() -> dict[str, Any]:
        return {
            "enabled": False,
            "solution_name": "",
            "known_concentration_um": None,
            "bias_nA": 0.0,
            "slope_nA_per_hour": None,
            "start_current_nA": None,
            "end_current_nA": None,
            "start_at": None,
            "end_at": None,
            "record_ids": [],
            "calculated_at": None,
        }

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.operation_lock = threading.RLock()
        self.history = WorkspaceHistory(HISTORY_PATH, PROJECT_DIR)
        self.settings = SettingsController()
        self.filter = FilterController()
        self.plateau = PlateauController()
        self.measurement = MeasurementController()
        self.schedule = ScheduleController(self.measurement)
        self.measurement.settings = self.settings.snapshot()["settings"]
        self.measurement.set_plateau_config(self.plateau.settings)
        with self.measurement.lock:
            self.measurement._reset_live_analysis_locked()
        self.schedule.set_plateau_config(self.plateau.settings)
        self.save_dir: Path | None = None
        self.workspace_root: Path | None = None
        self.workspace_available = False
        self.workspace_error = ""
        self.model: CalibrationModel | None = None
        self.model_path: Path | None = None
        self.model_settings: dict[str, Any] | None = None
        self.model_plateau: dict[str, Any] | None = None
        self.calibration_filter: dict[str, Any] | None = None
        self.calibration_settings: dict[str, Any] | None = None
        self.calibration_plateau: dict[str, Any] | None = None
        self.points: list[CalibrationPoint] = []
        self.point_records: list[dict[str, Any]] = []
        self.selected_point_ids: list[str] = []
        self.model_created_at: float | None = None
        self.validation_started_at: float | None = None
        self.records: list[dict[str, Any]] = []
        self.validation_overrides: dict[str, dict[str, Any]] = {}
        self.manual_validation_points: list[dict[str, Any]] = []
        self.deleted_validation_point_ids: set[str] = set()
        self.drift = self._empty_drift()
        self.workspace_runtime_settings: dict[str, Any] | None = None
        self.workspace_runtime_filter: dict[str, Any] | None = None
        self.workspace_runtime_plateau: dict[str, Any] | None = None
        self.latest_workflow_result: dict[str, Any] | None = None
        if WORKFLOW_PATH.exists():
            try:
                saved = json.loads(WORKFLOW_PATH.read_text(encoding="utf-8"))
                raw_save_dir = str(saved.get("save_dir") or "").strip()
                if not raw_save_dir:
                    raise ValueError("已保存的工作区路径为空")
                self.save_dir = self._resolve_save_dir(raw_save_dir)
                raw_workspace_root = str(saved.get("workspace_root") or "").strip()
                saved_workspace_root = (
                    self._resolve_save_dir(raw_workspace_root)
                    if raw_workspace_root else None
                )
                inferred_root = self._workspace_root_for(self.save_dir)
                self.workspace_root = (
                    saved_workspace_root
                    if saved_workspace_root is not None and (
                        saved_workspace_root == self.save_dir
                        or self.save_dir.parent == saved_workspace_root
                    )
                    else inferred_root
                )
                self._load_workspace(create=False)
            except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
                self.workspace_available = False
                self.workspace_error = self.workspace_error or (
                    f"已保存的工作区当前不可用：{exc}"
                )
                DIAGNOSTICS.record(
                    "warning", "workspace.restore_failed",
                    "Saved workspace could not be restored",
                    path=WORKFLOW_PATH, error=str(exc),
                )
        self.schedule.metadata_hook = self._prepare_export_metadata
        self.measurement.completion_hook = self._measurement_completed

    def hardware_idle(self) -> bool:
        """Updates may use the network only while no hardware operation is active."""
        return bool(
            not self.measurement.is_busy()
            and not self.schedule.snapshot()["active"]
            and self.settings.snapshot()["state"] != "applying"
        )

    @staticmethod
    def _resolve_save_dir(value: str) -> Path:
        raw = os.path.expandvars(os.path.expanduser(value.strip()))
        if not raw:
            raise ValueError("请先选择工作区目录")
        path = Path(raw)
        if not path.is_absolute():
            path = PROJECT_DIR / path
        return path.resolve()

    def _configured_save_dir(self) -> Path:
        if self.save_dir is None:
            raise RuntimeError("请先选择工作区，未选择工作区时不能测量或保存数据")
        return self.save_dir

    def _configured_workspace_root(self) -> Path:
        save_dir = self._configured_save_dir()
        if self.workspace_root is None:
            self.workspace_root = self._workspace_root_for(save_dir)
        return self.workspace_root

    def _require_workspace(self) -> Path:
        save_dir = self._configured_save_dir()
        try:
            self._probe_workspace(save_dir, create=False)
        except ValueError as exc:
            self.workspace_available = False
            self.workspace_error = str(exc)
            raise RuntimeError(f"工作区不可用：{exc}") from exc
        self.workspace_available = True
        self.workspace_error = ""
        return save_dir

    def _workspace_is_available(self) -> bool:
        save_dir = self.save_dir
        available = bool(
            save_dir is not None
            and self.workspace_available
            and save_dir.is_dir()
            and os.access(save_dir, os.W_OK)
        )
        if self.workspace_available and not available:
            self.workspace_available = False
            self.workspace_error = "工作区目录不存在或不可写，请重新选择目录"
        return available

    def _workspace_root_for(self, path: Path) -> Path:
        """Resolve a batch directory back to its selected workspace root."""
        resolved = path.resolve()
        marker = self.history.marker_info(resolved)
        if marker.get("kind") != BATCH_KIND:
            return resolved
        parent = resolved.parent
        root_marker = self.history.marker_info(parent)
        root_id = str(marker.get("workspace_root_id") or "")
        if root_marker.get("kind") == WORKSPACE_KIND and (
            not root_id or root_marker.get("workspace_id") == root_id
        ):
            return parent.resolve()
        return resolved

    def _batch_metadata(self) -> dict[str, str]:
        if self.save_dir is None:
            return {"batch_id": "", "batch_label": ""}
        marker = self.history.marker_info(self.save_dir)
        kind = str(marker.get("kind") or WORKSPACE_KIND)
        return {
            "batch_id": str(marker.get("workspace_id") or "")
            if kind == BATCH_KIND else "",
            "batch_label": str(
                marker.get("label") or self.save_dir.name
            ) if kind == BATCH_KIND else "",
        }

    @staticmethod
    def _safe_filename(value: str) -> str:
        cleaned = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", value.strip())
        cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
        return cleaned or "sample"

    @staticmethod
    def _concentration_token(value: Any) -> str:
        if value in (None, ""):
            return "unknown"
        numeric = float(value)
        return "unknown" if not math.isfinite(numeric) else f"{numeric:g}uM"

    def _workspace_paths(self) -> dict[str, Path]:
        save_dir = self._configured_save_dir()
        return {
            "points": save_dir / "calibration-points.csv",
            "model": save_dir / "calibration-model.json",
            "settings": save_dir / "calibration-settings.json",
            "plateau": save_dir / "calibration-plateau.json",
            "filter": save_dir / "calibration-filter.json",
            "selection": save_dir / "calibration-selection.json",
            "validation": save_dir / "calibration-validation.json",
            "index": save_dir / "measurement-index.csv",
            "drift": save_dir / "calibration-drift.json",
            "runtime": save_dir / "workspace-state.json",
        }

    def _sync_points(self) -> None:
        self.points = [
            CalibrationPoint(
                float(record["concentration_um"]),
                float(record["current_nA"]),
                str(record.get("label", "")),
            )
            for record in self.point_records
        ]

    def _points_revision_locked(self) -> str:
        """Identify the persisted candidate set while ``self.lock`` is held."""
        encoded = json.dumps(
            _json_safe(self.point_records),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _filter_signature(config: dict[str, Any] | None) -> dict[str, Any]:
        """Normalize a filter config for legacy workspace metadata.

        Filter settings are retained for audit/history only.  They are not a
        compatibility gate: calibration candidates may intentionally combine
        different display or analysis filters.
        """
        normalized = validate_filter_config(config or FILTER_DEFAULTS)
        return normalized if normalized["mode"] == "analysis" else {"mode": "off"}

    @staticmethod
    def _uses_plateau_protocol(settings: dict[str, Any] | None) -> bool:
        return bool(
            settings
            and settings.get("method") == "it"
            and settings.get("adaptive_stop")
        )

    @staticmethod
    def _plateau_signature(
        config: PlateauConfig | dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        if config is None:
            return None
        return PlateauConfig.validate(config).to_dict()

    @classmethod
    def _same_calibration_protocol(
        cls,
        first_settings: dict[str, Any],
        first_plateau: dict[str, Any] | None,
        second_settings: dict[str, Any],
        second_plateau: PlateauConfig | dict[str, Any] | None,
    ) -> bool:
        if not SettingsController.same_analysis_protocol(
            first_settings, second_settings
        ):
            return False
        if not (
            cls._uses_plateau_protocol(first_settings)
            or cls._uses_plateau_protocol(second_settings)
        ):
            return True
        try:
            normalized_first = cls._plateau_signature(first_plateau)
            normalized_second = cls._plateau_signature(second_plateau)
        except (TypeError, ValueError):
            return False
        return (
            normalized_first is not None
            and normalized_second is not None
            and normalized_first == normalized_second
        )

    @classmethod
    def _run_plateau_signature(
        cls, run: dict[str, Any], settings: dict[str, Any]
    ) -> dict[str, Any] | None:
        if not cls._uses_plateau_protocol(settings):
            return None
        summary = run.get("summary")
        adaptive = summary.get("adaptive_stop") if isinstance(summary, dict) else None
        config = adaptive.get("config") if isinstance(adaptive, dict) else None
        signature = cls._plateau_signature(config)
        if signature is None:
            raise ValueError(
                "自动停止标定缺少平台参数元数据，本次结果不能加入标定点"
            )
        return signature

    def _load_workspace(self, *, create: bool = True) -> None:
        save_dir = self._configured_save_dir()
        try:
            self._probe_workspace(save_dir, create=create)
        except ValueError as exc:
            self.workspace_available = False
            self.workspace_error = str(exc)
            raise
        if self.workspace_root is None or not (
            self.workspace_root == save_dir
            or save_dir.parent == self.workspace_root
        ):
            self.workspace_root = self._workspace_root_for(save_dir)
        self.workspace_available = True
        self.workspace_error = ""
        paths = self._workspace_paths()
        with self.lock:
            self.points = []
            self.model = None
            self.model_path = None
            self.model_settings = None
            self.model_plateau = None
            self.calibration_settings = None
            self.calibration_plateau = None
            self.calibration_filter = None
            self.point_records = []
            self.selected_point_ids = []
            self.model_created_at = None
            self.validation_started_at = None
            self.records = []
            self.validation_overrides = {}
            self.manual_validation_points = []
            self.deleted_validation_point_ids = set()
            self.drift = self._empty_drift()
            self.workspace_runtime_settings = None
            self.workspace_runtime_filter = None
            self.workspace_runtime_plateau = None
            self.latest_workflow_result = None
            if paths["points"].exists():
                try:
                    with paths["points"].open(
                        newline="", encoding="utf-8", errors="replace"
                    ) as handle:
                        for index, row in enumerate(csv.DictReader(handle), 1):
                            point_id = str(row.get("point_id") or f"point-{index:04d}")
                            if any(item["point_id"] == point_id for item in self.point_records):
                                point_id = f"{point_id}-{index}"
                            self.point_records.append({
                                "point_id": point_id,
                                "acquired_at": float(row.get("acquired_at") or 0),
                                "run_id": str(row.get("run_id") or ""),
                                "label": str(row.get("label") or ""),
                                "concentration_um": float(row["concentration_um"]),
                                "current_nA": float(row["current_nA"]),
                                "data_path": str(row.get("data_path") or ""),
                            })
                    self._sync_points()
                except (OSError, ValueError, KeyError):
                    self.point_records = []
                    self.points = []
            if paths["settings"].exists():
                try:
                    saved_calibration_settings = json.loads(
                        paths["settings"].read_text(encoding="utf-8")
                    )
                    # Before these controls existed, firmware always stepped from 0 V
                    # immediately. Preserve that historical protocol instead of
                    # silently assigning today's defaults to legacy calibration data.
                    saved_calibration_settings.setdefault("initial_potential_v", 0.0)
                    saved_calibration_settings.setdefault("prestep_s", 0.0)
                    self.calibration_settings = SettingsController.validate(
                        saved_calibration_settings
                    )
                except (OSError, ValueError, TypeError, json.JSONDecodeError):
                    self.calibration_settings = None
            if (
                self._uses_plateau_protocol(self.calibration_settings)
                and paths["plateau"].exists()
            ):
                try:
                    saved_plateau = json.loads(
                        paths["plateau"].read_text(encoding="utf-8")
                    )
                    raw_plateau = (
                        saved_plateau.get("settings", saved_plateau)
                        if isinstance(saved_plateau, dict) else None
                    )
                    self.calibration_plateau = self._plateau_signature(raw_plateau)
                except (OSError, ValueError, TypeError, json.JSONDecodeError):
                    self.calibration_plateau = None
            if paths["filter"].exists():
                try:
                    saved_filter = json.loads(
                        paths["filter"].read_text(encoding="utf-8")
                    )
                    self.calibration_filter = validate_filter_config(
                        saved_filter.get("settings", saved_filter)
                        if isinstance(saved_filter, dict) else None
                    )
                except (OSError, ValueError, TypeError, json.JSONDecodeError):
                    self.calibration_filter = None
            if paths["model"].exists() and self.calibration_settings is not None:
                try:
                    self.model = load_model(paths["model"])
                    self.model_path = paths["model"]
                    self.model_settings = dict(self.calibration_settings)
                    self.model_plateau = (
                        dict(self.calibration_plateau)
                        if self.calibration_plateau is not None else None
                    )
                except (OSError, ValueError, json.JSONDecodeError):
                    self.model = None
            if self.model is not None:
                if paths["selection"].exists():
                    try:
                        selection = json.loads(
                            paths["selection"].read_text(encoding="utf-8")
                        )
                        known_ids = {record["point_id"] for record in self.point_records}
                        self.selected_point_ids = [
                            str(point_id) for point_id in selection.get("selected_point_ids", [])
                            if str(point_id) in known_ids
                        ]
                        raw_created_at = selection.get("created_at")
                        self.model_created_at = (
                            float(raw_created_at) if raw_created_at is not None else None
                        )
                        raw_validation_started_at = selection.get(
                            "validation_started_at", raw_created_at
                        )
                        self.validation_started_at = (
                            float(raw_validation_started_at)
                            if raw_validation_started_at is not None else None
                        )
                    except (OSError, ValueError, TypeError, json.JSONDecodeError):
                        self.selected_point_ids = []
                        self.model_created_at = None
                        self.validation_started_at = None
                if not self.selected_point_ids:
                    # Legacy models used every saved point. Preserve that model's scope.
                    self.selected_point_ids = [
                        record["point_id"] for record in self.point_records
                    ]
            if paths["index"].exists():
                try:
                    with paths["index"].open(
                        newline="", encoding="utf-8"
                    ) as handle:
                        self.records = list(csv.DictReader(handle))
                except OSError:
                    self.records = []
            if paths["drift"].exists():
                try:
                    saved_drift = json.loads(
                        paths["drift"].read_text(encoding="utf-8")
                    )
                    if isinstance(saved_drift, dict):
                        self.drift = {**self._empty_drift(), **saved_drift}
                except (OSError, ValueError, TypeError, json.JSONDecodeError):
                    self.drift = self._empty_drift()
            if paths["validation"].exists():
                try:
                    saved_validation = json.loads(
                        paths["validation"].read_text(encoding="utf-8")
                    )
                    raw_points = (saved_validation.get("points", saved_validation)
                                  if isinstance(saved_validation, dict) else {})
                    if isinstance(raw_points, dict):
                        self.validation_overrides = {
                            str(point_id): dict(values)
                            for point_id, values in raw_points.items()
                            if isinstance(values, dict)
                        }
                    raw_manual = saved_validation.get("manual_points", [])
                    if isinstance(raw_manual, list):
                        self.manual_validation_points = [
                            dict(point) for point in raw_manual
                            if isinstance(point, dict)
                        ]
                    raw_deleted = saved_validation.get("deleted_point_ids", [])
                    if isinstance(raw_deleted, list):
                        self.deleted_validation_point_ids = {
                            str(point_id) for point_id in raw_deleted
                            if str(point_id).strip()
                        }
                except (OSError, ValueError, TypeError, json.JSONDecodeError):
                    self.validation_overrides = {}
                    self.manual_validation_points = []
                    self.deleted_validation_point_ids = set()

            if paths["runtime"].exists():
                try:
                    runtime = json.loads(paths["runtime"].read_text(encoding="utf-8"))
                    if not isinstance(runtime, dict):
                        raise ValueError("workspace state must be an object")
                    self.workspace_runtime_settings = SettingsController.validate(
                        runtime.get("settings", {})
                    )
                    self.workspace_runtime_filter = validate_filter_config(
                        runtime.get("filter", {})
                    )
                    self.workspace_runtime_plateau = PlateauConfig.validate(
                        runtime.get("plateau")
                    ).to_dict()
                except (OSError, ValueError, TypeError, json.JSONDecodeError):
                    self.workspace_runtime_settings = None
                    self.workspace_runtime_filter = None
                    self.workspace_runtime_plateau = None

    _WORKSPACE_STATE_FIELDS = (
        "save_dir", "workspace_root", "workspace_available", "workspace_error",
        "model", "model_path", "model_settings", "model_plateau",
        "calibration_filter", "calibration_settings", "calibration_plateau",
        "points", "point_records", "selected_point_ids", "model_created_at",
        "validation_started_at", "records", "validation_overrides",
        "manual_validation_points", "deleted_validation_point_ids", "drift",
        "workspace_runtime_settings", "workspace_runtime_filter",
        "workspace_runtime_plateau", "latest_workflow_result",
    )

    @staticmethod
    def _atomic_json_file(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", prefix=f".{path.name}.",
                suffix=".tmp", dir=path.parent, delete=False,
            ) as handle:
                temporary_path = Path(handle.name)
                json.dump(payload, handle, indent=2, ensure_ascii=False)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, path)
            temporary_path = None
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    def _workspace_memory_snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                name: copy.deepcopy(getattr(self, name))
                for name in self._WORKSPACE_STATE_FIELDS
            }

    def _restore_workspace_memory(self, snapshot: dict[str, Any]) -> None:
        with self.lock:
            for name, value in snapshot.items():
                setattr(self, name, value)

    def _controller_snapshot(self) -> dict[str, Any]:
        return {
            "settings": self.settings.snapshot(),
            "filter": self.filter.snapshot()["settings"],
            "plateau": self.plateau.snapshot()["settings"],
        }

    def _restore_controllers(self, snapshot: dict[str, Any]) -> None:
        settings_snapshot = snapshot["settings"]
        with self.settings.lock:
            self.settings.settings = dict(settings_snapshot["settings"])
            self.settings.applied = bool(settings_snapshot["applied"])
            self.settings.state = str(settings_snapshot["state"])
            self.settings.message = str(settings_snapshot["message"])
            self.settings.error = str(settings_snapshot.get("error") or "")
        with self.filter.lock:
            self.filter.settings = validate_filter_config(snapshot["filter"])
        plateau = PlateauConfig.validate(snapshot["plateau"])
        with self.plateau.lock:
            self.plateau.settings = plateau
        with self.measurement.lock:
            self.measurement.settings = dict(self.settings.settings)
            self.measurement.filter_config = dict(self.filter.settings)
            self.measurement.set_plateau_config(plateau)
            self.measurement._reset_live_analysis_locked()
        self.schedule.set_plateau_config(plateau)

    def _latest_record_settings(self) -> dict[str, Any] | None:
        for record in reversed(self.records):
            try:
                raw = json.loads(str(record.get("measurement_settings_json") or ""))
                if isinstance(raw, dict):
                    return SettingsController.validate(raw)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
        return None

    def _activate_workspace_configuration(self) -> None:
        settings = (
            self.workspace_runtime_settings
            or self.calibration_settings
            or self._latest_record_settings()
        )
        filter_settings = self.workspace_runtime_filter or self.calibration_filter
        plateau_settings = self.workspace_runtime_plateau or self.calibration_plateau
        if settings is not None:
            with self.settings.lock:
                self.settings.settings = SettingsController.validate(settings)
                self.settings.applied = False
                self.settings.state = "not_applied"
                self.settings.message = "已恢复历史条件，继续测量前请重新应用到硬件"
                self.settings.error = ""
        if filter_settings is not None:
            with self.filter.lock:
                self.filter.settings = validate_filter_config(filter_settings)
        if plateau_settings is not None:
            with self.plateau.lock:
                self.plateau.settings = PlateauConfig.validate(plateau_settings)
        with self.measurement.lock:
            self.measurement.settings = dict(self.settings.settings)
            self.measurement.filter_config = dict(self.filter.settings)
            self.measurement.set_plateau_config(self.plateau.settings)
            self.measurement._reset_live_analysis_locked()
        self.schedule.set_plateau_config(self.plateau.settings)

    @staticmethod
    def _probe_workspace(path: Path, create: bool) -> None:
        try:
            if create:
                path.mkdir(parents=True, exist_ok=True)
            if not path.is_dir():
                raise OSError("目录不存在")
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", prefix=".sensus-write-test-",
                dir=path, delete=True,
            ) as probe:
                probe.write("ok")
                probe.flush()
        except OSError as exc:
            raise ValueError(f"保存目录不可写：{exc}") from exc

    def _switch_workspace(
        self,
        path: Path,
        *,
        create: bool,
        restore: bool,
        workspace_root: Path | None = None,
    ) -> None:
        if self.measurement.is_busy() or self.schedule.snapshot()["active"]:
            raise RuntimeError("测量或自动任务运行期间不能切换工作目录")
        self._probe_workspace(path, create)
        memory_before = self._workspace_memory_snapshot()
        controllers_before = self._controller_snapshot()
        workflow_before = WORKFLOW_PATH.read_bytes() if WORKFLOW_PATH.exists() else None
        try:
            self.save_dir = path.resolve()
            self.workspace_root = (
                workspace_root.resolve()
                if workspace_root is not None
                else self._workspace_root_for(self.save_dir)
            )
            self._load_workspace(create=False)
            if restore:
                self._activate_workspace_configuration()
            self._atomic_json_file(WORKFLOW_PATH, {
                "save_dir": str(self.save_dir),
                "workspace_root": str(self.workspace_root),
            })
        except Exception:
            self._restore_workspace_memory(memory_before)
            self._restore_controllers(controllers_before)
            try:
                if workflow_before is None:
                    WORKFLOW_PATH.unlink(missing_ok=True)
                else:
                    WORKFLOW_PATH.parent.mkdir(parents=True, exist_ok=True)
                    temporary: Path | None = None
                    try:
                        with tempfile.NamedTemporaryFile(
                            mode="wb", prefix=f".{WORKFLOW_PATH.name}.", suffix=".tmp",
                            dir=WORKFLOW_PATH.parent, delete=False,
                        ) as handle:
                            temporary = Path(handle.name)
                            handle.write(workflow_before)
                            handle.flush()
                            os.fsync(handle.fileno())
                        os.replace(temporary, WORKFLOW_PATH)
                        temporary = None
                    finally:
                        if temporary is not None:
                            temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    def _new_batch_path(self, root: Path, label: str) -> Path:
        stamp = time.strftime("%Y%m%d_%H%M%S")
        name = self._safe_filename(label) if label.strip() else f"batch-{stamp}"
        candidate = root / name
        suffix = 2
        while candidate.exists():
            candidate = root / f"{name}-{suffix}"
            suffix += 1
        return candidate

    def _start_batch(self, root: Path, label: str) -> None:
        self._probe_workspace(root, create=True)
        self.history.register(
            root, WorkspaceHistory.summarize(root), root.name,
            kind=WORKSPACE_KIND,
        )
        batch_path = self._new_batch_path(root, label)
        self._switch_workspace(
            batch_path, create=True, restore=False, workspace_root=root,
        )
        display_label = label.strip() or f"批次 {batch_path.name}"
        self.history.register_batch(
            batch_path, root, self._history_summary(), display_label,
        )

    def configure_workflow(self, payload: dict[str, Any]) -> dict[str, Any]:
        root = self._resolve_save_dir(str(payload.get("save_dir", "")))
        label = str(payload.get("batch_name") or "").strip()
        with self.operation_lock:
            self._start_batch(root, label)
            self._refresh_history_best_effort()
        return self.workflow_snapshot()

    def workflow_snapshot(self) -> dict[str, Any]:
        workspace_available = self._workspace_is_available()
        current_settings = self.settings.snapshot()["settings"]
        current_plateau = self.plateau.settings
        is_it = current_settings.get("method", "it") == "it"
        settings_match = (
            self.calibration_settings is None
            or self._same_calibration_protocol(
                self.calibration_settings,
                self.calibration_plateau,
                current_settings,
                current_plateau,
            )
        )
        calibration_ready = (
            is_it
            and self.model is not None
            and self.model_settings is not None
            and self._same_calibration_protocol(
                self.model_settings,
                self.model_plateau,
                current_settings,
                current_plateau,
            )
        )
        distinct = len({float(point.concentration_um) for point in self.points})
        stage = "collect"
        if self.points and not calibration_ready:
            stage = "select"
        if calibration_ready:
            stage = "test"
        schedule_state = self.schedule.snapshot()
        if schedule_state["active"] and schedule_state["sample_role"] == "stabilization":
            stage = "stabilization"
        batch = self._batch_metadata()
        return _json_safe({
            "save_dir": str(self.save_dir) if self.save_dir is not None else "",
            "workspace_root": (
                str(self.workspace_root) if self.workspace_root is not None else ""
            ),
            "workspace_configured": self.save_dir is not None,
            "workspace_available": workspace_available,
            "workspace_error": self.workspace_error,
            **batch,
            "calibration_ready": calibration_ready,
            "settings_match": settings_match,
            "stage": stage,
            "points_count": len(self.points),
            "selected_points_count": len(self.selected_point_ids),
            "distinct_concentrations": distinct,
            "model_path": str(self.model_path) if self.model_path else "",
            "model_created_at": self.model_created_at,
            "latest_result": self.latest_workflow_result,
            "records": list(reversed(self.records[-20:])),
        })

    def _persist_workspace_runtime(self) -> None:
        settings = self.settings.snapshot()["settings"]
        filter_settings = self.filter.snapshot()["settings"]
        plateau = self.plateau.snapshot()["settings"]
        self._atomic_json_file(self._workspace_paths()["runtime"], {
            "version": 1,
            "settings": settings,
            "filter": filter_settings,
            "plateau": plateau,
            "saved_at": time.time(),
        })
        with self.lock:
            self.workspace_runtime_settings = dict(settings)
            self.workspace_runtime_filter = dict(filter_settings)
            self.workspace_runtime_plateau = dict(plateau)

    def _history_summary(self) -> dict[str, Any]:
        with self.lock:
            completed = [row for row in self.records if row.get("state") == "completed"]
            roles = {
                role: sum(row.get("sample_role") == role for row in completed)
                for role in ("calibration", "test", "stabilization", "cv")
            }
            latest = completed[-1] if completed else None
            latest_at = 0.0
            if latest is not None:
                try:
                    latest_at = float(latest.get("finished_at") or 0)
                except (TypeError, ValueError):
                    latest_at = 0.0
            settings = (
                self.workspace_runtime_settings
                or self.calibration_settings
                or self._latest_record_settings()
                or {}
            )
            return _json_safe({
                "points_count": len(self.point_records),
                "selected_points_count": len(self.selected_point_ids),
                "records_count": len(self.records),
                "completed_count": len(completed),
                "calibration_count": roles["calibration"],
                "test_count": roles["test"],
                "stabilization_count": roles["stabilization"],
                "cv_count": roles["cv"],
                "has_model": self.model is not None,
                "model_r2": self.model.r2 if self.model is not None else None,
                "model_created_at": self.model_created_at,
                "method": settings.get("method", "it"),
                "latest_result_at": latest_at or None,
                "latest_sample_name": str((latest or {}).get("sample_name") or ""),
                "latest_sample_role": str((latest or {}).get("sample_role") or ""),
            })

    def register_history(
        self, payload: dict[str, Any] | None = None, *, allow_busy: bool = False,
    ) -> dict[str, Any]:
        payload = payload or {}
        if not allow_busy and (
            self.measurement.is_busy() or self.schedule.snapshot()["active"]
        ):
            raise RuntimeError("测量或自动任务运行期间不能登记工作区")
        with self.operation_lock:
            save_dir = self._require_workspace()
            self._persist_workspace_runtime()
            marker = self.history.marker_info(save_dir)
            kind = str(marker.get("kind") or WORKSPACE_KIND)
            root_id = str(marker.get("workspace_root_id") or "")
            label = str(payload.get("label") or marker.get("label") or "")
            return self.history.register(
                save_dir,
                self._history_summary(),
                label,
                kind=kind,
                workspace_root_id=root_id,
            )

    def import_history(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Register an existing local data directory without switching into it."""
        payload = payload or {}
        raw_path = str(payload.get("path") or "").strip()
        if not raw_path:
            raise ValueError("请填写历史数据目录")
        path = self._resolve_save_dir(raw_path)
        status, detail = WorkspaceHistory._health(path)
        if status == "missing":
            raise ValueError(detail)
        if status != "available":
            raise ValueError(detail or "历史工作区不可用")
        label = str(payload.get("label") or path.name or "未命名工作区")
        with self.operation_lock:
            marker = self.history.marker_info(path)
            self.history.register(
                path,
                WorkspaceHistory.summarize(path),
                label,
                create_marker=False,
                kind=str(marker.get("kind") or WORKSPACE_KIND),
                workspace_root_id=str(marker.get("workspace_root_id") or ""),
            )
        return self.history_snapshot()

    def _refresh_history_best_effort(self) -> None:
        try:
            self.register_history(allow_busy=True)
        except (OSError, RuntimeError, TypeError, ValueError):
            pass

    def _discover_workspace_batches(self) -> None:
        """Register batch directories copied or migrated into the active root."""
        if not self._workspace_is_available():
            return
        save_dir = self._configured_save_dir()
        root = self._configured_workspace_root().resolve()
        root_marker = self.history.marker_info(root)
        root_id = str(root_marker.get("workspace_id") or "")
        if root_marker.get("kind", WORKSPACE_KIND) != WORKSPACE_KIND or not root_id:
            return
        known_ids = {
            str(entry.get("workspace_id") or "")
            for entry in self.history.list(save_dir).get("entries", [])
        }
        if root_id not in known_ids:
            self.history.register(
                root,
                WorkspaceHistory.summarize(root),
                str(root_marker.get("label") or root.name),
                create_marker=False,
                kind=WORKSPACE_KIND,
            )
            known_ids.add(root_id)
        try:
            children = list(root.iterdir())
        except OSError:
            return
        for child in children:
            if not child.is_dir():
                continue
            marker = self.history.marker_info(child)
            batch_id = str(marker.get("workspace_id") or "")
            if (
                marker.get("kind") != BATCH_KIND
                or marker.get("workspace_root_id") != root_id
                or not batch_id
                or batch_id in known_ids
            ):
                continue
            self.history.register(
                child,
                WorkspaceHistory.summarize(child),
                str(marker.get("label") or child.name),
                create_marker=False,
                kind=BATCH_KIND,
                workspace_root_id=root_id,
            )
            known_ids.add(batch_id)

    def history_snapshot(self) -> dict[str, Any]:
        if not self._workspace_is_available():
            return {
                "entries": [],
                "workspace_root": (
                    str(self.workspace_root) if self.workspace_root is not None else ""
                ),
                "active_workspace_id": "",
                "workspaces": [],
                "batches": [],
                "current_batches": [],
                "registry_error": self.workspace_error,
            }
        save_dir = self._configured_save_dir()
        workspace_root = self._configured_workspace_root()
        self._discover_workspace_batches()
        snapshot = self.history.list(save_dir)
        root_marker = self.history.marker_info(workspace_root)
        root_id = str(root_marker.get("workspace_id") or "")
        entries = snapshot.get("entries", [])
        snapshot.update({
            "workspace_root": str(workspace_root),
            "active_workspace_id": root_id,
            "workspaces": [
                entry for entry in entries
                if entry.get("kind", WORKSPACE_KIND) != BATCH_KIND
            ],
            "batches": [
                entry for entry in entries
                if entry.get("kind") == BATCH_KIND
            ],
            "current_batches": [
                entry for entry in entries
                if entry.get("kind") == BATCH_KIND
                and entry.get("workspace_root_id") == root_id
            ],
        })
        return snapshot

    @staticmethod
    def _downsample_curve(values: Any, maximum: int = 3000) -> list[Any]:
        count = len(values)
        if count <= maximum:
            return values.tolist() if hasattr(values, "tolist") else list(values)
        step = max(1, math.ceil((count - 1) / (maximum - 1)))
        indexes = list(range(0, count, step))
        if indexes[-1] != count - 1:
            indexes.append(count - 1)
        return [values[index].item() if hasattr(values[index], "item") else values[index]
                for index in indexes]

    def _curve_from_record(
        self, record: dict[str, Any], *, maximum_points: int = 3000,
    ) -> dict[str, Any] | None:
        if not self._workspace_is_available():
            return None
        save_dir = self._configured_save_dir()
        raw_path = str(record.get("data_path") or "").strip()
        if not raw_path:
            return None
        path = Path(raw_path).resolve()
        try:
            path.relative_to(save_dir.resolve())
        except ValueError:
            return None
        if not path.is_file():
            return None
        settings: dict[str, Any] = {}
        try:
            decoded = json.loads(str(record.get("measurement_settings_json") or ""))
            if isinstance(decoded, dict):
                settings = decoded
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
        method = str(settings.get("method") or "it")
        try:
            if method == "cv":
                data = load_cv_run(path)
                return {
                    "method": "cv",
                    "time_s": self._downsample_curve(data["time_s"], maximum_points),
                    "potential_v": self._downsample_curve(
                        data["potential_v"], maximum_points
                    ),
                    "current_nA": self._downsample_curve(
                        data["current_nA"], maximum_points
                    ),
                    "cycle": self._downsample_curve(data["cycle"], maximum_points),
                    "valid": self._downsample_curve(data["valid"], maximum_points),
                }
            time_s, current_nA, valid = load_run_csv(path)
            return {
                "method": "it",
                "time_s": self._downsample_curve(time_s, maximum_points),
                "current_nA": self._downsample_curve(current_nA, maximum_points),
                "valid": self._downsample_curve(valid, maximum_points),
            }
        except (OSError, ValueError, csv.Error):
            return None

    def history_curves_snapshot(self) -> dict[str, Any]:
        with self.lock:
            curves = []
            for record in self.records:
                if record.get("state") != "completed" or not record.get("data_path"):
                    continue
                curves.append({
                    "run_id": str(record.get("run_id") or ""),
                    "sample_name": str(record.get("sample_name") or "未命名样品"),
                    "sample_role": str(record.get("sample_role") or ""),
                    "finished_at": record.get("finished_at"),
                    "steady_current_nA": record.get("steady_current_nA"),
                    "measurement_settings_json": record.get("measurement_settings_json", ""),
                })
        return {"curves": list(reversed(curves[-80:]))}

    def load_history_curves(self, payload: dict[str, Any]) -> dict[str, Any]:
        requested = payload.get("run_ids", [])
        if not isinstance(requested, list):
            raise ValueError("历史曲线必须是数组")
        requested_ids = [str(value) for value in requested if str(value)]
        if len(requested_ids) > 80:
            raise ValueError("一次最多叠加 80 条历史曲线")
        # Keep the aggregate canvas/JSON workload bounded when "select all"
        # loads a large batch. Up to 12 curves retain the previous 3000-point
        # detail; larger selections share the same 36k-point budget.
        maximum_points = min(
            3000, max(300, 36_000 // max(1, len(requested_ids)))
        )
        with self.lock:
            records_by_id = {
                str(record.get("run_id") or ""): dict(record)
                for record in self.records if record.get("state") == "completed"
            }
        curves: list[dict[str, Any]] = []
        for run_id in requested_ids:
            record = records_by_id.get(run_id)
            if record is None:
                raise ValueError("历史曲线不属于当前批次")
            curve = self._curve_from_record(record, maximum_points=maximum_points)
            if curve is not None:
                curves.append({
                    "run_id": run_id,
                    "sample_name": str(record.get("sample_name") or run_id),
                    "sample_role": str(record.get("sample_role") or ""),
                    "finished_at": record.get("finished_at"),
                    **curve,
                })
        return {"curves": curves}

    def open_history(self, payload: dict[str, Any]) -> dict[str, Any]:
        workspace_id = str(payload.get("workspace_id") or "")
        if not workspace_id:
            raise ValueError("请指定历史记录")
        if payload.get("unsaved_changes") and not payload.get("discard_unsaved"):
            raise RuntimeError("当前页面有未保存编辑，请先保存或确认放弃")
        with self.operation_lock:
            _, path = self.history.resolve(workspace_id)
            self._switch_workspace(path, create=False, restore=True)
        preview = None
        for record in reversed(self.records):
            if record.get("state") == "completed":
                curve = self._curve_from_record(record)
                if curve is not None:
                    preview = {"run_id": str(record.get("run_id") or ""),
                               "sample_name": str(record.get("sample_name") or ""),
                               **curve}
                    break
        return {
            "workflow": self.workflow_snapshot(),
            "calibration": self.model_payload(),
            "settings": self.settings.snapshot(),
            "filter": self.filter.snapshot(),
            "plateau": self.plateau.snapshot(),
            "history": self.history_snapshot(),
            "measurement_preview": preview,
        }

    def favorite_history(self, payload: dict[str, Any]) -> dict[str, Any]:
        workspace_id = str(payload.get("workspace_id") or "")
        if not workspace_id:
            raise ValueError("请指定历史记录")
        favorite = payload.get("favorite")
        return self.history.favorite(
            workspace_id, None if favorite is None else bool(favorite)
        )

    def remove_history(self, payload: dict[str, Any]) -> dict[str, Any]:
        workspace_id = str(payload.get("workspace_id") or "")
        if not workspace_id:
            raise ValueError("请指定历史记录")
        self.history.remove(workspace_id)
        return self.history_snapshot()

    def start_measurement(self, payload: dict[str, Any]) -> dict[str, Any]:
        save_dir = self._require_workspace()
        sample_name = str(payload.get("sample_name", "")).strip()
        if not sample_name:
            raise ValueError("请填写样品名称")
        current_settings = self.settings.snapshot()["settings"]
        role = (
            "cv" if current_settings.get("method") == "cv"
            else str(payload.get("sample_role", "calibration"))
        )
        if role not in {"calibration", "stabilization", "test", "cv"}:
            raise ValueError("样品类型必须是标定、稳定化、测试或 CV")
        raw_concentration = payload.get("known_concentration_um")
        concentration = (
            None if raw_concentration in (None, "") else float(raw_concentration)
        )
        if concentration is not None and not math.isfinite(concentration):
            raise ValueError("浓度必须是有限数字")
        if concentration is not None and concentration < 0:
            raise ValueError("浓度不能为负数")
        if role == "calibration" and concentration is None:
            raise ValueError("标定样品必须填写已知浓度")
        if (
            role == "calibration"
            and self.points
            and not self.workflow_snapshot()["settings_match"]
        ):
            raise RuntimeError("当前 IT 条件与该目录中的标定点不同，请选择新的保存目录")
        if role in {"stabilization", "test"} and not self.workflow_snapshot()["calibration_ready"]:
            raise RuntimeError("请先选择标定点并生成当前 IT 条件下的测试曲线")
        metadata = {
            **payload,
            "sample_name": sample_name,
            "known_concentration_um": concentration,
            "sample_role": role,
            "save_dir": str(save_dir),
            "source": payload.get("source") or "manual_gui",
        }
        metadata = self._prepare_export_metadata(metadata)
        return self.measurement.start_verified(
            metadata=metadata, settings=current_settings,
            filter_config=self.filter.snapshot()["settings"],
            plateau_config=self.plateau.settings,
        )

    def start_schedule(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self.operation_lock:
            save_dir = self._require_workspace()
            if not self.settings.snapshot()["applied"]:
                raise RuntimeError("请先将当前检测条件应用到硬件")
            if HARDWARE_TRANSPORT_REQUESTED != "rtt" or _selected_device_copy() is not None:
                _refresh_usb_transport()
            if HARDWARE_TRANSPORT == "rtt":
                _require_jlink_target(JLINK_SERIAL)
            role = str(payload.get("sample_role") or "test")
            workflow = self.workflow_snapshot()
            is_it = self.settings.snapshot()["settings"].get("method") == "it"
            if (
                is_it
                and role in {"stabilization", "test"}
                and not workflow["calibration_ready"]
            ):
                raise RuntimeError("请先选择标定点并生成测试曲线")
            if (
                role == "calibration"
                and workflow["points_count"]
                and not workflow["settings_match"]
            ):
                raise RuntimeError("当前 IT 条件与已有标定点不同，请新建标定")
            prepared = {
                **payload,
                "settings": self.settings.snapshot()["settings"],
                "save_dir": str(save_dir),
            }
            self.schedule.set_filter_config(self.filter.snapshot()["settings"])
            self.schedule.set_plateau_config(self.plateau.settings)
            return self.schedule.start(prepared)

    def start_debug_run(self, payload: dict[str, Any]) -> dict[str, Any]:
        """硬件 DEBUG 模式的「一次 I-t 测量」。

        与正式测量的差别只有三点,其余完全共用同一条已验证的 RTT/J-Link 路径:
          ① metadata 打 `debug` 标记 ⇒ 收尾时不进标定工作区(见 _measurement_completed)
          ② **不传 live_raw_path** ⇒ raw 留在 run_dir/raw.csv,不写进保存目录
          ③ 不校验样品名/浓度/标定就绪 —— DEBUG 页刻意不暴露那套工作流

        🔴 但两个门禁必须保留:自动测量运行期间不许插队(会抢探头),
        已有测量在跑时不许再起(同上)。这两条与正式测量同口径。
        """
        self._require_workspace()
        if self.schedule.snapshot()["active"]:
            raise RuntimeError("自动测量运行期间不能起硬件 DEBUG 轮(探头只有一支)")
        if self.measurement.is_busy():
            raise RuntimeError("已有测量正在运行")
        # 🔴 刻意**不**要求 settings.applied。
        #   正式测量要求它,是因为标定/拟合假设固件的 E 与时长跟 settings 一致;
        #   debug 轮什么都不导出、不拟合,这个耦合不存在。
        #   反过来,"固件与 GUI 设置不一致"恰恰是硬件调试时的常态 —— 要求先
        #   重编译+烧录才能看一眼硬件,等于取消了这个页面的用途。
        #   而且现在固件开机就打 CFG_BOOT/CFG_DERIVED 给出**真实生效**的配置,
        #   那比 applied 这个上位机侧的标志是更硬的证据。不一致由 UI 提示,不拦。
        metadata = {
            "debug": True,
            "sample_name": str(payload.get("note") or "hw-debug"),
            "sample_role": "test",
            "source": "debug_gui",
        }
        probe_only = bool(payload.get("probe_only"))
        current_settings = self.settings.snapshot()["settings"]
        if current_settings.get("method") == "cv":
            # CV validation aliases its low potential and quiet time into the
            # generic acquisition fields. Those values are not valid IT
            # settings, so Debug needs a clean IT timing/potential baseline.
            current_settings = {
                **current_settings,
                **{
                    key: SettingsController.DEFAULTS[key]
                    for key in (
                        "initial_potential_v", "potential_v", "prestep_s",
                        "duration_s", "fit_window_s", "adaptive_stop",
                    )
                },
            }
        debug_settings = SettingsController.validate({
            **current_settings,
            "method": "it",
        })
        return self.measurement.start(
            metadata=metadata,
            settings=debug_settings,
            filter_config=self.filter.snapshot()["settings"],
            plateau_config=self.plateau.settings,
            trigger="ARMED" if probe_only else "FRESH_START",
        )

    def _prepare_export_metadata(self, metadata: dict[str, Any]) -> dict[str, Any]:
        prepared = dict(metadata)
        sample_name = str(prepared.get("sample_name") or "sample")
        concentration = prepared.get("known_concentration_um")
        with self.lock:
            save_dir = self._require_workspace()
            stem = self._reserve_export_stem(sample_name, concentration)
            prepared["export_stem"] = stem
            prepared["live_raw_path"] = str(save_dir / f"{stem}-raw.csv")
        return prepared

    def _reserve_export_stem(self, sample_name: str, concentration: Any) -> str:
        save_dir = self._configured_save_dir()
        root = (
            f"{self._safe_filename(sample_name)}-"
            f"{self._concentration_token(concentration)}"
        )
        candidate = root
        number = 2
        while any((save_dir / f"{candidate}{suffix}").exists()
                  for suffix in (".csv", "-raw.csv", "-filtered.csv", "-summary.json", ".png")):
            candidate = f"{root}-r{number}"
            number += 1
        return candidate

    def _save_calibration_points(self) -> None:
        path = self._workspace_paths()["points"]
        with path.open("w", newline="", encoding="utf-8") as handle:
            fields = [
                "point_id", "acquired_at", "run_id", "label",
                "concentration_um", "current_nA", "data_path", "selected",
            ]
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            selected = set(self.selected_point_ids)
            for record in self.point_records:
                writer.writerow({
                    **{key: record.get(key, "") for key in fields},
                    "selected": int(record["point_id"] in selected),
                })

    @staticmethod
    def _calibration_points_text(
        records: list[dict[str, Any]], selected_point_ids: list[str]
    ) -> str:
        buffer = io.StringIO(newline="")
        fields = [
            "point_id", "acquired_at", "run_id", "label",
            "concentration_um", "current_nA", "data_path", "selected",
        ]
        writer = csv.DictWriter(buffer, fieldnames=fields)
        writer.writeheader()
        selected = set(selected_point_ids)
        for record in records:
            writer.writerow({
                **{key: record.get(key, "") for key in fields},
                "selected": int(record["point_id"] in selected),
            })
        return buffer.getvalue()

    @staticmethod
    def _replace_workspace_files(contents: dict[Path, str | None]) -> None:
        """Commit related workspace files together, restoring them on failure."""
        staged: dict[Path, Path] = {}
        backups: dict[Path, Path | None] = {}
        committed: list[Path] = []
        temporary_paths: set[Path] = set()
        try:
            for path, content in contents.items():
                path.parent.mkdir(parents=True, exist_ok=True)
                if content is None:
                    continue
                with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", newline="", delete=False,
                    prefix=f".{path.name}.stage-", dir=path.parent,
                ) as handle:
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
                    temporary = Path(handle.name)
                temporary_paths.add(temporary)
                mode = (path.stat().st_mode & 0o777) if path.exists() else 0o644
                os.chmod(temporary, mode)
                staged[path] = temporary

            for path, content in contents.items():
                backup: Path | None = None
                if path.exists():
                    descriptor, backup_name = tempfile.mkstemp(
                        prefix=f".{path.name}.backup-", dir=path.parent
                    )
                    os.close(descriptor)
                    backup = Path(backup_name)
                    temporary_paths.add(backup)
                    os.replace(path, backup)
                backups[path] = backup
                committed.append(path)
                if content is not None:
                    os.replace(staged.pop(path), path)
        except Exception as exc:
            rollback_errors: list[str] = []
            for path in reversed(committed):
                backup = backups.get(path)
                try:
                    path.unlink(missing_ok=True)
                    if backup is not None and backup.exists():
                        os.replace(backup, path)
                except OSError as rollback_exc:
                    rollback_errors.append(f"{path.name}: {rollback_exc}")
            if rollback_errors:
                # Keep any surviving backups available for manual recovery.
                temporary_paths.difference_update(
                    backup for backup in backups.values()
                    if backup is not None and backup.exists()
                )
                raise RuntimeError(
                    "标定文件写入失败且回滚不完整："
                    + "; ".join(rollback_errors)
                ) from exc
            raise
        finally:
            for path in temporary_paths:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass

    @staticmethod
    def _ensure_measurement_index_schema(
        path: Path, required_fields: list[str]
    ) -> tuple[list[str], bool]:
        """Add new index columns without corrupting rows written by older releases."""
        if not path.exists() or path.stat().st_size == 0:
            return list(required_fields), False
        with path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            existing_fields = list(reader.fieldnames or [])
            missing = [field for field in required_fields if field not in existing_fields]
            if not missing:
                return existing_fields, False
            rows = list(reader)

        migrated_fields = [*existing_fields, *missing]
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", newline="", prefix=f".{path.name}.",
                suffix=".tmp", dir=path.parent, delete=False,
            ) as handle:
                temporary_path = Path(handle.name)
                writer = csv.DictWriter(handle, fieldnames=migrated_fields)
                writer.writeheader()
                for row in rows:
                    overflow_values = row.get(None)
                    if isinstance(overflow_values, list):
                        for field, value in zip(missing, overflow_values):
                            if row.get(field) in (None, ""):
                                row[field] = value
                    writer.writerow({field: row.get(field, "") for field in migrated_fields})
            os.replace(temporary_path, path)
            temporary_path = None
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
        return migrated_fields, True

    def _append_record(self, result: dict[str, Any]) -> None:
        path = self._workspace_paths()["index"]
        required_fields = [
            "finished_at", "run_id", "sample_name", "sample_role",
            "known_concentration_um", "steady_current_nA",
            "predicted_concentration_um", "state", "data_path", "raw_path",
            "measurement_settings_json",
        ]
        fields, migrated = self._ensure_measurement_index_schema(path, required_fields)
        if migrated:
            with path.open(newline="", encoding="utf-8") as handle:
                self.records = list(csv.DictReader(handle))
        row = {key: result.get(key, "") for key in fields}
        measurement_settings = result.get("measurement_settings")
        row["measurement_settings_json"] = (
            json.dumps(measurement_settings, ensure_ascii=False, sort_keys=True)
            if isinstance(measurement_settings, dict) else ""
        )
        new_file = not path.exists() or path.stat().st_size == 0
        with path.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            if new_file:
                writer.writeheader()
            writer.writerow(row)
        self.records.append({
            key: str(row.get(key, "")) for key in required_fields
        })

    def _save_drift(self) -> None:
        self._workspace_paths()["drift"].write_text(
            json.dumps(_json_safe(self.drift), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    def _save_validation_overrides(self) -> None:
        self._workspace_paths()["validation"].write_text(
            json.dumps({"points": self.validation_overrides,
                        "manual_points": self.manual_validation_points,
                        "deleted_point_ids": sorted(
                            self.deleted_validation_point_ids
                        )}, indent=2,
                       ensure_ascii=False),
            encoding="utf-8",
        )

    def update_validation_points(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Persist user edits to completed test rows without rewriting raw runs."""
        raw_points = payload.get("points", [])
        if not isinstance(raw_points, list):
            raise ValueError("测试点必须是数组")
        available = {
            str(row.get("run_id") or f"validation-{index:04d}")
            for index, row in enumerate(self.records, 1)
            if row.get("sample_role") == "test" and row.get("state") == "completed"
        }
        overrides: dict[str, dict[str, Any]] = {}
        manual = {str(point.get("point_id") or ""): dict(point)
                  for point in self.manual_validation_points}
        for item in raw_points:
            if not isinstance(item, dict):
                raise ValueError("测试点必须是对象")
            point_id = str(item.get("point_id") or "")
            if not point_id or (point_id not in available and point_id not in manual):
                raise ValueError("测试点记录不存在")
            try:
                current = float(item["current_nA"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError("测试点电流必须是数字") from exc
            if not math.isfinite(current):
                raise ValueError("测试点电流必须是有限数字")
            raw_concentration = item.get("concentration_um")
            if raw_concentration in (None, ""):
                concentration = None
            else:
                try:
                    concentration = float(raw_concentration)
                except (TypeError, ValueError) as exc:
                    raise ValueError("测试点浓度必须是数字") from exc
                if not math.isfinite(concentration) or concentration < 0:
                    raise ValueError("测试点浓度必须是非负有限数字")
            updated = {
                "sample_name": str(item.get("sample_name") or "").strip(),
                "concentration_um": concentration,
                "current_nA": current,
            }
            if point_id in manual:
                manual[point_id].update(updated)
            else:
                overrides[point_id] = updated
        with self.lock:
            self.validation_overrides = overrides
            self.manual_validation_points = list(manual.values())
            self._save_validation_overrides()
        return self.model_payload()

    def delete_validation_point(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Hide one test point while preserving its underlying measurement files."""
        point_id = str(payload.get("point_id") or "").strip()
        if not point_id:
            raise ValueError("请选择要删除的测试点")
        if self.measurement.is_busy() or self.schedule.snapshot()["active"]:
            raise RuntimeError("测量或自动任务运行期间不能删除测试点")
        with self.operation_lock, self.lock:
            manual_before = len(self.manual_validation_points)
            self.manual_validation_points = [
                point for point in self.manual_validation_points
                if str(point.get("point_id") or "") != point_id
            ]
            removed_manual = len(self.manual_validation_points) != manual_before
            record_exists = any(
                str(row.get("run_id") or f"validation-{index:04d}") == point_id
                and row.get("sample_role") == "test"
                and row.get("state") == "completed"
                for index, row in enumerate(self.records, 1)
            )
            if not removed_manual and not record_exists:
                raise ValueError("测试点记录不存在")
            if record_exists:
                self.deleted_validation_point_ids.add(point_id)
            self.validation_overrides.pop(point_id, None)
            self._save_validation_overrides()
        self._refresh_history_best_effort()
        return self.model_payload()

    def _stabilization_records(self) -> list[dict[str, Any]]:
        if self.model_created_at is None or self.model_settings is None:
            return []
        records: list[dict[str, Any]] = []
        selected = set(self.drift.get("record_ids") or [])
        for row in self.records:
            if row.get("sample_role") != "stabilization" or row.get("state") != "completed":
                continue
            try:
                current = float(row["steady_current_nA"])
                finished_at = float(row["finished_at"])
            except (KeyError, TypeError, ValueError):
                continue
            if (not math.isfinite(current) or not math.isfinite(finished_at)
                    or finished_at <= self.model_created_at):
                continue
            try:
                record_settings = json.loads(str(row["measurement_settings_json"]))
                if (not isinstance(record_settings, dict)
                        or not SettingsController.same_analysis_protocol(
                            record_settings, self.model_settings
                        )):
                    continue
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                continue
            run_id = str(row.get("run_id") or "")
            raw_concentration = row.get("known_concentration_um")
            try:
                concentration = (
                    None if raw_concentration in (None, "") else float(raw_concentration)
                )
            except (TypeError, ValueError):
                continue
            if (concentration is None or not math.isfinite(concentration)
                    or concentration < 0):
                continue
            records.append({
                "run_id": run_id,
                "sample_name": str(row.get("sample_name") or run_id),
                "finished_at": finished_at,
                "steady_current_nA": current,
                "known_concentration_um": concentration,
                "selected": run_id in selected,
            })
        return sorted(records, key=lambda record: record["finished_at"])

    def drift_payload(self) -> dict[str, Any]:
        with self.lock:
            return _json_safe({
                **self.drift,
                "records": self._stabilization_records(),
                "effective_bias_nA": self._effective_bias_nA(),
            })

    def _effective_bias_nA(self) -> float:
        return float(self.drift.get("bias_nA") or 0.0) if self.drift.get("enabled") else 0.0

    def calculate_drift(self, payload: dict[str, Any]) -> dict[str, Any]:
        raw_concentration = payload.get("known_concentration_um")
        if raw_concentration in (None, ""):
            raise ValueError("请填写稳定化溶液浓度")
        concentration = float(raw_concentration)
        if not math.isfinite(concentration):
            raise ValueError("稳定化溶液浓度必须是有限数字")
        if concentration < 0:
            raise ValueError("稳定化溶液浓度不能为负数")
        candidates = self._stabilization_records()
        if len(candidates) < 2:
            raise ValueError("至少需要两次已完成的稳定化 IT 才能计算漂移")
        requested_ids = [str(value) for value in payload.get("record_ids", [])]
        if requested_ids:
            requested = set(requested_ids)
            records = [record for record in candidates if record["run_id"] in requested]
        else:
            start_id = str(payload.get("start_run_id") or candidates[0]["run_id"])
            end_id = str(payload.get("end_run_id") or candidates[-1]["run_id"])
            index = {record["run_id"]: i for i, record in enumerate(candidates)}
            if start_id not in index or end_id not in index:
                raise ValueError("选择的稳定化记录不存在")
            start_index, end_index = sorted((index[start_id], index[end_id]))
            records = candidates[start_index:end_index + 1]
        if len(records) < 2:
            raise ValueError("漂移范围至少要包含两次稳定化 IT")
        if any(not math.isclose(
            float(record["known_concentration_um"]), concentration,
            rel_tol=1e-9, abs_tol=1e-9,
        ) for record in records):
            raise ValueError("漂移范围只能包含与填写浓度一致的稳定化记录")

        t0 = records[0]["finished_at"]
        x = [(record["finished_at"] - t0) / 3600.0 for record in records]
        y = [record["steady_current_nA"] for record in records]
        mean_x = sum(x) / len(x)
        mean_y = sum(y) / len(y)
        denominator = sum((value - mean_x) ** 2 for value in x)
        slope = (
            sum((xv - mean_x) * (yv - mean_y) for xv, yv in zip(x, y)) / denominator
            if denominator > 0 else None
        )
        with self.lock:
            self.drift = {
                "enabled": bool(payload.get("enabled", self.drift.get("enabled", False))),
                "solution_name": str(payload.get("solution_name") or "").strip(),
                "known_concentration_um": concentration,
                "bias_nA": float(y[-1] - y[0]),
                "slope_nA_per_hour": slope,
                "start_current_nA": float(y[0]),
                "end_current_nA": float(y[-1]),
                "start_at": records[0]["finished_at"],
                "end_at": records[-1]["finished_at"],
                "record_ids": [record["run_id"] for record in records],
                "calculated_at": time.time(),
            }
            self._save_drift()
        return self.drift_payload()

    def toggle_drift(self, payload: dict[str, Any]) -> dict[str, Any]:
        enabled = bool(payload.get("enabled"))
        with self.lock:
            if enabled and self.drift.get("calculated_at") is None:
                raise ValueError("请先选择稳定化范围并计算漂移")
            self.drift["enabled"] = enabled
            self._save_drift()
        return self.drift_payload()

    def _measurement_completed(self, run: dict[str, Any]) -> None:
        metadata = dict(run.get("metadata") or {})
        # 🔴 硬件 DEBUG 轮不进工作流:不导出、不进 measurement-index.csv、不触发
        #    浓度预测。否则调参数时随手跑的几轮会污染标定工作区,而"污染"这件事
        #    要到下次拟合曲线时才会暴露出来 —— 那时已经分不清哪几行是调试轮。
        #    刻意**不新增第四个 sample_role**:那要串改 5 处调用点却买不到任何东西。
        if metadata.get("debug"):
            result = {
                "finished_at": run.get("finished_at"),
                "run_id": run.get("run_id"),
                "debug": True,
                "state": run.get("state"),
                "note": "硬件 DEBUG 轮:原始数据保留在 run_dir,不进标定工作区",
                "run_dir": run.get("run_dir"),
                "raw_path": run.get("raw_path"),
            }
            self.measurement.set_workflow_result(result)
            _notify_measurement_completion(run, result)
            return
        hardware_taint = run.get("hardware_taint")
        if hardware_taint:
            result = {
                "finished_at": run.get("finished_at"),
                "run_id": run.get("run_id"),
                "sample_name": str(
                    metadata.get("sample_name") or run.get("run_id") or "sample"
                ),
                "sample_role": str(metadata.get("sample_role") or "test"),
                "state": "error",
                "tainted": True,
                "hardware_taint": hardware_taint,
                "note": "硬件报告 IT_TAINTED：原始数据保留，但不进入标定、预测或测量索引",
                "run_dir": run.get("run_dir"),
                "raw_path": run.get("raw_path"),
            }
            self.measurement.set_workflow_result(result)
            _notify_measurement_completion(run, result)
            return
        sample_name = str(metadata.get("sample_name") or run.get("run_id") or "sample")
        concentration = metadata.get("known_concentration_um")
        role = str(metadata.get("sample_role") or "test")
        result: dict[str, Any] = {
            "finished_at": run.get("finished_at"),
            "run_id": run.get("run_id"),
            "sample_name": sample_name,
            "sample_role": role,
            "known_concentration_um": concentration,
            "state": run.get("state"),
            "steady_current_nA": (run.get("summary") or {}).get("steady_current_nA"),
            "predicted_concentration_um": None,
            "measurement_settings": run.get("settings"),
        }
        try:
            with self.lock:
                save_dir = self._require_workspace()
                stem = str(metadata.get("export_stem") or "")
                if not stem:
                    stem = self._reserve_export_stem(sample_name, concentration)
                raw_source = Path(str(run.get("raw_path") or ""))
                data_source = Path(str(run.get("resampled_path") or ""))
                filtered_source = Path(str(run.get("filtered_path") or ""))
                raw_target = save_dir / f"{stem}-raw.csv"
                data_target = save_dir / f"{stem}.csv"
                filtered_target = save_dir / f"{stem}-filtered.csv"
                summary_target = save_dir / f"{stem}-summary.json"
                plot_target = save_dir / f"{stem}.png"
                if raw_source.exists():
                    if raw_source.resolve() != raw_target.resolve():
                        shutil.copy2(raw_source, raw_target)
                    result["raw_path"] = str(raw_target)
                else:
                    result["raw_path"] = ""
                if data_source.exists():
                    shutil.copy2(data_source, data_target)
                    result["data_path"] = str(data_target)
                    try:
                        if run["settings"].get("method") == "cv":
                            plot_source = Path(str(run.get("plot_path") or ""))
                            if plot_source.exists():
                                shutil.copy2(plot_source, plot_target)
                            else:
                                plot_cv(raw_source, plot_target)
                        else:
                            from .it_tool import _plot_run
                            _plot_run(data_source, plot_target,
                                      float(run["settings"]["fit_window_s"]))
                        result["plot_path"] = str(plot_target)
                    except Exception:
                        result["plot_path"] = ""
                else:
                    result["data_path"] = ""
                    result["plot_path"] = ""
                if filtered_source.exists():
                    shutil.copy2(filtered_source, filtered_target)
                    result["filtered_data_path"] = str(filtered_target)
                else:
                    result["filtered_data_path"] = ""

                steady = result["steady_current_nA"]
                try:
                    steady_value = float(steady) if steady is not None else None
                except (TypeError, ValueError, OverflowError):
                    steady_value = None
                if steady_value is not None and not math.isfinite(steady_value):
                    steady_value = None
                if run.get("state") == "completed" and steady_value is not None:
                    if role == "calibration" and concentration is not None:
                        run_settings = SettingsController.validate(dict(run["settings"]))
                        run_plateau = self._run_plateau_signature(run, run_settings)
                        if self.calibration_settings is None:
                            self.calibration_settings = run_settings
                            self.calibration_plateau = run_plateau
                            self._workspace_paths()["settings"].write_text(
                                json.dumps(self.calibration_settings, indent=2,
                                           ensure_ascii=False),
                                encoding="utf-8",
                            )
                            if run_plateau is not None:
                                self._workspace_paths()["plateau"].write_text(
                                    json.dumps(
                                        {"settings": run_plateau},
                                        indent=2,
                                        ensure_ascii=False,
                                    ),
                                    encoding="utf-8",
                                )
                            else:
                                self._workspace_paths()["plateau"].unlink(
                                    missing_ok=True
                                )
                        elif not self._same_calibration_protocol(
                            self.calibration_settings,
                            self.calibration_plateau,
                            run_settings,
                            run_plateau,
                        ):
                            raise ValueError(
                                "本次 IT 或自动停止参数与已有标定点不一致，"
                                "结果未加入标定"
                            )
                        if self.calibration_filter is None:
                            self.calibration_filter = validate_filter_config(
                                (run.get("filter") or {}).get("config")
                                if isinstance(run.get("filter"), dict) else None
                            )
                            self._workspace_paths()["filter"].write_text(
                                json.dumps({"settings": self.calibration_filter},
                                           indent=2, ensure_ascii=False),
                                encoding="utf-8",
                            )
                        point_id = str(run.get("run_id") or f"point-{time.time_ns()}")
                        existing_ids = {item["point_id"] for item in self.point_records}
                        if point_id in existing_ids:
                            point_id = f"{point_id}-{time.time_ns()}"
                        self.point_records.append({
                            "point_id": point_id,
                            "acquired_at": float(run.get("finished_at") or time.time()),
                            "run_id": str(run.get("run_id") or ""),
                            "label": sample_name,
                            "concentration_um": float(concentration),
                            "current_nA": steady_value,
                            "data_path": result.get("data_path", ""),
                        })
                        self._sync_points()
                        self._save_calibration_points()
                        result["calibration_points"] = len(self.points)
                        result["candidate_added"] = True
                        result["calibration_ready"] = self.workflow_snapshot()[
                            "calibration_ready"
                        ]
                    elif role == "test" and self.model is not None:
                        effective_current = steady_value - self._effective_bias_nA()
                        try:
                            result["predicted_concentration_um"] = float(
                                self.model.predict_concentration(effective_current)
                            )
                        except (TypeError, ValueError, OverflowError):
                            # Outlying polynomial currents may have no inverse
                            # in the calibrated range. The completed run must
                            # still be indexed and score Grey rather than being
                            # discarded by the export transaction.
                            result["predicted_concentration_um"] = None

                exported_summary = {
                    **(run.get("summary") or {}),
                    "sample_name": sample_name,
                    "sample_role": role,
                    "known_concentration_um": concentration,
                    "predicted_concentration_um": result["predicted_concentration_um"],
                    "measurement_settings": run.get("settings"),
                    "source_run_dir": run.get("run_dir"),
                    "saved_data_path": result.get("data_path", ""),
                    "saved_filtered_data_path": result.get("filtered_data_path", ""),
                    "saved_raw_path": result.get("raw_path", ""),
                    "calibration_model_path": (
                        str(self.model_path) if self.model_path else ""
                    ),
                    "calibration_model_created_at": self.model_created_at,
                    "calibration_validation_started_at": self.validation_started_at,
                    "calibration_selected_point_ids": self.selected_point_ids,
                    "calibration_model": (
                        self.model.to_json() if self.model is not None else None
                    ),
                    "drift_correction": self.drift_payload(),
                }
                summary_target.write_text(
                    json.dumps(_json_safe(exported_summary), indent=2,
                               ensure_ascii=False),
                    encoding="utf-8",
                )
                result["summary_path"] = str(summary_target)
                self._append_record(result)
                self.latest_workflow_result = dict(result)
        except Exception as exc:
            result["export_error"] = str(exc)
            self.latest_workflow_result = dict(result)
        self._refresh_history_best_effort()
        self.measurement.set_workflow_result(result)
        _notify_measurement_completion(run, result)

    def reset_calibration(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        if self.measurement.is_busy() or self.schedule.snapshot()["active"]:
            raise RuntimeError("测量或自动任务运行期间不能重置标定")
        payload = payload or {}
        label = str(payload.get("batch_name") or "").strip()
        with self.operation_lock:
            self._require_workspace()
            root = self._configured_workspace_root()
            self.workspace_root = root
            self._start_batch(root, label)
            self._refresh_history_best_effort()
        return self.workflow_snapshot()

    def fit(self, payload: dict[str, Any]) -> dict[str, Any]:
        # Measurement and schedule starts use the same lock at the HTTP
        # boundary. Holding it through the activity snapshot and fit prevents
        # an idle fit from crossing into a newly started acquisition.
        with self.operation_lock:
            return self._fit_locked(payload)

    def _fit_locked(self, payload: dict[str, Any]) -> dict[str, Any]:
        raw_points = payload.get("points", [])
        if not isinstance(raw_points, list):
            raise ValueError("标定点必须是数组")
        degree = int(payload.get("degree", 1))
        if degree not in (1, 2):
            raise ValueError("上位机标定仅支持线性或二次模型")
        requested_ids = payload.get("selected_point_ids")
        if requested_ids is not None and not isinstance(requested_ids, list):
            raise ValueError("选中标定点必须是数组")

        # Read controller activity before taking the workspace lock. Some
        # terminal measurement paths invoke the completion hook while holding
        # MeasurementController.lock, so taking those locks in the opposite
        # order here could deadlock the final export.
        busy = self.measurement.is_busy() or self.schedule.snapshot()["active"]

        # Completion exports and fitting both replace related workspace files.
        # Keep the entire fit transaction under one lock so a just-finished run
        # cannot be lost to a stale browser payload.
        with self.lock:
            current_revision = self._points_revision_locked()
            supplied_revision = payload.get("points_revision")
            if (
                not busy
                and supplied_revision is not None
                and str(supplied_revision) != current_revision
            ):
                raise RuntimeError("候选标定点已在后台更新，请刷新页面后重试")

            if busy:
                # During acquisition, only completed candidates already saved
                # by _measurement_completed are eligible. Browser edits and a
                # still-running summary must never enter the fitted model.
                records = [dict(record) for record in self.point_records]
            else:
                records = []
                seen_ids: set[str] = set()
                for index, row in enumerate(raw_points, 1):
                    if not isinstance(row, dict):
                        raise ValueError("标定点必须是对象")
                    point_id = str(row.get("point_id") or f"manual-{index:04d}")
                    if point_id in seen_ids:
                        point_id = f"{point_id}-{index}"
                    seen_ids.add(point_id)
                    records.append({
                        "point_id": point_id,
                        "acquired_at": float(row.get("acquired_at") or 0),
                        "run_id": str(row.get("run_id") or ""),
                        "label": str(row.get("label", "")),
                        "concentration_um": float(row["concentration_um"]),
                        "current_nA": float(row["current_nA"]),
                        "data_path": str(row.get("data_path") or ""),
                    })

            selected_ids = (
                [str(value) for value in requested_ids]
                if requested_ids is not None
                else [record["point_id"] for record in records]
            )
            available_ids = {record["point_id"] for record in records}
            unknown_ids = set(selected_ids) - available_ids
            if busy and unknown_ids:
                raise RuntimeError(
                    "测量进行中只能使用已完成并保存的候选点，"
                    "请刷新标定页后重新选择"
                )
            selected_id_set = set(selected_ids)
            selected_records = [
                record for record in records
                if record["point_id"] in selected_id_set
            ]
            if len(selected_records) < degree + 1:
                raise ValueError(f"至少选择 {degree + 1} 个标定点")
            selected_points = [
                CalibrationPoint(
                    float(record["concentration_um"]),
                    float(record["current_nA"]),
                    str(record["label"]),
                )
                for record in selected_records
            ]
            model_settings = self.settings.snapshot()["settings"]
            model_plateau = (
                self._plateau_signature(self.plateau.settings)
                if self._uses_plateau_protocol(model_settings) else None
            )
            if (
                self.calibration_settings is not None
                and not self._same_calibration_protocol(
                    self.calibration_settings,
                    self.calibration_plateau,
                    model_settings,
                    model_plateau,
                )
            ):
                raise ValueError(
                    "当前 IT 条件与候选标定点不一致，不能生成测试曲线"
                )
            model = fit_calibration(selected_points, degree=degree)
            self._require_workspace()
            paths = self._workspace_paths()
            path = paths["model"]
            current_filter = validate_filter_config(
                self.filter.snapshot()["settings"]
            )
            created_at = time.time()
            validation_started_at = (
                created_at
                if self.validation_started_at is None
                else self.validation_started_at
            )
            selected_record_ids = [
                record["point_id"] for record in selected_records
            ]
            selection_payload = {
                "created_at": created_at,
                "validation_started_at": validation_started_at,
                "degree": degree,
                "selected_point_ids": selected_record_ids,
                "candidate_points_count": len(records),
            }
            self._replace_workspace_files({
                path: json.dumps(model.to_json(), indent=2) + "\n",
                paths["settings"]: json.dumps(
                    model_settings, indent=2, ensure_ascii=False
                ),
                paths["plateau"]: (
                    json.dumps(
                        {"settings": model_plateau},
                        indent=2,
                        ensure_ascii=False,
                    )
                    if model_plateau is not None else None
                ),
                paths["filter"]: json.dumps({
                    "settings": current_filter,
                    "policy": "mixed_filters_allowed",
                }, indent=2, ensure_ascii=False),
                paths["selection"]: json.dumps(
                    selection_payload, indent=2, ensure_ascii=False
                ),
                paths["points"]: self._calibration_points_text(
                    records, selected_record_ids
                ),
            })
            self.point_records = records
            self._sync_points()
            self.selected_point_ids = selected_record_ids
            self.model = model
            self.model_path = path
            self.model_settings = model_settings
            self.model_plateau = (
                dict(model_plateau) if model_plateau is not None else None
            )
            self.calibration_settings = dict(model_settings)
            self.calibration_plateau = (
                dict(model_plateau) if model_plateau is not None else None
            )
            self.calibration_filter = current_filter
            self.model_created_at = created_at
            self.validation_started_at = validation_started_at
            return self.model_payload()

    def load_points(self, path: str) -> dict[str, Any]:
        points = load_calibration_points(path)
        with self.lock:
            self.point_records = [
                {
                    "point_id": f"import-{index:04d}",
                    "acquired_at": 0,
                    "run_id": "",
                    "label": point.label,
                    "concentration_um": point.concentration_um,
                    "current_nA": point.current_nA,
                    "data_path": "",
                }
                for index, point in enumerate(points, 1)
            ]
            self._sync_points()
            self.selected_point_ids = []
        return self.points_payload()

    def model_payload(self) -> dict[str, Any]:
        with self.lock:
            bias_nA = self._effective_bias_nA()
            points_payload = self.points_payload()
            model_compatible = (
                self.model is not None
                and self.model_settings is not None
                and self._same_calibration_protocol(
                    self.model_settings,
                    self.model_plateau,
                    self.settings.snapshot()["settings"],
                    self.plateau.settings,
                )
            )
            if self.model is None:
                return {
                    "model": None,
                    "points": points_payload["points"],
                    "points_revision": points_payload["points_revision"],
                    "validation_points": [],
                    "ap_score": evaluate_ap_score([]),
                    "selected_point_ids": self.selected_point_ids,
                    "model_created_at": self.model_created_at,
                    "validation_started_at": self.validation_started_at,
                    "drift_bias_nA": bias_nA,
                    "model_compatible": False,
                }
            model = self.model
            xs = [
                model.concentration_min_um
                + (model.concentration_max_um - model.concentration_min_um) * i / 199
                for i in range(200)
            ]
            ys = [float(model.current_from_concentration(x)) + bias_nA for x in xs]
            validation_points = self._validation_points_payload(model, bias_nA)
            ap_score = evaluate_ap_score(validation_points)
            scored_details = {
                int(detail["sequence"]): detail for detail in ap_score["points"]
            }
            for index, point in enumerate(validation_points, 1):
                detail = scored_details.get(index)
                if detail is not None:
                    point.update({
                        "zone": detail["zone"], "sample_score": detail["score"],
                        "absolute_error_um": detail["absolute_error_um"],
                        "error_percent": detail["error_percent"],
                    })
                else:
                    point.update({"zone": None, "sample_score": None,
                                 "absolute_error_um": None, "error_percent": None})
            return {
                "model": _json_safe(model.to_json()),
                "model_path": str(self.model_path) if self.model_path else "",
                "points": points_payload["points"],
                "points_revision": points_payload["points_revision"],
                "curve": {"concentration_um": xs, "current_nA": ys},
                "validation_points": validation_points,
                "ap_score": ap_score,
                "measurement_settings": self.model_settings,
                "selected_point_ids": self.selected_point_ids,
                "model_created_at": self.model_created_at,
                "validation_started_at": self.validation_started_at,
                "drift_bias_nA": bias_nA,
                "model_compatible": model_compatible,
            }

    def _validation_points_payload(
        self, model: CalibrationModel, bias_nA: float = 0.0
    ) -> list[dict[str, Any]]:
        """Build test points for the calibration chart without refitting the model.

        Only completed test runs acquired after the first model in the current
        calibration batch, with a finite steady current, can be placed on its
        chart. A missing known concentration is kept editable but excluded from
        AP scoring until the user fills it in. Regenerating the model preserves
        those test results; explicitly starting a new calibration batch resets
        the boundary without deleting the historical measurement index. The
        measured current stays untouched; the model's optional drift bias is
        applied only when calculating expected current and concentration.
        """
        if self.validation_started_at is None:
            return []
        validation_points: list[dict[str, Any]] = []
        sources: list[dict[str, Any]] = []
        for index, row in enumerate(self.records, 1):
            if row.get("sample_role") != "test" or row.get("state") != "completed":
                continue
            point_id = str(row.get("run_id") or f"validation-{index:04d}")
            if point_id in self.deleted_validation_point_ids:
                continue
            override = self.validation_overrides.get(point_id, {})
            try:
                measured_current = float(override.get("current_nA", row["steady_current_nA"]))
                finished_at = float(row.get("finished_at") or 0)
            except (KeyError, TypeError, ValueError):
                continue
            if not math.isfinite(measured_current):
                continue
            if finished_at <= self.validation_started_at:
                continue
            raw_concentration = override.get(
                "concentration_um", row.get("known_concentration_um")
            )
            try:
                concentration = (
                    None if raw_concentration in (None, "")
                    else float(raw_concentration)
                )
            except (TypeError, ValueError):
                concentration = None
            if concentration is not None and (
                not math.isfinite(concentration) or concentration < 0
            ):
                concentration = None
            expected_current = (
                float(model.current_from_concentration(concentration)) + bias_nA
                if concentration is not None else None
            )
            try:
                predicted_concentration = float(
                    model.predict_concentration(measured_current - bias_nA)
                )
            except (TypeError, ValueError, OverflowError):
                # A quadratic model may have no real inverse for an outlying
                # test current. Keep the measured point and current error on
                # the chart, while leaving concentration error unavailable.
                predicted_concentration = None
            sources.append({
                "point_id": point_id,
                "run_id": point_id,
                "sample_name": str(override.get("sample_name") or row.get("sample_name") or point_id or "测试样品"),
                "finished_at": finished_at,
                "concentration_um": concentration,
                "current_nA": measured_current,
                "expected_current_nA": expected_current,
                "predicted_concentration_um": predicted_concentration,
                "error_nA": (
                    measured_current - expected_current
                    if expected_current is not None else None
                ),
                "error_um": (
                    predicted_concentration - concentration
                    if predicted_concentration is not None
                    and concentration is not None else None
                ),
                "data_path": str(row.get("data_path") or ""),
                "edited": bool(override),
            })
        for point in self.manual_validation_points:
            point_id = str(point.get("point_id") or "")
            try:
                measured_current = float(point["current_nA"])
                finished_at = float(point.get("finished_at") or 0)
            except (KeyError, TypeError, ValueError):
                continue
            if not point_id or not math.isfinite(measured_current):
                continue
            raw_concentration = point.get("concentration_um")
            try:
                concentration = None if raw_concentration in (None, "") else float(raw_concentration)
            except (TypeError, ValueError):
                concentration = None
            if concentration is not None and (not math.isfinite(concentration) or concentration < 0):
                concentration = None
            expected_current = (float(model.current_from_concentration(concentration)) + bias_nA
                                if concentration is not None else None)
            try:
                predicted_concentration = float(model.predict_concentration(measured_current - bias_nA))
            except (TypeError, ValueError, OverflowError):
                predicted_concentration = None
            sources.append({
                "point_id": point_id,
                "run_id": str(point.get("source_point_id") or point_id),
                "sample_name": str(point.get("sample_name") or point_id),
                "finished_at": finished_at,
                "concentration_um": concentration,
                "current_nA": measured_current,
                "expected_current_nA": expected_current,
                "predicted_concentration_um": predicted_concentration,
                "error_nA": measured_current - expected_current if expected_current is not None else None,
                "error_um": predicted_concentration - concentration if predicted_concentration is not None and concentration is not None else None,
                "data_path": str(point.get("data_path") or ""),
                "edited": True,
                "manual": True,
            })
        return sources

    def add_validation_to_calibration(self, payload: dict[str, Any]) -> dict[str, Any]:
        point_id = str(payload.get("point_id") or "")
        if self.measurement.is_busy() or self.schedule.snapshot()["active"]:
            raise RuntimeError("测量或自动任务运行期间不能修改标定点")
        if self.model is None:
            raise ValueError("请先生成测试曲线")
        validation = next((point for point in self._validation_points_payload(self.model)
                           if point["point_id"] == point_id), None)
        if validation is None:
            raise ValueError("测试点不存在")
        concentration = validation.get("concentration_um")
        current = validation.get("current_nA")
        if concentration is None or current is None:
            raise ValueError("测试点必须有真实浓度和测量电流才能加入标定")
        if any(record.get("run_id") == validation.get("run_id")
               for record in self.point_records):
            return self.model_payload()
        with self.operation_lock, self.lock:
            candidate_id = f"from-test-{point_id}"
            while any(record["point_id"] == candidate_id for record in self.point_records):
                candidate_id = f"{candidate_id}-copy"
            self.point_records.append({
                "point_id": candidate_id,
                "acquired_at": float(validation.get("finished_at") or time.time()),
                "run_id": str(validation.get("run_id") or point_id),
                "label": str(validation.get("sample_name") or point_id),
                "concentration_um": float(concentration),
                "current_nA": float(current),
                "data_path": str(validation.get("data_path") or ""),
            })
            self._sync_points()
            self._save_calibration_points()
        self._refresh_history_best_effort()
        return self.model_payload()

    def add_calibration_to_validation(self, payload: dict[str, Any]) -> dict[str, Any]:
        point_id = str(payload.get("point_id") or "")
        if self.measurement.is_busy() or self.schedule.snapshot()["active"]:
            raise RuntimeError("测量或自动任务运行期间不能修改测试点")
        with self.operation_lock, self.lock:
            source = next((record for record in self.point_records
                           if record["point_id"] == point_id), None)
            if source is None:
                raise ValueError("标定点不存在")
            existing = next((point for point in self.manual_validation_points
                             if point.get("source_point_id") == point_id), None)
            if existing is None:
                self.manual_validation_points.append({
                    "point_id": f"manual-test-{point_id}",
                    "source_point_id": point_id,
                    "sample_name": str(source.get("label") or point_id),
                    "concentration_um": source.get("concentration_um"),
                    "current_nA": source.get("current_nA"),
                    "data_path": str(source.get("data_path") or ""),
                    "finished_at": source.get("acquired_at") or time.time(),
                })
                self._save_validation_overrides()
        self._refresh_history_best_effort()
        return self.model_payload()

    def points_payload(self) -> dict[str, Any]:
        with self.lock:
            selected_ids = set(self.selected_point_ids)
            return {
                "points_revision": self._points_revision_locked(),
                "points": [
                    {
                        **record,
                        "selected": record["point_id"] in selected_ids,
                    }
                    for record in self.point_records
                ],
            }

    def predict(self, payload: dict[str, Any]) -> dict[str, Any]:
        model = load_model(payload["model_path"]) if payload.get("model_path") else self.model
        if model is None:
            raise ValueError("尚未拟合标定曲线")
        if not payload.get("model_path") and self.model_settings is not None:
            current_settings = self.settings.snapshot()["settings"]
            if not self._same_calibration_protocol(
                self.model_settings,
                self.model_plateau,
                current_settings,
                self.plateau.settings,
            ):
                raise ValueError("当前 IT 条件与标定模型不一致，请在当前条件下重新标定")
        current = payload.get("current_nA")
        if current is None or current == "":
            summary = self.measurement.summary or {}
            current = summary.get("steady_current_nA")
        if current is None:
            raise ValueError("没有可用的未知样品稳态电流")
        current = float(current)
        bias_nA = 0.0 if payload.get("model_path") else self._effective_bias_nA()
        concentration = model.predict_concentration(current - bias_nA)
        return {
            "current_nA": current,
            "predicted_concentration_um": concentration,
            "model_r2": model.r2,
            "model_path": str(self.model_path) if self.model_path else "",
            "drift_bias_nA": bias_nA,
            "bias_corrected_current_nA": current - bias_nA,
        }


APP = AppState()
HTTP_SERVER: ThreadingHTTPServer | None = None
_SHUTDOWN_LOCK = threading.Lock()
_SHUTDOWN_REQUESTED = False


def _diagnostic_runtime_context() -> dict[str, Any]:
    with APP.measurement.lock:
        measurement = {
            "state": APP.measurement.state,
            "message": APP.measurement.message,
            "error": APP.measurement.error,
            "run_id": APP.measurement.run_id,
            "run_dir": str(APP.measurement.run_dir or ""),
            "started_at": APP.measurement.started_at,
            "finished_at": APP.measurement.finished_at,
            "config_gate": dict(APP.measurement._config_gate),
        }
    with APP.lock:
        workspace = {
            "workspace_root": str(APP.workspace_root or ""),
            "save_dir": str(APP.save_dir or ""),
            "available": APP.workspace_available,
            "error": APP.workspace_error,
        }
    settings = APP.settings.snapshot()
    schedule = APP.schedule.snapshot()
    return {
        "project_dir": str(PROJECT_DIR),
        "state_dir": str(STATE_DIR),
        "transport": _transport_status(),
        "selected_device": _selected_device_copy(),
        "measurement": measurement,
        "settings": {
            "state": settings.get("state"),
            "message": settings.get("message"),
            "error": settings.get("error"),
            "applied": settings.get("applied"),
            "firmware_source": settings.get("firmware_source"),
            "firmware_sha256": settings.get("firmware_sha256"),
            "firmware_transport": settings.get("firmware_transport"),
            "settings": settings.get("settings"),
        },
        "schedule": {
            "active": schedule.get("active"),
            "message": schedule.get("message"),
            "failed_runs": schedule.get("failed_runs"),
        },
        "workspace": workspace,
    }


def _diagnostic_current_run_files() -> list[tuple[str, Path]]:
    with APP.measurement.lock:
        run_dir = APP.measurement.run_dir
        backend_log = (
            APP.measurement.raw_log.with_name(
                APP.measurement.raw_log.stem + "-backend.log"
            )
            if APP.measurement.raw_log is not None else None
        )
        candidates = [
            ("collector.log", run_dir / "collector.log" if run_dir else None),
            ("firmware-rtt.log", APP.measurement.raw_log),
            ("rtt-backend.log", backend_log),
            ("hardware-audit.jsonl", APP.measurement.audit_path),
            ("summary.json", APP.measurement.summary_path),
            ("commands.txt", APP.measurement.cmd_path),
        ]
    return [(name, path) for name, path in candidates if path is not None]


def _request_server_shutdown() -> None:
    """Ask the serving loop to enter its existing graceful cleanup path."""
    global _SHUTDOWN_REQUESTED
    with _SHUTDOWN_LOCK:
        if _SHUTDOWN_REQUESTED:
            return
        _SHUTDOWN_REQUESTED = True
        server = HTTP_SERVER
    DIAGNOSTICS.record(
        "info", "application.shutdown_requested",
        "Graceful application shutdown was requested",
    )
    if server is not None:
        # ``shutdown`` must run outside the request/serve thread. Starting it
        # after the response has been written lets the browser receive its ACK.
        threading.Thread(
            target=server.shutdown,
            name="gui-server-shutdown",
            daemon=True,
        ).start()


def _release_hardware_for_shutdown(timeout_s: float = 330.0) -> dict[str, Any]:
    """Wait out non-interruptible operations, then stop acquisition normally."""
    SHUTDOWN_INTENT.set()
    deadline = time.monotonic() + max(0.0, timeout_s)
    try:
        # Driver preparation acquires its own lock before APP.operation_lock.
        # Waiting here avoids taking those locks in the reverse order when the
        # user closes the application immediately after clicking Prepare.
        while JLINK_DRIVER_INSTALL_LOCK.locked():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError(
                    "J-Link 驱动仍在安全准备，请稍后再次点击退出"
                )
            time.sleep(min(0.1, remaining))
        with APP.operation_lock:
            if APP.schedule.snapshot()["active"]:
                APP.schedule.stop()
            if APP.measurement.is_busy():
                APP.measurement.stop()
            while time.monotonic() < deadline:
                if APP.hardware_idle():
                    return {
                        "ok": True,
                        "message": "硬件已安全释放，后端正在退出",
                    }
                time.sleep(0.1)
            if APP.hardware_idle():
                return {
                    "ok": True,
                    "message": "硬件已安全释放，后端正在退出",
                }
            raise RuntimeError("硬件任务仍在安全停止，请稍后再次点击退出")
    except Exception:
        SHUTDOWN_INTENT.clear()
        raise


class DiagnosticHTTPServer(ThreadingHTTPServer):
    """Keep failures outside an API handler inside the diagnostic timeline."""

    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request: Any, client_address: Any) -> None:
        del request
        exc = sys.exc_info()[1]
        context = {"client_address": client_address}
        if isinstance(
            exc,
            (BrokenPipeError, ConnectionResetError, ConnectionAbortedError),
        ):
            DIAGNOSTICS.record(
                "info", "http.client.disconnected",
                "Client connection ended while HTTP was being processed",
                error=str(exc), **context,
            )
            return
        if isinstance(exc, BaseException):
            DIAGNOSTICS.exception(
                "http.request.unhandled", "Unhandled HTTP worker failure",
                exc, **context,
            )
            return
        DIAGNOSTICS.record(
            "error", "http.request.unhandled",
            "HTTP worker failed without exception details", **context,
        )


class RequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        # Keep the one-click Terminal quiet except for meaningful server errors.
        return

    def _send_json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        status_code = int(status)
        request_id = str(getattr(self, "_request_id", ""))
        response_payload = payload
        if (
            status_code >= 400
            and isinstance(payload, dict)
            and payload.get("error")
        ):
            diagnostic_id = str(payload.get("diagnostic_id") or "")
            if not diagnostic_id:
                diagnostic_id = DIAGNOSTICS.record(
                    "error" if status_code >= 500 else "warning",
                    "api.request.error",
                    str(payload.get("error")),
                    method=str(getattr(self, "command", "")),
                    path=str(getattr(self, "path", "")),
                    status=status_code,
                    request_id=request_id,
                    body_keys=getattr(self, "_request_body_keys", []),
                )
            response_payload = {**payload, "diagnostic_id": diagnostic_id}
            setattr(self, "_diagnostic_id", diagnostic_id)
        data = json.dumps(_json_safe(response_payload), ensure_ascii=False).encode("utf-8")
        self._response_status = status_code
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        if request_id:
            self.send_header("X-Request-ID", request_id)
        diagnostic_id = str(getattr(self, "_diagnostic_id", ""))
        if diagnostic_id:
            self.send_header("X-Diagnostic-ID", diagnostic_id)
        self.end_headers()
        self.wfile.write(data)

    def _send_bytes(
        self, data: bytes, content_type: str, *, download_name: str = ""
    ) -> None:
        self._response_status = int(HTTPStatus.OK)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        request_id = str(getattr(self, "_request_id", ""))
        if request_id:
            self.send_header("X-Request-ID", request_id)
        if download_name:
            self.send_header(
                "Content-Disposition", f'attachment; filename="{download_name}"'
            )
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict[str, Any]:
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            raise ValueError("控制接口只接受 application/json")
        origin = self.headers.get("Origin")
        if origin:
            parsed_origin = urlparse(origin)
            if parsed_origin.netloc != self.headers.get("Host", ""):
                raise ValueError("拒绝跨站控制请求")
        length = int(self.headers.get("Content-Length", "0"))
        if length < 0 or length > 2_000_000:
            raise ValueError("请求体过大")
        raw = self.rfile.read(length)
        if not raw:
            return {}
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("请求必须是 JSON 对象")
        self._request_body_keys = sorted(str(key) for key in payload)
        return payload

    def _run_request(self, method: str, callback: Any) -> None:
        started_at = time.monotonic()
        self._request_id = hashlib.sha256(
            f"{time.time_ns()}:{threading.get_ident()}:{getattr(self, 'path', '')}".encode()
        ).hexdigest()[:12]
        self._response_status = 0
        self._diagnostic_id = ""
        self._request_body_keys = []
        path = str(getattr(self, "path", ""))
        if method == "POST":
            DIAGNOSTICS.record(
                "info", "api.request.started", "Control request started",
                method=method, path=path, request_id=self._request_id,
            )
        try:
            callback()
        except (
            BrokenPipeError, ConnectionResetError, ConnectionAbortedError,
        ) as exc:
            DIAGNOSTICS.record(
                "info", "api.client.disconnected",
                "Client disconnected before the response completed",
                method=method, path=path, request_id=self._request_id,
                error=str(exc),
            )
        except Exception as exc:  # never let a request thread vanish silently
            diagnostic_id = DIAGNOSTICS.exception(
                "api.request.unhandled", "Unhandled API request failure", exc,
                method=method, path=path, request_id=self._request_id,
                body_keys=self._request_body_keys,
            )
            self._diagnostic_id = diagnostic_id
            try:
                self._send_json(
                    {
                        "error": "后台发生未预期错误，请根据诊断编号查看日志",
                        "diagnostic_id": diagnostic_id,
                    },
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                )
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
        finally:
            status = int(getattr(self, "_response_status", 0) or 0)
            if method == "POST" and 0 < status < 400:
                DIAGNOSTICS.record(
                    "info", "api.request.completed", "Control request completed",
                    method=method, path=path, status=status,
                    request_id=self._request_id,
                    body_keys=self._request_body_keys,
                    duration_ms=round((time.monotonic() - started_at) * 1000, 1),
                )

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        self._run_request("GET", self._do_GET)

    def _do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            # Keep the workstation usable in embedded browsers that may drop an
            # external stylesheet after handing the tab back to the user.
            html = (GUI_DIR / "index.html").read_text(encoding="utf-8")
            css = (GUI_DIR / "styles.css").read_text(encoding="utf-8")
            html = re.sub(
                r'<link rel="stylesheet" href="/assets/styles\.css[^"]*">',
                f"<style>\n{css}\n</style>", html, count=1,
            )
            self._send_bytes(html.encode("utf-8"), "text/html; charset=utf-8")
            return
        if parsed.path == "/compact":
            self._send_bytes(
                (GUI_DIR / "compact.html").read_bytes(), "text/html; charset=utf-8"
            )
            return
        if parsed.path == "/api/status":
            self._send_json(APP.measurement.snapshot())
            return
        if parsed.path == "/api/devices":
            busy = not APP.hardware_idle() or JLINK_DRIVER_INSTALL_LOCK.locked()
            # Never open a CDC port while the collector owns it. During a
            # running measurement the list is descriptive only; selection is
            # rejected until the operation is idle.
            result = _cached_devices_payload(busy=busy)
            result["busy"] = busy
            self._send_json(result)
            return
        if parsed.path == "/api/devices/jlink-driver/status":
            self._send_json(_jlink_driver_task_snapshot())
            return
        if parsed.path == "/api/calibration":
            self._send_json(APP.model_payload())
            return
        if parsed.path == "/api/schedule":
            self._send_json(APP.schedule.snapshot())
            return
        if parsed.path == "/api/settings":
            self._send_json(APP.settings.snapshot())
            return
        if parsed.path == "/api/filter":
            self._send_json(APP.filter.snapshot())
            return
        if parsed.path == "/api/plateau":
            self._send_json(APP.plateau.snapshot())
            return
        if parsed.path == "/api/workflow":
            self._send_json(APP.workflow_snapshot())
            return
        if parsed.path == "/api/history":
            self._send_json(APP.history_snapshot())
            return
        if parsed.path == "/api/drift":
            self._send_json(APP.drift_payload())
            return
        if parsed.path == "/api/debug":
            payload = APP.measurement.debug_snapshot()
            # 只作提示:不一致时 UI 提醒"固件里跑的可能不是 GUI 这套参数",
            # 但**不阻止**调试轮(理由见 start_debug_run)。
            payload["settings_applied"] = bool(
                APP.settings.snapshot().get("applied"))
            self._send_json(payload)
            return
        if parsed.path == "/api/health":
            server_port = (
                int(HTTP_SERVER.server_address[1])
                if HTTP_SERVER is not None else DEFAULT_PORT
            )
            self._send_json({
                "ok": True,
                "product": "SensUs-Electrochem-Workstation",
                "health_schema": 2,
                "project": str(PROJECT_DIR),
                "version": __version__,
                "diagnostic_session": DIAGNOSTICS.session_id,
                "backend_pid": os.getpid(),
                "launcher_pid": str(os.environ.get("SENSUS_APP_PID", "")),
                "launch_token": str(
                    os.environ.get("SENSUS_LAUNCH_TOKEN", "")
                ),
                "server_port": server_port,
                "hardware_busy": bool(
                    not APP.hardware_idle()
                    or JLINK_DRIVER_INSTALL_LOCK.locked()
                ),
                "measurement_state": APP.measurement.state,
                "schedule_active": bool(APP.schedule.snapshot()["active"]),
                "settings_state": APP.settings.snapshot()["state"],
                "app_update_busy": bool(APP_UPDATER.busy),
            })
            return
        if parsed.path == "/api/diagnostics":
            raw_limit = parse_qs(parsed.query).get("limit", ["80"])[0]
            try:
                limit = max(1, min(int(raw_limit), 300))
            except (TypeError, ValueError):
                limit = 80
            result = DIAGNOSTICS.snapshot(limit=limit)
            result["runtime"] = _diagnostic_runtime_context()
            self._send_json(result)
            return
        if parsed.path == "/api/diagnostics/download":
            bundle = DIAGNOSTICS.bundle(
                context=_diagnostic_runtime_context(),
                extra_files=_diagnostic_current_run_files(),
            )
            self._send_bytes(
                bundle,
                "application/zip",
                download_name=f"SensUs-diagnostics-{DIAGNOSTICS.session_id}.zip",
            )
            return
        if parsed.path == "/api/frontend":
            self._send_json(FRONTEND_UPDATER.mark_ready())
            return
        if parsed.path == "/api/app-update":
            self._send_json(APP_UPDATER.status(trigger_check=True))
            return
        if parsed.path.startswith("/assets/"):
            name = Path(parsed.path.removeprefix("/assets/")).name
            asset = GUI_DIR / name
            if asset.exists() and asset.is_file():
                content_type = "text/css; charset=utf-8" if asset.suffix == ".css" else "text/javascript; charset=utf-8"
                self._send_bytes(asset.read_bytes(), content_type)
                return
        self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        self._run_request("POST", self._do_POST)

    def _do_POST(self) -> None:
        shutdown_requested = False
        try:
            payload = self._body()
            if SHUTDOWN_INTENT.is_set() and self.path != "/api/shutdown":
                raise RuntimeError("应用正在安全退出，不能再启动新的硬件任务")
            update_blocked_paths = {
                "/api/devices/select", "/api/devices/jlink-driver/install",
                "/api/measurement/start", "/api/range",
                "/api/range/measurement", "/api/range/auto",
                "/api/debug/start", "/api/debug/begin", "/api/debug/cmd",
                "/api/schedule/start", "/api/settings/apply",
            }
            if APP_UPDATER.busy and self.path in update_blocked_paths:
                raise RuntimeError("软件更新正在准备，暂时不能启动测量、烧录或硬件操作")
            driver_blocked_paths = {
                "/api/devices/select", "/api/measurement/start",
                "/api/range", "/api/range/measurement", "/api/range/auto",
                "/api/debug/start", "/api/debug/begin", "/api/debug/cmd",
                "/api/schedule/start", "/api/settings/apply",
            }
            if (
                JLINK_DRIVER_INSTALL_LOCK.locked()
                and self.path in driver_blocked_paths
            ):
                raise RuntimeError("J-Link 驱动正在准备，请等待设备自动恢复")
            if self.path == "/api/frontend/ready":
                result = FRONTEND_UPDATER.mark_ready()
            elif self.path == "/api/diagnostics/client":
                message = str(payload.get("message") or "Frontend error")[
                    :CLIENT_DIAGNOSTIC_MAX_LENGTH
                ]
                diagnostic_id = DIAGNOSTICS.record(
                    "error",
                    "frontend." + str(payload.get("kind") or "error")[:80],
                    message,
                    source=str(payload.get("source") or "")[:500],
                    stack=str(payload.get("stack") or "")[
                        :CLIENT_DIAGNOSTIC_MAX_LENGTH
                    ],
                    page=str(payload.get("page") or "")[:500],
                    context=payload.get("context") or {},
                )
                result = {"ok": True, "diagnostic_id": diagnostic_id}
            elif self.path == "/api/shutdown":
                # Wait for firmware/driver operations to leave their
                # non-interruptible section. Active acquisition is stopped via
                # its normal protocol before the server process may exit.
                result = _release_hardware_for_shutdown()
                shutdown_requested = True
            elif self.path == "/api/app-update/start":
                with APP.operation_lock:
                    _ensure_not_shutting_down()
                    if not APP.hardware_idle():
                        raise RuntimeError("请先停止测量、自动任务或硬件参数更新")
                    result = APP_UPDATER.start_download()
            elif self.path == "/api/app-update/apply":
                with APP.operation_lock:
                    _ensure_not_shutting_down()
                    if not APP.hardware_idle():
                        raise RuntimeError("请先停止测量、自动任务或硬件参数更新")
                    result = APP_UPDATER.begin_install()
                    shutdown_requested = True
            elif self.path == "/api/devices/select":
                with APP.operation_lock:
                    _ensure_not_shutting_down()
                    if not APP.hardware_idle():
                        raise RuntimeError("测量或自动任务运行期间不能切换设备")
                    requested_id = str(payload.get("device_id") or "").strip()
                    if requested_id in {"", "auto"}:
                        _set_device_selection(None)
                        APP.settings.restore_for_transport(HARDWARE_TRANSPORT)
                        result = _cached_devices_payload()
                        result["message"] = "已恢复自动检测"
                    else:
                        with DEVICE_DISCOVERY_LOCK:
                            devices = copy.deepcopy(DEVICE_DISCOVERY_CACHE)
                        device = next(
                            (item for item in devices if item.get("id") == requested_id),
                            None,
                        )
                        if device is None:
                            raise ValueError("设备已断开，请刷新设备列表")
                        if not device.get("selectable"):
                            if device.get("kind") == "usb":
                                raise RuntimeError(
                                    "该 USB 设备尚未同时识别 DATA 和 SMP CDC，请重新插拔后刷新"
                                )
                            raise RuntimeError("该 J-Link 没有可用的探头序列号")
                        _set_device_selection(device)
                        try:
                            _refresh_usb_transport()
                        except Exception:
                            _set_device_selection(None)
                            raise
                        APP.settings.restore_for_transport(HARDWARE_TRANSPORT)
                        result = _devices_payload_from_devices(devices)
                        result["message"] = f"已选择 {device.get('name', '设备')}"
            elif self.path == "/api/devices/jlink-driver/install":
                requested_id = str(payload.get("device_id") or "").strip()
                if not requested_id.startswith("jlink:"):
                    raise ValueError("请选择需要准备的 J-Link")
                result = _start_jlink_driver_task(requested_id)
            elif self.path == "/api/measurement/start":
                with APP.operation_lock:
                    _ensure_not_shutting_down()
                    if APP.schedule.snapshot()["active"]:
                        raise RuntimeError("自动测量运行期间不能插入手动测量")
                    if not APP.settings.snapshot()["applied"]:
                        raise RuntimeError("请先将当前检测条件应用到硬件")
                    result = APP.start_measurement(payload)
            elif self.path == "/api/range":
                result = APP.measurement.send_range(payload)
            elif self.path == "/api/range/measurement":
                result = APP.measurement.switch_to_measurement_range()
            elif self.path == "/api/range/auto":
                result = APP.measurement.set_auto_switch(payload)
            elif self.path == "/api/measurement/stop":
                result = APP.measurement.stop()
            # ── 硬件 DEBUG 模式 ───────────────────────────────────────────
            # 复用 MeasurementController(它本来就是"一次 I-t 测量"这个抽象),
            # 只是打上 debug 标记并**不传 live_raw_path** ⇒ raw 留在 run_dir。
            elif self.path == "/api/debug/start":
                with APP.operation_lock:
                    _ensure_not_shutting_down()
                    result = APP.start_debug_run(payload)
            elif self.path == "/api/debug/stop":
                with APP.measurement.lock:
                    APP.measurement.require_debug_run()
                    result = APP.measurement.stop()
            elif self.path == "/api/debug/begin":
                with APP.measurement.lock:
                    APP.measurement.require_debug_run()
                    result = APP.measurement.begin_debug_measurement(
                        str(payload.get("line", "")))
            elif self.path == "/api/debug/cmd":
                with APP.measurement.lock:
                    APP.measurement.require_debug_run()
                    result = APP.measurement.send_command(
                        str(payload.get("line", ""))
                    )
            elif self.path == "/api/calibration/load":
                result = APP.load_points(str(payload["path"]))
            elif self.path == "/api/calibration/fit":
                result = APP.fit(payload)
            elif self.path == "/api/calibration/validation":
                result = APP.update_validation_points(payload)
            elif self.path == "/api/calibration/validation/delete":
                result = APP.delete_validation_point(payload)
            elif self.path == "/api/calibration/promote-validation":
                result = APP.add_validation_to_calibration(payload)
            elif self.path == "/api/calibration/add-validation":
                result = APP.add_calibration_to_validation(payload)
            elif self.path == "/api/drift/calculate":
                result = APP.calculate_drift(payload)
            elif self.path == "/api/drift/toggle":
                result = APP.toggle_drift(payload)
            elif self.path == "/api/predict":
                result = APP.predict(payload)
            elif self.path == "/api/schedule/start":
                with APP.operation_lock:
                    _ensure_not_shutting_down()
                    result = APP.start_schedule(payload)
            elif self.path == "/api/schedule/stop":
                result = APP.schedule.stop()
            elif self.path == "/api/settings/apply":
                with APP.operation_lock:
                    _ensure_not_shutting_down()
                    if (APP.measurement.is_busy()
                            or APP.schedule.snapshot()["active"]):
                        raise RuntimeError("测量或自动任务运行期间不能修改硬件参数")
                    result = APP.settings.apply(payload)
                    with APP.measurement.lock:
                        APP.measurement.settings = dict(result["settings"])
                        if APP.measurement._rolling_metrics_frozen is None:
                            APP.measurement._reset_live_analysis_locked()
            elif self.path == "/api/filter/apply":
                result = APP.filter.apply(payload)
                APP.schedule.set_filter_config(result["settings"])
                # A filter is host-side and can be changed during acquisition.
                # Keep the run's eventual analysis and the live display on the
                # same configuration; raw acquisition remains untouched.
                if APP.measurement.snapshot()["state"] == "running":
                    APP.measurement.set_filter_config(result["settings"])
            elif self.path == "/api/plateau/apply":
                with APP.operation_lock, APP.measurement.lock:
                    _ensure_not_shutting_down()
                    if APP.schedule.snapshot()["active"]:
                        raise RuntimeError(
                            "自动任务运行期间不能修改自动停止参数"
                        )
                    if (
                        APP.measurement.state == "running"
                        and (
                            APP.measurement.user_stop_requested
                            or APP.measurement.auto_stop_requested
                        )
                    ):
                        raise RuntimeError(
                            "测量正在停止，不能修改自动停止参数"
                        )
                    if (APP.measurement.state == "running"
                            and not APP.measurement.metadata.get("debug")):
                        raise RuntimeError(
                            "正式测量运行期间不能修改自动停止参数"
                        )
                    result = APP.plateau.apply(payload)
                    APP.schedule.set_plateau_config(result["settings"])
                    APP.measurement.set_plateau_config(result["settings"])
            elif self.path == "/api/workspace/browse":
                result = _browse_workspace_directory(
                    str(payload.get("initial_path") or "")
                )
            elif self.path == "/api/workflow/config":
                with APP.operation_lock:
                    _ensure_not_shutting_down()
                    result = APP.configure_workflow(payload)
            elif self.path == "/api/workflow/reset-calibration":
                with APP.operation_lock:
                    _ensure_not_shutting_down()
                    result = APP.reset_calibration(payload)
            elif self.path == "/api/history/register":
                result = APP.register_history(payload)
            elif self.path == "/api/history/import":
                result = APP.import_history(payload)
            elif self.path == "/api/history/open":
                result = APP.open_history(payload)
            elif self.path == "/api/history/favorite":
                result = APP.favorite_history(payload)
            elif self.path == "/api/history/remove":
                result = APP.remove_history(payload)
            elif self.path == "/api/history/curves":
                result = APP.history_curves_snapshot()
            elif self.path == "/api/history/curves/load":
                result = APP.load_history_curves(payload)
            else:
                self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
                return
            self._send_json(result)
            if shutdown_requested:
                _request_server_shutdown()
        except (
            BrokenPipeError, ConnectionResetError, ConnectionAbortedError,
        ):
            raise
        except (ValueError, KeyError, TypeError, OSError, AppUpdateError) as exc:
            payload = {"error": str(exc)}
            diagnostic_id = str(getattr(exc, "diagnostic_id", ""))
            if diagnostic_id:
                payload["diagnostic_id"] = diagnostic_id
            self._send_json(payload, HTTPStatus.BAD_REQUEST)
        except RuntimeError as exc:
            payload = {"error": str(exc)}
            diagnostic_id = str(getattr(exc, "diagnostic_id", ""))
            if diagnostic_id:
                payload["diagnostic_id"] = diagnostic_id
            self._send_json(payload, HTTPStatus.CONFLICT)


def serve(host: str = "127.0.0.1", port: int = DEFAULT_PORT,
          open_browser: bool = False) -> None:
    global HTTP_SERVER, _SHUTDOWN_REQUESTED
    DIAGNOSTICS.install_exception_hooks()
    DIAGNOSTICS.record(
        "info", "application.starting", "Workstation backend is starting",
        host=host,
        port=port,
        project_dir=PROJECT_DIR,
        state_dir=STATE_DIR,
        log_dir=DIAGNOSTICS.log_dir,
        transport=HARDWARE_TRANSPORT,
    )
    # A CDC probe can outlive the browser request during USB re-enumeration.
    # Daemon request threads keep the server responsive and let shutdown
    # finish without waiting for a detached serial read.
    try:
        server = DiagnosticHTTPServer((host, port), RequestHandler)
    except Exception as exc:
        DIAGNOSTICS.exception(
            "application.bind_failed", "Workstation backend could not listen",
            exc, host=host, port=port,
        )
        raise
    with _SHUTDOWN_LOCK:
        HTTP_SERVER = server
        _SHUTDOWN_REQUESTED = False
    SHUTDOWN_INTENT.clear()
    FRONTEND_UPDATER.start(APP.hardware_idle)
    url = f"http://{host}:{server.server_port}/"
    DIAGNOSTICS.record(
        "info", "application.ready", "Workstation backend is ready",
        url=url,
        diagnostic_session=DIAGNOSTICS.session_id,
        transport=_transport_status(),
    )
    print(f"i-t GUI: {url}", flush=True)
    if open_browser:
        threading.Timer(0.35, lambda: webbrowser.open(url)).start()
    # 🔴 必须接 SIGTERM(SIGBREAK on Windows)。Python 对 SIGTERM 的默认动作
    #    是**立刻退出、不跑 finally** ⇒ `pkill -f gui_server` 之后,它起的
    #    collector 与 JLinkExe 会活下来、继续占着探头和 telnet 19021,下一次
    #    启动的 run 连不上就带 traceback 死。2026-08-10 实测踩到:一个孤儿
    #    collector 让新 run 直接 ConnectionResetError,而现场看起来像"探头坏了"。
    #    Windows 没有 SIGTERM,用 CTRL_BREAK_EVENT 代替(taskkill 会发这个)。
    def _graceful(_sig, _frm):
        DIAGNOSTICS.record(
            "info", "application.signal", "Shutdown signal received",
            signal=int(_sig),
        )

        def release_and_shutdown() -> None:
            released = False
            try:
                _release_hardware_for_shutdown(timeout_s=330.0)
                released = True
            except Exception as exc:
                DIAGNOSTICS.exception(
                    "application.signal_release_failed",
                    "Hardware could not be released cleanly after a signal",
                    exc,
                    signal=int(_sig),
                )
            if released:
                server.shutdown()

        threading.Thread(
            target=release_and_shutdown,
            name="signal-safe-shutdown",
            daemon=True,
        ).start()

    _signals = (signal.SIGBREAK, signal.SIGINT) if _IS_WIN else (signal.SIGTERM, signal.SIGINT)  # type: ignore[attr-defined]
    for sig in _signals:
        try:
            signal.signal(sig, _graceful)
        except (ValueError, AttributeError):
            pass   # 非主线程时不给注册,或平台不支持时忽略

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        DIAGNOSTICS.record(
            "info", "application.keyboard_interrupt", "Keyboard interrupt received"
        )
    except Exception as exc:
        DIAGNOSTICS.exception(
            "application.serve_failed", "HTTP serving loop failed", exc,
            url=url,
        )
        raise
    finally:
        FRONTEND_UPDATER.stop()
        APP_UPDATER.stop()
        APP.schedule.stop()
        # 🔴 同步收干净,不能只靠 stop() 里那个 1.5s 延迟线程 —— 进程一退它就没了。
        APP.measurement.stop()
        proc = APP.measurement.process
        if proc is not None and proc.poll() is None:
            MeasurementController._terminate_tree(proc)
            try:
                proc.wait(timeout=6)
            except subprocess.TimeoutExpired:
                MeasurementController._kill_tree(proc)
        APP.measurement.wait_for_completion()
        server.server_close()
        with _SHUTDOWN_LOCK:
            if HTTP_SERVER is server:
                HTTP_SERVER = None
        DIAGNOSTICS.record(
            "info", "application.stopped", "Workstation backend stopped cleanly",
            url=url,
        )
        print("i-t GUI 已退出(采集子进程与 J-Link 已收回)", flush=True)


def main(argv: list[str] | None = None) -> int:
    global HARDWARE_TRANSPORT, HARDWARE_TRANSPORT_REQUESTED
    global SERIAL_DATA_PORT, SERIAL_SMP_PORT
    parser = argparse.ArgumentParser(description="本地 i-t 电化学检测 GUI")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--open-browser", action="store_true")
    parser.add_argument("--transport", choices=("auto", "rtt", "serial"),
                        default=HARDWARE_TRANSPORT,
                        help="auto 自动检测；V4.0 用 rtt；V5.1 用 serial")
    parser.add_argument("--serial-port", default=SERIAL_DATA_PORT,
                        help="V5.1 DATA CDC 路径；auto 可省略")
    parser.add_argument("--smp-port", default=SERIAL_SMP_PORT,
                        help="V5.1 SMP CDC 路径；仅 USB 固件更新需要")
    args = parser.parse_args(argv)
    HARDWARE_TRANSPORT_REQUESTED = args.transport.lower()
    HARDWARE_TRANSPORT = _resolve_hardware_transport(
        args.transport, args.serial_port or ""
    )
    SERIAL_SMP_PORT = str(args.smp_port or "").strip()
    APP.settings.restore_for_transport(HARDWARE_TRANSPORT)
    DIAGNOSTICS.record(
        "info", "transport.resolved", "Hardware transport resolved",
        requested=HARDWARE_TRANSPORT_REQUESTED,
        selected=HARDWARE_TRANSPORT,
        serial_data_port=SERIAL_DATA_PORT,
        serial_smp_port=SERIAL_SMP_PORT,
    )
    serve(args.host, args.port, args.open_browser)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
