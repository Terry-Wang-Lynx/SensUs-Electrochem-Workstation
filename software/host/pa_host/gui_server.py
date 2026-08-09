"""Local browser GUI for the 10 Hz electrochemical i-t workflow.

The GUI deliberately uses only Python's standard library on the server side;
the browser renders the plots with a small canvas-based frontend.  This keeps
the one-click tool usable on the lab Mac without installing a desktop GUI
toolkit.  Hardware acquisition is still delegated to ``pa_host.it_tool`` so
the tested RTT/J-Link path remains the single source of truth.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .it import (
    CalibrationPoint,
    CalibrationModel,
    fit_calibration,
    load_calibration_points,
    load_model,
    load_run_csv,
    resample_run_10hz,
    save_model,
    save_summary,
    summarize_run,
)
from .collect import find_rtt_address
from .cv import (
    export_cv_csv,
    load_cv_run,
    plot_cv,
    save_cv_summary,
    summarize_cv,
)


PACKAGE_DIR = Path(__file__).resolve().parent


def _find_project_dir() -> Path:
    configured = os.environ.get("SENSUS_PROJECT_DIR")
    candidates = [Path(configured).expanduser()] if configured else []
    candidates.extend([Path.cwd(), *Path.cwd().parents, PACKAGE_DIR, *PACKAGE_DIR.parents])
    for candidate in candidates:
        if (candidate / "software" / "firmware" / "CMakeLists.txt").exists():
            return candidate.resolve()
    return PACKAGE_DIR.parents[2]


PROJECT_DIR = _find_project_dir()
GUI_DIR = PACKAGE_DIR / "gui"
RUNS_DIR = PROJECT_DIR / "measurements" / "gui_runs"
DEFAULT_PORT = 8765
MEASUREMENT_DURATION_S = 180.0
COLLECTOR_DURATION_S = 190.0
TARGET_RATE_HZ = 10.0
FIT_WINDOW_S = 20.0
FIRMWARE_ELF = PROJECT_DIR / "software" / "firmware" / "build" / "firmware" / "zephyr" / "zephyr.elf"
FIRMWARE_HEX = PROJECT_DIR / "software" / "firmware" / "build" / "firmware" / "zephyr" / "zephyr.hex"
FIRMWARE_CONFIG = PROJECT_DIR / "software" / "firmware" / "src" / "measurement_config.h"
SETTINGS_PATH = PROJECT_DIR / "measurements" / "gui_settings.json"
WORKFLOW_PATH = PROJECT_DIR / "measurements" / "gui_workflow.json"
DEFAULT_SAVE_DIR = PROJECT_DIR / "measurements" / "experiment_data"
JLINK_SERIAL = os.environ.get("SENSUS_JLINK_SERIAL", "29734569")
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


def _release_stale_measurement_bridge() -> None:
    """Gracefully release an orphaned workstation OpenOCD RTT bridge."""
    if not _port_accepts_connections(19021):
        return
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


def _now_id(prefix: str) -> str:
    return f"{prefix}_{time.strftime('%Y%m%d_%H%M%S')}"


class SettingsController:
    """Validate method parameters and build/flash matching firmware."""

    DEFAULTS = {
        "method": "it",
        "initial_potential_v": 0.2,
        "potential_v": 0.2,
        "prestep_s": 0.0,
        "duration_s": 180.0,
        "target_rate_hz": 10.0,
        "sens_period_code": 0,
        "fit_window_s": 20.0,
        "fsr_nA": 40000,
        "offset_nA": 20000,
        "offset_mode": "50pct",
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
        self.settings = dict(self.DEFAULTS)
        loaded_saved = False
        firmware_verified = False
        if SETTINGS_PATH.exists():
            try:
                saved = json.loads(SETTINGS_PATH.read_text())
                if not isinstance(saved, dict):
                    raise ValueError("settings file must contain an object")
                saved_settings = saved.get("settings", saved)
                self.settings = self.validate(saved_settings)
                loaded_saved = True
                expected_hash = str(saved.get("firmware_sha256") or "")
                firmware_verified = bool(
                    expected_hash and expected_hash == self._firmware_hash()
                )
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                pass
        self.applied = firmware_verified
        self.state = "applied" if firmware_verified else "not_applied"
        if firmware_verified:
            self.message = "已恢复与当前固件一致的硬件参数"
        elif loaded_saved:
            self.message = "已恢复参数，但当前固件尚未烧录确认"
        else:
            self.message = "参数尚未应用到硬件"
        self.error = ""

    @staticmethod
    def _firmware_hash() -> str:
        if not FIRMWARE_HEX.exists():
            return ""
        return hashlib.sha256(FIRMWARE_HEX.read_bytes()).hexdigest()

    @staticmethod
    def same_analysis_protocol(first: dict[str, Any], second: dict[str, Any]) -> bool:
        """Compare settings that determine sampled IT data and its analysis.

        Startup potential/hold are not sampled. They remain in run metadata for
        traceability but do not invalidate a curve whose sampled potential,
        duration, rate, fit window and current range are unchanged.
        """
        if first.get("method", "it") != second.get("method", "it"):
            return False
        ignored = {
            "initial_potential_v", "prestep_s", "cv_low_v", "cv_high_v",
            "cv_scan_rate_v_s", "cv_cycles", "cv_step_v", "cv_quiet_s",
        }
        return (
            {key: value for key, value in first.items() if key not in ignored}
            == {key: value for key, value in second.items() if key not in ignored}
        )

    @classmethod
    def validate(cls, payload: dict[str, Any]) -> dict[str, Any]:
        merged = {**cls.DEFAULTS, **payload}
        method = str(merged["method"]).lower()
        initial_potential_v = float(merged["initial_potential_v"])
        potential_v = float(merged["potential_v"])
        prestep_s = float(merged["prestep_s"])
        duration_s = float(merged["duration_s"])
        target_rate_hz = float(merged["target_rate_hz"])
        sens_period_code = int(merged["sens_period_code"])
        fit_window_s = float(merged["fit_window_s"])
        cv_low_v = float(merged["cv_low_v"])
        cv_high_v = float(merged["cv_high_v"])
        cv_scan_rate_v_s = float(merged["cv_scan_rate_v_s"])
        cv_cycles = int(merged["cv_cycles"])
        cv_step_v = float(merged["cv_step_v"])
        cv_quiet_s = float(merged["cv_quiet_s"])
        cv_eis_fsr_uA = int(merged["cv_eis_fsr_uA"])
        fsr_nA = int(merged["fsr_nA"])
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
            if not 0 <= prestep_s <= 300:
                raise ValueError("阶跃前保持时间必须在 0 至 300 秒之间")
            if not 10 <= duration_s <= 3600:
                raise ValueError("I-T 时长必须在 10 至 3600 秒之间")
        else:
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
        if method == "it" and not 1 <= fit_window_s <= duration_s:
            raise ValueError("拟合窗口必须在 1 秒与测量时长之间")
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
            "prestep_s": round(prestep_s, 3),
            "duration_s": round(duration_s, 3),
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
        """Keep legacy I-T common mode unless a positive rail margin is needed."""
        if settings["method"] == "cv":
            return 800
        highest_potential_mv = max(
            int(round(settings["initial_potential_v"] * 1000)),
            int(round(settings["potential_v"] * 1000)),
        )
        return 800 if highest_potential_mv > 390 else 400

    def apply(self, payload: dict[str, Any]) -> dict[str, Any]:
        settings = self.validate(payload)
        with self.lock:
            self.state = "applying"
            self.message = "正在编译并写入硬件参数"
            self.error = ""
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
            f"#define GUI_WP_FSR {FSR_OPTIONS.get(settings['fsr_nA'], 'MAX30131_FSR_2000NA')}\n"
            f"#define GUI_WP_OFFSET_SEL {OFFSET_OPTIONS[settings['offset_mode']][0]}\n"
            f"#define GUI_IT_USE_EIS {1 if settings['fsr_nA'] in IT_WIDE_FSR_OPTIONS else 0}\n"
            f"#define GUI_IT_EIS_FSR {IT_WIDE_FSR_OPTIONS.get(settings['fsr_nA'], 'MAX30131_EIS_FSR_40UA')}\n"
            f"#define GUI_IT_SAMPLE_INTERVAL_MS {it_sample_interval_ms}U\n"
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
            _release_stale_measurement_bridge()
            FIRMWARE_CONFIG.write_text(header)
            build = (
                "source ~/ncs/zephyr/zephyr-env.sh && "
                "west build -b pa_converter_v40 -d software/firmware/build "
                "software/firmware -- -DBOARD_ROOT=$PWD/software/firmware "
                "-DDTS_ROOT=$PWD/software/firmware"
            )
            subprocess.run(
                ["/bin/zsh", "-lc", build], cwd=PROJECT_DIR,
                check=True, capture_output=True, text=True,
            )
            subprocess.run([
                "openocd", "-f", "interface/jlink.cfg",
                "-c", f"adapter serial {JLINK_SERIAL}",
                "-c", "transport select swd",
                "-f", "target/nrf52.cfg",
                "-c", f"program {FIRMWARE_HEX} verify reset exit",
            ], cwd=PROJECT_DIR, check=True, capture_output=True, text=True)
            SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
            SETTINGS_PATH.write_text(json.dumps({
                "settings": settings,
                "firmware_sha256": self._firmware_hash(),
            }, indent=2, ensure_ascii=False))
        except (subprocess.CalledProcessError, OSError) as exc:
            output = getattr(exc, "stderr", "") or getattr(exc, "stdout", "") or str(exc)
            detail = output.strip().splitlines()
            with self.lock:
                self.state = "error"
                self.error = detail[-1] if detail else "固件编译或烧录失败"
                self.message = "参数应用失败"
            raise RuntimeError(self.error) from exc
        with self.lock:
            self.settings = settings
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
                    else int(round(self.settings["duration_s"]
                                   * self.settings["target_rate_hz"]))
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
        self.summary_path: Path | None = None
        self.plot_path: Path | None = None
        self.started_at: float | None = None
        self.finished_at: float | None = None
        self.process: subprocess.Popen[str] | None = None
        self.summary: dict[str, Any] | None = None
        self.workflow_result: dict[str, Any] | None = None
        self.thread: threading.Thread | None = None
        self.metadata: dict[str, Any] = {}
        self.on_complete: Any = None
        self.completion_hook: Any = None
        self.settings = dict(SettingsController.DEFAULTS)
        self.bridge_process: subprocess.Popen[str] | None = None
        self.bridge_log_handle: Any = None
        self.user_stop_requested = False
        self._reset_data_cache()

    def _reset_data_cache(self) -> None:
        self._data_cache_path: Path | None = None
        self._data_cache_position = 0
        self._data_cache_pending = ""
        self._data_cache_header: list[str] | None = None
        self._data_cache_first_dev_ms: float | None = None
        self._data_cache: dict[str, list[Any]] = {
            "time_s": [], "current_nA": [], "valid": [],
        }

    def start(self, metadata: dict[str, Any] | None = None,
              on_complete: Any = None,
              settings: dict[str, Any] | None = None) -> dict[str, Any]:
        with self.lock:
            if self.state == "running":
                raise RuntimeError("已有测量正在运行")
            self.settings = SettingsController.validate(settings or self.settings)
            method = self.settings["method"]
            RUNS_DIR.mkdir(parents=True, exist_ok=True)
            self.run_id = _now_id(method)
            self.run_dir = RUNS_DIR / self.run_id
            self.run_dir.mkdir(parents=True, exist_ok=False)
            self.metadata = dict(metadata or {})
            live_raw_path = str(self.metadata.get("live_raw_path") or "")
            self.raw_path = Path(live_raw_path) if live_raw_path else self.run_dir / "raw.csv"
            self.raw_path.parent.mkdir(parents=True, exist_ok=True)
            self.raw_log = self.run_dir / "rtt.log"
            self.resampled_path = self.run_dir / (
                "cv.csv" if method == "cv" else "resampled_10hz.csv"
            )
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
            self._reset_data_cache()
            self.state = "running"
            self.message = f"已启动硬件 {method.upper()} 测量，等待 RTT 数据"
            self.on_complete = on_complete

            env = os.environ.copy()
            host_dir = str(PROJECT_DIR / "software" / "host")
            env["PYTHONPATH"] = host_dir + os.pathsep + env.get("PYTHONPATH", "")
            command = [
                sys.executable,
                "-m",
                "pa_host.it_tool",
                "measure",
                "--socket",
                "127.0.0.1:19021",
                "--out",
                str(self.raw_path),
                "--raw-log",
                str(self.raw_log),
                "--trigger",
                "FRESH_START",
                "--duration",
                str(self.settings["prestep_s"] + self.settings["duration_s"] + 5),
                "--idle-timeout",
                "25",
            ]
            if method == "cv":
                command.append("--cv")
            log_handle = (self.run_dir / "collector.log").open("w", buffering=1)
            try:
                self._start_bridge()
                self.process = subprocess.Popen(
                    command,
                    cwd=PROJECT_DIR,
                    env=env,
                    stdout=log_handle,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
            except Exception:
                log_handle.close()
                self._stop_bridge()
                self.state = "error"
                self.error = "无法启动采集进程"
                raise
            self.thread = threading.Thread(target=self._watch, args=(log_handle,), daemon=True)
            self.thread.start()
            return self.snapshot()

    def _start_bridge(self) -> None:
        assert self.run_dir is not None
        rtt_address = find_rtt_address(FIRMWARE_ELF)
        self.bridge_log_handle = (self.run_dir / "openocd.log").open("w", buffering=1)
        command = [
            "openocd", "-f", "interface/jlink.cfg",
            "-c", f"adapter serial {JLINK_SERIAL}",
            "-c", "transport select swd",
            "-c", "adapter speed 4000",
            "-f", "target/nrf52.cfg",
            "-c", "init",
            "-c", f'rtt setup 0x{rtt_address:08x} 0x800 "SEGGER RTT"',
            "-c", "rtt server start 19021 0",
        ]
        self.bridge_process = subprocess.Popen(
            command, cwd=PROJECT_DIR, stdout=self.bridge_log_handle,
            stderr=subprocess.STDOUT, text=True,
        )
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            if self.bridge_process.poll() is not None:
                raise RuntimeError("OpenOCD 硬件桥启动失败")
            try:
                with socket.create_connection(("127.0.0.1", 4444), timeout=0.4):
                    break
            except OSError:
                time.sleep(0.2)
        else:
            raise RuntimeError("等待 OpenOCD 硬件桥超时")
        self._openocd_telnet(
			'rtt stop\n'
			f'rtt setup 0x{rtt_address:08x} 0x800 "SEGGER RTT"\n'
			'rtt start\nexit\n'
        )

    @staticmethod
    def _openocd_telnet(commands: str) -> None:
        with socket.create_connection(("127.0.0.1", 4444), timeout=3) as connection:
            connection.sendall(commands.encode("ascii"))
            connection.settimeout(0.4)
            try:
                while connection.recv(4096):
                    pass
            except (TimeoutError, socket.timeout):
                pass

    def _stop_bridge(self) -> None:
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
            process = self.process
            if self.state != "running" or process is None:
                return self.snapshot()
            self.user_stop_requested = True
            self.message = "正在停止硬件测量"
            try:
                with socket.create_connection(("127.0.0.1", 19021), timeout=1) as conn:
                    conn.sendall(b"STOP\n")
            except OSError:
                process.terminate()
            else:
                threading.Thread(
                    target=self._terminate_if_running,
                    args=(process, 1.5), daemon=True,
                ).start()
            return self.snapshot()

    @staticmethod
    def _terminate_if_running(process: subprocess.Popen[str], delay_s: float) -> None:
        time.sleep(delay_s)
        if process.poll() is None:
            process.terminate()

    def _watch(self, log_handle: Any) -> None:
        assert self.process is not None
        process = self.process
        while process.poll() is None:
            with self.lock:
                self.message = self._progress_message()
            time.sleep(0.8)
        return_code = process.wait()
        log_handle.close()
        self._stop_bridge()
        with self.lock:
            self.finished_at = time.time()
            if return_code != 0:
                if self.user_stop_requested and return_code in (3, -15):
                    self.state = "idle"
                    self.error = ""
                    self.message = "测量已停止"
                    self._notify_complete()
                    return
                self.state = "error"
                self.error = (
                    f"采集进程退出码 {return_code}。请查看 {self.run_dir / 'collector.log'}"
                )
                self.message = "测量失败"
                self._notify_complete()
                return
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
                        duration_s=self.settings["duration_s"],
                        target_rate_hz=self.settings["target_rate_hz"],
                    )
                    summary = summarize_run(
                        self.resampled_path, window_s=self.settings["fit_window_s"]
                    )
                    save_summary(summary, self.summary_path)
                    self.message = "测量完成，已生成 10 Hz 数据和末段汇总"
                self.summary = _json_safe(asdict(summary))
                self.state = "completed"
            except Exception as exc:  # keep the raw run even if analysis fails
                self.state = "error"
                self.error = f"测量已落盘，但分析失败：{exc}"
                self.message = "分析失败，原始数据仍已保存"
        self._notify_complete()

    def _notify_complete(self) -> None:
        snapshot = self.snapshot()
        callbacks = (self.completion_hook, self.on_complete)
        for callback in callbacks:
            if callback is not None:
                try:
                    callback(snapshot)
                except Exception:
                    pass

    def set_workflow_result(self, result: dict[str, Any]) -> None:
        with self.lock:
            self.workflow_result = _json_safe(result)
            saved = result.get("data_path") or result.get("raw_path")
            if self.state == "completed" and saved:
                self.message = f"测量完成并已自动保存：{saved}"

    def _progress_message(self) -> str:
        count = len(self._data()["time_s"])
        return f"正在采集：已收到 {count} 个原生点"

    def _data(self) -> dict[str, Any]:
        if not self.raw_path or not self.raw_path.exists():
            return {"time_s": [], "current_nA": [], "valid": []}
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
                if self.settings["method"] == "cv":
                    self._data_cache.update({
                        "potential_v": [], "cycle": [], "direction": [],
                    })
            with self.raw_path.open(newline="") as handle:
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
                first_dev_ms = (
                    dev_ms if self._data_cache_first_dev_ms is None
                    else self._data_cache_first_dev_ms
                )
                current_nA = float(row["fa_fw"]) / 1_000_000
                valid = (
                    int(row.get("sat") or 0) == 0
                    and int(row.get("ovf") or 0) == 0
                )
                if self.settings["method"] == "cv":
                    potential_v = float(row["potential_mv"]) / 1000
                    cycle = int(row["cycle"])
                    direction = int(row["direction"])
                self._data_cache_first_dev_ms = first_dev_ms
                self._data_cache["time_s"].append(
                    (dev_ms - first_dev_ms) / 1000
                )
                self._data_cache["current_nA"].append(current_nA)
                self._data_cache["valid"].append(valid)
                if self.settings["method"] == "cv":
                    self._data_cache["potential_v"].append(potential_v)
                    self._data_cache["cycle"].append(cycle)
                    self._data_cache["direction"].append(direction)
            except (KeyError, TypeError, ValueError, csv.Error):
                continue
        return self._data_cache

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            data = self._data()
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
                "state": self.state,
                "message": self.message,
                "error": self.error,
                "run_id": self.run_id,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "run_dir": str(self.run_dir) if self.run_dir else "",
                "raw_path": str(self.raw_path) if self.raw_path else "",
                "resampled_path": str(self.resampled_path) if self.resampled_path else "",
                "summary_path": str(self.summary_path) if self.summary_path else "",
                "plot_path": str(self.plot_path) if self.plot_path else "",
                "summary": self.summary,
                "workflow_result": self.workflow_result,
                "metadata": self.metadata,
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
        self.completed_runs = 0
        self.next_run_at: float | None = None
        self.sample_prefix = "自动样品"
        self.known_concentration_um: float | None = None
        self.sample_role = "test"
        self.save_dir = ""
        self.message = "自动测量未启动"
        self.history: list[dict[str, Any]] = []
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.settings = dict(SettingsController.DEFAULTS)
        self.metadata_hook: Any = None

    def start(self, payload: dict[str, Any]) -> dict[str, Any]:
        interval_minutes = float(payload.get("interval_minutes", 5))
        settings = SettingsController.validate(payload.get("settings", {}))
        sample_role = (
            "cv" if settings["method"] == "cv"
            else str(payload.get("sample_role") or "test")
        )
        raw_concentration = payload.get("known_concentration_um")
        known_concentration = (
            None if raw_concentration in (None, "") else float(raw_concentration)
        )
        if sample_role not in {"calibration", "stabilization", "test", "cv"}:
            raise ValueError("自动任务类型必须是标定、稳定化、测试或 CV")
        if sample_role == "calibration" and known_concentration is None:
            raise ValueError("自动标定任务必须填写已知浓度")
        minimum_interval_s = settings["prestep_s"] + settings["duration_s"] + 10
        if interval_minutes * 60 < minimum_interval_s:
            raise ValueError(
                f"当前测量条件要求间隔至少 {minimum_interval_s / 60:.2f} 分钟"
            )
        with self.lock:
            if self.active:
                raise RuntimeError("自动测量已经在运行")
            if self.measurement.state == "running":
                raise RuntimeError("请等待当前手动测量结束")
            self.active = True
            self.interval_s = interval_minutes * 60
            self.max_runs = max(0, int(payload.get("max_runs", 0)))
            self.total_minutes = max(0.0, float(payload.get("total_minutes", 0)))
            self.completed_runs = 0
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
            self.stop_event.clear()
            self.thread = threading.Thread(target=self._loop, daemon=True)
            self.thread.start()
            return self.snapshot()

    def stop(self) -> dict[str, Any]:
        with self.lock:
            self.active = False
            self.next_run_at = None
            self.stop_at = None
            self.message = "自动测量已停止；正在进行的测量会正常完成"
            self.stop_event.set()
            return self.snapshot()

    def _loop(self) -> None:
        while not self.stop_event.wait(0.25):
            with self.lock:
                if not self.active:
                    return
                if self.stop_at is not None and time.time() >= self.stop_at:
                    self.active = False
                    self.next_run_at = None
                    self.message = f"稳定化阶段已结束，共完成 {self.completed_runs} 次测量"
                    self.stop_event.set()
                    return
                due = self.next_run_at is not None and time.time() >= self.next_run_at
            if not due:
                continue
            if self.measurement.state == "running":
                continue
            with self.lock:
                if (self.stop_at is not None
                        and time.time() + self.settings["prestep_s"]
                        + self.settings["duration_s"] + 5 > self.stop_at):
                    self.active = False
                    self.next_run_at = None
                    self.message = f"计划时段已结束，共完成 {self.completed_runs} 次测量"
                    self.stop_event.set()
                    return
                run_number = self.completed_runs + 1
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
                self.message = f"正在执行第 {run_number} 次自动测量"
            try:
                if self.metadata_hook is not None:
                    metadata = self.metadata_hook(metadata)
                self.measurement.start(
                    metadata=metadata, on_complete=self._completed,
                    settings=self.settings,
                )
            except RuntimeError:
                continue
            except Exception as exc:
                with self.lock:
                    self.message = f"自动测量启动失败：{exc}"
                    self.active = False
                return

    def _completed(self, run: dict[str, Any]) -> None:
        with self.lock:
            self.completed_runs += 1
            self.history.insert(0, {
                "run_id": run.get("run_id"),
                "finished_at": run.get("finished_at"),
                "state": run.get("state"),
                "summary": run.get("summary"),
                "metadata": run.get("metadata"),
                "run_dir": run.get("run_dir"),
            })
            self.history = self.history[:100]
            if self.max_runs and self.completed_runs >= self.max_runs:
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
                "completed_runs": self.completed_runs,
                "next_run_at": self.next_run_at,
                "sample_prefix": self.sample_prefix,
                "known_concentration_um": self.known_concentration_um,
                "sample_role": self.sample_role,
                "save_dir": self.save_dir,
                "message": self.message,
                "history": self.history,
                "settings": self.settings,
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
        self.settings = SettingsController()
        self.measurement = MeasurementController()
        self.schedule = ScheduleController(self.measurement)
        self.measurement.settings = self.settings.snapshot()["settings"]
        self.save_dir = DEFAULT_SAVE_DIR
        self.model: CalibrationModel | None = None
        self.model_path: Path | None = None
        self.model_settings: dict[str, Any] | None = None
        self.calibration_settings: dict[str, Any] | None = None
        self.points: list[CalibrationPoint] = []
        self.point_records: list[dict[str, Any]] = []
        self.selected_point_ids: list[str] = []
        self.model_created_at: float | None = None
        self.records: list[dict[str, Any]] = []
        self.drift = self._empty_drift()
        self.latest_workflow_result: dict[str, Any] | None = None
        if WORKFLOW_PATH.exists():
            try:
                saved = json.loads(WORKFLOW_PATH.read_text())
                self.save_dir = self._resolve_save_dir(str(saved.get("save_dir", "")))
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                self.save_dir = DEFAULT_SAVE_DIR
        self._load_workspace()
        self.schedule.metadata_hook = self._prepare_export_metadata
        self.measurement.completion_hook = self._measurement_completed

    @staticmethod
    def _resolve_save_dir(value: str) -> Path:
        raw = os.path.expandvars(os.path.expanduser(value.strip()))
        path = Path(raw) if raw else DEFAULT_SAVE_DIR
        if not path.is_absolute():
            path = PROJECT_DIR / path
        return path.resolve()

    @staticmethod
    def _safe_filename(value: str) -> str:
        cleaned = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", value.strip())
        cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
        return cleaned or "sample"

    @staticmethod
    def _concentration_token(value: Any) -> str:
        if value in (None, ""):
            return "unknown"
        return f"{float(value):g}uM"

    def _workspace_paths(self) -> dict[str, Path]:
        return {
            "points": self.save_dir / "calibration-points.csv",
            "model": self.save_dir / "calibration-model.json",
            "settings": self.save_dir / "calibration-settings.json",
            "selection": self.save_dir / "calibration-selection.json",
            "index": self.save_dir / "measurement-index.csv",
            "drift": self.save_dir / "calibration-drift.json",
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

    def _load_workspace(self) -> None:
        self.save_dir.mkdir(parents=True, exist_ok=True)
        paths = self._workspace_paths()
        with self.lock:
            self.points = []
            self.model = None
            self.model_path = None
            self.model_settings = None
            self.calibration_settings = None
            self.point_records = []
            self.selected_point_ids = []
            self.model_created_at = None
            self.records = []
            self.drift = self._empty_drift()
            self.latest_workflow_result = None
            if paths["points"].exists():
                try:
                    with paths["points"].open(newline="") as handle:
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
                    saved_calibration_settings = json.loads(paths["settings"].read_text())
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
            if paths["model"].exists() and self.calibration_settings is not None:
                try:
                    self.model = load_model(paths["model"])
                    self.model_path = paths["model"]
                    self.model_settings = dict(self.calibration_settings)
                except (OSError, ValueError, json.JSONDecodeError):
                    self.model = None
            if self.model is not None:
                if paths["selection"].exists():
                    try:
                        selection = json.loads(paths["selection"].read_text())
                        known_ids = {record["point_id"] for record in self.point_records}
                        self.selected_point_ids = [
                            str(point_id) for point_id in selection.get("selected_point_ids", [])
                            if str(point_id) in known_ids
                        ]
                        raw_created_at = selection.get("created_at")
                        self.model_created_at = (
                            float(raw_created_at) if raw_created_at is not None else None
                        )
                    except (OSError, ValueError, TypeError, json.JSONDecodeError):
                        self.selected_point_ids = []
                if not self.selected_point_ids:
                    # Legacy models used every saved point. Preserve that model's scope.
                    self.selected_point_ids = [
                        record["point_id"] for record in self.point_records
                    ]
            if paths["index"].exists():
                try:
                    with paths["index"].open(newline="") as handle:
                        self.records = list(csv.DictReader(handle))[-100:]
                except OSError:
                    self.records = []
            if paths["drift"].exists():
                try:
                    saved_drift = json.loads(paths["drift"].read_text())
                    if isinstance(saved_drift, dict):
                        self.drift = {**self._empty_drift(), **saved_drift}
                except (OSError, ValueError, TypeError, json.JSONDecodeError):
                    self.drift = self._empty_drift()

    def configure_workflow(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.measurement.state == "running" or self.schedule.snapshot()["active"]:
            raise RuntimeError("测量或自动任务运行期间不能切换工作目录")
        path = self._resolve_save_dir(str(payload.get("save_dir", "")))
        try:
            path.mkdir(parents=True, exist_ok=True)
            probe = path / ".sensus-write-test"
            probe.write_text("ok")
            probe.unlink()
        except OSError as exc:
            raise ValueError(f"保存目录不可写：{exc}") from exc
        with self.lock:
            self.save_dir = path
            WORKFLOW_PATH.parent.mkdir(parents=True, exist_ok=True)
            WORKFLOW_PATH.write_text(json.dumps({"save_dir": str(path)}, indent=2,
                                                ensure_ascii=False))
        self._load_workspace()
        return self.workflow_snapshot()

    def workflow_snapshot(self) -> dict[str, Any]:
        current_settings = self.settings.snapshot()["settings"]
        is_it = current_settings.get("method", "it") == "it"
        settings_match = (
            self.calibration_settings is None
            or SettingsController.same_analysis_protocol(
                self.calibration_settings, current_settings
            )
        )
        calibration_ready = (
            is_it
            and self.model is not None
            and self.model_settings is not None
            and SettingsController.same_analysis_protocol(
                self.model_settings, current_settings
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
        return _json_safe({
            "save_dir": str(self.save_dir),
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

    def start_measurement(self, payload: dict[str, Any]) -> dict[str, Any]:
        requested_dir = str(payload.get("save_dir", "")).strip()
        if requested_dir and self._resolve_save_dir(requested_dir) != self.save_dir:
            self.configure_workflow({"save_dir": requested_dir})
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
        if concentration is not None and concentration < 0:
            raise ValueError("浓度不能为负数")
        if role == "calibration" and concentration is None:
            raise ValueError("标定样品必须填写已知浓度")
        if (role == "calibration" and self.points and self.calibration_settings is not None
                and not SettingsController.same_analysis_protocol(
                    self.calibration_settings, current_settings
                )):
            raise RuntimeError("当前 IT 条件与该目录中的标定点不同，请选择新的保存目录")
        if role in {"stabilization", "test"} and not self.workflow_snapshot()["calibration_ready"]:
            raise RuntimeError("请先选择标定点并生成当前 IT 条件下的测试曲线")
        metadata = {
            **payload,
            "sample_name": sample_name,
            "known_concentration_um": concentration,
            "sample_role": role,
            "save_dir": str(self.save_dir),
            "source": payload.get("source") or "manual_gui",
        }
        metadata = self._prepare_export_metadata(metadata)
        return self.measurement.start(metadata=metadata, settings=current_settings)

    def _prepare_export_metadata(self, metadata: dict[str, Any]) -> dict[str, Any]:
        prepared = dict(metadata)
        sample_name = str(prepared.get("sample_name") or "sample")
        concentration = prepared.get("known_concentration_um")
        with self.lock:
            stem = self._reserve_export_stem(sample_name, concentration)
            prepared["export_stem"] = stem
            prepared["live_raw_path"] = str(self.save_dir / f"{stem}-raw.csv")
        return prepared

    def _reserve_export_stem(self, sample_name: str, concentration: Any) -> str:
        root = (
            f"{self._safe_filename(sample_name)}-"
            f"{self._concentration_token(concentration)}"
        )
        candidate = root
        number = 2
        while any((self.save_dir / f"{candidate}{suffix}").exists()
                  for suffix in (".csv", "-raw.csv", "-summary.json", ".png")):
            candidate = f"{root}-r{number}"
            number += 1
        return candidate

    def _save_calibration_points(self) -> None:
        path = self._workspace_paths()["points"]
        with path.open("w", newline="") as handle:
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

    def _append_record(self, result: dict[str, Any]) -> None:
        path = self._workspace_paths()["index"]
        fields = [
            "finished_at", "run_id", "sample_name", "sample_role",
            "known_concentration_um", "steady_current_nA",
            "predicted_concentration_um", "state", "data_path", "raw_path",
        ]
        row = {key: result.get(key, "") for key in fields}
        new_file = not path.exists() or path.stat().st_size == 0
        with path.open("a", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            if new_file:
                writer.writeheader()
            writer.writerow(row)
        self.records.append({key: str(row.get(key, "")) for key in fields})
        self.records = self.records[-100:]

    def _save_drift(self) -> None:
        self._workspace_paths()["drift"].write_text(
            json.dumps(_json_safe(self.drift), indent=2, ensure_ascii=False)
        )

    def _stabilization_records(self) -> list[dict[str, Any]]:
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
            run_id = str(row.get("run_id") or "")
            raw_concentration = row.get("known_concentration_um")
            try:
                concentration = (
                    None if raw_concentration in (None, "") else float(raw_concentration)
                )
            except (TypeError, ValueError):
                concentration = None
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
        raw_concentration = payload.get("known_concentration_um")
        concentration = (
            None if raw_concentration in (None, "") else float(raw_concentration)
        )
        if concentration is not None and concentration < 0:
            raise ValueError("稳定化溶液浓度不能为负数")
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
        }
        try:
            with self.lock:
                requested_dir = metadata.get("save_dir")
                if requested_dir:
                    self.save_dir = self._resolve_save_dir(str(requested_dir))
                    self.save_dir.mkdir(parents=True, exist_ok=True)
                stem = str(metadata.get("export_stem") or "")
                if not stem:
                    stem = self._reserve_export_stem(sample_name, concentration)
                raw_source = Path(str(run.get("raw_path") or ""))
                data_source = Path(str(run.get("resampled_path") or ""))
                raw_target = self.save_dir / f"{stem}-raw.csv"
                data_target = self.save_dir / f"{stem}.csv"
                summary_target = self.save_dir / f"{stem}-summary.json"
                plot_target = self.save_dir / f"{stem}.png"
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

                steady = result["steady_current_nA"]
                if run.get("state") == "completed" and steady is not None:
                    if role == "calibration" and concentration is not None:
                        if self.calibration_settings is None:
                            self.calibration_settings = SettingsController.validate(
                                dict(run["settings"])
                            )
                            self._workspace_paths()["settings"].write_text(
                                json.dumps(self.calibration_settings, indent=2,
                                           ensure_ascii=False)
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
                            "current_nA": float(steady),
                            "data_path": result.get("data_path", ""),
                        })
                        self._sync_points()
                        self._save_calibration_points()
                        result["calibration_points"] = len(self.points)
                        result["candidate_added"] = True
                        result["calibration_ready"] = (
                            self.model is not None
                            and self.model_settings is not None
                            and SettingsController.same_analysis_protocol(
                                self.model_settings, self.settings.snapshot()["settings"]
                            )
                        )
                    elif role == "test" and self.model is not None:
                        effective_current = float(steady) - self._effective_bias_nA()
                        result["predicted_concentration_um"] = float(
                            self.model.predict_concentration(effective_current)
                        )

                exported_summary = {
                    **(run.get("summary") or {}),
                    "sample_name": sample_name,
                    "sample_role": role,
                    "known_concentration_um": concentration,
                    "predicted_concentration_um": result["predicted_concentration_um"],
                    "measurement_settings": run.get("settings"),
                    "source_run_dir": run.get("run_dir"),
                    "saved_data_path": result.get("data_path", ""),
                    "saved_raw_path": result.get("raw_path", ""),
                    "calibration_model_path": (
                        str(self.model_path) if self.model_path else ""
                    ),
                    "calibration_model_created_at": self.model_created_at,
                    "calibration_selected_point_ids": self.selected_point_ids,
                    "calibration_model": (
                        self.model.to_json() if self.model is not None else None
                    ),
                    "drift_correction": self.drift_payload(),
                }
                summary_target.write_text(json.dumps(_json_safe(exported_summary), indent=2,
                                                     ensure_ascii=False))
                result["summary_path"] = str(summary_target)
                self._append_record(result)
                self.latest_workflow_result = dict(result)
        except Exception as exc:
            result["export_error"] = str(exc)
            self.latest_workflow_result = dict(result)
        self.measurement.set_workflow_result(result)

    def reset_calibration(self) -> dict[str, Any]:
        if self.measurement.state == "running" or self.schedule.snapshot()["active"]:
            raise RuntimeError("测量或自动任务运行期间不能重置标定")
        stamp = time.strftime("%Y%m%d_%H%M%S")
        paths = self._workspace_paths()
        with self.lock:
            for key in ("points", "model", "settings", "selection", "drift"):
                path = paths[key]
                if path.exists():
                    archived = path.with_name(f"{path.stem}-{stamp}{path.suffix}")
                    path.replace(archived)
            self.points = []
            self.point_records = []
            self.selected_point_ids = []
            self.model = None
            self.model_path = None
            self.model_settings = None
            self.calibration_settings = None
            self.model_created_at = None
            self.latest_workflow_result = None
            self.drift = self._empty_drift()
        return self.workflow_snapshot()

    def fit(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.measurement.state == "running" or self.schedule.snapshot()["active"]:
            raise RuntimeError("请等待当前测量或自动任务结束后再生成测试曲线")
        raw_points = payload.get("points", [])
        degree = int(payload.get("degree", 1))
        records: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for index, row in enumerate(raw_points, 1):
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
        requested_ids = payload.get("selected_point_ids")
        selected_ids = (
            [str(value) for value in requested_ids]
            if requested_ids is not None else [record["point_id"] for record in records]
        )
        selected_id_set = set(selected_ids)
        selected_records = [
            record for record in records if record["point_id"] in selected_id_set
        ]
        if len(selected_records) < degree + 1:
            raise ValueError(f"至少选择 {degree + 1} 个标定点")
        selected_points = [
            CalibrationPoint(
                float(record["concentration_um"]), float(record["current_nA"]),
                str(record["label"]),
            )
            for record in selected_records
        ]
        model = fit_calibration(selected_points, degree=degree)
        self.save_dir.mkdir(parents=True, exist_ok=True)
        paths = self._workspace_paths()
        path = paths["model"]
        model_settings = self.settings.snapshot()["settings"]
        if (self.calibration_settings is not None
                and not SettingsController.same_analysis_protocol(
                    self.calibration_settings, model_settings
                )):
            raise ValueError("当前 IT 条件与候选标定点不一致，不能生成测试曲线")
        save_model(model, path)
        paths["settings"].write_text(
            json.dumps(model_settings, indent=2, ensure_ascii=False)
        )
        created_at = time.time()
        paths["selection"].write_text(json.dumps({
            "created_at": created_at,
            "degree": degree,
            "selected_point_ids": [
                record["point_id"] for record in selected_records
            ],
            "candidate_points_count": len(records),
        }, indent=2, ensure_ascii=False))
        with self.lock:
            self.point_records = records
            self._sync_points()
            self.selected_point_ids = [
                record["point_id"] for record in selected_records
            ]
            self.model = model
            self.model_path = path
            self.model_settings = model_settings
            self.calibration_settings = dict(model_settings)
            self.model_created_at = created_at
            self._save_calibration_points()
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
            model_compatible = (
                self.model is not None
                and self.model_settings is not None
                and SettingsController.same_analysis_protocol(
                    self.model_settings, self.settings.snapshot()["settings"]
                )
            )
            if self.model is None:
                return {
                    "model": None,
                    "points": self.points_payload()["points"],
                    "selected_point_ids": self.selected_point_ids,
                    "model_created_at": self.model_created_at,
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
            return {
                "model": _json_safe(model.to_json()),
                "model_path": str(self.model_path) if self.model_path else "",
                "points": self.points_payload()["points"],
                "curve": {"concentration_um": xs, "current_nA": ys},
                "measurement_settings": self.model_settings,
                "selected_point_ids": self.selected_point_ids,
                "model_created_at": self.model_created_at,
                "drift_bias_nA": bias_nA,
                "model_compatible": model_compatible,
            }

    def points_payload(self) -> dict[str, Any]:
        return {
            "points": [
                {
                    **record,
                    "selected": record["point_id"] in set(self.selected_point_ids),
                }
                for record in self.point_records
            ]
        }

    def predict(self, payload: dict[str, Any]) -> dict[str, Any]:
        model = load_model(payload["model_path"]) if payload.get("model_path") else self.model
        if model is None:
            raise ValueError("尚未拟合标定曲线")
        if not payload.get("model_path") and self.model_settings is not None:
            current_settings = self.settings.snapshot()["settings"]
            if not SettingsController.same_analysis_protocol(
                    current_settings, self.model_settings):
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


class RequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        # Keep the one-click Terminal quiet except for meaningful server errors.
        return

    def _send_json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        data = json.dumps(_json_safe(payload), ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _send_bytes(self, data: bytes, content_type: str) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 2_000_000:
            raise ValueError("请求体过大")
        raw = self.rfile.read(length)
        if not raw:
            return {}
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("请求必须是 JSON 对象")
        return payload

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._send_bytes((GUI_DIR / "index.html").read_bytes(), "text/html; charset=utf-8")
            return
        if parsed.path == "/compact":
            self._send_bytes(
                (GUI_DIR / "compact.html").read_bytes(), "text/html; charset=utf-8"
            )
            return
        if parsed.path == "/api/status":
            self._send_json(APP.measurement.snapshot())
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
        if parsed.path == "/api/workflow":
            self._send_json(APP.workflow_snapshot())
            return
        if parsed.path == "/api/drift":
            self._send_json(APP.drift_payload())
            return
        if parsed.path == "/api/health":
            self._send_json({"ok": True, "project": str(PROJECT_DIR)})
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
        try:
            payload = self._body()
            if self.path == "/api/measurement/start":
                if APP.schedule.snapshot()["active"]:
                    raise RuntimeError("自动测量运行期间不能插入手动测量")
                if not APP.settings.snapshot()["applied"]:
                    raise RuntimeError("请先将当前检测条件应用到硬件")
                result = APP.start_measurement(payload)
            elif self.path == "/api/measurement/stop":
                result = APP.measurement.stop()
            elif self.path == "/api/calibration/load":
                result = APP.load_points(str(payload["path"]))
            elif self.path == "/api/calibration/fit":
                result = APP.fit(payload)
            elif self.path == "/api/drift/calculate":
                result = APP.calculate_drift(payload)
            elif self.path == "/api/drift/toggle":
                result = APP.toggle_drift(payload)
            elif self.path == "/api/predict":
                result = APP.predict(payload)
            elif self.path == "/api/schedule/start":
                if not APP.settings.snapshot()["applied"]:
                    raise RuntimeError("请先将当前检测条件应用到硬件")
                role = str(payload.get("sample_role") or "test")
                workflow = APP.workflow_snapshot()
                is_it = APP.settings.snapshot()["settings"].get("method") == "it"
                if is_it and role in {"stabilization", "test"} and not workflow["calibration_ready"]:
                    raise RuntimeError("请先选择标定点并生成测试曲线")
                if (role == "calibration" and workflow["points_count"]
                        and not workflow["settings_match"]):
                    raise RuntimeError("当前 IT 条件与已有标定点不同，请新建标定")
                payload["settings"] = APP.settings.snapshot()["settings"]
                payload["save_dir"] = str(APP.save_dir)
                result = APP.schedule.start(payload)
            elif self.path == "/api/schedule/stop":
                result = APP.schedule.stop()
            elif self.path == "/api/settings/apply":
                if (APP.measurement.snapshot()["state"] == "running"
                        or APP.schedule.snapshot()["active"]):
                    raise RuntimeError("测量或自动任务运行期间不能修改硬件参数")
                result = APP.settings.apply(payload)
                APP.measurement.settings = dict(result["settings"])
            elif self.path == "/api/workflow/config":
                result = APP.configure_workflow(payload)
            elif self.path == "/api/workflow/reset-calibration":
                result = APP.reset_calibration()
            else:
                self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
                return
            self._send_json(result)
        except (ValueError, KeyError, TypeError, OSError) as exc:
            self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except RuntimeError as exc:
            self._send_json({"error": str(exc)}, HTTPStatus.CONFLICT)


def serve(host: str = "127.0.0.1", port: int = DEFAULT_PORT,
          open_browser: bool = False) -> None:
    server = ThreadingHTTPServer((host, port), RequestHandler)
    url = f"http://{host}:{server.server_port}/"
    print(f"i-t GUI: {url}", flush=True)
    if open_browser:
        threading.Timer(0.35, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        APP.schedule.stop()
        APP.measurement.stop()
        server.server_close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="本地 i-t 电化学检测 GUI")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--open-browser", action="store_true")
    args = parser.parse_args(argv)
    serve(args.host, args.port, args.open_browser)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
