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
from .collect import (
    DEVICE as JLINK_DEVICE,
    JLINK_EXE,
    SPEED_KHZ as JLINK_SPEED_KHZ,
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
NCS_VENV_ACTIVATE = Path(
    os.environ.get("SENSUS_NCS_VENV_ACTIVATE", "~/ncs/.venv/bin/activate")
).expanduser()

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
    """``source <ncs venv>/bin/activate && `` 前缀,venv 不存在时返回空串。

    west 不在系统 PATH 上而在 NCS 的 venv 内;返回空串是为了兼容 west 已在
    PATH 上的机器(以及 CI),此时让 west 自己去报错,而不是先报 activate 缺失。
    """
    if not NCS_VENV_ACTIVATE.exists():
        return ""
    return f"source {shlex.quote(str(NCS_VENV_ACTIVATE))} && "
FSR_OPTIONS = {
    50: "MAX30131_FSR_50NA",
    100: "MAX30131_FSR_100NA",
    250: "MAX30131_FSR_250NA",
    500: "MAX30131_FSR_500NA",
    1000: "MAX30131_FSR_1000NA",
    2000: "MAX30131_FSR_2000NA",
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
    """Validate IT parameters and build/flash matching firmware when needed."""

    DEFAULTS = {
        "initial_potential_v": 0.2,
        "potential_v": 0.2,
        "prestep_s": 0.0,
        "duration_s": 180.0,
        "target_rate_hz": 10.0,
        "sens_period_code": 0,
        "fit_window_s": 20.0,
        "fsr_nA": 500,
        "offset_nA": 19,
        "offset_mode": "19nA",
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
    def _flash_firmware() -> None:
        """用 JLinkExe V8.80 把 hex 烧进片子。

        🔴 为什么不是 openocd:Homebrew 的 openocd **没有编 jlink 驱动**
        (`Error: The specified adapter driver was not found (jlink)`;libjaylink
        不在其依赖里,`brew` 的稳定 bottle 同样没有,重装无效)。JLinkExe V8.80 是本项目
        唯一验证过能连这两支克隆探头的通道,见
        docs/troubleshooting/jlink-v9克隆-swd-turnaround不松线.md。2026-08-09 换。

        🔴 必须查输出标记:JLinkExe 连不上目标时也可能 exit 0,不能只靠 returncode。
        成功标记取自 2026-08-09 实测输出(`O.K.` + `Script processing completed.`)。
        """
        script = f"loadfile {FIRMWARE_HEX}\nr\ng\nq\n"
        with tempfile.NamedTemporaryFile(
            "w", suffix=".jlink", delete=False, encoding="utf-8"
        ) as handle:
            handle.write(script)
            script_path = Path(handle.name)
        try:
            done = subprocess.run(
                [
                    str(JLINK_EXE), "-device", JLINK_DEVICE, "-if", "SWD",
                    "-speed", str(JLINK_SPEED_KHZ), "-autoconnect", "1",
                    "-NoGui", "1", "-ExitOnError", "1",
                    "-SelectEmuBySN", JLINK_SERIAL,
                    "-CommanderScript", str(script_path),
                ],
                cwd=PROJECT_DIR, check=True, capture_output=True, text=True,
            )
        finally:
            script_path.unlink(missing_ok=True)
        blob = f"{done.stdout}\n{done.stderr}"
        if "O.K." not in blob or "Script processing completed." not in blob:
            tail = [line for line in blob.strip().splitlines() if line.strip()][-3:]
            raise RuntimeError("JLinkExe 烧录未确认成功:" + " | ".join(tail))

    @staticmethod
    def same_analysis_protocol(first: dict[str, Any], second: dict[str, Any]) -> bool:
        """Compare settings that determine sampled IT data and its analysis.

        Startup potential/hold are not sampled. They remain in run metadata for
        traceability but do not invalidate a curve whose sampled potential,
        duration, rate, fit window and current range are unchanged.
        """
        ignored = {"initial_potential_v", "prestep_s"}
        return (
            {key: value for key, value in first.items() if key not in ignored}
            == {key: value for key, value in second.items() if key not in ignored}
        )

    @classmethod
    def validate(cls, payload: dict[str, Any]) -> dict[str, Any]:
        merged = {**cls.DEFAULTS, **payload}
        initial_potential_v = float(merged["initial_potential_v"])
        potential_v = float(merged["potential_v"])
        prestep_s = float(merged["prestep_s"])
        duration_s = float(merged["duration_s"])
        target_rate_hz = float(merged["target_rate_hz"])
        sens_period_code = int(merged["sens_period_code"])
        fit_window_s = float(merged["fit_window_s"])
        fsr_nA = int(merged["fsr_nA"])
        raw_offset_mode = payload.get("offset_mode")
        if raw_offset_mode in (None, ""):
            raw_offset_mode = f"{int(payload.get('offset_nA', cls.DEFAULTS['offset_nA']))}nA"
        offset_mode = str(raw_offset_mode)
        if not -0.4 <= initial_potential_v <= 0.39:
            raise ValueError("IT 起始电位必须在 -0.4 至 +0.39 V 之间")
        if not -0.4 <= potential_v <= 0.39:
            raise ValueError("IT 测试电位必须在 -0.4 至 +0.39 V 之间")
        if not 0 <= prestep_s <= 300:
            raise ValueError("阶跃前保持时间必须在 0 至 300 秒之间")
        if not 10 <= duration_s <= 3600:
            raise ValueError("IT 时长必须在 10 至 3600 秒之间")
        if not 0.5 <= target_rate_hz <= 10:
            raise ValueError("输出采样频率必须在 0.5 至 10 Hz 之间")
        if sens_period_code not in SENS_PERIOD_MS:
            raise ValueError("不支持该硬件采样周期")
        if not 1 <= fit_window_s <= duration_s:
            raise ValueError("拟合窗口必须在 1 秒与测量时长之间")
        if fit_window_s * target_rate_hz < 3:
            raise ValueError("拟合窗口内至少需要 3 个输出采样点")
        if fsr_nA not in FSR_OPTIONS:
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
        }

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
        sens_code = settings["sens_period_code"]
        sens_ms = SENS_PERIOD_MS[sens_code]
        header = (
            "#ifndef SENSUS_MEASUREMENT_CONFIG_H\n"
            "#define SENSUS_MEASUREMENT_CONFIG_H\n\n"
            "/* Generated by the local electrochemistry workstation. */\n"
            f"#define GUI_WP_FSR {FSR_OPTIONS[settings['fsr_nA']]}\n"
            f"#define GUI_WP_OFFSET_SEL {OFFSET_OPTIONS[settings['offset_mode']][0]}\n"
            f"#define GUI_WP_START_E_MV {initial_potential_mv}\n"
            f"#define GUI_WP_E_MV {potential_mv}\n"
            f"#define GUI_PRESTEP_DURATION_MS {prestep_ms}U\n"
            f"#define GUI_MEASUREMENT_DURATION_MS {duration_ms}U\n\n"
            f"#define GUI_SENS_PERIOD_CODE 0x{sens_code:X}U\n"
            f"#define GUI_SENS_PERIOD_MS {sens_ms}U\n\n"
            "#endif\n"
        )
        try:
            FIRMWARE_CONFIG.write_text(header)
            # 🔴 west 装在 NCS 自己的 venv 里(默认 ~/ncs/.venv/bin/west)。
            #    `zephyr-env.sh` 只把 $ZEPHYR_BASE/scripts 塞进 PATH,**不激活该 venv**
            #    ⇒ 不先激活就是 `zsh:1: command not found: west`,按钮看起来"没反应"
            #    (失败 <1s,label 闪一下就弹回去)。2026-08-09 实测确认。
            #    只在这个子 shell 里激活:NCS venv 与本工作站 venv 依赖冲突,不可合并。
            build = (
                f"{ncs_venv_prefix()}"
                "source ~/ncs/zephyr/zephyr-env.sh && "
                "west build -b pa_converter_v40 -d software/firmware/build "
                "software/firmware -- -DBOARD_ROOT=$PWD/software/firmware "
                "-DDTS_ROOT=$PWD/software/firmware"
            )
            subprocess.run(
                ["/bin/zsh", "-lc", build], cwd=PROJECT_DIR,
                check=True, capture_output=True, text=True,
            )
            self._flash_firmware()
            SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
            SETTINGS_PATH.write_text(json.dumps({
                "settings": settings,
                "firmware_sha256": self._firmware_hash(),
            }, indent=2, ensure_ascii=False))
        # RuntimeError:_flash_firmware() 的「exit 0 但没烧成」判据会抛它,
        # 不接住的话会变成未处理 500,state 停在 "applying",前端只能看到通用错误。
        except (subprocess.CalledProcessError, OSError, RuntimeError) as exc:
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
                "native_rate_hz": 1000 / SENS_PERIOD_MS[self.settings["sens_period_code"]],
                "output_points": int(round(
                    self.settings["duration_s"] * self.settings["target_rate_hz"]
                )),
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
        self.cmd_path: Path | None = None   # 方案 C:在线切档命令文件
        self.cell_v_path: Path | None = None  # 电极电压连采 CSV(与电流不同速率)
        self.audit_path: Path | None = None   # 配置变更审计 jsonl(每次改参数留痕)
        # DEBUG 页的增量读状态。全部按"读位置 + 累积列表"做 ⇒ 1Hz 刷新不随
        # 文件增长变慢(一轮 180s 就上千行,全量重读会明显卡)。
        self._audit_pos = 0
        self._auto_get_at = 0.0
        self._audit_cache: list[dict[str, Any]] = []
        self._cfg_live: dict[str, Any] = {}
        self._afe_status: dict[str, Any] = {}
        self._last_reject: dict[str, Any] = {}
        self._phase: dict[str, Any] = {}
        self._dbg_cur_pos = 0
        self._dbg_cur: list[dict[str, Any]] = []
        self._dbg_cur_hdr: list[str] | None = None
        self._dbg_cv_pos = 0
        self._dbg_cv: list[dict[str, Any]] = []
        self._dbg_cv_hdr: list[str] | None = None
        # 方案 C:运行时档位真值。**不能用 SettingsController 的值代替** ——
        # 那是"最后一次烧录进去的编译期默认",而 RANGE 命令会在运行中改掉它,
        # 两者可以不一致。唯一权威来源是固件回的 RANGE_APPLIED 行。
        self.range_runtime: dict[str, Any] = {
            "pending": None, "applied": None, "rejected": None, "at": None,
        }
        self._rtt_pos = 0
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
        self.bridge_process: subprocess.Popen[str] | None = None
        self.bridge_log_handle: Any = None
        self.user_stop_requested = False

    def start(self, metadata: dict[str, Any] | None = None,
              on_complete: Any = None,
              settings: dict[str, Any] | None = None) -> dict[str, Any]:
        with self.lock:
            if self.state == "running":
                raise RuntimeError("已有测量正在运行")
            RUNS_DIR.mkdir(parents=True, exist_ok=True)
            self.run_id = _now_id("it")
            self.run_dir = RUNS_DIR / self.run_id
            self.run_dir.mkdir(parents=True, exist_ok=False)
            self.metadata = dict(metadata or {})
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
            self._auto_get_at = 0.0
            self._audit_cache = []
            # 🔴 **不清 _cfg_live / _afe_status**:它们描述的是**设备**,不是某一轮。
            #    清掉的后果是 DEBUG 面板在两轮之间没有任何设备真值可显示,控件只能
            #    退回 HTML 默认值(FSR 50nA、E 空)——而那时按「应用」就会把**猜的值**
            #    写进硬件。保留上次已知值,新一轮的 auto-GET 几秒内就会刷新它。
            self._last_reject = {}
            self._phase = {}          # 阶段是**本轮**的属性,新一轮必须清
            self._dbg_cur_pos = 0
            self._dbg_cur = []
            self._dbg_cur_hdr = None
            self._dbg_cv_pos = 0
            self._dbg_cv = []
            self._dbg_cv_hdr = None
            self.range_runtime = {"pending": None, "applied": None,
                                  "rejected": None, "at": None}
            self._rtt_pos = 0
            self._auto_switch_done = False
            self.resampled_path = self.run_dir / "resampled_10hz.csv"
            self.summary_path = self.run_dir / "summary.json"
            self.plot_path = self.run_dir / "it_curve.png"
            self.started_at = time.time()
            self.finished_at = None
            self.summary = None
            self.workflow_result = None
            self.error = ""
            self.user_stop_requested = False
            self.state = "running"
            self.message = "已启动硬件测量，等待 RTT 数据"
            self.on_complete = on_complete
            self.settings = SettingsController.validate(settings or self.settings)

            env = os.environ.copy()
            host_dir = str(PROJECT_DIR / "software" / "host")
            env["PYTHONPATH"] = host_dir + os.pathsep + env.get("PYTHONPATH", "")
            command = [
                sys.executable,
                "-m",
                "pa_host.it_tool",
                "measure",
                # 🔴 2026-08-09:原来是 `--socket 127.0.0.1:19021` + 自己起一条
                #    openocd RTT 桥。但 Homebrew 的 openocd 没编 jlink 驱动
                #    (`adapter driver was not found (jlink)`),这条桥从来起不来。
                #    改成让 collector 走它自己那条已实现且验证过的 JLinkExe V8.80
                #    通道:它负责 SetRTTAddr + rtt start,RTT 仍出在 telnet 19021,
                #    并且 finally 里有 terminate/wait/kill 的完整回收(禁 pkill)。
                "--start-jlink",
                "--elf",
                str(FIRMWARE_ELF),
                "--probe-serial",
                JLINK_SERIAL,
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
                "START",
                "--duration",
                str(self.settings["prestep_s"] + self.settings["duration_s"] + 5),
                "--idle-timeout",
                "25",
            ]
            log_handle = (self.run_dir / "collector.log").open("w", buffering=1)
            try:
                self.process = subprocess.Popen(
                    command,
                    cwd=PROJECT_DIR,
                    env=env,
                    stdout=log_handle,
                    stderr=subprocess.STDOUT,
                    text=True,
                    # 🔴 自成进程组:进程树是
                    #      gui_server → it_tool → pa_host.collect → JLinkExe
                    #    只 terminate 第一层(it_tool)的话,孙进程 collect 与曾孙
                    #    JLinkExe 都活下来,**并且不会因 idle-timeout 自愈**
                    #    (2026-08-09 实测:停止 60s 后两者仍在跑、19021 仍被占,
                    #    下一次烧录/测量就会撞上探头被占)。有了进程组才能整棵收掉。
                    start_new_session=True,
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
            process = self.process
            if self.state != "running" or process is None:
                return self.snapshot()
            self.user_stop_requested = True
            self.message = "正在停止硬件测量"
            try:
                with socket.create_connection(("127.0.0.1", 19021), timeout=1) as conn:
                    conn.sendall(b"STOP\n")
            except OSError:
                self._terminate_tree(process)
            else:
                threading.Thread(
                    target=self._terminate_if_running,
                    args=(process, 1.5), daemon=True,
                ).start()
            return self.snapshot()

    def send_range(self, payload: dict[str, Any]) -> dict[str, Any]:
        """方案 C:测量进行中在线切换 FSR / offset 档,**不复位、不中断极化**。

        为什么必须经采集器的命令文件:JLinkExe 的 RTT telnet 只把采集器持有的
        那个连接的输入送进下行通道,另开连接写命令目标端收不到(2026-08-09 实测)。
        """
        with self.lock:
            if self.state != "running" or self.cmd_path is None:
                raise RuntimeError("只有测量进行中才能在线切档")
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
    def send_command(self, line: str) -> dict[str, Any]:
        """下发任意一行命令。send_range() 是它的一个特例。

        🔴 同样必须经采集器的命令文件 —— JLinkExe 的 RTT telnet 只把**采集器持有
        的那个连接**的输入送进下行通道,另开 telnet 写命令目标端收不到
        (2026-08-09 实测)。所以"没有测量在跑"时无处可发,只能拒绝。
        """
        line = line.strip()
        if not line:
            raise ValueError("命令为空")
        if "\n" in line or "\r" in line:
            raise ValueError("一行一条命令,不许含换行")
        if len(line) >= 128:
            # 与固件 AFE_CFG_LINE_MAX 同口径:超长在固件侧只会被拒,不如在这里挡
            raise ValueError(f"命令过长({len(line)} ≥ 128 字符)")
        with self.lock:
            if self.state != "running" or self.cmd_path is None:
                raise RuntimeError("命令只能在测量进行中下发(RTT 下行通道由采集器持有)")
            with self.cmd_path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
            if line.startswith(("RANGE ", "SET ")):
                self.range_runtime = {**self.range_runtime, "pending": line,
                                      "rejected": None, "at": time.time()}
            return _json_safe({"sent": line, "cmd_file": str(self.cmd_path)})

    def _audit_events(self, limit: int = 60) -> list[dict[str, Any]]:
        """读 audit.jsonl 的尾部。增量读:只从上次位置往后追加,不全量重读。"""
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
        for raw in fresh.splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                continue
            self._audit_cache.append(event)
            kind = event.get("kind")
            if kind in ("CFG_APPLIED", "CFG_DERIVED", "CFG_BOOT"):
                self._cfg_live.update({k: v for k, v in event.items()
                                       if k not in ("raw", "kind")})
            elif kind == "CFG_CONFIRMED":
                self._cfg_live.update({k: v for k, v in event.items()
                                       if k not in ("raw", "kind")})
                self._cfg_live["confirmed_ep"] = event.get("ep")
            elif kind == "AFE_STATUS":
                self._afe_status = {k: v for k, v in event.items() if k != "kind"}
            elif kind == "IT_PHASE":
                self._phase = {k: v for k, v in event.items() if k != "kind"}
            elif kind in ("CFG_REJECT", "CFG_FAULT", "CFG_ROLLBACK", "OCP_REJECT",
                          "RANGE_REJECT"):
                # 🔴 拒因必须摆到显眼处。埋在滚动日志尾部时,用户看到的是
                #    "我点了下发,然后什么都没发生" —— 那和命令没送达同形。
                self._last_reject = {k: v for k, v in event.items() if k != "kind"}
                self._last_reject["kind"] = kind
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
        for raw in fresh.splitlines():
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
        cur = self._read_kv_csv(self.raw_path, "_dbg_cur_pos", "_dbg_cur",
                                ("dev_ms", "fa_fw", "sat", "epoch", "counts"))
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
                "valid": [not int(r.get("sat", 0) or 0) for r in cur],
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
        # 🔴 自愈:开机那几行 CFG_BOOT/CFG_APPLIED/CFG_DERIVED 常常收不到 ——
        #   JLinkExe 的 `rtt start` 会把读指针对到当前写指针,**跳过缓冲里已有的
        #   字节**,而那几行在 rtt start 之前(复位后 ~300ms)就写完了。
        #   这正是 GET 幂等重放的用途:它不 ep++、不写任何寄存器,只把设备当前
        #   认知整套重打一遍。检测到"在跑但一条 CFG_* 都没有"就自动补一次。
        # 判据必须用**只有 CFG_DERIVED 才带**的字段(bits)。用 `not self._cfg_live`
        # 不行:CFG_BOOT 只带 ep/ms/fw/reason,一到就让字典非空,GET 反而不发了
        # ——"有几个键"和"有没有派生量"是两件事。
        if (self.state == "running" and self._cfg_live.get("bits") is None
                and time.time() - self._auto_get_at > 3.0):
            self._auto_get_at = time.time()
            try:
                self.send_command("GET")
            except (RuntimeError, ValueError):
                pass
        return _json_safe({
            "state": self.state,
            "message": self.message,
            "error": self.error,
            "run_id": self.run_id,
            "run_dir": str(self.run_dir) if self.run_dir else "",
            "raw_path": str(self.raw_path) if self.raw_path else "",
            "audit_path": str(self.audit_path) if self.audit_path else "",
            "cell_v_path": str(self.cell_v_path) if self.cell_v_path else "",
            "cfg": self._cfg_live,
            "afe_status": self._afe_status,
            "last_reject": self._last_reject or None,
            "phase": self._phase or None,
            "cell_v": self._cell_voltages(),
            "series": self._debug_series(),
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

    @staticmethod
    def _terminate_tree(process: subprocess.Popen[str]) -> None:
        """整棵进程组收掉,而不是只收第一层。

        🔴 只 `process.terminate()` 收不干净:树是
        it_tool → pa_host.collect → JLinkExe,`terminate` 只打到 it_tool,
        collect 与 JLinkExe 会一直活着占住探头和 telnet 19021(实测 60s 不自愈)。
        配合 Popen(start_new_session=True) 才能用 killpg 一次收完。
        JLinkExe 本身**不理 SIGTERM**(实测),但它父进程 collect 一退、stdin 管道
        EOF,它就会自己退 —— 所以关键是让 collect 收到信号并跑完它的 finally。
        """
        if process.poll() is not None:
            return
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            process.terminate()

    @classmethod
    def _terminate_if_running(cls, process: subprocess.Popen[str], delay_s: float) -> None:
        time.sleep(delay_s)
        if process.poll() is None:
            cls._terminate_tree(process)

    def _watch(self, log_handle: Any) -> None:
        assert self.process is not None
        process = self.process
        while process.poll() is None:
            self._scan_range_events()
            self._maybe_auto_switch()
            with self.lock:
                self.message = self._progress_message()
            time.sleep(0.8)
        self._scan_range_events()
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
                resample_run_10hz(
                    self.raw_path, self.resampled_path,
                    duration_s=self.settings["duration_s"],
                    target_rate_hz=self.settings["target_rate_hz"],
                )
                summary = summarize_run(
                    self.resampled_path, window_s=self.settings["fit_window_s"]
                )
                save_summary(summary, self.summary_path)
                self.summary = _json_safe(asdict(summary))
                self.state = "completed"
                self.message = "测量完成，已生成 10 Hz 数据和末 20 s 汇总"
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
        for line in fresh.splitlines():
            line = line.strip()
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
                    self.range_runtime = {"pending": None, "applied": kv,
                                          "rejected": None, "at": time.time()}
            elif "RANGE_REJECT" in line:
                with self.lock:
                    self.range_runtime = {**self.range_runtime, "pending": None,
                                          "rejected": line[line.index("RANGE_REJECT"):],
                                          "at": time.time()}

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
        count = 0
        if self.raw_path and self.raw_path.exists():
            try:
                t, _, _ = load_run_csv(self.raw_path)
                count = len(t)
            except (OSError, ValueError):
                pass
        return f"正在采集：已收到约 {count} 个原生点"

    def _data(self) -> dict[str, Any]:
        if not self.raw_path or not self.raw_path.exists():
            return {"time_s": [], "current_nA": [], "valid": []}
        try:
            t, current, valid = load_run_csv(self.raw_path)
        except (OSError, ValueError):
            return {"time_s": [], "current_nA": [], "valid": []}
        return {
            "time_s": [float(x) for x in t],
            "current_nA": [float(x) for x in current],
            "valid": [bool(x) for x in valid],
        }

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
        out["railed"] = any(out.get(k) in (0, 4095) for k in ("ce_code", "wo_code"))
        # 🔴 恒电位环用了多少驱动、还剩多少 —— 这两个数把"环路快饱和了"变成可读数字。
        #   ce_drive_mv:C 放大器为了把电流推过电解池,需要把 CE 压到 RE 之下多少。
        #                健康态实测只需 ~60 mV(v1:CE 140 / RE 201)。
        #   ce_headroom_mv:CE 距 0 轨还有多少。它见底 ⇒ 环路钳不住设定电位。
        if isinstance(out.get("ce_mv"), (int, float)) and isinstance(out.get("re_mv"), (int, float)):
            out["ce_drive_mv"] = out["re_mv"] - out["ce_mv"]
            out["ce_headroom_mv"] = out["ce_mv"]
        out["rows"] = len(rows) - 1
        return out

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
            payload = {
                "state": self.state,
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
                    "native_rate_note": "MAX30131 原生约 8.06 Hz；高于原生的输出频率由主机重采样",
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
        sample_role = str(payload.get("sample_role") or "test")
        raw_concentration = payload.get("known_concentration_um")
        known_concentration = (
            None if raw_concentration in (None, "") else float(raw_concentration)
        )
        if sample_role not in {"calibration", "stabilization", "test"}:
            raise ValueError("自动任务类型必须是标定、稳定化或测试")
        if sample_role == "calibration" and known_concentration is None:
            raise ValueError("自动标定任务必须填写已知浓度")
        minimum_interval_s = settings["prestep_s"] + settings["duration_s"] + 10
        if interval_minutes * 60 < minimum_interval_s:
            raise ValueError(
                f"当前 IT 条件要求间隔至少 {minimum_interval_s / 60:.2f} 分钟"
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
        settings_match = (
            self.calibration_settings is None
            or SettingsController.same_analysis_protocol(
                self.calibration_settings, current_settings
            )
        )
        calibration_ready = (
            self.model is not None
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
        role = str(payload.get("sample_role", "calibration"))
        if role not in {"calibration", "stabilization", "test"}:
            raise ValueError("样品类型必须是标定、稳定化或测试")
        raw_concentration = payload.get("known_concentration_um")
        concentration = (
            None if raw_concentration in (None, "") else float(raw_concentration)
        )
        if concentration is not None and concentration < 0:
            raise ValueError("浓度不能为负数")
        if role == "calibration" and concentration is None:
            raise ValueError("标定样品必须填写已知浓度")
        current_settings = self.settings.snapshot()["settings"]
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

    def start_debug_run(self, payload: dict[str, Any]) -> dict[str, Any]:
        """硬件 DEBUG 模式的「一次 I-t 测量」。

        与正式测量的差别只有三点,其余完全共用同一条已验证的 RTT/J-Link 路径:
          ① metadata 打 `debug` 标记 ⇒ 收尾时不进标定工作区(见 _measurement_completed)
          ② **不传 live_raw_path** ⇒ raw 留在 run_dir/raw.csv,不写进保存目录
          ③ 不校验样品名/浓度/标定就绪 —— DEBUG 页刻意不暴露那套工作流

        🔴 但两个门禁必须保留:自动测量运行期间不许插队(会抢探头),
        已有测量在跑时不许再起(同上)。这两条与正式测量同口径。
        """
        if self.schedule.snapshot()["active"]:
            raise RuntimeError("自动测量运行期间不能起硬件 DEBUG 轮(探头只有一支)")
        if self.measurement.snapshot()["state"] == "running":
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
        return self.measurement.start(
            metadata=metadata, settings=self.settings.snapshot()["settings"])

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
        # 🔴 硬件 DEBUG 轮不进工作流:不导出、不进 measurement-index.csv、不触发
        #    浓度预测。否则调参数时随手跑的几轮会污染标定工作区,而"污染"这件事
        #    要到下次拟合曲线时才会暴露出来 —— 那时已经分不清哪几行是调试轮。
        #    刻意**不新增第四个 sample_role**:那要串改 5 处调用点却买不到任何东西。
        if metadata.get("debug"):
            self.measurement.set_workflow_result({
                "finished_at": run.get("finished_at"),
                "run_id": run.get("run_id"),
                "debug": True,
                "state": run.get("state"),
                "note": "硬件 DEBUG 轮:原始数据保留在 run_dir,不进标定工作区",
                "run_dir": run.get("run_dir"),
                "raw_path": run.get("raw_path"),
            })
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
        if parsed.path == "/api/debug":
            payload = APP.measurement.debug_snapshot()
            # 只作提示:不一致时 UI 提醒"固件里跑的可能不是 GUI 这套参数",
            # 但**不阻止**调试轮(理由见 start_debug_run)。
            payload["settings_applied"] = bool(
                APP.settings.snapshot().get("applied"))
            self._send_json(payload)
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
                    raise RuntimeError("请先将当前 IT 条件应用到硬件")
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
                result = APP.start_debug_run(payload)
            elif self.path == "/api/debug/stop":
                result = APP.measurement.stop()
            elif self.path == "/api/debug/cmd":
                result = APP.measurement.send_command(str(payload.get("line", "")))
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
                    raise RuntimeError("请先将当前 IT 条件应用到硬件")
                role = str(payload.get("sample_role") or "test")
                workflow = APP.workflow_snapshot()
                if role in {"stabilization", "test"} and not workflow["calibration_ready"]:
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
    # 🔴 必须接 SIGTERM。Python 对 SIGTERM 的默认动作是**立刻退出、不跑 finally**
    #    ⇒ `pkill -f gui_server` 之后,它起的 collector 与 JLinkExe 会活下来、
    #    继续占着探头和 telnet 19021,下一次启动的 run 连不上就带 traceback 死。
    #    2026-08-10 实测踩到:一个孤儿 collector 让新 run 直接
    #    ConnectionResetError,而现场看起来像"探头坏了"。
    def _graceful(_sig, _frm):
        threading.Thread(target=server.shutdown, daemon=True).start()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _graceful)
        except ValueError:
            pass   # 非主线程时不给注册,忽略

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        APP.schedule.stop()
        # 🔴 同步收干净,不能只靠 stop() 里那个 1.5s 延迟线程 —— 进程一退它就没了。
        APP.measurement.stop()
        proc = APP.measurement.process
        if proc is not None and proc.poll() is None:
            MeasurementController._terminate_tree(proc)
            try:
                proc.wait(timeout=6)
            except subprocess.TimeoutExpired:
                pass
        server.server_close()
        print("i-t GUI 已退出(采集子进程与 J-Link 已收回)", flush=True)


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
