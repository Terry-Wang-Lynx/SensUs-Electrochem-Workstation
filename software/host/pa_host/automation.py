"""Declarative, bounded experiment plans and audited automation ownership."""

from __future__ import annotations

import copy
import csv
import hashlib
import json
import math
import os
import subprocess
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import automation_devices, runtime, v52_ble
from .experiment_progress import Progress

API_PREFIX = "/api/automation/v1"
CHANNELS = "ABCDEFGH"
MAX_STEPS = 500
MAX_SECONDS = 8 * 3600
GATE_TIMEOUT_S = 55.0
STOP_TIMEOUT_S = 15.0
POLL_SECONDS = 0.05
GUIDED_RETRY_BACKOFF_S = 1.0
PARAMETERS = {"descend_speed_mm_s", "ascend_speed_mm_s", "translate_speed_mm_s", "dwell_s",
              "settle_s", "z_safe_mm", "z_immersed_mm", "x_first_mm", "column_pitch_mm", "channels"}


class AutomationError(ValueError):
    def __init__(self, code, message, path="", status=400):
        super().__init__(message)
        self.code, self.message, self.path, self.status = code, message, path, status
        self.request_id = uuid.uuid4().hex

    def as_dict(self):
        result = {"code": self.code, "message": self.message, "path": self.path,
                  "request_id": self.request_id}
        if isinstance(getattr(self, "cause", None), dict):
            result["cause"] = copy.deepcopy(self.cause)
        return result


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def _utc(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


def _write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    temporary.replace(path)


def _number(value, path, minimum=0, maximum=MAX_SECONDS):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise AutomationError("invalid_value", "A finite number is required", path)
    if not minimum <= value <= maximum:
        raise AutomationError("invalid_value", f"Value must be between {minimum} and {maximum}", path)
    return value


def _object(value, path):
    if not isinstance(value, dict):
        raise AutomationError("invalid_value", "An object is required", path)
    return value


def _keys(value, allowed, path):
    unexpected = set(value) - set(allowed)
    if unexpected:
        field = sorted(unexpected, key=str)[0]
        raise AutomationError("invalid_value", f"Unknown field {field}", f"{path}.{field}")


def _text(value, path, maximum=500, allow_empty=False):
    if not isinstance(value, str) or len(value) > maximum or (not allow_empty and not value.strip()):
        raise AutomationError("invalid_value", "A bounded text value is required", path)
    return value


def _channels(value, path):
    if not isinstance(value, list) or any(channel not in CHANNELS or len(channel) != 1
                                          for channel in value if isinstance(channel, str)):
        raise AutomationError("invalid_value", "Channels must be a list of A–H", path)
    if any(not isinstance(channel, str) for channel in value) or len(set(value)) != len(value):
        raise AutomationError("invalid_value", "Channels must be unique A–H identifiers", path)
    return [channel for channel in CHANNELS if channel in value]


def default_experiment(output_dir=""):
    return {"schema_version": "1.0", "name": "八电极浸泡测量", "output_dir": str(output_dir),
            "bindings": {"boards": {slot: {"address": "", "settings": {}} for slot in "AB"},
                         "motion": {"port": "", "baud": 115200, "axes": {
                             axis: {"address": address, "lead_mm": 2, "travel_mm": travel,
                                    "positive_direction": 0, "max_speed_mm_s": 10}
                             for axis, address, travel in (("X", 1, 150), ("Z", 2, 50))}}},
            "defaults": {"descend_speed_mm_s": 0.5, "ascend_speed_mm_s": 1,
                         "translate_speed_mm_s": 2, "dwell_s": 180, "settle_s": 2,
                         "z_safe_mm": 0, "z_immersed_mm": 6, "x_first_mm": 0,
                         "column_pitch_mm": 9, "channels": list(CHANNELS)},
            "electrodes": [{"id": channel, "name": f"样品{channel}"} for channel in CHANNELS],
            "columns": [{"index": index, "solution": "PBS" if index == 1 else f"溶液{index}",
                         "concentration": {"value": 0 if index == 1 else 10, "unit": "uM"},
                         "overrides": {}} for index in range(1, 13)],
            "program": [{"op": operation, **({"column": column} if operation == "move_to_column" else {})}
                        for column in (1, 8) for operation in ("move_to_column", "immerse", "dwell", "withdraw")],
            "safety": {"motion_policy_confirmed": False, "electrical_policy_confirmed": False}}


def _validate_motion(motion):
    _object(motion, "bindings.motion")
    _keys(motion, {"port", "baud", "axes"}, "bindings.motion")
    _text(motion.get("port"), "bindings.motion.port", allow_empty=True)
    baud = _number(motion.get("baud"), "bindings.motion.baud", 1200, 921600)
    if not isinstance(baud, int):
        raise AutomationError("invalid_value", "Baud must be an integer", "bindings.motion.baud")
    axes = _object(motion.get("axes"), "bindings.motion.axes")
    _keys(axes, {"X", "Z"}, "bindings.motion.axes")
    for axis in ("X", "Z"):
        config = _object(axes.get(axis), f"bindings.motion.axes.{axis}")
        _keys(config, {"address", "lead_mm", "travel_mm", "positive_direction", "max_speed_mm_s"}, f"bindings.motion.axes.{axis}")
        for field, minimum, maximum in (("address", 1, 255), ("lead_mm", 0.0001, 1000),
                                        ("travel_mm", 0.0001, 10000), ("positive_direction", 0, 1),
                                        ("max_speed_mm_s", 0.0001, 10000)):
            value = _number(config.get(field), f"bindings.motion.axes.{axis}.{field}", minimum, maximum)
            if field in ("address", "positive_direction") and int(value) != value:
                raise AutomationError("invalid_value", "An integer is required", f"bindings.motion.axes.{axis}.{field}")
        if config["max_speed_mm_s"] > config["lead_mm"] * 100:
            raise AutomationError("invalid_value", "Maximum axis speed exceeds the 6000 RPM protocol limit", f"bindings.motion.axes.{axis}.max_speed_mm_s")
    if axes["X"]["address"] == axes["Z"]["address"]:
        raise AutomationError("invalid_value", "X and Z must have distinct non-broadcast addresses", "bindings.motion.axes")
    return axes


def _validate_params(params, axes, path, guided=False):
    for key in params:
        if key not in PARAMETERS:
            raise AutomationError("invalid_value", f"Unknown parameter {key}", f"{path}.{key}")
    for field in ("descend_speed_mm_s", "ascend_speed_mm_s", "translate_speed_mm_s"):
        axis = "X" if field == "translate_speed_mm_s" else "Z"
        _number(params[field], f"{path}.{field}", axes[axis]["lead_mm"] / 60, axes[axis]["max_speed_mm_s"])
    for field in ("z_safe_mm", "z_immersed_mm"):
        _number(params[field], f"{path}.{field}", 0, axes["Z"]["travel_mm"])
    if params["z_safe_mm"] >= params["z_immersed_mm"]:
        raise AutomationError("invalid_value", "Immersion must be deeper than the safe Z position", path)
    _number(params["x_first_mm"], f"{path}.x_first_mm", 0, axes["X"]["travel_mm"])
    pitch = _number(params["column_pitch_mm"], f"{path}.column_pitch_mm",
                    -axes["X"]["travel_mm"] if guided else 0.0001, axes["X"]["travel_mm"])
    if guided:
        if pitch == 0:
            raise AutomationError("invalid_value", "Column pitch must be nonzero", f"{path}.column_pitch_mm")
        _number(params["x_first_mm"] + 11 * pitch, f"{path}.x_last_mm", 0, axes["X"]["travel_mm"])
    _number(params["dwell_s"], f"{path}.dwell_s", 0, MAX_SECONDS)
    _number(params["settle_s"], f"{path}.settle_s", 0, MAX_SECONDS)
    params["channels"] = _channels(params["channels"], f"{path}.channels")


def compile_experiment(document):
    _object(document, "experiment")
    try:
        encoded = json.dumps(document, allow_nan=False)
    except (TypeError, ValueError, RecursionError) as error:
        raise AutomationError("invalid_value", "Experiment must contain finite JSON values", "experiment") from error
    if len(encoded) > 1024 * 1024:
        raise AutomationError("plan_limit", "Experiment exceeds 1 MiB", "experiment")
    experiment = copy.deepcopy(document)
    version = experiment.get("schema_version")
    if version not in ("1.0", "1.1"):
        raise AutomationError("schema_version", "Supported schema versions are 1.0 and 1.1", "schema_version")
    guided = version == "1.1"
    _keys(experiment, {"schema_version", "name", "output_dir", "bindings", "defaults", "electrodes", "columns", "program", "safety"}
          | ({"run_policy", "calibration_id", "execution"} if guided else set()), "experiment")
    if guided:
        if experiment.get("run_policy") != "guided":
            raise AutomationError("invalid_value", "Schema 1.1 requires guided run policy", "run_policy")
        _text(experiment.get("calibration_id"), "calibration_id", 200)
    _text(experiment.get("name"), "name")
    output_dir = _text(experiment.get("output_dir", ""), "output_dir", 4096, True)
    if output_dir and not Path(output_dir).is_absolute():
        raise AutomationError("invalid_value", "Output directory must be absolute", "output_dir")
    bindings = _object(experiment.get("bindings"), "bindings")
    _keys(bindings, {"boards", "motion"}, "bindings")
    axes = _validate_motion(bindings.get("motion"))
    boards = _object(bindings.get("boards"), "bindings.boards")
    _keys(boards, {"A", "B"}, "bindings.boards")
    for slot in "AB":
        board = _object(boards.get(slot), f"bindings.boards.{slot}")
        _keys(board, {"address", "settings"}, f"bindings.boards.{slot}")
        _text(board.get("address"), f"bindings.boards.{slot}.address", 200, True)
        settings = _object(board.get("settings"), f"bindings.boards.{slot}.settings")
        _keys(settings, {*v52_ble.DEFAULTS, "board_profile", "protocol", "offset_nA", "conv_time_code",
                         "conv_code", "charge_pump", "cp"}, f"bindings.boards.{slot}.settings")
        for key, value in settings.items():
            expected = v52_ble.DEFAULTS.get(key)
            if isinstance(expected, (int, float)) and not isinstance(expected, bool):
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise AutomationError("invalid_value", "A numeric setting is required", f"bindings.boards.{slot}.settings.{key}")
        if settings.get("board_profile", "v52") != "v52" or settings.get("protocol", v52_ble.PROTOCOL) != v52_ble.PROTOCOL:
            raise AutomationError("invalid_value", "Only the V5.2 BLE profile and protocol are supported", f"bindings.boards.{slot}.settings")
        try:
            v52_ble.validate_settings(settings)
        except (TypeError, ValueError) as error:
            raise AutomationError("invalid_value", str(error), f"bindings.boards.{slot}.settings") from error
    defaults = _object(experiment.get("defaults"), "defaults")
    if set(defaults) != PARAMETERS:
        raise AutomationError("invalid_value", "Defaults must contain all documented parameters", "defaults")
    _validate_params(defaults, axes, "defaults", guided)
    electrodes = experiment.get("electrodes")
    if not isinstance(electrodes, list) or len(electrodes) != 8:
        raise AutomationError("invalid_value", "Define eight electrode identities", "electrodes")
    for index, electrode in enumerate(electrodes):
        _object(electrode, f"electrodes[{index}]")
        _keys(electrode, {"id", "name"}, f"electrodes[{index}]")
        _text(electrode.get("id"), f"electrodes[{index}].id", 1)
        _text(electrode.get("name"), f"electrodes[{index}].name")
    if sorted(electrode.get("id", "") for electrode in electrodes) != list(CHANNELS):
        raise AutomationError("invalid_value", "Electrode ids must uniquely cover A–H", "electrodes")
    columns = experiment.get("columns")
    if not isinstance(columns, list) or not 1 <= len(columns) <= 12:
        raise AutomationError("invalid_value", "Define 1–12 columns", "columns")
    column_map = {}
    for index, column in enumerate(columns):
        path = f"columns[{index}]"
        _object(column, path)
        _keys(column, {"index", "solution", "concentration", "overrides"}, path)
        number = _number(column.get("index"), f"{path}.index", 1, 12)
        if int(number) != number or number in column_map:
            raise AutomationError("invalid_value", "Column indices must be distinct integers", f"{path}.index")
        _text(column.get("solution"), f"{path}.solution")
        if "concentration" not in column:
            raise AutomationError("invalid_value", "Concentration must be explicitly supplied or null", f"{path}.concentration")
        concentration = column.get("concentration")
        if not guided or concentration is not None:
            _object(concentration, f"{path}.concentration")
            _keys(concentration, {"value", "unit"}, f"{path}.concentration")
            _number(concentration.get("value"), f"{path}.concentration.value", 0, 1e30)
            _text(concentration.get("unit"), f"{path}.concentration.unit", 50)
        overrides = _object(column.get("overrides", {}), f"{path}.overrides")
        params = {**defaults, **overrides}
        _validate_params(params, axes, f"{path}.overrides", guided)
        _number(params["x_first_mm"] + (number - 1) * params["column_pitch_mm"], f"{path}.x_mm", 0, axes["X"]["travel_mm"])
        column_map[number] = column
    safety = _object(experiment.get("safety"), "safety")
    _keys(safety, {"motion_policy_confirmed", "electrical_policy_confirmed"}, "safety")
    for key in ("motion_policy_confirmed", "electrical_policy_confirmed"):
        if key in safety and not isinstance(safety[key], bool):
            raise AutomationError("invalid_value", "Safety confirmation must be boolean", f"safety.{key}")
    steps, warnings = [], ["Geometry is an unverified editing reference; calibrate real X/Z travel and direction.",
                            "Two BLE boards are not hardware synchronized; host time is receipt time.",
                            "V5.2 channel 3 has an unresolved analog anomaly; no accuracy acceptance is implied.",
                            "Fingerprints identify provenance, not electrochemical features."]
    current_column = None
    positioned_column = None
    position = {"X": 0.0, "Z": 0.0}
    duration = 0.0

    def append(op, params, source_path):
        nonlocal duration
        if len(steps) >= MAX_STEPS:
            raise AutomationError("plan_limit", "Expanded plan exceeds 500 steps", source_path)
        step = {"id": f"step-{len(steps) + 1:04d}", "op": op, "column": current_column,
                "resolved_params": params, "source_path": source_path}
        if current_column is not None:
            step.update(solution=column_map[current_column]["solution"],
                        concentration=copy.deepcopy(column_map[current_column]["concentration"]))
        if op.startswith("move_"):
            axis = params["axis"]
            duration += abs(position[axis] - params["target_mm"]) / params["speed_mm_s"]
            position[axis] = params["target_mm"]
        else:
            duration += params.get("seconds", 0)
        if duration > MAX_SECONDS:
            raise AutomationError("plan_limit", "Estimated plan duration exceeds eight hours", source_path)
        step["fingerprint"] = _hash(step)
        steps.append(step)

    def expand(actions, depth=0, path="program"):
        nonlocal current_column, positioned_column
        if not isinstance(actions, list) or not actions or len(actions) > MAX_STEPS:
            raise AutomationError("plan_limit", "Program/body must contain 1–500 actions", path)
        for index, action in enumerate(actions):
            action_path = f"{path}[{index}]"
            _object(action, action_path)
            operation = action.get("op")
            if not isinstance(operation, str):
                raise AutomationError("invalid_action", "Action op must be a supported primitive name", f"{action_path}.op")
            if operation == "repeat":
                if depth >= 3:
                    raise AutomationError("plan_limit", "Repeat nesting exceeds three levels", action_path)
                count = action.get("count")
                if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 500:
                    raise AutomationError("plan_limit", "Repeat count must be 1–500", f"{action_path}.count")
                if set(action) - {"op", "count", "body"}:
                    raise AutomationError("invalid_action", "Unexpected repeat fields", action_path)
                for repeat_index in range(count):
                    expand(action.get("body"), depth + 1, f"{action_path}.body#{repeat_index}")
                continue
            if operation not in {"move_to_column", "immerse", "withdraw", "wait", "dwell", "wait_operator"}:
                raise AutomationError("invalid_action", "Unknown action primitive", f"{action_path}.op")
            if operation == "wait" and "seconds" not in action:
                raise AutomationError("invalid_action", "Wait requires explicit seconds", f"{action_path}.seconds")
            allowed = {"op", *PARAMETERS}
            if operation in {"move_to_column", "immerse", "withdraw", "dwell"}:
                allowed.add("column")
            if operation in {"wait", "dwell"}:
                allowed.add("seconds")
            if operation == "dwell":
                allowed.add("acquire")
                if guided:
                    allowed.add("windows")
            if operation == "wait_operator":
                allowed.add("message")
            if set(action) - allowed:
                raise AutomationError("invalid_action", "Unexpected action fields", action_path)
            if "column" in action:
                column = action["column"]
                if isinstance(column, bool) or not isinstance(column, int) or column not in column_map:
                    raise AutomationError("invalid_column", "Action column must be defined", f"{action_path}.column")
                if operation in {"immerse", "withdraw"} and column != current_column:
                    raise AutomationError("invalid_column", "Move to this column before immersion or withdrawal", action_path)
                if operation == "dwell" and positioned_column is not None and column != positioned_column:
                    raise AutomationError("invalid_column", "Dwell metadata must match the positioned column", action_path)
                current_column = column
            if operation in {"move_to_column", "immerse", "withdraw", "dwell"} and current_column is None:
                raise AutomationError("missing_column", "Select a defined column before this action", action_path)
            if operation in {"immerse", "withdraw"} and positioned_column != current_column:
                raise AutomationError("missing_column", "Move to the column before immersion or withdrawal", action_path)
            if operation == "dwell" and positioned_column is not None and positioned_column != current_column:
                raise AutomationError("invalid_column", "Dwell metadata must match the positioned column", action_path)
            overrides = column_map[current_column].get("overrides", {}) if current_column else {}
            params = {**defaults, **overrides, **{key: value for key, value in action.items() if key in PARAMETERS}}
            _validate_params(params, axes, action_path, guided)
            if operation == "move_to_column":
                target = params["x_first_mm"] + (current_column - 1) * params["column_pitch_mm"]
                _number(target, f"{action_path}.x_mm", 0, axes["X"]["travel_mm"])
                append("move_z", {"axis": "Z", "target_mm": params["z_safe_mm"],
                                  "speed_mm_s": params["ascend_speed_mm_s"], "purpose": "safe_before_x"}, action_path)
                append("move_x", {"axis": "X", "target_mm": target,
                                  "speed_mm_s": params["translate_speed_mm_s"]}, action_path)
                positioned_column = current_column
            elif operation in {"immerse", "withdraw"}:
                immersed = operation == "immerse"
                append("move_z", {"axis": "Z", "target_mm": params["z_immersed_mm" if immersed else "z_safe_mm"],
                                  "speed_mm_s": params["descend_speed_mm_s" if immersed else "ascend_speed_mm_s"],
                                  "purpose": operation}, action_path)
                if immersed and params["settle_s"]:
                    append("wait", {"seconds": params["settle_s"], "purpose": "settle"}, action_path)
            elif operation in {"dwell", "wait"}:
                seconds = action.get("seconds", params["dwell_s"])
                if isinstance(seconds, (int, float)) and not isinstance(seconds, bool) and seconds > MAX_SECONDS:
                    raise AutomationError("plan_limit", "Action exceeds eight hours", action_path)
                _number(seconds, f"{action_path}.seconds")
                channels = _channels(action.get("acquire", params["channels"]), f"{action_path}.acquire") if operation == "dwell" else []
                resolved_windows = {}
                if guided and operation == "dwell":
                    windows = _object(action.get("windows", {}), f"{action_path}.windows")
                    _keys(windows, channels, f"{action_path}.windows")
                    for channel in channels:
                        window_path = f"{action_path}.windows.{channel}"
                        window = windows.get(channel, {"start_s": 0, "duration_s": seconds})
                        _object(window, window_path)
                        _keys(window, {"start_s", "duration_s"}, window_path)
                        start = _number(window.get("start_s"), f"{window_path}.start_s", 0, seconds)
                        length = _number(window.get("duration_s"), f"{window_path}.duration_s", 0, seconds)
                        if start + length > seconds:
                            raise AutomationError("invalid_value", "Channel window exceeds the column duration", window_path)
                        if length:
                            resolved_windows[channel] = {"start_s": start, "duration_s": length}
                    channels = list(resolved_windows)
                    if not channels:
                        raise AutomationError("invalid_value", "A guided dwell requires at least one nonzero acquisition window", f"{action_path}.windows")
                masks = {slot: sum(1 << (CHANNELS.index(channel) % 4) for channel in channels
                                   if (CHANNELS.index(channel) // 4 == slot_index)) for slot_index, slot in enumerate("AB")}
                settings = {}
                for slot, mask in masks.items():
                    if mask:
                        try:
                            settings[slot] = v52_ble.validate_settings({**boards[slot]["settings"], "duration_s": max(10, seconds) if guided else seconds,
                                "adaptive_stop": False, "analysis_channel": next(channel for channel in range(1, 5) if mask & (1 << (channel - 1)))})
                            if seconds < v52_ble.period_seconds(settings[slot]["sens_period_code"]):
                                raise ValueError("Acquisition duration must include at least one conversion period")
                        except (TypeError, ValueError) as error:
                            raise AutomationError("invalid_value", str(error), f"{action_path}.seconds") from error
                resolved = {"seconds": seconds, "acquire": channels, "channel_masks": masks, "settings": settings}
                if guided and operation == "dwell":
                    resolved["windows"] = resolved_windows
                append(operation, resolved, action_path)
            else:
                message = _text(action.get("message", "Operator confirmation required"), f"{action_path}.message")
                append(operation, {"message": message}, action_path)
                if "Operator waiting is excluded from estimated duration; runtime is limited to eight hours." not in warnings:
                    warnings.append("Operator waiting is excluded from estimated duration; runtime is limited to eight hours.")
    expand(experiment.get("program"))
    if "execution" in experiment:
        execution = _object(experiment["execution"], "execution")
        route = execution.get("route")
        count = execution.get("count")
        mode = execution.get("mode")
        if (set(execution) != {"mode", "count", "route"} or mode not in ("once", "repeat", "custom")
                or isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 500
                or (mode != "repeat" and count != 1)
                or not isinstance(route, list) or not route
                or any(isinstance(column, bool) or not isinstance(column, int) or column not in column_map for column in route)
                or (mode != "custom" and route != sorted(set(route)))):
            raise AutomationError("invalid_value", "Invalid finite execution descriptor", "execution")
        moves = [step["column"] for step in steps if step["op"] == "move_x"]
        dwells = [step["column"] for step in steps if step["op"] == "dwell"]
        if moves != route * count or dwells != route * count:
            raise AutomationError("invalid_value", "Execution route does not match actual visits", "execution")
        cursor = 0
        for column in route * count:
            expected = [("move_z", "safe_before_x"), ("move_x", None), ("move_z", "immerse")]
            for operation, purpose in expected:
                if (cursor >= len(steps) or steps[cursor]["op"] != operation
                        or steps[cursor]["column"] != column
                        or steps[cursor]["resolved_params"].get("purpose") != purpose):
                    raise AutomationError("invalid_value", "Execution visit requires safe positioning and immersion", "execution")
                cursor += 1
            if (cursor < len(steps) and steps[cursor]["op"] == "wait"
                    and steps[cursor]["resolved_params"].get("purpose") == "settle"):
                cursor += 1
            for operation, purpose in [("dwell", None), ("move_z", "withdraw")]:
                if (cursor >= len(steps) or steps[cursor]["op"] != operation
                        or steps[cursor]["column"] != column
                        or steps[cursor]["resolved_params"].get("purpose") != purpose):
                    raise AutomationError("invalid_value", "Execution visit requires exactly one dwell and safe withdrawal", "execution")
                cursor += 1
        if cursor != len(steps):
            raise AutomationError("invalid_value", "Execution contains actions outside its visits", "execution")
        visit_index = 0
        for step in steps:
            if step["op"] == "move_z" and step["resolved_params"].get("purpose") == "safe_before_x":
                visit_index += 1
            step.update(cycle_index=(max(1, visit_index) - 1) // len(route) + 1,
                        path_index=(max(1, visit_index) - 1) % len(route) + 1, visit_index=max(1, visit_index))
            step["fingerprint"] = _hash({key: value for key, value in step.items() if key != "fingerprint"})
    return {"experiment": experiment, "plan_hash": _hash({"experiment": experiment, "steps": steps}),
            "steps": steps, "warnings": warnings, "duration_s": duration}


def experiment_schema(version="1.0"):
    numeric = {"type": "number", "minimum": 0}
    params = {key: dict(numeric) for key in PARAMETERS if key != "channels"}
    params["channels"] = {"type": "array", "items": {"enum": list(CHANNELS)}, "uniqueItems": True, "maxItems": 8}
    action_properties = {"op": {"enum": ["move_to_column", "immerse", "withdraw", "wait", "dwell", "wait_operator", "repeat"]},
                         "column": {"type": "integer", "minimum": 1, "maximum": 12}, "seconds": numeric,
                         "acquire": params["channels"], "message": {"type": "string", "maxLength": 500},
                         "count": {"type": "integer", "minimum": 1, "maximum": 500},
                         "body": {"type": "array", "items": {"$ref": "#/$defs/action"}, "minItems": 1, "maxItems": 500}, **params}
    text_schema = {"type": "string", "minLength": 1, "maxLength": 500}
    axis_schema = {"type": "object", "additionalProperties": False,
                   "required": ["address", "lead_mm", "travel_mm", "positive_direction", "max_speed_mm_s"],
                   "properties": {"address": {"type": "integer", "minimum": 1, "maximum": 255},
                                  "positive_direction": {"enum": [0, 1]},
                                  **{key: {"type": "number", "exclusiveMinimum": 0} for key in ("lead_mm", "travel_mm", "max_speed_mm_s")}}}
    settings_properties = {key: {"type": "boolean" if isinstance(value, bool) else "number" if isinstance(value, (int, float)) else "string"}
                           for key, value in v52_ble.DEFAULTS.items()}
    settings_properties.update({key: {"type": "number"} for key in ("offset_nA", "conv_time_code", "conv_code", "charge_pump", "cp")})
    settings_properties.update(board_profile={"const": "v52"}, protocol={"const": v52_ble.PROTOCOL})
    board_schema = {"type": "object", "required": ["address", "settings"], "additionalProperties": False,
                    "properties": {"address": {"type": "string", "maxLength": 200},
                                   "settings": {"type": "object", "properties": settings_properties, "additionalProperties": False}}}
    schema = {"$schema": "https://json-schema.org/draft/2020-12/schema", "$id": "urn:sensus:automation:1.0",
            "title": "SensUs declarative experiment", "type": "object",
            "additionalProperties": False,
            "required": ["schema_version", "name", "bindings", "defaults", "electrodes", "columns", "program", "safety"],
            "properties": {"schema_version": {"const": "1.0"}, "name": {"type": "string", "minLength": 1, "maxLength": 500},
                           "output_dir": {"type": "string"}, "bindings": {"type": "object", "required": ["boards", "motion"],
                               "additionalProperties": False, "properties": {
                                   "boards": {"type": "object", "required": ["A", "B"], "additionalProperties": False,
                                              "properties": {slot: board_schema for slot in "AB"}},
                                   "motion": {"type": "object", "required": ["port", "baud", "axes"], "additionalProperties": False,
                                              "properties": {"port": {"type": "string"}, "baud": {"type": "integer", "minimum": 1200, "maximum": 921600},
                                                             "axes": {"type": "object", "required": ["X", "Z"], "additionalProperties": False,
                                                                      "properties": {axis: axis_schema for axis in ("X", "Z")}}}}}},
                           "defaults": {"type": "object", "required": sorted(PARAMETERS), "properties": params, "additionalProperties": False},
                           "electrodes": {"type": "array", "minItems": 8, "maxItems": 8, "items": {
                               "type": "object", "required": ["id", "name"], "additionalProperties": False,
                               "properties": {"id": {"enum": list(CHANNELS)}, "name": text_schema}}},
                           "columns": {"type": "array", "minItems": 1, "maxItems": 12, "items": {
                               "type": "object", "required": ["index", "solution", "concentration"], "additionalProperties": False,
                               "properties": {"index": {"type": "integer", "minimum": 1, "maximum": 12}, "solution": text_schema,
                                              "concentration": {"type": "object", "required": ["value", "unit"], "additionalProperties": False,
                                                                "properties": {"value": {"type": "number", "minimum": 0, "maximum": 1e30}, "unit": {"type": "string", "minLength": 1, "maxLength": 50}}},
                                              "overrides": {"type": "object", "properties": params, "additionalProperties": False}}}},
                           "program": {"type": "array", "minItems": 1, "maxItems": 500, "items": {"$ref": "#/$defs/action"}},
                           "safety": {"type": "object", "additionalProperties": False, "properties": {key: {"type": "boolean"} for key in ("motion_policy_confirmed", "electrical_policy_confirmed")}}},
            "$defs": {"action": {"type": "object", "required": ["op"], "properties": action_properties, "additionalProperties": False}},
            "x-runtime-limits": {"expanded_steps": 500, "repeat_depth": 3, "duration_s": MAX_SECONDS,
                                 "acquisition_seconds": [10, 3600], "finite_numbers": True}}
    if version == "1.1":
        schema["$id"] = "urn:sensus:automation:1.1"
        schema["required"] += ["run_policy", "calibration_id"]
        schema["properties"].update(schema_version={"const": "1.1"}, run_policy={"const": "guided"},
                                    calibration_id={"type": "string", "minLength": 1, "maxLength": 200})
        schema["properties"]["execution"] = {"type": "object", "additionalProperties": False,
            "required": ["mode", "count", "route"], "properties": {
                "mode": {"enum": ["once", "repeat", "custom"]},
                "count": {"type": "integer", "minimum": 1, "maximum": 500},
                "route": {"type": "array", "minItems": 1, "items": {"type": "integer", "minimum": 1, "maximum": 12}}}}
        params["column_pitch_mm"] = {"type": "number", "not": {"const": 0}}
        action_properties["column_pitch_mm"] = params["column_pitch_mm"]
        action_properties["windows"] = {"type": "object", "propertyNames": {"enum": list(CHANNELS)},
            "additionalProperties": {"type": "object", "required": ["start_s", "duration_s"],
                "additionalProperties": False, "properties": {key: numeric for key in ("start_s", "duration_s")}}}
        concentration = schema["properties"]["columns"]["items"]["properties"]["concentration"]
        schema["properties"]["columns"]["items"]["properties"]["concentration"] = {"anyOf": [concentration, {"type": "null"}]}
        schema["x-runtime-limits"]["acquisition_seconds"] = [0, 3600]
        schema["x-runtime-limits"]["physical_calibration_columns"] = [1, 12]
    return schema


def openapi_document():
    paths = {}
    for method, names in (("get", ("capabilities", "schema", "openapi.json", "state", "ports", "events", "guided", "limits", "devices", "boards", "twin", "guided/calibration/jog")),
                          ("post", ("validate", "preview", "experiment", "run", "stop", "resume", "pause", "motion/connect",
                                    "boards/scan", "boards/bind", "boards/connect", "boards/disconnect",
                                    "motion/reference", "motion/enable", "motion/stop", "motion/disconnect", "motion/refresh", "twin/register", "twin/register-datum",
                                    "guided/experiment", "guided/preview", "guided/run", "guided/calibration/begin",
                                    "guided/calibration/capture", "guided/calibration/finish", "guided/calibration/cancel",
                                    "guided/calibration/jog/start", "guided/calibration/jog/heartbeat", "guided/calibration/jog/stop", "guided/calibration/jog/config",
                                    "limits/begin", "limits/capture", "limits/save", "limits/cancel"))):
        for name in names:
            operation = {"operationId": name.replace("/", "_").replace(".", "_"),
                         "responses": {"200": {"description": "Structured result"},
                                       "400": {"description": "Validation failed"}, "409": {"description": "Resource or plan conflict"}}}
            if method == "post":
                properties = {}
                required = []
                if name == "boards/bind":
                    properties["addresses"] = {"type": "object", "required": ["A", "B"], "additionalProperties": False,
                        "properties": {slot: {"type": "string", "maxLength": 200} for slot in ("A", "B")},
                        "description": "A=front, away from Z motor (A-D); B=rear, near Z motor (E-H). Empty means unbound; nonempty addresses must be distinct."}
                    required = ["addresses"]
                if name in {"boards/connect", "boards/disconnect"}:
                    properties["addresses"] = {"type": "array", "minItems": 1, "maxItems": 2, "uniqueItems": True,
                        "items": {"type": "string", "minLength": 1, "maxLength": 200}}
                    required = ["addresses"]
                    operation["responses"]["200"]["description"] = "Independent per-address result; inspect errors and connections for partial failure"
                if name in {"validate", "preview", "experiment", "run"}:
                    properties["experiment"] = {"oneOf": [{"$ref": "#/components/schemas/Experiment"},
                                                           {"$ref": "#/components/schemas/Experiment11"}]}
                    required.append("experiment")
                if name == "run":
                    properties.update(mode={"enum": ["simulate", "live"]}, plan_hash={"type": "string"}, idempotency_key={"type": "string"})
                    required += ["mode", "plan_hash", "idempotency_key"]
                if name == "motion/connect":
                    properties["motion"] = {"type": "object"}
                    required = ["motion"]
                if name == "motion/reference":
                    properties["confirmed"] = {"const": True}
                    required = ["confirmed"]
                if name == "motion/enable":
                    properties["axis"] = {"enum": ["X", "Z"]}
                    required = ["axis"]
                if name in {"guided/experiment", "guided/preview", "guided/run"}:
                    properties["document"] = {"type": "object", "description": "Guided template document, schema guided-1.0"}
                    required = ["document"]
                if name in {"guided/preview", "guided/run"}:
                    properties["mode"] = {"enum": ["simulate", "live"]}
                    required.append("mode")
                if name == "guided/run":
                    properties.update(plan_hash={"type": "string"}, idempotency_key={"type": "string"})
                    required += ["plan_hash", "idempotency_key"]
                if name == "guided/calibration/capture":
                    properties["point"] = {"enum": ["first", "last", "working", "safe"]}
                    required = ["point"]
                if name == "guided/calibration/begin":
                    properties["control_mode"] = {"const": "keyboard"}
                    properties["motion_confirmed"] = {"const": True}
                    required = ["control_mode", "motion_confirmed"]
                if name.startswith("guided/calibration/jog/"):
                    properties["session_token"] = {"type": "string", "minLength": 1, "maxLength": 100}
                    required = ["session_token"]
                    if not name.endswith("/config"):
                        properties["press_id"] = {"type": "string", "minLength": 1, "maxLength": 100}
                        required.append("press_id")
                    if name.endswith("/start"):
                        properties["axis"] = {"enum": ["X", "Z"]}
                        properties["direction"] = {"enum": ["left", "right", "up", "down"]}
                        required += ["axis", "direction"]
                    if name.endswith("/heartbeat"):
                        properties["sequence"] = {"type": "integer", "minimum": 1}
                        required.append("sequence")
                    if name.endswith("/config"):
                        for field in ("speed_mm_s", "max_speed_mm_s"):
                            properties[field] = {"type": "number", "exclusiveMinimum": 0, "maximum": 20}
                            required.append(field)
                        properties["fast_motion_confirmed"] = {"type": "boolean", "description":
                            "Must be true above 2 mm/s: electrodes clear of wells and at least 10 mm clear travel"}
                if name == "limits/capture":
                    properties["boundary"] = {"enum": ["x_left", "x_right", "z_up", "z_down"]}
                    required = ["boundary"]
                if name == "limits/save":
                    properties["clearance_confirmed"] = {"const": True}
                    required = ["clearance_confirmed"]
                if name == "twin/register":
                    bounds = ("x_left_mm", "x_right_mm", "z_up_lift_mm", "z_down_lift_mm")
                    properties["cad_bounds"] = {"type": "object", "additionalProperties": False,
                        "properties": {field: {"type": "number"} for field in bounds}, "required": list(bounds)}
                    properties["confirmed"] = {"const": True}
                    required = ["cad_bounds", "confirmed"]
                if name == "twin/register-datum":
                    properties["confirmed"] = {"const": True}
                    properties["datum"] = {"enum": ["left_up", "right_down"], "default": "left_up"}
                    required = ["confirmed"]
                operation["requestBody"] = {"required": bool(required), "content": {"application/json": {"schema": {
                    "type": "object", "properties": properties, "required": required,
                    **({"additionalProperties": False} if name.startswith(("limits/", "guided/calibration/jog/")) or name in ("twin/register", "twin/register-datum", "motion/refresh", "guided/calibration/begin") else {})}}}}
            paths[f"{API_PREFIX}/{name}"] = {method: operation}
    schema = experiment_schema()
    schema.pop("$id", None)
    schema.pop("$schema", None)
    def relocate_refs(value, prefix="Experiment"):
        if isinstance(value, dict):
            for key, item in value.items():
                if key == "$ref" and isinstance(item, str) and item.startswith("#/"):
                    value[key] = f"#/components/schemas/{prefix}/" + item[2:]
                else:
                    relocate_refs(item, prefix)
        elif isinstance(value, list):
            for item in value:
                relocate_refs(item, prefix)
    relocate_refs(schema)
    guided_schema = experiment_schema("1.1")
    guided_schema.pop("$id", None)
    guided_schema.pop("$schema", None)
    relocate_refs(guided_schema, "Experiment11")
    return {"openapi": "3.1.0", "info": {"title": "SensUs automation API", "version": "1.0",
            "description": "Local declarative automation. Simulation is synthetic; dual-board/motion hardware acceptance is pending."},
            "servers": [{"url": "/"}], "paths": paths, "components": {"schemas": {"Experiment": schema, "Experiment11": guided_schema}}}


class _Cancelled(Exception):
    pass


class _MotionCancellation:
    def __init__(self, event, deadline):
        self.event, self.deadline = event, deadline

    def is_set(self):
        return self.event.is_set() or time.monotonic() >= self.deadline


class AutomationController:
    def __init__(self, state_dir, runs_dir, project_dir, *, motion_factory=None, process_factory=None,
                 board_stop_factory=None, simulation_scale=0.001, parked_callback=None,
                 require_absolute_limits=False, motion_feedback_interval_s=0, board_release_factory=None):
        from .motion_limits import AbsoluteMotionLimits
        self.state_dir, self.runs_dir, self.project_dir = map(Path, (state_dir, runs_dir, project_dir))
        self.limits = AbsoluteMotionLimits(self.state_dir)
        self._require_absolute_limits = require_absolute_limits is True
        self._motion_factory, self._process_factory = motion_factory, process_factory or subprocess.Popen
        self._board_stop_factory = board_stop_factory or v52_ble.stop_device
        self._board_release_factory = board_release_factory
        self._parked_callback = parked_callback
        self._simulation_scale = _number(simulation_scale, "simulation_scale", 0, 1)
        self._lock = threading.RLock()
        self._motion = None
        self._motion_config = None
        self._motion_calibration_validator = None
        self._run_motion_calibration = None
        self._motion_feedback_interval_s = _number(motion_feedback_interval_s, "motion_feedback_interval_s", 0, 60)
        self._motion_feedback_thread = None
        self._motion_feedback_stop = threading.Event()
        self._motion_feedback_generation = 0
        self._motion_feedback_error = None
        self._thread = None
        self._stop_event, self._resume_event = threading.Event(), threading.Event()
        self._events = deque(maxlen=2048)
        self._unowned_motion_events = deque(maxlen=2048)
        self._unowned_motion_events_lock = threading.Lock()
        self._unowned_motion_dropped = 0
        self._event_seq = 0
        self._event_loss_seq = 0
        self._children = []
        self._run_dir = None
        self._deadline = None
        self._idempotency = {}
        self._recovery_required = False
        self._owned_boards = []
        self._owns_motion = False
        self._recovery_motion_config = None
        self._audit_failure = None
        self._manual_calibration_active = False
        self._alignment_io_active = False
        self._keyboard_operation_active = False
        self._alignment_connection_io = False
        self._limit_calibration_active = False
        self._limit_failure = None
        self._limit_monitor = None
        self._limit_monitor_stop = threading.Event()
        self._motion_move_active = False
        self._guided_unknown_slots = set()
        self._guided_missing_slots = set()
        self._guided_attempts = {}
        self._guided_last_step = None
        self._experiment = default_experiment()
        self._progress = None
        self._state = {"state": "idle", "mode": None, "run_id": None, "step_index": -1, "column": None,
                       "position": {"X": None, "Z": None}, "message": "Ready", "error": None,
                       "stop_confirmed": True, "manifest_path": None, "channels": [],
                       "pause_requested": False, "column_timing": None, "boards": {}}
        try:
            stored = json.loads((self.state_dir / "automation.json").read_text(encoding="utf-8"))
            self._experiment = compile_experiment(stored["experiment"])["experiment"]
            self._idempotency = stored.get("idempotency", {})
            previous = stored.get("state", {})
            if not previous.get("stop_confirmed", True):
                self._state.update(previous, state="error", message="Previous STOP is unconfirmed; verify outputs before reuse")
                self._recovery_required = True
                ownership = stored.get("ownership")
                if ownership is None:
                    self._set_ownership(compile_experiment(self._experiment))
                else:
                    self._owned_boards = ownership.get("boards", [])
                    self._owns_motion = ownership.get("motion", False)
                    self._recovery_motion_config = ownership.get("motion_config")
        except (OSError, ValueError, KeyError):
            pass

    def _busy(self):
        return bool(self._manual_calibration_active or self._limit_calibration_active or (self._thread and self._thread.is_alive())
                    or not self._state["stop_confirmed"])

    def _set_ownership(self, compiled):
        slots = {slot for step in compiled["steps"] for slot, mask in step["resolved_params"].get("channel_masks", {}).items() if mask}
        self._owned_boards = [compiled["experiment"]["bindings"]["boards"][slot]["address"] for slot in sorted(slots)]
        self._owns_motion = any(step["op"].startswith("move_") for step in compiled["steps"])
        self._recovery_motion_config = compiled["experiment"]["bindings"]["motion"] if self._owns_motion else None

    def is_busy(self):
        with self._lock:
            return self._busy()

    def _idle(self):
        if self._busy():
            raise AutomationError("busy", "Automation resources are active or STOP remains unconfirmed", status=409)

    def snapshot(self):
        with self._lock:
            self._drain_unowned_motion_events()
            motion = self._motion_snapshot()
            progress = self._progress.snapshot(self._state["state"], self._state["step_index"],
                self._state.get("column_timing"), time.time()) if self._progress else None
            return copy.deepcopy({**self._state, "busy": self._busy(), "motion": motion,
                                  "progress": progress,
                                  "synthetic": self._state["mode"] == "simulate",
                                  "simulation_scale": self._simulation_scale,
                                  "recovery_required": self._recovery_required,
                                  "events": list(self._events)[-100:], "experiment": self._experiment})

    def _motion_snapshot(self):
        motion = self._motion.snapshot() if self._motion else {"connected": False, "referenced": False,
                "position": {"X": None, "Z": None}, "axes": {}, "stop_confirmed": True}
        return automation_devices.motion_status(motion, self._motion_feedback_error)

    def device_status(self, board_bindings=None):
        with self._lock:
            bindings = self._experiment["bindings"]["boards"] if board_bindings is None else board_bindings
            boards = automation_devices.board_status(bindings, self._state["boards"],
                synthetic=self._state["mode"] != "live",
                active=bool(not self._recovery_required and self._state["state"] in
                            ("running", "waiting_operator", "waiting_recovery", "paused")
                            and self._thread and self._thread.is_alive()))
            return {"motion": self._motion_snapshot(), "boards": boards, "limits": self.limits.snapshot(self._motion)}

    def _motion_feedback_busy(self):
        manual = self._motion.snapshot().get("manual", {}) if self._motion is not None else {}
        return bool(self._alignment_io_active or self._motion_move_active or self._recovery_required
                    or manual.get("active") is True and manual.get("control_mode") == "keyboard"
                    or self._state["mode"] == "live" and self._owns_motion
                    and self._thread and self._thread.is_alive())

    def _refresh_motion_feedback(self, driver, generation):
        if driver is not self._motion or generation != self._motion_feedback_generation:
            return False
        current = driver.snapshot()
        if not current.get("connected") or current.get("faulted") or self._motion_feedback_error:
            return False
        if self._motion_feedback_busy():
            return True
        try:
            driver.refresh()
        except Exception as error:
            if getattr(error, "code", None) == "motion_busy":
                return True
            if driver is self._motion and generation == self._motion_feedback_generation:
                self._motion_feedback_error = {"code": getattr(error, "code", "motion_feedback_failed"),
                                               "message": str(error)}
                self._event("motion_feedback_failed", error=self._motion_feedback_error)
            return False
        return driver is self._motion and generation == self._motion_feedback_generation

    def refresh_motion(self):
        with self._lock:
            if self._motion is not None:
                self._refresh_motion_feedback(self._motion, self._motion_feedback_generation)
            return self.snapshot()

    def _motion_feedback_loop(self, driver, generation, stop_event):
        while not stop_event.wait(self._motion_feedback_interval_s):
            with self._lock:
                if stop_event.is_set() or not self._refresh_motion_feedback(driver, generation):
                    return

    def _stop_motion_feedback(self):
        self._motion_feedback_stop.set()
        self._motion_feedback_generation += 1
        worker = self._motion_feedback_thread
        self._motion_feedback_thread = None
        return worker

    def _start_motion_feedback(self):
        self._stop_motion_feedback()
        if self._motion_feedback_interval_s == 0:
            return
        self._motion_feedback_stop = threading.Event()
        self._motion_feedback_thread = threading.Thread(target=self._motion_feedback_loop,
            args=(self._motion, self._motion_feedback_generation, self._motion_feedback_stop),
            name="automation-motion-feedback", daemon=True)
        self._motion_feedback_thread.start()

    def _persist(self):
        _write_json(self.state_dir / "automation.json", {"experiment": self._experiment,
                    "idempotency": self._idempotency, "state": self._state,
                    "ownership": {"boards": self._owned_boards, "motion": self._owns_motion,
                                  "motion_config": self._recovery_motion_config}})

    def _audit_failed(self, error):
        self._audit_failure = AutomationError("audit_failed", f"Experiment audit could not be saved: {error}", "artifacts").as_dict()
        self._state["audit_error"] = self._audit_failure
        self._stop_event.set()

    def _persist_best_effort(self):
        try:
            self._persist()
        except Exception as error:
            self._audit_failed(error)
            self._state.update(state="error", error=self._audit_failure, message=self._audit_failure["message"])

    def _manifest_best_effort(self, compiled):
        if self._audit_failure:
            self._state.update(state="error", error=self._audit_failure, message=self._audit_failure["message"])
        try:
            self._manifest(compiled)
        except Exception as error:
            self._audit_failed(error)
        if self._audit_failure:
            self._state.update(state="error", error=self._audit_failure, message=self._audit_failure["message"])

    def _owns_live_motion_log(self):
        return bool(self._run_dir and self._state["mode"] == "live" and self._owns_motion
                    and self._state["state"] not in ("idle", "completed", "stopped", "error")
                    and self._thread and self._thread.is_alive())

    def _motion_event(self, detail):
        if self._owns_live_motion_log():
            self._event("motion_wire", detail=detail)
        else:
            timestamp = time.time()
            with self._unowned_motion_events_lock:
                if len(self._unowned_motion_events) == self._unowned_motion_events.maxlen:
                    self._unowned_motion_dropped += 1
                self._unowned_motion_events.append((timestamp, detail))

    def _drain_unowned_motion_events(self):
        while True:
            with self._unowned_motion_events_lock:
                dropped, self._unowned_motion_dropped = self._unowned_motion_dropped, 0
                if dropped:
                    self._event_seq += dropped
                    self._event_loss_seq = self._event_seq
                try:
                    timestamp, detail = self._unowned_motion_events.popleft()
                except IndexError:
                    return
            self._event_seq += 1
            self._events.append({"seq": self._event_seq, "event": "motion_wire", "host_unix_s": timestamp,
                                 "host_utc": _utc(timestamp), "run_id": None, "synthetic": False, "detail": detail})

    def _event(self, event, **details):
        with self._lock:
            self._drain_unowned_motion_events()
            self._event_seq += 1
            timestamp = time.time()
            physical_motion = event in ("motion_wire", "motion_feedback_failed")
            owned_live_run = self._owns_live_motion_log()
            record = {"seq": self._event_seq, "event": event, "host_unix_s": timestamp,
                      "host_utc": _utc(timestamp), "run_id": self._state["run_id"],
                      "synthetic": self._state["mode"] == "simulate", **details}
            if physical_motion:
                record.update(synthetic=False, run_id=self._state["run_id"] if owned_live_run else None)
            self._events.append(record)
            if self._run_dir and (not physical_motion or owned_live_run):
                try:
                    with (self._run_dir / "events.jsonl").open("a", encoding="utf-8") as handle:
                        handle.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")
                except Exception as error:
                    self._audit_failed(error)

    def events(self, after=0):
        if isinstance(after, bool) or not isinstance(after, int) or after < 0:
            raise AutomationError("invalid_value", "Event cursor must be a nonnegative integer", "after")
        with self._lock:
            self._drain_unowned_motion_events()
            return {"events": copy.deepcopy([event for event in self._events if event["seq"] > after]),
                    "last_seq": self._event_seq, "truncated": bool(after < self._event_loss_seq
                        or self._events and after < self._events[0]["seq"] - 1)}

    def validate(self, experiment):
        return compile_experiment(experiment)

    def save(self, experiment):
        compiled = compile_experiment(experiment)
        with self._lock:
            self._idle()
            self._experiment = compiled["experiment"]
            self._persist()
            return self.snapshot()

    def limits_snapshot(self):
        with self._lock:
            return self.limits.snapshot(self._motion)

    def _limits_error(self, error):
        if isinstance(error, AutomationError):
            return error
        return AutomationError(getattr(error, "code", "absolute_limits_failed"),
                               getattr(error, "message", str(error)), "limits", 409)

    def _limits_session(self):
        if not self._limit_calibration_active or self._motion is None:
            raise AutomationError("limits_session", "Begin absolute limit capture on the connected bus first", "limits", 409)
        if self._stop_event.is_set():
            raise AutomationError("limits_cancelled", "STOP interrupted absolute limit capture", "limits", 409)

    def _limits_operation(self, operation):
        try:
            result = operation()
            if self._stop_event.is_set():
                self.limits.cancel(self._motion)
                raise AutomationError("limits_cancelled", "STOP interrupted absolute limit capture", "limits", 409)
            return result
        except Exception as error:
            raise self._limits_error(error) from error

    def begin_limits(self):
        with self._lock:
            self._idle()
            if self._motion is None:
                raise AutomationError("motion_disconnected", "Connect the motion bus first", "limits", 409)
            self._stop_event.clear()
            self._limit_calibration_active = True
            try:
                result = self._limits_operation(lambda: self.limits.begin(self._motion))
            except Exception:
                self._limit_calibration_active = self._stop_event.is_set() or self.limits.snapshot(self._motion)["calibrating"]
                raise
            self._event("limits_capture_started")
            return result

    def capture_limit(self, boundary):
        with self._lock:
            self._limits_session()
            result = self._limits_operation(lambda: self.limits.capture(self._motion, boundary))
            self._event("limit_captured", boundary=boundary)
            return result

    def save_limits(self, clearance_confirmed=False):
        with self._lock:
            self._limits_session()
            result = self._limits_operation(lambda: self.limits.save(self._motion, clearance_confirmed=clearance_confirmed))
            self._limit_calibration_active = False
            self._event("limits_saved")
            return result

    def cancel_limits(self):
        with self._lock:
            if not self._limit_calibration_active:
                self._idle()
            try:
                result = self.limits.cancel(self._motion)
            except Exception as error:
                raise self._limits_error(error) from error
            self._limit_calibration_active = False
            self._event("limits_capture_cancelled")
            return result

    def reserved_motion_port(self):
        with self._lock:
            return self._motion_config["port"] if self._motion and self._motion.snapshot().get("connected") else ""

    def ports(self):
        from serial.tools import list_ports
        with self._lock:
            if not self._recovery_required:
                self._idle()
            elif self._thread and self._thread.is_alive():
                raise AutomationError("busy", "Wait for the active stop worker before enumerating ports", status=409)
        return {"ports": [{"device": port.device, "description": port.description, "hwid": port.hwid}
                          for port in list_ports.comports()]}

    def connect_motion(self, motion):
        _validate_motion(motion)
        if not motion["port"].strip():
            raise AutomationError("invalid_value", "Select the shared RS485 port", "motion.port")
        with self._lock:
            if self._recovery_required and not (self._thread and self._thread.is_alive()):
                if motion != (self._recovery_motion_config or self._experiment["bindings"]["motion"]):
                    raise AutomationError("recovery_identity", "Recovery must reconnect the saved motion bus and axis identities", "motion", 409)
            else:
                self._idle()
            if self._motion and (not self._recovery_required or self._motion.snapshot().get("connected")):
                raise AutomationError("motion_connected", "Disconnect the existing bus before reconnecting", status=409)
            factory = self._motion_factory
            if factory is None:
                from .emm_motion import SharedEmmBus
                factory = SharedEmmBus
            driver = factory(motion["port"], motion["baud"], motion["axes"],
                             event_callback=self._motion_event,
                             **({"require_absolute_limits": True} if self._require_absolute_limits else {}))
            prior_ownership = self._recovery_required or self._owns_motion or bool(self._owned_boards)
            try:
                driver.connect()
            except Exception as connect_error:
                if not prior_ownership and driver.snapshot().get("serial_opened_once") is False:
                    driver.close()
                    self._event("motion_port_not_opened", port=motion["port"], error=str(connect_error))
                    raise AutomationError("motion_port_unavailable",
                        f"无法打开串口 {motion['port']}。若更换了 USB 插口，请枚举并选择当前 RS485 串口后重新连接；本次未打开端口或发送电机指令。原始错误：{connect_error}",
                        "bindings.motion.port", 409) from connect_error
                try:
                    result = driver.close()
                    if (result is False or isinstance(result, dict) and result.get("stop_confirmed") is not True
                            or driver.snapshot().get("stop_confirmed") is False):
                        raise AutomationError("stop_unconfirmed", "Failed connection cleanup did not confirm stationary motion")
                except Exception as stop_error:
                    self._motion, self._motion_config = driver, copy.deepcopy(motion)
                    raise self._retain_motion_ownership(stop_error) from connect_error
                raise
            self._motion, self._motion_config = driver, copy.deepcopy(motion)
            self._motion_feedback_error = None
            self._start_motion_feedback()
            return self.snapshot()

    def reference_motion(self, confirmed=False):
        with self._lock:
            self._idle()
            if confirmed is not True:
                raise AutomationError("reference_confirmation", "Explicitly confirm the current known reference position", "confirmed")
            if self._motion is None:
                raise AutomationError("motion_disconnected", "Connect the motion bus first", status=409)
            self._motion.confirm_reference()
            return self.snapshot()

    def enable_motion(self, axis):
        with self._lock:
            self._idle()
            if self._motion is None:
                raise AutomationError("motion_disconnected", "Connect the motion bus first", status=409)
            if axis not in ("X", "Z"):
                raise AutomationError("invalid_value", "Axis must be X or Z", "axis")
            self._motion.enable(axis)
            return self.snapshot()

    def stop_motion(self):
        if self.is_busy():
            return self.stop()
        with self._lock:
            if self._motion:
                try:
                    result = self._motion.stop_all()
                except Exception as error:
                    raise self._retain_motion_ownership(error) from error
                if not isinstance(result, dict) or result.get("stop_confirmed") is not True:
                    self._retain_motion_ownership(AutomationError("stop_unconfirmed", "Motion STOP lacks stationary confirmation"))
                else:
                    self._state["stop_confirmed"] = True
                    self._recovery_required = False
                    self._owns_motion = False
                    self._owned_boards = []
                    self._persist_best_effort()
            return self.snapshot()

    def _retain_motion_ownership(self, error):
        failure = AutomationError("stop_unconfirmed", f"Motion STOP is unconfirmed: {error}", "bindings.motion", 409)
        self._state.update(state="error", mode="live", stop_confirmed=False, error=failure.as_dict(), message=failure.message,
                           run_id=None, manifest_path=None, step_index=-1, column=None, channels=[], position={"X": None, "Z": None})
        self._recovery_required = True
        self._owns_motion = True
        self._recovery_motion_config = copy.deepcopy(self._motion_config)
        self._event("motion_stop_unconfirmed", error=failure.as_dict())
        self._persist_best_effort()
        return failure

    def disconnect_motion(self):
        worker = None
        try:
            with self._lock:
                self._idle()
                worker = self._stop_motion_feedback()
                if self._motion:
                    try:
                        result = self._motion.close()
                        if (result is False or isinstance(result, dict) and result.get("stop_confirmed") is not True
                                or self._motion.snapshot().get("stop_confirmed") is False):
                            raise AutomationError("stop_unconfirmed", "Disconnect did not confirm stationary motion")
                    except Exception as error:
                        raise self._retain_motion_ownership(error) from error
                    self._motion = None
                    self._motion_config = None
                    self._motion_feedback_error = None
                return self.snapshot()
        finally:
            if worker is not None and worker is not threading.current_thread():
                worker.join(timeout=1)

    def _live_gates(self, compiled):
        experiment = compiled["experiment"]
        enforced_limits = None
        safety = experiment["safety"]
        moves = any(step["op"].startswith("move_") for step in compiled["steps"])
        acquires = any(step["resolved_params"].get("acquire") for step in compiled["steps"])
        boards = experiment["bindings"]["boards"]
        if acquires:
            slots = "AB" if experiment["schema_version"] == "1.0" else sorted({slot for step in compiled["steps"]
                    for slot, mask in step["resolved_params"].get("channel_masks", {}).items() if mask})
            addresses = [boards[slot]["address"].strip().casefold() for slot in slots]
            if not all(addresses) or len(set(addresses)) != len(addresses):
                raise AutomationError("live_gate", "Live experiments require two distinct configured BLE identities", "bindings.boards", 409)
            if safety.get("electrical_policy_confirmed") is not True:
                raise AutomationError("live_gate", "Confirm the polarization/electrical policy before live acquisition", "safety.electrical_policy_confirmed", 409)
        if moves:
            if safety.get("motion_policy_confirmed") is not True or safety.get("electrical_policy_confirmed") is not True:
                raise AutomationError("live_gate", "Confirm motion and electrical policies before live motion", "safety", 409)
            if self._motion is None or self._motion_config != experiment["bindings"]["motion"]:
                raise AutomationError("live_gate", "Connect the exact configured motion bus and calibrate it first", "bindings.motion", 409)
            if self._require_absolute_limits:
                try:
                    enforced_limits = self.limits.require(self._motion)
                    self.limits.preflight(self._motion, compiled["steps"])
                except Exception as error:
                    stop_result = getattr(error, "stop_result", None)
                    if isinstance(stop_result, dict) and stop_result.get("stop_confirmed") is not True:
                        self._state["execution_error"] = self._limits_error(error).as_dict()
                        self._retain_motion_ownership(error)
                    raise self._limits_error(error) from error
            motion = self._motion.refresh()
            if self._motion_calibration_validator is not None:
                from .calibrated_motion import validate_calibrated_motion
                from .emm_motion import MotionError

                record = self._motion_calibration_validator(compiled)
                try:
                    self._motion.install_experiment_calibration(record)
                except MotionError as error:
                    stopped = getattr(error, "stop_result", None)
                    if not isinstance(stopped, dict):
                        try:
                            stopped = self._motion.stop_all()
                        except Exception as stop_error:
                            stopped = {"stop_confirmed": False, "error": str(stop_error)}
                    cause = {"code": error.code, "message": str(error), "stop_result": copy.deepcopy(stopped)}
                    if isinstance(getattr(error, "wire_diagnostic", None), dict):
                        cause["wire"] = copy.deepcopy(error.wire_diagnostic)
                    failure = AutomationError("calibration_motion", "启动前的标定位置或设备检查未通过，实验未启动。双轴已确认停止，请重新进入标定核对当前位置。", "calibration", 409)
                    failure.cause = cause
                    if stopped.get("stop_confirmed") is not True:
                        self._state["execution_error"] = failure.as_dict()
                        failure = self._retain_motion_ownership(error)
                        failure.message = "启动检查失败，双轴停止尚未确认。请先松开方向键并点击「停止」；不会继续实验。"
                        failure.args = (failure.message,)
                        failure.cause = cause
                        self._state.update(error=failure.as_dict(), message=failure.message)
                        self._persist_best_effort()
                    raise failure from error
                motion = self._motion.snapshot()
                self._run_motion_calibration = validate_calibrated_motion(compiled, motion, record)
            if not motion.get("connected") or not motion.get("referenced") or any(
                    not all(motion.get("axes", {}).get(axis, {}).get(field) for field in
                            ("referenced", "enabled", "enable_confirmed", "telemetry_valid")) for axis in ("X", "Z")):
                raise AutomationError("live_gate", "Both axes require a known reference, explicit enable and valid telemetry", "bindings.motion.axes", 409)
        return enforced_limits

    def start(self, experiment, mode="simulate", plan_hash=None, idempotency_key=None):
        compiled = compile_experiment(experiment)
        if mode not in ("simulate", "live"):
            raise AutomationError("invalid_value", "Mode must be simulate or live", "mode")
        if plan_hash != compiled["plan_hash"]:
            raise AutomationError("plan_changed", "Preview the current experiment and submit its matching plan_hash", "plan_hash", 409)
        _text(idempotency_key, "idempotency_key", 200)
        identity = _hash({"mode": mode, "plan_hash": compiled["plan_hash"]})
        with self._lock:
            previous = self._idempotency.get(idempotency_key)
            if previous:
                if previous["identity"] != identity:
                    raise AutomationError("idempotency_conflict", "This idempotency key belongs to a different mode or plan", "idempotency_key", 409)
                return self.snapshot() if previous["run_id"] == self._state["run_id"] else copy.deepcopy(previous["result"])
            self._idle()
            if len(self._idempotency) >= 10000:
                raise AutomationError("idempotency_limit", "Idempotency registry reached its safety limit; archive this state directory", status=409)
            self._stop_event.clear()
            self._run_motion_calibration = None
            enforced_limits = self._live_gates(compiled) if mode == "live" else None
            if self._stop_event.is_set():
                raise AutomationError("cancelled", "STOP interrupted experiment preflight", "limits", 409)
            self._experiment = compiled["experiment"]
            self._set_ownership(compiled)
            self._recovery_required = False
            run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:10]
            root = Path(self._experiment.get("output_dir") or self.runs_dir)
            self._run_dir = root / mode / run_id
            self._run_dir.mkdir(parents=True, exist_ok=False)
            _write_json(self._run_dir / "experiment.json", self._experiment)
            _write_json(self._run_dir / "plan.json", compiled)
            if enforced_limits is not None:
                _write_json(self._run_dir / "absolute-motion-limits.json", enforced_limits)
            if self._run_motion_calibration is not None:
                _write_json(self._run_dir / "motion-calibration.json", self._run_motion_calibration)
            for name in ("events.jsonl", "samples.jsonl"):
                (self._run_dir / name).touch(exist_ok=False)
            channels = [{"id": electrode["id"], "name": electrode["name"],
                         "board_slot": "A" if CHANNELS.index(electrode["id"]) < 4 else "B",
                         "channel": CHANNELS.index(electrode["id"]) % 4 + 1,
                         "time_s": [], "current_nA": [], "valid": False, "latest_current_nA": None,
                         "synthetic": mode == "simulate"} for electrode in self._experiment["electrodes"]]
            self._state.update(state="running", mode=mode, run_id=run_id, step_index=-1, column=None,
                               message="Running synthetic simulation" if mode == "simulate" else "Running live experiment",
                               error=None, stop_confirmed=mode == "simulate", channels=channels,
                               pause_requested=False, column_timing=None, boards={}, execution_error=None,
                               position={"X": 0.0, "Z": 0.0} if mode == "simulate" else self.snapshot()["motion"]["position"],
                               manifest_path=str(self._run_dir / "manifest.json"))
            self._progress = Progress(compiled["steps"], self._state["position"],
                                      compiled["experiment"].get("execution"), started=time.time(),
                                      calibration=self._run_motion_calibration)
            self._resume_event.clear()
            self._audit_failure = None
            self._limit_failure = None
            self._motion_move_active = False
            self._guided_unknown_slots.clear()
            self._guided_missing_slots.clear()
            self._guided_attempts.clear()
            self._guided_last_step = None
            self._state["audit_error"] = None
            self._deadline = time.monotonic() + MAX_SECONDS
            self._idempotency[idempotency_key] = {"identity": identity, "run_id": run_id, "result": self.snapshot()}
            self._event("run_started", plan_hash=compiled["plan_hash"], mode=mode, warnings=compiled["warnings"],
                        **({"absolute_limits_fingerprint": _hash(enforced_limits)} if enforced_limits is not None else {}))
            self._persist()
            self._thread = threading.Thread(target=self._execute, args=(compiled, idempotency_key), name=f"automation-{run_id}", daemon=True)
            self._thread.start()
            return self.snapshot()

    def _check_cancelled(self):
        if self._limit_failure:
            raise AutomationError(self._limit_failure["code"], self._limit_failure["message"], "limits", 409)
        if self._audit_failure:
            raise AutomationError("audit_failed", self._audit_failure["message"], "artifacts")
        if self._stop_event.is_set():
            raise _Cancelled()
        if self._deadline and time.monotonic() >= self._deadline:
            raise AutomationError("runtime_limit", "Eight-hour runtime limit reached")

    def _wait(self, seconds):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self._check_cancelled()
            self._stop_event.wait(min(POLL_SECONDS, max(0, deadline - time.monotonic())))
        self._check_cancelled()

    def _sample(self, step, channel, data, config, synthetic=False):
        channel_index = CHANNELS.index(channel)
        slot = "A" if channel_index < 4 else "B"
        electrode = next(item for item in self._experiment["electrodes"] if item["id"] == channel)
        identity = {"slot": slot, "address": self._experiment["bindings"]["boards"][slot]["address"],
                    "protocol": v52_ble.PROTOCOL, "info": config.get("info"), "synthetic": synthetic}
        timestamp = data["host_unix_s"]
        record = {"run_id": self._state["run_id"], "step_id": step["id"], "column": step["column"],
                  "solution": step.get("solution"), "concentration": step.get("concentration"),
                  "electrode_id": channel, "electrode_name": electrode["name"], "board_slot": slot,
                  "board_address": identity["address"], "channel": channel_index % 4 + 1,
                  "host_utc": _utc(timestamp), "synthetic": synthetic, **data,
                  "device_fingerprint": _hash(identity), "electrode_fingerprint": _hash(electrode),
                  "config_fingerprint": _hash(config), "step_fingerprint": step["fingerprint"]}
        record.update({key: step[key] for key in ("cycle_index", "path_index", "visit_index") if key in step})
        record["sample_fingerprint"] = _hash(record)
        if self._experiment["schema_version"] == "1.1":
            with (self._run_dir / "raw-samples.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")
            if not data.get("effective", False):
                return
        with (self._run_dir / "samples.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")
        with self._lock:
            view = next(item for item in self._state["channels"] if item["id"] == channel)
            view["time_s"].append(data.get("elapsed_s", data["uptime_ms"] / 1000))
            view["current_nA"].append(data["current_fa"] / 1000000)
            view["time_s"] = view["time_s"][-2000:]
            view["current_nA"] = view["current_nA"][-2000:]
            view.update(latest_current_nA=data["current_fa"] / 1000000, valid=not bool(data["flags"] & 3))

    def _simulate_dwell(self, step):
        params = step["resolved_params"]
        guided = self._experiment["schema_version"] == "1.1"
        if guided:
            started = time.time()
            self._state["column_timing"] = {"column": step["column"], "step_id": step["id"],
                "duration_s": params["seconds"], "started_unix_s": started,
                "deadline_unix_s": started + params["seconds"], "elapsed_s": 0, "remaining_s": params["seconds"]}
        for tick in range(24):
            self._wait(params["seconds"] * self._simulation_scale / 24)
            if guided:
                with self._lock:
                    completed_seconds = params["seconds"] * (tick + 1) / 24
                    self._state["column_timing"].update(elapsed_s=completed_seconds,
                                                       remaining_s=params["seconds"] - completed_seconds)
            for channel in params["acquire"]:
                index = CHANNELS.index(channel)
                slot = "A" if index < 4 else "B"
                elapsed = params["seconds"] * (tick if guided else tick + 1) / 24
                metadata = {}
                if guided:
                    window = params["windows"][channel]
                    metadata = {"effective": window["start_s"] <= elapsed < window["start_s"] + window["duration_s"],
                                "window": window, "attempt_id": "synthetic-1", "run_token": self._state["run_id"]}
                self._sample(step, channel, {"host_unix_s": time.time(), "board_seq": tick * 4 + index % 4,
                             "uptime_ms": round(elapsed * 1000), "counts": 20000 + index * 100 + tick,
                             "current_fa": round((index + 1) * 1000000 + 10000 * math.sin(tick / 3)),
                             "flags": 0, "elapsed_s": elapsed, **metadata}, params["settings"][slot], True)
        if guided:
            self._state["column_timing"].update(elapsed_s=params["seconds"], remaining_s=0)

    def _execute(self, compiled, idempotency_key):
        final_state, failure = "completed", None
        try:
            self._start_limit_monitor()
            for index, step in enumerate(compiled["steps"]):
                self._check_cancelled()
                with self._lock:
                    self._state.update(step_index=index, column=step["column"], message=step["op"])
                    if self._progress:
                        self._progress.begin(index, time.time(), self._simulation_scale if self._state["mode"] == "simulate" else 1)
                self._event("step_started", step=step)
                params = step["resolved_params"]
                if step["op"].startswith("move_"):
                    if self._experiment["schema_version"] == "1.1" and step["op"] == "move_x" and self._guided_unknown_slots:
                        self._guided_wait_recovery()
                    if self._state["mode"] == "live":
                        with self._lock:
                            self._motion_move_active = True
                        try:
                            self._motion.move_to(params["axis"], params["target_mm"], params["speed_mm_s"],
                                                 cancel_event=_MotionCancellation(self._stop_event, self._deadline))
                        finally:
                            with self._lock:
                                self._motion_move_active = False
                    else:
                        distance = abs(self._state["position"][params["axis"]] - params["target_mm"])
                        self._wait(distance / params["speed_mm_s"] * self._simulation_scale)
                    self._check_cancelled()
                    with self._lock:
                        self._state["position"][params["axis"]] = params["target_mm"]
                    if self._experiment["schema_version"] == "1.1" and params.get("purpose") == "withdraw":
                        with self._lock:
                            if self._progress:
                                self._progress.complete(index)
                        self._guided_after_withdraw(index < len(compiled["steps"]) - 1)
                elif step["op"] == "wait_operator":
                    with self._lock:
                        self._resume_event.clear()
                        self._state.update(state="waiting_operator", message=params["message"])
                    self._event("waiting_operator", step_id=step["id"], message=params["message"])
                    while not self._resume_event.wait(POLL_SECONDS):
                        self._check_cancelled()
                    self._check_cancelled()
                    with self._lock:
                        self._state["state"] = "running"
                elif params.get("acquire"):
                    if self._state["mode"] == "simulate":
                        self._simulate_dwell(step)
                    else:
                        if self._experiment["schema_version"] == "1.1":
                            self._acquire_guided(step)
                        else:
                            self._acquire(step)
                else:
                    self._wait(params["seconds"] * (self._simulation_scale if self._state["mode"] == "simulate" else 1))
                self._event("step_completed", step_id=step["id"])
                with self._lock:
                    if self._progress and not (self._experiment["schema_version"] == "1.1"
                                              and params.get("purpose") == "withdraw"):
                        self._progress.complete(index)
        except _Cancelled:
            final_state = "stopped"
        except Exception as error:
            from .emm_motion import MotionError
            final_state = "error"
            if isinstance(error, AutomationError):
                failure = error.as_dict()
            elif self._require_absolute_limits and hasattr(error, "code"):
                failure = self._limits_error(error).as_dict()
                self._limit_failure = failure
            else:
                message = ("电机回包长度异常，实验已中断。请点击「重连并重置」，重新完成四点标定后再开始；原始回包保留在技术详情中。"
                           if isinstance(error, MotionError) and error.code == "response_length" else str(error))
                if (isinstance(error, MotionError) and error.code == "calibration_motion"
                        and str(error) in {f"{axis} feedback left the four-point experiment window" for axis in ("X", "Z")}):
                    message = f"{str(error)[0]} 轴位置反馈超出本次四点实验范围，实验已中止。请先确认双轴停止，再重新进入标定；原始原因已保留在技术详情中。"
                failure = AutomationError("execution_failed", message).as_dict()
            if isinstance(error, MotionError):
                failure["cause"] = {"code": error.code, "message": str(error)}
                if isinstance(getattr(error, "wire_diagnostic", None), dict):
                    failure["cause"]["wire"] = copy.deepcopy(error.wire_diagnostic)
        finally:
            self._stop_limit_monitor()
            if self._limit_failure:
                final_state, failure = "error", self._limit_failure
            parked_reference = None
            if (final_state == "completed" and not self._audit_failure and not self._stop_event.is_set()
                    and self._state["mode"] == "live" and self._experiment["schema_version"] == "1.1"
                    and self._owns_motion and self._motion is not None and self._parked_callback is not None):
                try:
                    with self._lock:
                        parked_reference = self._motion.reference_identity()
                except Exception as error:
                    final_state = "error"
                    failure = AutomationError("calibration_checkpoint", f"Parked reference could not be read: {error}").as_dict()
                    self._event("calibration_checkpoint_failed", error=failure)
            natural_completion = (final_state == "completed" and not self._audit_failure and not self._stop_event.is_set()
                                  and self._state["mode"] == "live" and self._run_motion_calibration is not None)
            confirmed = self._cleanup(natural_completion=natural_completion)
            if self._completion_failure is not None:
                final_state, failure = "error", copy.deepcopy(self._completion_failure)
                parked_reference = None
            if self._natural_park_proof is not None and not self._natural_proof_current():
                final_state = "stopped" if failure is None else "error"
                parked_reference = None
                confirmed = self._stop_invalidated_natural_park() and confirmed
            if confirmed and parked_reference is not None:
                try:
                    with self._lock:
                        self._parked_callback(self._motion, None if self._natural_park_proof is not None else parked_reference)
                except Exception as error:
                    final_state = "error"
                    failure = AutomationError("calibration_checkpoint", f"Parked reference could not be saved: {error}").as_dict()
                    self._event("calibration_checkpoint_failed", error=failure)
            if not confirmed:
                final_state = "error"
                self._state["execution_error"] = failure
                failure = AutomationError("stop_unconfirmed", "At least one output has no verified STOP; resource ownership is retained").as_dict()
            with self._lock:
                if self._natural_park_proof is not None and not self._natural_proof_current():
                    final_state = "stopped" if failure is None else "error"
                    confirmed = self._stop_invalidated_natural_park() and confirmed
                    if not confirmed:
                        self._state["execution_error"] = failure
                        failure = AutomationError("stop_unconfirmed", "STOP interrupted the final natural completion proof; ownership is retained").as_dict()
                        final_state = "error"
                self._state.update(state=final_state, stop_confirmed=confirmed, error=failure,
                                   message=final_state if failure is None else failure["message"])
                if self._progress:
                    self._progress.finished = time.time()
                self._recovery_required = not confirmed
                if confirmed:
                    self._owned_boards = []
                    self._owns_motion = False
                self._event("run_finished", state=final_state, stop_confirmed=confirmed, error=failure)
                self._manifest_best_effort(compiled)
                result = self.snapshot()
                result["busy"] = not confirmed
                self._idempotency[idempotency_key]["result"] = result
                self._persist_best_effort()
                self._run_dir = None

    def _manifest(self, compiled):
        files = {}
        for path in sorted(self._run_dir.rglob("*")):
            if path.is_file() and path.name != "manifest.json":
                digest = hashlib.sha256()
                with path.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(chunk)
                files[str(path.relative_to(self._run_dir))] = digest.hexdigest()
        _write_json(self._run_dir / "manifest.json", {"schema_version": "1.0", "run_id": self._state["run_id"],
                    "mode": self._state["mode"], "synthetic": self._state["mode"] == "simulate",
                    "state": self._state["state"], "stop_confirmed": self._state["stop_confirmed"], "error": self._state["error"],
                    "plan_hash": compiled["plan_hash"], "experiment_fingerprint": _hash(compiled["experiment"]),
                    "fingerprint_kind": "provenance_only", "hardware_synchronized": False,
                    "warnings": compiled["warnings"], "files": files, "finished_utc": _utc(time.time()),
                    "boards": self._state.get("boards", {}), "column_timing": self._state.get("column_timing"),
                    "progress": self.snapshot()["progress"],
                    **({"execution": compiled["experiment"]["execution"], "visits": [
                        {key: step[key] for key in ("id", "column", "cycle_index", "path_index", "visit_index")}
                        for step in compiled["steps"] if step["op"] == "dwell"]} if "execution" in compiled["experiment"] else {}),
                    "execution_error": self._state.get("execution_error"), "audit_error": self._state.get("audit_error")})

    def stop(self):
        self._stop_event.set()
        self._resume_event.set()
        with self._lock:
            self._stop_event.set()
            self._resume_event.set()
            parked_stop = (getattr(self, "_natural_park_proof", None) is not None
                           and self._state["mode"] == "live" and self._motion is not None
                           and self._state["state"] in ("completed", "stopped", "error"))
            if parked_stop:
                self._natural_park_proof = None
                self._owns_motion = True
                self._recovery_motion_config = copy.deepcopy(self._motion_config)
                self._recovery_required = True
                self._state["stop_confirmed"] = False
            if not self._busy() and not parked_stop:
                return self.snapshot()
            if self._limit_calibration_active:
                try:
                    self.limits.cancel(self._motion)
                except Exception as error:
                    self._event("limits_cancel_failed", error=str(error))
                self._limit_calibration_active = False
                self._owns_motion = True
                self._recovery_motion_config = copy.deepcopy(self._motion_config)
            self._state.update(state="stopping", message="Stopping all owned resources")
            self._event("stop_requested")
            for child in self._children:
                try:
                    child["cmd"].write_text("STOP\n", encoding="utf-8")
                except Exception as error:
                    self._event("collector_stop_request_failed", slot=child["slot"], error=str(error))
            if parked_stop or not self._thread or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._recover_stop, name="automation-stop-recovery", daemon=True)
                self._thread.start()
            return self.snapshot()

    def _recover_stop(self):
        confirmed, errors = True, []
        self._audit_failure = None
        self._state["audit_error"] = None
        try:
            manifest_path = self._state.get("manifest_path")
            if manifest_path and Path(manifest_path).parent.is_dir():
                self._run_dir = Path(manifest_path).parent
                orphan_commands = [*self._run_dir.glob("step-*/board-*/commands.txt"),
                                   *self._run_dir.glob("step-*/board-*/attempt-*/commands.txt")]
                for command_path in orphan_commands:
                    try:
                        command_path.write_text("STOP\n", encoding="utf-8")
                    except Exception as error:
                        confirmed = False
                        errors.append(str(error))
                        self._event("orphan_stop_request_failed", path=str(command_path), error=str(error))
            if self._children:
                self._stop_children()
                if any(child["process"].poll() is None for child in self._children):
                    raise AutomationError("stop_unconfirmed", "An old collector process still owns the BLE connection")
            for address in self._owned_boards:
                try:
                    if not address.strip():
                        raise AutomationError("recovery_identity", "Saved BLE identity is missing")
                    result = self._board_stop_factory(address)
                    stopped = isinstance(result, dict) and result.get("stop_confirmed") is True
                    confirmed = confirmed and stopped
                    self._event("recovery_board_stop", address=address, stop_confirmed=stopped, result=result)
                except Exception as error:
                    confirmed = False
                    errors.append(str(error))
                    self._event("recovery_board_stop", address=address, stop_confirmed=False, error=str(error))
        except Exception as error:
            confirmed = False
            errors.append(str(error))
        finally:
            if self._owns_motion:
                try:
                    if self._motion is None:
                        confirmed = False
                        errors.append("Reconnect the saved RS485 bus read-only, then explicitly retry STOP")
                    else:
                        if getattr(self._motion, "_transport_detached", False) and not self._motion.snapshot().get("connected"):
                            self._motion.connect()
                            self._event("recovery_motion_reconnected", port=self._motion.port)
                        result = self._motion.stop_all()
                        confirmed = bool(isinstance(result, dict) and result.get("stop_confirmed") is True) and confirmed
                        self._event("recovery_motion_stop", result=result)
                except Exception as error:
                    confirmed = False
                    errors.append(str(error))
                    self._event("recovery_motion_stop", stop_confirmed=False, error=str(error))
            with self._lock:
                self._recovery_required = not confirmed
                error = None if confirmed else AutomationError("stop_unconfirmed", "; ".join(errors) or "STOP remains unconfirmed").as_dict()
                self._state.update(state="stopped" if confirmed else "error", stop_confirmed=confirmed,
                                   message="Recovery STOP confirmed" if confirmed else error["message"], error=error)
                self._event("recovery_finished", stop_confirmed=confirmed, error=error)
                if confirmed:
                    self._children.clear()
                    self._owned_boards = []
                    self._owns_motion = False
                if self._run_dir:
                    self._manifest_best_effort(compile_experiment(self._experiment))
                result = self.snapshot()
                result["busy"] = not confirmed
                for entry in self._idempotency.values():
                    if entry["run_id"] == self._state["run_id"]:
                        entry["result"] = result
                self._persist_best_effort()
                self._run_dir = None

    def pause(self):
        with self._lock:
            if (self._experiment["schema_version"] != "1.1" or not self._thread or not self._thread.is_alive()
                    or self._state["state"] not in ("running", "waiting_recovery")):
                raise AutomationError("pause_rejected", "Only a running guided experiment can pause", status=409)
            self._state["pause_requested"] = True
            self._event("pause_requested")
            return self.snapshot()

    def resume(self):
        with self._lock:
            if self._state["state"] not in ("waiting_operator", "paused", "waiting_recovery"):
                raise AutomationError("resume_rejected", "Only an operator wait can resume; faults cannot auto-resume", status=409)
            self._resume_event.set()
            self._state["pause_requested"] = False
            self._event("operator_resumed")
            return self.snapshot()

    def _natural_proof_current(self):
        proof = self._natural_park_proof
        if not isinstance(proof, dict) or self._motion is None or self._stop_event.is_set():
            return False
        state = self._motion.snapshot()
        return (state.get("connected") is True and not state.get("faulted")
                and state.get("connection_token") == proof.get("connection_token")
                and state.get("stop_generation") == proof.get("stop_generation"))

    def _stop_invalidated_natural_park(self):
        self._natural_park_proof = None
        try:
            result = self._motion.stop_all()
            confirmed = isinstance(result, dict) and result.get("stop_confirmed") is True
            self._event("natural_park_interrupted_stop", result=result, stop_confirmed=confirmed)
            return confirmed
        except Exception as error:
            self._event("natural_park_interrupted_stop", error=str(error), stop_confirmed=False)
            return False

    def _cleanup(self, *, natural_completion=False):
        confirmed = True
        self._natural_park_proof = None
        self._completion_failure = None
        try:
            if self._children:
                confirmed = self._stop_children()
        except Exception as error:
            confirmed = False
            self._event("collector_cleanup_failed", error=str(error))
        finally:
            if self._limit_failure and self._state["mode"] == "live":
                for address in self._owned_boards:
                    try:
                        result = self._board_stop_factory(address)
                        stopped = isinstance(result, dict) and result.get("stop_confirmed") is True
                        confirmed = stopped and confirmed
                        self._event("limit_fault_board_stop", address=address, stop_confirmed=stopped)
                    except Exception as error:
                        confirmed = False
                        self._event("limit_fault_board_stop", address=address, stop_confirmed=False, error=str(error))
            if self._state["mode"] == "live" and self._owns_motion and self._motion:
                natural_attempted = False
                try:
                    verifier = getattr(self._motion, "verify_natural_park", None)
                    if (natural_completion and confirmed and not self._guided_unknown_slots and not self._audit_failure
                            and not self._stop_event.is_set() and callable(verifier)):
                        natural_attempted = True
                        proof = verifier(self._run_motion_calibration,
                                         cancel_event=_MotionCancellation(self._stop_event, self._deadline))
                        if not isinstance(proof, dict) or proof.get("basis") != "natural_finite_completion":
                            raise AutomationError("motion_completion", "Natural parking returned no valid completion proof", "motion", 409)
                        self._natural_park_proof = proof
                        if not self._natural_proof_current():
                            raise AutomationError("cancelled", "STOP interrupted natural parking verification", "motion", 409)
                        self._event("motion_naturally_parked", proof=copy.deepcopy(proof))
                    else:
                        confirmed = self._motion.stop_all().get("stop_confirmed") is True and confirmed
                except Exception as error:
                    self._event("motion_stop_failed", error=str(error))
                    self._natural_park_proof = None
                    if natural_attempted:
                        failure = AutomationError("motion_completion", "实验结束时的静止位置验证未通过，已请求双轴停止；数据已保留。", "motion", 409)
                        failure.cause = {"code": getattr(error, "code", "motion_completion"), "message": str(error)}
                        if isinstance(getattr(error, "wire_diagnostic", None), dict):
                            failure.cause["wire"] = copy.deepcopy(error.wire_diagnostic)
                        self._completion_failure = failure.as_dict()
                        try:
                            confirmed = self._motion.stop_all().get("stop_confirmed") is True and confirmed
                        except Exception as stop_error:
                            self._event("motion_stop_failed", error=str(stop_error))
                            confirmed = False
                    else:
                        confirmed = False
        return confirmed and not self._guided_unknown_slots

    def _start_limit_monitor(self):
        if not ((self._require_absolute_limits or self._run_motion_calibration is not None)
                and self._state["mode"] == "live" and self._owns_motion and self._motion):
            return
        self._limit_monitor_stop.clear()
        self._limit_monitor = threading.Thread(target=self._monitor_limits, name="automation-limit-monitor", daemon=True)
        self._limit_monitor.start()

    def _stop_limit_monitor(self):
        self._limit_monitor_stop.set()
        monitor = self._limit_monitor
        if monitor is not None and monitor is not threading.current_thread():
            monitor.join()
        self._limit_monitor = None

    def _monitor_limits(self):
        while not self._limit_monitor_stop.wait(POLL_SECONDS):
            if not self._lock.acquire(timeout=POLL_SECONDS):
                continue
            try:
                if self._limit_monitor_stop.is_set() or self._stop_event.is_set():
                    return
                if self._motion_move_active:
                    continue
                try:
                    if self._require_absolute_limits:
                        self.limits.require(self._motion)
                        self._motion.check_absolute_limits()
                    if self._run_motion_calibration is not None:
                        from .calibrated_motion import check_calibrated_feedback
                        if self._motion.snapshot().get("startup_approach_pending") is not True:
                            check_calibrated_feedback(self._motion.refresh(), self._run_motion_calibration)
                except Exception as error:
                    failure = self._limits_error(error).as_dict()
                    stop_result = getattr(error, "stop_result", None)
                    if not isinstance(stop_result, dict):
                        try:
                            stop_result = self._motion.stop_all()
                        except Exception as stop_error:
                            stop_result = {"stop_confirmed": False, "error": str(stop_error)}
                    self._limit_failure = failure
                    self._stop_event.set()
                    self._resume_event.set()
                    self._state.update(state="stopping", error=failure, message=failure["message"])
                    self._event("absolute_limit_fault", error=failure)
                    self._event("limit_fault_motion_stop", result=stop_result)
                    for child in self._children:
                        try:
                            child["cmd"].write_text("STOP\n", encoding="utf-8")
                        except Exception as stop_error:
                            self._event("collector_stop_request_failed", slot=child["slot"], error=str(stop_error))
                    return
            finally:
                self._lock.release()

    def _child_state(self, child):
        try:
            state = json.loads(child["state_path"].read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if (not isinstance(state, dict) or state.get("run_token") != child["token"]
                or child.get("attempt_id") is not None and state.get("attempt_id") != child["attempt_id"]):
            raise AutomationError("collector_identity", "Collector state token does not match this run", child["slot"])
        return state

    def _spawn_child(self, step, slot, mask, attempt=None):
        directory = self._run_dir / step["id"] / f"board-{slot}"
        attempt_id = None if attempt is None else f"{step['id']}-{slot}-{attempt:04d}"
        if attempt is not None:
            directory = directory / f"attempt-{attempt:04d}"
        directory.mkdir(parents=True, exist_ok=False)
        token = uuid.uuid4().hex
        settings = step["resolved_params"]["settings"][slot]
        _write_json(directory / "settings.json", settings)
        command_path = directory / "commands.txt"
        command_path.touch()
        paths = {"out": directory / "selected.csv", "all-channels": directory / "all-channels.csv",
                 "monitor": directory / "monitor.csv", "state-file": directory / "state.json",
                 "audit": directory / "audit.jsonl", "raw-log": directory / "raw.log",
                 "cmd-file": command_path, "settings": directory / "settings.json",
                 "start-gate": directory / "start-gate.json"}
        command = runtime.module_command("pa_host.collect_ble", "--address",
                    self._experiment["bindings"]["boards"][slot]["address"], "--run-token", token,
                    "--channel-mask", mask, *[value for key, path in paths.items() for value in (f"--{key}", path)])
        if attempt_id is not None:
            command.extend(["--attempt-id", attempt_id, "--fault-policy", "guided"])
        environment = os.environ.copy()
        environment["PYTHONUNBUFFERED"] = "1"
        if not runtime.is_frozen():
            environment["PYTHONPATH"] = str(self.project_dir / "software" / "host") + os.pathsep + environment.get("PYTHONPATH", "")
        log = (directory / "collector.log").open("w", encoding="utf-8")
        child = {"slot": slot, "address": self._experiment["bindings"]["boards"][slot]["address"],
                 "token": token, "cmd": command_path, "state_path": paths["state-file"],
                 "gate": paths["start-gate"], "samples": paths["all-channels"], "log": log,
                 "mask": mask, "offset": 0, "partial": "", "header_read": False,
                 "config": None, "first_host_time": None, "sample_counts": {}, "previous_seq": None}
        child.update(attempt_id=attempt_id, spawned_unix_s=time.time(), gate_released=False, step_id=step["id"],
                     start_ack_unix_s=None, start_requested_unix_s=None)
        try:
            if self._board_release_factory is not None:
                self._board_release_factory(child["address"])
            process = self._process_factory(command, cwd=self.project_dir, env=environment,
                        stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, text=True,
                        **(runtime.hidden_subprocess_kwargs(new_process_group=True) if os.name == "nt" else {"start_new_session": True}))
        except Exception:
            log.close()
            raise
        child["process"] = process
        with self._lock:
            self._children.append(child)
        self._event("collector_spawned", step_id=step["id"], slot=slot, run_token=token, channel_mask=mask)
        return child

    def _read_samples(self, child, step):
        try:
            with child["samples"].open("r", encoding="utf-8", newline="") as handle:
                handle.seek(child["offset"])
                chunk = handle.read(65536)
                child["offset"] = handle.tell()
        except FileNotFoundError:
            return
        lines = (child["partial"] + chunk).split("\n")
        child["partial"] = lines.pop()
        if len(child["partial"]) > 4096:
            raise AutomationError("collector_data", "Oversized sample record", child["slot"])
        for line in lines:
            if not line.strip():
                continue
            fields = next(csv.reader([line]))
            if not child["header_read"]:
                if fields != ["host_unix_s", "seq", "dev_ms", "channel", "counts", "fa_fw", "flags"]:
                    raise AutomationError("collector_data", "Unexpected collector sample columns", child["slot"])
                child["header_read"] = True
                continue
            try:
                if len(fields) != 7:
                    raise ValueError("Expected seven sample fields")
                timestamp = float(fields[0])
                sequence, uptime, board_channel, counts, current_fa, flags = map(int, fields[1:])
                if not math.isfinite(timestamp) or timestamp <= 0:
                    raise ValueError("Invalid host sample time")
                parsed = v52_ble.Sample.parse(f"D,{sequence},{uptime},{board_channel},{counts},{current_fa},{flags}")
                if parsed is None or not child["mask"] & (1 << (board_channel - 1)):
                    raise ValueError("Sample channel was not requested")
                sequence_gap = child["previous_seq"] is not None and sequence != child["previous_seq"] + 1
                if sequence_gap and child.get("attempt_id") is None:
                    raise ValueError("Board sequence discontinuity")
                child["previous_seq"] = sequence
                if flags & 2 and child.get("attempt_id") is None:
                    raise ValueError("Board FIFO overflow")
            except (ValueError, TypeError) as error:
                raise AutomationError("collector_data", str(error), child["slot"]) from error
            child["sample_counts"][board_channel] = child["sample_counts"].get(board_channel, 0) + 1
            if child["first_host_time"] is None:
                child["first_host_time"] = timestamp
            channel = CHANNELS[(0 if child["slot"] == "A" else 4) + board_channel - 1]
            metadata = {}
            elapsed = timestamp - child["first_host_time"]
            if child.get("attempt_id") is not None:
                timing = self._state["column_timing"]
                elapsed = timestamp - timing["started_unix_s"]
                window = step["resolved_params"]["windows"][channel]
                metadata = {"attempt_id": child["attempt_id"], "run_token": child["token"],
                            "sequence_gap": sequence_gap, "quality": "flagged" if flags & 3 else "sequence_gap" if sequence_gap else "valid",
                            "start_requested_unix_s": child["start_requested_unix_s"],
                            "start_ack_unix_s": child["start_ack_unix_s"], "window": window,
                            "effective": (timestamp >= child["start_ack_unix_s"]
                                          and window["start_s"] <= elapsed < window["start_s"] + window["duration_s"])}
                if timestamp < child["spawned_unix_s"]:
                    raise AutomationError("collector_identity", "Sample predates its collector attempt", child["slot"])
            self._sample(step, channel, {"host_unix_s": timestamp, "board_seq": sequence,
                         "uptime_ms": uptime, "counts": counts, "current_fa": current_fa, "flags": flags,
                         "elapsed_s": elapsed, **metadata}, child["config"])
        return len(chunk) == 65536

    def _guided_board(self, slot, **changes):
        with self._lock:
            board = self._state["boards"].setdefault(slot, {"phase": "unused", "attempt_id": None,
                "run_token": None, "start_ack_unix_s": None, "stop_confirmed": True,
                "output_state": "stopped", "gaps": []})
            board.update(changes)
        return board

    def _guided_spawn(self, step, slot, mask):
        self._guided_attempts[slot] = self._guided_attempts.get(slot, 0) + 1
        child = self._spawn_child(step, slot, mask, self._guided_attempts[slot])
        self._guided_board(slot, address=child["address"], phase="configuring", attempt_id=child["attempt_id"], run_token=child["token"],
                           start_ack_unix_s=None, stop_confirmed=False, output_state="unknown")
        return child

    def _guided_retire(self, child, step, error=None):
        slot = child["slot"]
        stopped = self._stop_children([child])
        if child["process"].poll() is None:
            raise AutomationError("stop_unconfirmed", "A collector still owns its BLE connection", slot)
        if child["config"] is not None and child["start_ack_unix_s"] is not None:
            while self._read_samples(child, step):
                self._check_cancelled()
        with self._lock:
            if child in self._children:
                self._children.remove(child)
        if stopped:
            self._guided_unknown_slots.discard(slot)
        else:
            self._guided_unknown_slots.add(slot)
        self._guided_board(slot, phase="retrying" if error else "stopped", stop_confirmed=stopped,
                           output_state="stopped" if stopped else "unknown", error=error)
        if error:
            self._guided_missing_slots.add(slot)
            timing = self._state["column_timing"]
            elapsed = timing["elapsed_s"] if timing else None
            board = self._guided_board(slot)
            if not board["gaps"] or board["gaps"][-1].get("end_s") is not None:
                board["gaps"].append({"column": step["column"], "start_s": elapsed, "end_s": None,
                                      "attempt_id": child["attempt_id"], "error": error})
            self._event("board_acquisition_gap", slot=slot, attempt_id=child["attempt_id"],
                        error=error, stop_confirmed=stopped)
        self._persist()

    def _guided_started(self, child, state, step):
        timing = self._state["column_timing"]
        if not timing or timing.get("step_id") != step["id"] or child.get("step_id") != step["id"]:
            raise AutomationError("collector_identity", "START acknowledgement belongs to another visit", child["slot"])
        acknowledgement = state.get("start_ack_unix_s")
        if acknowledgement is None or child["start_ack_unix_s"] is not None:
            return
        if (isinstance(acknowledgement, bool) or not isinstance(acknowledgement, (int, float))
                or not math.isfinite(acknowledgement) or acknowledgement < child["spawned_unix_s"]
                or acknowledgement > time.time() + 1):
            raise AutomationError("collector_identity", "Invalid START acknowledgement timestamp", child["slot"])
        child["start_ack_unix_s"] = acknowledgement
        child["start_requested_unix_s"] = state.get("start_requested_unix_s")
        timing = self._state["column_timing"]
        if timing["started_unix_s"] is None:
            first_acknowledgement, first_state = acknowledgement, state
            for peer in list(self._children):
                if peer is child or peer.get("attempt_id") is None or not peer["gate_released"]:
                    continue
                peer_state = self._child_state(peer)
                peer_acknowledgement = peer_state.get("start_ack_unix_s") if peer_state else None
                if (isinstance(peer_acknowledgement, (int, float)) and not isinstance(peer_acknowledgement, bool)
                        and math.isfinite(peer_acknowledgement) and peer["spawned_unix_s"] <= peer_acknowledgement < first_acknowledgement):
                    first_acknowledgement, first_state = peer_acknowledgement, peer_state
            started = first_state.get("start_ack_monotonic_s", time.monotonic() - (time.time() - first_acknowledgement))
            if not isinstance(started, (int, float)) or not math.isfinite(started) or started > time.monotonic() + 1:
                raise AutomationError("collector_identity", "Invalid START monotonic timestamp", child["slot"])
            timing.update(started_unix_s=first_acknowledgement,
                          deadline_unix_s=first_acknowledgement + timing["duration_s"])
            timing["_deadline_monotonic_s"] = started + timing["duration_s"]
            for slot, mask in step["resolved_params"]["channel_masks"].items():
                if mask and (slot != child["slot"] or acknowledgement > first_acknowledgement):
                    board = self._guided_board(slot)
                    board["gaps"].append({"column": step["column"], "start_s": 0, "end_s": None,
                                          "attempt_id": board["attempt_id"], "error": "START not yet acknowledged"})
            self._event("column_clock_started", step_id=step["id"], slot=child["slot"],
                        start_ack_unix_s=acknowledgement, deadline_unix_s=timing["deadline_unix_s"])
            for peer in list(self._children):
                if peer.get("attempt_id") is not None and peer["gate_released"]:
                    _write_json(peer["gate"], {"run_token": peer["token"], "attempt_id": peer["attempt_id"],
                        "deadline_unix_s": timing["deadline_unix_s"], "remaining_s": timing["remaining_s"]})
        self._guided_unknown_slots.discard(child["slot"])
        self._guided_missing_slots.discard(child["slot"])
        board = self._guided_board(child["slot"], phase="running", start_ack_unix_s=acknowledgement,
                                   output_state="running", stop_confirmed=False, error=None)
        if board["gaps"] and board["gaps"][-1].get("end_s") is None:
            board["gaps"][-1]["end_s"] = max(0, acknowledgement - timing["started_unix_s"])
        self._event("board_started", slot=child["slot"], attempt_id=child["attempt_id"],
                    start_requested_unix_s=child["start_requested_unix_s"], start_ack_unix_s=acknowledgement)

    def _acquire_guided(self, step):
        params = step["resolved_params"]
        slots = {slot: mask for slot, mask in params["channel_masks"].items() if mask}
        self._guided_last_step = step
        self._guided_missing_slots = set(slots)
        timing = {"column": step["column"], "step_id": step["id"], "duration_s": params["seconds"],
                  "started_unix_s": None, "deadline_unix_s": None, "elapsed_s": 0, "remaining_s": params["seconds"]}
        self._state["column_timing"] = timing
        active, retries, attempts = {}, dict.fromkeys(slots, 0.0), dict.fromkeys(slots, 0)
        gate_deadline = time.monotonic() + GATE_TIMEOUT_S
        while True:
            self._check_cancelled()
            now = time.monotonic()
            deadline = timing.get("_deadline_monotonic_s")
            if deadline is not None:
                timing.update(elapsed_s=min(params["seconds"], max(0, params["seconds"] - (deadline - now))),
                              remaining_s=max(0, deadline - now))
                if now >= deadline:
                    break
            elif now >= gate_deadline:
                raise AutomationError("collector_timeout", "No acquisition board acknowledged START before the bounded deadline")
            for slot, mask in slots.items():
                self._check_cancelled()
                child = active.get(slot)
                if child is None:
                    if now < retries[slot]:
                        continue
                    attempts[slot] += 1
                    child = active[slot] = self._guided_spawn(step, slot, mask)
                result = child["process"].poll()
                state = self._child_state(child)
                if state and state.get("error_kind") == "audit":
                    raise AutomationError("audit_failed", str(state.get("error")), slot)
                if state and isinstance(state.get("channel_quality"), dict):
                    self._guided_board(slot, channel_quality=state["channel_quality"])
                if state and state.get("config_confirmed") is True and isinstance(state.get("config"), dict):
                    child["config"] = state["config"]
                    if not child["gate_released"] and not state.get("error") and result is None:
                        if state.get("phase") != "configuring":
                            raise AutomationError("collector_identity", "Collector started before its gate", slot)
                        gate = {"run_token": child["token"], "attempt_id": child["attempt_id"]}
                        if timing["deadline_unix_s"] is not None:
                            gate.update(deadline_unix_s=timing["deadline_unix_s"], remaining_s=timing["remaining_s"])
                        _write_json(child["gate"], gate)
                        child["gate_released"] = True
                        self._event("start_gate_released", slot=slot, step_id=step["id"], **gate)
                    self._guided_started(child, state, step)
                    if child["start_ack_unix_s"] is not None:
                        while self._read_samples(child, step):
                            self._check_cancelled()
                if result is not None or state and state.get("error"):
                    error = str(state.get("error") if state else "Collector ended without state")
                    if result == 0 and state and state.get("stop_confirmed") is True:
                        error = "Acquisition ended before the column deadline"
                    self._guided_retire(child, step, error)
                    active.pop(slot)
                    retries[slot] = now + min(8.0, GUIDED_RETRY_BACKOFF_S * 2 ** min(attempts[slot] - 1, 3))
            self._wait(POLL_SECONDS)
        timing.update(elapsed_s=params["seconds"], remaining_s=0)
        for slot in slots:
            board = self._guided_board(slot)
            if board["gaps"] and board["gaps"][-1].get("end_s") is None:
                board["gaps"][-1]["end_s"] = params["seconds"]
        for child in list(active.values()):
            self._guided_retire(child, step)
        self._event("acquisition_completed", step_id=step["id"], gaps={slot: self._guided_board(slot)["gaps"] for slot in slots},
                    unknown_output_slots=sorted(self._guided_unknown_slots))

    def _guided_after_withdraw(self, has_more):
        if not has_more:
            return
        if self._state["pause_requested"]:
            self._guided_pause_at_safe_height()
        if self._state["mode"] == "live" and self._guided_last_step is not None:
            slots = {slot for slot, mask in self._guided_last_step["resolved_params"]["channel_masks"].items() if mask}
            if self._guided_unknown_slots or slots and slots <= self._guided_missing_slots:
                self._guided_wait_recovery()

    def _guided_pause_at_safe_height(self):
        with self._lock:
            self._resume_event.clear()
            self._state.update(state="paused", message="Paused at safe height after the column")
        self._event("paused_after_withdraw", column=self._state["column"])
        while not self._resume_event.wait(POLL_SECONDS):
            self._check_cancelled()
        self._check_cancelled()
        self._state["state"] = "running"

    def _guided_wait_recovery(self):
        step = self._guided_last_step
        if step is None:
            raise AutomationError("stop_unconfirmed", "Board output ownership remains unknown")
        masks = step["resolved_params"]["channel_masks"]
        needed = set(self._guided_unknown_slots) or {slot for slot, mask in masks.items() if mask}
        available = {slot for slot, mask in masks.items() if mask} - self._guided_missing_slots
        self._state.update(state="waiting_recovery", message="At safe height; waiting for board recovery and confirmed STOP")
        self._event("waiting_recovery", unknown_output_slots=sorted(self._guided_unknown_slots))
        active, retries = {}, dict.fromkeys(needed, 0.0)
        while self._guided_unknown_slots or not available:
            self._check_cancelled()
            for slot in list(needed):
                if slot not in active and time.monotonic() >= retries[slot]:
                    active[slot] = self._guided_spawn(step, slot, masks[slot])
                child = active.get(slot)
                if child is None:
                    continue
                result = child["process"].poll()
                state = self._child_state(child)
                if state and state.get("error_kind") == "audit":
                    raise AutomationError("audit_failed", str(state.get("error")), slot)
                ready = state and state.get("config_confirmed") is True and not state.get("error")
                if ready or result is not None:
                    self._guided_retire(child, step, None if ready else str(state.get("error") if state else "Board unavailable"))
                    active.pop(slot)
                    if ready and slot not in self._guided_unknown_slots:
                        available.add(slot)
                        needed.discard(slot)
                        self._guided_missing_slots.discard(slot)
                    retries[slot] = time.monotonic() + 5
            if self._guided_unknown_slots or not available:
                self._wait(POLL_SECONDS)
        for child in list(active.values()):
            self._guided_retire(child, step)
        self._state["state"] = "running"
        self._event("board_recovery_ready", slots=sorted(available))
        if self._state["pause_requested"]:
            self._guided_pause_at_safe_height()

    def _acquire(self, step):
        try:
            for slot, mask in step["resolved_params"]["channel_masks"].items():
                self._check_cancelled()
                if mask:
                    self._spawn_child(step, slot, mask)
            gate_deadline = time.monotonic() + GATE_TIMEOUT_S
            while True:
                self._check_cancelled()
                ready = True
                for child in self._children:
                    result = child["process"].poll()
                    state = self._child_state(child)
                    if result is not None or (state and (state.get("error") or state.get("phase") != "configuring")):
                        raise AutomationError("collector_config", "Collector failed or started before the common gate", child["slot"])
                    if not state or state.get("config_confirmed") is not True or not isinstance(state.get("config"), dict):
                        ready = False
                    else:
                        child["config"] = state["config"]
                if ready:
                    break
                if time.monotonic() >= gate_deadline:
                    raise AutomationError("collector_timeout", "All required boards must confirm configuration before START")
                self._wait(POLL_SECONDS)
            self._check_cancelled()
            self._event("all_configurations_confirmed", step_id=step["id"], slots=[child["slot"] for child in self._children])
            for child in self._children:
                self._check_cancelled()
                _write_json(child["gate"], {"run_token": child["token"]})
                self._event("start_gate_released", step_id=step["id"], slot=child["slot"], run_token=child["token"])
            acquisition_deadline = time.monotonic() + step["resolved_params"]["seconds"] + STOP_TIMEOUT_S + 15
            while True:
                self._check_cancelled()
                finished = True
                for child in self._children:
                    result = child["process"].poll()
                    state = self._child_state(child)
                    while self._read_samples(child, step):
                        self._check_cancelled()
                    if state and state.get("error"):
                        raise AutomationError("collector_failed", str(state["error"]), child["slot"])
                    if result is None:
                        finished = False
                    elif result != 0 or not state or state.get("phase") != "completed" or state.get("stop_confirmed") is not True:
                        raise AutomationError("collector_failed", "Collector did not complete with a verified STOP", child["slot"])
                    else:
                        expected = {channel for channel in range(1, 5) if child["mask"] & (1 << (channel - 1))}
                        if set(child["sample_counts"]) != expected or child["partial"]:
                            raise AutomationError("collector_data", "Required channels are missing or the last sample is truncated", child["slot"])
                if finished:
                    break
                if time.monotonic() >= acquisition_deadline:
                    raise AutomationError("collector_timeout", "Collector exceeded the bounded acquisition deadline")
                self._wait(POLL_SECONDS)
            self._event("acquisition_completed", step_id=step["id"])
        finally:
            children = list(self._children)
            stopped = self._stop_children()
            for child in children:
                if child["config"] is not None:
                    while self._read_samples(child, step):
                        pass
            if not stopped:
                raise AutomationError("stop_unconfirmed", "A board STOP is unconfirmed; further motion is blocked")

    def _stop_children(self, children=None):
        selected = children is not None
        children = list(self._children if children is None else children)
        def poll(child):
            try:
                return child["process"].poll()
            except Exception as error:
                self._event("collector_poll_failed", slot=child["slot"], error=str(error))
                return None
        for child in children:
            try:
                child["cmd"].write_text("STOP\n", encoding="utf-8")
            except Exception as error:
                self._event("collector_stop_request_failed", slot=child["slot"], error=str(error))
        deadline = time.monotonic() + STOP_TIMEOUT_S
        while any(poll(child) is None for child in children) and time.monotonic() < deadline:
            time.sleep(POLL_SECONDS)
        confirmed = True
        for child in children:
            process = child["process"]
            exited = poll(child) is not None
            if not exited:
                try:
                    process.terminate()
                except Exception as error:
                    self._event("collector_terminate_failed", slot=child["slot"], error=str(error))
                try:
                    process.wait(timeout=2)
                except Exception as error:
                    self._event("collector_wait_failed", slot=child["slot"], error=str(error))
                    try:
                        process.kill()
                    except Exception as kill_error:
                        self._event("collector_kill_failed", slot=child["slot"], error=str(kill_error))
                    try:
                        process.wait(timeout=2)
                    except Exception as wait_error:
                        self._event("collector_wait_failed", slot=child["slot"], error=str(wait_error))
            try:
                state = self._child_state(child)
            except AutomationError:
                state = None
            stopped = exited and state is not None and state.get("stop_confirmed") is True
            if child.get("attempt_id") is not None:
                if stopped:
                    self._guided_unknown_slots.discard(child["slot"])
                else:
                    self._guided_unknown_slots.add(child["slot"])
                self._guided_board(child["slot"], phase="stopped" if stopped else "retrying",
                                   stop_confirmed=stopped, output_state="stopped" if stopped else "unknown")
            confirmed = confirmed and stopped
            self._event("collector_stop_result", slot=child["slot"], stop_confirmed=stopped,
                        state=state, forced=not exited)
            try:
                child["log"].close()
            except Exception as error:
                self._audit_failed(error)
        if confirmed and not selected:
            with self._lock:
                self._children.clear()
        return confirmed
