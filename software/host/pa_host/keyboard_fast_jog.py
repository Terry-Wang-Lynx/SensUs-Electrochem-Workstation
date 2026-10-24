"""Finite position lookahead for held-key coarse alignment."""

import copy
import math
import threading
import time

from .emm_motion import MotionError, _MotionCancellation, _number


FAST_MAX_SPEED = 20.0
FAST_LOOKAHEAD_MM = 6.0
FAST_ACCELERATION = 250
FAST_SETTLING_RESERVE_MM = 0.1


def _target_representation_budget(counts_per_pulse):
    return counts_per_pulse / 2 + 2


def _finite_envelope(start_count, committed, hard_end, reserve_counts, sign, tolerance):
    envelope_end = committed + sign * reserve_counts
    lower = start_count - tolerance if sign > 0 else max(hard_end, envelope_end)
    upper = min(hard_end, envelope_end) if sign > 0 else start_count + tolerance
    return lower, upper


def _audit_stopped_axes(bus, axis, initial, generation, envelope=None, cancel_event=None):
    try:
        bus._check_cancel(cancel_event)
        states = bus._keyboard_stopped_states(generation=generation, interruptible=True,
                                              cancel_event=cancel_event)
        bus._check_cancel(cancel_event)
        bus._keyboard_session(generation)
    except MotionError:
        bus._check_cancel(cancel_event)
        raise
    if envelope is not None and not envelope[0] <= states[axis]["raw_count"] <= envelope[1]:
        raise MotionError("calibration_motion", "Stopped fast alignment left its committed finite envelope")
    for held_axis, held in states.items():
        if held_axis != axis and not bus._keyboard_held_matches(initial[held_axis], held):
            raise MotionError("calibration_motion", "Held axis changed during fast alignment")
    return states


def run_fast_alignment(bus, axis, direction, speed_mm_s, max_speed_mm_s, max_distance_mm, duration_s,
                       cancel_event=None):
    config = bus._axis(axis)
    if isinstance(direction, bool) or not isinstance(direction, int) or direction not in (0, 1):
        raise MotionError("invalid_direction", "Keyboard motor direction must be 0 or 1")
    speed = _number(speed_mm_s, "speed_mm_s", positive=True)
    maximum = _number(max_speed_mm_s, "max_speed_mm_s", positive=True)
    continuous = max_distance_mm is None and duration_s is None
    if continuous and not callable(getattr(cancel_event, "is_set", None)):
        raise MotionError("alignment_cancellation_required", "Continuous alignment requires an owner cancellation signal")
    distance = None if continuous else _number(max_distance_mm, "max_distance_mm", positive=True)
    duration = None if continuous else _number(duration_s, "duration_s", positive=True)
    rpm = math.floor(speed * 60 / config["lead_mm"])
    if (not 2 < speed <= min(maximum, config["max_speed_mm_s"], FAST_MAX_SPEED)
            or maximum > FAST_MAX_SPEED or not 1 <= rpm <= 3000):
        raise MotionError("speed_range", "Fast alignment requires >2..20 mm/s within the configured axis limit")
    if not continuous and (distance > 10 or duration > 30):
        raise MotionError("move_duration", "A fast hold is bounded to 10 mm and 30 seconds")
    with bus._operation():
        bus._keyboard_session()
        with bus._state_lock:
            generation = bus._stop_generation
            segment_cancel = threading.Event()
            bus._keyboard_segment_cancel = segment_cancel
            bus._keyboard_segment_started = time.monotonic()
        cancellation = _MotionCancellation(segment_cancel, cancel_event)
        deadline = None if continuous else time.monotonic() + duration
        initial = None
        envelope = None
        try:
            initial = bus._keyboard_start_states(axis, generation=generation, cancel_event=cancellation, interruptible=True)
            state = initial[axis]
            start_count = state["raw_count"]
            sign = -1 if direction else 1
            counts_per_pulse = 65536 / state["config"]["pulses_per_rev"]
            target_representation_budget = _target_representation_budget(counts_per_pulse)
            pulse_mm = config["lead_mm"] / state["config"]["pulses_per_rev"]
            tolerance = max(1.0, state["config"]["position_window_deg"] * 65536 / 360)
            baseline = state["input_target_count"] if bus._responsive_keyboard else bus._read_manual_target(axis, 0x33)
            bus._check_cancel(cancellation)
            bus._keyboard_session(generation)
            if abs(baseline - start_count) > tolerance:
                raise MotionError("manual_target_mismatch", "Fast alignment target no longer matches the encoder")
            if continuous:
                origins = bus._manual.get("start_counts")
                if not isinstance(origins, dict) or axis not in origins:
                    raise MotionError("manual_calibration_session", "Continuous alignment requires the current session origin")
                hard_end, available = bus._keyboard_direction_budget(axis, direction, state)
                distance = sign * (hard_end - start_count) * config["lead_mm"] / 65536
                if available < 2 * pulse_mm:
                    return {**bus._keyboard_position(initial), "segment_mm": 0, "hold_exhausted": True,
                            "stop_reason": "travel_limit"}
            else:
                hard_end = start_count + sign * math.floor(distance / pulse_mm) * counts_per_pulse
            reserve_mm = FAST_SETTLING_RESERVE_MM if continuous else min(FAST_SETTLING_RESERVE_MM, distance / 2, FAST_LOOKAHEAD_MM / 2)
            reserve_counts = reserve_mm * 65536 / config["lead_mm"]
            requested_end = hard_end - sign * reserve_counts
            total_pulses = math.floor(sign * (requested_end - baseline) / counts_per_pulse + 1e-12)
            if total_pulses < 1:
                raise MotionError("pulse_range", "Fast hold distance is below one pulse")
            final_target = baseline + sign * total_pulses * counts_per_pulse
            proposed = copy.deepcopy(initial)
            proposed[axis]["raw_count"] = hard_end
            bus._keyboard_bounds(proposed)
            committed = baseline
            envelope = (start_count - tolerance, start_count + tolerance)
            submitted = 0
            quiet_since = None
            quiet_count = None
            current = state
            while True:
                bus._check_cancel(cancellation)
                bus._keyboard_session(generation)
                if deadline is not None and time.monotonic() >= deadline:
                    stopped = bus.stop_keyboard_alignment()
                    if not stopped["stop_confirmed"]:
                        raise MotionError("stop_unconfirmed", "Fast hold time limit STOP was not confirmed", stop_result=stopped)
                    states = _audit_stopped_axes(bus, axis, initial, bus._stop_generation, envelope)
                    with bus._state_lock:
                        bus._check_cancel()
                        stopped = bus._current_stop_result()
                        if not stopped["stop_confirmed"]:
                            raise MotionError("cancelled", "A newer STOP has not completed", stop_result=stopped)
                        return {"segment_mm": max(0, sign * (states[axis]["raw_count"] - start_count) * config["lead_mm"] / 65536),
                                "hold_exhausted": True, "stop_result": stopped}
                if submitted:
                    current = bus._read_axis(axis, cancel_event=cancellation, interruptible=True)
                    bus._check_cancel(cancellation)
                    bus._keyboard_session(generation)
                    bus._healthy(axis, current, require_enabled=True)
                    bus._keyboard_bounds({axis: current})
                    lower, upper = envelope
                    if not lower <= current["raw_count"] <= upper:
                        bus._event("keyboard_fast_envelope_violation", axis=axis, raw_count=current["raw_count"],
                                   committed_count=committed, lower_count=lower, upper_count=upper,
                                   hard_limit_count=hard_end)
                        raise MotionError("calibration_motion", "Fast alignment left its committed finite envelope")
                if submitted < total_pulses:
                    ahead_mm = sign * (committed - current["raw_count"]) * config["lead_mm"] / 65536
                    if not submitted or ahead_mm <= 4:
                        margin_mm = tolerance * config["lead_mm"] / 65536 + pulse_mm
                        allowed = current["raw_count"] + sign * (FAST_LOOKAHEAD_MM - reserve_mm - margin_mm) * 65536 / config["lead_mm"]
                        extra = min(total_pulses - submitted,
                                    math.floor(sign * (allowed - committed) / counts_per_pulse + 1e-12))
                        if extra > 0:
                            bus._check_cancel(cancellation)
                            body = (bytes([config["address"], 0xFD, direction]) + rpm.to_bytes(2, "big")
                                    + bytes([FAST_ACCELERATION]) + extra.to_bytes(4, "big") + bytes([0, 0]))
                            pending_target = baseline + sign * (submitted + extra) * counts_per_pulse
                            envelope = _finite_envelope(start_count, pending_target, hard_end, reserve_counts, sign, tolerance)
                            with bus._state_lock:
                                bus._keyboard_session(generation)
                                bus._manual["polling_axis"] = axis
                            bus._transact(body, 4, ack=True, cancel_event=cancellation, interruptible=True)
                            submitted += extra
                            committed = baseline + sign * submitted * counts_per_pulse
                            actual_target = bus._read_manual_target(axis, 0x33)
                            bus._check_cancel(cancellation)
                            bus._keyboard_session(generation)
                            if abs(actual_target - committed) > target_representation_budget:
                                bus._event("keyboard_fast_target_mismatch", axis=axis, expected_count=committed,
                                           actual_count=actual_target, error_count=actual_target - committed,
                                           representation_budget_counts=target_representation_budget,
                                           submitted_pulses=submitted)
                                raise MotionError("manual_target_mismatch", "Drive target differs from the fast alignment finite target")
                            bus._event("keyboard_fast_target", axis=axis, committed_count=committed,
                                       actual_target_count=actual_target, target_error_count=actual_target - committed,
                                       representation_budget_counts=target_representation_budget,
                                       lookahead_limit_mm=FAST_LOOKAHEAD_MM, submitted_mm=submitted * pulse_mm,
                                       settling_reserve_mm=reserve_mm, hard_limit_count=hard_end,
                                       command_speed_mm_s=rpm * config["lead_mm"] / 60)
                    quiet_since = quiet_count = None
                else:
                    reached = (current["speed_rpm"] == 0 and bool(current["status"] & 2)
                               and abs(current["raw_count"] - final_target) <= tolerance)
                    if not reached:
                        quiet_since = quiet_count = None
                    elif quiet_count is None or abs(current["raw_count"] - quiet_count) > bus._encoder_stability_counts(current):
                        quiet_since, quiet_count = time.monotonic(), current["raw_count"]
                    if reached and quiet_since is not None and time.monotonic() - quiet_since >= 0.2:
                        bus._transact(bytes([config["address"], 0xFE, 0x98, 0]), 4, ack=True,
                                      cancel_event=cancellation, interruptible=True)
                        states = _audit_stopped_axes(bus, axis, initial, generation, envelope,
                                                    cancel_event=cancellation)
                        bus._check_cancel(cancellation)
                        bus._keyboard_session(generation)
                        for held_axis, held in states.items():
                            if held_axis != axis and not bus._keyboard_held_matches(initial[held_axis], held):
                                raise MotionError("calibration_motion", "Held axis changed during final fast alignment audit")
                        if abs(states[axis]["raw_count"] - final_target) > tolerance:
                            raise MotionError("calibration_motion", "Fast alignment left its final target")
                        return {**bus._keyboard_position(states), "segment_mm": total_pulses * pulse_mm,
                                "command_speed_mm_s": rpm * config["lead_mm"] / 60, "hold_exhausted": True,
                                "stop_reason": "travel_limit" if continuous else "hold_limit"}
                bus._stop_event.wait(0.01)
        except MotionError as error:
            if error.code == "cancelled":
                if generation == bus._stop_generation:
                    error.stop_result = bus.stop_keyboard_alignment()
                else:
                    with bus._keyboard_stop_lock:
                        error.stop_result = bus._current_stop_result()
                if initial is not None and error.stop_result and error.stop_result.get("stop_confirmed") is True:
                    with bus._keyboard_stop_lock:
                        if bus._manual.get("active") and not bus._faulted:
                            try:
                                _audit_stopped_axes(bus, axis, initial, bus._stop_generation, envelope)
                            except MotionError as audit_error:
                                if audit_error.code != "cancelled":
                                    bus._keyboard_failure(audit_error)
                                    raise
                error.stop_result = bus._current_stop_result()
            else:
                bus._keyboard_failure(error)
            raise
        finally:
            with bus._state_lock:
                if bus._keyboard_segment_cancel is segment_cancel:
                    bus._keyboard_segment_cancel = None
                    bus._keyboard_segment_started = None
                    bus._manual.pop("polling_axis", None)
