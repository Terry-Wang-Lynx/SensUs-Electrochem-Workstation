"""Cached communication status without opening or polling physical devices."""

from __future__ import annotations

import copy
import math
import time


FEEDBACK_FRESH_SECONDS = 2.0


def calibration_status(guided, motion):
    required = guided.get("four_point_required") is True
    active = guided.get("manual", {}).get("active") is True
    record = guided.get("calibration")
    valid, stationary = False, False
    try:
        identity = {"port": motion.get("port"), "baud": motion.get("baud"), "axes": {}}
        profiles = {}
        for axis in ("X", "Z"):
            state = motion["axes"][axis]
            identity["axes"][axis] = state["mechanical"]
            profiles[axis] = {field: state.get(field) for field in
                              ("firmware_version", "hardware_version", "config", "power_flag_baseline")}
        valid = bool(record and record.get("usable") is True and record.get("four_point") is True
                     and not active and set(record["points"]) == {"first", "last", "working", "safe"}
                     and motion.get("connected") is True and not motion.get("faulted") and not motion.get("feedback_error")
                     and record.get("connection_token") == motion.get("connection_token") and record.get("connection_token")
                     and record["motion"] == identity and record["reference"]["profiles"] == profiles)
        for axis in ("X", "Z"):
            state = motion["axes"][axis]
            age = state.get("feedback_age_s")
            valid = bool(valid and state.get("telemetry_valid") is True and state.get("enabled") is True
                         and not isinstance(age, bool) and isinstance(age, (int, float))
                         and math.isfinite(age) and 0 <= age <= FEEDBACK_FRESH_SECONDS)
            if motion.get("referenced") is True:
                valid = valid and state.get("reference_count") == record["reference"]["reference_counts"][axis]
            else:
                valid = valid and abs(state["raw_count"] - record["reference"]["raw_counts"][axis]) <= 1
        stationary = all(motion["axes"][axis].get("speed_rpm") == 0 for axis in ("X", "Z"))
    except (KeyError, TypeError, ValueError, AttributeError):
        valid = False
    ready = bool(valid and stationary)
    state = "calibrating" if active else "ready" if ready else "stale" if record else "missing"
    return {"required": required, "ready": ready, "display_valid": bool(valid), "state": state,
            "points": ["first", "last", "working", "safe"]}


def motion_status(snapshot, feedback_error=None):
    result = copy.deepcopy(snapshot)
    result["feedback_error"] = copy.deepcopy(feedback_error)
    result["faulted"] = bool(result.get("faulted") or feedback_error)
    now = time.monotonic()
    for axis in result.get("axes", {}).values():
        updated = axis.get("updated_monotonic")
        valid_time = (isinstance(updated, (int, float)) and not isinstance(updated, bool)
                      and math.isfinite(updated) and updated <= now)
        age = now - updated if valid_time else None
        axis["feedback_age_s"] = age
        axis["telemetry_valid"] = bool(axis.get("telemetry_valid") and result.get("connected")
                                       and not result["faulted"] and age is not None
                                       and age <= FEEDBACK_FRESH_SECONDS)
    return result


def _address(binding):
    value = binding.get("address", "") if isinstance(binding, dict) else binding
    return value if isinstance(value, str) else ""


def board_status(bindings, runtime, *, synthetic, active):
    result = {}
    for slot, channels in (("A", "ABCD"), ("B", "EFGH")):
        address = _address(bindings.get(slot, ""))
        state = "bound_unverified" if address else "unbound"
        board = runtime.get(slot, {})
        if address and not synthetic and not board.get("synthetic") and board.get("address") == address:
            phase = board.get("phase")
            if phase == "error" or not active and (board.get("output_state") == "unknown"
                                                    or board.get("stop_confirmed") is False):
                state = "error"
            elif active and phase == "running" and board.get("start_ack_unix_s") is not None:
                state = "acquiring"
            elif active and phase in ("configuring", "retrying", "reconnecting"):
                state = "reconnecting"
            elif phase in ("running", "stopped", "completed"):
                state = "stopped_unverified"
        result[slot] = {"address": address, "state": state, "channel_ids": list(channels)}
    return result
