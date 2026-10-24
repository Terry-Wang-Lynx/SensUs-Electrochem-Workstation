from __future__ import annotations

import copy
import hashlib
import json
import threading
import time
from pathlib import Path

import pytest

from pa_host.automation import AutomationController, AutomationError, compile_experiment, default_experiment, openapi_document
from pa_host.emm_motion import SharedEmmBus
from test_emm_motion import FakeSerial, signed
from test_automation import CollectorFactory, FakeCollector
from test_guided_runner import GuidedFactory


def controller_fixture(tmp_path, *, strict=True, board_stop=None):
    serial = FakeSerial(timeout=0.001, write_timeout=0.02)
    serial.positions = {1: 65536, 2: 65536}
    drivers = []

    def factory(port, baud, axes, **kwargs):
        driver = SharedEmmBus(port, baud, axes, serial_factory=lambda **options: serial,
                              transaction_timeout_s=0.02, **kwargs)
        driver._command_gap_s = 0
        drivers.append(driver)
        return driver

    controller = AutomationController(tmp_path / "state", tmp_path / "runs", tmp_path,
                                      motion_factory=factory, require_absolute_limits=strict,
                                      board_stop_factory=board_stop or (lambda address: {"stop_confirmed": True}))
    document = default_experiment()
    document["bindings"]["motion"]["port"] = "offline-fake-bus"
    document["bindings"]["boards"]["A"]["address"] = "board-a"
    document["bindings"]["boards"]["B"]["address"] = "board-b"
    document["safety"] = {"motion_policy_confirmed": True, "electrical_policy_confirmed": True}
    document["program"] = [{"op": "move_to_column", "column": 1}]
    controller.connect_motion(document["bindings"]["motion"])
    return controller, document, serial, drivers


def capture_bounds(controller, serial, *, x_left=6000000, save=True):
    controller.begin_limits()
    for boundary, address, count in (("x_left", 1, x_left), ("x_right", 1, 0),
                                      ("z_up", 2, 0), ("z_down", 2, 3000000)):
        serial.positions[address] = count
        controller.capture_limit(boundary)
    serial.positions = {1: 65536, 2: 65536}
    return controller.save_limits(clearance_confirmed=True) if save else controller.limits_snapshot()


def ready(controller):
    controller.reference_motion(True)
    controller.enable_motion("X")
    controller.enable_motion("Z")


def wait_state(controller, state=None):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        result = controller.snapshot()
        if state is not None and result["state"] == state or state is None and not result["busy"]:
            return result
        time.sleep(0.005)
    return controller.snapshot()


@pytest.mark.parametrize("version", ["1.0", "1.1"])
def test_strict_live_motion_requires_captured_bounds_for_each_schema(tmp_path, version):
    controller, document, serial, _drivers = controller_fixture(tmp_path)
    if version == "1.1":
        document.update(schema_version="1.1", run_policy="guided", calibration_id="saved-calibration")
    ready(controller)
    before = len(serial.writes)
    with pytest.raises(AutomationError) as caught:
        controller.start(document, "live", compile_experiment(document)["plan_hash"], "uncalibrated")
    assert "limit" in caught.value.code
    assert not any(frame[1] == 0xFD for frame in serial.writes[before:])
    assert not (tmp_path / "runs").exists()


def test_limits_workflow_excludes_reference_enable_and_run_without_drive_writes(tmp_path):
    controller, document, serial, _drivers = controller_fixture(tmp_path)
    before = len(serial.writes)
    assert controller.begin_limits()["calibrating"] is True
    assert controller.is_busy()
    for operation in (lambda: controller.reference_motion(True), lambda: controller.enable_motion("X"),
                      lambda: controller.connect_motion(document["bindings"]["motion"]),
                      lambda: controller.start(document, "simulate", compile_experiment(document)["plan_hash"], "busy")):
        with pytest.raises(AutomationError) as caught:
            operation()
        assert caught.value.code == "busy"
    controller.capture_limit("x_left")
    count = len(serial.writes)
    assert controller.limits_snapshot()["calibrating"] is True
    assert len(serial.writes) == count
    assert not any(frame[1] in (0xF3, 0xFD, 0x0A, 0x9A) for frame in serial.writes[before:])
    assert controller.cancel_limits()["active"] is False
    assert not controller.is_busy()


def test_all_targets_checked_before_any_real_motion(tmp_path):
    controller, document, serial, _drivers = controller_fixture(tmp_path)
    capture_bounds(controller, serial, x_left=2000000)
    ready(controller)
    document["program"] += [{"op": "move_to_column", "column": 8}]
    before = len(serial.writes)
    with pytest.raises(AutomationError) as caught:
        controller.start(document, "live", compile_experiment(document)["plan_hash"], "outside-later-target")
    assert "limit" in caught.value.code
    assert not any(frame[1] == 0xFD for frame in serial.writes[before:])
    assert not (tmp_path / "runs").exists()


@pytest.mark.parametrize("field", ["absolute_limits", "require_absolute_limits", "limits"])
def test_experiment_cannot_override_absolute_limits_policy(tmp_path, field):
    controller, document, _serial, _drivers = controller_fixture(tmp_path)
    document[field] = False
    with pytest.raises(AutomationError) as caught:
        controller.start(document, "live", "unused", "bypass")
    assert caught.value.code == "invalid_value"
    assert controller.limits_snapshot()["active"] is False


@pytest.mark.parametrize("wait_action", [{"op": "wait", "seconds": 20}, {"op": "wait_operator", "message": "Operator wait"}])
def test_limit_monitor_stops_uncommanded_axis_during_waits(tmp_path, wait_action):
    stopped_boards = []
    controller, document, serial, _drivers = controller_fixture(
        tmp_path, board_stop=lambda address: stopped_boards.append(address) or {"stop_confirmed": True})
    capture_bounds(controller, serial)
    ready(controller)
    document["program"] += [copy.deepcopy(wait_action), {"op": "dwell", "column": 1, "seconds": 10, "acquire": ["A", "E"]}]
    controller.start(document, "live", compile_experiment(document)["plan_hash"], "drift")
    deadline = time.monotonic() + 2
    while controller.snapshot()["step_index"] < 2 and time.monotonic() < deadline:
        time.sleep(0.005)
    serial.positions[2] = 3000001
    result = wait_state(controller)
    assert result["state"] == "error"
    assert "limit" in result["error"]["code"]
    assert result["stop_confirmed"] is True
    assert set(stopped_boards) == {"board-a", "board-b"}
    assert {frame[0] for frame in serial.writes if frame[1] == 0xFE} == {1, 2}


def test_telemetry_failure_retains_fault_and_unknown_stop_ownership(tmp_path):
    controller, document, serial, _drivers = controller_fixture(tmp_path)
    capture_bounds(controller, serial)
    ready(controller)
    document["program"] += [{"op": "wait_operator", "message": "Wait for operator"}]
    controller.start(document, "live", compile_experiment(document)["plan_hash"], "telemetry")
    wait_state(controller, "waiting_operator")
    serial.overrides[2, 0x36] = b""
    result = wait_state(controller, "error")
    assert result["error"]["code"] == "stop_unconfirmed"
    assert result["execution_error"] is not None
    assert result["stop_confirmed"] is False
    assert result["busy"] and result["recovery_required"]
    serial.overrides.clear()
    controller.stop()
    assert wait_state(controller)["stop_confirmed"] is True


def test_stop_cancels_limit_capture_and_keeps_stop_available(tmp_path):
    controller, _document, serial, _drivers = controller_fixture(tmp_path)
    controller.begin_limits()
    controller.capture_limit("x_left")
    result = controller.stop()
    assert result["state"] in {"stopping", "stopped"}
    result = wait_state(controller)
    assert result["stop_confirmed"] is True
    assert controller.limits_snapshot()["calibrating"] is False
    assert controller.limits_snapshot()["active"] is False
    assert {frame[0] for frame in serial.writes if frame[1] == 0xFE} == {1, 2}
    with pytest.raises(AutomationError):
        controller.save_limits(clearance_confirmed=True)


def test_openapi_exposes_four_boundary_capture_without_numeric_injection():
    paths = openapi_document()["paths"]
    for suffix in ("limits", "limits/begin", "limits/capture", "limits/save", "limits/cancel"):
        assert f"/api/automation/v1/{suffix}" in paths
    capture = paths["/api/automation/v1/limits/capture"]["post"]["requestBody"]["content"]["application/json"]["schema"]
    assert capture["properties"]["boundary"]["enum"] == ["x_left", "x_right", "z_up", "z_down"]
    assert set(capture["properties"]) == {"boundary"}
    assert capture["additionalProperties"] is False


def test_driver_detected_move_violation_keeps_specific_fault(tmp_path):
    controller, document, serial, _drivers = controller_fixture(tmp_path)
    capture_bounds(controller, serial)
    ready(controller)
    document["program"] = [{"op": "move_to_column", "column": 2}]
    serial.on_move = lambda: serial.positions.update({2: 3000001})
    controller.start(document, "live", compile_experiment(document)["plan_hash"], "move-drift")
    result = wait_state(controller)
    assert result["state"] == "error"
    assert result["error"]["code"] == "absolute_limit_violation"
    assert result["stop_confirmed"] is True


@pytest.mark.parametrize("operation", ["capture", "save"])
def test_stop_invalidates_inflight_capture_or_save_without_deadlock(tmp_path, operation):
    controller, _document, serial, _drivers = controller_fixture(tmp_path)
    capture_bounds(controller, serial, save=False)
    entered, release = threading.Event(), threading.Event()
    errors = []

    def blocked_position(frame):
        entered.set()
        assert release.wait(2)
        return bytes([1, 0x36]) + signed(serial.positions[1], 4) + b"\x6b"

    serial.overrides[1, 0x36] = blocked_position

    def capture_or_save():
        try:
            if operation == "capture":
                controller.capture_limit("x_left")
            else:
                controller.save_limits(clearance_confirmed=True)
        except Exception as error:
            errors.append(error)

    worker = threading.Thread(target=capture_or_save)
    worker.start()
    assert entered.wait(1)
    stopper = threading.Thread(target=controller.stop)
    stopper.start()
    assert controller._stop_event.wait(1)
    release.set()
    worker.join(2)
    stopper.join(2)
    assert not worker.is_alive() and not stopper.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], AutomationError)
    assert errors[0].code == "limits_cancelled"
    assert wait_state(controller)["stop_confirmed"] is True
    assert controller.limits_snapshot()["active"] is False
    assert controller.limits_snapshot()["calibrating"] is False


def test_stop_during_plan_preflight_prevents_later_start(tmp_path):
    controller, document, serial, _drivers = controller_fixture(tmp_path)
    capture_bounds(controller, serial)
    ready(controller)
    document["program"] += [{"op": "wait_operator", "message": "Wait"}]
    entered, release = threading.Event(), threading.Event()
    errors = []

    def blocked_position(frame):
        entered.set()
        assert release.wait(2)
        return bytes([1, 0x36]) + signed(serial.positions[1], 4) + b"\x6b"

    serial.overrides[1, 0x36] = blocked_position

    def start():
        try:
            controller.start(document, "live", compile_experiment(document)["plan_hash"], "preflight-stop")
        except Exception as error:
            errors.append(error)

    worker = threading.Thread(target=start)
    worker.start()
    assert entered.wait(1)
    stopper = threading.Thread(target=controller.stop)
    stopper.start()
    assert controller._stop_event.wait(1)
    release.set()
    worker.join(2)
    stopper.join(2)
    try:
        assert not worker.is_alive() and not stopper.is_alive()
        assert len(errors) == 1 and isinstance(errors[0], AutomationError)
        assert errors[0].code == "cancelled"
        assert not (tmp_path / "runs").exists()
    finally:
        controller.stop()
        wait_state(controller)


def test_authorization_fault_stops_axes_before_waiting_for_collectors(tmp_path):
    controller, document, serial, drivers = controller_fixture(tmp_path)
    capture_bounds(controller, serial)
    ready(controller)
    entered, release = threading.Event(), threading.Event()
    factory = CollectorFactory(hold_gate=True)

    class BlockingStopCollector(FakeCollector):
        def poll(self):
            if Path(self.options["--cmd-file"]).read_text().strip() == "STOP":
                entered.set()
                assert release.wait(2)
            return super().poll()

    def process_factory(command, **kwargs):
        child = BlockingStopCollector(factory, dict(zip(command[3::2], command[4::2])))
        factory.children.append(child)
        return child

    controller._process_factory = process_factory
    document["program"] += [{"op": "dwell", "column": 1, "seconds": 10, "acquire": ["A", "E"]}]
    controller.start(document, "live", compile_experiment(document)["plan_hash"], "authorization-loss")
    deadline = time.monotonic() + 2
    while len(factory.children) < 2 and time.monotonic() < deadline:
        time.sleep(0.005)
    assert len(factory.children) == 2
    with controller._lock:
        drivers[0].install_absolute_limits(None)
    try:
        assert entered.wait(2)
        assert {frame[0] for frame in serial.writes if frame[1] == 0xFE} == {1, 2}
    finally:
        release.set()
        controller.stop()
    result = wait_state(controller)
    assert result["state"] == "error"
    assert result["error"]["code"] == "absolute_limits_required"


def test_limit_monitor_continues_through_guided_ble_recovery(tmp_path):
    stopped_boards = []
    controller, document, serial, _drivers = controller_fixture(
        tmp_path, board_stop=lambda address: stopped_boards.append(address) or {"stop_confirmed": False})
    capture_bounds(controller, serial)
    ready(controller)
    document.update(schema_version="1.1", run_policy="guided", calibration_id="saved-calibration")
    document["defaults"]["settle_s"] = 0
    document["program"] += [{"op": "dwell", "seconds": 0.5, "acquire": ["A", "E"]},
                            {"op": "withdraw"}, {"op": "move_to_column", "column": 2}]
    controller._process_factory = GuidedFactory(time, loss_at=time.monotonic() + 0.25, unavailable="AB")
    controller.start(document, "live", compile_experiment(document)["plan_hash"], "ble-recovery")
    try:
        result = wait_state(controller, "waiting_recovery")
        assert result["state"] == "waiting_recovery", result
        serial.positions[1] = 6000001
        result = wait_state(controller, "error")
        assert result["error"]["code"] == "stop_unconfirmed"
        assert result["execution_error"]["code"] == "absolute_limit_violation"
        assert result["busy"] and result["recovery_required"]
        assert set(stopped_boards) == {"board-a", "board-b"}
        assert {frame[0] for frame in serial.writes if frame[1] == 0xFE} == {1, 2}
    finally:
        controller._board_stop_factory = lambda address: {"stop_confirmed": True}
        controller.stop()
        wait_state(controller)


def test_strict_policy_keeps_simulation_available_without_motion_hardware(tmp_path):
    def forbidden(*args, **kwargs):
        pytest.fail("Simulation accessed motion hardware")

    controller = AutomationController(tmp_path / "state", tmp_path / "runs", tmp_path,
                                      motion_factory=forbidden, require_absolute_limits=True,
                                      simulation_scale=0)
    document = default_experiment()
    controller.start(document, "simulate", compile_experiment(document)["plan_hash"], "simulation")
    assert wait_state(controller)["state"] == "completed"
    assert controller.limits_snapshot()["active"] is False


def test_real_run_preserves_enforced_limits_and_manifest_hash_after_recalibration(tmp_path):
    controller, document, serial, _drivers = controller_fixture(tmp_path)
    capture_bounds(controller, serial)
    ready(controller)
    enforced = controller.limits_snapshot()["record"]
    controller.start(document, "live", compile_experiment(document)["plan_hash"], "audited-limits")
    result = wait_state(controller)
    assert result["state"] == "completed", result
    directory = Path(result["manifest_path"]).parent
    path = directory / "absolute-motion-limits.json"
    assert path.exists()
    preserved = path.read_bytes()
    assert json.loads(preserved) == enforced
    manifest = json.loads(Path(result["manifest_path"]).read_text())
    assert manifest["files"][path.name] == hashlib.sha256(preserved).hexdigest()
    started = next(event for event in controller.events()["events"] if event["event"] == "run_started")
    assert started["absolute_limits_fingerprint"] == hashlib.sha256(
        json.dumps(enforced, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()).hexdigest()
    capture_bounds(controller, serial, x_left=5000000)
    assert controller.limits_snapshot()["record"] != enforced
    assert path.read_bytes() == preserved


def test_simulation_does_not_claim_saved_bounds_were_enforced(tmp_path):
    controller, document, serial, _drivers = controller_fixture(tmp_path)
    capture_bounds(controller, serial)
    controller.start(document, "simulate", compile_experiment(document)["plan_hash"], "synthetic-audit")
    result = wait_state(controller)
    assert result["state"] == "completed", result
    directory = Path(result["manifest_path"]).parent
    assert not (directory / "absolute-motion-limits.json").exists()
    started = next(event for event in controller.events()["events"] if event["event"] == "run_started")
    assert "absolute_limits_fingerprint" not in started
