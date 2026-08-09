#!/usr/bin/env python3
"""实时收数落盘 —— 从固件 RTT 读行,校验后追加写 CSV。

用途
    A 段(实时):把固件经 SEGGER RTT 吐出的行协议落成 CSV,边收边做完整性检查。
    B 段(离线)交给 analyze.py。两段刻意分开:收数不能因为分析崩掉而丢数据。

三种取数来源
    --start-jlink   ★推荐★ 自己起 JLinkExe(SetRTTAddr + rtt start),从 telnet 19021 读
    --socket H:P    连一个已经在跑的 RTT telnet 服务
    --tail FILE     跟读一个 RTT 日志文件(你自己起的 logger)

用法
    # 最常用:一条命令搞定(RTT 地址自动从 ELF 提取)
    python3 -m pa_host.collect --start-jlink --out run.csv \\
        --elf /tmp/pabuild/firmware/zephyr/zephyr.elf

    # 回板前用合成数据验证整条链(不需要硬件)
    python3 -m pa_host.synth /tmp/fake.csv --hours 1
    python3 -m pa_host.analyze /tmp/fake.csv --fsr-pa 50000

🔴 两个必须知道的坑(2026-07-31 实测)
    1. **必须用 J-Link V8.80**,不能用 /usr/local/bin 的 V9.46 —— V9.x 的 DLL 丢了对这支
       克隆探头固件的 legacy 回退,连不上目标。
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
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

from .record import (
    CSV_COLUMNS,
    Sample,
    check_integrity,
    parse_line,
    sample_to_row,
)

# 🔴 只能用 V8.80;路径来自 STM32CubeIDE 自带的 J-Link 工具链
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
    """起 JLinkExe,SetRTTAddr + rtt start,RTT 数据出到 telnet `port`。

    stdin 保持打开 —— JLinkExe 一旦读到 EOF 就退出,RTT 服务随之关闭。
    """
    if not JLINK_EXE.exists():
        sys.exit(
            f"找不到 {JLINK_EXE}\n"
            "→ 需要 J-Link **V8.80**。注意克隆探头配 V9.x 连不上(见模块 docstring)。"
        )

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
    proc.stdin.flush()   # 🔴 不关 stdin:关了 JLinkExe 就退出
    return proc


def read_socket_lines(host: str, port: int, connect_timeout: float = 20.0,
                      idle_timeout: float | None = None,
                      trigger: str | None = None):
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
    trigger_bytes = (
        (trigger.rstrip("\r\n") + "\n").encode("ascii") if trigger else None
    )
    trigger_pending = trigger_bytes is not None
    resends = 0
    warned_unacked = False
    if trigger_bytes:
        sock.sendall(trigger_bytes)
        print(f"[collect] 已发送硬件命令:{trigger}(未确认前每秒重发)",
              file=sys.stderr)
    sock.settimeout(1.0)
    buf = b""
    last_data = time.monotonic()
    last_trigger_at = time.monotonic()
    with sock:
        while True:
            # 🔴 重发闸门必须在循环顶部按**时间**判,不能挂在 except socket.timeout 上。
            #    2026-08-09 踩过第二次:固件仍在吐上一轮数据时是 8 样本/秒连续流,
            #    `recv` 永不超时 ⇒ 挂在超时分支上的重发一次都不会执行。而"上一轮还在
            #    吐"恰恰就是需要重发的那个场景 —— 等于把重发放在了它唯一不可能触发
            #    的位置。现象:collector.log 只有最初那一条发送、没有「固件已确认」,
            #    rtt.log 里连 IT_START 都没有,界面「设备测量中」而曲线永远空。
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
                    if trigger_pending and parse_it_start(line) is not None:
                        trigger_pending = False
                        print(f"[collect] 固件已确认 {trigger}"
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


# --------------------------------------------------------------------------
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
IT_DONE_RE = re.compile(r"^IT_DONE\s+native=(\d+)\s+expected=(\d+)\s+elapsed_ms=(\d+)\s*$")
IT_START_RE = re.compile(r"^IT_START\s+run=(\d+)\s+target_mv=(-?\d+)\s*$")
IT_ABORTED_RE = re.compile(
    r"^IT_ABORTED\s+reason=(restart|stop)\s+native=(\d+)\s+elapsed_ms=(\d+)\s*$"
)
POTENTIAL_FAULT_RE = re.compile(r"^POTENTIAL_FAULT\s+.+$")


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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="从 RTT 实时收数并落盘 CSV")
    ap.add_argument("--out", required=True, type=Path, help="输出 CSV 路径")
    ap.add_argument("--raw-log", type=Path,
                    help="可选:同时保存未解析的 RTT 原始行(含启动/标定日志)")

    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--start-jlink", action="store_true",
                     help="★推荐★ 自己起 JLinkExe(SetRTTAddr + rtt start)并读 telnet")
    src.add_argument("--socket", metavar="HOST:PORT",
                     help="连已在跑的 RTT telnet(如 127.0.0.1:19021)")
    src.add_argument("--tail", type=Path, help="跟读一个 RTT 日志文件")

    ap.add_argument("--elf", type=Path, default=DEFAULT_ELF,
                    help=f"用于自动提取 RTT 控制块地址的 ELF(默认 {DEFAULT_ELF})")
    ap.add_argument("--rtt-address", type=lambda s: int(s, 0), default=None,
                    help="手动给 RTT 控制块地址(给了就不查 ELF)")
    ap.add_argument("--port", type=int, default=RTT_TELNET_PORT)
    ap.add_argument("--probe-serial", default=None,
                    help="多探头时指定 S/N(本机那支克隆板是 29734569)")
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
    ap.add_argument("--trigger", default=None,
                    help="连接 RTT 后发送命令，并等待对应 IT_START 后再收数")
    args = ap.parse_args(argv)

    proc: subprocess.Popen | None = None
    if args.start_jlink:
        addr = args.rtt_address or find_rtt_address(args.elf)
        proc = start_jlink_rtt(addr, args.probe_serial, args.port,
                               args.reset_before_read)
        lines = read_socket_lines("127.0.0.1", args.port,
                                  idle_timeout=args.idle_timeout,
                                  trigger=args.trigger)
    elif args.socket:
        host, _, port_s = args.socket.partition(":")
        lines = read_socket_lines(host or "127.0.0.1", int(port_s or args.port),
                                  idle_timeout=args.idle_timeout,
                                  trigger=args.trigger)
    else:
        lines = tail_lines(args.tail, args.idle_timeout)

    samples: list[Sample] = []
    junk = 0
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
    try:
        with args.out.open("a", buffering=1) as out:
            if new_file:
                out.write("# pA-Converter V4.0 实时采集\n")
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
                aborted = parse_it_aborted(clean_line)
                if aborted is not None:
                    reason, native, elapsed_ms = aborted
                    print(f"[collect] 固件中止上一轮:{reason},native={native},"
                          f"elapsed={elapsed_ms}ms", file=sys.stderr)
                    if reason == "stop" or acquisition_started:
                        aborted_reason = reason
                        break
                    continue
                fault = parse_potential_fault(clean_line)
                if fault is not None:
                    potential_fault = fault
                    print(f"[collect] 电位寄存器审计失败:{fault}", file=sys.stderr)
                    continue
                done = parse_it_done(clean_line) if args.it_10hz else None
                if done is not None:
                    if not acquisition_started:
                        continue
                    native, expected, elapsed_ms = done
                    print(f"[collect] 固件报告 IT 完成:{native}/{expected} 样本,"
                          f"elapsed={elapsed_ms}ms,立即收尾", file=sys.stderr)
                    break
                s = parse_line(clean_line)
                if s is None:
                    if line.strip():
                        junk += 1
                    continue

                if not acquisition_started:
                    continue

                samples.append(s)
                # 🔴 用 record.py 的 sample_to_row —— CSV 字段顺序的单一真源
                out.write(",".join(sample_to_row(s, time.time())) + "\n")
                if args.progress_every and len(samples) % args.progress_every == 0:
                    flag = ""
                    if s.sat:
                        flag = f"  🔴SAT={s.sat}"
                    print(f"[collect] {len(samples)} 样本 | 最新 "
                          f"{s.fa / 1000:.3f} pA (counts={s.counts}){flag}",
                          file=sys.stderr)
    finally:
        if raw_out is not None:
            raw_out.close()
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
            or (rep.manual_mode and not args.it_10hz)
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
        if rep.manual_mode and not args.it_10hz:
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
        mode_text = "10Hz 工作流(原生约8Hz)" if args.it_10hz else "全 AUTO"
        print(f"✅ 完整性检查通过(序号连续、无溢出、tag 合法、{mode_text})",
              file=sys.stderr)

    print(f"\n下一步:python3 -m pa_host.analyze {args.out} --fsr-pa 50000 "
          f"--dev-clock --plot {args.out.with_suffix('.png')}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
