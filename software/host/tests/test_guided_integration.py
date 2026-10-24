import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from pa_host import gui_server
from pa_host.automation import AutomationError


@pytest.fixture
def guided_app(monkeypatch):
    controller = Mock()
    controller._lock = threading.RLock()
    controller._manual_calibration_active = False
    controller.is_busy.return_value = False
    controller._motion = Mock()
    service = Mock()
    service.snapshot.return_value = {
        "document": {"schema_version": "guided-1.0"},
        "calibration": None,
        "manual": {"active": False},
    }
    value = SimpleNamespace(
        automation=controller, guided_automation=service, operation_lock=threading.RLock(),
        measurement=SimpleNamespace(is_busy=lambda: False),
        schedule=SimpleNamespace(snapshot=lambda: {"active": False}),
        settings=SimpleNamespace(snapshot=lambda: {"state": "applied"}),
    )
    monkeypatch.setattr(gui_server, "APP", value)
    monkeypatch.setattr(gui_server, "APP_UPDATER", SimpleNamespace(busy=False))
    monkeypatch.setattr(gui_server, "JLINK_DRIVER_INSTALL_LOCK", threading.Lock())
    monkeypatch.setattr(gui_server, "SHUTDOWN_INTENT", threading.Event())
    return value


def dispatch(action, payload=None, method="POST"):
    return gui_server._automation_dispatch(method, action, payload or {}, {})


def test_guided_document_get_has_no_hardware_side_effect(guided_app):
    assert dispatch("guided", method="GET")["calibration"] is None
    guided_app.guided_automation.snapshot.assert_called_once_with()
    guided_app.automation._motion.assert_not_called()


def test_guided_preview_compiles_exact_draft_and_mode(guided_app):
    document = {"schema_version": "guided-1.0", "name": "10微摩尔每升"}
    guided_app.guided_automation.compile.return_value = {"plan_hash": "draft-hash"}
    assert dispatch("guided/preview", {"document": document, "mode": "simulate"}) == {"plan_hash": "draft-hash"}
    guided_app.guided_automation.compile.assert_called_once_with(
        document, mode="simulate", motion=guided_app.automation._motion,
    )
    guided_app.automation.start.assert_not_called()


@pytest.mark.parametrize("missing", ["plan_hash", "idempotency_key"])
def test_guided_run_requires_hash_and_request_identity(guided_app, missing):
    payload = {"document": {}, "plan_hash": "plan", "idempotency_key": "request"}
    del payload[missing]
    with pytest.raises(AutomationError) as caught:
        dispatch("guided/run", payload)
    assert caught.value.path == missing
    guided_app.automation.start.assert_not_called()


def test_guided_run_compiles_trusted_geometry_then_uses_controller_gates(guided_app):
    source = {"schema_version": "guided-1.0"}
    experiment = {"schema_version": "1.1", "calibration_id": "saved-only"}
    guided_app.guided_automation.compile.return_value = {"experiment": experiment, "plan_hash": "verified"}
    guided_app.automation.start.return_value = {"state": "running"}
    result = dispatch("guided/run", {
        "document": source, "mode": "live", "plan_hash": "verified", "idempotency_key": "unique",
    })
    assert result["state"] == "running"
    guided_app.guided_automation.compile.assert_called_once_with(source, mode="live", motion=guided_app.automation._motion)
    guided_app.automation.start.assert_called_once_with(
        experiment, mode="live", plan_hash="verified", idempotency_key="unique",
    )


def test_guided_run_rejects_changed_preview_before_start(guided_app):
    guided_app.guided_automation.compile.return_value = {"experiment": {}, "plan_hash": "new"}
    with pytest.raises(AutomationError) as caught:
        dispatch("guided/run", {"document": {}, "plan_hash": "old", "idempotency_key": "request"})
    assert caught.value.code == "plan_changed"
    guided_app.automation.start.assert_not_called()


def test_live_calibration_error_cannot_fall_back_to_simulator_geometry(guided_app):
    guided_app.guided_automation.compile.side_effect = AutomationError("calibration_required", "请主动标定")
    with pytest.raises(AutomationError) as caught:
        dispatch("guided/run", {"document": {}, "mode": "live", "plan_hash": "hash", "idempotency_key": "key"})
    assert caught.value.code == "calibration_required"
    guided_app.automation.start.assert_not_called()


def test_manual_calibration_reserves_hardware_before_release(guided_app):
    def begin(motion):
        assert guided_app.automation._manual_calibration_active is True
        guided_app.guided_automation.snapshot.return_value["manual"]["active"] = True
        return {"manual": {"active": True}}

    guided_app.guided_automation.begin_calibration.side_effect = begin
    result = dispatch("guided/calibration/begin")
    assert result["manual"]["active"] is True
    guided_app.automation._idle.assert_called_once_with()
    assert guided_app.automation._manual_calibration_active is True


def test_failed_calibration_release_retains_unknown_manual_ownership(guided_app):
    guided_app.guided_automation.begin_calibration.side_effect = AutomationError("release_unconfirmed", "状态未知")
    guided_app.guided_automation.snapshot.return_value["manual"]["active"] = True
    with pytest.raises(AutomationError):
        dispatch("guided/calibration/begin")
    assert guided_app.automation._manual_calibration_active is True


def test_calibration_capture_requires_explicit_entry(guided_app):
    with pytest.raises(AutomationError) as caught:
        dispatch("guided/calibration/capture", {"point": "first"})
    assert caught.value.code == "calibration_inactive"
    guided_app.guided_automation.capture_calibration.assert_not_called()


def test_calibration_finish_releases_manual_lock_only_after_service_confirms(guided_app):
    guided_app.automation._manual_calibration_active = True
    guided_app.guided_automation.finish_calibration.return_value = {"manual": {"active": False}, "calibration": {"id": "saved"}}
    guided_app.guided_automation.snapshot.return_value["manual"]["active"] = False
    assert dispatch("guided/calibration/finish")["calibration"]["id"] == "saved"
    assert guided_app.automation._manual_calibration_active is False


def test_manual_calibration_does_not_allow_legacy_reference_bypass(guided_app):
    guided_app.automation._manual_calibration_active = True
    with pytest.raises(AutomationError) as caught:
        dispatch("motion/reference", {"confirmed": True})
    assert caught.value.code == "calibration_active"
    guided_app.automation.reference_motion.assert_not_called()


def test_pause_available_to_active_automation(guided_app):
    guided_app.automation.pause.return_value = {"pause_requested": True}
    assert dispatch("pause")["pause_requested"] is True
    guided_app.automation.pause.assert_called_once_with()


def test_manual_ownership_participates_in_real_global_controller_lock(guided_app, tmp_path):
    from pa_host.automation import AutomationController

    controller = AutomationController(tmp_path / "state", tmp_path / "runs", tmp_path)
    guided_app.automation = controller
    controller._manual_calibration_active = True
    assert controller.is_busy() is True
    assert gui_server.AppState.hardware_idle(guided_app) is False
    with pytest.raises(AutomationError) as caught:
        dispatch("run", {"experiment": {}, "plan_hash": "ignored", "idempotency_key": "ignored"})
    assert caught.value.code == "calibration_active"


def test_guided_compile_failure_does_not_save_or_start(guided_app):
    guided_app.guided_automation.compile.side_effect = AutomationError("invalid_value", "窗口越界", "columns[0].windows.A")
    with pytest.raises(AutomationError) as caught:
        dispatch("guided/run", {"document": {}, "plan_hash": "hash", "idempotency_key": "key"})
    assert caught.value.path == "columns[0].windows.A"
    guided_app.guided_automation.save.assert_not_called()
    guided_app.automation.start.assert_not_called()


def test_agent_can_request_schema_11_without_changing_legacy_default(guided_app):
    schema = gui_server._automation_dispatch("GET", "schema", {}, {"version": ["1.1"]})
    assert schema["properties"]["schema_version"]["const"] == "1.1"
    assert dispatch("schema", method="GET")["properties"]["schema_version"]["const"] == "1.0"


def test_schema_query_rejects_unsupported_version(guided_app):
    with pytest.raises(AutomationError) as caught:
        gui_server._automation_dispatch("GET", "schema", {}, {"version": ["2.0"]})
    assert caught.value.code == "schema_version"
