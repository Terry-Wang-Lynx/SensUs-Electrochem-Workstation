import copy

import pytest

from pa_host.automation import AutomationError
from pa_host.calibrated_motion import _bounds, validate_calibrated_motion
from pa_host.emm_motion import MotionError
import test_calibration_actual_extent as extent
import test_keyboard_responsiveness as responsive
from test_live_motion_bus import LiveMotionSerial


INCIDENT_CAPTURES = {
    "first": {"X": -7232911, "Z": 1930645},
    "working": {"X": -7232909, "Z": 1930645},
    "safe": {"X": -7232909, "Z": 1030585},
    "last": {"X": -697161, "Z": 1969219},
}
INCIDENT_FINAL = {"X": -697163, "Z": 442205}


def set_pose(serial, counts):
    for axis, count in counts.items():
        address = 1 if axis == "X" else 2
        serial.positions[address] = count
        for targets in serial.targets.values():
            targets[address] = count


def incident_session(tmp_path, monkeypatch, pose):
    monkeypatch.setattr(extent, "CAPTURES", copy.deepcopy(INCIDENT_CAPTURES))
    service, bus, serial = extent.actual_session(tmp_path)
    set_pose(serial, pose)
    serial.writes.clear()
    return service, bus, serial


@pytest.mark.parametrize("outside", ["incident_above_safe", "left_of_first", "right_of_last", "below_working"])
def test_finish_contains_fresh_pose_without_expanding_four_point_motion_authority(tmp_path, monkeypatch, outside):
    pose = {"X": -697163, "Z": 1500000}
    if outside == "incident_above_safe":
        pose = dict(INCIDENT_FINAL)
    elif outside == "left_of_first":
        pose["X"] = INCIDENT_CAPTURES["first"]["X"] - 500000
    elif outside == "right_of_last":
        pose["X"] = INCIDENT_CAPTURES["last"]["X"] + 500000
    else:
        pose["Z"] = INCIDENT_CAPTURES["working"]["Z"] + 2000000
    service, bus, serial = incident_session(tmp_path, monkeypatch, pose)
    token = bus.snapshot()["connection_token"]
    points = copy.deepcopy(service.snapshot()["manual"]["points"])
    try:
        result = service.finish_calibration(bus)
        assert result["calibration"]["usable"] is True
        assert result["manual"]["status"] == "complete"
        assert result["calibration"]["points"] == points
        snapshot = bus.snapshot()
        assert snapshot["connection_token"] == token
        assert snapshot["faulted"] is False
        for axis in ("X", "Z"):
            state = snapshot["axes"][axis]
            assert state["raw_count"] == pose[axis]
            assert state["speed_rpm"] == 0
            assert 0 <= state["position_mm"] <= bus.axes[axis]["travel_mm"]
            assert state["position_mm"] == pytest.approx((pose[axis] - state["reference_count"]) / 32768)
        record = result["calibration"]
        bounds = _bounds(record)
        assert bounds["X"][1] - bounds["X"][0] == pytest.approx((INCIDENT_CAPTURES["last"]["X"] - INCIDENT_CAPTURES["first"]["X"]) / 32768)
        assert bounds["Z"][1] - bounds["Z"][0] == pytest.approx((INCIDENT_CAPTURES["working"]["Z"] - INCIDENT_CAPTURES["safe"]["Z"]) / 32768)
        for axis in ("X", "Z"):
            snapshot["axes"][axis]["position_mm"] = sum(bounds[axis]) / 2
        compiled = {"experiment": {"schema_version": "1.1", "run_policy": "guided", "calibration_id": record["id"],
                                    "bindings": {"motion": copy.deepcopy(record["motion"])}}, "steps": []}
        assert validate_calibrated_motion(compiled, snapshot, record) == record
        for axis in ("X", "Z"):
            for target in (bounds[axis][0] - 0.1, bounds[axis][1] + 0.1):
                compiled["steps"] = [{"op": "move_" + axis.lower(), "resolved_params": {"axis": axis, "target_mm": target}}]
                with pytest.raises(AutomationError, match="window"):
                    validate_calibrated_motion(compiled, snapshot, record)
        assert not any(frame[1] in (0xF3, 0xFD, 0xF6, 0x0A, 0x9A) for frame in serial.writes)
    finally:
        if service._alignment is not None:
            service._alignment.close(stop=False)


def test_wrong_geometry_precommit_keeps_same_editable_session_and_points(tmp_path, monkeypatch):
    service, bus, serial = incident_session(tmp_path, monkeypatch, INCIDENT_FINAL)
    set_pose(serial, {"X": -697163, "Z": INCIDENT_CAPTURES["working"]["Z"] + 100})
    service.capture_calibration(bus, "safe")
    before = copy.deepcopy(service.snapshot()["manual"])
    serial.writes.clear()
    try:
        with pytest.raises(AutomationError) as caught:
            service.finish_calibration(bus)
        assert caught.value.code == "calibration_height"
        after = service.snapshot()["manual"]
        assert after["session_token"] == before["session_token"]
        assert after["points"] == before["points"]
        assert after["active"] is True and after["status"] == "aligning"
        assert bus.snapshot()["faulted"] is False
        assert not any(frame[1] in (0xF3, 0xFD, 0xF6, 0x0A, 0x9A) for frame in serial.writes)
    finally:
        service.freeze_alignment()
        service._alignment.close(stop=False)


def finished_ingress_session(tmp_path, monkeypatch):
    class IngressSerial(LiveMotionSerial):
        def __init__(self, **options):
            super().__init__(**options)
            self.statuses = {1: 3, 2: 3}
    monkeypatch.setattr(responsive, "SummarySerial", IngressSerial)
    service, bus, serial = incident_session(tmp_path, monkeypatch, INCIDENT_FINAL)
    result = service.finish_calibration(bus)
    return service, bus, serial, result["calibration"]


def test_completed_above_safe_calibration_enters_z_safe_then_allows_x(tmp_path, monkeypatch):
    service, bus, serial, record = finished_ingress_session(tmp_path, monkeypatch)
    service.compile(service.snapshot()["document"], mode="live", motion=bus)
    bus.install_experiment_calibration(service.trusted_calibration(bus))
    serial.writes.clear()
    result = bus.move_to("Z", record["z_safe_mm"], 0.5)
    assert abs(result["axes"]["Z"]["position_mm"] - record["z_safe_mm"]) < 0.005
    assert result.get("startup_approach_pending") is False
    assert serial.collisions == []
    assert {frame[0] for frame in serial.writes if frame[1] == 0xFD} == {2}
    assert not any(frame[1] == 0xF3 for frame in serial.writes)
    bus.move_to("X", record["x_first_mm"], 0.5)
    assert serial.collisions == []
    assert {frame[0] for frame in serial.writes if frame[1] == 0xFD} == {1, 2}


@pytest.mark.parametrize("damage", ["x_first", "wrong_z", "stop", "z_drift", "held_x_drift"])
def test_startup_ingress_permission_is_narrow_and_revocable(tmp_path, monkeypatch, damage):
    service, bus, serial, record = finished_ingress_session(tmp_path, monkeypatch)
    bus.install_experiment_calibration(service.trusted_calibration(bus))
    serial.writes.clear()
    axis, target = "Z", record["z_safe_mm"]
    if damage == "x_first":
        axis, target = "X", record["x_first_mm"]
    elif damage == "wrong_z":
        target = record["z_working_mm"]
    elif damage == "stop":
        assert bus.stop_all()["stop_confirmed"] is True
    elif damage == "z_drift":
        set_pose(serial, {"Z": INCIDENT_FINAL["Z"] - 1000})
    else:
        set_pose(serial, {"X": INCIDENT_FINAL["X"] + 1000})
    with pytest.raises(MotionError):
        bus.move_to(axis, target, 0.5)
    assert not any(frame[1] in (0xFD, 0xF3) for frame in serial.writes)


def test_actual_controller_enters_from_completion_pose_before_running_column(tmp_path, monkeypatch):
    from pa_host.automation import AutomationController, compile_experiment
    from test_guided_runner import wait_terminal

    service, bus, serial, record = finished_ingress_session(tmp_path / "calibration", monkeypatch)
    document = copy.deepcopy(service.snapshot()["document"])
    document["defaults"]["settle_s"] = 0
    document["safety"].update(motion_policy_confirmed=True, electrical_policy_confirmed=True)
    experiment = service.compile(document, mode="live", motion=bus)["experiment"]
    experiment["program"] = experiment["program"][:4]
    experiment["program"] = [{"op": "wait", "seconds": 0.02} if action["op"] == "dwell" else action
                             for action in experiment["program"]]
    compiled = compile_experiment(experiment)
    assert compiled["steps"][0]["op"] == "move_z"
    assert compiled["steps"][0]["resolved_params"]["purpose"] == "safe_before_x"
    controller = AutomationController(tmp_path / "state", tmp_path / "runs", tmp_path)
    controller._motion = bus
    controller._motion_config = copy.deepcopy(record["motion"])
    controller._motion_calibration_validator = lambda planned: service.trusted_calibration(bus)
    serial.writes.clear()
    controller.start(experiment, "live", compiled["plan_hash"], "completed-pose-ingress")
    controller._thread.join(10)
    result = wait_terminal(controller)
    assert result["state"] == "completed", result
    assert result["stop_confirmed"] is True
    commands = [frame for frame in serial.writes if frame[1] == 0xFD]
    assert commands[0][0] == 2
    assert any(frame[0] == 1 for frame in commands[1:])
    assert serial.collisions == []
    assert not any(frame[1] in (0xF3, 0xF6, 0x0A, 0x9A) for frame in serial.writes)


def ingress_plan(record):
    return {"experiment": {"schema_version": "1.1", "run_policy": "guided", "calibration_id": record["id"],
                            "bindings": {"motion": copy.deepcopy(record["motion"])}},
            "steps": [{"op": "move_z", "resolved_params": {"axis": "Z", "target_mm": record["z_safe_mm"],
                                                              "purpose": "safe_before_x"}}]}


@pytest.mark.parametrize("damage", ["wait_prefix", "acquire_prefix", "purpose", "target"])
def test_ingress_plan_requires_exact_safe_z_first_step(tmp_path, monkeypatch, damage):
    service, bus, serial, record = finished_ingress_session(tmp_path, monkeypatch)
    plan = ingress_plan(record)
    assert validate_calibrated_motion(plan, bus.snapshot(), record) == record
    if damage == "wait_prefix":
        plan["steps"].insert(0, {"op": "wait", "resolved_params": {"seconds": 0.01}})
    elif damage == "acquire_prefix":
        plan["steps"].insert(0, {"op": "dwell", "resolved_params": {"acquire": ["A"], "seconds": 0.01}})
    elif damage == "purpose":
        plan["steps"][0]["resolved_params"]["purpose"] = "other"
    else:
        plan["steps"][0]["resolved_params"]["target_mm"] += 0.001
    serial.writes.clear()
    with pytest.raises(AutomationError):
        validate_calibrated_motion(plan, bus.snapshot(), record)
    assert serial.writes == []


@pytest.mark.parametrize("damage", ["profile", "age", "connection", "identity"])
def test_initial_ingress_context_requires_fresh_matching_identity(tmp_path, monkeypatch, damage):
    service, bus, serial, record = finished_ingress_session(tmp_path, monkeypatch)
    state = bus.snapshot()
    if damage == "profile":
        state["axes"]["Z"]["config"]["microstep"] = 32
    elif damage == "age":
        state["axes"]["X"]["feedback_age_s"] = 3
    elif damage == "connection":
        state["connection_token"] = "another-power-session"
    else:
        state["axes"]["X"]["hardware_version"] += 1
    serial.writes.clear()
    with pytest.raises(AutomationError):
        validate_calibrated_motion(ingress_plan(record), state, record)
    assert serial.writes == []


def test_checkpoint_preserves_immutable_completion_pose_after_ingress(tmp_path, monkeypatch):
    from pa_host.guided_automation import GuidedAutomation

    service, bus, serial, record = finished_ingress_session(tmp_path, monkeypatch)
    completed = copy.deepcopy(record["completion_raw_counts"])
    origin = copy.deepcopy(record["reference"]["reference_counts"])
    assert completed == INCIDENT_FINAL
    bus.install_experiment_calibration(service.trusted_calibration(bus))
    bus.move_to("Z", record["z_safe_mm"], 0.5)
    service.checkpoint_reference(bus)
    reloaded = GuidedAutomation(service._path.parent, require_four_point=True).snapshot()["calibration"]
    assert reloaded["completion_raw_counts"] == completed
    assert reloaded["reference"]["reference_counts"] == origin
    assert reloaded["reference"]["raw_counts"]["Z"] != completed["Z"]
    assert reloaded["reference"]["raw_counts"]["Z"] == bus.snapshot()["axes"]["Z"]["raw_count"]
    assert reloaded["z_safe_mm"] == record["z_safe_mm"]
    assert reloaded["z_working_mm"] == record["z_working_mm"]
    assert serial.collisions == []


def test_observed_ingress_overshoot_below_safe_stops_both_axes(tmp_path, monkeypatch):
    service, bus, serial, record = finished_ingress_session(tmp_path, monkeypatch)
    bus.install_experiment_calibration(service.trusted_calibration(bus))
    serial.writes.clear()
    serial.after_progress = lambda device: device.positions.__setitem__(2, INCIDENT_CAPTURES["safe"]["Z"] + 1000)
    with pytest.raises(MotionError) as caught:
        bus.move_to("Z", record["z_safe_mm"], 0.5)
    assert caught.value.code == "calibration_motion"
    assert caught.value.stop_result["stop_confirmed"] is True
    assert {frame[0] for frame in serial.writes if frame[1] == 0xFE} == {1, 2}
    assert serial.collisions == []
    assert not any(frame[1] == 0xF3 for frame in serial.writes)


def test_optional_absolute_limits_still_reject_ingress_target(tmp_path, monkeypatch):
    from pa_host.motion_limits import AbsoluteMotionLimits

    service, bus, serial, record = finished_ingress_session(tmp_path / "calibration", monkeypatch)
    limits = AbsoluteMotionLimits(tmp_path / "limits")
    limits.begin(bus)
    for boundary, counts in (("x_left", {"X": 0}), ("x_right", {"X": -8000000}),
                              ("z_up", {"Z": 300000}), ("z_down", {"Z": 700000})):
        set_pose(serial, counts)
        limits.capture(bus, boundary)
    set_pose(serial, INCIDENT_FINAL)
    limits.save(bus, clearance_confirmed=True)
    bus.install_experiment_calibration(service.trusted_calibration(bus))
    serial.writes.clear()
    with pytest.raises(MotionError) as caught:
        bus.move_to("Z", record["z_safe_mm"], 0.5)
    assert "absolute" in caught.value.code
    assert not any(frame[1] in (0xFD, 0xF3) for frame in serial.writes)


def test_restore_reference_after_stop_cannot_revive_ingress_permission(tmp_path, monkeypatch):
    service, bus, serial, record = finished_ingress_session(tmp_path, monkeypatch)
    bus.install_experiment_calibration(service.trusted_calibration(bus))
    identity = bus.reference_identity()
    assert bus.snapshot()["startup_approach_pending"] is True
    assert bus.stop_all()["stop_confirmed"] is True
    bus.restore_reference(identity)
    assert bus.snapshot()["startup_approach_pending"] is False
    serial.writes.clear()
    with pytest.raises(MotionError):
        bus.move_to("Z", record["z_safe_mm"], 0.5)
    assert not any(frame[1] in (0xFD, 0xF3) for frame in serial.writes)
