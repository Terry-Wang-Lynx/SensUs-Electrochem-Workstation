"""Motion preflight and feedback checks derived from one experiment calibration."""

import copy
import math
import time

from .automation import AutomationError


def _finite(value):
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def _reject(message):
    raise AutomationError("calibration_motion", message, "calibration", 409)


def _bounds(record):
    if (not isinstance(record, dict) or record.get("reference_valid") is not True
            or set(record.get("points", {})) != {"first", "last", "working", "safe"}):
        _reject("A trusted current-session four-point calibration is required")
    fields = ("x_first_mm", "x_last_mm", "z_working_mm", "z_safe_mm")
    if not all(_finite(record.get(field)) for field in fields):
        _reject("Calibration coordinates must be finite")
    if record["x_first_mm"] == record["x_last_mm"] or record["z_safe_mm"] >= record["z_working_mm"]:
        _reject("Calibration needs distinct physical columns and a safe height above immersion")
    return {"X": sorted((record["x_first_mm"], record["x_last_mm"])),
            "Z": [record["z_safe_mm"], record["z_working_mm"]]}


def check_calibrated_feedback(motion, record):
    return _check_calibrated_feedback(motion, record, {"X", "Z"})


def check_calibrated_axis_feedback(motion, record, axis):
    if axis not in ("X", "Z"):
        _reject("Choose the active X or Z axis")
    return _check_calibrated_feedback(motion, record, {axis})


def calibrated_startup_approach(motion, record):
    bounds = _bounds(record)
    if not isinstance(motion, dict):
        _reject("当前电机反馈不可用，不能开始实验。")
    state = motion.get("axes", {}).get("Z", {})
    position = state.get("position_mm")
    lead = state.get("mechanical", {}).get("lead_mm")
    window = state.get("config", {}).get("position_window_deg")
    tolerance = lead * window / 360 if _finite(lead) and _finite(window) else 0
    if not _finite(position) or bounds["Z"][0] - tolerance <= position <= bounds["Z"][1] + tolerance:
        check_calibrated_feedback(motion, record)
        return None
    completion = record.get("completion_raw_counts", {})
    for axis in ("X", "Z"):
        current = motion.get("axes", {}).get(axis, {})
        expected = completion.get(axis)
        raw = current.get("raw_count")
        degrees = current.get("config", {}).get("position_window_deg")
        if (not _finite(expected) or not _finite(raw) or not _finite(degrees)
                or abs(raw - expected) > max(1, degrees * 65536 / 360)
                or current.get("speed_rpm") != 0 or not current.get("status", 0) & 2):
            _reject("当前位置不在四点实验区内，也不匹配完成标定时的静止位置；请重新进入对位确认，不能直接开始实验。")
    if position < 0:
        _reject("完成位置超出已确认的软件坐标范围，请重新标定。")
    approach = {"calibration_id": record["id"], "connection_token": record["connection_token"],
                "stop_generation": motion.get("stop_generation"), "reference_counts": copy.deepcopy(record["reference"]["reference_counts"]),
                "start_raw_counts": {axis: motion["axes"][axis]["raw_count"] for axis in ("X", "Z")},
                "start_z_mm": position, "target_z_mm": record["z_safe_mm"]}
    check_calibrated_approach_feedback(motion, record, approach)
    return approach


def check_calibrated_approach_feedback(motion, record, approach, observed_axes=None):
    if (approach["calibration_id"] != record["id"] or approach["connection_token"] != motion.get("connection_token")
            or approach["stop_generation"] != motion.get("stop_generation")
            or approach["reference_counts"] != record["reference"]["reference_counts"]
            or approach["target_z_mm"] != record["z_safe_mm"]):
        _reject("安全高度接近许可已失效，请重新确认标定与设备状态。")
    return _check_calibrated_feedback(motion, record, observed_axes or {"X", "Z"},
                                     z_approach=sorted((approach["start_z_mm"], approach["target_z_mm"])))


def _check_calibrated_feedback(motion, record, observed_axes, z_approach=None):
    bounds = _bounds(record)
    if (not isinstance(motion, dict) or motion.get("connected") is not True
            or motion.get("faulted") or motion.get("feedback_error") or motion.get("referenced") is not True
            or not record.get("connection_token") or record["connection_token"] != motion.get("connection_token")):
        _reject("The powered motion connection no longer matches the calibration")
    identity = {"port": motion.get("port"), "baud": motion.get("baud"), "axes": {}}
    profiles = {}
    now = time.monotonic()
    for axis in ("X", "Z"):
        state = motion.get("axes", {}).get(axis, {})
        identity["axes"][axis] = state.get("mechanical")
        profiles[axis] = {field: state.get(field) for field in
                          ("firmware_version", "hardware_version", "config", "power_flag_baseline")}
        if not all(state.get(field) is True for field in ("referenced", "enabled", "enable_confirmed")):
            _reject(f"{axis} must remain referenced, explicitly locked and healthy")
        if axis in observed_axes:
            if state.get("telemetry_valid") is not True:
                _reject(f"{axis} feedback is unavailable")
            age = state.get("feedback_age_s")
            if age is None and _finite(state.get("updated_monotonic")):
                age = now - state["updated_monotonic"]
            if not _finite(age) or not 0 <= age <= 2:
                _reject(f"{axis} feedback is stale")
        if state.get("reference_count") != record.get("reference", {}).get("reference_counts", {}).get(axis):
            _reject("The software reference changed after calibration")
        lead = state.get("mechanical", {}).get("lead_mm")
        window = state.get("config", {}).get("position_window_deg")
        if not _finite(lead) or lead <= 0 or not _finite(window) or not 0 <= window <= 1.8:
            _reject("The calibrated drive scale or arrival tolerance is invalid")
        tolerance = max(lead / 65536, lead * window / 360)
        position = state.get("position_mm")
        lower, upper = z_approach if axis == "Z" and z_approach is not None else bounds[axis]
        if not _finite(position) or not lower - tolerance <= position <= upper + tolerance:
            _reject(f"{axis} feedback left the four-point experiment window")
    if identity != record.get("motion") or profiles != record.get("reference", {}).get("profiles"):
        _reject("The complete motor identity or configuration changed")
    return bounds


def validate_calibrated_motion(compiled, motion, record):
    approach = calibrated_startup_approach(motion, record)
    bounds = _bounds(record)
    if approach is not None:
        first = compiled.get("steps", [None])[0] if compiled.get("steps") else None
        params = first.get("resolved_params", {}) if isinstance(first, dict) else {}
        if (not isinstance(first, dict) or first.get("op") != "move_z" or params.get("axis") != "Z"
                or params.get("purpose") != "safe_before_x" or params.get("target_mm") != record["z_safe_mm"]):
            _reject("当前位置不在记录的采集高度区间内；实物计划必须先沿 Z 到达安全高度，再横移或采集。")
    experiment = compiled["experiment"]
    if (experiment.get("schema_version") != "1.1" or experiment.get("run_policy") != "guided"
            or experiment.get("calibration_id") != record.get("id")
            or experiment.get("bindings", {}).get("motion") != record["motion"]):
        _reject("Use the current server-compiled calibrated template for live motion")
    for step in compiled["steps"]:
        if not step["op"].startswith("move_"):
            continue
        params = step["resolved_params"]
        axis, target = params.get("axis"), params.get("target_mm")
        if axis not in bounds or not _finite(target) or not bounds[axis][0] <= target <= bounds[axis][1]:
            _reject("A motion target exceeds the four-point experiment window")
    return copy.deepcopy(record)
