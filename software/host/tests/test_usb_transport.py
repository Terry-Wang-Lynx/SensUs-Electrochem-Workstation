from argparse import Namespace
from pathlib import Path
import subprocess
from unittest.mock import Mock, patch

import pytest

from pa_host import gui_server
from pa_host import it_tool


class _PortInfo:
    def __init__(self, device: str) -> None:
        self.device = device
        self.description = "pA-Converter V5.1"
        self.hwid = "USB VID:PID=2FE3:0100 SER=board LOCATION=1-1"
        self.manufacturer = "ZEPHYR"
        self.product = "pA-Converter V5.1"
        self.serial_number = "board"
        self.vid = 0x2FE3
        self.pid = 0x0100
        self.location = "1-1"


def _jlink_port(device: str = "/dev/cu.usbmodem0000297345691") -> _PortInfo:
    info = _PortInfo(device)
    info.description = "J-Link"
    info.hwid = "USB VID:PID=1366:0105 SER=000029734569 LOCATION=1-1.4"
    info.manufacturer = "SEGGER"
    info.product = "J-Link"
    info.serial_number = "000029734569"
    info.vid = 0x1366
    info.pid = 0x0105
    info.location = "1-1.4"
    return info


def test_transport_auto_prefers_discovered_data_cdc(monkeypatch) -> None:
    monkeypatch.setattr(gui_server, "SERIAL_DATA_PORT", "")
    monkeypatch.setattr(
        gui_server, "_discover_serial_data_port",
        lambda: "/dev/cu.usbmodem1103",
    )

    assert gui_server._resolve_hardware_transport("auto", "") == "serial"
    assert gui_server.SERIAL_DATA_PORT == "/dev/cu.usbmodem1103"


def test_jlink_cdc_is_excluded_from_v51_data_candidates(monkeypatch) -> None:
    jlink = _jlink_port()
    usb = _PortInfo("/dev/cu.usbmodem1103")
    monkeypatch.setattr(gui_server, "_listed_serial_port_infos", lambda: [jlink, usb])

    assert gui_server._serial_port_infos() == [usb]
    assert gui_server._jlink_probe_serial(jlink) == "29734569"


def test_v51_data_candidates_do_not_run_native_jlink_discovery(monkeypatch) -> None:
    from serial.tools import list_ports

    jlink = _jlink_port()
    usb = _PortInfo("/dev/cu.usbmodem1103")
    monkeypatch.setattr(list_ports, "comports", lambda: [jlink, usb])
    monkeypatch.setattr(
        gui_server, "discover_jlink_usb_devices",
        Mock(side_effect=AssertionError("native J-Link discovery must stay out")),
    )

    assert gui_server._serial_port_infos() == [usb]


def test_device_discovery_groups_usb_interfaces_and_lists_jlink(monkeypatch) -> None:
    data = _PortInfo("/dev/cu.usbmodem-data")
    smp = _PortInfo("/dev/cu.usbmodem-smp")
    data.serial_number = smp.serial_number = "B4122550F6C771BD"
    jlink = _jlink_port()
    monkeypatch.setattr(
        gui_server, "_all_serial_port_infos", lambda: [data, smp, jlink]
    )
    monkeypatch.setattr(gui_server, "_probe_serial_data_candidate", lambda port: port == data.device)

    devices = gui_server._discover_devices(probe=True)

    assert [device["kind"] for device in devices] == ["usb", "jlink"]
    usb, probe = devices
    assert usb["selectable"] is True
    assert usb["name"] == "USB 71BD"
    assert usb["data_port"] == data.device
    assert usb["smp_port"] == smp.device
    assert probe["id"] == "jlink:29734569"
    assert probe["probe_serial"] == "29734569"


def test_device_discovery_probes_known_data_before_smp(monkeypatch) -> None:
    smp = _PortInfo("/dev/cu.usbmodem1101")
    data = _PortInfo("/dev/cu.usbmodem1103")
    attempts: list[str] = []
    monkeypatch.setattr(gui_server, "SERIAL_DATA_PORT", data.device)
    monkeypatch.setattr(gui_server, "_all_serial_port_infos", lambda: [smp, data])
    monkeypatch.setattr(
        gui_server, "_probe_serial_data_candidate",
        lambda port: attempts.append(port) is None and port == data.device,
    )

    devices = gui_server._discover_devices(probe=True)

    assert attempts == [data.device]
    assert devices[0]["data_port"] == data.device
    assert devices[0]["smp_port"] == smp.device


def test_manual_usb_refresh_prefers_selected_data_over_first_interface(
    monkeypatch,
) -> None:
    smp = _PortInfo("/dev/cu.usbmodem1101")
    data = _PortInfo("/dev/cu.usbmodem1103")
    identity = gui_server._usb_identity(data)
    attempts: list[str] = []
    diagnostics = Mock()
    monkeypatch.setattr(gui_server, "HARDWARE_TRANSPORT_REQUESTED", "auto")
    monkeypatch.setattr(gui_server, "HARDWARE_TRANSPORT", "serial")
    monkeypatch.setattr(gui_server, "SERIAL_DATA_PORT", data.device)
    monkeypatch.setattr(gui_server, "SERIAL_SMP_PORT", smp.device)
    monkeypatch.setattr(gui_server, "DIAGNOSTICS", diagnostics)
    monkeypatch.setattr(gui_server, "_listed_serial_port_infos", lambda: [smp, data])
    monkeypatch.setattr(
        gui_server, "_probe_serial_data_candidate",
        lambda port: attempts.append(port) is None and port == data.device,
    )
    monkeypatch.setattr(gui_server, "SELECTED_DEVICE", {
        "id": identity,
        "kind": "usb",
        "name": "USB 71BD",
        "data_port": data.device,
        "smp_port": smp.device,
    })

    gui_server._refresh_usb_transport()

    assert attempts == [data.device]
    assert gui_server.SERIAL_DATA_PORT == data.device
    assert gui_server.SERIAL_SMP_PORT == smp.device
    diagnostics.record.assert_called_once_with(
        "info", "device.selection.validated",
        "Selected USB DATA and SMP interfaces validated",
        device_id=identity, device_name="USB 71BD",
        data_port=data.device, smp_port=smp.device,
        attempted_ports=[data.device],
    )


def test_manual_usb_refresh_rediscovers_data_after_reenumeration(monkeypatch) -> None:
    smp = _PortInfo("/dev/cu.usbmodem2101")
    data = _PortInfo("/dev/cu.usbmodem2103")
    identity = gui_server._usb_identity(data)
    attempts: list[str] = []
    monkeypatch.setattr(gui_server, "HARDWARE_TRANSPORT_REQUESTED", "auto")
    monkeypatch.setattr(gui_server, "HARDWARE_TRANSPORT", "serial")
    monkeypatch.setattr(gui_server, "SERIAL_DATA_PORT", "/dev/cu.usbmodem1103")
    monkeypatch.setattr(gui_server, "SERIAL_SMP_PORT", "/dev/cu.usbmodem1101")
    monkeypatch.setattr(gui_server, "DIAGNOSTICS", Mock())
    monkeypatch.setattr(gui_server, "_listed_serial_port_infos", lambda: [smp, data])
    monkeypatch.setattr(
        gui_server, "_probe_serial_data_candidate",
        lambda port: attempts.append(port) is None and port == data.device,
    )
    monkeypatch.setattr(gui_server, "SELECTED_DEVICE", {
        "id": identity,
        "kind": "usb",
        "name": "USB 71BD",
        "data_port": "/dev/cu.usbmodem1103",
        "smp_port": "/dev/cu.usbmodem1101",
    })

    gui_server._refresh_usb_transport()

    assert attempts == [smp.device, data.device]
    assert gui_server.SERIAL_DATA_PORT == data.device
    assert gui_server.SERIAL_SMP_PORT == smp.device


def test_source_usb_upgrade_pins_upload_to_selected_smp(
    monkeypatch, tmp_path: Path,
) -> None:
    image = tmp_path / "app.signed.bin"
    image.write_bytes(b"signed")
    smpmgr = tmp_path / "smpmgr"
    smpmgr.write_text("tool", encoding="utf-8")
    reset = Mock(stdout="reset", stderr="")
    upgraded = Mock(stdout="Upgrade complete.", stderr="")
    monkeypatch.setattr(gui_server.runtime, "is_frozen", lambda: False)
    monkeypatch.setattr(gui_server, "_IS_WIN", False)
    monkeypatch.setattr(gui_server, "SMPMGR_EXE", smpmgr)
    monkeypatch.setattr(gui_server, "SERIAL_SMP_PORT", "/dev/cu.usbmodem1101")
    monkeypatch.setattr(gui_server, "PROJECT_DIR", tmp_path)
    monkeypatch.setattr(gui_server, "DIAGNOSTICS", Mock())
    ready = Mock()
    monkeypatch.setattr(gui_server, "_wait_for_usb_transport_ready", ready)
    monkeypatch.setattr(gui_server, "_usb_physical_snapshot_for_port", lambda _port: {})
    monkeypatch.setattr(
        gui_server.subprocess, "run", Mock(side_effect=[reset, upgraded]),
    )

    gui_server.SettingsController._upgrade_v51_firmware(image)

    calls = gui_server.subprocess.run.call_args_list
    assert calls[0].args[0] == [
        str(smpmgr), "--port", "/dev/cu.usbmodem1101", "--timeout", "5",
        "os", "reset",
    ]
    assert calls[1].args[0] == [
        str(smpmgr), "--port", "/dev/cu.usbmodem1101", "--timeout", "10",
        "upgrade", str(image),
    ]
    ready.assert_called_once_with()


def test_usb_upgrade_wait_retries_transient_reenumeration(monkeypatch) -> None:
    refresh = Mock(side_effect=[RuntimeError("设备已断开"), None])
    monkeypatch.setattr(gui_server, "_refresh_usb_transport", refresh)
    sleep = Mock()
    monkeypatch.setattr(gui_server.time, "sleep", sleep)

    gui_server._wait_for_usb_transport_ready(timeout_s=1)

    assert refresh.call_count == 2
    sleep.assert_called_once_with(0.2)


def test_usb_upgrade_wait_reports_last_transport_error(monkeypatch) -> None:
    monkeypatch.setattr(
        gui_server, "_refresh_usb_transport",
        Mock(side_effect=RuntimeError("DATA CDC 无响应")),
    )
    monotonic = iter((0.0, 1.0))
    monkeypatch.setattr(gui_server.time, "monotonic", lambda: next(monotonic))

    with pytest.raises(RuntimeError, match="应用 DATA CDC 未恢复.*无响应"):
        gui_server._wait_for_usb_transport_ready(timeout_s=0.5)


def test_bootloader_smp_wait_follows_windows_com_renumbering(monkeypatch) -> None:
    from serial.tools import list_ports

    old = _PortInfo("COM9")
    new = _PortInfo("COM12")
    new.product = "MCUboot SMP"
    snapshots = iter(([old], [], [new], [new]))
    monkeypatch.setattr(
        list_ports, "comports", lambda: next(snapshots, [new]),
    )
    monkeypatch.setattr(gui_server.time, "sleep", lambda _seconds: None)

    port = gui_server._wait_for_bootloader_smp_port(
        "COM9",
        {"serial_number": "board", "location": "1-1", "ports": ["COM9"]},
        timeout_s=2,
    )

    assert port == "COM12"


def test_bootloader_wait_does_not_reuse_lingering_application_smp(
    monkeypatch,
) -> None:
    from serial.tools import list_ports

    old_smp = _PortInfo("COM9")
    new_smp = _PortInfo("COM12")
    new_smp.product = "MCUboot SMP"
    snapshots = iter(([old_smp], [old_smp], [new_smp], [new_smp]))
    monkeypatch.setattr(
        list_ports, "comports", lambda: next(snapshots, [new_smp]),
    )
    monkeypatch.setattr(gui_server.time, "sleep", lambda _seconds: None)

    port = gui_server._wait_for_bootloader_smp_port(
        "COM9",
        {
            "serial_number": "board", "location": "1-1",
            "ports": ["COM8", "COM9"],
        },
        timeout_s=2,
    )

    assert port == "COM12"


def test_bootloader_wait_uses_location_when_clone_serials_match(
    monkeypatch,
) -> None:
    from serial.tools import list_ports

    old_smp = _PortInfo("COM9")
    old_smp.serial_number = "cloned-board"
    old_smp.location = "1-1"
    other_board = _PortInfo("COM10")
    other_board.serial_number = "cloned-board"
    other_board.location = "1-2"
    new_smp = _PortInfo("COM12")
    new_smp.serial_number = "cloned-board"
    new_smp.location = "1-1"
    new_smp.product = "MCUboot SMP"
    snapshots = iter((
        [old_smp, other_board],
        [other_board],
        [other_board, new_smp],
        [other_board, new_smp],
    ))
    monkeypatch.setattr(
        list_ports, "comports", lambda: next(snapshots, [other_board, new_smp]),
    )
    monkeypatch.setattr(gui_server.time, "sleep", lambda _seconds: None)

    port = gui_server._wait_for_bootloader_smp_port(
        "COM9",
        {
            "serial_number": "cloned-board",
            "location": "1-1",
            "ports": ["COM9"],
        },
        timeout_s=2,
    )

    assert port == "COM12"


def test_linux_interface_suffixes_still_group_one_usb_board() -> None:
    data = _PortInfo("/dev/ttyACM0")
    smp = _PortInfo("/dev/ttyACM1")
    data.serial_number = smp.serial_number = ""
    data.location = "1-1:1.0"
    smp.location = "1-1:1.1"

    assert gui_server._same_usb_device(data, smp) is True
    assert gui_server._usb_identity(data) == gui_server._usb_identity(smp)


def test_windows_interface_suffixes_still_group_one_usb_board() -> None:
    data = _PortInfo("COM7")
    smp = _PortInfo("COM8")
    data.serial_number = smp.serial_number = ""
    data.location = "1-2:x.0"
    smp.location = "1-2:x.1"

    assert gui_server._same_usb_device(data, smp) is True
    assert gui_server._usb_identity(data) == gui_server._usb_identity(smp)


def test_usb_interfaces_without_shared_identity_are_never_auto_paired() -> None:
    first = _PortInfo("/dev/ttyACM0")
    second = _PortInfo("/dev/ttyACM1")
    first.serial_number = "board-a"
    first.location = ""
    second.serial_number = ""
    second.location = ""

    assert gui_server._same_usb_device(first, second) is False


def test_usb_display_name_falls_back_to_short_data_port() -> None:
    usb = _PortInfo("/dev/cu.usbmodem1103")
    usb.serial_number = ""

    assert gui_server._usb_display_name(usb, data_port=usb.device) == "USB 1103"


def test_device_discovery_keeps_two_usb_boards_and_two_jlinks_separate(monkeypatch) -> None:
    usb1_data = _PortInfo("/dev/cu.usbmodem-board1-data")
    usb1_smp = _PortInfo("/dev/cu.usbmodem-board1-smp")
    usb2_data = _PortInfo("/dev/cu.usbmodem-board2-data")
    usb2_smp = _PortInfo("/dev/cu.usbmodem-board2-smp")
    for info, serial, location in (
        (usb1_data, "board-1", "1-1"), (usb1_smp, "board-1", "1-1"),
        (usb2_data, "board-2", "1-2"), (usb2_smp, "board-2", "1-2"),
    ):
        info.serial_number, info.location = serial, location
    jlink1 = _jlink_port()
    jlink2 = _jlink_port("/dev/cu.usbmodem0000123456791")
    jlink2.serial_number = "000012345679"
    jlink2.location = "1-1.5"
    monkeypatch.setattr(
        gui_server, "_all_serial_port_infos",
        lambda: [usb1_data, usb1_smp, usb2_data, usb2_smp, jlink1, jlink2],
    )
    monkeypatch.setattr(
        gui_server, "_probe_serial_data_candidate",
        lambda port: port in {usb1_data.device, usb2_data.device},
    )

    devices = gui_server._discover_devices(probe=True)

    assert len(devices) == 4
    assert len({device["id"] for device in devices}) == 4
    assert {device["probe_serial"] for device in devices if device["kind"] == "jlink"} == {
        "29734569", "12345679",
    }
    assert {device["serial_number"] for device in devices if device["kind"] == "usb"} == {
        "board-1", "board-2",
    }


def test_transport_status_exposes_selected_backend_label(monkeypatch) -> None:
    monkeypatch.setattr(gui_server, "HARDWARE_TRANSPORT", "serial")
    monkeypatch.setattr(gui_server, "HARDWARE_TRANSPORT_REQUESTED", "auto")

    assert gui_server._transport_status() == {
        "transport": "serial",
        "transport_label": "USB DATA CDC",
        "transport_requested": "auto",
    }


def test_smp_port_is_the_sibling_cdc_of_the_same_usb_device(monkeypatch) -> None:
    data = _PortInfo("/dev/cu.usbmodem1103")
    smp = _PortInfo("/dev/cu.usbmodem1101")
    unrelated = _PortInfo("/dev/cu.usbmodem2201")
    unrelated.serial_number = "other"
    unrelated.location = "2-1"
    monkeypatch.setattr(
        gui_server, "_serial_port_infos", lambda: [data, smp, unrelated]
    )
    monkeypatch.setattr(gui_server, "SERIAL_SMP_PORT", "")

    assert gui_server._discover_serial_smp_port(data.device) == smp.device


def test_auto_transport_refreshes_after_usb_is_inserted(monkeypatch) -> None:
    monkeypatch.setattr(gui_server, "HARDWARE_TRANSPORT", "rtt")
    monkeypatch.setattr(gui_server, "HARDWARE_TRANSPORT_REQUESTED", "auto")
    monkeypatch.setattr(gui_server, "SERIAL_DATA_PORT", "")
    monkeypatch.setattr(gui_server, "SERIAL_SMP_PORT", "")
    monkeypatch.setattr(gui_server, "_discover_devices", lambda **_kwargs: [])
    monkeypatch.setattr(
        gui_server,
        "_discover_serial_data_port",
        lambda *, force=False: "/dev/cu.usbmodem1103",
    )
    monkeypatch.setattr(
        gui_server,
        "_discover_serial_smp_port",
        lambda data_port, *, force=False: "/dev/cu.usbmodem1101",
    )

    gui_server._refresh_usb_transport()

    assert gui_server.HARDWARE_TRANSPORT == "serial"
    assert gui_server.SERIAL_DATA_PORT == "/dev/cu.usbmodem1103"
    assert gui_server.SERIAL_SMP_PORT == "/dev/cu.usbmodem1101"


def test_auto_transport_rejects_multiple_usable_devices(monkeypatch) -> None:
    monkeypatch.setattr(gui_server, "HARDWARE_TRANSPORT", "rtt")
    monkeypatch.setattr(gui_server, "HARDWARE_TRANSPORT_REQUESTED", "auto")
    monkeypatch.setattr(gui_server, "_discover_devices", lambda **_kwargs: [
        {"id": "jlink:1", "kind": "jlink", "selectable": True,
         "probe_serial": "1", "name": "J-Link 1"},
        {"id": "usb:2", "kind": "usb", "selectable": True,
         "data_port": "/dev/cu.usbmodem2", "smp_port": "/dev/cu.usbmodem2-smp",
         "name": "USB 2"},
    ])

    with pytest.raises(RuntimeError, match="多个可用设备"):
        gui_server._refresh_usb_transport()


def test_refresh_rejects_a_stale_serial_path_after_usb_reenumeration(monkeypatch) -> None:
    monkeypatch.setattr(gui_server, "HARDWARE_TRANSPORT", "serial")
    monkeypatch.setattr(gui_server, "HARDWARE_TRANSPORT_REQUESTED", "auto")
    monkeypatch.setattr(gui_server, "SERIAL_DATA_PORT", "/dev/cu.usbmodem1103")
    monkeypatch.setattr(gui_server, "SERIAL_SMP_PORT", "/dev/cu.usbmodem1101")
    monkeypatch.setattr(gui_server, "_discover_devices", lambda **_kwargs: [])
    monkeypatch.setattr(
        gui_server, "_discover_serial_data_port",
        lambda *, force=False: None,
    )

    with pytest.raises(RuntimeError, match="重新插拔 USB"):
        gui_server._refresh_usb_transport()


def test_transport_rtt_does_not_probe_usb(monkeypatch) -> None:
    monkeypatch.setattr(gui_server, "SERIAL_DATA_PORT", "")
    monkeypatch.setattr(
        gui_server, "_discover_serial_data_port",
        lambda: pytest.fail("RTT mode must not probe USB"),
    )

    assert gui_server._resolve_hardware_transport("rtt", "") == "rtt"


def test_serial_transport_requires_a_data_port(monkeypatch) -> None:
    monkeypatch.setattr(gui_server, "SERIAL_DATA_PORT", "")
    monkeypatch.setattr(gui_server, "_discover_serial_data_port", lambda: None)

    with pytest.raises(ValueError, match="DATA CDC"):
        gui_server._resolve_hardware_transport("serial", "")


def test_auto_transport_keeps_gui_available_for_unidentified_usb_cdc(
    monkeypatch,
) -> None:
    monkeypatch.setattr(gui_server, "SERIAL_DATA_PORT", "")
    monkeypatch.setattr(gui_server, "SERIAL_SMP_PORT", "/dev/cu.usbmodem1101")
    monkeypatch.setattr(gui_server, "_discover_serial_data_port", lambda: None)
    monkeypatch.setattr(gui_server, "_serial_port_infos", lambda: [_PortInfo(
        "/dev/cu.usbmodem1101"
    )])

    assert gui_server._resolve_hardware_transport("auto", "") == "rtt"
    assert gui_server.SERIAL_DATA_PORT == ""
    assert gui_server.SERIAL_SMP_PORT == ""


def test_process_tail_omits_openocd_shutdown_banner() -> None:
    assert gui_server._meaningful_process_tail(
        "Error: no J-Link found\nshutdown command invoked\n"
    ) == ["Error: no J-Link found"]


def test_bounded_openocd_probe_requests_shutdown_before_termination(
    monkeypatch,
) -> None:
    sent: list[bytes] = []

    class Control:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def sendall(self, payload: bytes) -> None:
            sent.append(payload)

    class Process:
        returncode = 0

        def __init__(self) -> None:
            self.communications = 0
            self.terminated = False
            self.killed = False

        def communicate(self, timeout=None):
            self.communications += 1
            if self.communications == 1:
                raise subprocess.TimeoutExpired("openocd", timeout)
            return ("stopped", "")

        def terminate(self) -> None:
            self.terminated = True

        def kill(self) -> None:
            self.killed = True

    process = Process()
    monkeypatch.setattr(gui_server, "_free_local_tcp_port", lambda: 19099)
    monkeypatch.setattr(gui_server.subprocess, "Popen", lambda *_a, **_k: process)
    monkeypatch.setattr(
        gui_server.socket, "create_connection", lambda *_a, **_k: Control(),
    )

    with pytest.raises(subprocess.TimeoutExpired):
        gui_server._run_openocd_bounded(["openocd", "-c", "init"], timeout_s=1)

    assert sent == [b"shutdown\n"]
    assert process.terminated is False
    assert process.killed is False


def test_it_tool_serial_mode_does_not_start_jlink() -> None:
    child = Mock()
    child.wait.return_value = 0
    args = Namespace(
        out=Path("run.csv"), duration=190.0, idle_timeout=25.0,
        cv=False, serial="/dev/cu.usbmodem-data", start_jlink=True,
        elf=Path("firmware.elf"), probe_serial="29734569",
        reset_before_read=False, socket=None, cmd_file=None, cell_v=None,
        audit=None, raw_log=None, trigger="START",
    )

    with patch.object(it_tool.subprocess, "Popen", return_value=child) as popen:
        assert it_tool._cmd_measure(args) == 0

    command = popen.call_args.args[0]
    assert command[command.index("--serial") + 1] == "/dev/cu.usbmodem-data"
    assert "--start-jlink" not in command
    assert "--probe-serial" not in command
