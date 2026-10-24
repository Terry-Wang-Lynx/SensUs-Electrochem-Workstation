from __future__ import annotations

import copy
import json

import pytest

from pa_host.automation import AutomationError
from pa_host.emm_motion import MotionError
from pa_host.guided_automation import GuidedAutomation
from test_four_point_calibration import PoweredCalibrationMotion


class KeyboardMotion(PoweredCalibrationMotion):
    def __init__(self, document):
        super().__init__(document)
        self.session_token = None
        self.keyboard = False
        self.stop_confirmed = True

    def begin_keyboard_alignment(self, confirmed=False):
        self.calls.append("keyboard_begin")
        if confirmed is not True:
            raise MotionError("alignment_confirmation", "Motion clearance requires confirmation")
        if self.error:
            raise self.error
        self.keyboard = True
        self.manual = False
        self.session_token = "keyboard-session-1"
        return self.capture_keyboard_alignment()

    def snapshot(self):
        state = super().snapshot()
        state["manual"] = {"active": self.keyboard, "control_mode": "keyboard", "session_token": self.session_token}
        state["stop_generation"] = 1
        state["stop_confirmed"] = self.stop_confirmed
        for axis in state["axes"].values():
            axis["speed_rpm"] = 0
        return state

    def capture_keyboard_alignment(self):
        self.calls.append("keyboard_capture")
        if self.error:
            raise self.error
        if not self.keyboard:
            raise MotionError("alignment_session", "No keyboard session")
        return {"raw_counts": copy.deepcopy(self.counts), "motion": copy.deepcopy(self.motion),
                "profiles": copy.deepcopy(self.profiles), "connection_token": self.connection_token,
                "session_token": self.session_token, "stop_generation": 1}

    def stop_keyboard_alignment(self):
        self.calls.append("keyboard_stop")
        return {"stop_confirmed": self.stop_confirmed, "axes": {}, "errors": []}

    def finish_keyboard_alignment(self, reference_counts):
        self.calls.append("keyboard_finish")
        self.reference = copy.deepcopy(reference_counts)
        self.referenced = True
        self.keyboard = False
        return self.reference_identity()

    def cancel_keyboard_alignment(self):
        self.calls.append("keyboard_cancel")
        self.keyboard = False
        return self.snapshot()


def keyboard_session(tmp_path, *, callback=None):
    service = GuidedAutomation(tmp_path, require_four_point=True, alignment_error_callback=callback)
    motion = KeyboardMotion(service.snapshot()["document"])
    service.begin_keyboard_calibration(motion, confirmed=True)
    return service, motion


def test_strict_default_advertises_keyboard_mode_without_starting_or_io(tmp_path):
    service = GuidedAutomation(tmp_path, require_four_point=True)
    state = service.snapshot()
    assert state["manual"]["control_mode"] == "keyboard"
    assert state["manual"]["active"] is False
    assert state["manual"]["alignment"]["settings"]["speed_mm_s"] == 0.5


def test_current_position_guard_request_is_consumed_not_reanchored_after_moving(tmp_path):
    service = GuidedAutomation(tmp_path, require_four_point=True)
    motion = KeyboardMotion(service.snapshot()["document"])
    boundary = {"axis": "Z", "direction": "up"}
    service._manual["boundary_at_current"] = boundary
    original_begin = motion.begin_keyboard_alignment
    requested = []

    def begin(confirmed=False, boundary_at_current=None):
        requested.append(boundary_at_current)
        return original_begin(confirmed=confirmed)

    motion.begin_keyboard_alignment = begin
    try:
        result = service.begin_keyboard_calibration(motion, confirmed=True, boundary_at_current=boundary)
        assert requested == [boundary]
        assert "boundary_at_current" not in result["manual"]
    finally:
        service.cancel_calibration(motion)


def test_begin_requires_explicit_literal_confirmation_before_driver_call(tmp_path):
    service = GuidedAutomation(tmp_path, require_four_point=True)
    motion = KeyboardMotion(service.snapshot()["document"])
    for confirmation in (False, 1, "true"):
        with pytest.raises(AutomationError):
            service.begin_keyboard_calibration(motion, confirmed=confirmation)
    assert motion.calls == []
    assert service.snapshot()["manual"]["active"] is False


def test_keyboard_begin_owns_current_session_and_blocks_automatic_work(tmp_path):
    service, motion = keyboard_session(tmp_path)
    state = service.snapshot()
    assert state["manual"]["active"] is True
    assert state["manual"]["control_mode"] == "keyboard"
    assert state["manual"]["session_token"] == "keyboard-session-1"
    assert state["manual"]["required_points"] == ["first", "last", "working", "safe"]
    assert state["manual"]["points"] == {}
    with pytest.raises(AutomationError):
        service.compile(state["document"])
    service.cancel_calibration(motion)


def test_capture_freezes_stops_reads_then_resumes_same_session(tmp_path):
    service, motion = keyboard_session(tmp_path)
    motion.calls.clear()
    state = service.capture_calibration(motion, "first")
    assert motion.calls == ["keyboard_stop", "keyboard_capture", "keyboard_capture"]
    assert state["manual"]["points"]["first"]["raw_counts"] == {"X": 0, "Z": 0}
    assert state["manual"]["alignment"]["status"] == "idle"
    service.cancel_calibration(motion)


def test_keyboard_four_points_finish_without_legacy_release_or_relock(tmp_path):
    service, motion = keyboard_session(tmp_path)
    for point, counts in {"first": {"X": 0, "Z": 0}, "last": {"X": 99 * 32768, "Z": 0},
                          "working": {"X": 0, "Z": 6 * 32768}, "safe": {"X": 0, "Z": 0}}.items():
        motion.counts = counts
        service.capture_calibration(motion, point)
    result = service.finish_calibration(motion)
    assert result["manual"]["active"] is False
    assert result["calibration"]["usable"] is True
    assert result["calibration"]["z_working_mm"] == 7
    assert result["calibration"]["column_pitch_mm"] == 9
    assert "keyboard_finish" in motion.calls
    assert not any(call in motion.calls for call in ("begin", "finish", "cancel"))


def test_keyboard_finish_requires_four_points_even_with_legacy_service_default(tmp_path):
    service = GuidedAutomation(tmp_path)
    motion = KeyboardMotion(service.snapshot()["document"])
    service.begin_keyboard_calibration(motion, confirmed=True)
    for point in ("first", "last", "safe"):
        service.capture_calibration(motion, point)
    with pytest.raises(AutomationError) as caught:
        service.finish_calibration(motion)
    assert caught.value.code == "calibration_points"
    assert "keyboard_finish" not in motion.calls
    service.cancel_calibration(motion)


def test_fresh_explicit_begin_can_supersede_persisted_legacy_interruption_only_after_preflight(tmp_path):
    service = GuidedAutomation(tmp_path, require_four_point=True)
    saved = {"document": service.snapshot()["document"], "calibration": None, "manual_interrupted": True}
    (tmp_path / "guided-automation.json").write_text(json.dumps(saved), encoding="utf-8")
    service = GuidedAutomation(tmp_path, require_four_point=True)
    motion = KeyboardMotion(service.snapshot()["document"])
    motion.error = MotionError("manual_relock_target", "Target differs from actual")
    with pytest.raises(AutomationError):
        service.begin_keyboard_calibration(motion, confirmed=True)
    assert service.snapshot()["manual"]["error"]["code"] == "manual_interrupted"
    motion.error = None
    state = service.begin_keyboard_calibration(motion, confirmed=True)
    assert state["manual"]["error"] is None
    assert state["manual"]["status"] == "aligning"
    service.cancel_calibration(motion)


def test_unknown_stop_marks_manual_blocked_retains_ownership_and_forbids_resume(tmp_path):
    notifications = []
    service, motion = keyboard_session(tmp_path, callback=lambda error, result: notifications.append((error, result)))
    motion.stop_confirmed = False
    with pytest.raises(AutomationError):
        service.freeze_alignment()
    state = service.snapshot()
    assert state["manual"]["active"] is True
    assert state["manual"]["status"] == "blocked"
    assert state["manual"]["session_token"] == "keyboard-session-1"
    assert notifications[0][1]["stop_confirmed"] is False
    with pytest.raises(AutomationError):
        service.resume_alignment()


def test_resume_rechecks_read_identity_and_never_trusts_client_or_changed_session(tmp_path):
    service, motion = keyboard_session(tmp_path)
    service.freeze_alignment()
    motion.session_token = "new-session"
    with pytest.raises(AutomationError):
        service.resume_alignment()
    assert service.snapshot()["manual"]["status"] == "blocked"


def test_cached_snapshot_never_uses_live_driver_capture(tmp_path):
    service, motion = keyboard_session(tmp_path)
    motion.calls.clear()
    service.alignment_snapshot()
    service.snapshot()
    assert motion.calls == []
    service.cancel_calibration(motion)


def test_global_stop_after_driver_begin_return_does_not_publish_a_new_jog_session(tmp_path):
    service = GuidedAutomation(tmp_path, require_four_point=True)
    motion = KeyboardMotion(service.snapshot()["document"])
    original = motion.begin_keyboard_alignment

    def stopped_begin(confirmed=False):
        captured = original(confirmed)
        motion.keyboard = False
        return captured

    motion.begin_keyboard_alignment = stopped_begin
    with pytest.raises(AutomationError):
        service.begin_keyboard_calibration(motion, confirmed=True)
    assert service.snapshot()["manual"]["status"] == "blocked"
    with pytest.raises(AutomationError):
        service.start_jog({"session_token": "keyboard-session-1", "press_id": "press", "axis": "X", "direction": "left"})


def test_real_driver_domain_capture_and_finish_reuse_trusted_fake_serial_counts(tmp_path):
    from test_powered_manual_driver import powered_bus

    motion, serial, events = powered_bus(enabled=True)
    service = GuidedAutomation(tmp_path, require_four_point=True)
    document = service.snapshot()["document"]
    state = motion.snapshot()
    document["bindings"]["motion"] = {"port": state["port"], "baud": state["baud"],
                                          "axes": {axis: state["axes"][axis]["mechanical"] for axis in ("X", "Z")}}
    service.save(document)
    service.begin_keyboard_calibration(motion, confirmed=True)
    serial.writes.clear()
    for point, counts in {"first": (0, 0), "last": (99 * 32768, 0),
                          "working": (0, -6 * 32768), "safe": (0, 0)}.items():
        serial.positions.update({1: counts[0], 2: counts[1]})
        for targets in serial.targets.values():
            targets.update(serial.positions)
        service.capture_calibration(motion, point)
    result = service.finish_calibration(motion)
    assert result["calibration"]["usable"] is True
    assert not any(frame[1] in (0xFD, 0xF3, 0xF6, 0x0A, 0x93, 0x9A) for frame in serial.writes)


def test_explicit_safe_retry_retires_blocked_manager_and_mints_fresh_session(tmp_path):
    service, motion = keyboard_session(tmp_path)
    old_manager = service._alignment
    motion.error = MotionError("test_read_failure", "Readback failed after a confirmed STOP")
    with pytest.raises(AutomationError):
        service.capture_calibration(motion, "first")
    assert service.snapshot()["manual"]["status"] == "blocked"
    motion.error = None
    original_begin = motion.begin_keyboard_alignment

    def new_begin(confirmed=False):
        captured = original_begin(confirmed)
        motion.session_token = "keyboard-session-2"
        captured["session_token"] = motion.session_token
        return captured

    motion.begin_keyboard_alignment = new_begin
    state = service.begin_keyboard_calibration(motion, confirmed=True)
    assert state["manual"]["session_token"] == "keyboard-session-2"
    assert state["manual"]["points"] == {}
    assert service._alignment is not old_manager
    assert old_manager.snapshot()["status"] == "closed"
    with pytest.raises(AutomationError):
        old_manager.start({"session_token": "keyboard-session-1", "press_id": "late", "axis": "X", "direction": "left"})
    service.cancel_calibration(motion)


def test_explicit_retry_cannot_clear_unknown_stop_with_confirmation_flag(tmp_path):
    service, motion = keyboard_session(tmp_path)
    motion.stop_confirmed = False
    with pytest.raises(AutomationError):
        service.freeze_alignment()
    motion.calls.clear()
    with pytest.raises(AutomationError):
        service.begin_keyboard_calibration(motion, confirmed=True)
    assert "keyboard_begin" not in motion.calls


def test_persisted_interruption_never_bypasses_known_unknown_stop(tmp_path):
    service = GuidedAutomation(tmp_path, require_four_point=True)
    saved = {"document": service.snapshot()["document"], "calibration": None, "manual_interrupted": True}
    (tmp_path / "guided-automation.json").write_text(json.dumps(saved), encoding="utf-8")
    service = GuidedAutomation(tmp_path, require_four_point=True)
    motion = KeyboardMotion(service.snapshot()["document"])
    motion.stop_confirmed = False
    with pytest.raises(AutomationError):
        service.begin_keyboard_calibration(motion, confirmed=True)
    assert motion.calls == []


def test_stop_during_manager_creation_retires_unpublished_jog_worker(tmp_path, monkeypatch):
    import pa_host.guided_automation as guided_module

    service = GuidedAutomation(tmp_path, require_four_point=True)
    motion = KeyboardMotion(service.snapshot()["document"])
    original_manager = guided_module.AlignmentJog
    managers = []

    def stopped_manager(*args, **kwargs):
        manager = original_manager(*args, **kwargs)
        managers.append(manager)
        motion.keyboard = False
        return manager

    monkeypatch.setattr(guided_module, "AlignmentJog", stopped_manager)
    with pytest.raises(AutomationError):
        service.begin_keyboard_calibration(motion, confirmed=True)
    assert service.snapshot()["manual"]["status"] == "blocked"
    assert managers[0].snapshot()["status"] == "closed"
