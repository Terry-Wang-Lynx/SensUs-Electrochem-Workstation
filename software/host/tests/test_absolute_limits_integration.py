import ast
import inspect
import textwrap
import time
from unittest.mock import Mock

import pytest

from pa_host import gui_server
from pa_host.automation import AutomationError
from test_guided_integration import guided_app


def dispatch(action, payload=None, method="POST"):
    return gui_server._automation_dispatch(method, action, payload or {}, {})


def test_limit_status_read_does_not_touch_hardware(guided_app):
    guided_app.automation.limits_snapshot.return_value = {"state": "missing", "active": False}
    assert dispatch("limits", method="GET") == {"state": "missing", "active": False}
    guided_app.automation.limits_snapshot.assert_called_once_with()
    guided_app.automation._motion.refresh.assert_not_called()


def test_capabilities_explain_single_calibration_policy(guided_app):
    result = dispatch("capabilities", method="GET")
    assert result["absolute_motion_limits"] == {
        "required_for_live_motion": False,
        "boundaries": ["x_left", "x_right", "z_up", "z_down"],
        "capture_mode": "legacy_optional",
        "hardware_limit_switches_verified": False,
    }
    assert result["experiment_calibration"]["points"] == ["first", "last", "working", "safe"]
    assert result["experiment_calibration"]["independent_extreme_limits_required"] is False


@pytest.mark.parametrize("action,body,method,args", [
    ("begin", {}, "begin_limits", ()),
    ("capture", {"boundary": "x_left"}, "capture_limit", ("x_left",)),
    ("cancel", {}, "cancel_limits", ()),
])
def test_limit_routes_delegate_only_operator_action(guided_app, action, body, method, args):
    handler = getattr(guided_app.automation, method)
    handler.return_value = {"calibrating": action != "cancel"}
    assert dispatch("limits/" + action, body) == handler.return_value
    handler.assert_called_once_with(*args)


@pytest.mark.parametrize("confirmed", [True, False, "true", 1, None])
def test_limit_save_requires_literal_clearance_confirmation(guided_app, confirmed):
    dispatch("limits/save", {"clearance_confirmed": confirmed})
    guided_app.automation.save_limits.assert_called_once_with(clearance_confirmed=confirmed is True)


@pytest.mark.parametrize("route,body", [
    ("limits/begin", {"raw_counts": {"X": 1, "Z": 2}}),
    ("limits/capture", {"boundary": "x_left", "raw_count": 123}),
    ("limits/save", {"clearance_confirmed": True, "bounds": {"X": [-1e10, 1e10]}}),
    ("limits/cancel", {"disable_enforcement": True}),
])
def test_api_cannot_inject_bounds_or_disable_limit_policy(guided_app, route, body):
    with pytest.raises(AutomationError) as caught:
        dispatch(route, body)
    assert caught.value.code == "invalid_value"
    guided_app.automation.begin_limits.assert_not_called()
    guided_app.automation.capture_limit.assert_not_called()
    guided_app.automation.save_limits.assert_not_called()
    guided_app.automation.cancel_limits.assert_not_called()


@pytest.mark.parametrize("action", ["run", "guided/run", "motion/reference", "motion/enable", "guided/calibration/begin"])
def test_active_limit_capture_cannot_be_bypassed_through_other_endpoints(guided_app, action):
    guided_app.automation._limit_calibration_active = True
    with pytest.raises(AutomationError) as caught:
        dispatch(action, {"confirmed": True, "axis": "X", "plan_hash": "hash", "idempotency_key": "key"})
    assert caught.value.code == "limit_calibration_active"
    guided_app.automation.start.assert_not_called()
    guided_app.automation.reference_motion.assert_not_called()
    guided_app.automation.enable_motion.assert_not_called()
    guided_app.guided_automation.begin_calibration.assert_not_called()


def test_stop_remains_available_during_limit_capture(guided_app):
    guided_app.automation._limit_calibration_active = True
    guided_app.automation.stop.return_value = {"state": "stopping"}
    assert dispatch("stop") == {"state": "stopping"}
    guided_app.automation.stop.assert_called_once_with()


def test_limit_capture_cannot_overlap_legacy_measurement(guided_app):
    guided_app.measurement.is_busy = Mock(return_value=True)
    with pytest.raises(AutomationError) as caught:
        dispatch("limits/begin")
    assert caught.value.code == "hardware_busy"
    guided_app.automation.begin_limits.assert_not_called()


def test_production_app_uses_single_four_point_calibration_instead_of_extra_limits():
    tree = ast.parse(textwrap.dedent(inspect.getsource(gui_server.AppState.__init__)))
    constructors = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name) and node.func.id == "AutomationController"]
    assert len(constructors) == 1
    policy = next((keyword.value for keyword in constructors[0].keywords
                   if keyword.arg == "require_absolute_limits"), None)
    assert isinstance(policy, ast.Constant) and policy.value is False
    guided = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
              and isinstance(node.func, ast.Name) and node.func.id == "GuidedAutomation"]
    assert len(guided) == 1
    strict = next(keyword.value for keyword in guided[0].keywords if keyword.arg == "require_four_point")
    assert isinstance(strict, ast.Constant) and strict.value is True


def test_real_service_driver_api_capture_reject_and_run_without_physical_hardware(guided_app, tmp_path):
    from pa_host.automation import AutomationController, compile_experiment, default_experiment
    from pa_host.emm_motion import SharedEmmBus
    from test_emm_motion import FakeSerial

    serial = FakeSerial(timeout=0.2, write_timeout=0.2)

    def motion_factory(port, baud, axes, **options):
        assert options["require_absolute_limits"] is True
        bus = SharedEmmBus(port, baud, axes, serial_factory=lambda **parameters: serial, **options)
        bus._command_gap_s = 0
        return bus

    controller = AutomationController(tmp_path / "state", tmp_path / "runs", tmp_path,
        motion_factory=motion_factory, require_absolute_limits=True)
    guided_app.automation = controller
    document = default_experiment()
    document["bindings"]["motion"]["port"] = "fake-no-hardware"
    document["defaults"].update(x_first_mm=5, column_pitch_mm=1, z_safe_mm=2, z_immersed_mm=4, settle_s=0)
    document["safety"] = {"motion_policy_confirmed": True, "electrical_policy_confirmed": True}
    document["program"] = [{"op": "move_to_column", "column": 1}, {"op": "immerse"}, {"op": "withdraw"}]
    dispatch("motion/connect", {"motion": document["bindings"]["motion"]})
    serial.writes.clear()
    dispatch("limits/begin")
    for boundary, axis, count in (("x_left", 1, 655360), ("x_right", 1, 0),
                                  ("z_up", 2, 0), ("z_down", 2, 655360)):
        serial.positions[axis] = count
        dispatch("limits/capture", {"boundary": boundary})
    result = dispatch("limits/save", {"clearance_confirmed": True})
    assert result["active"] is True
    assert {frame[1] for frame in serial.writes} <= {0x1F, 0x42, 0x1A, 0x36, 0x35, 0x3A}
    serial.positions.update({1: 0, 2: 0})
    dispatch("motion/reference", {"confirmed": True})
    dispatch("motion/enable", {"axis": "X"})
    dispatch("motion/enable", {"axis": "Z"})
    document["defaults"]["x_first_mm"] = 21
    before = sum(frame[1] == 0xFD for frame in serial.writes)
    with pytest.raises(AutomationError) as caught:
        dispatch("run", {"experiment": document, "mode": "live",
            "plan_hash": compile_experiment(document)["plan_hash"], "idempotency_key": "out-of-bounds"})
    assert "absolute" in caught.value.code
    assert sum(frame[1] == 0xFD for frame in serial.writes) == before
    document["defaults"]["x_first_mm"] = 5
    dispatch("run", {"experiment": document, "mode": "live",
        "plan_hash": compile_experiment(document)["plan_hash"], "idempotency_key": "bounded-fake-run"})
    deadline = time.monotonic() + 4
    while controller.is_busy() and time.monotonic() < deadline:
        time.sleep(0.002)
    state = controller.snapshot()
    assert state["state"] == "completed", state
    assert state["stop_confirmed"] is True
    assert dispatch("limits", method="GET")["active"] is True
