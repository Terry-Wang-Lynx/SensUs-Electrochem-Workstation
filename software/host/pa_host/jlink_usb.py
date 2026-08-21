"""Driver-independent discovery of SEGGER J-Link USB devices."""

from __future__ import annotations

import json
import logging
import plistlib
import re
import subprocess
import sys
from dataclasses import dataclass
from typing import Any, Callable, Iterable


JLINK_VENDOR_ID = 0x1366
_INSTANCE_ID_RE = re.compile(
    r"^USB\\VID_([0-9A-F]{4})&PID_([0-9A-F]{4})"
    r"(?:&MI_([0-9A-F]{2}))?\\",
    flags=re.IGNORECASE,
)

# 🔴 探针枚举子进程的超时。原值 25s 与设备发现的轮询周期同量级,
# 等于超时窗口永远追不上自己:一轮卡满 25s,probing 就一直是 true,
# data_port/smp_port 永远为空。实测子进程 ~1s 内就干完活
# (CPU 0.92s;脚本单独跑 1.0–1.4s),6s 已给 4~6× 余量,
# 足够覆盖 PowerShell 冷启动 + Defender 扫描,又远小于轮询周期。
# ⚠️ 真机上存在"活干完后干等 24s 被 kill"的未查明现象,这里只把
# 代价从 25s 压到 6s,不假装修好了那个机制。
_WINDOWS_DISCOVERY_TIMEOUT_S = 6.0
# ioreg 是本地内核注册表快照,历史值 8s 保持不变(同样远小于轮询周期)。
_MACOS_DISCOVERY_TIMEOUT_S = 8.0

_LOGGER = logging.getLogger(__name__)

# 诊断留痕用的可注入 sink。本模块在 gui_server 的下层(gui_server
# import 本模块),直接 import 那边的 DIAGNOSTICS 会形成循环 import;
# 因此留一个 hook,签名与 DiagnosticStore.record(level, event, message,
# **context) 兼容。未注入时退化到 stdlib logging —— 失败至少不是
# 完全静默的。
_DIAGNOSTICS_SINK: Callable[..., Any] | None = None


def set_diagnostics_sink(sink: Callable[..., Any] | None) -> None:
    """注入 ``DiagnosticStore.record`` 之类的记录器;传 ``None`` 恢复默认。"""
    global _DIAGNOSTICS_SINK
    _DIAGNOSTICS_SINK = sink


def _record_discovery_failure(platform: str, exc: BaseException) -> None:
    """记录一次 J-Link 枚举失败,并保证记录本身绝不抛异常。"""
    detail = f"{type(exc).__name__}: {exc}"
    sink = _DIAGNOSTICS_SINK
    if sink is not None:
        try:
            sink(
                "warning",
                "device.jlink.usb_discovery_failed",
                "J-Link USB 枚举失败,按未插入 J-Link 处理",
                platform=platform,
                error=detail,
            )
            return
        except Exception:  # 留痕失败绝不能反过来打断设备发现
            pass
    _LOGGER.warning(
        "J-Link USB discovery failed on %s, treating as no probe present: %s",
        platform, detail,
    )


@dataclass(frozen=True)
class JLinkUsbInfo:
    """PortInfo-compatible descriptor for a J-Link without a CDC port."""

    device: str
    description: str
    hwid: str
    manufacturer: str
    product: str
    serial_number: str
    vid: int
    pid: int
    location: str
    interface: str
    instance_id: str
    status: str
    problem_code: int


def _json_rows(output: str) -> list[dict[str, Any]]:
    raw = str(output or "").strip().lstrip("\ufeff")
    payload = json.loads(raw or "[]")
    # PowerShell 无匹配设备时可能输出字面 `null`(空管道赋值 → $null),
    # 这与"没插 J-Link"等价,不是异常。
    if payload is None:
        return []
    if isinstance(payload, dict):
        payload = [payload]
    if not isinstance(payload, list):
        raise RuntimeError("J-Link discovery response was not a list")
    return [item for item in payload if isinstance(item, dict)]


def _windows_payload() -> list[dict[str, Any]]:
    script = r"""
$ProgressPreference = 'SilentlyContinue'
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
function Read-DeviceProperty([string]$InstanceId, [string]$KeyName) {
  $property = Get-PnpDeviceProperty `
    -InstanceId $InstanceId -KeyName $KeyName -ErrorAction SilentlyContinue
  if ($null -ne $property) { return $property.Data }
  return $null
}
# 服务端过滤:-InstanceId 让 PnP provider 自己筛,不再把机器上全部设备
# (实测 255 个)灌进管道再用 Where-Object 逐个比。VID_1366 设备为 0 时
# 这一步几乎不产生工作量。
# 🔴 -ErrorAction SilentlyContinue 必须留:无匹配时 Get-PnpDevice 会报
# "No matching MSFT_PnPDevice objects found",而脚本顶部的
# $ErrorActionPreference='Stop' 会把它升级成终止错误 ——
# "没插 J-Link" 必须等价于空结果,不是失败。
$devices = @(
  Get-PnpDevice -PresentOnly -InstanceId 'USB\VID_1366*' `
    -ErrorAction SilentlyContinue
)
$rows = New-Object System.Collections.ArrayList
foreach ($device in $devices) {
  $instanceId = [string]$device.InstanceId
  # 二次校验前缀:-InstanceId 的通配语义由 provider 实现,这里保证与
  # 原 `-like 'USB\VID_1366*'` 完全一致(-like 同样不区分大小写)。
  if ($instanceId -notlike 'USB\VID_1366*') { continue }
  [void]$rows.Add([PSCustomObject]@{
    instance_id = $instanceId
    friendly_name = [string]$device.FriendlyName
    class_name = [string]$device.Class
    status = [string]$device.Status
    bus_description = [string](Read-DeviceProperty $instanceId 'DEVPKEY_Device_BusReportedDeviceDesc')
    serial_number = [string](Read-DeviceProperty $instanceId 'DEVPKEY_Device_SerialNumber')
    parent = [string](Read-DeviceProperty $instanceId 'DEVPKEY_Device_Parent')
    container_id = [string](Read-DeviceProperty $instanceId 'DEVPKEY_Device_ContainerId')
    location = [string](Read-DeviceProperty $instanceId 'DEVPKEY_Device_LocationInfo')
    problem_code = [int](Read-DeviceProperty $instanceId 'DEVPKEY_Device_ProblemCode')
  })
}
# 用 -InputObject 而不是管道:管道会把空数组吃掉(输出空串),把 $null
# 变成字面 "null";-InputObject @() 稳定输出合法的 []。
ConvertTo-Json -InputObject @($rows.ToArray()) -Compress -Depth 4
"""
    completed = subprocess.run(
        [
            "powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive",
            "-ExecutionPolicy", "Bypass", "-Command", script,
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=_WINDOWS_DISCOVERY_TIMEOUT_S,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if completed.returncode != 0:
        raise RuntimeError(
            completed.stderr.strip() or "Windows J-Link discovery failed"
        )
    return _json_rows(completed.stdout)


def _instance_serial(
    instance_id: str, parent: str, explicit_serial: str, mi: int | None,
) -> str:
    if explicit_serial.strip():
        return explicit_serial.strip()
    source = parent if mi is not None and _INSTANCE_ID_RE.match(parent) else instance_id
    return source.rsplit("\\", 1)[-1].strip()


def _windows_infos(rows: Iterable[dict[str, Any]]) -> list[JLinkUsbInfo]:
    grouped: dict[str, list[tuple[int, JLinkUsbInfo]]] = {}
    for row in rows:
        instance_id = str(row.get("instance_id") or "")
        match = _INSTANCE_ID_RE.match(instance_id)
        if match is None:
            continue
        vid, pid = int(match.group(1), 16), int(match.group(2), 16)
        if vid != JLINK_VENDOR_ID:
            continue
        mi = int(match.group(3), 16) if match.group(3) else None
        serial = _instance_serial(
            instance_id,
            str(row.get("parent") or ""),
            str(row.get("serial_number") or ""),
            mi,
        )
        product = str(
            row.get("bus_description") or row.get("friendly_name") or "J-Link"
        )
        status = str(row.get("status") or "Unknown")
        try:
            problem_code = int(row.get("problem_code") or 0)
        except (TypeError, ValueError):
            problem_code = 0
        info = JLinkUsbInfo(
            device=instance_id,
            description=product,
            hwid=f"USB VID:PID={vid:04X}:{pid:04X} SER={serial}",
            manufacturer="SEGGER",
            product=product,
            serial_number=serial,
            vid=vid,
            pid=pid,
            location=str(row.get("location") or ""),
            interface=f"MI_{mi:02X}" if mi is not None else "",
            instance_id=instance_id,
            status=status,
            problem_code=problem_code,
        )
        container = str(row.get("container_id") or "").strip()
        parent = str(row.get("parent") or "").strip()
        # ContainerId is the strongest physical identity. When Windows omits
        # it, group composite children under their parent instance while
        # preserving separate non-MI instances even if cloned probes report the
        # same serial number.
        key = container or (
            parent.lower() if mi is not None and parent
            else instance_id.lower()
        )
        # The non-MI parent represents the physical probe. Keep an interface
        # only when Windows did not expose that parent in the PnP snapshot.
        rank = 0 if mi is None else 1
        grouped.setdefault(key, []).append((rank, info))
    return [
        min(candidates, key=lambda item: item[0])[1]
        for candidates in grouped.values()
    ]


def _walk_ioreg(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_ioreg(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_ioreg(child)


def _macos_infos(payload: Any) -> list[JLinkUsbInfo]:
    infos: dict[tuple[int, str, str], JLinkUsbInfo] = {}
    for item in _walk_ioreg(payload):
        try:
            vid = int(item.get("idVendor"))
            pid = int(item.get("idProduct"))
        except (TypeError, ValueError):
            continue
        if vid != JLINK_VENDOR_ID:
            continue
        serial = str(
            item.get("USB Serial Number")
            or item.get("kUSBSerialNumberString")
            or ""
        ).strip()
        location_value = item.get("locationID")
        location = (
            f"0x{int(location_value):08x}"
            if isinstance(location_value, int) else str(location_value or "")
        )
        product = str(
            item.get("USB Product Name")
            or item.get("kUSBProductString")
            or "J-Link"
        )
        registry_id = str(item.get("IORegistryEntryID") or location or serial)
        info = JLinkUsbInfo(
            device=f"ioreg:{registry_id}",
            description=product,
            hwid=f"USB VID:PID={vid:04X}:{pid:04X} SER={serial}",
            manufacturer=str(
                item.get("USB Vendor Name")
                or item.get("kUSBVendorString")
                or "SEGGER"
            ),
            product=product,
            serial_number=serial,
            vid=vid,
            pid=pid,
            location=location,
            interface="",
            instance_id=f"ioreg:{registry_id}",
            status="OK",
            problem_code=0,
        )
        infos[(pid, serial.lower(), location)] = info
    return list(infos.values())


def _macos_payload() -> Any:
    completed = subprocess.run(
        ["ioreg", "-a", "-p", "IOUSB", "-l", "-w", "0"],
        check=False,
        capture_output=True,
        timeout=_MACOS_DISCOVERY_TIMEOUT_S,
    )
    if completed.returncode != 0:
        detail = (
            completed.stderr.decode("utf-8", "replace")
            if isinstance(completed.stderr, bytes)
            else str(completed.stderr or "")
        ).strip()
        raise RuntimeError(detail or "macOS J-Link discovery failed")
    payload = (
        completed.stdout.encode("utf-8")
        if isinstance(completed.stdout, str) else completed.stdout
    )
    return plistlib.loads(payload)


def discover_jlink_usb_devices(
    platform: str | None = None,
) -> list[JLinkUsbInfo]:
    """Enumerate J-Link USB devices even when no CDC/driver is present.

    🔴 本函数**永不抛异常**。J-Link 枚举是一条与目标板无关的可选路径:
    枚举失败与"没插 J-Link"对上位机是同一个结论,都返回空列表。

    这条红线是真机事故换来的:Win11 上 PowerShell 子进程每轮都
    timeout=25s 超时,`subprocess.TimeoutExpired` 既不在
    ``gui_server._all_serial_port_infos`` 捕获的
    ``(OSError, RuntimeError, ValueError, JSONDecodeError)`` 里
    (它是 ``SubprocessError``),也不在 ``_run_device_discovery`` 捕获的
    ``(OSError, RuntimeError, ValueError)`` 里 —— 于是 device-discovery
    线程带着未处理异常死掉,`thread.unhandled` 每 25s 一条无限循环,
    probing 永远 true、data_port/smp_port 永远为空,整块 USB 板永久不可用。
    而那台机器上 VID_1366 设备数是 0:一段毫无关系的枚举瘫掉了全部设备发现。
    """
    platform = platform or sys.platform
    try:
        if platform == "win32":
            return _windows_infos(_windows_payload())
        if platform == "darwin":
            return _macos_infos(_macos_payload())
    except Exception as exc:
        # 故意宽捕获(不是 BaseException:KeyboardInterrupt/SystemExit 照旧
        # 传播)。要挡住的至少有:subprocess.TimeoutExpired、OSError
        # (powershell.exe/ioreg 不存在或不可执行)、json.JSONDecodeError、
        # plistlib.InvalidFileException、以及 returncode != 0 抛的
        # RuntimeError。但这条路径是可选的 —— 哪怕是解析代码自己的 bug,
        # 也不该让设备发现线程死掉;异常类型与信息全部留痕,便于回溯。
        _record_discovery_failure(platform, exc)
    return []
