import copy
import json
from pathlib import Path

import pytest

from pa_host.automation import AutomationError
from test_digital_twin import cached_inputs


def calibrated_inputs():
    motion, limits = cached_inputs()
    motion["referenced"] = True
    for axis, position in (("X", 10), ("Z", 2)):
        motion["axes"][axis].update(referenced=True, enable_confirmed=True,
                                   reference_count=0, position_mm=position)
    record = {"id": "trusted-four-point", "connection_token": motion["connection_token"],
              "motion": copy.deepcopy(limits["record"]["motion"]), "reference_valid": True,
              "reference": {"profiles": copy.deepcopy(limits["record"]["profiles"]),
                            "reference_counts": {"X": 0, "Z": 0}},
              "points": {point: {} for point in ("first", "last", "working", "safe")},
              "x_first_mm": 10, "x_last_mm": 109, "z_working_mm": 8, "z_safe_mm": 2}
    compiled = {"experiment": {"schema_version": "1.1", "run_policy": "guided",
                                "calibration_id": record["id"],
                                "bindings": {"motion": record["motion"]}},
                "steps": [{"op": "move_x", "resolved_params": {"axis": "X", "target_mm": 109}},
                          {"op": "move_z", "resolved_params": {"axis": "Z", "target_mm": 8}}]}
    return compiled, motion, record


def test_four_points_authorize_their_experiment_window_without_extreme_limits():
    from pa_host.calibrated_motion import validate_calibrated_motion

    compiled, motion, record = calibrated_inputs()
    assert motion["absolute_limits"] is not None
    motion["absolute_limits"] = None
    assert validate_calibrated_motion(compiled, motion, record) == record


@pytest.mark.parametrize("damage", ["missing", "legacy", "reconnect", "profile", "reference", "id", "x", "z"])
def test_four_point_policy_rejects_untrusted_context_and_targets(damage):
    from pa_host.calibrated_motion import validate_calibrated_motion

    compiled, motion, record = calibrated_inputs()
    if damage == "missing":
        record = None
    elif damage == "legacy":
        record["points"].pop("working")
    elif damage == "reconnect":
        motion["connection_token"] = "another-connection"
    elif damage == "profile":
        motion["axes"]["X"]["power_flag_baseline"] = 128
    elif damage == "reference":
        motion["axes"]["Z"]["reference_count"] = 100
    elif damage == "id":
        compiled["experiment"]["calibration_id"] = "client-injected"
    else:
        compiled["steps"][0 if damage == "x" else 1]["resolved_params"]["target_mm"] = 110 if damage == "x" else 9
    with pytest.raises(AutomationError):
        validate_calibrated_motion(compiled, motion, record)


@pytest.mark.parametrize("damage", ["outside", "age", "invalid", "disabled", "nonfinite"])
def test_feedback_guard_continues_during_sampling_and_waits(damage):
    from pa_host.calibrated_motion import check_calibrated_feedback

    compiled, motion, record = calibrated_inputs()
    if damage == "outside":
        motion["axes"]["X"]["position_mm"] = 110
    elif damage == "age":
        motion["axes"]["Z"]["feedback_age_s"] = 3
    elif damage == "invalid":
        motion["axes"]["X"]["telemetry_valid"] = False
    elif damage == "disabled":
        motion["axes"]["X"]["enabled"] = False
    else:
        motion["axes"]["Z"]["position_mm"] = float("nan")
    with pytest.raises(AutomationError):
        check_calibrated_feedback(motion, record)


def test_production_motion_validator_blocks_before_any_position_command(tmp_path):
    from pa_host.automation import compile_experiment
    from test_automation_limits import controller_fixture, ready

    controller, document, serial, _drivers = controller_fixture(tmp_path, strict=False)
    ready(controller)
    def refuse(compiled):
        raise AutomationError("calibration_required", "No trusted four-point calibration")
    controller._motion_calibration_validator = refuse
    before = len(serial.writes)
    with pytest.raises(AutomationError, match="No trusted four-point"):
        controller.start(document, "live", compile_experiment(document)["plan_hash"], "untrusted")
    assert not any(frame[1] == 0xFD for frame in serial.writes[before:])


def test_four_point_feedback_monitor_and_calibration_audit_without_extra_limits(tmp_path):
    from pa_host.automation import compile_experiment
    from test_automation_limits import controller_fixture, ready, wait_state

    controller, document, serial, _drivers = controller_fixture(tmp_path, strict=False)
    ready(controller)
    motion = controller._motion.snapshot()
    document.update(schema_version="1.1", run_policy="guided", calibration_id="four-point")
    document["program"].append({"op": "wait_operator", "message": "Wait"})
    record = {"id": "four-point", "connection_token": motion["connection_token"],
              "reference_valid": True, "motion": copy.deepcopy(document["bindings"]["motion"]),
              "reference": controller._motion.reference_identity(),
              "points": {point: {} for point in ("first", "last", "working", "safe")},
              "x_first_mm": 0, "x_last_mm": 99, "z_safe_mm": 0, "z_working_mm": 6}
    controller._motion_calibration_validator = lambda compiled: copy.deepcopy(record)
    controller._motion.install_experiment_calibration = lambda calibration: None
    controller.start(document, "live", compile_experiment(document)["plan_hash"], "four-point-monitor")
    wait_state(controller, "waiting_operator")
    serial.positions[1] = 4000000
    result = wait_state(controller, "error")
    assert result["stop_confirmed"] is True
    assert result["error"]["code"] == "calibration_motion"
    manifest = json.loads(Path(result["manifest_path"]).read_text())
    assert "motion-calibration.json" in manifest["files"]
    assert not controller.limits_snapshot()["active"]
