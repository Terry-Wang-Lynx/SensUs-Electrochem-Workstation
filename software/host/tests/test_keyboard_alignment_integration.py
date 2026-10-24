import threading
from unittest.mock import Mock

import pytest

from pa_host import gui_server
from pa_host.automation import AutomationError
from test_guided_integration import guided_app


@pytest.fixture
def alignment_app(guided_app):
    guided_app.guided_automation._require_four_point = True
    guided_app.guided_automation.snapshot.return_value["manual"] = {
        "active": True, "control_mode": "keyboard", "status": "aligning", "session_token": "session-current"}
    guided_app.automation._manual_calibration_active = True
    guided_app.automation._stop_event = threading.Event()
    guided_app.automation._motion.snapshot.return_value = {"connected": True, "faulted": False}
    guided_app.automation.snapshot.return_value = {"state": "stopped", "stop_confirmed": True,
                                                 "recovery_required": False, "busy": True}
    return guided_app


def dispatch(action, body=None, method="POST"):
    return gui_server._automation_dispatch(method, action, body or {}, {})


def test_keyboard_begin_requires_explicit_motion_authorization(alignment_app):
    alignment_app.guided_automation.begin_keyboard_calibration.return_value = {"manual": {"active": True}}
    dispatch("guided/calibration/begin", {"control_mode": "keyboard", "motion_confirmed": True})
    alignment_app.guided_automation.begin_keyboard_calibration.assert_called_once_with(
        alignment_app.automation._motion, confirmed=True)
    alignment_app.guided_automation.begin_calibration.assert_not_called()


def test_keyboard_begin_passes_only_explicit_current_direction_boundary(alignment_app):
    alignment_app.guided_automation.begin_keyboard_calibration.return_value = {"manual": {"active": True}}
    dispatch("guided/calibration/begin", {"control_mode": "keyboard", "motion_confirmed": True,
                                          "boundary_at_current": {"axis": "Z", "direction": "up"}})
    alignment_app.guided_automation.begin_keyboard_calibration.assert_called_once_with(
        alignment_app.automation._motion, confirmed=True, boundary_at_current={"axis": "Z", "direction": "up"})


@pytest.mark.parametrize("action,method,body", [
    ("start", "start_jog", {"session_token": "session-current", "press_id": "press-1", "axis": "X", "direction": "left"}),
    ("heartbeat", "heartbeat_jog", {"session_token": "session-current", "press_id": "press-1", "sequence": 1}),
    ("stop", "stop_jog", {"session_token": "session-current", "press_id": "press-1"}),
    ("config", "configure_jog", {"session_token": "session-current", "speed_mm_s": 0.5, "max_speed_mm_s": 1}),
    ("config", "configure_jog", {"session_token": "session-current", "speed_mm_s": 20, "max_speed_mm_s": 20,
                                   "fast_motion_confirmed": True}),
])
def test_keyboard_routes_only_delegate_whitelisted_structured_actions(alignment_app, action, method, body):
    dispatch("guided/calibration/jog/" + action, body)
    getattr(alignment_app.guided_automation, method).assert_called_once_with(body)
    alignment_app.automation.enable_motion.assert_not_called()
    alignment_app.automation.reference_motion.assert_not_called()


@pytest.mark.parametrize("action", ["start", "heartbeat", "stop", "config"])
def test_keyboard_api_rejects_raw_commands_and_forged_positions(alignment_app, action):
    with pytest.raises(AutomationError) as caught:
        dispatch("guided/calibration/jog/" + action, {"session_token": "session-current", "raw_counts": {"X": 0}})
    assert caught.value.code == "invalid_value"
    alignment_app.guided_automation.start_jog.assert_not_called()
    alignment_app.guided_automation.heartbeat_jog.assert_not_called()
    alignment_app.guided_automation.stop_jog.assert_not_called()
    alignment_app.guided_automation.configure_jog.assert_not_called()


def test_key_release_stop_bypasses_busy_operation_and_measurement_locks(alignment_app):
    alignment_app.measurement.is_busy = Mock(return_value=True)
    alignment_app.operation_lock.acquire()
    try:
        dispatch("guided/calibration/jog/stop", {"session_token": "session-current", "press_id": "press-1"})
    finally:
        alignment_app.operation_lock.release()
    alignment_app.guided_automation.stop_jog.assert_called_once()


def test_auto_execution_cannot_start_keyboard_motion(alignment_app):
    alignment_app.automation.snapshot.return_value.update(state="running")
    with pytest.raises(AutomationError) as caught:
        dispatch("guided/calibration/jog/start", {"session_token": "session-current", "press_id": "press-1",
                                                   "axis": "Z", "direction": "up"})
    assert caught.value.code == "busy"
    alignment_app.guided_automation.start_jog.assert_not_called()


def test_cached_alignment_get_never_reads_or_enables_physical_axes(alignment_app):
    alignment_app.guided_automation.alignment_snapshot.return_value = {"active": False}
    assert dispatch("guided/calibration/jog", method="GET") == {"active": False}
    alignment_app.automation._motion.refresh.assert_not_called()
    alignment_app.automation.enable_motion.assert_not_called()


def test_keyboard_io_never_holds_controller_lock_or_exposes_observer_races(alignment_app):
    def begin(motion, confirmed):
        assert not alignment_app.automation._lock._is_owned()
        assert alignment_app.automation._alignment_io_active is True
        assert alignment_app.automation._keyboard_operation_active is True
        return {"manual": {"active": True}}
    alignment_app.guided_automation.begin_keyboard_calibration.side_effect = begin
    dispatch("guided/calibration/begin", {"control_mode": "keyboard", "motion_confirmed": True})
    assert alignment_app.automation._alignment_io_active is False


def test_point_capture_drains_input_outside_controller_lock(alignment_app):
    events = []
    def freeze():
        assert not alignment_app.automation._lock._is_owned()
        events.append("freeze")
    def capture(motion, point):
        assert events == ["freeze"]
        assert not alignment_app.automation._lock._is_owned()
        events.append("capture")
        return {"manual": {"active": True}}
    alignment_app.guided_automation.freeze_alignment.side_effect = freeze
    alignment_app.guided_automation.capture_calibration.side_effect = capture
    dispatch("guided/calibration/capture", {"point": "working"})
    assert events == ["freeze", "capture"]


def test_openapi_exposes_only_typed_jog_controls():
    from pa_host.automation import openapi_document

    paths = openapi_document()["paths"]
    assert "/api/automation/v1/guided/calibration/jog" in paths
    schema = paths["/api/automation/v1/guided/calibration/jog/start"]["post"]["requestBody"]["content"]["application/json"]["schema"]
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"session_token", "press_id", "axis", "direction"}
    assert "raw_counts" not in schema["properties"]


def test_shutdown_stops_keyboard_before_waiting_for_operation_lock():
    import inspect

    source = inspect.getsource(gui_server._release_hardware_for_shutdown)
    assert source.index("_halt_keyboard_input") < source.index("with APP.operation_lock")
    assert "_halt_keyboard_input" not in inspect.getsource(gui_server._wait_for_usb_transport_ready)


def test_unknown_begin_stop_retains_controller_recovery_ownership(alignment_app):
    failure = AutomationError("alignment_begin", "Enable failed and STOP is unknown")
    failure.stop_result = {"stop_confirmed": False, "axes": {}}
    alignment_app.guided_automation.begin_keyboard_calibration.side_effect = failure
    with pytest.raises(AutomationError):
        dispatch("guided/calibration/begin", {"control_mode": "keyboard", "motion_confirmed": True})
    alignment_app.automation._retain_motion_ownership.assert_called_once()


def test_global_stop_latches_driver_before_reading_busy_begin_domain(alignment_app):
    alignment_app.automation._keyboard_operation_active = True
    alignment_app.automation._motion.stop_all.return_value = {"stop_confirmed": True}
    def snapshot():
        alignment_app.automation._motion.stop_all.assert_called_once()
        assert not alignment_app.automation._lock._is_owned()
        return {"manual": {"active": False}}
    alignment_app.guided_automation.snapshot.side_effect = snapshot
    gui_server._halt_keyboard_input(alignment_app.automation, alignment_app.guided_automation)


def test_interrupted_calibration_allows_readonly_reconnect_without_clearing_it(alignment_app):
    alignment_app.automation._motion = None
    alignment_app.guided_automation.snapshot.return_value["manual"].update(
        status="blocked", error={"code": "manual_interrupted"})
    alignment_app.guided_automation.alignment_snapshot.return_value = {"session_token": None}
    def connect(settings):
        assert alignment_app.automation._manual_calibration_active is False
        return {"motion": {"connected": True}}
    alignment_app.automation.connect_motion.side_effect = connect
    assert dispatch("motion/connect", {"motion": {"port": "cached-test"}})["motion"]["connected"] is True
    assert alignment_app.automation._manual_calibration_active is True
    assert alignment_app.guided_automation.snapshot()["manual"]["active"] is True
    alignment_app.guided_automation.begin_keyboard_calibration.assert_not_called()
    alignment_app.automation.enable_motion.assert_not_called()


def test_overlapping_keyboard_io_holds_do_not_resume_observer_early(alignment_app):
    with gui_server._keyboard_hardware_io(alignment_app.automation, alignment_app.guided_automation):
        with gui_server._keyboard_hardware_io(alignment_app.automation, alignment_app.guided_automation):
            assert alignment_app.automation._alignment_io_active is True
        assert alignment_app.automation._alignment_io_active is True
    assert alignment_app.automation._alignment_io_active is False


@pytest.mark.parametrize("schedule", ["real_workers", "controlled", "delayed_preflight"])
def test_real_controller_shaped_keyboard_flow_uses_finite_steps_and_no_reenable(guided_app, tmp_path, monkeypatch, schedule):
    import copy
    import time

    from pa_host import guided_automation
    from pa_host.alignment_jog import AlignmentJog
    from pa_host.automation import AutomationController
    from pa_host.emm_motion import SharedEmmBus
    from pa_host.guided_automation import GuidedAutomation
    from test_powered_manual_driver import PoweredManualSerial

    clock = [0.0]
    def alignment_factory(*args, **options):
        return AlignmentJog(*args, **options, clock=lambda: clock[0], start_threads=False)
    if schedule == "controlled":
        monkeypatch.setattr(guided_automation, "AlignmentJog", alignment_factory)
    serial = PoweredManualSerial(timeout=0.2, write_timeout=0.2)
    motion_command = threading.Event()
    original_write = serial.write
    def observe_write(frame):
        result = original_write(frame)
        if frame[1] == 0xFD:
            motion_command.set()
        return result
    serial.write = observe_write
    serial.statuses = {1: 3, 2: 3}
    def factory(port, baud, axes, **options):
        return SharedEmmBus(port, baud, axes, serial_factory=lambda **settings: serial, **options)
    controller = AutomationController(tmp_path / "automation", tmp_path / "runs", tmp_path,
                                      motion_factory=factory, motion_feedback_interval_s=0.05)
    service = GuidedAutomation(tmp_path / "guided", require_four_point=True,
        alignment_error_callback=lambda error, result: gui_server._alignment_fault(controller, error, result))
    document = service.snapshot()["document"]
    document["bindings"]["motion"]["port"] = "fake-powered-keyboard"
    service.save(document)
    controller.connect_motion(document["bindings"]["motion"])
    guided_app.automation, guided_app.guided_automation = controller, service
    serial.writes.clear()
    try:
        began = dispatch("guided/calibration/begin", {"control_mode": "keyboard", "motion_confirmed": True})
        assert controller.snapshot()["busy"] is True
        assert began["manual"]["control_mode"] == "keyboard"
        token = began["manual"]["session_token"]
        if schedule == "controlled":
            clock[0] += 2.1
            service._alignment._watch_once(refresh_idle=True)
        else:
            time.sleep(2.1)
        assert all(axis["telemetry_valid"] for axis in controller.snapshot()["motion"]["axes"].values())
        dispatch("guided/calibration/jog/config", {"session_token": token, "speed_mm_s": 0.5, "max_speed_mm_s": 1})
        press = {"session_token": token, "press_id": "integration-hold"}
        preflight_started = threading.Event()
        if schedule == "delayed_preflight":
            original_preflight = controller._motion._keyboard_start_states
            def delayed_preflight(*args, **options):
                preflight_started.set()
                time.sleep(2.2)
                return original_preflight(*args, **options)
            monkeypatch.setattr(controller._motion, "_keyboard_start_states", delayed_preflight)
        dispatch("guided/calibration/jog/start", {**press, "axis": "X", "direction": "left"})
        assert service._alignment.LEASE_SECONDS == 0.35
        if schedule == "controlled":
            clock[0] += 0.1
            dispatch("guided/calibration/jog/heartbeat", {**press, "sequence": 1})
            service._alignment._step_once()
        else:
            started = time.monotonic()
            deadline = started + 10
            sequence = 0
            checked_old_budget = False
            while not motion_command.is_set() and time.monotonic() < deadline:
                sequence += 1
                dispatch("guided/calibration/jog/heartbeat", {**press, "sequence": sequence})
                if schedule == "delayed_preflight" and time.monotonic() - started >= 2:
                    assert preflight_started.is_set()
                    checked_old_budget = True
                motion_command.wait(0.1)
            if schedule == "delayed_preflight":
                assert checked_old_budget
        assert any(frame[1] == 0xFD for frame in serial.writes), service.alignment_snapshot()
        dispatch("guided/calibration/jog/stop", press)
        sampled = dispatch("guided/calibration/capture", {"point": "first"})
        assert set(sampled["manual"]["points"]) == {"first"}
        dispatch("stop")
        assert service.snapshot()["manual"]["active"] is False
        assert controller.snapshot()["busy"] is False
        frames = [frame for frame in serial.writes if frame[1] == 0xFD]
        assert frames
        assert all(int.from_bytes(frame[6:10], "big") * 2 / 3200 <= 0.1 for frame in frames)
        assert not any(frame[1] in (0xF3, 0xF6, 0x0A, 0x9A) for frame in serial.writes)
        before = copy.deepcopy(serial.writes)
        with pytest.raises(AutomationError):
            dispatch("guided/calibration/jog/start", {**press, "axis": "Z", "direction": "down"})
        controller._motion.refresh()
        observer_reads = {bytes((address, opcode, 0x6B))
                          for address in (1, 2) for opcode in (0x35, 0x36, 0x3A)}
        assert all(bytes(frame) in observer_reads for frame in serial.writes[len(before):])
    finally:
        manager = service._alignment
        if manager is not None:
            manager.close(stop=False)
        controller.disconnect_motion()
