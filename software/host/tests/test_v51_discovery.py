#!/usr/bin/env python3
"""Pure selection tests for the V5.1 dual-CDC App transport."""

from __future__ import annotations

from types import SimpleNamespace

from pa_host import gui_server
from pa_host.gui_server import _choose_v51_ports


def _port(device: str, *, serial: str = "V51-BOARD",
          product: str = "pA-Converter V5.1") -> SimpleNamespace:
    return SimpleNamespace(
        device=device,
        serial_number=serial,
        product=product,
        description=product,
        vid=0x2FE3,
        pid=0x0100,
    )


def test_status_stream_identifies_data_and_leaves_other_port_for_smp() -> None:
    infos = [_port("/dev/cu.usbmodem1101"), _port("/dev/cu.usbmodem1103")]

    selected = _choose_v51_ports(
        infos, probe=lambda path: path.endswith("1103")
    )

    assert selected["data_port"] == "/dev/cu.usbmodem1103"
    assert selected["smp_port"] == "/dev/cu.usbmodem1101"
    assert selected["mcuboot_port"] == "/dev/cu.usbmodem1101"
    assert selected["discovery"] == "verified_status_stream"


def test_v51_dts_order_matches_macos_usb_interface_layout() -> None:
    infos = [_port("/dev/cu.usbmodem1103"), _port("/dev/cu.usbmodem1101")]

    selected = _choose_v51_ports(infos, probe=lambda _path: False)

    assert selected["data_port"] == "/dev/cu.usbmodem1103"
    assert selected["smp_port"] == "/dev/cu.usbmodem1101"
    assert selected["discovery"] == "verified_usb_layout"
    assert selected["error"] == ""


def test_unverified_platform_keeps_port_order_labeled_as_inference(
    monkeypatch,
) -> None:
    infos = [_port("/dev/ttyACM0"), _port("/dev/ttyACM1")]
    monkeypatch.setattr(gui_server.sys, "platform", "linux")

    selected = _choose_v51_ports(infos, probe=lambda _path: False)

    assert selected["discovery"] == "inferred_port_order"
    assert selected["error"]


def test_mcuboot_descriptor_is_never_treated_as_application_data() -> None:
    infos = [_port(
        "/dev/cu.usbmodem1101", product="pA-Converter V5.1 MCUBOOT"
    )]

    selected = _choose_v51_ports(infos, probe=lambda _path: True)

    assert selected["mcuboot_port"] == "/dev/cu.usbmodem1101"
    assert selected["data_port"] == ""
    assert selected["smp_port"] == ""
    assert selected["discovery"] == "mcuboot"


def test_multiple_physical_boards_are_rejected_as_ambiguous() -> None:
    infos = [
        _port("/dev/cu.usbmodem1101", serial="BOARD-A"),
        _port("/dev/cu.usbmodem1103", serial="BOARD-A"),
        _port("/dev/cu.usbmodem1201", serial="BOARD-B"),
        _port("/dev/cu.usbmodem1203", serial="BOARD-B"),
    ]

    selected = _choose_v51_ports(infos, probe=lambda _path: False)

    assert selected["discovery"] == "ambiguous"
    assert selected["data_port"] == ""
    assert "多块" in selected["error"]
