from __future__ import annotations

import copy
import threading

import pytest

from pa_host.alignment_jog import AlignmentJog
from pa_host.automation import AutomationError


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class JogMotion:
    def __init__(self):
        self.axes = {axis: {"mechanical": {"lead_mm": 2, "max_speed_mm_s": 10}, "config": {"pulses_per_rev": 3200}}
                     for axis in ("X", "Z")}
        self.steps = []
        self.stops = 0
        self.stop_confirmed = True
        self.step_entered = threading.Event()
        self.step_release = None

    def snapshot(self):
        return {"connected": True, "faulted": False, "axes": copy.deepcopy(self.axes)}

    def alignment_step(self, axis, direction, speed_mm_s, duration_s, max_speed_mm_s, cancel_event=None):
        self.steps.append({"axis": axis, "direction": direction, "speed_mm_s": speed_mm_s,
                           "duration_s": duration_s, "max_speed_mm_s": max_speed_mm_s,
                           "cancel_event": cancel_event})
        self.step_entered.set()
        if self.step_release is not None:
            assert self.step_release.wait(2)
        return {"segment_mm": speed_mm_s * duration_s}

    def stop_keyboard_alignment(self):
        self.stops += 1
        if self.step_release is not None:
            self.step_release.set()
        return {"stop_confirmed": self.stop_confirmed, "axes": {}, "errors": []}


def jogging(*, threads=False, on_blocked=None):
    motion, clock = JogMotion(), Clock()
    manager = AlignmentJog(motion, "session-1", clock=clock, start_threads=threads, on_blocked=on_blocked)
    return manager, motion, clock


def command(press_id="press-1", **fields):
    return {"session_token": "session-1", "press_id": press_id, **fields}


def start(manager, press_id="press-1", axis="X", direction="left"):
    return manager.start(command(press_id, axis=axis, direction=direction))


def test_defaults_and_semantic_direction_have_finite_single_segments():
    manager, motion, clock = jogging()
    assert manager.snapshot()["settings"]["speed_mm_s"] == 0.5
    assert manager.snapshot()["settings"]["max_speed_mm_s"] == 1
    start(manager)
    manager._step_once()
    assert len(motion.steps) == 1
    assert motion.steps[0]["direction"] == 0
    assert motion.steps[0]["speed_mm_s"] * motion.steps[0]["duration_s"] <= 0.1
    assert motion.steps[0]["duration_s"] <= 0.12


@pytest.mark.parametrize("axis,direction,motor_direction", [
    ("X", "left", 0), ("X", "right", 1), ("Z", "up", 1), ("Z", "down", 0)])
def test_physical_coordinate_words_map_to_driver_direction(axis, direction, motor_direction):
    manager, motion, clock = jogging()
    start(manager, axis=axis, direction=direction)
    manager._step_once()
    assert motion.steps[0]["direction"] == motor_direction


def test_lease_expiry_stops_and_late_heartbeat_or_start_cannot_revive():
    manager, motion, clock = jogging()
    start(manager)
    clock.now = 0.351
    manager._watch_once()
    assert motion.stops == 1
    assert manager.snapshot()["status"] == "idle"
    with pytest.raises(AutomationError):
        manager.heartbeat(command(sequence=1))
    with pytest.raises(AutomationError):
        start(manager)
    manager._step_once()
    assert motion.steps == []


def test_reordered_heartbeat_does_not_extend_lease():
    manager, motion, clock = jogging()
    start(manager)
    clock.now = 0.2
    manager.heartbeat(command(sequence=2))
    clock.now = 0.4
    with pytest.raises(AutomationError):
        manager.heartbeat(command(sequence=1))
    clock.now = 0.551
    manager._watch_once()
    assert motion.stops == 1


def test_stop_before_start_tombstones_and_duplicate_start_does_not_queue():
    manager, motion, clock = jogging()
    manager.stop(command("released-before-arrival"))
    with pytest.raises(AutomationError):
        start(manager, "released-before-arrival")
    start(manager)
    start(manager)
    assert manager.snapshot()["press_id"] == "press-1"
    manager._step_once()
    manager.stop(command())
    manager._step_once()
    assert len(motion.steps) == 1


def test_new_press_cannot_replace_active_press_or_queue():
    manager, motion, clock = jogging()
    start(manager)
    with pytest.raises(AutomationError):
        start(manager, "press-2", axis="Z", direction="up")
    manager._step_once()
    assert [step["axis"] for step in motion.steps] == ["X"]


def test_tombstone_capacity_rejects_new_ids_instead_of_evicting_old_release():
    manager, motion, clock = jogging()
    manager.MAX_PRESS_IDS = 2
    manager.stop(command("first"))
    manager.stop(command("second"))
    with pytest.raises(AutomationError):
        manager.stop(command("third"))
    with pytest.raises(AutomationError):
        start(manager, "first")
    assert motion.steps == []


@pytest.mark.parametrize("field,value", [
    ("speed_mm_s", True), ("speed_mm_s", float("nan")), ("speed_mm_s", 0.001),
    ("speed_mm_s", 1.1), ("max_speed_mm_s", float("inf")), ("max_speed_mm_s", 2.01),
    ("max_speed_mm_s", False)])
def test_settings_require_finite_nonboolean_bounded_speed_and_minimum_one_rpm(field, value):
    manager, motion, clock = jogging()
    with pytest.raises(AutomationError):
        manager.configure({"session_token": "session-1", field: value})


def test_settings_recheck_axis_maximum_and_change_requires_release():
    manager, motion, clock = jogging()
    motion.axes["Z"]["mechanical"]["max_speed_mm_s"] = 0.75
    with pytest.raises(AutomationError):
        manager.configure({"session_token": "session-1", "max_speed_mm_s": 1})
    motion.axes["Z"]["mechanical"]["max_speed_mm_s"] = 10
    start(manager)
    with pytest.raises(AutomationError):
        manager.configure({"session_token": "session-1", "speed_mm_s": 0.6})


def test_lease_expiry_still_requires_new_press_after_thirty_seconds():
    manager, motion, clock = jogging()
    start(manager)
    for sequence in range(1, 151):
        clock.now = sequence * 0.2
        manager.heartbeat(command(sequence=sequence))
    assert motion.stops == 0
    clock.now += 0.351
    manager._watch_once()
    assert motion.stops == 1
    with pytest.raises(AutomationError):
        start(manager)
    start(manager, "new-press")


def test_fine_hold_crosses_ten_millimeters_without_stopping_between_holds():
    manager, motion, clock = jogging()
    manager.configure({"session_token": "session-1", "speed_mm_s": 2, "max_speed_mm_s": 2})
    start(manager)
    for segment in range(120):
        manager._step_once()
    assert sum(step["speed_mm_s"] * step["duration_s"] for step in motion.steps) > 10
    assert manager.snapshot()["status"] == "holding"
    assert motion.stops == 0


def test_stop_cancels_and_issues_priority_stop_before_waiting_for_inflight_segment():
    manager, motion, clock = jogging(threads=True)
    motion.step_release = threading.Event()
    start(manager)
    assert motion.step_entered.wait(1)
    manager.stop(command())
    assert motion.steps[0]["cancel_event"].is_set()
    assert motion.stops == 1
    manager.close()


def test_unknown_stop_blocks_retains_session_and_callback_is_outside_manager_lock():
    notifications = []
    manager, motion, clock = jogging(on_blocked=lambda error, result: notifications.append((error, result)))
    motion.stop_confirmed = False
    start(manager)
    with pytest.raises(AutomationError):
        manager.stop(command())
    assert manager.snapshot()["status"] == "blocked"
    assert manager.snapshot()["session_token"] == "session-1"
    assert notifications[0][1]["stop_confirmed"] is False
    with pytest.raises(AutomationError):
        start(manager, "new-press")


def test_unknown_stop_preserves_original_serial_fault_in_diagnostics():
    from pa_host.emm_motion import MotionError

    manager, motion, clock = jogging()
    error = MotionError("response_length", "Unexpected serial response length")
    error.wire_diagnostic = {"pending_hex": "0100ee6b", "address": 2, "opcode": 0x36}
    error.stop_result = {"stop_confirmed": False, "axes": {}, "errors": []}
    with pytest.raises(AutomationError):
        manager._halt("fault", force=True, error=error)
    blocked = manager.snapshot()["error"]
    assert blocked["code"] == "alignment_stop_unknown"
    assert blocked["cause"]["code"] == "response_length"
    assert blocked["cause"]["wire"]["pending_hex"] == "0100ee6b"


def test_freeze_is_idempotent_blocks_new_start_and_can_only_resume_same_session():
    manager, motion, clock = jogging()
    start(manager)
    manager.freeze()
    assert manager.snapshot()["status"] == "frozen"
    manager.freeze()
    assert motion.stops == 1
    with pytest.raises(AutomationError):
        start(manager, "new-press")
    with pytest.raises(AutomationError):
        manager.resume("other-session")
    manager.resume("session-1")
    start(manager, "new-press")


@pytest.mark.parametrize("payload", [
    {"session_token": "other", "press_id": "press", "axis": "X", "direction": "left"},
    {"session_token": "session-1", "press_id": "press", "axis": "X", "direction": 0},
    {"session_token": "session-1", "press_id": "press", "axis": "Z", "direction": "left"},
    {"session_token": "session-1", "press_id": "press", "axis": "X", "direction": "left", "speed_mm_s": 2},
])
def test_requests_cannot_supply_driver_directions_override_speed_or_claim_another_session(payload):
    manager, motion, clock = jogging()
    with pytest.raises(AutomationError):
        manager.start(payload)
    assert motion.steps == []


def test_blocked_callback_never_holds_manager_lock():
    acquired = threading.Event()

    def notified(error, result):
        def read_snapshot():
            manager.snapshot()
            acquired.set()

        reader = threading.Thread(target=read_snapshot)
        reader.start()
        assert acquired.wait(1)
        reader.join(1)

    manager, motion, clock = jogging(on_blocked=notified)
    motion.stop_confirmed = False
    start(manager)
    with pytest.raises(AutomationError):
        manager.stop(command())


def test_driver_fault_stops_blocks_and_retains_confirmed_stop_evidence():
    from pa_host.emm_motion import MotionError

    notifications = []
    manager, motion, clock = jogging(on_blocked=lambda error, result: notifications.append((error, result)))

    def failed_step(*args, **kwargs):
        raise MotionError("profile_changed", "Fresh axis profile differs")

    motion.alignment_step = failed_step
    start(manager)
    with pytest.raises(AutomationError):
        manager._step_once()
    assert motion.stops == 1
    assert manager.snapshot()["status"] == "blocked"
    assert notifications[0][0]["code"] == "profile_changed"
    assert notifications[0][1]["stop_confirmed"] is True


def test_fine_hold_does_not_shorten_the_segment_near_the_old_time_limit():
    manager, motion, clock = jogging()
    start(manager)
    manager._active["started"] = -29.9999
    manager._step_once()
    assert len(motion.steps) == 1
    assert motion.steps[0]["duration_s"] == pytest.approx(0.12)
    assert motion.stops == 0
    assert manager.snapshot()["status"] == "holding"


def test_fault_uses_fresh_stop_evidence_from_driver_instead_of_losing_it_on_closed_session():
    from pa_host.emm_motion import MotionError

    notifications = []
    manager, motion, clock = jogging(on_blocked=lambda error, result: notifications.append((error, result)))
    stopped = {"stop_confirmed": True, "axes": {"X": {"stop_basis": "acknowledged_stop"}}, "errors": []}

    def failed_step(*args, **kwargs):
        error = MotionError("profile_changed", "Changed driver profile")
        error.stop_result = stopped
        raise error

    def closed_stop():
        raise MotionError("alignment_session", "The driver already stopped and closed its session")

    motion.alignment_step = failed_step
    motion.stop_keyboard_alignment = closed_stop
    start(manager)
    with pytest.raises(AutomationError):
        manager._step_once()
    assert notifications[0][1] == stopped


def test_idle_feedback_refreshes_at_half_second_intervals_outside_manager_lock():
    manager, motion, clock = jogging()
    refreshed = []

    def refresh():
        acquired = threading.Event()
        reader = threading.Thread(target=lambda: (manager.snapshot(), acquired.set()))
        reader.start()
        assert acquired.wait(1)
        reader.join(1)
        refreshed.append(clock.now)
        return motion.snapshot()

    motion.refresh = refresh
    clock.now = 0.49
    manager._watch_once(refresh_idle=True)
    assert refreshed == []
    clock.now = 0.5
    manager._watch_once(refresh_idle=True)
    clock.now = 0.99
    manager._watch_once(refresh_idle=True)
    clock.now = 1.0
    manager._watch_once(refresh_idle=True)
    assert refreshed == [0.5, 1.0]
    start(manager)
    clock.now = 1.2
    manager._watch_once(refresh_idle=True)
    assert refreshed == [0.5, 1.0]
    manager.freeze()
    clock.now = 2
    manager._watch_once(refresh_idle=True)
    assert refreshed == [0.5, 1.0]


def test_idle_refresh_motion_busy_skips_but_other_error_stops_and_blocks():
    from pa_host.emm_motion import MotionError

    notifications = []
    manager, motion, clock = jogging(on_blocked=lambda error, result: notifications.append((error, result)))

    def busy_refresh():
        raise MotionError("motion_busy", "An operation won the idle read race")

    motion.refresh = busy_refresh
    clock.now = 0.5
    manager._watch_once(refresh_idle=True)
    assert manager.snapshot()["status"] == "idle"
    assert motion.stops == 0

    def failed_refresh():
        raise MotionError("telemetry_lost", "Idle feedback is unavailable")

    motion.refresh = failed_refresh
    clock.now = 1
    with pytest.raises(AutomationError):
        manager._watch_once(refresh_idle=True)
    assert manager.snapshot()["status"] == "blocked"
    assert motion.stops == 1
    assert notifications[0][0]["code"] == "telemetry_lost"


def test_motion_busy_step_retries_without_spending_budget_or_faulting_session():
    from pa_host.emm_motion import MotionError

    manager, motion, clock = jogging()
    original_step = motion.alignment_step
    attempts = []

    def busy_once(*args, **kwargs):
        attempts.append(args)
        if len(attempts) == 1:
            raise MotionError("motion_busy", "Idle feedback still owns the operation")
        return original_step(*args, **kwargs)

    motion.alignment_step = busy_once
    start(manager)
    manager._step_once()
    assert manager.snapshot()["status"] == "holding"
    assert manager.snapshot()["distance_mm"] == 0
    assert motion.steps == []
    assert motion.stops == 0
    manager._step_once()
    assert len(motion.steps) == 1
    assert manager.snapshot()["distance_mm"] == pytest.approx(0.06)


def test_start_racing_idle_refresh_never_issues_fd_after_lease_expires():
    from pa_host.emm_motion import MotionError

    manager, motion, clock = jogging()
    entered, released = threading.Event(), threading.Event()

    def paused_refresh():
        entered.set()
        assert released.wait(2)
        return motion.snapshot()

    def busy_step(*args, **kwargs):
        raise MotionError("motion_busy", "The idle read is in progress")

    original_stop = motion.stop_keyboard_alignment

    def stop_and_release():
        released.set()
        return original_stop()

    motion.refresh = paused_refresh
    motion.alignment_step = busy_step
    motion.stop_keyboard_alignment = stop_and_release
    clock.now = 0.5
    refresher = threading.Thread(target=lambda: manager._watch_once(refresh_idle=True))
    refresher.start()
    assert entered.wait(1)
    start(manager)
    manager._step_once()
    assert manager.snapshot()["status"] == "holding"
    clock.now = 0.851
    manager._step_once()
    refresher.join(1)
    assert not refresher.is_alive()
    assert motion.steps == []
    assert motion.stops == 1
    assert manager.snapshot()["status"] == "idle"
    assert manager.snapshot()["last_stop_reason"] == "lease_expired"


def test_keyboard_worker_waits_for_idle_read_without_submitting_a_segment():
    manager, motion, clock = jogging()
    manager._idle_inflight = True
    start(manager)
    assert manager._step_once() is True
    assert motion.steps == []


def test_idle_failure_blocks_new_press_before_publishing_stop_result():
    from pa_host.emm_motion import MotionError

    manager, motion, clock = jogging()
    entered, released = threading.Event(), threading.Event()
    original_halt = manager._halt

    def failed_refresh():
        raise MotionError("transaction_timeout", "Idle read failed", stop_result={
            "stop_confirmed": True, "axes": {}, "errors": []})

    def paused_halt(*args, **options):
        entered.set()
        assert released.wait(2)
        return original_halt(*args, **options)

    errors = []

    def refresh_once():
        try:
            manager._watch_once(refresh_idle=True)
        except AutomationError as error:
            errors.append(error)

    motion.refresh = failed_refresh
    manager._halt = paused_halt
    clock.now = 0.5
    refresher = threading.Thread(target=refresh_once)
    refresher.start()
    try:
        assert entered.wait(1)
        with pytest.raises(AutomationError):
            start(manager)
        manager._step_once()
        assert motion.steps == []
    finally:
        released.set()
        refresher.join(2)
    assert not refresher.is_alive()
    assert errors
    assert manager.snapshot()["status"] == "blocked"
    assert manager.snapshot()["stop_result"]["stop_confirmed"] is True


@pytest.mark.parametrize("latest_confirmed", [False, True])
def test_concurrent_fault_retains_diagnostic_without_replacing_newer_stop_evidence(latest_confirmed):
    from pa_host.emm_motion import MotionError

    notifications = []
    manager, motion, clock = jogging(on_blocked=lambda error, result: notifications.append((error, result)))
    manager._blocked = manager._stopping = True
    manager._stop_result = {"stop_confirmed": latest_confirmed, "axes": {}, "errors": []}
    manager._stop_done.set()
    error = MotionError("checksum", "Idle response was malformed", stop_result={
        "stop_confirmed": not latest_confirmed, "axes": {}, "errors": []})
    error.wire_diagnostic = {"pending_hex": "0100ee6b"}
    with pytest.raises(AutomationError) as caught:
        manager._halt("fault", force=True, error=error)
    expected = "checksum" if latest_confirmed else "alignment_stop_unknown"
    assert caught.value.code == expected
    state = manager.snapshot()
    diagnostic = state["error"] if latest_confirmed else state["error"]["cause"]
    assert diagnostic["code"] == "checksum"
    assert diagnostic["wire"] == error.wire_diagnostic
    assert state["stop_result"]["stop_confirmed"] is latest_confirmed
    assert len(notifications) == 1


def test_freeze_issues_priority_stop_then_drains_idle_read_before_returning():
    manager, motion, clock = jogging()
    entered, released, stopped, frozen = (threading.Event() for _event in range(4))

    def paused_refresh():
        entered.set()
        assert released.wait(2)
        return motion.snapshot()

    def stop():
        stopped.set()
        return {"stop_confirmed": True, "axes": {}, "errors": []}

    motion.refresh = paused_refresh
    motion.stop_keyboard_alignment = stop
    clock.now = 0.5
    refresher = threading.Thread(target=lambda: manager._watch_once(refresh_idle=True))
    refresher.start()
    assert entered.wait(1)
    freezer = threading.Thread(target=lambda: (manager.freeze(), frozen.set()))
    freezer.start()
    assert stopped.wait(1)
    assert not frozen.wait(0.02)
    released.set()
    assert frozen.wait(1)
    refresher.join(1)
    freezer.join(1)
    assert manager.snapshot()["status"] == "frozen"
