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

两个必须知道的坑
    1. 部分探头不接受 JLinkExe 的命令行 ``-autoconnect`` 初始化，但可以用
       Commander 脚本中的 ``si/speed/device/connect`` 顺序稳定连接。因此烧录和
       目标核对使用这条显式连接路径；RTT 则优先使用随包的 libjaylink
       OpenOCD 保持一个长连接会话。
    2. 部分兼容探头可执行 Commander 的短内存事务，但新版 SEGGER DLL 的异步
       RTT 轮询持续报 memory read error。OpenOCD 无法建链时，本模块才按公开的
       SEGGER RTT 环形缓冲布局做带回读校验的 Commander 短事务作为回退。
       🔴 该地址**每次重新编译都可能变**,所以默认从 ELF 的 `_SEGGER_RTT` 符号自动提取,
       别硬编码。

⚠️ 这支克隆探头会整体掉出 USB,唯一恢复方式是物理拔插;**别用 pkill 杀 J-Link 进程**
   (会把它打掉线)。本模块用 Popen.terminate()。

快照日期
    2026-08-17
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
import tempfile
import threading
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
from . import runtime

# 优先使用用户系统中已安装的 SEGGER Commander；若不存在，回退到随包的
# libjaylink OpenOCD。公开分发包不能重分发 SEGGER 工具，因此这里必须覆盖官方
# 安装器与 STM32CubeIDE 的跨平台安装位置。
_IS_WIN = sys.platform == "win32"
BRIDGE_SHUTDOWN_COMMAND = b"SENSUS_BRIDGE_SHUTDOWN\n"

JLINK_V880_DIR_MACOS = Path(
    "/Applications/STM32CubeIDE.app/Contents/Eclipse/plugins/"
    "com.st.stm32cube.ide.mcu.externaltools.jlink.macos64_2.5.100.202509120932/tools/bin"
)
# Windows: SEGGER J-Link 默认安装路径
JLINK_DIR_WIN = Path("C:/Program Files/SEGGER/JLink")
# Windows: STM32CubeIDE 自带的 J-Link
JLINK_CUBEIDE_WIN = Path("C:/ST/STM32CubeIDE/STM32CubeIDE/plugins/"
    "com.st.stm32cube.ide.mcu.externaltools.jlink.win32_2.5.100.202509120932/tools/bin")

def _resolve_jlink_exe() -> Path:
    """Find a compatible system-installed J-Link Commander.

    The lab's V9-compatible probe implements the older J-Link USB protocol.
    SEGGER V8.80 is the newest release verified to support its SWD turnaround
    and Flash algorithm. Keep newer installations as a fallback, but prefer an
    explicit V8.80 installation when both are present. The environment override
    remains authoritative for genuine probes and future versions.
    """
    jlink_name = "JLink.exe" if _IS_WIN else "JLinkExe"
    override = os.environ.get("SENSUS_JLINK_EXE")
    if override:
        return Path(override).expanduser()
    candidates: list[Path] = []
    if _IS_WIN:
        program_roots = [
            Path(os.environ.get("ProgramFiles", "C:/Program Files")),
            Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)")),
        ]
        candidates.extend(
            root / "SEGGER" / "JLink_V880" / jlink_name
            for root in program_roots
        )
        candidates.append(JLINK_CUBEIDE_WIN / jlink_name)
        for root in program_roots:
            segger = root / "SEGGER"
            if segger.exists():
                candidates.extend(sorted(
                    segger.glob(f"JLink*/{jlink_name}"), reverse=True
                ))
        candidates.append(JLINK_DIR_WIN / jlink_name)
        for st_root in (Path("C:/ST"), program_roots[0] / "STMicroelectronics"):
            if st_root.exists():
                candidates.extend(sorted(
                    st_root.glob(
                        "**/com.st.stm32cube.ide.mcu.externaltools.jlink.*/"
                        f"tools/bin/{jlink_name}"
                    ),
                    reverse=True,
                ))
    else:
        candidates.extend((
            Path.home() / "Applications/SEGGER/JLink_V880" / jlink_name,
            Path("/Applications/SEGGER/JLink_V880") / jlink_name,
            JLINK_V880_DIR_MACOS / jlink_name,
        ))
        for segger in (Path("/Applications/SEGGER"), Path.home() / "Applications/SEGGER"):
            if segger.exists():
                candidates.extend(sorted(
                    segger.glob(f"JLink*/{jlink_name}"), reverse=True
                ))
        cubeide = Path("/Applications/STM32CubeIDE.app/Contents/Eclipse/plugins")
        if cubeide.exists():
            candidates.extend(sorted(
                cubeide.glob(
                    "com.st.stm32cube.ide.mcu.externaltools.jlink.*/"
                    f"tools/bin/{jlink_name}"
                ),
                reverse=True,
            ))
    found = shutil.which(jlink_name)
    if found:
        candidates.append(Path(found))

    seen: set[Path] = set()
    for candidate in candidates:
        try:
            canonical = candidate.resolve()
        except OSError:
            canonical = candidate
        if canonical in seen:
            continue
        seen.add(canonical)
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    return Path(f"/__sensus_no_system_jlink__/{jlink_name}")


JLINK_EXE = _resolve_jlink_exe()


def _resolve_openocd() -> tuple[Path, Path]:
    """选取启用 J-Link 驱动的 OpenOCD 及其 scripts 目录。"""
    configured = os.environ.get("SENSUS_OPENOCD_EXE")
    candidates: list[Path] = [Path(configured).expanduser()] if configured else []
    if runtime.is_frozen():
        # Portable builds place OpenOCD beside the staged ``workstation``
        # resources, rather than on PATH. Keep this after an explicit
        # environment override but before host-installed tools so a copied
        # app remains self-contained and reproducible.
        resource_roots: list[Path] = []
        try:
            resource_roots.append(runtime.project_dir().parent)
        except (OSError, RuntimeError):
            pass
        executable_parent = Path(sys.executable).resolve().parent
        resource_roots.extend((executable_parent, executable_parent.parent))
        seen_roots: set[Path] = set()
        for root in resource_roots:
            root = root.resolve()
            if root in seen_roots:
                continue
            seen_roots.add(root)
            candidates.append(root / "tools" / "openocd" / "bin" /
                             ("openocd.exe" if _IS_WIN else "openocd"))
    if _IS_WIN:
        openocd_name = "openocd.exe"
        candidates.extend([
            Path(os.environ.get("OPENOCD_HOME", "")) / "bin" / openocd_name,
            Path("C:/Program Files/OpenOCD/bin") / openocd_name,
            Path.home() / ".local/share/sensus-openocd-jlink/bin" / openocd_name,
            Path(shutil.which("openocd") or "openocd.exe"),
        ])
    else:
        candidates.extend([
            Path.home() / ".local/share/sensus-openocd-jlink/bin/openocd",
            Path("/opt/homebrew/bin/openocd"),
            Path("/usr/local/bin/openocd"),
            Path(shutil.which("openocd") or "/nonexistent/openocd"),
        ])
    executable = next((path for path in candidates if path.exists()), candidates[0])

    configured_scripts = os.environ.get("SENSUS_OPENOCD_SCRIPTS")
    script_candidates: list[Path] = (
        [Path(configured_scripts).expanduser()] if configured_scripts else []
    )
    if _IS_WIN:
        script_candidates.extend([
            executable.parent.parent / "share/openocd/scripts",
            Path(os.environ.get("OPENOCD_HOME", "")) / "share/openocd/scripts",
            Path("C:/Program Files/OpenOCD/share/openocd/scripts"),
            Path.home() / ".local/share/sensus-openocd-jlink/share/openocd/scripts",
        ])
    else:
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
if _IS_WIN:
    _ZEPHYR_SDK_NM_DEFAULT = Path(
        os.environ.get("ZEPHYR_SDK_HOME", str(Path.home() / "zephyr-sdk-1.0.1"))
    ) / "gnu/arm-zephyr-eabi/bin/arm-zephyr-eabi-nm.exe"
else:
    _ZEPHYR_SDK_NM_DEFAULT = Path(
        os.environ.get(
            "SENSUS_ZEPHYR_SDK_DIR",
            str(Path.home() / "sensus-toolchains/zephyr-sdk-1.0.1"),
        )
    ).expanduser() / "gnu/arm-zephyr-eabi/bin/arm-zephyr-eabi-nm"
ZEPHYR_SDK_NM = Path(
    os.environ.get("SENSUS_ARM_NM")
    or shutil.which("arm-zephyr-eabi-nm")
    or _ZEPHYR_SDK_NM_DEFAULT
)

DEVICE = "nRF52833_xxAA"
# Some older and compatible probes need a slow first SWD handshake even though
# they can run faster after the DAP is awake. The workstation traffic is small,
# so keeping the complete session at 100 kHz costs little and removes a class of
# intermittent first-connect failures seen at 1/4 MHz.
SPEED_KHZ = 100
JLINK_PROBE_ATTEMPTS = 2
RTT_TELNET_PORT = 19021
RTT_RAM_START = 0x20000000
RTT_RAM_END = 0x20020000
RTT_MAX_CHANNELS = 16
RTT_MAX_BUFFER_SIZE = 64 * 1024
RTT_BRIDGE_POLL_INTERVAL_S = 0.08
OPENOCD_RTT_START_TIMEOUT_S = 8.0
OPENOCD_RTT_START_POLL_INTERVAL_S = 0.1
# 触发命令未被固件确认前的重发间隔与最大次数。
# 🔴 按**挂钟时间**重发,不依赖 socket 空闲 —— 固件仍在吐上一轮数据时永不空闲。
TRIGGER_RESEND_INTERVAL_S = 1.0
TRIGGER_MAX_RESENDS = 20
SERIAL_TRIGGER_RESEND_INTERVAL_S = 3.0
# 命令文件轮询间隔(方案 C:外部命令经采集器 socket 转发给固件)
CMD_POLL_INTERVAL_S = 0.5
DEFAULT_ELF = Path("/tmp/pabuild/firmware/zephyr/zephyr.elf")
NRF52833_INFO_PART_ADDRESS = 0x10000100
NRF52833_INFO_PART_VALUE = 0x00052833


def jlink_connection_script(*commands: str, speed_khz: int = SPEED_KHZ) -> str:
    """Build the explicit connection sequence accepted by all tested probes."""
    lines = [
        "si SWD",
        f"speed {int(speed_khz)}",
        f"device {DEVICE}",
        "connect",
        *commands,
    ]
    return "\n".join(lines) + "\n"


def run_jlink_script(
    script: str,
    probe_serial: str | None = None,
    *,
    executable: Path | None = None,
    timeout_s: float = 15.0,
    cancel_event: threading.Event | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run one Commander script and always remove its temporary file."""
    executable = executable or JLINK_EXE
    if not executable.is_file():
        raise FileNotFoundError(f"J-Link Commander not found: {executable}")
    with tempfile.NamedTemporaryFile(
        "w", suffix=".jlink", delete=False, encoding="utf-8"
    ) as handle:
        handle.write(script)
        script_path = Path(handle.name)
    command = [str(executable), "-NoGui", "1", "-ExitOnError", "1"]
    if probe_serial:
        command += ["-SelectEmuBySN", str(probe_serial)]
    command += ["-CommanderScript", str(script_path)]
    try:
        if cancel_event is not None:
            if cancel_event.is_set():
                raise JLinkOperationCancelled("J-Link Commander operation cancelled")
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            deadline = time.monotonic() + max(1.0, timeout_s)
            while True:
                if cancel_event.is_set():
                    process.terminate()
                    try:
                        process.communicate(timeout=1.0)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.communicate()
                    raise JLinkOperationCancelled(
                        "J-Link Commander operation cancelled"
                    )
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    process.terminate()
                    try:
                        stdout, stderr = process.communicate(timeout=1.0)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        stdout, stderr = process.communicate()
                    raise subprocess.TimeoutExpired(
                        command, timeout_s, output=stdout, stderr=stderr
                    )
                try:
                    stdout, stderr = process.communicate(
                        timeout=min(0.1, remaining)
                    )
                except subprocess.TimeoutExpired:
                    continue
                return subprocess.CompletedProcess(
                    command, process.returncode, stdout, stderr
                )
        return subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(1.0, timeout_s),
        )
    finally:
        script_path.unlink(missing_ok=True)


class JLinkOperationCancelled(RuntimeError):
    """Raised when bridge shutdown interrupts an in-flight Commander call."""


class RTTControlBlockUnavailable(RuntimeError):
    """The expected RTT layout is absent from an otherwise readable target."""


def probe_jlink_target(
    probe_serial: str | None = None,
    *,
    executable: Path | None = None,
    timeout_s: float = 10.0,
) -> tuple[bool, str]:
    """Read nRF52833 INFO.PART without resetting or writing the target.

    Commander can return exit code zero after a failed ``connect``. Match the
    FICR value and retry once so a transient first SWD handshake never becomes
    either a false connected state or an unnecessary OpenOCD fallback.
    """
    script = jlink_connection_script(
        f"mem32 0x{NRF52833_INFO_PART_ADDRESS:08X} 1",
        "q",
    )
    expected = re.compile(
        rf"\b{NRF52833_INFO_PART_ADDRESS:08X}\s*=\s*"
        rf"{NRF52833_INFO_PART_VALUE:08X}\b",
        flags=re.IGNORECASE,
    )
    outputs: list[str] = []
    for attempt in range(JLINK_PROBE_ATTEMPTS):
        try:
            done = run_jlink_script(
                script,
                probe_serial,
                executable=executable,
                timeout_s=timeout_s,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            outputs.append(str(exc))
        else:
            output = f"{done.stdout}\n{done.stderr}"
            outputs.append(output)
            if done.returncode == 0 and expected.search(output):
                return True, "\n".join(outputs)
        if attempt + 1 < JLINK_PROBE_ATTEMPTS:
            time.sleep(0.15)
    return False, "\n".join(outputs)


def _parse_jlink_mem32(output: str, address: int, count: int) -> list[int]:
    """Parse an exact contiguous Commander ``mem32`` result."""
    words: dict[int, int] = {}
    for line in output.splitlines():
        match = re.match(
            r"^\s*([0-9A-Fa-f]{8})\s*=\s*"
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
            "JLinkExe 内存读取不完整，缺少地址:"
            + ", ".join(f"0x{item:08X}" for item in missing[:4])
        )
    return [words[item] for item in expected]


def _jlink_file_argument(path: Path) -> str:
    # Commander scripts consume native Windows paths directly; doubling each
    # backslash turns ``C:\\path`` into a different path on the target host.
    text = str(path.resolve())
    if any(character in text for character in ('"', "\n", "\r")):
        raise RuntimeError(f"JLinkExe 无法处理临时文件路径:{path}")
    return f'"{text}"'


class JLinkMemoryRTT:
    """Minimal RTT channel-0 transport built from verified memory operations.

    Some compatible probes can perform short Commander memory transactions but
    fail the asynchronous RTT APIs in newer SEGGER DLLs. This transport follows
    the public SEGGER RTT ring-buffer layout directly. Every pointer update and
    every down-channel payload is read back before it becomes visible to the
    target, so a transient SWD failure cannot silently become a command or a
    duplicate measurement.
    """

    _TRANSIENT_MARKERS = (
        "could not read memory",
        "could not write memory",
        "cannot connect to target",
        "failed to connect",
        "script execution aborted",
        "error while programming",
    )

    def __init__(
        self,
        rtt_addr: int,
        probe_serial: str | None,
        *,
        executable: Path | None = None,
    ) -> None:
        self.rtt_addr = int(rtt_addr)
        self.probe_serial = probe_serial
        self.executable = executable or JLINK_EXE
        self._lock = threading.Lock()
        self._close_lock = threading.Lock()
        self._cancel = threading.Event()
        self._operation_done = threading.Event()
        self._operation_done.set()
        self._closed = False
        self._temporary = tempfile.TemporaryDirectory(prefix="sensus-rtt-")
        self._sequence = 0
        self.up_descriptor = 0
        self.down_descriptor = 0
        self.up_buffer = 0
        self.down_buffer = 0
        self.up_size = 0
        self.down_size = 0
        try:
            self._load_layout()
        except BaseException:
            self._temporary.cleanup()
            self._closed = True
            raise

    def cancel(self) -> None:
        self._cancel.set()

    def close(self) -> None:
        self.cancel()
        if not self._operation_done.wait(5.0):
            return
        with self._close_lock:
            if self._closed:
                return
            self._temporary.cleanup()
            self._closed = True

    def _run(
        self,
        *commands: str,
        timeout_s: float = 20.0,
        attempts: int = 3,
    ) -> str:
        outputs: list[str] = []
        self._operation_done.clear()
        try:
            if self._cancel.is_set():
                raise JLinkOperationCancelled(
                    "J-Link Commander operation cancelled"
                )
            for attempt in range(max(1, attempts)):
                done = run_jlink_script(
                    jlink_connection_script(*commands, "q"),
                    self.probe_serial,
                    executable=self.executable,
                    timeout_s=timeout_s,
                    cancel_event=self._cancel,
                )
                blob = f"{done.stdout}\n{done.stderr}"
                outputs.append(blob)
                lowered = blob.lower()
                succeeded = (
                    done.returncode == 0
                    and "Script processing completed." in blob
                    and not any(marker in lowered for marker in self._TRANSIENT_MARKERS)
                )
                if succeeded:
                    return blob
                if attempt + 1 < attempts:
                    if self._cancel.wait(0.08 * (attempt + 1)):
                        raise JLinkOperationCancelled(
                            "J-Link Commander operation cancelled"
                        )
        finally:
            self._operation_done.set()
        tail = [
            line.strip()
            for line in "\n".join(outputs).splitlines()
            if line.strip()
        ][-8:]
        raise RuntimeError("JLinkExe RTT 内存操作失败:" + " | ".join(tail))

    @staticmethod
    def _validate_buffer(name: str, address: int, size: int) -> None:
        if (
            size <= 1
            or size > RTT_MAX_BUFFER_SIZE
            or address < RTT_RAM_START
            or address + size > RTT_RAM_END
        ):
            raise RuntimeError(
                f"RTT {name} 缓冲区越界:addr=0x{address:08X}, size={size}"
            )

    def _load_layout(self) -> None:
        if self.rtt_addr < RTT_RAM_START or self.rtt_addr + 24 > RTT_RAM_END:
            raise RuntimeError(f"RTT 控制块地址越界:0x{self.rtt_addr:08X}")
        header_blob = self._run(f"mem32 0x{self.rtt_addr:08X} 6")
        header = _parse_jlink_mem32(header_blob, self.rtt_addr, 6)
        magic = b"".join(word.to_bytes(4, "little") for word in header[:4])
        if magic != b"SEGGER RTT\x00\x00\x00\x00\x00\x00":
            raise RTTControlBlockUnavailable(
                f"0x{self.rtt_addr:08X} 不是有效的 SEGGER RTT 控制块"
            )
        up_count, down_count = header[4:6]
        if not 1 <= up_count <= RTT_MAX_CHANNELS:
            raise RuntimeError(f"RTT 上行通道数量异常:{up_count}")
        if not 1 <= down_count <= RTT_MAX_CHANNELS:
            raise RuntimeError(f"RTT 下行通道数量异常:{down_count}")

        self.up_descriptor = self.rtt_addr + 24
        self.down_descriptor = self.up_descriptor + up_count * 24
        if self.down_descriptor + down_count * 24 > RTT_RAM_END:
            raise RuntimeError("RTT 通道描述符越过 nRF52833 RAM")
        descriptors = self._run(
            f"mem32 0x{self.up_descriptor:08X} 6",
            f"mem32 0x{self.down_descriptor:08X} 6",
        )
        up = _parse_jlink_mem32(descriptors, self.up_descriptor, 6)
        down = _parse_jlink_mem32(descriptors, self.down_descriptor, 6)
        self.up_buffer, self.up_size = up[1], up[2]
        self.down_buffer, self.down_size = down[1], down[2]
        self._validate_buffer("上行", self.up_buffer, self.up_size)
        self._validate_buffer("下行", self.down_buffer, self.down_size)
        self._validate_descriptor("上行", up, self.up_buffer, self.up_size)
        self._validate_descriptor("下行", down, self.down_buffer, self.down_size)

    @staticmethod
    def _validate_descriptor(
        name: str,
        descriptor: list[int],
        expected_buffer: int,
        expected_size: int,
    ) -> None:
        if descriptor[1] != expected_buffer or descriptor[2] != expected_size:
            raise RuntimeError(f"RTT {name}缓冲区在读取期间发生变化")
        if descriptor[3] >= expected_size or descriptor[4] >= expected_size:
            raise RuntimeError(
                f"RTT {name}指针越界:wr={descriptor[3]}, rd={descriptor[4]}, "
                f"size={expected_size}"
            )

    def _descriptor(self, *, up: bool) -> list[int]:
        address = self.up_descriptor if up else self.down_descriptor
        expected_buffer = self.up_buffer if up else self.down_buffer
        expected_size = self.up_size if up else self.down_size
        blob = self._run(f"mem32 0x{address:08X} 6")
        descriptor = _parse_jlink_mem32(blob, address, 6)
        self._validate_descriptor(
            "上行" if up else "下行",
            descriptor,
            expected_buffer,
            expected_size,
        )
        return descriptor

    def discard_pending_up(self) -> None:
        """Atomically discard bytes predating this collector connection."""
        with self._lock:
            descriptor = self._descriptor(up=True)
            write_offset = descriptor[3]
            read_pointer = self.up_descriptor + 16
            blob = self._run(
                f"w4 0x{read_pointer:08X}, 0x{write_offset:08X}",
                f"mem32 0x{read_pointer:08X} 1",
            )
            confirmed = _parse_jlink_mem32(blob, read_pointer, 1)[0]
            if confirmed != write_offset:
                raise RuntimeError("JLinkExe 未能确认清空 RTT 历史上行数据")

    def _next_file(self, label: str) -> Path:
        self._sequence += 1
        return Path(self._temporary.name) / f"{self._sequence:08d}-{label}.bin"

    def recv(self, _max_bytes: int = 65536) -> bytes:
        with self._lock:
            descriptor = self._descriptor(up=True)
            write_offset, read_offset = descriptor[3], descriptor[4]
            available = (write_offset - read_offset) % self.up_size
            if available == 0:
                return b""

            first_length = min(available, self.up_size - read_offset)
            second_length = available - first_length
            first_path = self._next_file("up-a")
            second_path = self._next_file("up-b") if second_length else None
            commands = [
                f"savebin {_jlink_file_argument(first_path)}, "
                f"0x{self.up_buffer + read_offset:08X}, 0x{first_length:X}"
            ]
            if second_path is not None:
                commands.append(
                    f"savebin {_jlink_file_argument(second_path)}, "
                    f"0x{self.up_buffer:08X}, 0x{second_length:X}"
                )
            read_pointer = self.up_descriptor + 16
            commands.extend((
                f"w4 0x{read_pointer:08X}, 0x{write_offset:08X}",
                f"mem32 0x{read_pointer:08X} 1",
            ))
            try:
                blob = self._run(*commands, timeout_s=30)
                confirmed = _parse_jlink_mem32(blob, read_pointer, 1)[0]
                if confirmed != write_offset:
                    raise RuntimeError("JLinkExe 未能确认 RTT 上行读指针")
                first = first_path.read_bytes()
                second = second_path.read_bytes() if second_path is not None else b""
                if len(first) != first_length or len(second) != second_length:
                    raise RuntimeError("JLinkExe 保存的 RTT 上行长度不完整")
                return first + second
            finally:
                first_path.unlink(missing_ok=True)
                if second_path is not None:
                    second_path.unlink(missing_ok=True)

    def sendall(self, payload: bytes, timeout_s: float = 8.0) -> None:
        if not payload:
            return
        deadline = time.monotonic() + timeout_s
        sent = 0
        with self._lock:
            while sent < len(payload):
                descriptor = self._descriptor(up=False)
                write_offset, read_offset = descriptor[3], descriptor[4]
                free = self.down_size - 1 - (
                    (write_offset - read_offset) % self.down_size
                )
                if free <= 0:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("RTT 下行缓冲区持续占满，固件未消费命令")
                    time.sleep(0.04)
                    continue
                chunk = payload[sent:sent + min(free, 64)]
                commands: list[str] = []
                for index, value in enumerate(chunk):
                    address = self.down_buffer + (
                        (write_offset + index) % self.down_size
                    )
                    commands.append(f"w1 0x{address:08X}, 0x{value:02X}")

                first_length = min(len(chunk), self.down_size - write_offset)
                second_length = len(chunk) - first_length
                first_path = self._next_file("down-a")
                second_path = self._next_file("down-b") if second_length else None
                commands.append(
                    f"savebin {_jlink_file_argument(first_path)}, "
                    f"0x{self.down_buffer + write_offset:08X}, 0x{first_length:X}"
                )
                if second_path is not None:
                    commands.append(
                        f"savebin {_jlink_file_argument(second_path)}, "
                        f"0x{self.down_buffer:08X}, 0x{second_length:X}"
                    )
                next_offset = (write_offset + len(chunk)) % self.down_size
                write_pointer = self.down_descriptor + 12
                commands.extend((
                    f"w4 0x{write_pointer:08X}, 0x{next_offset:08X}",
                    f"mem32 0x{write_pointer:08X} 1",
                ))
                try:
                    blob = self._run(*commands, timeout_s=30)
                    echoed = first_path.read_bytes()
                    if second_path is not None:
                        echoed += second_path.read_bytes()
                    if echoed != chunk:
                        raise RuntimeError("JLinkExe RTT 下行命令回读不一致")
                    confirmed = _parse_jlink_mem32(
                        blob, write_pointer, 1
                    )[0]
                    if confirmed != next_offset:
                        raise RuntimeError("JLinkExe 未能确认 RTT 下行写指针")
                finally:
                    first_path.unlink(missing_ok=True)
                    if second_path is not None:
                        second_path.unlink(missing_ok=True)
                sent += len(chunk)


class JLinkMemoryRTTBridge:
    """Expose :class:`JLinkMemoryRTT` as one local bidirectional socket."""

    stdin = None

    def __init__(
        self,
        transport: JLinkMemoryRTT,
        port: int,
        control_port: int | None = None,
    ) -> None:
        self.transport = transport
        self.port = int(port)
        self._stop = threading.Event()
        self._done = threading.Event()
        self._error: BaseException | None = None
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", self.port))
        self._listener.listen(1)
        self._listener.settimeout(0.2)
        self._control_listener: socket.socket | None = None
        self._control_thread: threading.Thread | None = None
        if control_port is not None:
            control_listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            control_listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                control_listener.bind(("127.0.0.1", int(control_port)))
                control_listener.listen(1)
                control_listener.settimeout(0.2)
                self._control_listener = control_listener
                self._control_thread = threading.Thread(
                    target=self._serve_control,
                    name=f"jlink-memory-rtt-control-{control_port}",
                    daemon=True,
                )
                self._control_thread.start()
            except OSError:
                control_listener.close()
        self._thread = threading.Thread(
            target=self._serve,
            name=f"jlink-memory-rtt-{self.port}",
            daemon=True,
        )
        self._thread.start()

    def _serve_control(self) -> None:
        listener = self._control_listener
        if listener is None:
            return
        while not self._stop.is_set():
            try:
                connection, _ = listener.accept()
            except (OSError, socket.timeout):
                continue
            try:
                connection.settimeout(1)
                payload = connection.recv(len(BRIDGE_SHUTDOWN_COMMAND) + 1)
                if payload == BRIDGE_SHUTDOWN_COMMAND:
                    self.terminate()
                    return
            except OSError:
                pass
            finally:
                connection.close()

    def _serve(self) -> None:
        client: socket.socket | None = None
        try:
            while not self._stop.is_set() and client is None:
                try:
                    client, _ = self._listener.accept()
                except socket.timeout:
                    continue
            if client is None:
                return
            client.settimeout(0.02)
            while not self._stop.is_set():
                try:
                    incoming = client.recv(4096)
                    if not incoming:
                        break
                    self.transport.sendall(incoming)
                except socket.timeout:
                    pass
                outgoing = self.transport.recv()
                if outgoing:
                    client.sendall(outgoing)
                else:
                    self._stop.wait(RTT_BRIDGE_POLL_INTERVAL_S)
        except BaseException as exc:
            if not (self._stop.is_set() and isinstance(
                exc, JLinkOperationCancelled
            )):
                self._error = exc
                print(f"[collect] J-Link RTT 内存桥失败:{exc}", file=sys.stderr)
        finally:
            if client is not None:
                try:
                    client.close()
                except OSError:
                    pass
            try:
                self._listener.close()
            except OSError:
                pass
            if self._control_listener is not None:
                try:
                    self._control_listener.close()
                except OSError:
                    pass
            self.transport.close()
            self._done.set()

    def poll(self) -> int | None:
        return 1 if self._error is not None else (0 if self._done.is_set() else None)

    def terminate(self) -> None:
        self._stop.set()
        self.transport.cancel()
        try:
            self._listener.close()
        except OSError:
            pass
        if self._control_listener is not None:
            try:
                self._control_listener.close()
            except OSError:
                pass

    def wait(self, timeout: float | None = None) -> int:
        if not self._done.wait(timeout):
            raise subprocess.TimeoutExpired("JLinkMemoryRTTBridge", timeout)
        return 1 if self._error is not None else 0

    def kill(self) -> None:
        self.terminate()


class _AcquisitionDuration:
    """Measure a collection timeout from the confirmed acquisition start."""

    def __init__(self, duration_s: float | None) -> None:
        self.duration_s = duration_s
        self.started_at_s: float | None = None

    def mark_started(self, now_s: float) -> None:
        if self.started_at_s is None:
            self.started_at_s = now_s

    def expired(self, now_s: float) -> bool:
        return bool(
            self.duration_s
            and self.started_at_s is not None
            and now_s - self.started_at_s > self.duration_s
        )


def _split_complete_lines(text: str, pending: str = "") -> tuple[list[str], str]:
    """Split an append-only command stream without dropping a partial line."""
    combined = pending + text
    if not combined:
        return [], ""
    chunks = combined.splitlines(keepends=True)
    if chunks and not chunks[-1].endswith(("\n", "\r")):
        pending = chunks.pop()
    else:
        pending = ""
    return [chunk.rstrip("\r\n") for chunk in chunks], pending


def _encode_firmware_command(command: str) -> bytes:
    """Encode one firmware command, rejecting characters RTT cannot carry."""
    normalized = command.rstrip("\r\n")
    try:
        return (normalized + "\n").encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError("firmware commands only support ASCII characters") from exc


def _firmware_command_arg(value: str) -> str:
    """Argparse adapter that reports non-ASCII trigger commands cleanly."""
    try:
        _encode_firmware_command(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc
    return value


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
        metadata_candidates = [
            elf.parent / "firmware.json",
            elf.parents[3] / "prebuilt/firmware.json" if len(elf.parents) > 3 else elf,
        ]
        for metadata in metadata_candidates:
            try:
                payload = json.loads(metadata.read_text(encoding="utf-8"))
                value = payload["rtt_address"]
                return int(value, 0) if isinstance(value, str) else int(value)
            except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
                continue
        sys.exit(
            f"找不到 nm: {ZEPHYR_SDK_NM}，也无法从固件元数据读取 RTT 地址\n"
            "→ 安装固件工具链，或用 --rtt-address 手动给"
        )

    out = subprocess.run(
        [str(ZEPHYR_SDK_NM), str(elf)], capture_output=True, text=True,
        encoding="utf-8", errors="replace", check=False
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
def _openocd_rtt_available() -> bool:
    return (
        OPENOCD_EXE.is_file()
        and (OPENOCD_SCRIPTS / "interface/jlink.cfg").is_file()
        and (OPENOCD_SCRIPTS / "target/nrf52.cfg").is_file()
    )


def _stop_rtt_process(process: Any) -> None:
    """Release a failed RTT backend before another driver opens the probe."""
    try:
        stream = getattr(process, "stdin", None)
        if stream:
            stream.close()
    except OSError:
        pass
    try:
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _wait_for_rtt_server(
    process: Any,
    port: int,
    timeout_s: float = OPENOCD_RTT_START_TIMEOUT_S,
) -> tuple[bool, str]:
    """Wait until OpenOCD actually accepts RTT clients, not merely until spawn."""
    deadline = time.monotonic() + timeout_s
    last_error = "RTT server 尚未监听"
    while time.monotonic() < deadline:
        returncode = process.poll()
        if returncode is not None:
            return False, f"OpenOCD 启动期退出（代码 {returncode}）"
        probe: socket.socket | None = None
        try:
            probe = socket.create_connection(("127.0.0.1", port), timeout=0.2)
            # START is sent only by the real collector connection, so this
            # readiness handshake cannot consume measurement samples.
            return True, ""
        except OSError as exc:
            last_error = str(exc)
        finally:
            if probe is not None:
                probe.close()
        time.sleep(OPENOCD_RTT_START_POLL_INTERVAL_S)
    return False, f"{timeout_s:g} 秒内未建立 RTT server：{last_error}"


def _start_openocd_rtt(
    rtt_addr: int,
    probe_serial: str | None,
    port: int,
    reset_before_read: bool,
) -> subprocess.Popen:
    adapter_serial = f"adapter serial {probe_serial}; " if probe_serial else ""
    reset_cmds = "reset halt; reset run; sleep 500; " if reset_before_read else ""
    server_commands = (
        f"{adapter_serial}adapter speed {SPEED_KHZ}; init; poll off; {reset_cmds}"
        f"rtt setup 0x{rtt_addr:08X} 0x100 \"SEGGER RTT\"; "
        f"rtt start; rtt server start {port} 0"
    )
    cmd = [
        str(OPENOCD_EXE), "-s", str(OPENOCD_SCRIPTS),
        "-f", "interface/jlink.cfg", "-c", "transport select swd",
        "-f", "target/nrf52.cfg", "-c", server_commands,
    ]
    return subprocess.Popen(
        cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT, text=True,
        encoding="utf-8", errors="replace",
    )


def _start_commander_memory_rtt(
    rtt_addr: int,
    probe_serial: str | None,
    port: int,
    reset_before_read: bool,
) -> Any:
    transport = JLinkMemoryRTT(
        rtt_addr,
        probe_serial,
        executable=JLINK_EXE,
    )
    # Only bytes predating this collector are dropped; AFE polarization stays.
    if reset_before_read:
        transport.discard_pending_up()
    return JLinkMemoryRTTBridge(
        transport, port, control_port=19022 if port == 19021 else None,
    )


def start_jlink_rtt(rtt_addr: int, probe_serial: str | None,
                    port: int, reset_before_read: bool = False) -> Any:
    """起 RTT 桥，数据出到 telnet ``port``。

    优先启动长连接的 libjaylink OpenOCD RTT server，避免每读一批数据都
    重启 Commander 导致 0.4–0.5 秒的批处理卡顿。OpenOCD 启动或建链失败时，
    先完整释放探头，再自动回退到带回读校验的 Commander 内存桥。
    """
    openocd_failure = "未找到完整的 OpenOCD 及 scripts"
    if _openocd_rtt_available():
        print(
            f"[collect] 启动常驻 libjaylink OpenOCD,RTT 控制块 @ "
            f"0x{rtt_addr:08X},telnet {port}",
            file=sys.stderr,
        )
        process = _start_openocd_rtt(
            rtt_addr, probe_serial, port, reset_before_read,
        )
        ready, openocd_failure = _wait_for_rtt_server(process, port)
        if ready:
            print("[collect] OpenOCD RTT server 已就绪", file=sys.stderr)
            return process
        _stop_rtt_process(process)
        print(
            f"[collect] OpenOCD RTT 不可用（{openocd_failure}），"
            "改用 Commander 内存桥",
            file=sys.stderr,
        )

    commander_failure = f"找不到 {JLINK_EXE}"
    if JLINK_EXE.is_file():
        jlink_ready, probe_output = probe_jlink_target(
            probe_serial, executable=JLINK_EXE, timeout_s=10,
        )
        if jlink_ready:
            print(
                f"[collect] 启动 J-Link RTT 内存桥,"
                f"控制块 @ 0x{rtt_addr:08X},本地端口 {port}",
                file=sys.stderr,
            )
            return _start_commander_memory_rtt(
                rtt_addr, probe_serial, port, reset_before_read,
            )
        tail = [line.strip() for line in probe_output.splitlines()
                if line.strip()][-3:]
        commander_failure = " | ".join(tail) or "Commander 无法连接目标"

    sys.exit(
        "J-Link RTT 后端均不可用\n"
        f"→ OpenOCD: {openocd_failure}\n"
        f"→ Commander: {commander_failure}\n"
        "请检查板卡供电、SWD 排线和探头选择。"
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
    # ARMED 只建立 RTT/命令通道，不立刻启动测量。Debug 首次进入时用它先 GET
    # 设备真值，再经 cmd_file 发 SET → START。它不能等同于 trigger=None：后者
    # 会让 collector 把连接前缓冲里的旧 IT_START/S 行误当成本轮数据。
    armed_only = trigger == "ARMED"
    trigger_command = None if armed_only else (
        "START" if trigger == "FRESH_START" else trigger
    )
    trigger_bytes = (
        _encode_firmware_command(trigger_command) if trigger_command else None
    )
    trigger_pending = trigger_bytes is not None
    trigger_resends_enabled = trigger_pending
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
    cmd_pending = ""
    armed_start_sent = False
    with sock:
        while True:
            # 与重发同理:必须在循环顶部按**挂钟时间**判。数据以 8 样本/秒连续流入时
            # recv 永不超时,挂在超时分支上的轮询一次都不会执行。
            if cmd_file is not None and \
                    time.monotonic() - last_cmd_poll >= CMD_POLL_INTERVAL_S:
                last_cmd_poll = time.monotonic()
                try:
                    if cmd_file.exists():
                        with cmd_file.open("r", encoding="utf-8", errors="replace") as fh:
                            fh.seek(cmd_pos)
                            fresh = fh.read()
                            cmd_pos = fh.tell()
                        command_lines, cmd_pending = _split_complete_lines(
                            fresh, cmd_pending
                        )
                        for raw_cmd in command_lines:
                            raw_cmd = raw_cmd.strip()
                            if not raw_cmd or raw_cmd.startswith("#"):
                                continue
                            try:
                                command_bytes = _encode_firmware_command(raw_cmd)
                            except ValueError as exc:
                                print(
                                    f"[collect] ⚠️ 拒绝命令文件中的非 ASCII 命令:"
                                    f"{raw_cmd!r} ({exc})",
                                    file=sys.stderr,
                                )
                                continue
                            sock.sendall(command_bytes)
                            if armed_only and raw_cmd == "START":
                                armed_start_sent = True
                                trigger_command = "START"
                                # ARMED 只会在带标识的 GET/CFG 回读已经证明
                                # 下行可用后写 START。此时重发会把命令堆在 RTT
                                # 下行队列里；固件稍后依次收到时会把每一条
                                # 解释为“重新开始”，导致采集每隔约 1 s 中止。
                                # 仍保留 trigger_pending 以等待唯一启动回执，
                                # 但这条已核验的通道上不再重发。
                                trigger_bytes = b"START\n"
                                trigger_pending = True
                                trigger_resends_enabled = False
                                resends = 0
                                warned_unacked = False
                                last_trigger_at = time.monotonic()
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
            if (trigger_pending and trigger_resends_enabled
                    and time.monotonic() - last_trigger_at >= TRIGGER_RESEND_INTERVAL_S):
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
                    physical_line = raw.decode("utf-8", "replace")
                    logical_lines = _split_glued_lifecycle_line(physical_line)
                    if len(logical_lines) > 1:
                        print("[collect] ⚠️ RTT 生命周期标记与上一行粘连，"
                              "已恢复行边界", file=sys.stderr)
                    for line in logical_lines:
                        start_seen = (
                            parse_it_start(line) is not None
                            or parse_cv_start(line) is not None
                        )
                        # ARMED 阶段允许 GET/CFG/CELL_V 上行通过，但忽略
                        # 缓冲里早于本次 START 的旧启动标记。
                        if armed_only and start_seen and not armed_start_sent:
                            print("[collect] 忽略 ARMED 前的旧 START 标记",
                                  file=sys.stderr)
                            continue
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

    CDC ACM ignores the nominal baud rate, but pyserial requires one. Opening
    the port asserts DTR; the V5.1 app waits for that event before emitting
    startup identity lines. The other CDC interface is SMP and must not be
    passed here.
    """
    if serial_factory is None:
        try:
            import serial
        except ImportError as exc:
            raise SystemExit(
                "USB 串口采集需要 pyserial>=3.5;请重新执行 pip install -e ."
            ) from exc
        serial_factory = serial.Serial

    armed_only = trigger == "ARMED"
    trigger_command = None if armed_only else (
        "START" if trigger == "FRESH_START" else trigger
    )
    try:
        trigger_bytes = (
            _encode_firmware_command(trigger_command)
            if trigger_command else None
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    trigger_pending = trigger_bytes is not None
    trigger_resends_enabled = trigger_pending
    resends = 0
    warned_unacked = False
    last_trigger_at = 0.0
    last_cmd_poll = 0.0
    cmd_pos = 0
    cmd_pending = ""
    armed_start_sent = False
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
        # A persistent app may hand us the tail of an already-started line.
        # Discard exactly that first physical line before sending commands.
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
            print("[collect] ⚠️ DATA CDC 2s 内无可对齐行", file=sys.stderr)

        # GET/STATUS are read-only and make every USB run self-describing.
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
                        command_lines, cmd_pending = _split_complete_lines(
                            fresh, cmd_pending
                        )
                        for raw_cmd in command_lines:
                            raw_cmd = raw_cmd.strip()
                            if not raw_cmd or raw_cmd.startswith("#"):
                                continue
                            command_bytes = _encode_firmware_command(raw_cmd)
                            stream.write(command_bytes)
                            stream.flush()
                            if armed_only and raw_cmd == "START":
                                armed_start_sent = True
                                trigger_command = "START"
                                trigger_bytes = b"START\n"
                                trigger_pending = True
                                # 配置闸门已经通过同一 DATA CDC 往返核验。
                                # 重发 START 只会在固件输出拥塞时造成重复启动。
                                trigger_resends_enabled = False
                                resends = 0
                                warned_unacked = False
                                last_trigger_at = time.monotonic()
                            print(f"[collect] 已经 DATA CDC 转发命令:{raw_cmd}",
                                  file=sys.stderr)
                except (OSError, ValueError) as exc:
                    print(f"[collect] ⚠️ DATA CDC 命令失败:{exc}", file=sys.stderr)

            if (trigger_pending and trigger_resends_enabled
                    and now - last_trigger_at >= SERIAL_TRIGGER_RESEND_INTERVAL_S):
                if resends < TRIGGER_MAX_RESENDS:
                    stream.write(trigger_bytes)
                    stream.flush()
                    resends += 1
                    last_trigger_at = now
                elif not warned_unacked:
                    warned_unacked = True
                    print(f"[collect] 🔴 DATA CDC 重发 {resends} 次仍未收到启动标记",
                          file=sys.stderr)

            try:
                chunk = stream.read(4096)
            except Exception as exc:
                raise SystemExit(f"V5.1 DATA CDC 读取失败:{exc}") from exc
            if chunk:
                last_data = time.monotonic()
                buf += chunk
                while b"\n" in buf:
                    raw, buf = buf.split(b"\n", 1)
                    physical_line = raw.decode("utf-8", "replace")
                    logical_lines = _split_glued_lifecycle_line(physical_line)
                    if len(logical_lines) > 1:
                        print("[collect] ⚠️ DATA CDC 生命周期标记与上一行粘连，"
                              "已恢复行边界", file=sys.stderr)
                    for line in logical_lines:
                        start_seen = (
                            parse_it_start(line) is not None
                            or parse_cv_start(line) is not None
                        )
                        if armed_only and start_seen and not armed_start_sent:
                            print("[collect] 忽略 ARMED 前的旧 START 标记",
                                  file=sys.stderr)
                            continue
                        if trigger_pending and start_seen:
                            trigger_pending = False
                            print(f"[collect] 固件已确认 {trigger_command}"
                                  f"(DATA CDC 重发 {resends} 次)", file=sys.stderr)
                        yield line
            elif idle_timeout is not None and time.monotonic() - last_data > idle_timeout:
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
    "MEAS_BOOT", "MEAS_CONFIRMED", "MEAS_REJECT",
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


# `cfg_events.csv` 的列 = 每个已确认快照一行宽表。同一 epoch 的 GET 回读
# 会用 req 区分，既可按 epoch join 电流 CSV，也不会被开机快照吞掉。
# 只收 CFG_APPLIED + CFG_DERIVED 的字段,其余事件只进 audit.jsonl。
CFG_EVENT_COLUMNS = (
    "host_unix_s", "ep", "src", "nlines", "forced", "perturbs_cell", "nregs",
    "skipped",
    "fsr", "off", "conv", "conv_src", "period", "sysper", "clk40", "ioc",
    "e_mv", "vwe_mv", "idle", "cellv", "chop", "rs", "ios", "satpct",
    "sel", "amps",
    "fsr_pa", "off_pa", "bits", "conv_ms", "period_ms", "idle_ppm",
    "lsb_frame_fa", "lsb_eff_fa", "rej50_db_x10", "rej50_worst_db_x10",
    "conv_alt", "red_max_pa", "ox_max_pa", "sat_margin", "sat_margin_pa",
    "sysbudget_ms", "sysper_ms", "daca", "dacb",
    "idle_warn", "headroom_warn", "sig_warn",
    "status1", "invalid_cfg", "vdd_oor", "verify_ok", "req", "confirmed",
)


class CfgEventAccumulator:
    """把同一 (epoch, req) 的 APPLIED/DERIVED/CONFIRMED 合成宽表。

    ⚠️ 只在 `CFG_CONFIRMED` 时才落盘 —— 未确认的 epoch 不该
    出现在宽表里,否则分析脚本会把一个被回滚掉的配置当成生效过的配置。
    未确认的行仍在 audit.jsonl 里,不丢信息。
    """

    def __init__(self) -> None:
        self.pending: dict[tuple[int, str | None], dict[str, object]] = {}
        self.rows: list[list[str]] = []
        self._done: set[tuple[int, str | None]] = set()
        self._faulted: set[tuple[int, str | None]] = set()

    @staticmethod
    def _key(event: dict[str, object]) -> tuple[int, str | None] | None:
        ep = event.get("ep")
        if not isinstance(ep, int):
            return None
        request_id = event.get("req")
        request_key = (
            str(request_id) if request_id not in (None, "", "-") else None
        )
        return ep, request_key

    def feed(self, event: dict[str, object]) -> list[str] | None:
        key = self._key(event)
        if key is None:
            return None
        ep, _request_id = key
        kind = event["kind"]
        if kind in ("CFG_APPLIED", "CFG_DERIVED"):
            row = self.pending.setdefault(key, {"ep": ep, "confirmed": 0})
            row.update({k: v for k, v in event.items()
                        if k not in ("kind", "raw")})
            return None
        if kind == "CFG_CONFIRMED":
            row = self.pending.pop(key, {"ep": ep})
            row.update({k: v for k, v in event.items()
                        if k not in ("kind", "raw")})
            if key in self._faulted or row.get("verify_ok") not in (None, 1):
                self._done.add(key)
                return None
            row["confirmed"] = 1
            # 🔴 两道去重,否则宽表会产生空壳或重复快照:
            #   ① 没有 CFG_DERIVED(没有 bits)的行是**空壳**——开机时
            #      CFG_BOOT+CFG_CONFIRMED 会先到,派生量还没来,落进去就是一行全空
            #   ② 同一 (epoch, req) 只留第一条完整快照
            if row.get("bits") in (None, ""):
                return None
            if key in self._done:
                return None
            self._done.add(key)
            out = [str(row.get(c, "")) for c in CFG_EVENT_COLUMNS]
            self.rows.append(out)
            return out
        if kind in ("CFG_ROLLBACK", "CFG_FAULT"):
            self.pending.pop(key, None)
            self._faulted.add(key)
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


_LIFECYCLE_TOKENS = (
    "IT_START ", "IT_DONE ", "IT_ABORTED ",
    "CV_START ", "CV_DONE ", "CV_ABORTED ",
)


def _is_lifecycle_line(line: str) -> bool:
    return any((
        parse_it_start(line) is not None,
        parse_it_done(line) is not None,
        parse_it_aborted(line) is not None,
        parse_cv_start(line) is not None,
        parse_cv_done(line) is not None,
        parse_cv_aborted(line) is not None,
    ))


def _split_glued_lifecycle_line(line: str) -> list[str]:
    """Recover a machine marker whose preceding newline was lost in transit.

    Lifecycle parsers remain strictly anchored: recovery is accepted only when
    the complete suffix is itself a valid marker. A diagnostic sentence that
    merely mentions ``IT_START`` therefore cannot open the acquisition gate.
    """

    cleaned = ANSI_RE.sub("", line)
    if _is_lifecycle_line(cleaned):
        return [cleaned.strip()]

    positions = sorted(
        {
            index
            for token in _LIFECYCLE_TOKENS
            for index in [cleaned.rfind(token)]
            if index > 0
        },
        reverse=True,
    )
    for index in positions:
        suffix = cleaned[index:].strip()
        if not _is_lifecycle_line(suffix):
            continue
        prefix = cleaned[:index].rstrip()
        if not prefix:
            return [suffix]
        return [*_split_glued_lifecycle_line(prefix), suffix]
    return [line]


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
                         "同时在旁边写 <...>-audit-cfg.csv:每个**已确认**"
                         "的配置快照一行,便于按 epoch 分段解释电流 CSV")

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
    ap.add_argument("--trigger", default=None, type=_firmware_command_arg,
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
    acquisition_duration = _AcquisitionDuration(args.duration)
    if acquisition_started:
        acquisition_duration.mark_started(time.monotonic())
    stop = False
    new_file = not args.out.exists() or args.out.stat().st_size == 0

    def _on_sigint(_sig, _frm):
        nonlocal stop
        stop = True
        print("\n[collect] 收到 Ctrl-C,收尾…", file=sys.stderr)

    signal.signal(signal.SIGINT, _on_sigint)
    # GUI 停止测量走 killpg(SIGTERM),Windows 走 taskkill(SIGBREAK)。
    # 默认动作会立刻终止本进程,下面的 finally 不执行 —— 探头仍会释放
    # (进程一死,stdin 管道 EOF 就把 JLinkExe 带走,2026-08-09 已实测),
    # 但 CSV 收尾与统计行不会写完。接住它是为了让用户点「停止」时数据
    # 文件是完整收尾的。
    if _IS_WIN:
        try:
            signal.signal(signal.SIGBREAK, _on_sigint)  # type: ignore[attr-defined]
        except (ValueError, AttributeError):
            pass
    else:
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
    #   cfg_events.csv 每个已确认快照一行 —— 给分析脚本按 epoch join 电流 CSV
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
                if acquisition_duration.expired(time.monotonic()):
                    print(f"[collect] 到达 --duration {args.duration}s,停止",
                          file=sys.stderr)
                    break

                if raw_out is not None:
                    raw_out.write(line.rstrip("\r\n") + "\n")

                # RTT 里混着 JLinkExe 横幅、Zephyr LOG(带 ANSI 色码)与数据行
                clean_line = ANSI_RE.sub("", line)
                started = parse_it_start(clean_line)
                if started is not None:
                    if not acquisition_started:
                        acquisition_duration.mark_started(time.monotonic())
                    acquisition_started = True
                    run_number, target_mv = started
                    print(f"[collect] 固件开始第 {run_number} 轮 IT:E={target_mv}mV",
                          file=sys.stderr)
                    continue
                cv_started = parse_cv_start(clean_line)
                if cv_started is not None:
                    if not acquisition_started:
                        acquisition_duration.mark_started(time.monotonic())
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
                  f"({len(audit_acc.rows)} 个已确认快照 → {cfg_csv_path})",
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
        if sys.platform == "darwin":
            print("   1) ioreg -p IOUSB -l -w 0 | grep J_Link   ← 探头还在 USB 上?",
                  file=sys.stderr)
        elif sys.platform == "win32":
            print("   1) 检查设备管理器 → 通用串行总线设备 → J-Link   ← 探头还在 USB 上?",
                  file=sys.stderr)
        else:
            print("   1) lsusb | grep J-Link   ← 探头还在 USB 上?",
                  file=sys.stderr)
        print("   2) 系统 JLinkExe 能否用显式 connect 命令连接目标", file=sys.stderr)
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
