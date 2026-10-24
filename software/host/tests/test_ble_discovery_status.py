from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from pa_host import gui_server


@pytest.fixture
def ble_status(monkeypatch):
    monkeypatch.setattr(gui_server, "APP", SimpleNamespace(hardware_idle=lambda: True))
    monkeypatch.setattr(gui_server, "BLE_DEVICE_CACHE", [])
    monkeypatch.setattr(gui_server, "DEVICE_DISCOVERY_CACHE", [])
    monkeypatch.setattr(gui_server, "SELECTED_DEVICE", None)
    monkeypatch.setattr(gui_server, "HARDWARE_TRANSPORT", "ble")
    if hasattr(gui_server, "BLE_SCAN_STATE"):
        monkeypatch.setattr(gui_server, "BLE_SCAN_STATE", {
            "state": "not_scanned", "count": 0, "completed_unix_s": None, "error": "",
        })


def test_read_only_discovery_does_not_claim_radio_on_or_start_scan(ble_status, monkeypatch):
    scanner = Mock()
    monkeypatch.setattr(gui_server.v52_ble, "scan_devices", scanner)
    result = gui_server._devices_payload_from_devices([])
    assert result["transport"] == "ble"
    assert result["ble_scan"]["state"] == "not_scanned"
    assert result["ble_scan"]["count"] == 0
    assert result["ble_scan"]["completed_unix_s"] is None
    scanner.assert_not_called()


def test_explicit_ble_scan_counts_distinct_same_name_boards(ble_status, monkeypatch):
    rows = [{"id": "ble:first", "kind": "ble", "name": "pA-V5.2", "address": "first", "selectable": True},
            {"id": "ble:second", "kind": "ble", "name": "pA-V5.2", "address": "second", "selectable": True}]
    scanner = Mock(return_value=rows)
    monkeypatch.setattr(gui_server.v52_ble, "scan_devices", scanner)
    result = gui_server._scan_ble_devices()
    assert result["ble_scan"]["state"] == "complete"
    assert result["ble_scan"]["count"] == 2
    assert result["ble_scan"]["completed_unix_s"] > 0
    assert [row["address"] for row in result["devices"]] == ["first", "second"]
    scanner.assert_called_once_with(timeout=4.0)


def test_scan_error_remains_visible_without_false_zero_device_success(ble_status, monkeypatch):
    monkeypatch.setattr(gui_server.v52_ble, "scan_devices", Mock(side_effect=RuntimeError("Bluetooth permission denied")))
    with pytest.raises(RuntimeError, match="permission"):
        gui_server._scan_ble_devices()
    result = gui_server._devices_payload_from_devices([])
    assert result["ble_scan"]["state"] == "error"
    assert "permission" in result["ble_scan"]["error"]
    assert result["ble_scan"]["completed_unix_s"] is None
