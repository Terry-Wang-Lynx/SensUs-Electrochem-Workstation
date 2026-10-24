from __future__ import annotations

import threading
import time

import pytest

from pa_host.emm_motion import MotionError
from test_emm_motion import config_reply
from test_powered_manual_driver import powered_bus


def keyboard_bus(*, enabled=True):
    bus, serial, events = powered_bus(enabled=enabled)
    started = bus.begin_keyboard_alignment(confirmed=True)
    serial.writes.clear()
    return bus, serial, events, started


@pytest.mark.parametrize("confirmed", [False, None, 1, "true"])
def test_begin_requires_literal_motion_confirmation_without_writes(confirmed):
    bus, serial, _ = powered_bus()
    with pytest.raises(MotionError) as caught:
        bus.begin_keyboard_alignment(confirmed=confirmed)
    assert caught.value.code == "keyboard_alignment_confirmation"
    assert serial.writes == []


def test_begin_default_never_enables_or_stops():
    bus, serial, _ = powered_bus()
    with pytest.raises(MotionError):
        bus.begin_keyboard_alignment()
    assert serial.writes == []


def test_begin_already_enabled_guards_both_targets_without_reenabling():
    bus, serial, _ = powered_bus(enabled=True)
    started = bus.begin_keyboard_alignment(confirmed=True)
    assert started["raw_counts"] == {"X": 65536, "Z": -32768}
    assert started["session_token"]
    assert bus.snapshot()["manual"]["control_mode"] == "keyboard"
    assert bus.snapshot()["manual"]["active"]
    assert all(state["enable_confirmed"] for state in bus.snapshot()["axes"].values())
    assert not bus.snapshot()["referenced"]
    assert {frame[1] for frame in serial.writes} <= {0xFE, 0x1F, 0x42, 0x1A, 0x33, 0x34, 0x36, 0x35, 0x3A}
    for address in (1, 2):
        for opcode in (0x33, 0x34):
            assert sum(frame[:2] == bytes([address, opcode]) for frame in serial.writes) == 2


def test_disabled_begin_checks_both_targets_twice_before_any_enable():
    bus, serial, _ = powered_bus()
    bus.begin_keyboard_alignment(confirmed=True)
    first_enable = next(index for index, frame in enumerate(serial.writes) if frame[1] == 0xF3)
    before_enable = serial.writes[:first_enable]
    for address in (1, 2):
        for opcode in (0x33, 0x34):
            assert sum(frame[:2] == bytes([address, opcode]) for frame in before_enable) == 2
    assert [frame for frame in serial.writes if frame[1] == 0xF3] == [
        bytes.fromhex("01f3ab01006b"), bytes.fromhex("02f3ab01006b")]


@pytest.mark.parametrize("failure", ["target", "profile", "unlock"])
def test_second_enable_rechecks_both_profiles_targets_and_existing_lock(failure):
    bus, serial, _ = powered_bus()

    def changed_after_first_enable(address):
        if address != 1:
            return
        if failure == "target":
            serial.targets[0x34][2] += 10000
        elif failure == "profile":
            changed = bytearray(config_reply(2))
            changed[9] = 32
            serial.overrides[2, 0x42] = bytes(changed)
        else:
            serial.statuses[1] &= ~1

    serial.on_enable = changed_after_first_enable
    with pytest.raises(MotionError):
        bus.begin_keyboard_alignment(confirmed=True)
    assert [frame[0] for frame in serial.writes if frame[1] == 0xF3] == [1]


@pytest.mark.parametrize("opcode", [0x33, 0x34])
def test_old_target_blocks_disabled_begin_before_f3(opcode):
    bus, serial, _ = powered_bus()
    serial.targets[opcode][2] += 10000
    with pytest.raises(MotionError) as caught:
        bus.begin_keyboard_alignment(confirmed=True)
    assert caught.value.code == "manual_target_mismatch"
    assert not any(frame[1] in (0xF3, 0xFD, 0xF6) for frame in serial.writes)
    assert bus.snapshot()["experiment_calibration"] is None


def test_begin_cannot_clear_fault_or_unknown_stop():
    bus, serial, _ = powered_bus()
    serial.overrides[1, 0xFE] = bytes.fromhex("01feee6b")
    assert bus.stop_all()["stop_confirmed"] is False
    serial.overrides.clear()
    serial.writes.clear()
    with pytest.raises(MotionError):
        bus.begin_keyboard_alignment(confirmed=True)
    assert serial.writes == []


def test_confirmed_begin_can_supersede_healthy_released_legacy_session():
    bus, serial, _ = powered_bus(enabled=True)
    bus.begin_manual_calibration(release_confirmed=True, z_supported=True)
    bus.stop_all()
    serial.writes.clear()
    started = bus.begin_keyboard_alignment(confirmed=True)
    assert started["session_token"]
    assert bus.snapshot()["manual"]["control_mode"] == "keyboard"
    assert serial.statuses == {1: 3, 2: 3}


@pytest.mark.parametrize("axis,direction,address,sign", [("X", 0, 1, 1), ("X", 1, 1, -1), ("Z", 0, 2, 1), ("Z", 1, 2, -1)])
def test_step_is_finite_relative_current_and_uses_physical_direction(axis, direction, address, sign):
    bus, serial, _, started = keyboard_bus()
    result = bus.alignment_step(axis, direction, 0.5, 0.12, 1.0)
    moves = [frame for frame in serial.writes if frame[1] == 0xFD]
    assert len(moves) == 1
    assert moves[0] == bytes([address, 0xFD, direction, 0, 15, 10, 0, 0, 0, 96, 2, 0, 0x6B])
    assert result["raw_counts"][axis] == started["raw_counts"][axis] + sign * 1966
    assert result["segment_mm"] == pytest.approx(0.06)
    assert result["command_speed_mm_s"] == pytest.approx(0.5)
    assert not any(frame[1] in (0xF3, 0xF6, 0x0A, 0x93, 0x9A) for frame in serial.writes)


def test_fast_step_caps_committed_displacement_and_floors_rpm():
    bus, serial, _, _ = keyboard_bus()
    result = bus.alignment_step("X", 0, 1.99, 0.12, 2.0)
    move = next(frame for frame in serial.writes if frame[1] == 0xFD)
    assert int.from_bytes(move[3:5], "big") == 59
    assert int.from_bytes(move[6:10], "big") == 160
    assert result["segment_mm"] == pytest.approx(0.1)
    assert result["duration_s"] <= 0.12


def test_floor_rpm_never_exceeds_just_below_representable_operator_cap():
    bus, serial, _, _ = keyboard_bus()
    with pytest.raises(MotionError) as caught:
        bus.alignment_step("X", 0, 1 / 30 - 1e-15, 0.12, 1 / 30 - 1e-15)
    assert caught.value.code == "speed_range"
    assert not any(frame[1] == 0xFD for frame in serial.writes)


def test_tiny_segment_cannot_complete_on_stale_reached_feedback_before_nominal_duration():
    bus, serial, _, _ = keyboard_bus()
    serial.move_completes = False
    serial.on_move = lambda: serial.statuses.update({1: 3})
    started = time.monotonic()
    result = bus.alignment_step("X", 0, 1 / 30, 0.12, 1)
    assert result["duration_s"] == pytest.approx(0.1125)
    assert time.monotonic() - started >= 0.1125


def test_segment_timeout_stops_both_axes_and_invalidates_session():
    bus, serial, _, _ = keyboard_bus()
    bus._move_timeout = 0.001
    serial.move_completes = False
    with pytest.raises(MotionError) as caught:
        bus.alignment_step("X", 0, 0.5, 0.12, 1)
    assert caught.value.code == "move_timeout"
    assert caught.value.stop_result["stop_confirmed"]
    assert not bus.snapshot()["manual"]["active"]


@pytest.mark.parametrize("axis,direction,speed,duration,maximum", [
    ("X", 0, 0.01, 0.12, 1), ("X", 0, 2.01, 0.12, 3),
    ("X", 0, 0.6, 0.12, 0.5), ("X", 0, 0.5, 0.121, 1),
    ("X", 0, 0.5, 0, 1), ("X", True, 0.5, 0.12, 1),
    ("X", 2, 0.5, 0.12, 1), ("Y", 0, 0.5, 0.12, 1),
    ("X", 0, float("nan"), 0.12, 1), ("X", 0, 0.5, 0.12, False),
])
def test_invalid_step_never_commits_motion(axis, direction, speed, duration, maximum):
    bus, serial, _, _ = keyboard_bus()
    with pytest.raises(MotionError):
        bus.alignment_step(axis, direction, speed, duration, maximum)
    assert not any(frame[1] in (0xFD, 0xF3, 0xF6) for frame in serial.writes)


def test_step_checks_fresh_profiles_before_fd():
    bus, serial, _, _ = keyboard_bus()
    changed = bytearray(config_reply(2))
    changed[9] = 32
    serial.overrides[2, 0x42] = bytes(changed)
    with pytest.raises(MotionError) as caught:
        bus.alignment_step("X", 0, 0.5, 0.12, 1)
    assert caught.value.code == "manual_profile_changed"
    assert not any(frame[1] == 0xFD for frame in serial.writes)
    assert caught.value.stop_result["stop_confirmed"]


@pytest.mark.parametrize("failure", ["disabled", "target", "held_move", "overshoot", "profile_during_move"])
def test_step_faults_and_stops_both_for_unsafe_feedback(failure):
    bus, serial, _, _ = keyboard_bus()
    if failure == "disabled":
        serial.statuses[2] = 2
    elif failure == "target":
        serial.targets[0x34][1] += 10000
    elif failure == "held_move":
        serial.on_move = lambda: serial.positions.update({2: serial.positions[2] + 1000})
    elif failure == "overshoot":
        serial.on_move = lambda: serial.positions.update({1: serial.positions[1] + 5000})
    else:
        def change_profile():
            changed = bytearray(config_reply(2))
            changed[9] = 32
            serial.overrides[2, 0x42] = bytes(changed)
        serial.on_move = change_profile
    with pytest.raises(MotionError) as caught:
        bus.alignment_step("X", 0, 0.5, 0.12, 1)
    assert caught.value.stop_result["stop_confirmed"]
    assert {frame[0] for frame in serial.writes if frame[1] == 0xFE} == {1, 2}
    assert not bus.snapshot()["manual"]["active"]


def test_step_respects_explicit_operator_bounds_without_claiming_mechanical_ends():
    bus, serial, _, _ = keyboard_bus()
    bus._manual["direction_bounds"] = {"X": {"upper": serial.positions[1] + 150 * 32768}}
    serial.positions[1] += 150 * 32768
    for targets in serial.targets.values():
        targets.update(serial.positions)
    result = bus.alignment_step("X", 0, 0.5, 0.12, 1)
    assert result["stop_reason"] == "travel_limit"
    assert result["hold_exhausted"]
    assert not bus.snapshot()["faulted"]
    assert not any(frame[1] == 0xFD for frame in serial.writes)


def test_feedback_outside_explicit_operator_bounds_is_still_a_real_fault():
    bus, serial, _, _ = keyboard_bus()
    bus._manual["direction_bounds"] = {"X": {"upper": serial.positions[1] + 150 * 32768}}
    serial.positions[1] += 151 * 32768
    for targets in serial.targets.values():
        targets.update(serial.positions)
    with pytest.raises(MotionError) as caught:
        bus.alignment_step("X", 1, 0.5, 0.12, 1)
    assert caught.value.code == "travel_limit"
    assert bus.snapshot()["faulted"]
    assert not any(frame[1] == 0xFD for frame in serial.writes)


def test_cancellation_stops_both_and_keeps_owned_locked_session():
    bus, serial, _, started = keyboard_bus()
    cancelled = threading.Event()
    serial.on_move = cancelled.set
    with pytest.raises(MotionError) as caught:
        bus.alignment_step("X", 0, 0.5, 0.12, 1, cancel_event=cancelled)
    assert caught.value.code == "cancelled"
    assert caught.value.stop_result["stop_confirmed"]
    assert bus.snapshot()["manual"]["active"]
    assert bus.capture_keyboard_alignment()["session_token"] == started["session_token"]
    assert not any(frame[1] == 0xF3 for frame in serial.writes)


def test_owned_key_release_stop_preserves_generation_but_global_stop_invalidates():
    bus, serial, _, started = keyboard_bus()
    assert bus.stop_keyboard_alignment()["stop_confirmed"]
    captured = bus.capture_keyboard_alignment()
    assert captured["session_token"] == started["session_token"]
    assert captured["stop_generation"] > started["stop_generation"]
    bus.alignment_step("X", 0, 0.5, 0.12, 1)
    bus.stop_all()
    with pytest.raises(MotionError):
        bus.capture_keyboard_alignment()
    assert not bus.snapshot()["manual"]["active"]
    assert not any(frame[1] == 0xF3 for frame in serial.writes)


def test_concurrent_global_stop_cannot_be_resurrected_by_owned_stop(monkeypatch):
    bus, _, _, _ = keyboard_bus()
    stopped = threading.Event()
    release = threading.Event()
    original_read = bus._read_axis

    def paused_read(axis, **options):
        result = original_read(axis, **options)
        if threading.current_thread().name == "owned-stop" and not stopped.is_set():
            stopped.set()
            assert release.wait(2)
        return result

    monkeypatch.setattr(bus, "_read_axis", paused_read)
    results = []
    owned = threading.Thread(target=lambda: results.append(bus.stop_keyboard_alignment()), name="owned-stop")
    owned.start()
    assert stopped.wait(2)
    global_stop = threading.Thread(target=bus.stop_all)
    global_stop.start()
    release.set()
    owned.join(2)
    global_stop.join(2)
    assert not owned.is_alive() and not global_stop.is_alive()
    assert not bus.snapshot()["manual"]["active"]
    with pytest.raises(MotionError):
        bus.alignment_step("X", 0, 0.5, 0.12, 1)


def test_capture_and_finish_remain_locked_and_only_install_software_reference():
    bus, serial, _, started = keyboard_bus()
    captured = bus.capture_keyboard_alignment()
    assert captured["raw_counts"] == started["raw_counts"]
    result = bus.finish_keyboard_alignment(captured["raw_counts"])
    assert result["reference_counts"] == captured["raw_counts"]
    assert bus.snapshot()["position"] == {"X": 0, "Z": 0}
    assert bus.snapshot()["referenced"]
    assert bus.snapshot()["manual"]["status"] == "complete"
    assert not any(frame[1] in (0xF3, 0xFD, 0xF6, 0x0A, 0x93, 0x9A) for frame in serial.writes)


def test_cancel_remains_locked_without_references_or_motion():
    bus, serial, _, _ = keyboard_bus()
    result = bus.cancel_keyboard_alignment()
    assert result["manual"]["status"] == "cancelled"
    assert not result["manual"]["active"]
    assert not result["referenced"]
    assert serial.statuses == {1: 3, 2: 3}
    assert not any(frame[1] in (0xF3, 0xFD, 0xF6) for frame in serial.writes)


def test_cancelling_inflight_step_and_priority_owned_stop_keeps_session(monkeypatch):
    bus, serial, _, started = keyboard_bus()
    cancelled = threading.Event()
    moving = threading.Event()
    release = threading.Event()
    original_read = bus._read_axis

    def paused_read(axis, **options):
        if (threading.current_thread().name == "moving-keyboard-step" and not moving.is_set()
                and any(frame[1] == 0xFD for frame in serial.writes)):
            moving.set()
            assert release.wait(2)
        return original_read(axis, **options)

    monkeypatch.setattr(bus, "_read_axis", paused_read)
    errors = []

    def step():
        try:
            bus.alignment_step("X", 0, 0.5, 0.12, 1, cancel_event=cancelled)
        except MotionError as error:
            errors.append(error)

    worker = threading.Thread(target=step, name="moving-keyboard-step")
    worker.start()
    assert moving.wait(2)
    cancelled.set()
    stopped = bus.stop_keyboard_alignment()
    release.set()
    worker.join(2)
    assert not worker.is_alive()
    assert stopped["stop_confirmed"]
    assert len(errors) == 1 and errors[0].code == "cancelled"
    assert bus.capture_keyboard_alignment()["session_token"] == started["session_token"]
    assert not any(frame[1] == 0xF3 for frame in serial.writes)


def test_completed_owned_stop_permanently_cancels_old_preflight_before_fd(monkeypatch):
    bus, serial, _, started = keyboard_bus()
    preflight_read = threading.Event()
    release = threading.Event()
    original_states = bus._keyboard_states

    def paused_states(**options):
        result = original_states(**options)
        if threading.current_thread().name == "old-preflight":
            preflight_read.set()
            assert release.wait(2)
        return result

    monkeypatch.setattr(bus, "_keyboard_states", paused_states)
    errors = []

    def step():
        try:
            bus.alignment_step("X", 0, 0.5, 0.12, 1)
        except MotionError as error:
            errors.append(error)

    worker = threading.Thread(target=step, name="old-preflight")
    worker.start()
    assert preflight_read.wait(2)
    assert bus.stop_keyboard_alignment()["stop_confirmed"]
    release.set()
    worker.join(2)
    assert not worker.is_alive()
    assert len(errors) == 1 and errors[0].code == "cancelled"
    assert not any(frame[1] == 0xFD for frame in serial.writes)
    assert bus.capture_keyboard_alignment()["session_token"] == started["session_token"]


def test_owned_stop_during_begin_cannot_reauthorize_incomplete_session(monkeypatch):
    bus, serial, _ = powered_bus(enabled=True)
    guarded = threading.Event()
    release = threading.Event()
    original_states = bus._keyboard_states

    def paused_states(**options):
        result = original_states(**options)
        if not guarded.is_set():
            guarded.set()
            assert release.wait(2)
        return result

    monkeypatch.setattr(bus, "_keyboard_states", paused_states)
    errors = []

    def begin():
        try:
            bus.begin_keyboard_alignment(confirmed=True)
        except MotionError as error:
            errors.append(error)

    worker = threading.Thread(target=begin)
    worker.start()
    assert guarded.wait(2)
    assert bus.stop_keyboard_alignment()["stop_confirmed"]
    release.set()
    worker.join(2)
    assert not worker.is_alive()
    assert len(errors) == 1
    assert not bus.snapshot()["manual"]["active"]
    assert not any(frame[1] == 0xF3 for frame in serial.writes)


@pytest.mark.parametrize("operation", ["capture", "finish", "cancel"])
def test_owned_stop_invalidates_old_inflight_terminal_operation(monkeypatch, operation):
    bus, _, _, started = keyboard_bus()
    guarded = threading.Event()
    release = threading.Event()
    original_states = bus._keyboard_states

    def paused_states(**options):
        result = original_states(**options)
        guarded.set()
        assert release.wait(2)
        return result

    monkeypatch.setattr(bus, "_keyboard_states", paused_states)
    errors = []

    def terminal_operation():
        try:
            if operation == "capture":
                bus.capture_keyboard_alignment()
            elif operation == "finish":
                bus.finish_keyboard_alignment(started["raw_counts"])
            else:
                bus.cancel_keyboard_alignment()
        except MotionError as error:
            errors.append(error)

    worker = threading.Thread(target=terminal_operation)
    worker.start()
    assert guarded.wait(2)
    assert bus.stop_keyboard_alignment()["stop_confirmed"]
    release.set()
    worker.join(2)
    assert not worker.is_alive()
    assert len(errors) == 1 and errors[0].code == "cancelled"
    assert not bus.snapshot()["referenced"]


@pytest.mark.parametrize("operation", ["capture", "finish", "cancel"])
def test_keyboard_terminal_operations_reject_unlocked_or_moving_axis(operation):
    bus, serial, _, started = keyboard_bus()
    serial.statuses[2] = 2
    with pytest.raises(MotionError):
        if operation == "capture":
            bus.capture_keyboard_alignment()
        elif operation == "finish":
            bus.finish_keyboard_alignment(started["raw_counts"])
        else:
            bus.cancel_keyboard_alignment()
    assert not any(frame[1] in (0xF3, 0xFD, 0xF6) for frame in serial.writes)
