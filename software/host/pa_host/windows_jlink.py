"""Windows-only preparation of the WinUSB interface used by OpenOCD."""

from __future__ import annotations

import base64
import ctypes
import json
import os
import re
import subprocess
import tempfile
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable


JLINK_VENDOR_ID = 0x1366
JLINK_WINUSB_HELPER_ENV = "SENSUS_WINUSB_HELPER"
_IS_WIN = os.name == "nt"
_DRIVER_ERROR_MARKERS = (
    "libusb_error_not_found",
    "libusb_error_not_supported",
    "libusb_error_access",
    "access denied",
    "cannot open j-link",
    "no j-link device found",
)
_PROBE_COMMUNICATION_ERROR_MARKERS = (
    "libusb_error_timeout",
    "sending data to device timed out",
    "jaylink_get_firmware_version() failed",
    "transport_write() failed: timeout occurred",
)
_COMMANDER_PROBE_CONNECTED_MARKERS = (
    "connecting to j-link via usb...o.k.",
    "connecting to j-link via usb...ok",
    "hardware version:",
    "s/n:",
)
_PROBE_BUSY_MARKERS = (
    "libusb_error_busy",
    "device is already in use",
    "j-link is already in use",
)
_LIBUSB_COMPATIBLE_SERVICES = {"winusb", "libusbk", "libusb0"}
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


@dataclass(frozen=True)
class JLinkUsbBinding:
    """One physical J-Link debug interface and its current PnP binding."""

    interface: JLinkUsbInterface
    probe_serial: str
    parent_id: str
    container_id: str
    status: str
    problem_code: int
    service: str
    driver_inf_path: str
    driver_provider: str

    @property
    def ready(self) -> bool:
        return (
            self.status.strip().lower() == "ok"
            and self.problem_code == 0
            and self.service.strip().lower() in _LIBUSB_COMPATIBLE_SERVICES
        )


def openocd_reports_missing_driver(output: object) -> bool:
    text = str(output or "").lower()
    return any(marker in text for marker in _DRIVER_ERROR_MARKERS)


def openocd_reports_probe_communication_error(output: object) -> bool:
    """Identify failures that happen before OpenOCD reaches the SWD target."""
    text = str(output or "").lower()
    return any(marker in text for marker in _PROBE_COMMUNICATION_ERROR_MARKERS)


def commander_reports_probe_connected(output: object) -> bool:
    """Return true only when Commander opened the probe before SWD failed."""
    text = str(output or "").lower()
    return any(marker in text for marker in _COMMANDER_PROBE_CONNECTED_MARKERS)


def reports_probe_busy(output: object) -> bool:
    text = str(output or "").lower()
    return any(marker in text for marker in _PROBE_BUSY_MARKERS)


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
function Read-DeviceProperty([string]$InstanceId, [string]$KeyName) {{
  $property = Get-PnpDeviceProperty `
    -InstanceId $InstanceId -KeyName $KeyName -ErrorAction SilentlyContinue
  if ($null -ne $property) {{ return $property.Data }}
  return $null
}}
$items = Get-PnpDevice -PresentOnly |
  Where-Object {{
    $_.InstanceId -like '{prefix}*'
  }} |
  ForEach-Object {{
    $instanceId = [string]$_.InstanceId
    [PSCustomObject]@{{
      instance_id = $instanceId
      status = [string]$_.Status
      class_name = [string]$_.Class
      problem_code = [int](Read-DeviceProperty $instanceId 'DEVPKEY_Device_ProblemCode')
      service = [string](Read-DeviceProperty $instanceId 'DEVPKEY_Device_Service')
      driver_inf_path = [string](Read-DeviceProperty $instanceId 'DEVPKEY_Device_DriverInfPath')
      driver_provider = [string](Read-DeviceProperty $instanceId 'DEVPKEY_Device_DriverProvider')
      parent = [string](Read-DeviceProperty $instanceId 'DEVPKEY_Device_Parent')
      container_id = [string](Read-DeviceProperty $instanceId 'DEVPKEY_Device_ContainerId')
      serial_number = [string](Read-DeviceProperty $instanceId 'DEVPKEY_Device_SerialNumber')
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


def _normalise_probe_serial(value: object) -> str:
    digits = re.sub(r"\D", "", str(value or ""))
    if not digits:
        return ""
    return str(int(digits, 10))


def _binding_probe_serial(
    item: dict[str, Any], instance_id: str, mi: int | None,
) -> str:
    explicit = _normalise_probe_serial(item.get("serial_number"))
    if explicit:
        return explicit
    parent = str(item.get("parent") or "")
    if mi is not None and _INSTANCE_ID_RE.match(parent):
        return _normalise_probe_serial(parent.rsplit("\\", 1)[-1])
    if mi is None:
        return _normalise_probe_serial(instance_id.rsplit("\\", 1)[-1])
    # Composite child instance tails such as 7&ABC&0&0002 are opaque PnP
    # addresses, not J-Link serial numbers.
    return ""


def jlink_bindings(vid: int, pid: int) -> list[JLinkUsbBinding]:
    """Return every supported debug interface without collapsing probes."""
    if int(vid) != JLINK_VENDOR_ID:
        raise ValueError("Only SEGGER J-Link USB interfaces may be prepared")
    matches: dict[str, JLinkUsbBinding] = {}
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
        if (found_pid, mi) not in _SUPPORTED_WINUSB_INTERFACES:
            continue
        try:
            problem_code = int(item.get("problem_code") or 0)
        except (TypeError, ValueError):
            problem_code = 0
        interface = JLinkUsbInterface(found_vid, found_pid, mi, instance_id)
        matches[instance_id.lower()] = JLinkUsbBinding(
            interface=interface,
            probe_serial=_binding_probe_serial(item, instance_id, mi),
            parent_id=str(item.get("parent") or ""),
            container_id=str(item.get("container_id") or ""),
            status=str(item.get("status") or ""),
            problem_code=problem_code,
            service=str(item.get("service") or ""),
            driver_inf_path=str(item.get("driver_inf_path") or ""),
            driver_provider=str(item.get("driver_provider") or ""),
        )
    return list(matches.values())


def problem_interfaces(vid: int, pid: int) -> list[JLinkUsbInterface]:
    return [
        binding.interface for binding in jlink_bindings(vid, pid)
        if not binding.ready
    ]


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
    executable: Path,
    arguments: list[str],
    *,
    timeout_s: float,
    restart_instance_id: str = "",
    cleanup_paths: tuple[Path, ...] = (),
    status_callback: Callable[[str], None] | None = None,
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
        if status_callback is not None:
            status_callback("正在安装 J-Link Windows 驱动")
        completed = subprocess.run(command, **common)
        pnp_lines: list[str] = []
        if completed.returncode == 0 and restart_instance_id:
            restart_common = {
                "check": False,
                "capture_output": True,
                "text": True,
                "encoding": "utf-8",
                "errors": "replace",
                "timeout": 30,
                "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0),
            }
            for label, pnp_command in (
                (
                    "RESTART",
                    ["pnputil.exe", "/restart-device", restart_instance_id],
                ),
                ("SCAN", ["pnputil.exe", "/scan-devices"]),
            ):
                try:
                    pnp = subprocess.run(pnp_command, **restart_common)
                    pnp_lines.append(f"SENSUS_PNP_{label}_EXIT={pnp.returncode}")
                    detail = f"{pnp.stdout}\n{pnp.stderr}".strip()
                    if detail:
                        pnp_lines.append(detail)
                except subprocess.TimeoutExpired:
                    pnp_lines.append(f"SENSUS_PNP_{label}_EXIT=124")
        stdout = "\n".join(
            part for part in (completed.stdout, *pnp_lines) if str(part).strip()
        )
        return subprocess.CompletedProcess(
            completed.args, completed.returncode, stdout, completed.stderr,
        )

    with tempfile.TemporaryDirectory(
        prefix="sensus-jlink-elevation-", ignore_cleanup_errors=True,
    ) as output_dir:
        stdout_path = Path(output_dir) / "stdout.txt"
        stderr_path = Path(output_dir) / "stderr.txt"
        status_path = Path(output_dir) / "status.txt"
        # Keep ownership on the unelevated caller. If the elevated process
        # creates these files itself, Windows may give the caller no read ACL.
        stdout_path.touch()
        stderr_path.touch()
        status_path.touch()

        def ps_literal(value: object) -> str:
            return "'" + str(value).replace("'", "''") + "'"

        deadline_ms = int((time.time() + max(1.0, timeout_s)) * 1000)
        wrapper_lines = [
            "$ErrorActionPreference = 'Stop'",
            (
                f"Set-Content -LiteralPath {ps_literal(status_path)} "
                "-Value 'installing' -Encoding ASCII"
            ),
            f"$argumentLine = {ps_literal(subprocess.list2cmdline(arguments))}",
            f"$deadlineMs = [int64]{deadline_ms}",
            (
                "function Get-RemainingMilliseconds { "
                "$remaining = $deadlineMs - "
                "[DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds(); "
                "return [int][Math]::Max(0, [Math]::Min(2147483647, $remaining)) }"
            ),
            (
                "function Wait-Bounded([System.Diagnostics.Process]$process) { "
                "$remaining = Get-RemainingMilliseconds; "
                "if ($remaining -le 0 -or -not $process.WaitForExit($remaining)) { "
                "try { $process.Kill() } catch {}; return $false }; return $true }"
            ),
            "$exitCode = 1",
            "try {",
            (
                f"$process = Start-Process -FilePath {ps_literal(executable)} "
                f"-ArgumentList $argumentLine -WindowStyle Hidden -PassThru "
                f"-RedirectStandardOutput {ps_literal(stdout_path)} "
                f"-RedirectStandardError {ps_literal(stderr_path)}"
            ),
            (
                "  if (Wait-Bounded $process) { $exitCode = $process.ExitCode } "
                "else { $exitCode = 124; "
                f"Add-Content -LiteralPath {ps_literal(stderr_path)} "
                "-Value 'J-Link WinUSB helper timed out' }"
            ),
        ]
        if restart_instance_id:
            restart_arguments = subprocess.list2cmdline(
                ["/restart-device", restart_instance_id]
            )
            wrapper_lines.extend((
                "  if ($exitCode -eq 0) {",
                f"    $restartArguments = {ps_literal(restart_arguments)}",
                (
                    "    $restart = Start-Process -FilePath 'pnputil.exe' "
                    "-ArgumentList $restartArguments -WindowStyle Hidden "
                    "-PassThru"
                ),
                (
                    "    $restartCode = if (Wait-Bounded $restart) { "
                    "$restart.ExitCode } else { 124 }"
                ),
                f"    Add-Content -LiteralPath {ps_literal(stdout_path)} "
                "-Value ('SENSUS_PNP_RESTART_EXIT=' + $restartCode)",
                (
                    "    $scan = Start-Process -FilePath 'pnputil.exe' "
                    "-ArgumentList '/scan-devices' -WindowStyle Hidden -PassThru"
                ),
                (
                    "    $scanCode = if (Wait-Bounded $scan) { "
                    "$scan.ExitCode } else { 124 }"
                ),
                f"    Add-Content -LiteralPath {ps_literal(stdout_path)} "
                "-Value ('SENSUS_PNP_SCAN_EXIT=' + $scanCode)",
                "  }",
            ))
        wrapper_lines.extend(("} finally {",))
        for cleanup_path in cleanup_paths:
            wrapper_lines.append(
                "  Remove-Item -LiteralPath "
                f"{ps_literal(Path(cleanup_path))} -Recurse -Force "
                "-ErrorAction SilentlyContinue"
            )
        wrapper_lines.extend(("}", "exit $exitCode"))
        wrapper = "\n".join(wrapper_lines)
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
        launcher_common = dict(common)
        launcher_common["timeout"] = max(1.0, timeout_s) + 15.0
        if status_callback is not None:
            status_callback("请确认 Windows 管理员权限提示")
        watcher_stop = threading.Event()

        def watch_elevated_status() -> None:
            while not watcher_stop.wait(0.1):
                try:
                    installing = status_path.read_text(
                        encoding="ascii", errors="replace",
                    ).strip() == "installing"
                except OSError:
                    installing = False
                if installing:
                    if status_callback is not None:
                        status_callback(
                            "管理员权限已确认，正在安装 J-Link Windows 驱动"
                        )
                    return

        watcher = threading.Thread(
            target=watch_elevated_status,
            name="jlink-uac-status",
            daemon=True,
        )
        watcher.start()
        try:
            launcher = subprocess.run(
                [
                    "powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive",
                    "-ExecutionPolicy", "Bypass", "-Command", script,
                ],
                env=environment,
                **launcher_common,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                "等待 Windows 管理员确认或驱动安装超时；请确认提示后重试"
            ) from exc
        finally:
            watcher_stop.set()
            watcher.join(timeout=1)
        def readable_output(path: Path) -> str:
            try:
                return path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                # The exit code remains authoritative. Driver installation must
                # not be reported as failed only because UAC changed a log ACL.
                return ""

        stdout = readable_output(stdout_path)
        stderr = readable_output(stderr_path)
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
    interfaces: list[JLinkUsbInterface] | None = None,
    status_callback: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    helper = Path(helper).resolve()
    if not _IS_WIN:
        raise RuntimeError("J-Link WinUSB preparation is only available on Windows")
    if not helper.is_file():
        raise RuntimeError("The portable WinUSB helper is missing")
    if int(vid) != JLINK_VENDOR_ID:
        raise ValueError("Only SEGGER J-Link USB interfaces may be prepared")

    interfaces = (
        repairable_interfaces(int(vid), int(pid))
        if interfaces is None else list(interfaces)
    )
    if not interfaces:
        raise RuntimeError(
            "No supported J-Link debug interface requiring WinUSB was found"
        )

    installed: list[dict[str, Any]] = []
    for interface in interfaces:
        if (
            (interface.vid, interface.pid) != (int(vid), int(pid))
            or (interface.pid, interface.mi) not in _SUPPORTED_WINUSB_INTERFACES
        ):
            raise RuntimeError("Unsupported J-Link debug interface")
        with tempfile.TemporaryDirectory(
            prefix="sensus-jlink-winusb-", ignore_cleanup_errors=True,
        ) as temporary:
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
            try:
                completed = _run_elevated(
                    helper,
                    arguments,
                    timeout_s=timeout_s,
                    restart_instance_id=interface.instance_id,
                    cleanup_paths=(Path(temporary),),
                    status_callback=status_callback,
                )
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError(
                    "J-Link 驱动准备超时；请确认 Windows 权限提示后重试"
                ) from exc
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
