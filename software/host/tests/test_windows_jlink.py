import base64
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from pa_host import gui_server, windows_jlink


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


@pytest.mark.parametrize(
    "output",
    [
        "LIBUSB_ERROR_NOT_FOUND",
        "libusb_error_not_supported",
        "Error: No J-Link device found",
    ],
)
def test_openocd_missing_driver_markers(output: str) -> None:
    assert windows_jlink.openocd_reports_missing_driver(output) is True


def test_openocd_target_error_is_not_a_missing_driver() -> None:
    assert windows_jlink.openocd_reports_missing_driver(
        "Error: Could not find MEM-AP to control the core"
    ) is False


def test_repairable_interfaces_only_selects_verified_debug_interface(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        windows_jlink,
        "_problem_device_payload",
        lambda _vid, _pid: [
            {"instance_id": r"USB\VID_1366&PID_0105&MI_00\SERIAL"},
            {"instance_id": r"USB\VID_1366&PID_0105&MI_02\SERIAL"},
            {"instance_id": r"USB\VID_1366&PID_0105\SERIAL"},
            {"instance_id": r"USB\VID_1234&PID_0105&MI_02\OTHER"},
        ],
    )

    interfaces = windows_jlink.repairable_interfaces(0x1366, 0x0105)

    assert [(item.pid, item.mi) for item in interfaces] == [(0x0105, 0x02)]


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
    commands: list[tuple[Path, list[str], float]] = []
    monkeypatch.setattr(windows_jlink, "_IS_WIN", True)
    monkeypatch.setattr(
        windows_jlink, "repairable_interfaces", lambda _vid, _pid: [interface]
    )

    def fake_run(executable, arguments, *, timeout_s):
        commands.append((executable, arguments, timeout_s))
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
    executable, arguments, timeout = commands[0]
    assert executable == helper.resolve()
    assert timeout == 91
    assert arguments[arguments.index("--iid") + 1] == "0x02"
    assert arguments[arguments.index("--type") + 1] == "0"
    assert arguments[arguments.index("--log") + 1] == "1"
    assert "--silent" not in arguments


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
        helper, ["--dest", str(tmp_path / "output with spaces")], timeout_s=30,
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
    assert '"' + str(tmp_path / "output with spaces") + '"' in wrapper
    assert result.returncode == 0


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
    monkeypatch.setattr(
        list_ports, "comports", lambda: [bluetooth, motherboard, board, jlink]
    )

    assert gui_server._all_serial_port_infos() == [board, jlink]


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


def test_jlink_pnp_confirmation_uses_the_discovered_vid_pid(monkeypatch) -> None:
    info = _port(
        "COM11", description="J-Link", manufacturer="SEGGER",
        serial_number="000029734569", vid=0x1366, pid=0x0105,
    )
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(gui_server, "_all_serial_port_infos", lambda: [info])
    monkeypatch.setattr(
        gui_server,
        "repairable_interfaces",
        lambda vid, pid: calls.append((vid, pid)) or [object()],
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
    other = _port(
        "COM12", description="J-Link", manufacturer="SEGGER",
        serial_number="000012345678", vid=0x1366, pid=0x0105,
    )
    helper = tmp_path / "wdi-simple.exe"
    helper.write_bytes(b"helper")
    monkeypatch.setattr(gui_server, "_IS_WIN", True)
    monkeypatch.setattr(gui_server, "WINUSB_HELPER", helper)
    monkeypatch.setattr(gui_server, "_find_jlink_for_id", lambda _id: selected)
    monkeypatch.setattr(
        gui_server,
        "_probe_jlink_target_status",
        lambda _serial, force: {"target_state": "unreachable", "driver_state": "missing"},
    )
    monkeypatch.setattr(
        gui_server, "_all_serial_port_infos", lambda: [selected, other]
    )

    with pytest.raises(RuntimeError, match="只保留这一只 J-Link"):
        gui_server._prepare_jlink_winusb("jlink:29734569")
