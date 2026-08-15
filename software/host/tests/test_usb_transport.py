from argparse import Namespace
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from pa_host import gui_server
from pa_host import it_tool


def test_transport_auto_prefers_discovered_data_cdc(monkeypatch) -> None:
    monkeypatch.setattr(gui_server, "SERIAL_DATA_PORT", "")
    monkeypatch.setattr(
        gui_server, "_discover_serial_data_port",
        lambda: "/dev/cu.usbmodem1103",
    )

    assert gui_server._resolve_hardware_transport("auto", "") == "serial"
    assert gui_server.SERIAL_DATA_PORT == "/dev/cu.usbmodem1103"


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
