"""Driver-independent discovery of SEGGER J-Link USB devices."""

from __future__ import annotations

import json
import plistlib
import re
import subprocess
import sys
from dataclasses import dataclass
from typing import Any, Iterable


JLINK_VENDOR_ID = 0x1366
_INSTANCE_ID_RE = re.compile(
    r"^USB\\VID_([0-9A-F]{4})&PID_([0-9A-F]{4})"
    r"(?:&MI_([0-9A-F]{2}))?\\",
    flags=re.IGNORECASE,
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
$items = Get-PnpDevice -PresentOnly |
  Where-Object { $_.InstanceId -like 'USB\VID_1366*' } |
  ForEach-Object {
    $instanceId = [string]$_.InstanceId
    [PSCustomObject]@{
      instance_id = $instanceId
      friendly_name = [string]$_.FriendlyName
      class_name = [string]$_.Class
      status = [string]$_.Status
      bus_description = [string](Read-DeviceProperty $instanceId 'DEVPKEY_Device_BusReportedDeviceDesc')
      serial_number = [string](Read-DeviceProperty $instanceId 'DEVPKEY_Device_SerialNumber')
      parent = [string](Read-DeviceProperty $instanceId 'DEVPKEY_Device_Parent')
      container_id = [string](Read-DeviceProperty $instanceId 'DEVPKEY_Device_ContainerId')
      location = [string](Read-DeviceProperty $instanceId 'DEVPKEY_Device_LocationInfo')
      problem_code = [int](Read-DeviceProperty $instanceId 'DEVPKEY_Device_ProblemCode')
    }
  }
@($items) | ConvertTo-Json -Compress -Depth 4
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
        timeout=25,
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
        timeout=8,
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
    """Enumerate J-Link USB devices even when no CDC/driver is present."""
    platform = platform or sys.platform
    if platform == "win32":
        return _windows_infos(_windows_payload())
    if platform == "darwin":
        return _macos_infos(_macos_payload())
    return []
