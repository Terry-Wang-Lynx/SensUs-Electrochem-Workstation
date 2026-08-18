import base64
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from pa_host import gui_server, jlink_usb, windows_jlink


def _port(
    device: str,
    *,
    description: str = "",
    hwid: str = "",
    manufacturer: str = "",
    product: str = "",
    serial_number: str = "",
    vid: int | None = None,
    pid: int | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        device=device,
        description=description,
        hwid=hwid,
        manufacturer=manufacturer,
        product=product,
        serial_number=serial_number,
        vid=vid,
        pid=pid,
        location="1-2",
    )


def _binding(
    interface: windows_jlink.JLinkUsbInterface,
    *,
    probe_serial: str = "",
    ready: bool = False,
) -> windows_jlink.JLinkUsbBinding:
    return windows_jlink.JLinkUsbBinding(
        interface=interface,
        probe_serial=probe_serial,
        parent_id="",
        container_id="container",
        status="OK",
        problem_code=0,
        service="WinUSB" if ready else "",
        driver_inf_path="oem55.inf" if ready else "",
        driver_provider="libwdi" if ready else "",
    )


@pytest.mark.parametrize(
    "output",
    [
        "LIBUSB_ERROR_NOT_FOUND",
        "libusb_error_not_supported",
        "LIBUSB_ERROR_ACCESS",
        "access denied",
        "cannot open J-Link",
        "Error: No J-Link device found",
    ],
)
def test_openocd_missing_driver_markers(output: str) -> None:
    assert windows_jlink.openocd_reports_missing_driver(output) is True


def test_openocd_target_error_is_not_a_missing_driver() -> None:
    assert windows_jlink.openocd_reports_missing_driver(
        "Error: Could not find MEM-AP to control the core"
    ) is False


@pytest.mark.parametrize(
    "output",
    [
        "LIBUSB_ERROR_TIMEOUT",
        "Error: Sending data to device timed out",
        "jaylink_get_firmware_version() failed: timeout occurred",
    ],
)
def test_openocd_probe_communication_markers(output: str) -> None:
    assert windows_jlink.openocd_reports_probe_communication_error(output) is True


def test_target_swd_error_is_not_a_probe_communication_error() -> None:
    assert windows_jlink.openocd_reports_probe_communication_error(
        "Error: Could not find MEM-AP to control the core"
    ) is False


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("LIBUSB_ERROR_BUSY", True),
        ("J-Link is already in use", True),
        ("LIBUSB_ERROR_ACCESS", False),
        ("access denied", False),
        ("cannot open J-Link", False),
    ],
)
def test_probe_busy_requires_an_explicit_ownership_error(
    output: str, expected: bool,
) -> None:
    assert windows_jlink.reports_probe_busy(output) is expected


def test_repairable_interfaces_only_selects_verified_debug_interface(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        windows_jlink,
        "_problem_device_payload",
        lambda _vid, _pid: [
            {"instance_id": r"USB\VID_1366&PID_0101\LEGACY"},
            {"instance_id": r"USB\VID_1366&PID_0105&MI_00\SERIAL"},
            {"instance_id": r"USB\VID_1366&PID_0105&MI_02\SERIAL"},
            {"instance_id": r"USB\VID_1366&PID_0105\SERIAL"},
            {"instance_id": r"USB\VID_1234&PID_0105&MI_02\OTHER"},
        ],
    )

    interfaces = windows_jlink.repairable_interfaces(0x1366, 0x0105)

    assert [(item.pid, item.mi) for item in interfaces] == [(0x0105, 0x02)]


def test_repairable_interfaces_accepts_legacy_single_interface(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        windows_jlink,
        "_problem_device_payload",
        lambda _vid, _pid: [{
            "instance_id": r"USB\VID_1366&PID_0101\000000123456",
        }],
    )

    interfaces = windows_jlink.repairable_interfaces(0x1366, 0x0101)

    assert [(item.pid, item.mi) for item in interfaces] == [(0x0101, None)]


def test_repairable_interfaces_detects_status_ok_without_driver_binding(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        windows_jlink,
        "_problem_device_payload",
        lambda _vid, _pid: [{
            "instance_id": r"USB\VID_1366&PID_0101\000000123456",
            "status": "OK",
            "problem_code": 0,
            "service": "",
            "driver_inf_path": "",
            "driver_provider": "",
        }],
    )

    interfaces = windows_jlink.repairable_interfaces(0x1366, 0x0101)

    assert [(item.pid, item.mi) for item in interfaces] == [(0x0101, None)]


@pytest.mark.parametrize("service", ["WinUSB", "libusbK", "libusb0"])
def test_repairable_interfaces_accepts_working_libusb_binding(
    monkeypatch, service: str,
) -> None:
    monkeypatch.setattr(
        windows_jlink,
        "_problem_device_payload",
        lambda _vid, _pid: [{
            "instance_id": r"USB\VID_1366&PID_0101\000000123456",
            "status": "OK",
            "problem_code": 0,
            "service": service,
            "driver_inf_path": "oem55.inf",
            "driver_provider": "libwdi",
        }],
    )

    assert windows_jlink.repairable_interfaces(0x1366, 0x0101) == []


def test_repairable_interfaces_repairs_broken_winusb_binding(monkeypatch) -> None:
    monkeypatch.setattr(
        windows_jlink,
        "_problem_device_payload",
        lambda _vid, _pid: [{
            "instance_id": r"USB\VID_1366&PID_0101\000000123456",
            "status": "Error",
            "problem_code": 28,
            "service": "WinUSB",
        }],
    )

    interfaces = windows_jlink.repairable_interfaces(0x1366, 0x0101)

    assert [(item.pid, item.mi) for item in interfaces] == [(0x0101, None)]


def test_composite_binding_reads_probe_serial_from_parent(monkeypatch) -> None:
    monkeypatch.setattr(
        windows_jlink,
        "_problem_device_payload",
        lambda _vid, _pid: [{
            "instance_id": r"USB\VID_1366&PID_0105&MI_02\7&ABC&0&0002",
            "parent": r"USB\VID_1366&PID_0105\000029734569",
            "status": "Error",
            "problem_code": 28,
        }],
    )

    bindings = windows_jlink.jlink_bindings(0x1366, 0x0105)

    assert len(bindings) == 1
    assert bindings[0].probe_serial == "29734569"
    assert bindings[0].interface.mi == 0x02


def test_same_pid_physical_probes_are_not_collapsed(monkeypatch) -> None:
    monkeypatch.setattr(
        windows_jlink,
        "_problem_device_payload",
        lambda _vid, _pid: [
            {
                "instance_id": r"USB\VID_1366&PID_0101\000000123456",
                "status": "Error", "problem_code": 28,
            },
            {
                "instance_id": r"USB\VID_1366&PID_0101\000000654321",
                "status": "Error", "problem_code": 28,
            },
        ],
    )

    bindings = windows_jlink.jlink_bindings(0x1366, 0x0101)

    assert {binding.probe_serial for binding in bindings} == {"123456", "654321"}
    assert len(windows_jlink.problem_interfaces(0x1366, 0x0101)) == 2


def test_problem_interfaces_rejects_non_segger_vendor() -> None:
    with pytest.raises(ValueError, match="SEGGER"):
        windows_jlink.problem_interfaces(0x1234, 0x0105)


def test_install_winusb_uses_only_supported_interface(
    tmp_path: Path, monkeypatch,
) -> None:
    helper = tmp_path / "wdi-simple.exe"
    helper.write_bytes(b"helper")
    interface = windows_jlink.JLinkUsbInterface(
        0x1366, 0x0105, 0x02,
        r"USB\VID_1366&PID_0105&MI_02\SERIAL",
    )
    commands: list[tuple[Path, list[str], float, str, tuple[Path, ...]]] = []
    monkeypatch.setattr(windows_jlink, "_IS_WIN", True)
    monkeypatch.setattr(
        windows_jlink, "repairable_interfaces", lambda _vid, _pid: [interface]
    )

    def fake_run(
        executable, arguments, *, timeout_s, restart_instance_id, cleanup_paths,
        status_callback,
    ):
        assert status_callback is None
        commands.append(
            (executable, arguments, timeout_s, restart_instance_id, cleanup_paths)
        )
        return subprocess.CompletedProcess([str(executable), *arguments], 0, "ok", "")

    monkeypatch.setattr(windows_jlink, "_run_elevated", fake_run)

    result = windows_jlink.install_winusb_driver(
        helper, vid=0x1366, pid=0x0105, timeout_s=91,
    )

    assert result["installed"] == [
        {
            "vid": 0x1366,
            "pid": 0x0105,
            "mi": 0x02,
            "instance_id": interface.instance_id,
        }
    ]
    executable, arguments, timeout, restart_instance_id, cleanup_paths = commands[0]
    assert executable == helper.resolve()
    assert timeout == 91
    assert restart_instance_id == interface.instance_id
    assert len(cleanup_paths) == 1
    assert arguments[arguments.index("--iid") + 1] == "0x02"
    assert arguments[arguments.index("--type") + 1] == "0"
    assert arguments[arguments.index("--log") + 1] == "1"
    assert "--silent" not in arguments


def test_install_winusb_legacy_device_does_not_guess_an_interface_id(
    tmp_path: Path, monkeypatch,
) -> None:
    helper = tmp_path / "wdi-simple.exe"
    helper.write_bytes(b"helper")
    interface = windows_jlink.JLinkUsbInterface(
        0x1366, 0x0101, None,
        r"USB\VID_1366&PID_0101\000000123456",
    )
    monkeypatch.setattr(windows_jlink, "_IS_WIN", True)
    monkeypatch.setattr(
        windows_jlink, "repairable_interfaces", lambda _vid, _pid: [interface]
    )
    calls: list[list[str]] = []

    def fake_run(
        _executable, arguments, *, timeout_s, restart_instance_id, cleanup_paths,
        status_callback,
    ):
        assert status_callback is None
        assert timeout_s == 180.0
        assert restart_instance_id == interface.instance_id
        assert len(cleanup_paths) == 1
        calls.append(arguments)
        return subprocess.CompletedProcess(arguments, 0, "ok", "")

    monkeypatch.setattr(windows_jlink, "_run_elevated", fake_run)

    result = windows_jlink.install_winusb_driver(
        helper, vid=0x1366, pid=0x0101,
    )

    assert result["installed"][0]["mi"] is None
    assert len(calls) == 1
    assert "--iid" not in calls[0]
    assert calls[0][calls[0].index("--pid") + 1] == "0x0101"


def test_install_winusb_never_guesses_an_interface(
    tmp_path: Path, monkeypatch,
) -> None:
    helper = tmp_path / "wdi-simple.exe"
    helper.write_bytes(b"helper")
    monkeypatch.setattr(windows_jlink, "_IS_WIN", True)
    monkeypatch.setattr(
        windows_jlink, "repairable_interfaces", lambda _vid, _pid: []
    )

    with pytest.raises(RuntimeError, match="supported J-Link debug interface"):
        windows_jlink.install_winusb_driver(helper, vid=0x1366, pid=0x0105)


def test_non_admin_helper_uses_elevated_wrapper_for_output_capture(
    tmp_path: Path, monkeypatch,
) -> None:
    helper = tmp_path / "folder with spaces" / "wdi-simple.exe"
    helper.parent.mkdir()
    helper.write_bytes(b"helper")
    captured: dict[str, object] = {}
    monkeypatch.setattr(windows_jlink, "_is_administrator", lambda: False)

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["environment"] = kwargs["env"]
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(windows_jlink.subprocess, "run", fake_run)

    result = windows_jlink._run_elevated(
        helper,
        ["--dest", str(tmp_path / "output with spaces")],
        timeout_s=30,
        restart_instance_id=r"USB\VID_1366&PID_0101\000000123456",
        cleanup_paths=(tmp_path / "output with spaces",),
    )

    outer = str(captured["command"][-1])
    assert "-Verb RunAs" in outer
    assert "RedirectStandardOutput" not in outer
    environment = captured["environment"]
    wrapper = base64.b64decode(
        environment["SENSUS_WINUSB_HELPER_COMMAND"]
    ).decode("utf-16-le")
    assert "-RedirectStandardOutput" in wrapper
    assert "-RedirectStandardError" in wrapper
    assert "pnputil.exe" in wrapper
    assert "/restart-device" in wrapper
    assert "Remove-Item -LiteralPath" in wrapper
    assert '"' + str(tmp_path / "output with spaces") + '"' in wrapper
    assert result.returncode == 0


def test_elevated_helper_log_acl_cannot_turn_success_into_failure(
    tmp_path: Path, monkeypatch,
) -> None:
    helper = tmp_path / "wdi-simple.exe"
    helper.write_bytes(b"helper")
    monkeypatch.setattr(windows_jlink, "_is_administrator", lambda: False)
    monkeypatch.setattr(
        windows_jlink.subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(command, 0, "", ""),
    )
    original_read_text = Path.read_text

    def deny_elevated_logs(path: Path, *args, **kwargs):
        if path.name in {"stdout.txt", "stderr.txt"}:
            raise PermissionError(5, "access denied", str(path))
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", deny_elevated_logs)

    result = windows_jlink._run_elevated(helper, [], timeout_s=30)

    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


def test_elevated_helper_timeout_is_reported_as_actionable_error(
    tmp_path: Path, monkeypatch,
) -> None:
    helper = tmp_path / "wdi-simple.exe"
    helper.write_bytes(b"helper")
    monkeypatch.setattr(windows_jlink, "_is_administrator", lambda: False)
    monkeypatch.setattr(
        windows_jlink.subprocess,
        "run",
        Mock(side_effect=subprocess.TimeoutExpired("powershell.exe", 2)),
    )

    with pytest.raises(RuntimeError, match="管理员确认.*超时"):
        windows_jlink._run_elevated(helper, [], timeout_s=1)


def test_uac_status_is_emitted_only_immediately_before_elevation(
    tmp_path: Path, monkeypatch,
) -> None:
    helper = tmp_path / "wdi-simple.exe"
    helper.write_bytes(b"helper")
    statuses: list[str] = []
    monkeypatch.setattr(windows_jlink, "_is_administrator", lambda: False)
    monkeypatch.setattr(
        windows_jlink.subprocess,
        "run",
        Mock(return_value=subprocess.CompletedProcess([], 0, "", "")),
    )

    windows_jlink._run_elevated(
        helper, [], timeout_s=1, status_callback=statuses.append,
    )

    assert statuses == ["请确认 Windows 管理员权限提示"]


def test_resolve_helper_finds_portable_sibling(tmp_path: Path, monkeypatch) -> None:
    project = tmp_path / "workstation"
    helper = tmp_path / "tools" / "winusb" / "wdi-simple.exe"
    project.mkdir()
    helper.parent.mkdir(parents=True)
    helper.write_bytes(b"helper")
    monkeypatch.delenv(windows_jlink.JLINK_WINUSB_HELPER_ENV, raising=False)

    assert windows_jlink.resolve_helper(project) == helper


def test_windows_serial_discovery_skips_non_usb_com_ports(monkeypatch) -> None:
    from serial.tools import list_ports

    bluetooth = _port("COM3", description="Standard Serial over Bluetooth link")
    motherboard = _port("COM4", description="Communications Port")
    board = _port(
        "COM10", description="USB Serial Device", vid=0x2FE3, pid=0x0100,
    )
    jlink = _port(
        "COM11", description="J-Link", manufacturer="SEGGER",
        serial_number="000029734569", vid=0x1366, pid=0x0105,
    )
    monkeypatch.setattr(gui_server, "_IS_WIN", True)
    monkeypatch.setattr(gui_server, "discover_jlink_usb_devices", lambda: [])
    monkeypatch.setattr(
        list_ports, "comports", lambda: [bluetooth, motherboard, board, jlink]
    )

    assert gui_server._all_serial_port_infos() == [board, jlink]


def test_driverless_legacy_jlink_is_discovered_without_a_com_port(
    monkeypatch,
) -> None:
    from serial.tools import list_ports

    legacy = jlink_usb.JLinkUsbInfo(
        device=r"USB\VID_1366&PID_0101\000000123456",
        description="J-Link",
        hwid="USB VID:PID=1366:0101 SER=000000123456",
        manufacturer="SEGGER",
        product="J-Link",
        serial_number="000000123456",
        vid=0x1366,
        pid=0x0101,
        location="Port_#0001.Hub_#0002",
        interface="",
        instance_id=r"USB\VID_1366&PID_0101\000000123456",
        status="Error",
        problem_code=28,
    )
    monkeypatch.setattr(list_ports, "comports", lambda: [])
    monkeypatch.setattr(
        gui_server, "discover_jlink_usb_devices", lambda: [legacy]
    )
    monkeypatch.setattr(
        gui_server, "_probe_jlink_target_status",
        lambda serial, force=False: gui_server._unknown_jlink_target_status(serial),
    )

    devices = gui_server._discover_devices(probe=False)

    assert len(devices) == 1
    assert devices[0]["id"] == "jlink:123456"
    assert devices[0]["probe_serial"] == "123456"
    assert devices[0]["pid"] == 0x0101
    assert devices[0]["cdc_port"].startswith("USB\\VID_1366")


def test_native_jlink_is_deduplicated_against_its_cdc_port(monkeypatch) -> None:
    from serial.tools import list_ports

    cdc = _port(
        "COM11", description="J-Link", manufacturer="SEGGER",
        serial_number="000029734569", vid=0x1366, pid=0x0105,
    )
    native = jlink_usb.JLinkUsbInfo(
        device=r"USB\VID_1366&PID_0105\000029734569",
        description="J-Link", hwid="USB VID:PID=1366:0105",
        manufacturer="SEGGER", product="J-Link",
        serial_number="000029734569", vid=0x1366, pid=0x0105,
        location="Port 1", interface="",
        instance_id=r"USB\VID_1366&PID_0105\000029734569",
        status="OK", problem_code=0,
    )
    monkeypatch.setattr(list_ports, "comports", lambda: [cdc])
    monkeypatch.setattr(gui_server, "_IS_WIN", True)
    monkeypatch.setattr(
        gui_server, "discover_jlink_usb_devices", lambda: [native]
    )

    infos = gui_server._all_serial_port_infos()

    assert infos == [cdc]


@pytest.mark.parametrize("pnp_problem,expected", [(True, "missing"), (False, "unknown")])
def test_jlink_driver_is_missing_only_after_pnp_confirmation(
    tmp_path: Path, monkeypatch, pnp_problem: bool, expected: str,
) -> None:
    helper = tmp_path / "wdi-simple.exe"
    helper.write_bytes(b"helper")
    monkeypatch.setattr(gui_server, "_IS_WIN", True)
    monkeypatch.setattr(gui_server, "WINUSB_HELPER", helper)
    monkeypatch.setattr(gui_server, "JLINK_EXE", tmp_path / "missing-jlink")
    monkeypatch.setattr(
        gui_server,
        "_openocd_target_probe",
        lambda _serial: (False, "LIBUSB_ERROR_NOT_FOUND\nNo J-Link device found"),
    )
    monkeypatch.setattr(
        gui_server, "_jlink_requires_winusb", lambda _serial: pnp_problem
    )
    with gui_server.JLINK_TARGET_CACHE_LOCK:
        gui_server.JLINK_TARGET_CACHE.clear()

    status = gui_server._probe_jlink_target_status("29734569", force=True)

    assert status["driver_state"] == expected
    assert status["driver_action"] == (
        "install_winusb" if pnp_problem else ""
    )


def test_ready_winusb_binding_is_not_reprepared_after_transient_probe_failure(
    monkeypatch,
) -> None:
    interface = windows_jlink.JLinkUsbInterface(
        0x1366, 0x0101, None,
        r"USB\VID_1366&PID_0101\000000123456",
    )
    monkeypatch.setattr(gui_server, "DEVICE_DISCOVERY_CACHE", [{
        "kind": "jlink", "probe_serial": "123456",
        "vid": 0x1366, "pid": 0x0101,
    }])
    monkeypatch.setattr(
        gui_server, "jlink_bindings",
        lambda _vid, _pid: [_binding(
            interface, probe_serial="123456", ready=True,
        )],
    )

    assert gui_server._jlink_requires_winusb("123456") is False


def test_target_probe_prefers_bundled_openocd_over_system_commander(
    tmp_path: Path, monkeypatch,
) -> None:
    commander = tmp_path / "JLink.exe"
    commander.touch()
    commander_probe = Mock(return_value=(False, "incompatible Commander"))
    monkeypatch.setattr(gui_server, "JLINK_EXE", commander)
    monkeypatch.setattr(gui_server, "_openocd_jlink_available", lambda: True)
    monkeypatch.setattr(
        gui_server, "_openocd_target_probe",
        lambda _serial: (True, "SENSUS_INFO_PART=0x00052833"),
    )
    monkeypatch.setattr(gui_server, "probe_jlink_target", commander_probe)
    with gui_server.JLINK_TARGET_CACHE_LOCK:
        gui_server.JLINK_TARGET_CACHE.clear()

    status = gui_server._probe_jlink_target_status("123456", force=True)

    assert status["target_state"] == "reachable"
    assert status["target_backend"] == "OpenOCD / libjaylink"
    commander_probe.assert_not_called()


def test_target_probe_cache_age_starts_after_slow_probe(monkeypatch) -> None:
    ticks = iter((10.0, 10.0, 25.0))
    monkeypatch.setattr(gui_server.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(gui_server, "_openocd_jlink_available", lambda: True)
    monkeypatch.setattr(
        gui_server, "_openocd_target_probe",
        lambda _serial: (True, "SENSUS_INFO_PART=0x00052833"),
    )
    with gui_server.JLINK_TARGET_CACHE_LOCK:
        gui_server.JLINK_TARGET_CACHE.clear()

    status = gui_server._probe_jlink_target_status("123456", force=True)

    assert status["target_checked_at"] == 25.0


def test_commander_probe_access_keeps_optional_winusb_action(
    tmp_path: Path, monkeypatch,
) -> None:
    helper = tmp_path / "wdi-simple.exe"
    helper.touch()
    commander = tmp_path / "JLink.exe"
    commander.touch()
    monkeypatch.setattr(gui_server, "_IS_WIN", True)
    monkeypatch.setattr(gui_server, "WINUSB_HELPER", helper)
    monkeypatch.setattr(gui_server, "JLINK_EXE", commander)
    monkeypatch.setattr(gui_server, "_openocd_jlink_available", lambda: True)
    monkeypatch.setattr(
        gui_server, "_openocd_target_probe",
        lambda _serial: (False, "LIBUSB_ERROR_NOT_FOUND\nNo J-Link device found"),
    )
    monkeypatch.setattr(
        gui_server, "probe_jlink_target",
        lambda *_args, **_kwargs: (
            False, "Connecting to J-Link via USB...O.K.\nHardware version: 1.00",
        ),
    )
    monkeypatch.setattr(gui_server, "_jlink_requires_winusb", lambda _serial: True)
    with gui_server.JLINK_TARGET_CACHE_LOCK:
        gui_server.JLINK_TARGET_CACHE.clear()

    status = gui_server._probe_jlink_target_status("123456", force=True)

    assert status["driver_state"] == "ready"
    assert status["target_failure"] == "target_unreachable"
    assert status["driver_action"] == "install_winusb"


def test_commander_fallback_can_recover_openocd_usb_timeout(
    tmp_path: Path, monkeypatch,
) -> None:
    commander = tmp_path / "JLink.exe"
    commander.touch()
    monkeypatch.setattr(gui_server, "JLINK_EXE", commander)
    monkeypatch.setattr(gui_server, "_openocd_jlink_available", lambda: True)
    monkeypatch.setattr(
        gui_server, "_openocd_target_probe",
        lambda _serial: (False, "LIBUSB_ERROR_TIMEOUT"),
    )
    commander_probe = Mock(return_value=(True, "10000100 = 00052833"))
    monkeypatch.setattr(gui_server, "probe_jlink_target", commander_probe)
    with gui_server.JLINK_TARGET_CACHE_LOCK:
        gui_server.JLINK_TARGET_CACHE.clear()

    status = gui_server._probe_jlink_target_status("123456", force=True)

    assert status["target_state"] == "reachable"
    assert status["target_backend"] == "SEGGER J-Link Commander"
    commander_probe.assert_called_once()


def test_commander_fallback_can_recover_openocd_access_error(
    tmp_path: Path, monkeypatch,
) -> None:
    commander = tmp_path / "JLink.exe"
    commander.touch()
    monkeypatch.setattr(gui_server, "JLINK_EXE", commander)
    monkeypatch.setattr(gui_server, "_openocd_jlink_available", lambda: True)
    monkeypatch.setattr(
        gui_server, "_openocd_target_probe",
        lambda _serial: (False, "LIBUSB_ERROR_ACCESS"),
    )
    commander_probe = Mock(return_value=(True, "10000100 = 00052833"))
    monkeypatch.setattr(gui_server, "probe_jlink_target", commander_probe)
    with gui_server.JLINK_TARGET_CACHE_LOCK:
        gui_server.JLINK_TARGET_CACHE.clear()

    status = gui_server._probe_jlink_target_status("123456", force=True)

    assert status["target_state"] == "reachable"
    assert status["target_backend"] == "SEGGER J-Link Commander"
    commander_probe.assert_called_once()


def test_openocd_access_error_offers_winusb_when_pnp_binding_is_not_ready(
    tmp_path: Path, monkeypatch,
) -> None:
    helper = tmp_path / "wdi-simple.exe"
    helper.touch()
    monkeypatch.setattr(gui_server, "_IS_WIN", True)
    monkeypatch.setattr(gui_server, "WINUSB_HELPER", helper)
    monkeypatch.setattr(gui_server, "JLINK_EXE", tmp_path / "missing-jlink")
    monkeypatch.setattr(gui_server, "_openocd_jlink_available", lambda: True)
    monkeypatch.setattr(
        gui_server, "_openocd_target_probe",
        lambda _serial: (False, "LIBUSB_ERROR_ACCESS"),
    )
    monkeypatch.setattr(gui_server, "_jlink_requires_winusb", lambda _serial: True)
    with gui_server.JLINK_TARGET_CACHE_LOCK:
        gui_server.JLINK_TARGET_CACHE.clear()

    status = gui_server._probe_jlink_target_status("123456", force=True)

    assert status["target_failure"] == "driver_missing"
    assert status["driver_action"] == "install_winusb"


def test_jlink_probe_timeout_is_not_reported_as_a_target_wiring_fault(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.setattr(gui_server, "JLINK_EXE", tmp_path / "missing-jlink")
    monkeypatch.setattr(
        gui_server,
        "_openocd_target_probe",
        lambda _serial: (
            False,
            "LIBUSB_ERROR_TIMEOUT\njaylink_get_firmware_version() failed",
        ),
    )
    monkeypatch.setattr(gui_server, "_jlink_requires_winusb", lambda _serial: False)
    with gui_server.JLINK_TARGET_CACHE_LOCK:
        gui_server.JLINK_TARGET_CACHE.clear()

    status = gui_server._probe_jlink_target_status("123456", force=True)

    assert status["target_state"] == "unreachable"
    assert status["target_failure"] == "probe_communication"
    assert "J-Link USB 通信超时" in status["target_detail"]
    assert "SWD 排线" not in status["target_detail"]


def test_jlink_pnp_confirmation_uses_the_discovered_vid_pid(monkeypatch) -> None:
    info = _port(
        "COM11", description="J-Link", manufacturer="SEGGER",
        serial_number="000029734569", vid=0x1366, pid=0x0105,
    )
    interface = windows_jlink.JLinkUsbInterface(
        0x1366, 0x0105, 0x02,
        r"USB\VID_1366&PID_0105&MI_02\7&ABC&0&0002",
    )
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(gui_server, "_all_serial_port_infos", lambda: [info])
    monkeypatch.setattr(
        gui_server,
        "jlink_bindings",
        lambda vid, pid: calls.append((vid, pid)) or [
            _binding(interface, probe_serial="29734569")
        ],
    )

    assert gui_server._jlink_requires_winusb("29734569") is True
    assert calls == [(0x1366, 0x0105)]


def test_driver_preparation_refuses_multiple_connected_jlinks(
    tmp_path: Path, monkeypatch,
) -> None:
    selected = _port(
        "COM11", description="J-Link", manufacturer="SEGGER",
        serial_number="000029734569", vid=0x1366, pid=0x0105,
    )
    helper = tmp_path / "wdi-simple.exe"
    helper.write_bytes(b"helper")
    monkeypatch.setattr(gui_server, "_IS_WIN", True)
    monkeypatch.setattr(gui_server, "WINUSB_HELPER", helper)
    monkeypatch.setattr(gui_server, "_find_jlink_for_id", lambda _id: selected)
    first = windows_jlink.JLinkUsbInterface(
        0x1366, 0x0105, 0x02,
        r"USB\VID_1366&PID_0105&MI_02\7&ABC&0&0002",
    )
    second = windows_jlink.JLinkUsbInterface(
        0x1366, 0x0105, 0x02,
        r"USB\VID_1366&PID_0105&MI_02\8&DEF&0&0002",
    )
    monkeypatch.setattr(gui_server, "jlink_bindings", lambda _vid, _pid: [
        _binding(first, probe_serial="29734569"),
        _binding(second, probe_serial="12345678"),
    ])
    monkeypatch.setattr(gui_server, "DEVICE_DISCOVERY_CACHE", [])

    with pytest.raises(RuntimeError, match="只保留目标探头"):
        gui_server._prepare_jlink_winusb("jlink:29734569")


def test_driver_preparation_reaches_uac_without_reprobing_openocd(
    tmp_path: Path, monkeypatch,
) -> None:
    helper = tmp_path / "wdi-simple.exe"
    helper.write_bytes(b"helper")
    device = {
        "id": "jlink:123456", "kind": "jlink", "probe_serial": "123456",
        "serial_number": "000000123456", "vid": 0x1366, "pid": 0x0101,
        "name": "J-Link · SN 123456", "selectable": True,
    }
    interface = windows_jlink.JLinkUsbInterface(
        0x1366, 0x0101, None,
        r"USB\VID_1366&PID_0101\000000123456",
    )
    order: list[str] = []
    initial = _binding(interface, probe_serial="123456", ready=False)
    ready = _binding(interface, probe_serial="123456", ready=True)
    pnp_results = iter(([initial], [], [ready], [ready]))
    monkeypatch.setattr(gui_server, "_IS_WIN", True)
    monkeypatch.setattr(gui_server, "WINUSB_HELPER", helper)
    monkeypatch.setattr(gui_server, "DEVICE_DISCOVERY_CACHE", [device])
    monkeypatch.setattr(
        gui_server, "jlink_bindings",
        lambda _vid, _pid: order.append("pnp") or next(pnp_results),
    )
    monkeypatch.setattr(
        gui_server, "install_winusb_driver",
        lambda *_args, **_kwargs: order.append("install") or {"installed": [{}]},
    )
    monkeypatch.setattr(
        gui_server, "_probe_jlink_target_status",
        lambda *_args, **_kwargs: order.append("probe") or {
            "target_state": "reachable", "target_failure": "",
            "driver_state": "ready",
        },
    )
    monkeypatch.setattr(gui_server, "_start_device_discovery", lambda: True)
    monkeypatch.setattr(gui_server.time, "sleep", lambda _seconds: None)

    result = gui_server._prepare_jlink_winusb("jlink:123456")

    assert order == ["pnp", "install", "pnp", "pnp", "pnp", "probe"]
    assert result["message"] == "J-Link 已准备并连上 nRF52833"


def test_driver_preparation_never_treats_absent_pnp_device_as_ready(
    tmp_path: Path, monkeypatch,
) -> None:
    helper = tmp_path / "wdi-simple.exe"
    helper.write_bytes(b"helper")
    device = {
        "id": "jlink:123456", "kind": "jlink", "probe_serial": "123456",
        "serial_number": "000000123456", "vid": 0x1366, "pid": 0x0101,
    }
    interface = windows_jlink.JLinkUsbInterface(
        0x1366, 0x0101, None,
        r"USB\VID_1366&PID_0101\000000123456",
    )
    calls = 0

    def bindings(_vid, _pid):
        nonlocal calls
        calls += 1
        return [_binding(interface, probe_serial="123456")] if calls == 1 else []

    monkeypatch.setattr(gui_server, "_IS_WIN", True)
    monkeypatch.setattr(gui_server, "WINUSB_HELPER", helper)
    monkeypatch.setattr(gui_server, "DEVICE_DISCOVERY_CACHE", [device])
    monkeypatch.setattr(gui_server, "jlink_bindings", bindings)
    monkeypatch.setattr(
        gui_server, "install_winusb_driver", lambda *_args, **_kwargs: {},
    )
    probe = Mock()
    monkeypatch.setattr(gui_server, "_probe_jlink_target_status", probe)
    monkeypatch.setattr(gui_server.time, "sleep", lambda _seconds: None)

    with pytest.raises(RuntimeError, match="未确认调试接口稳定恢复"):
        gui_server._prepare_jlink_winusb("jlink:123456")

    probe.assert_not_called()


def test_driver_task_is_idempotent_and_survives_request_completion(monkeypatch) -> None:
    started = threading.Event()
    release = threading.Event()
    calls: list[str] = []

    def prepare(device_id: str, **_kwargs) -> dict[str, str]:
        calls.append(device_id)
        started.set()
        assert release.wait(2)
        return {"message": "ready"}

    app = SimpleNamespace(
        operation_lock=threading.RLock(),
        hardware_idle=lambda: True,
    )
    monkeypatch.setattr(gui_server, "APP", app)
    monkeypatch.setattr(gui_server, "JLINK_DRIVER_INSTALL_LOCK", threading.Lock())
    monkeypatch.setattr(gui_server, "DEVICE_DISCOVERY_CANCEL", threading.Event())
    monkeypatch.setattr(gui_server, "JLINK_DRIVER_TASK", {
        "state": "idle", "device_id": "", "message": "", "error": "",
        "diagnostic_id": "", "started_at": None, "finished_at": None,
    })
    monkeypatch.setattr(gui_server, "_prepare_jlink_winusb", prepare)
    monkeypatch.setattr(gui_server, "_start_device_discovery", Mock(return_value=True))

    first = gui_server._start_jlink_driver_task("jlink:123456")
    assert first["state"] == "running"
    assert started.wait(1)
    second = gui_server._start_jlink_driver_task("jlink:123456")
    assert second["state"] == "running"
    assert calls == ["jlink:123456"]

    release.set()
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        snapshot = gui_server._jlink_driver_task_snapshot()
        if snapshot["state"] == "succeeded" and not snapshot["running"]:
            break
        time.sleep(0.01)

    assert snapshot["state"] == "succeeded"
    assert snapshot["message"] == "ready"


def test_driver_task_rechecks_shutdown_after_claiming_driver_lock(
    monkeypatch,
) -> None:
    shutdown = threading.Event()

    class RacingLock:
        held = False

        def acquire(self, blocking=True):
            del blocking
            self.held = True
            shutdown.set()
            return True

        def release(self):
            self.held = False

        def locked(self):
            return self.held

    driver_lock = RacingLock()
    app = SimpleNamespace(hardware_idle=lambda: True)
    monkeypatch.setattr(gui_server, "APP", app)
    monkeypatch.setattr(gui_server, "SHUTDOWN_INTENT", shutdown)
    monkeypatch.setattr(gui_server, "JLINK_DRIVER_INSTALL_LOCK", driver_lock)

    with pytest.raises(RuntimeError, match="安全退出"):
        gui_server._start_jlink_driver_task("jlink:123456")

    assert driver_lock.locked() is False


def test_empty_device_cache_returns_immediately_and_starts_one_probe(
    monkeypatch,
) -> None:
    monkeypatch.setattr(gui_server, "DEVICE_DISCOVERY_CACHE", [])
    monkeypatch.setattr(gui_server, "DEVICE_DISCOVERY_AT", 0.0)
    monkeypatch.setattr(gui_server, "DEVICE_DISCOVERY_THREAD", None)
    started: list[bool] = []
    monkeypatch.setattr(
        gui_server, "_start_device_discovery",
        lambda: started.append(True) or True,
    )
    monkeypatch.setattr(
        gui_server, "_discover_devices",
        lambda **_kwargs: pytest.fail("HTTP cache path must not enumerate devices"),
    )

    payload = gui_server._cached_devices_payload()

    assert payload["devices"] == []
    assert payload["probing"] is True
    assert started == [True]
