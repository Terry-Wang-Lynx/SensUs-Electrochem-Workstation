"""Windows-only preparation of the WinUSB interface used by OpenOCD."""

from __future__ import annotations

import base64
import ctypes
import json
import os
import re
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


JLINK_VENDOR_ID = 0x1366
JLINK_WINUSB_HELPER_ENV = "SENSUS_WINUSB_HELPER"
_IS_WIN = os.name == "nt"
_DRIVER_ERROR_MARKERS = (
    "libusb_error_not_found",
    "libusb_error_not_supported",
    "no j-link device found",
)
_PROBE_COMMUNICATION_ERROR_MARKERS = (
    "libusb_error_timeout",
    "sending data to device timed out",
    "jaylink_get_firmware_version() failed",
    "transport_write() failed: timeout occurred",
)
# Driver replacement must be interface-specific. Rebinding the composite
# parent or CDC interface would make the probe disappear from device discovery.
_SUPPORTED_WINUSB_INTERFACES = {
    # Legacy J-Link ARM-OB: one vendor-specific USB function, no CDC child.
    (0x0101, None),
    # Newer composite J-Link: bind only the debug function, preserving CDC.
    (0x0105, 0x02),
}
_INSTANCE_ID_RE = re.compile(
    r"^USB\\VID_([0-9A-F]{4})&PID_([0-9A-F]{4})"
    r"(?:&MI_([0-9A-F]{2}))?\\",
    flags=re.IGNORECASE,
)


@dataclass(frozen=True)
class JLinkUsbInterface:
    vid: int
    pid: int
    mi: int | None
    instance_id: str


def openocd_reports_missing_driver(output: object) -> bool:
    text = str(output or "").lower()
    return any(marker in text for marker in _DRIVER_ERROR_MARKERS)


def openocd_reports_probe_communication_error(output: object) -> bool:
    """Identify failures that happen before OpenOCD reaches the SWD target."""
    text = str(output or "").lower()
    return any(marker in text for marker in _PROBE_COMMUNICATION_ERROR_MARKERS)


def resolve_helper(project_dir: Path) -> Path:
    configured = os.environ.get(JLINK_WINUSB_HELPER_ENV, "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    project_dir = Path(project_dir).resolve()
    candidates = (
        project_dir.parent / "tools" / "winusb" / "wdi-simple.exe",
        project_dir / "tools" / "winusb" / "wdi-simple.exe",
        project_dir / "artifacts" / "build" / "windows-x64"
        / "winusb" / "wdi-simple.exe",
    )
    return next((path for path in candidates if path.is_file()), candidates[0])


def _problem_device_payload(vid: int, pid: int) -> list[dict[str, Any]]:
    prefix = f"USB\\VID_{vid:04X}&PID_{pid:04X}"
    script = f"""
$ProgressPreference = 'SilentlyContinue'
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
$items = Get-PnpDevice -PresentOnly |
  Where-Object {{
    $_.InstanceId -like '{prefix}*' -and $_.Status -ne 'OK'
  }} |
  ForEach-Object {{
    [PSCustomObject]@{{
      instance_id = [string]$_.InstanceId
      status = [string]$_.Status
      class_name = [string]$_.Class
    }}
  }}
@($items) | ConvertTo-Json -Compress
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
            completed.stderr.strip() or "Windows device status query failed"
        )
    raw = completed.stdout.strip().lstrip("\ufeff")
    payload = json.loads(raw or "[]")
    if isinstance(payload, dict):
        return [payload]
    if not isinstance(payload, list):
        raise RuntimeError("Windows device status response was not a list")
    return [item for item in payload if isinstance(item, dict)]


def problem_interfaces(vid: int, pid: int) -> list[JLinkUsbInterface]:
    if int(vid) != JLINK_VENDOR_ID:
        raise ValueError("Only SEGGER J-Link USB interfaces may be prepared")
    matches: dict[tuple[int, int, int | None], JLinkUsbInterface] = {}
    for item in _problem_device_payload(int(vid), int(pid)):
        instance_id = str(item.get("instance_id") or "")
        match = _INSTANCE_ID_RE.match(instance_id)
        if match is None:
            continue
        found_vid = int(match.group(1), 16)
        found_pid = int(match.group(2), 16)
        if (found_vid, found_pid) != (int(vid), int(pid)):
            continue
        mi = int(match.group(3), 16) if match.group(3) else None
        interface = JLinkUsbInterface(found_vid, found_pid, mi, instance_id)
        matches[(found_vid, found_pid, mi)] = interface
    return list(matches.values())


def repairable_interfaces(vid: int, pid: int) -> list[JLinkUsbInterface]:
    """Return only verified J-Link debug interfaces that need a driver."""
    return [
        interface for interface in problem_interfaces(vid, pid)
        if (interface.pid, interface.mi) in _SUPPORTED_WINUSB_INTERFACES
    ]


def _is_administrator() -> bool:
    if not _IS_WIN:
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False


def _run_elevated(
    executable: Path, arguments: list[str], *, timeout_s: float,
) -> subprocess.CompletedProcess[str]:
    command = [str(executable), *arguments]
    common = {
        "check": False,
        "capture_output": True,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "timeout": timeout_s,
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0),
    }
    if _is_administrator():
        return subprocess.run(command, **common)

    with tempfile.TemporaryDirectory(prefix="sensus-jlink-elevation-") as output_dir:
        stdout_path = Path(output_dir) / "stdout.txt"
        stderr_path = Path(output_dir) / "stderr.txt"

        def ps_literal(value: object) -> str:
            return "'" + str(value).replace("'", "''") + "'"

        wrapper = "\n".join((
            "$ErrorActionPreference = 'Stop'",
            f"$argumentLine = {ps_literal(subprocess.list2cmdline(arguments))}",
            (
                f"$process = Start-Process -FilePath {ps_literal(executable)} "
                f"-ArgumentList $argumentLine -WindowStyle Hidden -Wait -PassThru "
                f"-RedirectStandardOutput {ps_literal(stdout_path)} "
                f"-RedirectStandardError {ps_literal(stderr_path)}"
            ),
            "exit $process.ExitCode",
        ))
        encoded_wrapper = base64.b64encode(
            wrapper.encode("utf-16-le")
        ).decode("ascii")
        environment = {
            **os.environ,
            "SENSUS_WINUSB_HELPER_COMMAND": encoded_wrapper,
        }
        script = """
$ErrorActionPreference = 'Stop'
$process = Start-Process `
  -FilePath 'powershell.exe' `
  -ArgumentList ('-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass ' +
    '-EncodedCommand ' + $env:SENSUS_WINUSB_HELPER_COMMAND) `
  -Verb RunAs -WindowStyle Hidden -Wait -PassThru
exit $process.ExitCode
"""
        launcher = subprocess.run(
            [
                "powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive",
                "-ExecutionPolicy", "Bypass", "-Command", script,
            ],
            env=environment,
            **common,
        )
        stdout = stdout_path.read_text(
            encoding="utf-8", errors="replace",
        ) if stdout_path.is_file() else ""
        stderr = stderr_path.read_text(
            encoding="utf-8", errors="replace",
        ) if stderr_path.is_file() else ""
        launcher_error = str(launcher.stderr or "").strip()
        if launcher_error:
            stderr = f"{launcher_error}\n{stderr}".strip()
        return subprocess.CompletedProcess(
            command, launcher.returncode, stdout, stderr,
        )


def install_winusb_driver(
    helper: Path,
    *,
    vid: int,
    pid: int,
    timeout_s: float = 180.0,
) -> dict[str, Any]:
    helper = Path(helper).resolve()
    if not _IS_WIN:
        raise RuntimeError("J-Link WinUSB preparation is only available on Windows")
    if not helper.is_file():
        raise RuntimeError("The portable WinUSB helper is missing")
    if int(vid) != JLINK_VENDOR_ID:
        raise ValueError("Only SEGGER J-Link USB interfaces may be prepared")

    interfaces = repairable_interfaces(int(vid), int(pid))
    if not interfaces:
        raise RuntimeError(
            "No supported J-Link debug interface requiring WinUSB was found"
        )

    installed: list[dict[str, Any]] = []
    for interface in interfaces:
        with tempfile.TemporaryDirectory(prefix="sensus-jlink-winusb-") as temporary:
            suffix = f"mi{interface.mi:02x}" if interface.mi is not None else "device"
            arguments = [
                "--name", "SensUs-J-Link-WinUSB",
                "--manufacturer", "SEGGER",
                "--vid", f"0x{interface.vid:04x}",
                "--pid", f"0x{interface.pid:04x}",
                "--type", "0",
                "--dest", temporary,
                "--inf", f"sensus-jlink-{interface.pid:04x}-{suffix}.inf",
                "--timeout", "120000",
                "--log", "1",
            ]
            if interface.mi is not None:
                arguments.extend(("--iid", f"0x{interface.mi:02x}"))
            completed = _run_elevated(
                helper, arguments, timeout_s=timeout_s,
            )
            if completed.returncode != 0:
                detail = " | ".join(
                    line.strip()
                    for line in f"{completed.stdout}\n{completed.stderr}".splitlines()
                    if line.strip()
                )
                raise RuntimeError(
                    "J-Link WinUSB preparation was cancelled or failed"
                    + (f": {detail}" if detail else f" (code {completed.returncode})")
                )
            installed.append(asdict(interface))
    return {"installed": installed, "helper": str(helper)}
