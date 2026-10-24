import ast
import inspect
import textwrap
from unittest.mock import Mock

import pytest

from pa_host import gui_server
from pa_host.automation import AutomationError
from test_guided_integration import guided_app


@pytest.fixture
def twin_app(guided_app):
    guided_app.digital_twin = Mock()
    guided_app.guided_automation.snapshot.return_value["document"]["bindings"] = {
        "boards": {"A": {"address": "board-a"}, "B": {"address": "board-b"}}}
    guided_app.automation.device_status.return_value = {
        "motion": {"connected": True, "axes": {"X": {"raw_count": 0}, "Z": {"raw_count": -10}}},
        "boards": {}, "limits": {"active": True, "record": {"fixture": "trusted-server-record"}},
    }
    return guided_app


def dispatch(action, payload=None, method="POST"):
    return gui_server._automation_dispatch(method, action, payload or {}, {})


def test_global_device_status_uses_guided_board_bindings_and_no_physical_reads(twin_app):
    result = dispatch("devices", method="GET")
    assert result["motion"]["connected"] is True
    twin_app.automation.device_status.assert_called_once_with(
        board_bindings={"A": {"address": "board-a"}, "B": {"address": "board-b"}})
    twin_app.automation._motion.refresh.assert_not_called()


def test_twin_get_receives_only_cached_motion_and_trusted_limits(twin_app):
    twin_app.digital_twin.snapshot.return_value = {"state": "unregistered", "pose": None}
    assert dispatch("twin", method="GET")["pose"] is None
    status = twin_app.automation.device_status.return_value
    twin_app.digital_twin.snapshot.assert_called_once_with(status["motion"], status["limits"])
    twin_app.automation._motion.refresh.assert_not_called()


@pytest.mark.parametrize("confirmation", [True, False, "true", 1, None])
def test_twin_registration_reads_raw_counts_from_server_not_payload(twin_app, confirmation):
    bounds = {"x_left_mm": -49.5, "x_right_mm": 49.5, "z_up_lift_mm": 35, "z_down_lift_mm": 0}
    dispatch("twin/register", {"cad_bounds": bounds, "confirmed": confirmation})
    status = twin_app.automation.device_status.return_value
    twin_app.digital_twin.register.assert_called_once_with(
        status["motion"], status["limits"], bounds, confirmed=confirmation is True)
    twin_app.automation._idle.assert_called_once_with()
    twin_app.automation._motion.refresh.assert_not_called()


@pytest.mark.parametrize("field", ["raw_counts", "reference_count", "disable_limits", "connection_token"])
def test_twin_registration_rejects_client_counter_and_guard_injection(twin_app, field):
    with pytest.raises(AutomationError) as caught:
        dispatch("twin/register", {"cad_bounds": {}, "confirmed": True, field: 123})
    assert caught.value.code == "invalid_value"
    twin_app.digital_twin.register.assert_not_called()


def test_registration_cannot_race_active_limit_capture(twin_app):
    twin_app.automation._limit_calibration_active = True
    with pytest.raises(AutomationError) as caught:
        dispatch("twin/register", {"cad_bounds": {}, "confirmed": True})
    assert caught.value.code == "limit_calibration_active"
    twin_app.digital_twin.register.assert_not_called()


def test_explicit_motion_refresh_does_not_register_or_enable_axes(twin_app):
    twin_app.automation.refresh_motion.return_value = {"motion": {"connected": True}}
    assert dispatch("motion/refresh")["motion"]["connected"] is True
    twin_app.automation.refresh_motion.assert_called_once_with()
    twin_app.automation.enable_motion.assert_not_called()
    twin_app.automation.reference_motion.assert_not_called()


def test_production_app_enables_existing_bus_feedback_only():
    tree = ast.parse(textwrap.dedent(inspect.getsource(gui_server.AppState.__init__)))
    constructors = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name) and node.func.id == "AutomationController"]
    assert len(constructors) == 1
    interval = next((keyword.value for keyword in constructors[0].keywords
                     if keyword.arg == "motion_feedback_interval_s"), None)
    assert isinstance(interval, ast.Constant) and interval.value == 0.5


@pytest.mark.parametrize("datum", ["left_up", "right_down"])
def test_datum_registration_is_independent_of_complete_motion_limits(twin_app, datum):
    twin_app.automation._limit_calibration_active = True
    twin_app.automation.snapshot.return_value = {"state": "stopped", "stop_confirmed": True,
                                               "recovery_required": False, "busy": True}
    dispatch("twin/register-datum", {"datum": datum, "confirmed": True})
    status = twin_app.automation.device_status.return_value
    twin_app.digital_twin.register_datum.assert_called_once_with(
        status["motion"], status["limits"], confirmed=True, datum=datum)
    twin_app.automation.save_limits.assert_not_called()
    twin_app.automation.cancel_limits.assert_not_called()
    twin_app.automation.reference_motion.assert_not_called()
    twin_app.automation.enable_motion.assert_not_called()


@pytest.mark.parametrize("field", ["raw_counts", "binding", "anchor_counts", "cad_anchor", "disable_limits"])
def test_datum_registration_rejects_client_anchor_injection(twin_app, field):
    with pytest.raises(AutomationError) as caught:
        dispatch("twin/register-datum", {"confirmed": True, field: {"X": 7, "Z": 0}})
    assert caught.value.code == "invalid_value"
    twin_app.digital_twin.register_datum.assert_not_called()


@pytest.mark.parametrize("state", [
    {"state": "stopped", "stop_confirmed": False},
    {"state": "running", "stop_confirmed": True},
    {"state": "stopped", "stop_confirmed": True, "recovery_required": True},
])
def test_datum_registration_does_not_bypass_busy_motion_or_unknown_stop(twin_app, state):
    twin_app.automation._limit_calibration_active = True
    twin_app.automation.snapshot.return_value = state
    with pytest.raises(AutomationError) as caught:
        dispatch("twin/register-datum", {"confirmed": True})
    assert caught.value.code == "busy"
    twin_app.digital_twin.register_datum.assert_not_called()


def test_datum_registration_keeps_manual_axis_and_worker_guards(twin_app):
    twin_app.automation.snapshot.return_value = {"state": "stopped", "stop_confirmed": True}
    twin_app.automation._manual_calibration_active = True
    with pytest.raises(AutomationError, match="Complete other operations"):
        dispatch("twin/register-datum", {"confirmed": True})
    twin_app.automation._manual_calibration_active = False
    twin_app.automation._thread = Mock()
    twin_app.automation._thread.is_alive.return_value = True
    with pytest.raises(AutomationError, match="Complete other operations"):
        dispatch("twin/register-datum", {"confirmed": True})
    twin_app.digital_twin.register_datum.assert_not_called()


@pytest.mark.parametrize("confirmation", [True, False, "true", 1, None])
def test_datum_registration_only_accepts_literal_confirmation(twin_app, confirmation):
    twin_app.automation.snapshot.return_value = {"state": "stopped", "stop_confirmed": True}
    dispatch("twin/register-datum", {"confirmed": confirmation})
    assert twin_app.digital_twin.register_datum.call_args.kwargs == {"confirmed": confirmation is True, "datum": "left_up"}


def test_openapi_keeps_display_datum_separate_from_motion_limits():
    from pa_host.automation import openapi_document

    description = openapi_document()["paths"]["/api/automation/v1/twin/register-datum"]["post"]
    schema = description["requestBody"]["content"]["application/json"]["schema"]
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["confirmed"]
    assert schema["properties"]["confirmed"] == {"const": True}
    assert schema["properties"]["datum"]["enum"] == ["left_up", "right_down"]
