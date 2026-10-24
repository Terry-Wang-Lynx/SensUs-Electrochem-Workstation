import copy
import functools
import threading

import pytest

from pa_host import gui_server
from pa_host.alignment_jog import AlignmentJog
from pa_host.automation import AutomationController, AutomationError
from pa_host.emm_motion import MotionError, SharedEmmBus
from pa_host.guided_automation import GuidedAutomation
from test_guided_integration import dispatch, guided_app
from test_powered_manual_driver import PoweredManualSerial


@pytest.fixture
def recovery_app(guided_app, tmp_path, monkeypatch):
    serials = []

    def factory(port, baud, axes, **options):
        serial = PoweredManualSerial(timeout=0.2, write_timeout=0.2)
        serial.statuses = {1: 3, 2: 3}
        serials.append(serial)
        bus = SharedEmmBus(port, baud, axes, serial_factory=lambda **settings: serial,
                           transaction_timeout_s=0.01, **options)
        bus._command_gap_s = 0
        return bus

    monkeypatch.setattr("pa_host.guided_automation.AlignmentJog", functools.partial(AlignmentJog, start_threads=False))
    controller = AutomationController(tmp_path / "automation", tmp_path / "runs", tmp_path,
                                      motion_factory=factory, motion_feedback_interval_s=60)
    service = GuidedAutomation(tmp_path / "guided", require_four_point=True,
        alignment_error_callback=lambda error, result: gui_server._alignment_fault(controller, error, result))
    document = service.snapshot()["document"]
    document["bindings"]["motion"]["port"] = "fake-stop-recovery"
    service.save(document)
    controller.connect_motion(document["bindings"]["motion"])
    controller._stop_motion_feedback()
    guided_app.automation, guided_app.guided_automation = controller, service
    dispatch("guided/calibration/begin", {"control_mode": "keyboard", "motion_confirmed": True})
    dispatch("guided/calibration/capture", {"point": "first"})
    yield controller, service, serials
    if service._alignment is not None:
        service._alignment.retire()
    controller._stop_motion_feedback()
    if controller._motion is not None:
        serials[-1].overrides.clear()
        controller._motion.close()


def fail_alignment_stop(controller, service, serial):
    serial.overrides[2, 0xFE] = b""
    failure = MotionError("calibration_motion", "Fast alignment left its committed finite envelope")
    controller._motion._keyboard_failure(failure)
    assert failure.stop_result["stop_confirmed"] is False
    with pytest.raises(AutomationError) as caught:
        service._alignment._halt("fault", force=True, error=failure)
    assert caught.value.code == "alignment_stop_unknown"
    serial.overrides.clear()


@pytest.mark.parametrize("action", ["stop", "motion/stop"])
def test_explicit_stop_recovers_keyboard_evidence_without_reviving_the_faulted_session(recovery_app, action):
    controller, service, serials = recovery_app
    serial = serials[-1]
    old_manager = service._alignment
    old_token = service.snapshot()["manual"]["session_token"]
    fail_alignment_stop(controller, service, serial)
    assert controller._motion.stop_all()["stop_confirmed"] is True
    before = copy.deepcopy(serial.writes)
    assert dispatch("guided", method="GET")["manual"]["alignment"]["stop_result"]["stop_confirmed"] is False
    assert serial.writes == before
    serial.writes.clear()
    dispatch(action)
    if controller._thread:
        controller._thread.join(3)
        assert not controller._thread.is_alive()
    recovered = service.snapshot()["manual"]
    assert controller.snapshot()["stop_confirmed"] is True
    assert recovered["alignment"]["stop_result"]["stop_confirmed"] is True
    assert recovered["status"] == "blocked"
    assert recovered["error"]["cause"]["code"] == "calibration_motion"
    assert recovered["points"] == {}
    assert {frame[0] for frame in serial.writes if frame[1] == 0xFE} == {1, 2}
    assert not any(frame[1] in (0xF3, 0xFD, 0xF6, 0x0A, 0x9A) for frame in serial.writes)
    with pytest.raises(AutomationError):
        old_manager.resume(old_token)
    with pytest.raises(AutomationError):
        dispatch("guided/calibration/jog/heartbeat", {"session_token": old_token, "press_id": "old", "sequence": 1})
    restarted = dispatch("guided/calibration/begin", {"control_mode": "keyboard", "motion_confirmed": True})
    assert restarted["manual"]["status"] == "aligning"
    assert restarted["manual"]["session_token"] != old_token
    assert restarted["manual"]["points"] == {}
    assert all(not any(frame[1] in (0xF3, 0xFD) for frame in current.writes) for current in serials)


def test_failed_explicit_stop_keeps_keyboard_recovery_blocked(recovery_app):
    controller, service, serials = recovery_app
    serial = serials[-1]
    fail_alignment_stop(controller, service, serial)
    assert controller._motion.stop_all()["stop_confirmed"] is True
    serial.overrides[2, 0xFE] = b""
    serial.writes.clear()
    dispatch("motion/stop")
    controller._thread.join(3)
    assert not controller._thread.is_alive()
    assert controller.snapshot()["recovery_required"] is True
    assert service.snapshot()["manual"]["alignment"]["stop_result"]["stop_confirmed"] is False
    with pytest.raises(AutomationError):
        dispatch("guided/calibration/begin", {"control_mode": "keyboard", "motion_confirmed": True})
    assert not any(frame[1] in (0xF3, 0xFD) for frame in serial.writes)


def test_preparing_another_stop_revokes_previous_proof_until_a_new_stop_completes(recovery_app):
    controller, service, serials = recovery_app
    fail_alignment_stop(controller, service, serials[-1])
    dispatch("motion/stop")
    assert service.alignment_snapshot()["stop_result"]["stop_confirmed"] is True
    recovery = service.prepare_alignment_stop_recovery(controller._motion)
    assert service.alignment_snapshot()["stop_result"] is None
    with pytest.raises(AutomationError, match="new current-connection evidence"):
        service.reconcile_alignment_stop_recovery(recovery)
    with pytest.raises(AutomationError):
        dispatch("guided/calibration/begin", {"control_mode": "keyboard", "motion_confirmed": True})


@pytest.mark.parametrize("publish_before_drain", [True, False])
@pytest.mark.parametrize("request_origin", ["step", "release"])
def test_explicit_stop_waits_for_inflight_fault_publication_before_fresh_dual_stop(
        recovery_app, monkeypatch, publish_before_drain, request_origin):
    controller, service, serials = recovery_app
    serial = serials[-1]
    manager = service._alignment
    fault_published, release_fault, drain_started, drain_finished, stop_finished = (
        threading.Event() for _index in range(5))
    errors = []
    original_blocked = manager._on_blocked
    original_prepare = manager.prepare_stop_recovery

    def hold_fault_tail(error, result):
        if publish_before_drain:
            original_blocked(error, result)
        fault_published.set()
        assert release_fault.wait(3)
        if not publish_before_drain:
            original_blocked(error, result)

    def preparing():
        drain_started.set()
        try:
            return original_prepare()
        finally:
            drain_finished.set()

    def run_step():
        try:
            if request_origin == "step":
                manager._step_once()
            else:
                manager.stop({"session_token": token, "press_id": "faulting-press"})
        except AutomationError as error:
            errors.append(error)

    def run_stop():
        try:
            dispatch("motion/stop")
        finally:
            stop_finished.set()

    monkeypatch.setattr(manager, "_on_blocked", hold_fault_tail)
    monkeypatch.setattr(manager, "prepare_stop_recovery", preparing)
    serial.overrides[1, 0x36] = bytes.fromhex("013600000100006a")
    serial.overrides[2, 0xFE] = b""
    token = service.snapshot()["manual"]["session_token"]
    manager.start({"session_token": token, "press_id": "faulting-press", "axis": "X", "direction": "left"})
    step_thread = threading.Thread(target=run_step)
    step_thread.start()
    stop_thread = threading.Thread(target=run_stop)
    try:
        assert fault_published.wait(3)
        serial.overrides.clear()
        serial.writes.clear()
        stop_thread.start()
        assert drain_started.wait(1)
        assert not drain_finished.wait(0.1)
        with pytest.raises(AutomationError, match="Drain the old keyboard session"):
            manager.retire()
        assert not stop_finished.is_set()
        assert not any(frame[1] == 0xFE for frame in serial.writes)
    finally:
        release_fault.set()
        step_thread.join(3)
        if stop_thread.ident is not None:
            stop_thread.join(5)
    assert not step_thread.is_alive() and not stop_thread.is_alive()
    assert errors and errors[0].code == "alignment_stop_unknown"
    assert service.alignment_snapshot()["stop_result"]["stop_confirmed"] is True
    assert {frame[0] for frame in serial.writes if frame[1] == 0xFE} == {1, 2}
    assert not any(frame[1] in (0xF3, 0xFD) for frame in serial.writes)
