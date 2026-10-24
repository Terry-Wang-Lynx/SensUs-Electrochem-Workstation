from __future__ import annotations

import json
from unittest.mock import Mock, patch

import pytest

from pa_host import gui_server


def v52_settings(channel=1):
    return {
        **gui_server.SettingsController.DEFAULTS,
        "board_profile": "v52", "protocol": "v52-0.1",
        "analysis_channel": channel,
        "working_electrode_v": 0.4, "potential_v": -0.2,
        "initial_potential_v": -0.2, "fsr_nA": 50,
        "offset_mode": "20pct", "offset_nA": 10,
    }


@pytest.fixture
def ble_globals(monkeypatch, tmp_path):
    device = {
        "id": "ble:test-address", "kind": "ble", "transport": "ble",
        "address": "test-address", "name": "pA-V5.2",
        "board_profile": "v52", "selectable": True, "protocol_verified": True,
    }
    monkeypatch.setattr(gui_server, "HARDWARE_TRANSPORT", "ble")
    monkeypatch.setattr(gui_server, "HARDWARE_TRANSPORT_REQUESTED", "auto")
    monkeypatch.setattr(gui_server, "SELECTED_DEVICE", device)
    monkeypatch.setattr(gui_server, "BLE_DEVICE_CACHE", [])
    monkeypatch.setattr(gui_server, "DEVICE_DISCOVERY_CACHE", [])
    monkeypatch.setattr(gui_server, "SERIAL_DATA_PORT", "")
    monkeypatch.setattr(gui_server, "SERIAL_SMP_PORT", "")
    monkeypatch.setattr(gui_server, "SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.setattr(gui_server, "RUNS_DIR", tmp_path / "runs")
    return device


def test_v52_settings_preserve_analysis_identity():
    settings = gui_server.SettingsController.validate(v52_settings(2))
    assert settings["board_profile"] == "v52"
    assert settings["analysis_channel"] == 2
    assert not gui_server.SettingsController.same_analysis_protocol(settings, v52_settings(1))
    legacy = {key: value for key, value in v52_settings().items()
              if key not in {"board_profile", "protocol", "analysis_channel"}}
    assert not gui_server.SettingsController.same_analysis_protocol(settings, legacy)


def test_display_annotations_do_not_break_saved_v52_calibration_identity():
    settings = v52_settings(1)
    saved_run_settings = {**settings, "native_rate_note": "MAX30134 native samples",
                          "display_metadata": {"label": "CH1"}}
    assert gui_server.SettingsController.same_analysis_protocol(settings, saved_run_settings)
    normalized = gui_server.SettingsController.validate(saved_run_settings)
    assert "native_rate_note" not in normalized
    assert "display_metadata" not in normalized
    assert not gui_server.SettingsController.same_analysis_protocol(normalized, v52_settings(2))


@pytest.mark.parametrize("change", [{"method": "cv"}, {"fsr_nA": 4000},
                                    {"prestep_s": 1}, {"analysis_channel": 0},
                                    {"offset_mode": "9nA"}])
def test_v52_rejects_unsupported_features(change):
    with pytest.raises(ValueError):
        gui_server.SettingsController.validate({**v52_settings(), **change})


def test_ble_selection_never_falls_through_to_jlink(ble_globals):
    gui_server._set_device_selection(ble_globals)
    assert gui_server.HARDWARE_TRANSPORT == "ble"
    assert gui_server._transport_status()["transport_label"] == "Bluetooth / V5.2"
    with patch.object(gui_server, "_ports_for_identity", side_effect=AssertionError("USB probe")):
        gui_server._refresh_usb_transport(reuse_verified_transport=True)


def test_ble_apply_never_builds_or_flashes(ble_globals):
    fake = Mock()
    fake.validate_settings.side_effect = lambda values: values
    fake.period_seconds.return_value = 0.124
    pool = Mock()
    pool.configure.return_value = {"info": {"part": "0x34"}, "status": {"mask": 0}}
    with patch.object(gui_server, "v52_ble", fake, create=True), patch.object(gui_server.APP, "ble_connections", pool):
        controller = gui_server.SettingsController()
        with patch.object(controller, "_run_build", side_effect=AssertionError("build")), \
             patch.object(controller, "_wait_for_runtime_firmware", side_effect=AssertionError("legacy probe")):
            result = controller.apply(v52_settings())
    assert result["applied"]
    assert result["firmware_source"] == "v52-ble"
    pool.configure.assert_called_once()


def test_ble_gate_requires_current_token(ble_globals, tmp_path):
    from pa_host.v52_view import V52RunFiles

    controller = gui_server.MeasurementController()
    controller.settings = v52_settings()
    controller.ble_files = V52RunFiles(tmp_path)
    controller._config_gate = {"state": "checking", "request_id": "current"}
    state = {"run_token": "old", "phase": "running", "config_confirmed": True}
    controller.ble_files.state_path.write_text(json.dumps(state))
    controller._poll_ble_state_locked()
    assert controller._config_gate["state"] == "checking"
    assert not controller._config_gate_event.is_set()
    state["run_token"] = "current"
    controller.ble_files.state_path.write_text(json.dumps(state))
    controller._poll_ble_state_locked()
    assert controller._config_gate["state"] == "matched"
    assert controller._config_gate_event.is_set()


def test_v52_view_keeps_channels_and_units_separate(tmp_path):
    from pa_host.v52_view import V52RunFiles

    files = V52RunFiles(tmp_path)
    files.all_channels_path.write_text(
        "host_unix_s,seq,dev_ms,channel,counts,fa_fw,flags\n"
        "100,0,2000,1,16,1000000,4\n100,1,2000,2,32,-2000000,5\n"
        "100,2,2000,3,48,3000000,6\n100,3,2000,4,64,4000000,4\n"
    )
    files.monitor_path.write_text(
        "host_unix_s,quantity,channel,counts,value,flags,unit\n"
        "100,VDD,0,2000,3000000,0,uV\n100,TEMP,0,5000,25000,0,milliC\n"
    )
    snapshot = files.snapshot()
    assert [entry["latest_current_nA"] for entry in snapshot["channels"]] == [1, -2, 3, 4]
    assert [entry["valid"] for entry in snapshot["channels"]] == [[True], [False], [False], [True]]
    assert snapshot["monitor"]["VDD:0"]["value"] == 3000000
    assert snapshot["monitor"]["TEMP:0"]["unit"] == "milliC"
    assert snapshot["state_path"] == str(tmp_path / "ble-state.json")
    assert snapshot["audit_path"] == str(tmp_path / "audit.jsonl")
    assert files.snapshot()["channels"] == snapshot["channels"]


def test_ble_collector_start_never_launches_legacy_commands(ble_globals):
    controller = gui_server.MeasurementController()
    process = Mock()
    process.poll.return_value = None
    with patch.object(gui_server.subprocess, "Popen", return_value=process) as launch, \
         patch.object(gui_server.threading.Thread, "start"), \
         patch.object(controller, "_send_runtime_gate_commands_locked", side_effect=AssertionError("legacy SET/MEAS")):
        result = controller.start(settings=v52_settings(), verify_runtime_config=True)
    command = launch.call_args.args[0]
    assert "pa_host.collect_ble" in command
    assert "--start-jlink" not in command
    assert "--it-10hz" not in command
    assert result["config_gate"]["state"] == "checking"
    assert controller.ble_files is not None
    assert not controller.cmd_path.exists() or not controller.cmd_path.read_text()
    launch.call_args.kwargs["stdout"].close()


def test_unconfirmed_ble_stop_never_reports_stopped(ble_globals, tmp_path):
    from pa_host.v52_view import V52RunFiles

    controller = gui_server.MeasurementController()
    controller.settings = v52_settings()
    controller.ble_files = V52RunFiles(tmp_path)
    controller.ble_files.state_path.write_text(json.dumps({
        "phase": "error", "stop_confirmed": False, "error": "STOP acknowledgment missing"}))
    controller.process = Mock()
    controller.process.poll.return_value = 3
    controller.process.wait.return_value = 3
    controller.user_stop_requested = True
    controller.state = "running"
    with patch.object(controller, "_notify_complete"):
        controller._watch(Mock())
    assert controller.state == "error"
    assert "STOP" in controller.error


def test_saved_ble_profile_survives_initial_legacy_transport_resolution(ble_globals):
    saved = {**v52_settings(4), "potential_v": -0.3, "initial_potential_v": -0.3}
    gui_server.SETTINGS_PATH.write_text(json.dumps({"settings": saved, "transport": "ble"}))
    controller = gui_server.SettingsController()
    controller.restore_for_transport("rtt")
    assert "board_profile" not in controller.settings
    controller.restore_for_transport("ble")
    assert controller.settings["analysis_channel"] == 4
    assert controller.settings["potential_v"] == -0.3
    assert not controller.applied


def test_switching_device_profiles_keeps_each_session_settings(ble_globals):
    controller = gui_server.SettingsController()
    controller.settings = v52_settings(3)
    controller.restore_for_transport("serial")
    controller.settings["potential_v"] = 0.15
    controller.restore_for_transport("ble")
    assert controller.settings["analysis_channel"] == 3
    controller.restore_for_transport("serial")
    assert controller.settings["potential_v"] == 0.15
