import copy
import time
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest

from pa_host import gui_server
from pa_host.automation import AutomationController, AutomationError
from pa_host.calibrated_motion import validate_calibrated_motion
from pa_host.emm_motion import MotionError
import test_calibration_actual_extent as extent
import test_keyboard_responsiveness as responsive
from test_calibration_finish_pose import set_pose
from test_emm_motion import signed
from test_guided_integration import guided_app
from test_guided_runner import GuidedFactory, wait_terminal
from test_live_motion_bus import LiveMotionSerial


RUN_CAPTURES = {
    "first": {"X": -7243607, "Z": 2046655},
    "last": {"X": -697163, "Z": 1932537},
    "working": {"X": -697163, "Z": 1932537},
    "safe": {"X": -697163, "Z": 671985},
}
RUN_COMPLETION = {"X": -7243607, "Z": 2046655}


def run_entry_session(tmp_path, monkeypatch, guided_app, *, below_counts=114118, lead_mm=2):
    class RunEntrySerial(LiveMotionSerial):
        def __init__(self, **options):
            super().__init__(**options)
            self.statuses = {1: 3, 2: 3}

    monkeypatch.setattr(extent, "CAPTURES", copy.deepcopy(RUN_CAPTURES))
    monkeypatch.setattr(responsive, "SummarySerial", RunEntrySerial)
    axes = copy.deepcopy(responsive.AXES)
    axes["Z"]["lead_mm"] = lead_mm
    monkeypatch.setattr(responsive, "AXES", axes)
    service, bus, serial = extent.actual_session(tmp_path / "calibration")
    completion = {"X": RUN_COMPLETION["X"], "Z": RUN_CAPTURES["working"]["Z"] + below_counts}
    set_pose(serial, completion)
    record = service.finish_calibration(bus)["calibration"]
    factory = GuidedFactory(time, loss_at=float("inf"))
    controller = AutomationController(tmp_path / "state", tmp_path / "runs", tmp_path,
                                      process_factory=factory, board_stop_factory=lambda *args, **kwargs: True)
    controller._motion = bus
    controller._motion_config = copy.deepcopy(record["motion"])
    controller._motion_calibration_validator = lambda compiled: service.trusted_calibration(bus)
    guided_app.automation, guided_app.guided_automation = controller, service
    document = copy.deepcopy(service.snapshot()["document"])
    document["safety"].update(motion_policy_confirmed=True, electrical_policy_confirmed=True)
    document["defaults"]["settle_s"] = 0
    for slot in "AB":
        document["bindings"]["boards"][slot]["address"] = "board-" + slot.lower()
        document["bindings"]["boards"][slot]["settings"]["sens_period_code"] = 0
    for column in document["columns"]:
        column.update(enabled=column["index"] == 2, duration_s=0.125)
    for electrode in document["electrodes"]:
        electrode["enabled"] = electrode["id"] in "AF"
    serial.writes.clear()
    return service, bus, serial, record, controller, factory, document


@pytest.mark.parametrize("below_counts,lead_mm", [(114118, 2), (32768, 2), (655360, 2), (114118, 1), (114118, 4)])
def test_actual_guided_run_enters_upward_from_completed_below_working_pose(
        tmp_path, monkeypatch, guided_app, below_counts, lead_mm):
    service, bus, serial, record, controller, factory, document = run_entry_session(
        tmp_path, monkeypatch, guided_app, below_counts=below_counts, lead_mm=lead_mm)
    compiled = gui_server._automation_dispatch("POST", "guided/preview", {"document": document, "mode": "live"}, {})
    assert compiled["steps"][0]["op"] == "move_z"
    assert compiled["steps"][0]["resolved_params"]["purpose"] == "safe_before_x"
    assert compiled["steps"][0]["resolved_params"]["target_mm"] == record["z_safe_mm"]
    assert bus.snapshot()["axes"]["Z"]["position_mm"] > record["z_working_mm"]
    acquisition_poses = []
    original_factory = controller._process_factory

    def collect_after_ingress(command, **options):
        pose = bus.snapshot()
        acquisition_poses.append(pose)
        assert pose.get("startup_approach_pending") is False
        assert pose["axes"]["Z"]["position_mm"] == pytest.approx(record["z_working_mm"], abs=0.005)
        return original_factory(command, **options)

    controller._process_factory = collect_after_ingress
    gui_server._automation_dispatch("POST", "guided/run", {
        "document": document, "mode": "live", "plan_hash": compiled["plan_hash"],
        "idempotency_key": "below-working-entry",
    }, {})
    controller._thread.join(10)
    result = wait_terminal(controller)
    assert result["state"] == "completed", result
    assert result["stop_confirmed"] is True
    commands = [frame for frame in serial.writes if frame[1] == 0xFD]
    assert commands[0][0] == 2 and commands[0][2] == 1
    assert any(frame[0] == 1 for frame in commands[1:])
    z_move = next(index for index, frame in enumerate(serial.writes) if frame[1] == 0xFD)
    x_move = next(index for index, frame in enumerate(serial.writes) if frame[:2] == b"\x01\xfd")
    z_quiet = serial.finite_quiet_records[0]
    assert z_quiet["address"] == 2 and z_quiet["quiet_s"] >= 0.2
    assert z_move < x_move
    assert not any(frame[1] == 0xFE for frame in serial.writes[z_move:x_move])
    for address in (1, 2):
        for opcode in (0x1F, 0x42, 0x1A):
            assert any(frame[:2] == bytes([address, opcode]) for frame in serial.writes[z_move + 1:x_move])
    assert acquisition_poses and len(factory.children) == 2
    assert serial.collisions == []
    assert not any(frame[1] in (0xF3, 0xF6, 0x0A, 0x9A) for frame in serial.writes)
    assert service.snapshot()["calibration"]["completion_raw_counts"] == record["completion_raw_counts"]


@pytest.mark.parametrize("damage", ["wait_prefix", "acquire_prefix", "x_first", "wrong_safe", "target_below_working"])
def test_below_working_entry_does_not_relax_plan_targets_or_first_action(
        tmp_path, monkeypatch, guided_app, damage):
    service, bus, serial, record, controller, factory, document = run_entry_session(tmp_path, monkeypatch, guided_app)
    compiled = service.compile(document, mode="live", motion=bus)
    assert validate_calibrated_motion(compiled, bus.snapshot(), record) == record
    if damage == "wait_prefix":
        compiled["steps"].insert(0, {"op": "wait", "resolved_params": {"seconds": 0.01}})
    elif damage == "acquire_prefix":
        compiled["steps"].insert(0, {"op": "dwell", "resolved_params": {"seconds": 0.01, "acquire": ["A"]}})
    elif damage == "x_first":
        compiled["steps"].pop(0)
    elif damage == "wrong_safe":
        compiled["steps"][0]["resolved_params"]["target_mm"] = record["z_working_mm"]
    else:
        next(step for step in compiled["steps"] if step["op"] == "move_z"
             and step["resolved_params"]["purpose"] != "safe_before_x")["resolved_params"]["target_mm"] = record["z_working_mm"] + 0.01
    with pytest.raises(AutomationError):
        validate_calibrated_motion(compiled, bus.snapshot(), record)
    assert not any(frame[1] in (0xFD, 0xF3) for frame in serial.writes)
    assert factory.children == []


def test_incident_guided_run_http_response_accepts_completed_calibration(tmp_path, monkeypatch, guided_app):
    service, bus, serial, record, controller, factory, document = run_entry_session(tmp_path, monkeypatch, guided_app)
    compiled = service.compile(document, mode="live", motion=bus)
    responses = []
    handler = SimpleNamespace(_request_id="incident-run", _send_json=lambda payload, status=200: responses.append((payload, status)))
    gui_server.RequestHandler._automation_response(handler, "POST", urlparse("/api/automation/v1/guided/run"), {
        "document": document, "mode": "live", "plan_hash": compiled["plan_hash"],
        "idempotency_key": "incident-http-entry",
    })
    response, status = responses[0]
    assert status == 200, response
    assert response.get("error") is None, response
    controller._thread.join(10)
    result = wait_terminal(controller)
    assert result["state"] == "completed", result
    assert result["stop_confirmed"] is True
    assert not any(frame[1] in (0xF3, 0xF6, 0x0A, 0x9A) for frame in serial.writes)


def test_below_working_ingress_cannot_move_deeper_than_observed_completion_pose(
        tmp_path, monkeypatch, guided_app):
    service, bus, serial, record, controller, factory, document = run_entry_session(tmp_path, monkeypatch, guided_app)
    compiled = gui_server._automation_dispatch("POST", "guided/preview", {"document": document, "mode": "live"}, {})
    events = []
    bus._callback = events.append
    observed_start = record["completion_raw_counts"]["Z"]
    progressed = []

    def drift_deeper_than_start(device):
        progressed.append(device.active_address)
        device.positions[2] = observed_start + 1000

    serial.after_progress = drift_deeper_than_start
    gui_server._automation_dispatch("POST", "guided/run", {
        "document": document, "mode": "live", "plan_hash": compiled["plan_hash"],
        "idempotency_key": "deeper-than-observed-start",
    }, {})
    result = wait_terminal(controller)
    assert progressed and set(progressed) == {2}
    assert result["state"] == "error", result
    assert result["error"]["cause"]["code"] == "calibration_motion"
    assert result["stop_confirmed"] is True
    commands = [frame for frame in serial.writes if frame[1] == 0xFD]
    assert len(commands) == 1
    assert commands[0][0] == 2 and commands[0][2] == 1
    assert {frame[0] for frame in serial.writes if frame[1] == 0xFE} == {1, 2}
    assert not any(event["kind"] == "arrived" for event in events)
    assert factory.children == []
    assert serial.collisions == []
    assert not any(frame[1] in (0xF3, 0xF6, 0x0A, 0x9A) for frame in serial.writes)


@pytest.mark.parametrize("fault", ["target_drift", "wire"])
@pytest.mark.parametrize("cleanup", ["confirmed", "unknown", "missing_result"])
def test_actual_guided_run_install_failure_reports_cause_and_retains_unknown_stop_ownership(
        tmp_path, monkeypatch, guided_app, fault, cleanup):
    service, bus, serial, record, controller, factory, document = run_entry_session(tmp_path, monkeypatch, guided_app)
    compiled = service.compile(document, mode="live", motion=bus)
    triggered = []

    def fail_install_and_cleanup(device, frame):
        if frame[:2] != b"\x01\x33" or triggered:
            return
        triggered.append(True)
        if cleanup == "unknown":
            device.overrides[1, 0xFE] = b""
        if fault == "target_drift":
            device.targets[0x33][1] += 1000
        else:
            device.overrides[1, 0x33] = bytes([1, 0x33]) + signed(device.positions[1], 4) + b"\x6b\x01\x00\xee\x6b"

    serial.on_request = fail_install_and_cleanup
    if cleanup == "missing_result":
        original_install = bus.install_experiment_calibration

        def discard_cleanup_result(calibration):
            try:
                return original_install(calibration)
            except MotionError as error:
                error.stop_result = None
                raise

        monkeypatch.setattr(bus, "install_experiment_calibration", discard_cleanup_result)
    responses = []
    handler = SimpleNamespace(_request_id="install-failed", _send_json=lambda payload, status=200: responses.append((payload, status)))
    gui_server.RequestHandler._automation_response(handler, "POST", urlparse("/api/automation/v1/guided/run"), {
        "document": document, "mode": "live", "plan_hash": compiled["plan_hash"],
        "idempotency_key": "failed-install",
    })
    response, status = responses[0]
    assert triggered
    assert status == 409, response
    failure = response["error"]
    unknown = cleanup == "unknown"
    assert failure["code"] == ("stop_unconfirmed" if unknown else "calibration_motion"), failure
    if not unknown:
        assert failure["path"] == "calibration"
    assert failure["cause"]["stop_result"]["stop_confirmed"] is (not unknown)
    if fault == "wire":
        assert failure["cause"]["wire"]["opcode"] == 0x33
        assert failure["cause"]["wire"]["pending_hex"] == "0100ee6b"
    else:
        assert "target positions" in failure["cause"]["message"]
    state = controller.snapshot()
    if unknown:
        assert state["state"] == "error"
        assert state["stop_confirmed"] is False
        assert state["execution_error"]["code"] == "calibration_motion"
        assert state["execution_error"]["path"] == "calibration"
        assert state["execution_error"]["cause"] == failure["cause"]
        assert controller._recovery_required is True
        assert controller._owns_motion is True
        assert controller.is_busy() is True
    else:
        assert state["stop_confirmed"] is True
        assert controller._recovery_required is False
        assert controller._owns_motion is False
    if cleanup == "missing_result":
        assert len([frame for frame in serial.writes if frame[:2] == b"\x01\xfe"]) >= 2
    assert {frame[0] for frame in serial.writes if frame[1] == 0xFE} == {1, 2}
    assert not any(frame[1] in (0xFD, 0xF3, 0xF6, 0x0A, 0x9A) for frame in serial.writes)
    assert factory.children == []
