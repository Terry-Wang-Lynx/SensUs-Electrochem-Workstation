#!/usr/bin/env python3
"""实时收数落盘 —— 从固件文本传输读行,校验后追加写 CSV。

用途
    A 段(实时):把固件经 SEGGER RTT 吐出的行协议落成 CSV,边收边做完整性检查。
    B 段(离线)交给 analyze.py。两段刻意分开:收数不能因为分析崩掉而丢数据。

四种取数来源
    --start-jlink   ★推荐★ 自己起 RTT 桥(JLinkExe/OpenOCD),从 telnet 19021 读
    --socket H:P    连一个已经在跑的 RTT telnet 服务
    --tail FILE     跟读一个 RTT 日志文件(你自己起的 logger)
    --serial PORT   V5.1 USB DATA CDC(文本行/命令双向)

用法
    # 最常用:一条命令搞定(RTT 地址自动从 ELF 提取)
    python3 -m pa_host.collect --start-jlink --out run.csv \\
        --elf /tmp/pabuild/firmware/zephyr/zephyr.elf

    # 回板前用合成数据验证整条链(不需要硬件)
    python3 -m pa_host.synth /tmp/fake.csv --hours 1
    python3 -m pa_host.analyze /tmp/fake.csv --fsr-pa 50000

🔴 两个必须知道的坑(2026-07-31 实测)
    1. SEGGER 后端只能用已验证的 J-Link V8.80，不能用 V9.46。
       V8.80 不存在时，本模块回退到已验证的 libjaylink OpenOCD。
       见 docs/troubleshooting/jlink-v9克隆-swd-turnaround不松线.md
    2. **`JLinkRTTLoggerExe` 的自动搜索找不到本固件的 RTT 控制块**(实测在 0x20001040,
       魔术字与缓冲都正常),而它**不接受地址参数** ⇒ 只能走 `JLinkExe` 的
       `exec SetRTTAddr <addr>` + `rtt start`,数据从 telnet 19021 出。本模块即按此实现。
       🔴 该地址**每次重新编译都可能变**,所以默认从 ELF 的 `_SEGGER_RTT` 符号自动提取,
       别硬编码。

⚠️ 这支克隆探头会整体掉出 USB,唯一恢复方式是物理拔插;**别用 pkill 杀 J-Link 进程**
   (会把它打掉线)。本模块用 Popen.terminate()。

快照日期
    2026-07-31
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .record import (
    CSV_COLUMNS,
    Sample,
    check_integrity,
    parse_line,
    sample_to_row,
)

# 首选已验证的 V8.80;路径来自 STM32CubeIDE 自带的 J-Link 工具链。
# 若 CubeIDE 被移除，回退到启用 libjaylink 的开源 OpenOCD：它仍通过同一个
# RTT 端口向上层提供完全相同的行协议，不改测量和解析逻辑。
JLINK_V880_DIR = Path(
    "/Applications/STM32CubeIDE.app/Contents/Eclipse/plugins/"
    "com.st.stm32cube.ide.mcu.externaltools.jlink.macos64_2.5.100.202509120932/tools/bin"
)
def _resolve_jlink_exe() -> Path:
    """选 JLinkExe:显式 env > V8.80 > PATH。

    🔴 这个顺序不能反。本机 `shutil.which("JLinkExe")` 解析到
    /usr/local/bin/JLinkExe → /Applications/SEGGER/JLink_V946/JLinkExe = V9.46,
    而 V9.x 的 DLL 丢了对克隆固件的 legacy 回退,连不上任何目标
    (见 docs/troubleshooting/jlink-v9克隆-swd-turnaround不松线.md)。
    原实现把 which() 排在 V8.80 前面 ⇒ 本文件顶部「只能用 V8.80」的注释
    与实际行为相反,默认就挑中了坏的那支。2026-08-09 修。
    """
    override = os.environ.get("SENSUS_JLINK_EXE")
    if override:
        return Path(override)
    v880 = JLINK_V880_DIR / "JLinkExe"
    if v880.exists():
        return v880
    found = shutil.which("JLinkExe")
    if found:
        print(f"[collect] ⚠️ 回退到 PATH 里的 {found};若是 V9.x 会连不上克隆探头,"
              f"请设 SENSUS_JLINK_EXE 指向 V8.80", file=sys.stderr)
        return Path(found)
    return v880


JLINK_EXE = _resolve_jlink_exe()


def _resolve_openocd() -> tuple[Path, Path]:
    """选取启用 J-Link 驱动的 OpenOCD 及其 scripts 目录。"""
    configured = os.environ.get("SENSUS_OPENOCD_EXE")
    candidates = [Path(configured).expanduser()] if configured else []
    candidates.extend([
        Path.home() / ".local/share/sensus-openocd-jlink/bin/openocd",
        Path(shutil.which("openocd") or "/nonexistent/openocd"),
    ])
    executable = next((path for path in candidates if path.exists()), candidates[0])

    configured_scripts = os.environ.get("SENSUS_OPENOCD_SCRIPTS")
    script_candidates = (
        [Path(configured_scripts).expanduser()] if configured_scripts else []
    )
    script_candidates.extend([
        executable.parent.parent / "share/openocd/scripts",
        Path("/opt/homebrew/share/openocd/scripts"),
        Path("/usr/local/share/openocd/scripts"),
    ])
    scripts = next(
        (path for path in script_candidates
         if (path / "interface/jlink.cfg").exists()),
        script_candidates[0],
    )
    return executable, scripts


OPENOCD_EXE, OPENOCD_SCRIPTS = _resolve_openocd()

# 从 ELF 提 _SEGGER_RTT 用
ZEPHYR_SDK_NM = Path(
    os.environ.get("SENSUS_ARM_NM")
    or shutil.which("arm-zephyr-eabi-nm")
    or (Path.home() / "zephyr-sdk-1.0.1/gnu/arm-zephyr-eabi/bin/arm-zephyr-eabi-nm")
)

DEVICE = "nRF52833_xxAA"
SPEED_KHZ = 4000
RTT_TELNET_PORT = 19021
# 触发命令未被固件确认前的重发间隔与最大次数。
# 🔴 按**挂钟时间**重发,不依赖 socket 空闲 —— 固件仍在吐上一轮数据时永不空闲。
TRIGGER_RESEND_INTERVAL_S = 1.0
TRIGGER_MAX_RESENDS = 20
SERIAL_TRIGGER_RESEND_INTERVAL_S = 3.0
# 命令文件轮询间隔(方案 C:外部命令经采集器 socket 转发给固件)
CMD_POLL_INTERVAL_S = 0.5
DEFAULT_ELF = Path("/tmp/pabuild/firmware/zephyr/zephyr.elf")


# --------------------------------------------------------------------------
# RTT 控制块地址
# --------------------------------------------------------------------------
def find_rtt_address(elf: Path) -> int:
    """从 ELF 的 `_SEGGER_RTT` 符号取控制块地址。

    🔴 为什么不硬编码:该地址由链接器分配,**每次改动固件都可能变**。
    2026-07-31 那次是 0x20001040,但这个值没有任何稳定性保证。
    """
    if not elf.exists():
        sys.exit(f"找不到 ELF: {elf}\n→ 用 --rtt-address 手动给,或用 --elf 指对路径")
    if not ZEPHYR_SDK_NM.exists():
        sys.exit(f"找不到 nm: {ZEPHYR_SDK_NM}\n→ 用 --rtt-address 手动给")

    out = subprocess.run(
        [str(ZEPHYR_SDK_NM), str(elf)], capture_output=True, text=True, check=False
    ).stdout
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[2] == "_SEGGER_RTT":
            return int(parts[0], 16)
    sys.exit(
        f"ELF 里没有 `_SEGGER_RTT` 符号:{elf}\n"
        "→ 固件可能没开 CONFIG_USE_SEGGER_RTT,或这不是目标 ELF"
    )


# --------------------------------------------------------------------------
# 取数来源
# --------------------------------------------------------------------------
def start_jlink_rtt(rtt_addr: int, probe_serial: str | None,
                    port: int, reset_before_read: bool = False) -> subprocess.Popen:
    """起 RTT 桥，数据出到 telnet ``port``。

    优先保留已验证的 JLinkExe V8.80 通路。它不存在时，使用
    libjaylink OpenOCD 建立等价的 RTT server。
    """
    if JLINK_EXE.exists():
        cmd = [str(JLINK_EXE), "-NoGui", "1", "-RTTTelnetPort", str(port)]
        if probe_serial:
            cmd += ["-SelectEmuBySN", probe_serial]

        print(f"[collect] 启动 JLinkExe,RTT 控制块 @ 0x{rtt_addr:08X},telnet {port}",
              file=sys.stderr)
        proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
            stderr=subprocess.STDOUT, text=True,
        )
        assert proc.stdin is not None
        reset_cmds = "r\nsleep 100\ng\nsleep 500\n" if reset_before_read else ""
        proc.stdin.write(
            f"si SWD\nspeed {SPEED_KHZ}\ndevice {DEVICE}\nconnect\n"
            f"{reset_cmds}exec SetRTTAddr 0x{rtt_addr:08X}\nrtt start\n"
        )
        proc.stdin.flush()   # 不关 stdin:JLinkExe 读到 EOF 会退出
        return proc

    if not OPENOCD_EXE.exists() or not (OPENOCD_SCRIPTS / "interface/jlink.cfg").exists():
        sys.exit(
            f"找不到 {JLINK_EXE}，也找不到可用的 libjaylink OpenOCD\n"
            "→ 设 SENSUS_JLINK_EXE 指向 V8.80，或设 SENSUS_OPENOCD_EXE/"
            "SENSUS_OPENOCD_SCRIPTS。"
        )

    adapter_serial = f"adapter serial {probe_serial}; " if probe_serial else ""
    reset_cmds = "reset halt; reset run; sleep 500; " if reset_before_read else ""
    server_commands = (
        f"{adapter_serial}adapter speed {SPEED_KHZ}; init; {reset_cmds}"
        f"rtt setup 0x{rtt_addr:08X} 0x100 \"SEGGER RTT\"; "
        f"rtt start; rtt server start {port} 0"
    )
    cmd = [
        str(OPENOCD_EXE), "-s", str(OPENOCD_SCRIPTS),
        "-f", "interface/jlink.cfg", "-c", "transport select swd",
        "-f", "target/nrf52.cfg", "-c", server_commands,
    ]
    print(f"[collect] 启动 libjaylink OpenOCD,RTT 控制块 @ "
          f"0x{rtt_addr:08X},telnet {port}",
          file=sys.stderr)
    return subprocess.Popen(
        cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT, text=True,
    )


def read_socket_lines(host: str, port: int, connect_timeout: float = 20.0,
                      idle_timeout: float | None = None,
                      trigger: str | None = None,
                      cmd_file: Path | None = None,
                      trigger_state: dict[str, Any] | None = None):
    """连 RTT telnet 服务,按行 yield。"""
    deadline = time.monotonic() + connect_timeout
    sock = None
    while time.monotonic() < deadline:
        try:
            sock = socket.create_connection((host, port), timeout=3)
            break
        except OSError:
            time.sleep(0.5)
    if sock is None:
        sys.exit(f"连不上 RTT telnet {host}:{port}(等了 {connect_timeout:.0f}s)")

    print(f"[collect] 已连上 {host}:{port}", file=sys.stderr)
    # 🔴 触发命令必须**重发到固件确认**,不能只发一次。2026-08-09 实测的竞态:
    #    JLinkExe 在**进程启动时**就监听 -RTTTelnetPort,早于它处理 stdin 里的
    #    `connect` / `exec SetRTTAddr` / `rtt start`。于是这里一连上就发的第一条
    #    START 被写进还没接通的下行通道、直接丢掉;固件停在 `IT_READY` 空转,
    #    最后「共 0 样本」。上行看着完全正常(能读到 boot log 和 IT_READY),
    #    所以这个坑从上行日志上看不出来。
    #    重发直到看见 `IT_START` 为止;看见就立刻停 —— 固件的运行态循环同样接
    #    CONTROL_START,多发一条会被当成再起一轮。
    trigger_command = "START" if trigger == "FRESH_START" else trigger
    trigger_bytes = (
        (trigger_command.rstrip("\r\n") + "\n").encode("ascii")
        if trigger_command else None
    )
    trigger_pending = trigger_bytes is not None
    resends = 0
    # 🔴 复位后必须能**重新武装** trigger。2026-08-10 实测的坑:残留缓冲里带着
    #    上一次开机的 IT_START,重发循环见到它就停了;随后检测到复位、门禁被重置,
    #    但 START 再也不会发出 ⇒ 固件停在 IT_READY,而上位机在等永远不来的样本。
    if trigger_state is None:
        trigger_state = {}
    warned_unacked = False
    if trigger_bytes:
        sock.sendall(trigger_bytes)
        print(f"[collect] 已发送硬件命令:{trigger_command}(未确认前每秒重发)",
              file=sys.stderr)
    sock.settimeout(1.0)
    buf = b""
    last_data = time.monotonic()
    last_trigger_at = time.monotonic()
    # 🔴 命令转发通道(方案 C 的主机侧)。
    #    为什么需要它:JLinkExe 的 RTT telnet **只把采集器持有的那个连接**的输入
    #    送进目标下行通道 —— 2026-08-09 实测,另开一个连接写 `STOP`/`RANGE`
    #    目标端毫无反应(顺带说明 gui_server.stop() 里那句 STOP-over-telnet
    #    从来没生效过,停止一直是靠 killpg 兜的)。
    #    所以外部想给固件下命令,只能把命令交给采集器,由它用自己的 socket 转发。
    #    机制:append-only 文本文件,一行一条命令;这里按读位置只发新增行,
    #    不截断文件 ⇒ 无写读竞态,事后还能查发过什么。
    last_cmd_poll = 0.0
    cmd_pos = 0
    with sock:
        while True:
            # 与重发同理:必须在循环顶部按**挂钟时间**判。数据以 8 样本/秒连续流入时
            # recv 永不超时,挂在超时分支上的轮询一次都不会执行。
            if cmd_file is not None and \
                    time.monotonic() - last_cmd_poll >= CMD_POLL_INTERVAL_S:
                last_cmd_poll = time.monotonic()
                try:
                    if cmd_file.exists():
                        with cmd_file.open("r", encoding="utf-8") as fh:
                            fh.seek(cmd_pos)
                            fresh = fh.read()
                            cmd_pos = fh.tell()
                        for raw_cmd in fresh.splitlines():
                            raw_cmd = raw_cmd.strip()
                            if not raw_cmd or raw_cmd.startswith("#"):
                                continue
                            sock.sendall((raw_cmd + "\n").encode("ascii"))
                            print(f"[collect] 已转发命令:{raw_cmd}", file=sys.stderr)
                except OSError as exc:
                    print(f"[collect] ⚠️ 读命令文件失败:{exc}", file=sys.stderr)
            # 🔴 重发闸门必须在循环顶部按**时间**判,不能挂在 except socket.timeout 上。
            #    2026-08-09 踩过第二次:固件仍在吐上一轮数据时是 8 样本/秒连续流,
            #    `recv` 永不超时 ⇒ 挂在超时分支上的重发一次都不会执行。而"上一轮还在
            #    吐"恰恰就是需要重发的那个场景 —— 等于把重发放在了它唯一不可能触发
            #    的位置。现象:collector.log 只有最初那一条发送、没有「固件已确认」,
            #    rtt.log 里连 IT_START 都没有,界面「设备测量中」而曲线永远空。
            if trigger_state.pop("rearm", False) and trigger_bytes is not None:
                trigger_pending = True
                resends = 0
                warned_unacked = False
                last_trigger_at = 0.0   # 立刻重发,不等下一个间隔
                print("[collect] 复位后重新武装 START", file=sys.stderr)
            if trigger_pending and time.monotonic() - last_trigger_at >= TRIGGER_RESEND_INTERVAL_S:
                if resends < TRIGGER_MAX_RESENDS:
                    try:
                        sock.sendall(trigger_bytes)
                    except OSError:
                        return
                    resends += 1
                    last_trigger_at = time.monotonic()
                elif not warned_unacked:
                    # 预算用完还没确认 ⇒ 后面收到的样本会被 acquisition_started
                    # 门禁全部丢掉。必须在这里就喊出来,否则表现为「一直在测、
                    # 曲线永远空」,要等整轮结束才看到「共 0 样本」。
                    warned_unacked = True
                    print(f"[collect] 🔴 重发 {resends} 次仍未收到干净的 IT_START ⇒ "
                          f"接下来的样本会被全部丢弃。固件可能仍在跑上一轮"
                          f"(rtt.log 里若有 ms 持续增长的 S 行即是)。",
                          file=sys.stderr)
            try:
                chunk = sock.recv(4096)
                if not chunk:
                    return
                last_data = time.monotonic()
                buf += chunk
                while b"\n" in buf:
                    raw, buf = buf.split(b"\n", 1)
                    line = raw.decode("utf-8", "replace")
                    # 🔴 必须用与下游门禁**完全相同**的判据 parse_it_start()。
                    #    2026-08-09 踩过:这里原本写 `"IT_START" in line`(子串),
                    #    而固件上一轮样本流未停时,`IT_START` printk 会和样本行的
                    #    `S` 前缀在 RTT 里交织成 `SIT_START run=1 target_mv=200`。
                    #    子串判据匹配上了 ⇒ 停止重发并打印「固件已确认」,可是
                    #    IT_START_RE 是 `^IT_START…` 锚定的、匹配不上 ⇒
                    #    acquisition_started 一直 False,734 个样本被静默丢弃,
                    #    界面上就是「设备测量中」但曲线永远空着。**日志在骗人。**
                    #    判据一致后,残行不算确认 ⇒ 继续重发 ⇒ 固件打出干净的
                    #    `IT_START run=2 …`,门禁才真的开。
                    start_seen = (
                        parse_it_start(line) is not None
                        or parse_cv_start(line) is not None
                    )
                    if trigger_pending and start_seen:
                        trigger_pending = False
                        print(f"[collect] 固件已确认 {trigger_command}"
                              f"(重发 {resends} 次)", file=sys.stderr)
                    yield line
            except socket.timeout:
                # 重发已移到循环顶部按时间判(见上),这里只管空闲超时。
                if idle_timeout is not None and time.monotonic() - last_data > idle_timeout:
                    return


def tail_lines(path: Path, idle_timeout: float | None = None):
    """跟读文件新增行(类似 tail -f)。文件尚未出现时等待。"""
    while not path.exists():
        time.sleep(0.2)
    with path.open("r", errors="replace") as fh:
        buf = ""
        last_data = time.monotonic()
        while True:
            chunk = fh.read(4096)
            if chunk:
                last_data = time.monotonic()
                buf += chunk
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    yield line
            else:
                if idle_timeout is not None and time.monotonic() - last_data > idle_timeout:
                    return
                time.sleep(0.1)


def read_serial_lines(port: str, baudrate: int = 115200,
                      idle_timeout: float | None = None,
                      trigger: str | None = None,
                      cmd_file: Path | None = None,
                      serial_factory=None):
    """Read the unchanged line protocol from the V5.1 DATA CDC interface.

    CDC ACM ignores the nominal baud rate, but pyserial requires one.  Opening
    the port asserts DTR; V5.1 firmware intentionally waits for that event
    before emitting ``CFG_BOOT``, so the first configuration identity cannot
    be lost.  The second CDC interface is SMP and must never be passed here.
    """
    if serial_factory is None:
        try:
            import serial
        except ImportError as exc:
            raise SystemExit(
                "USB 串口采集需要 pyserial>=3.5;请重新执行 pip install -e ."
            ) from exc
        serial_factory = serial.Serial

    trigger_command = "START" if trigger == "FRESH_START" else trigger
    trigger_bytes = (
        (trigger_command.rstrip("\r\n") + "\n").encode("ascii")
        if trigger_command else None
    )
    trigger_pending = trigger_bytes is not None
    resends = 0
    warned_unacked = False
    last_trigger_at = 0.0
    last_cmd_poll = 0.0
    cmd_pos = 0
    buf = b""
    last_data = time.monotonic()

    try:
        stream_context = serial_factory(
            port=port, baudrate=baudrate, timeout=0.1, write_timeout=1.0
        )
    except Exception as exc:
        raise SystemExit(f"打不开 V5.1 DATA CDC {port}: {exc}") from exc

    print(f"[collect] 已打开 V5.1 DATA CDC {port}(DTR=1)", file=sys.stderr)
    with stream_context as stream:
        # A persistent V5.1 app emits CELL_V while no host owns DATA.  macOS can
        # hand the new reader the tail of that already-started line.  Sending
        # START immediately used to concatenate that tail with IT_START, hide
        # the anchored marker, and provoke a duplicate START/restart.  Discard
        # exactly the first physical line, preserving any complete following
        # lines already delivered in the same USB packet.
        sync_deadline = time.monotonic() + 2.0
        while b"\n" not in buf and time.monotonic() < sync_deadline:
            try:
                chunk = stream.read(4096)
            except Exception as exc:
                raise SystemExit(f"V5.1 DATA CDC 读取失败:{exc}") from exc
            if chunk:
                buf += chunk
                last_data = time.monotonic()
        if b"\n" in buf:
            _discarded, buf = buf.split(b"\n", 1)
            print("[collect] DATA CDC 已对齐到完整行边界", file=sys.stderr)
        else:
            buf = b""
            print("[collect] ⚠️ DATA CDC 2s 内无可对齐行",
                  file=sys.stderr)

        # Make every acquisition self-describing.  GET/STATUS are read-only
        # and are queued before START, so CFG_APPLIED/DERIVED/CONFIRMED and a
        # fresh STATUS1 audit are captured in the same raw log as the samples.
        preamble = b"GET\nSTATUS\n" + (trigger_bytes or b"")
        stream.write(preamble)
        stream.flush()
        if trigger_pending:
            resends = 1
            last_trigger_at = time.monotonic()

        while True:
            now = time.monotonic()
            if cmd_file is not None and now - last_cmd_poll >= CMD_POLL_INTERVAL_S:
                last_cmd_poll = now
                try:
                    if cmd_file.exists():
                        with cmd_file.open("r", encoding="utf-8") as fh:
                            fh.seek(cmd_pos)
                            fresh = fh.read()
                            cmd_pos = fh.tell()
                        for raw_cmd in fresh.splitlines():
                            raw_cmd = raw_cmd.strip()
                            if not raw_cmd or raw_cmd.startswith("#"):
                                continue
                            stream.write((raw_cmd + "\n").encode("ascii"))
                            stream.flush()
                            print(f"[collect] 已经 DATA CDC 转发命令:{raw_cmd}",
                                  file=sys.stderr)
                except OSError as exc:
                    print(f"[collect] ⚠️ 读命令文件失败:{exc}", file=sys.stderr)

            if trigger_pending and \
                    now - last_trigger_at >= SERIAL_TRIGGER_RESEND_INTERVAL_S:
                if resends < TRIGGER_MAX_RESENDS:
                    stream.write(trigger_bytes)
                    stream.flush()
                    resends += 1
                    last_trigger_at = now
                elif not warned_unacked:
                    warned_unacked = True
                    print(f"[collect] 🔴 DATA CDC 重发 {resends} 次仍未收到干净的 "
                          f"IT_START/CV_START", file=sys.stderr)

            try:
                chunk = stream.read(4096)
            except Exception as exc:
                raise SystemExit(f"V5.1 DATA CDC 读取失败:{exc}") from exc
            if chunk:
                last_data = time.monotonic()
                buf += chunk
                while b"\n" in buf:
                    raw, buf = buf.split(b"\n", 1)
                    line = raw.decode("utf-8", "replace")
                    start_seen = (
                        parse_it_start(line) is not None
                        or parse_cv_start(line) is not None
                    )
                    if trigger_pending and start_seen:
                        trigger_pending = False
                        print(f"[collect] 固件已确认 {trigger_command}"
                              f"(DATA CDC 重发 {resends} 次)", file=sys.stderr)
                    yield line
            elif idle_timeout is not None and \
                    time.monotonic() - last_data > idle_timeout:
                return


# --------------------------------------------------------------------------
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
# 🔴 三条标记行的尾部在 2026-08-10 加了 `ep=` / `tainted=`。这些正则原来都是
#    **严格锚定**(`\s*$`)的,不放开尾部会让新固件的完成/中止标记完全不被识别 ——
#    后果不是少一个字段,而是采集器永远等不到收尾、按 idle timeout 超时退出。
#    统一改成"尾部允许任意 key=value",既兼容旧日志也兼容以后再加字段。
_TAIL = r"(?:\s+[a-z_]+=\S+)*\s*$"
IT_DONE_RE = re.compile(
    r"^IT_DONE\s+native=(\d+)\s+expected=(\d+)\s+elapsed_ms=(\d+)" + _TAIL)
IT_START_RE = re.compile(r"^IT_START\s+run=(\d+)\s+target_mv=(-?\d+)" + _TAIL)
# reason 域也扩了:除 restart/stop,现在还有 invalid_cfg / vdd_oor(STATUS1 升级)
IT_ABORTED_RE = re.compile(
    r"^IT_ABORTED\s+reason=(restart|stop|hardware|invalid_cfg|vdd_oor)\s+native=(\d+)"
    r"\s+elapsed_ms=(\d+)" + _TAIL
)
CV_START_RE = re.compile(
    r"^CV_START\s+run=(\d+)\s+low_mv=(-?\d+)\s+high_mv=(-?\d+)\s+"
    r"rate_mv_s=(\d+)\s+cycles=(\d+)" + _TAIL
)
CV_DONE_RE = re.compile(
    r"^CV_DONE\s+native=(\d+)\s+expected=(\d+)\s+elapsed_ms=(\d+)\s+cycles=(\d+)"
    + _TAIL
)
CV_ABORTED_RE = re.compile(
    r"^CV_ABORTED\s+reason=(restart|stop|hardware|invalid_cfg|vdd_oor)\s+native=(\d+)"
    r"\s+elapsed_ms=(\d+)" + _TAIL
)
POTENTIAL_FAULT_RE = re.compile(r"^POTENTIAL_FAULT\s+.+$")
# 电极电压连采行(固件 CELL_V)。System ADC 与 Sensor ADC 在 AUTO 下并行,
# 速率由 SYS_PERIOD 决定(≈1Hz),与电流样本(8Hz)**不同步** ⇒ 必须落到独立 CSV,
# 塞进电流 CSV 会错行。
# System ADC PGA 增益码(与固件 max30131_regs.h 同值):0=2x 1=1x 2=0.5x 3=0.25x
SYSADC_GAIN_0P5X = 2

CELL_V_RE = re.compile(
    r"^CELL_V\s+ms=(-?\d+)\s+idle=(-?\d+)\s+we_mv=(-?\d+)\s+re_mv=(-?\d+)\s+"
    r"ce_mv=(-?\d+)\s+wo_mv=(-?\d+)\s+e_mv=(-?\d+)\s+we_code=(\d+)\s+"
    r"re_code=(\d+)\s+ce_code=(\d+)\s+wo_code=(\d+)"
    # ep / ocp 是 2026-08-10 追加的可选尾组。⚠️ 这条正则原来严格锚定,
    # 若不放开就会**直接打断 cellv.csv 落盘**(一行都匹配不上,静默产出空文件)。
    r"(?:\s+ep=(\d+))?(?:\s+ocp=([01]))?(?:\s+dropped=(\d+))?(?:\s+vgain=(\d+))?"
    # vdd_mv / vdd_code 是 2026-08-11 追加的可选尾组(tag 0xE0)。**都可能是 -1**
    # ⇒ 必须允许负号:-1 表示"一次都没收到过 VDD 词",与"读到 0V"是两件事。
    r"(?:\s+vdd_mv=(-?\d+))?(?:\s+vdd_code=(-?\d+))?\s*$"
)
CELL_V_COLUMNS = (
    "host_unix_s", "dev_ms", "idle_mode", "we_mv", "re_mv", "ce_mv", "wo_mv",
    "e_mv", "we_code", "re_code", "ce_code", "wo_code", "epoch", "ocp",
    "dropped",
    # System ADC 的 PGA 增益码(0=2x 1=1x 2=0.5x 3=0.25x)。
    # 🔴 必须落盘:它决定 code→mV 的换算系数,而增益会随放大器状态切换
    # ⇒ 事后光有 code 没有 vgain 是解释不了的。
    "vgain",
    # VDD 实测(tag 0xE0,走 SYS_PWR_GAIN=0.25× ⇒ LSB 1.5mV,满量程 6144mV)。
    # 🔴 -1 = 没收到过,不是 0V。它决定 headroom 上限 VDD−1.1V,是判断
    # 「V_WE 会不会超共模上限」的唯一实测依据,不能只靠编译期假定的 3000。
    "vdd_mv", "vdd_code",
)


def parse_cell_v(line: str) -> list[str] | None:
    """把一行 CELL_V 解析成 CSV 字段;不是该行则返回 None。"""
    match = CELL_V_RE.match(ANSI_RE.sub("", line).strip())
    if match is None:
        return None
    # 🔴 缺失字段的默认值**逐个**给,不能一律 0:
    #    `vgain` 缺失代表"2026-08-11 之前的固件",而那些版本固定用 0.5×(码 2)。
    #    给 0 会被解释成 2×、把旧日志里的电位读数放大 4 倍 —— mV 列本来是对的,
    #    只有拿 code 复算的人会中招,属于典型的静默错算。
    #    `vdd_mv`/`vdd_code` 缺失代表"2026-08-11 之前的固件,根本没选 VDD 通道"
    #    ⇒ 默认给 **-1**(= 无此数据),给 0 会被读成"VDD 测到 0V"即掉电。
    defaults = ("0",) * 11 + ("0", "0", "0", str(SYSADC_GAIN_0P5X), "-1", "-1")
    groups = [g if g is not None else d
              for g, d in zip(match.groups(), defaults)]
    return [f"{time.time():.3f}", *groups]


# --------------------------------------------------------------------------
# 配置变更审计
# --------------------------------------------------------------------------
# 固件把每次启动/改参数拆成多行上报,每行自带 `ep=`。**拆行而不是一大行**是因为
# 上行 RTT 在 NO_BLOCK_SKIP 下丢的是整条写入 —— 拆开后丢一行 ≠ 丢全部,而且
# `CFG_APPLIED.nregs` 与 `CFG_REG i=/n=` 让丢行**可检测**(检测到就发 GET 重放)。
AUDIT_PREFIXES = (
    "CFG_BOOT", "CFG_APPLIED", "CFG_DERIVED", "CFG_REG", "CFG_CONFIRMED",
    "CFG_REJECT", "CFG_ROLLBACK", "CFG_FAULT", "CFG_NOOP",
    "AFE_STATUS", "REG_PEEK", "REG_POKE",
    "OCP_BEGIN", "OCP_DONE", "OCP_RESTORED", "OCP_REJECT",
    "RANGE_APPLIED", "RANGE_REJECT", "IT_TAINTED",
    # 阶段行:静置期没有电流样本,靠它上位机才知道"在静置"而不是"卡住了"
    "IT_PHASE",
)
_AUDIT_RE = re.compile(r"^(" + "|".join(AUDIT_PREFIXES) + r")(\s|$)")
_KV_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=(\S*)")


def _coerce(value: str) -> object:
    """`0x1F` / `-12` / 其它 → int / int / 原样字符串。"""
    if value.startswith(("0x", "0X")):
        try:
            return int(value, 16)
        except ValueError:
            return value
    try:
        return int(value)
    except ValueError:
        return value


def parse_audit(line: str) -> dict[str, object] | None:
    """把一行审计输出解析成 dict;不是审计行则返回 None。

    刻意保留 `raw`(原始整行):这些行是事后复盘"当时到底是什么配置"的唯一依据,
    解析器有 bug 时至少原文还在。
    """
    cleaned = ANSI_RE.sub("", line).strip()
    if _AUDIT_RE.match(cleaned) is None:
        return None
    kind = cleaned.split(None, 1)[0]
    event: dict[str, object] = {
        "host_unix_s": round(time.time(), 3),
        "kind": kind,
        "raw": cleaned,
    }
    for key, value in _KV_RE.findall(cleaned[len(kind):]):
        event[key] = _coerce(value)
    return event


# `cfg_events.csv` 的列 = 每个 epoch 一行宽表(给分析脚本按 epoch join 电流 CSV 用)。
# 只收 CFG_APPLIED + CFG_DERIVED 的字段,其余事件只进 audit.jsonl。
CFG_EVENT_COLUMNS = (
    "host_unix_s", "ep", "src", "nlines", "forced", "perturbs_cell", "nregs",
    "skipped",
    "fsr", "off", "conv", "conv_src", "period", "sysper", "clk40", "ioc",
    "e_mv", "vwe_mv", "idle", "cellv", "chop", "rs", "ios", "sel", "amps",
    "fsr_pa", "off_pa", "bits", "conv_ms", "period_ms", "idle_ppm",
    "lsb_frame_fa", "lsb_eff_fa", "rej50_db_x10", "rej50_worst_db_x10",
    "conv_alt", "red_max_pa", "ox_max_pa", "sat_margin", "sat_margin_pa",
    "sysbudget_ms", "sysper_ms", "daca", "dacb",
    "idle_warn", "headroom_warn", "sig_warn",
    "status1", "invalid_cfg", "confirmed",
)


class CfgEventAccumulator:
    """把同一个 epoch 的 APPLIED/DERIVED/CONFIRMED 三行合成一行宽表。

    ⚠️ 只在 `CFG_CONFIRMED`(或该 epoch 结束)时才落盘 —— 未确认的 epoch 不该
    出现在宽表里,否则分析脚本会把一个被回滚掉的配置当成生效过的配置。
    未确认的行仍在 audit.jsonl 里,不丢信息。
    """

    def __init__(self) -> None:
        self.pending: dict[int, dict[str, object]] = {}
        self.rows: list[list[str]] = []
        self._done: set[int] = set()

    def feed(self, event: dict[str, object]) -> list[str] | None:
        ep = event.get("ep")
        if not isinstance(ep, int):
            return None
        kind = event["kind"]
        if kind in ("CFG_APPLIED", "CFG_DERIVED"):
            row = self.pending.setdefault(ep, {"ep": ep, "confirmed": 0})
            row.update({k: v for k, v in event.items()
                        if k not in ("kind", "raw")})
            return None
        if kind == "CFG_CONFIRMED":
            row = self.pending.pop(ep, {"ep": ep})
            row.update({k: v for k, v in event.items()
                        if k not in ("kind", "raw")})
            row["confirmed"] = 1
            # 🔴 两道去重,否则宽表违反"每 epoch 一行"的契约:
            #   ① 没有 CFG_DERIVED(没有 bits)的行是**空壳**——开机时
            #      CFG_BOOT+CFG_CONFIRMED 会先到,派生量还没来,落进去就是一行全空
            #   ② 同一 epoch 只留第一条完整的(GET 重放会对同一 epoch 再报一次)
            if row.get("bits") in (None, ""):
                return None
            if ep in self._done:
                return None
            self._done.add(ep)
            out = [str(row.get(c, "")) for c in CFG_EVENT_COLUMNS]
            self.rows.append(out)
            return out
        if kind in ("CFG_ROLLBACK", "CFG_FAULT"):
            self.pending.pop(ep, None)  # 回滚掉的 epoch 不进宽表
        return None


def parse_it_done(line: str) -> tuple[int, int, int] | None:
    """Return firmware completion counts for the machine-readable IT marker."""
    match = IT_DONE_RE.match(ANSI_RE.sub("", line).strip())
    if match is None:
        return None
    return tuple(int(value) for value in match.groups())


def parse_potential_fault(line: str) -> str | None:
    """Return the machine-readable DAC audit failure, if present."""

    cleaned = ANSI_RE.sub("", line).strip()
    return cleaned if POTENTIAL_FAULT_RE.match(cleaned) else None


def parse_it_start(line: str) -> tuple[int, int] | None:
    """Return run number and target potential from an IT start marker."""

    match = IT_START_RE.match(ANSI_RE.sub("", line).strip())
    return None if match is None else tuple(int(value) for value in match.groups())


def parse_it_aborted(line: str) -> tuple[str, int, int] | None:
    """Return the reason, sample count and elapsed time for an aborted run."""

    match = IT_ABORTED_RE.match(ANSI_RE.sub("", line).strip())
    if match is None:
        return None
    reason, native, elapsed_ms = match.groups()
    return reason, int(native), int(elapsed_ms)


def parse_cv_start(line: str) -> tuple[int, int, int, int, int] | None:
    match = CV_START_RE.match(ANSI_RE.sub("", line).strip())
    return None if match is None else tuple(int(value) for value in match.groups())


def parse_cv_done(line: str) -> tuple[int, int, int, int] | None:
    match = CV_DONE_RE.match(ANSI_RE.sub("", line).strip())
    return None if match is None else tuple(int(value) for value in match.groups())


def parse_cv_aborted(line: str) -> tuple[str, int, int] | None:
    match = CV_ABORTED_RE.match(ANSI_RE.sub("", line).strip())
    if match is None:
        return None
    reason, native, elapsed_ms = match.groups()
    return reason, int(native), int(elapsed_ms)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="从 RTT/USB CDC 实时收数并落盘 CSV")
    ap.add_argument("--out", required=True, type=Path, help="输出 CSV 路径")
    ap.add_argument("--raw-log", type=Path,
                    help="可选:同时保存未解析的 RTT 原始行(含启动/标定日志)")
    ap.add_argument("--cell-v", type=Path,
                    help="电极电压连采 CSV(默认 <out 的 stem>-cellv.csv)。"
                         "速率由固件 SYS_PERIOD 定(≈1Hz),与电流样本不同步,"
                         "所以必须独立成文件")
    ap.add_argument("--audit", type=Path,
                    help="配置变更审计 jsonl(默认 <out 的 stem>-audit.jsonl)。"
                         "同时在旁边写 <...>-audit-cfg.csv:每个**已确认**的 epoch "
                         "一行宽表,便于按 epoch 分段解释电流 CSV")

    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--start-jlink", action="store_true",
                     help="★推荐★ 自己起 RTT 桥(JLinkExe/OpenOCD)并读 telnet")
    measure_cmd_help = ("命令文件(append-only,一行一条,如 `RANGE 2 5`)。"
                        "采集器用自己的 RTT socket 转发 —— 另开连接写下行无效,见模块内注释")
    ap.add_argument("--cmd-file", type=Path, default=None, help=measure_cmd_help)
    src.add_argument("--socket", metavar="HOST:PORT",
                     help="连已在跑的 RTT telnet(如 127.0.0.1:19021)")
    src.add_argument("--tail", type=Path, help="跟读一个 RTT 日志文件")
    src.add_argument("--serial", metavar="PORT",
                     help="V5.1 DATA CDC,如 /dev/cu.usbmodemXXXX。不得填 SMP 口")

    ap.add_argument("--elf", type=Path, default=DEFAULT_ELF,
                    help=f"用于自动提取 RTT 控制块地址的 ELF(默认 {DEFAULT_ELF})")
    ap.add_argument("--rtt-address", type=lambda s: int(s, 0), default=None,
                    help="手动给 RTT 控制块地址(给了就不查 ELF)")
    ap.add_argument("--port", type=int, default=RTT_TELNET_PORT)
    ap.add_argument("--probe-serial", default=None,
                    help="多探头时指定 S/N(本机那支克隆板是 29734569)")
    ap.add_argument("--no-reset-before-read", dest="reset_before_read",
                    action="store_false", default=True,
                    help="不要在开始读 RTT 前复位目标。"
                         "🔴 默认是**复位**,因为 RTT 上行缓冲在目标复位前不会清空:"
                         "设备无人看管跑了一阵后,缓冲里压满上一次开机的字节,"
                         "collector 一连上就会把它们当本轮数据解析落盘 —— "
                         "2026-08-10 实测因此得出过完全错误的硬件结论。"
                         "复位会跑 SEGGER_RTT_Init(),缓冲指针归零 ⇒ 读到的一定是本轮的。"
                         "代价:每次采集从固件编译期默认值起步(运行时 SET 不跨轮保留),"
                         "且极化被打断 —— 但紧接着就是 Quiet Time 重新极化,与 CHI 流程一致。")
    ap.add_argument("--reset-before-read", action="store_true",
                    help="启动 J-Link RTT 后复位并运行目标;适合开始一轮新测量")
    ap.add_argument("--duration", type=float, default=None,
                    help="收多少秒后自动停(默认一直收,Ctrl-C 停)")
    ap.add_argument("--idle-timeout", type=float, default=None,
                    help="多少秒没有新数据就退出")
    ap.add_argument("--progress-every", type=int, default=10,
                    help="每 N 个样本打一次进度到 stderr")
    ap.add_argument("--it-10hz", action="store_true",
                    help="10Hz i-t 工作流:接受固件 AUTO 原生约8Hz样本,交由上位机重采样")
    ap.add_argument("--cv", action="store_true",
                    help="CV 工作流:等待 CV 标记并保存逐点电位、圈数与方向")
    ap.add_argument("--trigger", default=None,
                    help="连接 RTT 后发送命令，并等待对应 IT_START 后再收数")
    args = ap.parse_args(argv)

    trigger_state: dict[str, Any] = {}
    proc: subprocess.Popen | None = None
    if args.start_jlink:
        addr = args.rtt_address or find_rtt_address(args.elf)
        proc = start_jlink_rtt(addr, args.probe_serial, args.port,
                               args.reset_before_read)
        lines = read_socket_lines("127.0.0.1", args.port, cmd_file=args.cmd_file,
                                  idle_timeout=args.idle_timeout,
                                  trigger=args.trigger)
    elif args.socket:
        host, _, port_s = args.socket.partition(":")
        lines = read_socket_lines(host or "127.0.0.1", int(port_s or args.port),
                                  cmd_file=args.cmd_file,
                                  idle_timeout=args.idle_timeout,
                                  trigger=args.trigger,
                                  trigger_state=trigger_state)
    elif args.serial:
        lines = read_serial_lines(args.serial, cmd_file=args.cmd_file,
                                  idle_timeout=args.idle_timeout,
                                  trigger=args.trigger)
    else:
        lines = tail_lines(args.tail, args.idle_timeout)

    samples: list[Sample] = []
    junk = 0
    # ══════════════════════════════════════════════════════════════════
    # 🔴 固件复位边界检测(2026-08-10 加,起因是它害我误诊了一整轮)
    # ══════════════════════════════════════════════════════════════════
    # RTT **上行缓冲在目标复位后不清空**:collector 一连上,JLinkExe 先 reset 目标,
    # 但缓冲里还压着上一次开机没被读走的字节。这些字节会被照常解析、照常落盘,
    # 于是一份"新采集"的 CSV 头部混着几十行**上一次开机**的数据。
    # 实测后果:我据此得出"RE/CE 满量程乱摆、电极悬空"的结论,而同一轮的
    # 原始词流其实干净得很(E = 200±1 mV)。**静默错归比缺数据坏得多。**
    # 判据:dev_ms 回退(固件时基单调)或 CFG_BOOT 行。两者都不依赖上位机时钟。
    last_dev_ms: int | None = None
    boot_boundaries = 0

    def note_boot_boundary(why: str) -> None:
        """处置一次固件复位边界。

        🔴 **必须分两种情况**,否则修 bug 会修出更坏的 bug(2026-08-10 亲历):
          ① 还没收到任何本轮样本 ⇒ 这条边界分隔的是"残留缓冲 vs 本轮",
             丢弃 + 重置门禁 + **重新武装 trigger** 都是对的。
          ② 已经收到样本 ⇒ 这是一次**运行中复位**,本轮作废,但绝不能"丢弃后
             继续跑" —— 那会让人以为还在采,实际数据已经断了。响铃并结束。
        我第一版无条件走 ①:于是一轮完整的 536 样本采集(固件侧全对)被
        collector 全部丢掉、trigger 又没重发,最后落成一个空 CSV。
        **"防止错归"不能以"销毁正确数据"为代价。**
        """
        nonlocal boot_boundaries, last_dev_ms
        boot_boundaries += 1
        last_dev_ms = None
        marker = f"# --- 固件复位({why}) ---"
        for handle in (out_ref.get("cur"), cell_v_out):
            if handle is not None:
                handle.write(marker + "\n")
        # 🔴 **只警告 + 标记,绝不销毁数据、绝不动门禁。**
        #
        # 我的第一版在这里 samples.clear() + 重置 acquisition_started + 重发 trigger。
        # 结果:一轮固件侧完全正确的 536 样本采集被 collector 整段丢掉,落成空 CSV
        # (残留缓冲里的 CELL_V 把 last_dev_ms 抬到 90s,本轮的 18s 一来就判"复位")。
        # 教训:**在一个启发式判据上挂销毁性动作,比它要修的错归更坏。**
        # 根因已经从源头解决 —— 默认 --reset-before-read 让 RTT 缓冲在开读前归零。
        # 这里保留检测只为一件事:万一还是出现了,人必须看得到,而不是静默。
        print(f"[collect] ⚠️ 检测到 dev_ms 不单调({why})。若你用了 --no-reset-before-read,"
              f"这很可能是上一次开机的残留数据混进来了;CSV 里已插入标记行。"
              f"**本轮数据未被丢弃**,请按标记行自行切分", file=sys.stderr)

    out_ref: dict[str, Any] = {"cur": None}
    potential_fault: str | None = None
    aborted_reason: str | None = None
    acquisition_started = args.trigger is None
    stop = False
    t0 = time.monotonic()
    new_file = not args.out.exists() or args.out.stat().st_size == 0

    def _on_sigint(_sig, _frm):
        nonlocal stop
        stop = True
        print("\n[collect] 收到 Ctrl-C,收尾…", file=sys.stderr)

    signal.signal(signal.SIGINT, _on_sigint)
    # GUI 停止测量走 killpg(SIGTERM)。SIGTERM 的默认动作会立刻终止本进程,
    # 下面的 finally 不执行 —— 探头仍会释放(进程一死,stdin 管道 EOF 就把
    # JLinkExe 带走,2026-08-09 已实测),但 CSV 收尾与统计行不会写完。
    # 接住它是为了让用户点「停止」时数据文件是完整收尾的。
    signal.signal(signal.SIGTERM, _on_sigint)

    raw_out = args.raw_log.open("w", buffering=1) if args.raw_log else None
    # 电极电压独立 CSV,默认放在电流 CSV 旁边(<stem>-cellv.csv)。
    cell_v_path = args.cell_v or args.out.with_name(args.out.stem + "-cellv.csv")
    cell_v_new = not cell_v_path.exists() or cell_v_path.stat().st_size == 0
    cell_v_out = cell_v_path.open("a", buffering=1)
    cell_v_rows = 0
    if cell_v_new:
        cell_v_out.write("# pA-Converter V4.0 电极电压连采(System ADC,与电流并行)\n")
        cell_v_out.write("# e_mv = we_mv - re_mv;code 撞 0 或 4095 = 超量程削顶\n")
        cell_v_out.write(",".join(CELL_V_COLUMNS) + "\n")
    # 配置变更审计。两份产物,用途不同:
    #   audit.jsonl   每行一个事件(含原始行)—— 复盘"当时到底发生了什么"
    #   cfg_events.csv 每 epoch 一行宽表 —— 给分析脚本按 epoch join 电流 CSV
    audit_path = args.audit or args.out.with_name(args.out.stem + "-audit.jsonl")
    cfg_csv_path = audit_path.with_name(audit_path.stem + "-cfg.csv")
    audit_new = not cfg_csv_path.exists() or cfg_csv_path.stat().st_size == 0
    audit_out = audit_path.open("a", buffering=1)
    cfg_csv_out = cfg_csv_path.open("a", buffering=1)
    audit_acc = CfgEventAccumulator()
    audit_rows = 0
    if audit_new:
        cfg_csv_out.write(",".join(CFG_EVENT_COLUMNS) + "\n")
    try:
        with args.out.open("a", buffering=1) as out:
            out_ref["cur"] = out
            if new_file:
                method = "CV" if args.cv else "IT"
                board = "V5.1" if args.serial else "V4.0"
                out.write(f"# pA-Converter {board} {method} 实时采集\n")
                out.write(f"# 起始 unix 时间: {time.time():.3f}\n")
                out.write(",".join(CSV_COLUMNS) + "\n")

            for line in lines:
                if stop:
                    break
                if args.duration and time.monotonic() - t0 > args.duration:
                    print(f"[collect] 到达 --duration {args.duration}s,停止",
                          file=sys.stderr)
                    break

                if raw_out is not None:
                    raw_out.write(line.rstrip("\r\n") + "\n")

                # RTT 里混着 JLinkExe 横幅、Zephyr LOG(带 ANSI 色码)与数据行
                clean_line = ANSI_RE.sub("", line)
                started = parse_it_start(clean_line)
                if started is not None:
                    acquisition_started = True
                    run_number, target_mv = started
                    print(f"[collect] 固件开始第 {run_number} 轮 IT:E={target_mv}mV",
                          file=sys.stderr)
                    continue
                cv_started = parse_cv_start(clean_line)
                if cv_started is not None:
                    acquisition_started = True
                    run_number, low_mv, high_mv, rate_mv_s, cycles = cv_started
                    print(f"[collect] 固件开始第 {run_number} 轮 CV:"
                          f"{low_mv}→{high_mv}mV,{rate_mv_s}mV/s,{cycles}圈",
                          file=sys.stderr)
                    continue
                aborted = parse_cv_aborted(clean_line) if args.cv else parse_it_aborted(clean_line)
                if aborted is not None:
                    reason, native, elapsed_ms = aborted
                    print(f"[collect] 固件中止上一轮:{reason},native={native},"
                          f"elapsed={elapsed_ms}ms", file=sys.stderr)
                    if acquisition_started:
                        aborted_reason = reason
                        break
                    # FRESH_START deliberately stops a stale firmware run before
                    # waiting for the following START marker.
                    continue
                fault = parse_potential_fault(clean_line)
                if fault is not None:
                    potential_fault = fault
                    print(f"[collect] 电位寄存器审计失败:{fault}", file=sys.stderr)
                    continue
                audit = parse_audit(clean_line)
                if audit is not None:
                    if audit["kind"] == "CFG_BOOT":
                        # 默认已复位 ⇒ CFG_BOOT 就是**本轮**的第一行,不是边界。
                        # 只把它作为"本轮固件从头开始了"的确认打出来。
                        print(f"[collect] 固件启动确认:{audit['raw']}", file=sys.stderr)
                    # 🔴 同样**不受 acquisition_started 门禁** —— 开机的
                    #    CFG_BOOT/CFG_DERIVED 正是最该留下的两行,它们比 START 早。
                    audit_out.write(json.dumps(audit, ensure_ascii=False) + "\n")
                    audit_rows += 1
                    if audit_acc.feed(audit) is not None:
                        cfg_csv_out.write(",".join(audit_acc.rows[-1]) + "\n")
                    if audit["kind"] in ("CFG_REJECT", "CFG_FAULT", "CFG_ROLLBACK"):
                        print(f"[collect] 🔴 {audit['raw']}", file=sys.stderr)
                    continue
                cellv = parse_cell_v(clean_line)
                if cellv is not None:
                    dev_ms = int(cellv[1])
                    if last_dev_ms is not None and dev_ms < last_dev_ms:
                        note_boot_boundary(f"dev_ms {last_dev_ms}→{dev_ms} 回退")
                    last_dev_ms = dev_ms
                    # 🔴 刻意**不受 acquisition_started 门禁**:idle 期间的电极电位
                    #    正是我们要看的东西(断开时电解池停在哪个电位),不能等 START。
                    if cell_v_out is not None:
                        cell_v_out.write(",".join(cellv) + "\n")
                    cell_v_rows += 1
                    continue
                done = parse_cv_done(clean_line) if args.cv else (
                    parse_it_done(clean_line) if args.it_10hz else None
                )
                if done is not None:
                    if not acquisition_started:
                        continue
                    native, expected, elapsed_ms = done[:3]
                    method = "CV" if args.cv else "IT"
                    print(f"[collect] 固件报告 {method} 完成:{native}/{expected} 样本,"
                          f"elapsed={elapsed_ms}ms,立即收尾", file=sys.stderr)
                    break
                s = parse_line(clean_line)
                if s is None:
                    if line.strip():
                        junk += 1
                    continue

                if last_dev_ms is not None and s.ms < last_dev_ms:
                    note_boot_boundary(f"样本 dev_ms {last_dev_ms}→{s.ms} 回退")
                last_dev_ms = s.ms

                if not acquisition_started:
                    continue

                samples.append(s)
                # 🔴 用 record.py 的 sample_to_row —— CSV 字段顺序的单一真源
                out.write(",".join(sample_to_row(s, time.time())) + "\n")
                if args.progress_every and len(samples) % args.progress_every == 0:
                    flag = ""
                    if s.sat:
                        flag = f"  🔴SAT={s.sat}"
                    potential = (
                        f" | {s.potential_mv / 1000:+.3f} V · 圈 {s.cycle}"
                        if s.potential_mv is not None else ""
                    )
                    print(f"[collect] {len(samples)} 样本 | 最新 "
                          f"{s.fa / 1_000_000:.3f} nA (counts={s.counts})"
                          f"{potential}{flag}",
                          file=sys.stderr)
    finally:
        if raw_out is not None:
            raw_out.close()
        cell_v_out.close()
        audit_out.close()
        cfg_csv_out.close()
        if boot_boundaries:
            print(f"[collect] ⚠️ 本次共检测到 {boot_boundaries} 次固件复位边界;"
                  f"CSV 里已插入 `# --- 固件复位` 标记行", file=sys.stderr)
        if audit_rows:
            print(f"[collect] 配置审计 {audit_rows} 条 → {audit_path}"
                  f"({len(audit_acc.rows)} 个已确认 epoch → {cfg_csv_path})",
                  file=sys.stderr)
        else:
            print("[collect] ⚠️ 未收到任何配置审计行 —— 固件版本可能早于 2026-08-10",
                  file=sys.stderr)
        if cell_v_rows:
            print(f"[collect] 电极电压 {cell_v_rows} 组 → {cell_v_path}", file=sys.stderr)
        else:
            print("[collect] ⚠️ 未收到任何 CELL_V 行 —— 若固件 WP_CELLV_ENABLE=true,"
                  "首查 0x55 的 SYS_SELECT 位号假设", file=sys.stderr)
        if proc is not None:
            # 🔴 用 terminate,别用 pkill —— pkill 会把这支克隆探头打掉线
            try:
                if proc.stdin:
                    proc.stdin.close()
            except OSError:
                pass
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

    # ---- 收尾完整性检查 ----
    print(f"\n[collect] 共 {len(samples)} 样本,忽略 {junk} 行非数据输出 → {args.out}",
          file=sys.stderr)
    if aborted_reason is not None:
        print(f"[collect] 本轮已由硬件中止:{aborted_reason}", file=sys.stderr)
        return 3

    if not samples:
        print("⚠️ 一个样本都没收到。排查顺序:", file=sys.stderr)
        if args.serial:
            print("   1) 传入的是 DATA CDC 而不是 SMP CDC 吗", file=sys.stderr)
            print("   2) CFG_BOOT/IT_READY 是否出现在 raw log", file=sys.stderr)
            print("   3) USB-C 插头方向是否为当前硬件可枚举的方向", file=sys.stderr)
        else:
            print("   1) ioreg -p IOUSB -l -w 0 | grep J_Link   ← 探头还在 USB 上?",
                  file=sys.stderr)
            print("   2) V8.80 的 JLinkExe 能 connect 吗(V9.46 一定不行)", file=sys.stderr)
            print("   3) RTT 地址对吗:nm zephyr.elf | grep _SEGGER_RTT", file=sys.stderr)
            print("   4) 固件真的在跑吗(halt 看 PC;空片是 0xFFFFFFFE)", file=sys.stderr)
        return 1

    rep = check_integrity(samples)
    print(f"[collect] {rep.summary()}", file=sys.stderr)

    if potential_fault is not None:
        print("🔴 本轮检测到电位寄存器跳变,数据已保存但禁止进入标定或预测。",
              file=sys.stderr)
        return 2

    if (rep.seq_gaps or rep.ovf_events or rep.bad_tag
            or (rep.manual_mode and not (args.it_10hz or args.cv))
            or rep.saturated):
        print("⚠️ 完整性检查有异常,先解释清楚再拿去算指标:", file=sys.stderr)
        if rep.seq_gaps:
            print(f"   - 序号断裂 {rep.seq_gaps} 处、推算丢样 {rep.missing_samples} 个"
                  " → 轮询间隔太长或 RTT 缓冲溢出", file=sys.stderr)
        if rep.ovf_events:
            print(f"   - FIFO 溢出 {rep.ovf_events} 批 → 调小 POLL_INTERVAL_MS",
                  file=sys.stderr)
        if rep.bad_tag:
            print(f"   - 异常 tag {rep.bad_tag} 个 → 非 Sensor1-DC 样本混入",
                  file=sys.stderr)
        if rep.manual_mode and not (args.it_10hz or args.cv):
            print(f"   - 手动模式样本 {rep.manual_mode} 个 → AUTO 未真正生效",
                  file=sys.stderr)
        if rep.saturated:
            print(f"   🔴 **饱和样本 {rep.saturated} 个**"
                  f"(LOW {rep.sat_low} / HIGH {rep.sat_high})", file=sys.stderr)
            if rep.sat_low:
                print("      LOW = counts 逼近 0:还原电流吃光 offset,"
                      "**WE 已失恒电位控制** ⇒ 这些点不是测量结果,"
                      "算 σ / 标定曲线前必须剔除;并考虑升 offset 档位",
                      file=sys.stderr)
            if rep.sat_high:
                print("      HIGH = counts 逼近满量程:氧化方向超出 FSR−offset",
                      file=sys.stderr)
        print("   🔴 丢样会让 Allan 偏差与 PSD 失真(时间轴不均匀),别直接下结论。",
              file=sys.stderr)
    else:
        mode_text = ("CV 逐点电位" if args.cv else
                     "10Hz 工作流(原生约8Hz)" if args.it_10hz else "全 AUTO")
        print(f"✅ 完整性检查通过(序号连续、无溢出、tag 合法、{mode_text})",
              file=sys.stderr)

    print(f"\n下一步:python3 -m pa_host.analyze {args.out} --fsr-pa 50000 "
          f"--dev-clock --plot {args.out.with_suffix('.png')}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
