import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from pa_host import gui_server


@pytest.fixture
def app(monkeypatch):
    automation = Mock()
    automation.is_busy.return_value = False
    automation.reserved_motion_port.return_value = "/dev/cu.motion"
    automation.snapshot.return_value = {"state": "idle", "mode": "simulate"}
    automation._lock = threading.RLock()
    automation._stop_event = threading.Event()
    automation._motion = None
    guided = Mock()
    guided.snapshot.return_value = {"manual": {"active": False, "control_mode": None}}
    guided.prepare_alignment_stop_recovery.return_value = None
    value = SimpleNamespace(
        automation=automation, guided_automation=guided, operation_lock=threading.RLock(),
        measurement=SimpleNamespace(is_busy=Mock(return_value=False)),
        schedule=SimpleNamespace(snapshot=Mock(return_value={"active": False})),
        settings=SimpleNamespace(snapshot=Mock(return_value={"state": "applied", "applied": True})),
    )
    monkeypatch.setattr(gui_server, "APP", value)
    monkeypatch.setattr(gui_server, "APP_UPDATER", SimpleNamespace(busy=False))
    monkeypatch.setattr(gui_server, "JLINK_DRIVER_INSTALL_LOCK", threading.Lock())
    monkeypatch.setattr(gui_server, "SHUTDOWN_INTENT", threading.Event())
    return value


def test_high_throughput_participates_in_global_hardware_ownership(app):
    app.automation.is_busy.return_value = True
    assert not gui_server.AppState.hardware_idle(app)


def test_motion_port_is_not_opened_by_legacy_usb_detection(app, monkeypatch):
    motion = SimpleNamespace(device="/dev/cu.motion", description="USB Serial")
    board = SimpleNamespace(device="/dev/cu.board", description="USB Serial")
    monkeypatch.setattr(gui_server, "_listed_serial_port_infos", lambda: [motion, board])
    monkeypatch.setattr(gui_server, "discover_jlink_usb_devices", lambda: [])
    assert [item.device for item in gui_server._all_serial_port_infos()] == ["/dev/cu.board"]
    assert [item.device for item in gui_server._serial_port_infos()] == ["/dev/cu.board"]
    assert gui_server._probe_serial_data_candidate("/dev/cu.motion") is False


def test_automation_stop_remains_available_while_other_work_is_busy(app):
    app.measurement.is_busy.return_value = True
    app.automation.stop.return_value = {"state": "stopping"}
    assert gui_server._automation_dispatch("POST", "stop", {}, {}) == {"state": "stopping"}
    app.automation.stop.assert_called_once_with()
    assert app.automation._stop_event.is_set()
    app.guided_automation.prepare_alignment_stop_recovery.assert_called_once_with(None)
    app.guided_automation.cancel_calibration.assert_not_called()
    app.automation._event.assert_not_called()


def test_automation_run_cannot_overlap_single_board_acquisition(app):
    app.measurement.is_busy.return_value = True
    with pytest.raises(Exception) as caught:
        gui_server._automation_dispatch("POST", "run", {"plan_hash": "abc", "idempotency_key": "key"}, {})
    assert getattr(caught.value, "code", "") == "hardware_busy"
    app.automation.start.assert_not_called()


@pytest.mark.parametrize("missing", ["plan_hash", "idempotency_key"])
def test_agent_run_requires_explicit_hash_and_idempotency(app, missing):
    body = {"experiment": {}, "mode": "simulate", "plan_hash": "hash", "idempotency_key": "key"}
    del body[missing]
    with pytest.raises(Exception) as caught:
        gui_server._automation_dispatch("POST", "run", body, {})
    assert getattr(caught.value, "path", "") == missing
    app.automation.start.assert_not_called()


def test_normal_hardware_start_blocked_during_high_throughput(app):
    app.automation.is_busy.return_value = True
    handler = gui_server.RequestHandler.__new__(gui_server.RequestHandler)
    handler.path = "/api/measurement/start"
    handler._body = lambda: {}
    handler._send_json = Mock()
    handler._do_POST()
    assert handler._send_json.call_args.args[1] == 409
    assert "高通量" in handler._send_json.call_args.args[0]["error"]


def test_queued_normal_start_rechecks_automation_after_operation_lock(app):
    class RacingLock:
        def __enter__(self):
            app.automation.is_busy.return_value = True

        def __exit__(self, *args):
            return False

    app.operation_lock = RacingLock()
    app.start_measurement = Mock()
    handler = gui_server.RequestHandler.__new__(gui_server.RequestHandler)
    handler.path = "/api/measurement/start"
    handler._body = lambda: {"sample_name": "queued"}
    handler._send_json = Mock()
    handler._do_POST()
    app.start_measurement.assert_not_called()
    assert handler._send_json.call_args.args[1] == 409


@pytest.mark.parametrize("failure,status,code", [
    (ValueError("Invalid JSON body"), 400, "invalid_request"),
    (OSError("Storage unavailable"), 500, "internal_error"),
])
def test_automation_http_errors_keep_machine_readable_envelope(app, failure, status, code):
    handler = gui_server.RequestHandler.__new__(gui_server.RequestHandler)
    handler.path = "/api/automation/v1/run"
    handler._request_id = "test-request"
    handler._body = Mock(side_effect=failure)
    handler._send_json = Mock()
    handler._do_POST()
    payload, actual_status = handler._send_json.call_args.args
    assert actual_status == status
    assert isinstance(payload["error"], dict)
    assert payload["error"]["code"] == code
    assert payload["request_id"] == "test-request"
    assert "message" in payload["error"] and "path" in payload["error"]
