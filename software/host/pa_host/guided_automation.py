"""Explicit template documents and persisted physical-column calibration."""

from __future__ import annotations

import copy
import json
import math
import threading
import uuid
from pathlib import Path

from .alignment_jog import AlignmentJog
from .automation import (CHANNELS, MAX_SECONDS, AutomationError, _keys, _number,
                         _object, _text, _validate_motion, _write_json, compile_experiment, default_experiment)


GUIDED_DEFAULTS = {"descend_speed_mm_s", "ascend_speed_mm_s", "translate_speed_mm_s", "settle_s", "dwell_s"}
RELOCK_MESSAGE = "手拧后原位锁定尚待实物验证，当前不释放电机"
FOUR_POINTS = ("first", "last", "working", "safe")
LEGACY_POINTS = ("first", "last", "safe")
PROFILE_FIELDS = ("firmware_version", "hardware_version", "config", "power_flag_baseline")


def default_guided_experiment(output_dir=""):
    legacy = default_experiment(output_dir)
    return {"schema_version": "guided-1.0", "name": "Template 1", "output_dir": str(output_dir),
            "bindings": legacy["bindings"], "safety": legacy["safety"],
            "defaults": {field: legacy["defaults"][field] for field in legacy["defaults"] if field in GUIDED_DEFAULTS},
            "columns": [{"index": index, "name": chr(64 + index), "enabled": True,
                         "duration_s": 180, "windows": {}} for index in range(1, 13)],
            "electrodes": [{"id": channel, "name": f"样品{channel}", "enabled": True} for channel in CHANNELS]}


def _effective_windows(column, defaults, channels):
    duration = defaults["dwell_s"] if column["duration_s"] is None else column["duration_s"]
    windows = {channel: copy.deepcopy(column.get("windows", {}).get(channel, {"start_s": 0, "duration_s": duration}))
               for channel in channels}
    return {channel: window for channel, window in windows.items() if window["duration_s"] > 0}


def _execution_settings(document):
    selected = sorted(column["index"] for column in document["columns"] if column["enabled"])
    if document.get("schema_version") == "guided-1.0":
        return {"mode": "once", "count": 1, "route": selected}
    settings = _object(document.get("execution"), "execution")
    if set(settings) != {"mode", "count", "route"}:
        raise AutomationError("invalid_value", "执行设置必须包含 mode、count 和 route", "execution")
    mode, count, route = settings["mode"], settings["count"], settings["route"]
    if mode not in ("once", "repeat", "custom"):
        raise AutomationError("invalid_value", "请选择一次性、重复或自定义路径实验", "execution.mode")
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 500:
        raise AutomationError("invalid_value", "实验轮数必须为 1–500 的整数", "execution.count")
    if mode != "repeat" and count != 1:
        raise AutomationError("invalid_value", "一次性和自定义路径只执行一轮", "execution.count")
    if mode != "custom":
        if route is not None:
            raise AutomationError("invalid_value", "一次性和重复实验使用勾选列，请将 route 设为 null", "execution.route")
        route = selected
    else:
        if not isinstance(route, list) or not route:
            raise AutomationError("invalid_value", "请提供非空列序列，例如 [1,3,5,3,1]", "execution.route")
        for index, number in enumerate(route):
            if isinstance(number, bool) or not isinstance(number, int) or not 1 <= number <= 12:
                raise AutomationError("invalid_value", "物理列号必须是 1–12 的整数，0 不能代替第 10 列", f"execution.route[{index}]")
            if number not in selected:
                raise AutomationError("invalid_value", f"自定义路径包含未勾选的第 {number} 列，请先启用此列", f"execution.route[{index}]")
    return {"mode": mode, "count": count, "route": copy.deepcopy(route)}


def _translated(document, calibration=None):
    experiment = default_experiment(document["output_dir"])
    experiment.update(schema_version="1.1", run_policy="guided", name=document["name"],
                      bindings=copy.deepcopy(document["bindings"]), safety=copy.deepcopy(document["safety"]),
                      calibration_id=calibration["id"] if calibration else "synthetic")
    channels = [channel for channel in CHANNELS if any(electrode["id"] == channel and electrode["enabled"]
                                                     for electrode in document["electrodes"])]
    experiment["defaults"].update(copy.deepcopy(document["defaults"]), channels=channels)
    if calibration:
        experiment["defaults"].update(x_first_mm=calibration["x_first_mm"],
                                      column_pitch_mm=calibration["column_pitch_mm"],
                                      z_immersed_mm=calibration["z_working_mm"], z_safe_mm=calibration["z_safe_mm"])
    else:
        axes = document["bindings"]["motion"]["axes"]
        experiment["defaults"].update(column_pitch_mm=min(9, axes["X"]["travel_mm"] / 12),
                                      z_immersed_mm=min(6, axes["Z"]["travel_mm"]))
    experiment["electrodes"] = [{"id": electrode["id"], "name": electrode["name"]}
                                for electrode in document["electrodes"]]
    experiment["columns"] = []
    experiment["program"] = []
    execution = _execution_settings(document)
    columns = {column["index"]: column for column in document["columns"]}
    for column in sorted(document["columns"], key=lambda item: item["index"]):
        if column["index"] not in execution["route"]:
            continue
        duration = document["defaults"]["dwell_s"] if column["duration_s"] is None else column["duration_s"]
        experiment["columns"].append({"index": column["index"], "solution": column["name"],
                                      "concentration": None, "overrides": {"dwell_s": duration}})
    for number in execution["route"]:
        column = columns[number]
        duration = document["defaults"]["dwell_s"] if column["duration_s"] is None else column["duration_s"]
        windows = _effective_windows(column, document["defaults"], channels)
        experiment["program"].extend([
            {"op": "move_to_column", "column": column["index"]}, {"op": "immerse"},
            {"op": "dwell", "seconds": duration, "acquire": list(windows), "windows": windows},
            {"op": "withdraw"}])
    if execution["count"] > 1:
        experiment["program"] = [{"op": "repeat", "count": execution["count"], "body": experiment["program"]}]
    if document["schema_version"] == "guided-1.1":
        experiment["execution"] = execution
    return experiment


def _validate_document(document):
    _object(document, "document")
    try:
        encoded = json.dumps(document, allow_nan=False)
    except (TypeError, ValueError, RecursionError) as error:
        raise AutomationError("invalid_value", "Document must contain finite JSON values", "document") from error
    if len(encoded.encode()) > 1024 * 1024:
        raise AutomationError("plan_limit", "Document exceeds 1 MiB", "document")
    version = document.get("schema_version")
    if version not in ("guided-1.0", "guided-1.1"):
        raise AutomationError("schema_version", "支持 guided-1.0 和 guided-1.1 模板", "schema_version")
    _keys(document, {"schema_version", "name", "output_dir", "bindings", "safety", "defaults", "columns", "electrodes"}
          | ({"execution"} if version == "guided-1.1" else set()), "document")
    _text(document.get("name"), "name")
    _text(document.get("output_dir"), "output_dir", 4096, True)
    bindings = _object(document.get("bindings"), "bindings")
    _keys(bindings, {"boards", "motion"}, "bindings")
    _validate_motion(bindings.get("motion"))
    defaults = _object(document.get("defaults"), "defaults")
    if set(defaults) != GUIDED_DEFAULTS:
        raise AutomationError("invalid_value", "Defaults must contain the five guided movement and timing parameters", "defaults")
    for field in GUIDED_DEFAULTS:
        _number(defaults[field], f"defaults.{field}", 0, MAX_SECONDS)
    electrodes = document.get("electrodes")
    if not isinstance(electrodes, list) or len(electrodes) != 8:
        raise AutomationError("invalid_value", "Define all eight physical electrodes", "electrodes")
    identifiers = []
    for index, electrode in enumerate(electrodes):
        path = f"electrodes[{index}]"
        _object(electrode, path)
        _keys(electrode, {"id", "name", "enabled"}, path)
        _text(electrode.get("id"), f"{path}.id", 1)
        _text(electrode.get("name"), f"{path}.name")
        if not isinstance(electrode.get("enabled"), bool):
            raise AutomationError("invalid_value", "Electrode selection must be boolean", f"{path}.enabled")
        identifiers.append(electrode["id"])
    if sorted(identifiers) != list(CHANNELS) or not any(electrode["enabled"] for electrode in electrodes):
        raise AutomationError("invalid_value", "Select at least one electrode with fixed identities A–H", "electrodes")
    channels = [electrode["id"] for electrode in electrodes if electrode["enabled"]]
    columns = document.get("columns")
    if not isinstance(columns, list) or len(columns) != 12:
        raise AutomationError("invalid_value", "Define all twelve physical columns", "columns")
    indices = []
    for index, column in enumerate(columns):
        path = f"columns[{index}]"
        _object(column, path)
        _keys(column, {"index", "name", "enabled", "duration_s", "windows"}, path)
        number = column.get("index")
        if isinstance(number, bool) or not isinstance(number, int) or not 1 <= number <= 12:
            raise AutomationError("invalid_value", "Physical column index must be an integer from 1 to 12", f"{path}.index")
        indices.append(number)
        _text(column.get("name"), f"{path}.name")
        if not isinstance(column.get("enabled"), bool):
            raise AutomationError("invalid_value", "Column selection must be boolean", f"{path}.enabled")
        if "duration_s" not in column:
            raise AutomationError("invalid_value", "Column duration or null inheritance is required", f"{path}.duration_s")
        duration = defaults["dwell_s"] if column["duration_s"] is None else column["duration_s"]
        _number(duration, f"{path}.duration_s", 0.0001, MAX_SECONDS)
        windows = _object(column.get("windows", {}), f"{path}.windows")
        _keys(windows, set(CHANNELS), f"{path}.windows")
        for channel, window in windows.items():
            window_path = f"{path}.windows.{channel}"
            _object(window, window_path)
            if set(window) != {"start_s", "duration_s"}:
                raise AutomationError("invalid_value", "A channel window needs start_s and duration_s", window_path)
            start = _number(window["start_s"], f"{window_path}.start_s", 0, duration)
            length = _number(window["duration_s"], f"{window_path}.duration_s", 0, duration)
            if start + length > duration:
                raise AutomationError("invalid_value", "A channel window cannot extend beyond the column duration", window_path)
        if column["enabled"] and not _effective_windows(column, defaults, channels):
            raise AutomationError("invalid_value", "An enabled column needs at least one positive acquisition window", f"{path}.windows")
    if sorted(indices) != list(range(1, 13)) or not any(column["enabled"] for column in columns):
        raise AutomationError("invalid_value", "Select at least one column with fixed identities 1–12", "columns")
    _execution_settings(document)
    result = copy.deepcopy(document)
    try:
        compile_experiment(_translated(result))
    except AutomationError as error:
        if error.code == "plan_limit" and version == "guided-1.1":
            raise AutomationError("plan_limit", "路径或轮数超出单次实验 500 个动作 / 8 小时上限，请减少列访问次数、轮数或停留时间", "execution") from error
        raise
    return result


def _saved_object(value, fields, path):
    _object(value, path)
    if set(value) != set(fields):
        raise AutomationError("invalid_value", "Saved calibration fields are incomplete or unknown", path)
    return value


def _saved_counts(value, path, *, raw=False):
    _saved_object(value, {"X", "Z"}, path)
    maximum = 0xFFFFFFFF if raw else 2 ** 40
    if any(isinstance(count, bool) or not isinstance(count, int) or not -maximum <= count <= maximum
           for count in value.values()):
        raise AutomationError("invalid_value", "Saved encoder counts must be bounded integers", path)
    return value


def _validate_saved_calibration(calibration):
    _object(calibration, "calibration")
    fields = {"id", "motion", "reference_valid", "reference", "x_first_mm", "x_last_mm",
              "column_pitch_mm", "z_working_mm", "z_safe_mm", "reference_margin_mm", "points"}
    four_point = isinstance(calibration.get("points"), dict) and "working" in calibration["points"]
    if four_point:
        fields.add("connection_token")
    if "completion_raw_counts" in calibration:
        fields.add("completion_raw_counts")
        _saved_counts(calibration["completion_raw_counts"], "calibration.completion_raw_counts", raw=True)
    _saved_object(calibration, fields, "calibration")
    if four_point:
        _text(calibration["connection_token"], "calibration.connection_token", 100)
    json.dumps(calibration, allow_nan=False)
    _text(calibration["id"], "calibration.id", 100)
    if not isinstance(calibration["reference_valid"], bool):
        raise AutomationError("invalid_value", "Saved reference validity must be boolean", "calibration.reference_valid")
    axes = _validate_motion(calibration["motion"])
    reference = _saved_object(calibration["reference"], {"version", "motion", "reference_counts", "raw_counts", "profiles"}, "calibration.reference")
    if reference["version"] != "emm-reference-1" or reference["motion"] != calibration["motion"]:
        raise AutomationError("invalid_value", "Saved reference motion identity differs", "calibration.reference")
    _saved_counts(reference["reference_counts"], "calibration.reference.reference_counts")
    _saved_counts(reference["raw_counts"], "calibration.reference.raw_counts", raw=True)
    profiles = _saved_object(reference["profiles"], {"X", "Z"}, "calibration.reference.profiles")
    for axis, mechanical in axes.items():
        path = f"calibration.reference.profiles.{axis}"
        profile = _saved_object(profiles[axis], {"firmware_version", "hardware_version", "config", "power_flag_baseline"}, path)
        for field in ("firmware_version", "hardware_version"):
            version = _number(profile[field], f"{path}.{field}", 1 if field == "firmware_version" else 0, 65534)
            if not isinstance(version, int):
                raise AutomationError("invalid_value", "Saved motor versions must be integers", path)
        if (isinstance(profile["power_flag_baseline"], bool) or not isinstance(profile["power_flag_baseline"], int)
                or profile["power_flag_baseline"] not in (0, 128)):
            raise AutomationError("invalid_value", "Invalid saved power flag", path)
        config = _saved_object(profile["config"], {"microstep", "step_angle", "pulses_per_rev", "response_mode",
                                                   "stall_protect", "position_window_deg", "raw_hex", "options_hex"}, f"{path}.config")
        raw = bytes.fromhex(_text(config["raw_hex"], f"{path}.config.raw_hex", 66))
        options = bytes.fromhex(_text(config["options_hex"], f"{path}.config.options_hex", 10))
        address = mechanical["address"]
        if (len(raw) != 33 or raw[:4] != bytes([address, 0x42, 0x21, 0x15]) or raw[-1] != 0x6B
                or len(options) != 5 or options[:2] != bytes([address, 0x1A]) or options[-1] != 0x6B
                or options[2] & 0x80):
            raise AutomationError("invalid_value", "Invalid saved motor configuration frames", path)
        data = raw[4:-1]
        if (data[0] not in (0x19, 0x32) or data[1:3] != b"\x02\x02" or data[4] != 0
                or data[16:19] != bytes([address, 0, 1]) or data[19] not in (0, 1)):
            raise AutomationError("invalid_value", "Unsupported saved motor configuration", path)
        microstep = data[5] or 256
        step_angle = 1.8 if data[0] == 0x19 else 0.9
        window = int.from_bytes(data[26:28], "big") / 10
        expected = {"microstep": microstep, "step_angle": step_angle,
                    "pulses_per_rev": microstep * (200 if data[0] == 0x19 else 400),
                    "response_mode": 1, "stall_protect": data[19], "position_window_deg": window}
        if (window > step_angle or any(isinstance(config[field], bool) or config[field] != value for field, value in expected.items())
                or any(not isinstance(config[field], int) for field in ("microstep", "pulses_per_rev", "response_mode", "stall_protect"))):
            raise AutomationError("invalid_value", "Saved parsed motor profile differs from its wire configuration", path)
    points = _saved_object(calibration["points"], FOUR_POINTS if four_point else LEGACY_POINTS, "calibration.points")
    positions = {point: {} for point in points}
    margins = _saved_object(calibration["reference_margin_mm"], {"X", "Z"}, "calibration.reference_margin_mm")
    for point, captured in points.items():
        path = f"calibration.points.{point}"
        _saved_object(captured, {"physical_column", "raw_counts"}, path)
        if (isinstance(captured["physical_column"], bool)
                or (point in ("first", "last") and not isinstance(captured["physical_column"], int))
                or captured["physical_column"] != {"first": 1, "last": 12, "working": None, "safe": None}[point]):
            raise AutomationError("invalid_value", "Saved calibration must use physical columns 1 and 12", path)
        _saved_counts(captured["raw_counts"], f"{path}.raw_counts", raw=True)
        for axis, mechanical in axes.items():
            sign = -1 if mechanical["positive_direction"] else 1
            positions[point][axis] = sign * (captured["raw_counts"][axis] - reference["reference_counts"][axis]) * mechanical["lead_mm"] / 65536
            _number(positions[point][axis], f"{path}.{axis}", 0, mechanical["travel_mm"])
    for axis, mechanical in axes.items():
        margin = _number(margins[axis], f"calibration.reference_margin_mm.{axis}", 1, 1 + mechanical["lead_mm"] / 65536)
        coordinate_samples = [position[axis] for position in positions.values()]
        if "completion_raw_counts" in calibration:
            sign = -1 if mechanical["positive_direction"] else 1
            completion = sign * (calibration["completion_raw_counts"][axis] - reference["reference_counts"][axis]) * mechanical["lead_mm"] / 65536
            _number(completion, f"calibration.completion_raw_counts.{axis}", 0, mechanical["travel_mm"])
            coordinate_samples.append(completion)
        if not math.isclose(min(coordinate_samples), margin, rel_tol=1e-12, abs_tol=1e-9):
            raise AutomationError("invalid_value", "Saved reference margin differs from captured encoder counts", "calibration.reference_margin_mm")
        sign = -1 if mechanical["positive_direction"] else 1
        parked = sign * (reference["raw_counts"][axis] - reference["reference_counts"][axis]) * mechanical["lead_mm"] / 65536
        tolerance = mechanical["lead_mm"] / 65536
        _number(parked, f"calibration.reference.raw_counts.{axis}", -tolerance, mechanical["travel_mm"] + tolerance)
    expected_geometry = {"x_first_mm": positions["first"]["X"], "x_last_mm": positions["last"]["X"],
                         "column_pitch_mm": (positions["last"]["X"] - positions["first"]["X"]) / 11,
                         "z_working_mm": positions["working" if four_point else "first"]["Z"],
                         "z_safe_mm": positions["safe"]["Z"]}
    for field, value in expected_geometry.items():
        _number(calibration[field], f"calibration.{field}", -10000, 10000)
        if not math.isclose(calibration[field], value, rel_tol=1e-12, abs_tol=1e-9):
            raise AutomationError("invalid_value", "Saved geometry differs from physical encoder captures", f"calibration.{field}")
    if abs(calibration["column_pitch_mm"]) < 0.0001 or calibration["z_safe_mm"] >= calibration["z_working_mm"]:
        raise AutomationError("invalid_value", "Saved calibration has invalid physical spacing or heights", "calibration")
    return copy.deepcopy(calibration)


def _calibration_geometry(points, settings, four_point, *, derive_coordinate_extents=False, completion_raw_counts=None):
    settings = copy.deepcopy(settings)
    reference_counts = {}
    reference_margin = {}
    positions = {point: {} for point in points}
    for axis, mechanical in settings["axes"].items():
        sign = -1 if mechanical["positive_direction"] else 1
        counts = [point["raw_counts"][axis] for point in points.values()]
        if completion_raw_counts is not None:
            counts.append(completion_raw_counts[axis])
        margin_counts = math.ceil(65536 / mechanical["lead_mm"])
        reference_counts[axis] = (min(counts) if sign == 1 else max(counts)) - sign * margin_counts
        reference_margin[axis] = margin_counts * mechanical["lead_mm"] / 65536
        for point, captured in points.items():
            position = sign * (captured["raw_counts"][axis] - reference_counts[axis]) * mechanical["lead_mm"] / 65536
            positions[point][axis] = position
        if derive_coordinate_extents:
            extent = max(sign * (count - reference_counts[axis]) * mechanical["lead_mm"] / 65536 for count in counts) + reference_margin[axis]
            mechanical["travel_mm"] = max(mechanical["travel_mm"], _number(extent, f"calibration.coordinate_extent.{axis}", 0, 10000))
        for point, position in positions.items():
            _number(position[axis], f"calibration.{point}.{axis}", 0, mechanical["travel_mm"])
    pitch = (positions["last"]["X"] - positions["first"]["X"]) / 11
    _number(abs(pitch), "calibration.column_pitch_mm", 0.0001, settings["axes"]["X"]["travel_mm"])
    working = "working" if four_point else "first"
    if positions["safe"]["Z"] >= positions[working]["Z"]:
        raise AutomationError("calibration_height", "提出安全Z必须高于浸入工作Z。已保留四点，请核对位置后重新读取这两个Z点，再点击完成标定。", "calibration.safe")
    calibration = {"id": uuid.uuid4().hex, "motion": copy.deepcopy(settings), "reference_valid": True,
                   "x_first_mm": positions["first"]["X"], "x_last_mm": positions["last"]["X"],
                   "column_pitch_mm": pitch, "z_working_mm": positions[working]["Z"],
                   "z_safe_mm": positions["safe"]["Z"], "reference_margin_mm": reference_margin,
                   "points": copy.deepcopy(points)}
    if completion_raw_counts is not None:
        calibration["completion_raw_counts"] = copy.deepcopy(completion_raw_counts)
    return calibration, reference_counts


class GuidedAutomation:
    def __init__(self, state_dir, *, require_four_point=False, alignment_error_callback=None):
        self._path = Path(state_dir) / "guided-automation.json"
        self._lock = threading.RLock()
        self._require_four_point = require_four_point is True
        self._document = default_guided_experiment()
        self._calibration = None
        self._motion = None
        self._calibrated_motion = None
        self._manual_identity = None
        self._alignment = None
        self._alignment_operation = threading.RLock()
        self._alignment_error_callback = alignment_error_callback
        self._manual = {"active": False, "supported": False, "status": "idle", "points": {},
                        "error": None, "message": RELOCK_MESSAGE,
                        "required_points": list(FOUR_POINTS if self._require_four_point else LEGACY_POINTS)}
        if self._require_four_point:
            self._manual.update(control_mode="keyboard", message="确认运动范围后开始键盘对位；保持两轴锁定，分别读取四个位置")
        if self._path.exists():
            try:
                saved = json.loads(self._path.read_text(encoding="utf-8"))
                self._document = _validate_document(saved["document"])
                calibration = saved.get("calibration")
                self._calibration = _validate_saved_calibration(calibration) if calibration is not None else None
                if saved.get("manual_interrupted"):
                    self._manual.update(active=True, status="blocked", error={"code": "manual_interrupted",
                                        "message": "Interrupted manual calibration requires explicit safe recovery"})
            except (OSError, ValueError, KeyError, TypeError, RecursionError, OverflowError) as error:
                self._calibration = None
                self._manual.update(active=True, status="blocked", error={"code": "state_read_failed", "message": str(error)})

    def _persist(self, *, document=None, calibration=None, replace_calibration=False, interrupted=None):
        value = {"document": self._document if document is None else document,
                 "calibration": calibration if replace_calibration else self._calibration,
                 "manual_interrupted": self._manual["active"] if interrupted is None else interrupted}
        try:
            _write_json(self._path, value)
        except (OSError, ValueError) as error:
            raise AutomationError("state_write_failed", f"Cannot save guided state: {error}", status=500) from error

    def _calibration_matches(self, document):
        return bool(self._calibration and self._calibration.get("reference_valid") is True
                    and self._calibration.get("motion") == document["bindings"]["motion"])

    @staticmethod
    def _four_point(calibration):
        return bool(calibration and set(calibration["points"]) == set(FOUR_POINTS)
                    and isinstance(calibration.get("connection_token"), str) and calibration["connection_token"])

    @staticmethod
    def _powered_identity(state):
        if state.get("connected") is not True or state.get("faulted") is True:
            raise ValueError("The powered motion connection is unavailable or faulted")
        token = state.get("connection_token")
        if not isinstance(token, str) or not token:
            raise ValueError("A current powered connection token is required")
        axes = state["axes"]
        return {"connection_token": token,
                "motion": {"port": state["port"], "baud": state["baud"],
                           "axes": {axis: copy.deepcopy(axes[axis]["mechanical"]) for axis in ("X", "Z")}},
                "profiles": {axis: {field: copy.deepcopy(axes[axis][field]) for field in PROFILE_FIELDS}
                             for axis in ("X", "Z")}}

    def _check_manual_identity(self, motion, captured=None):
        if self._manual_identity is None:
            if self._require_four_point:
                raise ValueError("A powered four-point session identity is required")
            return
        current = self._powered_identity(motion.snapshot())
        if current != self._manual_identity:
            raise ValueError("Connection, configuration or power identity changed during manual calibration")
        if captured is not None and any(captured.get(field) != current[field] for field in current):
            raise ValueError("Captured encoder identity differs from the powered manual session")

    def _cached_calibration_usable(self, calibration):
        if not self._four_point(calibration):
            return not self._require_four_point
        if self._calibrated_motion is None:
            return False
        try:
            state = self._calibrated_motion.snapshot()
            identity = self._powered_identity(state)
            if (identity["connection_token"] != calibration["connection_token"]
                    or identity["motion"] != calibration["motion"]
                    or identity["profiles"] != calibration["reference"]["profiles"]):
                return False
            if state.get("referenced") is True:
                return all(state["axes"][axis]["reference_count"] == calibration["reference"]["reference_counts"][axis]
                           for axis in ("X", "Z"))
            return all(abs(state["axes"][axis]["raw_count"] - calibration["reference"]["raw_counts"][axis]) <= 1
                       for axis in ("X", "Z"))
        except (ValueError, KeyError, TypeError, AttributeError):
            return False

    def snapshot(self):
        with self._lock:
            calibration = copy.deepcopy(self._calibration)
            if calibration:
                calibration["four_point"] = self._four_point(calibration)
                calibration["usable"] = (self._calibration_matches(self._document) and not self._manual["active"]
                                         and self._cached_calibration_usable(calibration))
                calibration["status"] = "saved" if calibration["usable"] else "stale"
            manual = copy.deepcopy(self._manual)
            if manual.get("control_mode") == "keyboard":
                manual["alignment"] = self.alignment_snapshot()
            return {"document": copy.deepcopy(self._document), "calibration": calibration,
                    "manual": manual, "four_point_required": self._require_four_point}

    def alignment_snapshot(self):
        with self._lock:
            manager = self._alignment
        if manager is not None:
            return manager.snapshot()
        return {"status": "idle", "active": False, "session_token": None, "press_id": None,
                "axis": None, "direction": None, "distance_mm": 0, "last_stop_reason": None,
                "error": None, "stop_result": None,
                "settings": {"speed_mm_s": 0.5, "max_speed_mm_s": 1.0, "hard_max_speed_mm_s": 20.0,
                             "fast_motion_confirmed": False,
                             "lease_ms": 350, "heartbeat_ms": 100, "max_hold_s": None, "max_hold_mm": None,
                             "hold_mode": "until_release"}}

    def _keyboard_manager(self):
        with self._lock:
            if (not self._manual["active"] or self._manual.get("control_mode") != "keyboard"
                    or self._alignment is None or self._manual["status"] == "blocked"):
                raise AutomationError("alignment_session", "An active healthy keyboard calibration session is required", status=409)
            return self._alignment

    def _check_keyboard_owner(self, motion, captured):
        state = motion.snapshot()
        manual = state.get("manual", {})
        if (manual.get("active") is not True or manual.get("control_mode") != "keyboard"
                or manual.get("session_token") != captured.get("session_token")
                or state.get("stop_generation") != captured.get("stop_generation")):
            raise AutomationError("alignment_session", "STOP or another session invalidated keyboard alignment", "calibration", 409)

    def _alignment_blocked(self, error, stop_result):
        try:
            with self._lock:
                self._manual.update(active=True, status="blocked", error=copy.deepcopy(error))
                self._persist(interrupted=True)
        finally:
            if self._alignment_error_callback:
                self._alignment_error_callback(error, stop_result)

    def _block_keyboard(self, error):
        with self._lock:
            manager = self._alignment
        if manager is not None:
            manager._halt("fault", force=True, error=error)
        raise AutomationError(getattr(error, "code", "alignment_error"), str(error), "calibration", 409) from error

    def begin_keyboard_calibration(self, motion, confirmed=False, boundary_at_current=None):
        if confirmed is not True:
            raise AutomationError("alignment_confirmation", "Confirm clear motion space before starting keyboard alignment", "motion_confirmed", 409)
        if motion is None:
            raise AutomationError("motion_disconnected", "Connect the motion bus before calibration", status=409)
        with self._alignment_operation:
            with self._lock:
                legacy_released = self._manual.get("control_mode") != "keyboard" and self._manual["status"] == "released"
                interrupted = (self._manual.get("error") or {}).get("code") == "manual_interrupted"
                state = motion.snapshot()
                if state.get("stop_confirmed") is False:
                    raise AutomationError("alignment_stop_unknown", "Recover unconfirmed motion STOP before beginning keyboard alignment", "calibration", 409)
                safe_retry = (self._manual["status"] == "blocked" and state.get("stop_confirmed") is True
                              and state.get("faulted") is not True
                              and all(state.get("axes", {}).get(axis, {}).get("speed_rpm") == 0
                                      and state["axes"][axis].get("telemetry_valid") is True for axis in ("X", "Z")))
                if self._manual["active"] and not (legacy_released or interrupted or safe_retry):
                    self._idle()
                try:
                    identity = self._powered_identity(state)
                except (ValueError, KeyError, TypeError, AttributeError) as error:
                    raise AutomationError("calibration_identity", str(error), "calibration", 409) from error
                if identity["motion"] != self._document["bindings"]["motion"]:
                    raise AutomationError("calibration_identity", "Connected motion settings must match the saved template", "bindings.motion", 409)
                old_manager = self._alignment
            if old_manager is not None:
                old_manager.retire()
            try:
                options = {} if boundary_at_current is None else {"boundary_at_current": boundary_at_current}
                captured = motion.begin_keyboard_alignment(confirmed=True, **options)
            except Exception as error:
                failure = AutomationError(getattr(error, "code", "alignment_begin"), str(error), "calibration", 409)
                failure.stop_result = getattr(error, "stop_result", None)
                raise failure from error
            with self._lock:
                self._motion = motion
                self._manual_identity = identity
                self._manual.update(active=True, supported=True, status="aligning", control_mode="keyboard",
                                    session_token=captured.get("session_token"), connection_token=identity["connection_token"],
                                    required_points=list(FOUR_POINTS), points={}, error=None,
                                    message="保持两轴锁定，按住方向键对位后分别读取四点")
                self._manual.pop("boundary_at_current", None)
                if self._calibration:
                    self._calibration["reference_valid"] = False
                try:
                    self._check_manual_identity(motion, captured)
                    self._check_keyboard_owner(motion, captured)
                    token = _text(captured.get("session_token"), "session_token", 100)
                    self._persist(interrupted=True)
                except Exception as error:
                    raise self._blocked(error) from error
            try:
                manager = AlignmentJog(motion, token, on_blocked=self._alignment_blocked)
            except Exception as error:
                self._alignment_blocked({"code": getattr(error, "code", "alignment_begin"), "message": str(error)},
                                        {"stop_confirmed": False, "axes": {}, "errors": []})
                raise AutomationError(getattr(error, "code", "alignment_begin"), str(error), "calibration", 409) from error
            try:
                with self._lock:
                    self._check_manual_identity(motion, captured)
                    self._check_keyboard_owner(motion, captured)
                    self._alignment = manager
            except Exception as error:
                manager.retire()
                with self._lock:
                    raise self._blocked(error) from error
            return self.snapshot()

    def configure_jog(self, payload):
        self._keyboard_manager().configure(payload)
        return self.snapshot()

    def start_jog(self, payload):
        self._keyboard_manager().start(payload)
        return self.snapshot()

    def heartbeat_jog(self, payload):
        self._keyboard_manager().heartbeat(payload)
        return self.snapshot()

    def stop_jog(self, payload):
        with self._lock:
            manager = self._alignment
        if manager is None:
            raise AutomationError("alignment_session", "No keyboard alignment session is owned", status=409)
        manager.stop(payload)
        return self.snapshot()

    def freeze_alignment(self):
        with self._lock:
            manager = self._alignment if self._manual["active"] else None
        if manager is not None:
            manager.freeze()
        return self.snapshot()

    def prepare_alignment_stop_recovery(self, motion):
        with self._lock:
            if (self._manual.get("control_mode") != "keyboard" or not self._manual["active"]
                    or self._alignment is None or motion is not self._motion):
                return None
            manager = self._alignment
        manager.prepare_stop_recovery()
        state = motion.snapshot()
        return {"manager": manager, "motion": motion, "generation": state["stop_generation"],
                "connection_token": state["connection_token"]}

    def reconcile_alignment_stop_recovery(self, recovery):
        with self._lock:
            if (self._alignment is not recovery["manager"] or self._motion is not recovery["motion"]
                    or self._manual["status"] != "blocked"):
                return
            before = recovery["motion"].snapshot()
            if before.get("connection_token") != recovery["connection_token"]:
                result = recovery["motion"]._current_stop_result()
                after = recovery["motion"].snapshot()
                if (not before.get("connected") or before.get("faulted") or not result.get("stop_confirmed")
                        or not before.get("connection_token") or before.get("connection_token") != after.get("connection_token")
                        or before.get("stop_generation") != after.get("stop_generation")
                        or after.get("stop_generation", -1) <= recovery["generation"]):
                    raise AutomationError("alignment_stop_unknown", "Reconnected bus requires fresh confirmed STOP before retiring old calibration", status=409)
                recovery["manager"].retire()
                self._alignment = None
                self._manual.update(session_token=None, connection_token=None,
                    error={"code": "manual_interrupted", "message": "RS485 reconnected and STOP confirmed; start a new keyboard calibration",
                           "cause": copy.deepcopy(self._manual.get("error"))})
            else:
                recovery["manager"].reconcile_stop_recovery(
                    recovery["motion"], recovery["generation"], recovery["connection_token"])
            self._manual["points"] = {}
            if self._calibration:
                self._calibration["reference_valid"] = False
            self._persist(interrupted=True)

    def reset_after_confirmed_stop(self, motion, boundary_at_current=None):
        with self._alignment_operation, self._lock:
            if motion is None or motion.snapshot().get("stop_confirmed") is not True:
                raise AutomationError("device_reset_stop_unknown", "停止尚未确认，不能清除当前设备会话", status=409)
            if self._alignment is not None:
                self._alignment.retire()
            self._alignment = None
            self._motion = self._manual_identity = self._calibrated_motion = None
            if self._calibration:
                self._calibration["reference_valid"] = False
            self._manual = {"active": False, "supported": False, "status": "idle", "points": {},
                            "error": None, "control_mode": "keyboard", "session_token": None,
                            "required_points": list(FOUR_POINTS),
                            "message": "设备状态已重置；请重新进入键盘标定，旧点位不再使用"}
            if boundary_at_current is not None:
                self._manual["boundary_at_current"] = copy.deepcopy(boundary_at_current)
            self._persist(interrupted=False)
            return self.snapshot()

    def resume_alignment(self):
        with self._alignment_operation:
            manager = self._keyboard_manager()
            with self._lock:
                motion, token = self._motion, self._manual["session_token"]
            try:
                captured = motion.capture_keyboard_alignment()
                with self._lock:
                    self._check_manual_identity(motion, captured)
                    self._check_keyboard_owner(motion, captured)
                    if captured.get("session_token") != token:
                        raise ValueError("Keyboard alignment session changed during readback")
                manager.resume(token)
            except Exception as error:
                self._block_keyboard(error)
            return self.snapshot()

    def _idle(self):
        if self._manual["active"]:
            message = ("上一次标定已中断。若要重新标定，请先确认双轴停止，再点击「重新开始键盘标定（旧点作废）」；旧点位不能沿用。"
                       if self._manual["status"] == "blocked" else
                       "当前标定尚未结束。若要重新开始或修改实验配置，请先点击「取消标定」，待停止确认后再操作；若要继续，请对齐并读取四个标定点。")
            raise AutomationError("manual_calibration_active", message, status=409)

    def bind_boards(self, addresses):
        if not isinstance(addresses, dict) or set(addresses) != {"A", "B"}:
            raise AutomationError("invalid_value", "请同时提交板 A 和板 B 的地址；未使用的板留空", "addresses")
        if any(not isinstance(value, str) or len(value) > 200 for value in addresses.values()):
            raise AutomationError("invalid_value", "蓝牙地址 / UUID 必须为文本", "addresses")
        addresses = {slot: value.strip() for slot, value in addresses.items()}
        if addresses["A"] and addresses["A"].casefold() == addresses["B"].casefold():
            raise AutomationError("duplicate_board", "板 A 和板 B 不能绑定同一蓝牙设备", "addresses")
        with self._lock:
            document = copy.deepcopy(self._document)
            for slot, address in addresses.items():
                document["bindings"]["boards"][slot]["address"] = address
            validated = _validate_document(document)
            self._persist(document=validated)
            self._document = validated
            return self.snapshot()

    def save(self, document):
        with self._lock:
            self._idle()
            validated = _validate_document(document)
            self._persist(document=validated)
            self._document = validated
            return self.snapshot()

    def compile(self, document, mode="simulate", motion=None):
        with self._lock:
            self._idle()
            validated = _validate_document(document)
            if mode not in ("simulate", "live"):
                raise AutomationError("invalid_value", "Mode must be simulate or live", "mode")
            calibration = self._calibration if self._calibration_matches(validated) else None
            if mode == "live":
                calibration = self._live_calibration(validated, motion)
            compiled = compile_experiment(_translated(validated, calibration))
            if calibration is None:
                compiled["warnings"].append("Synthetic simulation geometry only; it cannot authorize live motion or replace physical calibration.")
            return compiled

    def _live_calibration(self, document, motion):
        calibration = self._calibration if self._calibration_matches(document) else None
        if calibration is None or (self._require_four_point and not self._four_point(calibration)):
            raise AutomationError("calibration_required", "A saved matching current-session physical calibration is required", "calibration", 409)
        if motion is None:
            raise AutomationError("calibration_reference", "Connect the calibrated motion bus first", "calibration", 409)
        try:
            state = motion.snapshot()
            if self._four_point(calibration):
                identity = self._powered_identity(state)
                if (identity["connection_token"] != calibration["connection_token"]
                        or identity["motion"] != calibration["motion"]
                        or identity["profiles"] != calibration["reference"]["profiles"]):
                    raise ValueError("Saved calibration belongs to another powered connection or motor profile")
            if not state.get("referenced"):
                motion.restore_reference(copy.deepcopy(calibration["reference"]))
            current = motion.reference_identity()
            if any(current[field] != calibration["reference"][field] for field in ("motion", "reference_counts", "profiles")):
                raise ValueError("Saved reference identity differs from the connected motion bus")
            if self._four_point(calibration):
                state = motion.snapshot()
                if self._powered_identity(state) != identity:
                    raise ValueError("Powered calibration identity changed while verifying the reference")
                if any(state["axes"][axis]["reference_count"] != current["reference_counts"][axis]
                       or state["axes"][axis]["raw_count"] != current["raw_counts"][axis] for axis in ("X", "Z")):
                    raise ValueError("Verified encoder reference differs from the current connected snapshot")
            self._calibrated_motion = motion
            return calibration
        except (ValueError, KeyError, TypeError, AttributeError) as error:
            raise AutomationError("calibration_reference", f"Cannot trust saved calibration reference: {error}", "calibration", 409) from error

    def trusted_calibration(self, motion):
        with self._lock:
            self._idle()
            return copy.deepcopy(self._live_calibration(self._document, motion))

    def _driver(self, motion):
        if not self._manual["active"] or motion is None or motion is not self._motion:
            raise AutomationError("manual_calibration_session", "An explicit manual session on the same motion connection is required", status=409)

    def _blocked(self, error):
        self._manual.update(active=True, status="blocked", error={"code": getattr(error, "code", "manual_calibration_error"),
                                                                 "message": str(error)})
        self._persist(interrupted=True)
        return AutomationError(getattr(error, "code", "manual_calibration_error"), str(error), "calibration", 409)

    def begin_calibration(self, motion, *, release_confirmed=False, z_supported=False):
        with self._lock:
            self._idle()
            if self._require_four_point and (release_confirmed is not True or z_supported is not True):
                raise AutomationError("manual_release_confirmation", "Confirm axis release and that Z is supported before manual calibration", "calibration", 409)
            if motion is None:
                raise AutomationError("motion_disconnected", "Connect the motion bus before calibration", status=409)
            supported = (getattr(motion, "manual_capture_supported", False) if self._require_four_point
                         else getattr(motion, "manual_calibration_supported", False))
            if supported is not True:
                raise AutomationError("manual_relock_unqualified", RELOCK_MESSAGE, "calibration", 409)
            state = motion.snapshot()
            motion_settings = {"port": state.get("port"), "baud": state.get("baud"),
                               "axes": {axis: state.get("axes", {}).get(axis, {}).get("mechanical") for axis in ("X", "Z")}}
            if motion_settings != self._document["bindings"]["motion"]:
                raise AutomationError("calibration_identity", "The connected motion settings must match the saved template", "bindings.motion", 409)
            try:
                identity = self._powered_identity(state) if self._require_four_point or state.get("connection_token") else None
            except (ValueError, KeyError, TypeError) as error:
                raise AutomationError("calibration_identity", str(error), "calibration", 409) from error
            self._persist(interrupted=True)
            self._motion = motion
            self._manual_identity = identity
            self._manual.update(active=True, supported=True, status="releasing", control_mode="manual", points={}, error=None)
            if identity:
                self._manual["connection_token"] = identity["connection_token"]
            if self._calibration:
                self._calibration["reference_valid"] = False
            try:
                if self._require_four_point:
                    captured = motion.begin_manual_calibration(release_confirmed=True, z_supported=True)
                else:
                    captured = motion.begin_manual_calibration()
                self._check_manual_identity(motion, captured)
                self._manual["status"] = "released"
                self._persist()
            except Exception as error:
                raise self._blocked(error) from error
            return self.snapshot()

    def capture_calibration(self, motion, point):
        with self._lock:
            keyboard = self._manual.get("control_mode") == "keyboard"
        if keyboard:
            return self._capture_keyboard_calibration(motion, point)
        with self._lock:
            self._driver(motion)
            if point not in FOUR_POINTS:
                raise AutomationError("invalid_value", "Point must be first, last, working or safe", "point")
            try:
                self._check_manual_identity(motion)
                captured = motion.capture_manual_position()
                self._check_manual_identity(motion, captured)
                if captured["motion"] != self._document["bindings"]["motion"]:
                    raise ValueError("Motion settings changed during manual calibration")
                counts = captured["raw_counts"]
                _saved_counts(counts, "calibration.raw_counts", raw=True)
            except Exception as error:
                raise self._blocked(error) from error
            self._manual["points"][point] = {"physical_column": {"first": 1, "last": 12, "working": None, "safe": None}[point],
                                             "raw_counts": copy.deepcopy(counts)}
            self._manual.update(status="released", error=None)
            return self.snapshot()

    def finish_calibration(self, motion):
        with self._lock:
            keyboard = self._manual.get("control_mode") == "keyboard"
        if keyboard:
            return self._finish_keyboard_calibration(motion)
        with self._lock:
            self._driver(motion)
            points = self._manual["points"]
            four_point = self._require_four_point or "working" in points
            if set(points) != set(FOUR_POINTS if four_point else LEGACY_POINTS):
                raise AutomationError("calibration_points", "Capture physical columns 1 and 12, independent working Z and safe Z" if four_point
                                      else "Capture physical columns 1 and 12 and the safe height", "calibration")
            try:
                self._check_manual_identity(motion)
                if four_point and self._manual_identity is None:
                    raise ValueError("A powered connection identity is required for four independent captures")
            except (ValueError, KeyError, TypeError, AttributeError) as error:
                raise self._blocked(error) from error
            settings = self._document["bindings"]["motion"]
            calibration, reference_counts = _calibration_geometry(points, settings, four_point)
            try:
                if four_point:
                    calibration["connection_token"] = self._manual_identity["connection_token"]
                calibration["reference"] = motion.finish_manual_calibration(reference_counts)
                if (calibration["reference"]["motion"] != settings
                        or calibration["reference"]["reference_counts"] != reference_counts):
                    raise ValueError("Manual relock did not confirm the captured software reference")
                self._check_manual_identity(motion)
                if four_point and calibration["reference"]["profiles"] != self._manual_identity["profiles"]:
                    raise ValueError("Motor profile changed during manual relock")
                if four_point:
                    state = motion.snapshot()
                    if any(state["axes"][axis].get("enabled") is not True
                           or state["axes"][axis].get("enable_confirmed") is not True for axis in ("X", "Z")):
                        raise AutomationError("enable_unconfirmed", "Both axes require confirmed current-position relock", "calibration", 409)
                _validate_saved_calibration(calibration)
                self._persist(calibration=calibration, replace_calibration=True, interrupted=False)
            except Exception as error:
                raise self._blocked(error) from error
            self._calibration = calibration
            self._manual.update(active=False, status="complete", error=None)
            self._calibrated_motion = motion
            self._motion = None
            self._manual_identity = None
            return self.snapshot()

    def cancel_calibration(self, motion):
        with self._lock:
            keyboard = self._manual.get("control_mode") == "keyboard"
        if keyboard:
            return self._cancel_keyboard_calibration(motion)
        with self._lock:
            self._driver(motion)
            try:
                self._check_manual_identity(motion)
                motion.cancel_manual_calibration()
                self._persist(interrupted=False)
            except Exception as error:
                raise self._blocked(error) from error
            self._manual.update(active=False, status="cancelled", error=None)
            self._motion = None
            self._manual_identity = None
            return self.snapshot()

    def _capture_keyboard_calibration(self, motion, point):
        with self._alignment_operation:
            with self._lock:
                self._driver(motion)
                if point not in FOUR_POINTS:
                    raise AutomationError("invalid_value", "Point must be first, last, working or safe", "point")
                token = self._manual["session_token"]
            self.freeze_alignment()
            try:
                captured = motion.capture_keyboard_alignment()
                with self._lock:
                    self._check_manual_identity(motion, captured)
                    self._check_keyboard_owner(motion, captured)
                    if captured.get("session_token") != token:
                        raise ValueError("Keyboard alignment session changed during capture")
                    counts = _saved_counts(captured["raw_counts"], "calibration.raw_counts", raw=True)
                    self._manual["points"][point] = {
                        "physical_column": {"first": 1, "last": 12, "working": None, "safe": None}[point],
                        "raw_counts": copy.deepcopy(counts)}
                    self._manual.update(status="aligning", error=None)
                self.resume_alignment()
            except Exception as error:
                if self.snapshot()["manual"]["status"] == "blocked":
                    raise
                self._block_keyboard(error)
            return self.snapshot()

    def _finish_keyboard_calibration(self, motion):
        with self._alignment_operation:
            try:
                with self._lock:
                    self._driver(motion)
                    points = copy.deepcopy(self._manual["points"])
                    if set(points) != set(FOUR_POINTS):
                        raise AutomationError("calibration_points", "请先读取第1列X、第12列X、浸入工作Z、提出安全Z四个点", "calibration")
                    original_settings = copy.deepcopy(self._document["bindings"]["motion"])
                    calibration, reference_counts = _calibration_geometry(points, original_settings, True,
                                                                         derive_coordinate_extents=True)
                    settings = calibration["motion"]
                    document = copy.deepcopy(self._document)
                    document["bindings"]["motion"] = copy.deepcopy(settings)
                    document = _validate_document(document)
                    token = self._manual["session_token"]
            except AutomationError as error:
                if error.code not in {"calibration_points", "calibration_height", "invalid_value"}:
                    raise
                self.resume_alignment()
                with self._lock:
                    self._manual.update(status="aligning", error={"code": error.code, "message": str(error), "path": error.path},
                                        message="标定尚未完成：四点记录保留，请按提示重新读取有问题的点后再完成")
                raise
            self.freeze_alignment()
            manager = self._keyboard_manager()
            geometry_pending = False
            try:
                captured = motion.capture_keyboard_alignment()
                with self._lock:
                    self._check_manual_identity(motion, captured)
                    self._check_keyboard_owner(motion, captured)
                    if captured.get("session_token") != token:
                        raise ValueError("Keyboard alignment session changed before finish")
                    geometry_pending = True
                    calibration, reference_counts = _calibration_geometry(points, original_settings, True,
                        derive_coordinate_extents=True,
                        completion_raw_counts=_saved_counts(captured["raw_counts"], "calibration.completion_raw_counts", raw=True))
                    settings = calibration["motion"]
                    document = copy.deepcopy(self._document)
                    document["bindings"]["motion"] = copy.deepcopy(settings)
                    document = _validate_document(document)
                    geometry_pending = False
                    calibration["connection_token"] = self._manual_identity["connection_token"]
                extent_options = {} if settings == original_settings else {"coordinate_extents": {
                    axis: settings["axes"][axis]["travel_mm"] for axis in ("X", "Z")}}
                reference = motion.finish_keyboard_alignment(reference_counts, **extent_options)
                with self._lock:
                    expected_identity = {**self._manual_identity, "motion": settings}
                    if self._powered_identity(motion.snapshot()) != expected_identity:
                        raise ValueError("Keyboard finish changed more than the verified software coordinate extents")
                    if (reference["motion"] != settings or reference["reference_counts"] != reference_counts
                            or reference["profiles"] != self._manual_identity["profiles"]):
                        raise ValueError("Keyboard finish did not confirm the captured software reference and profiles")
                    state = motion.snapshot()
                    if any(state["axes"][axis].get("enabled") is not True
                           or state["axes"][axis].get("enable_confirmed") is not True for axis in ("X", "Z")):
                        raise AutomationError("enable_unconfirmed", "Both axes must remain confirmed locked", "calibration", 409)
                    calibration["reference"] = reference
                    _validate_saved_calibration(calibration)
                    self._persist(document=document, calibration=calibration, replace_calibration=True, interrupted=False)
                    self._document = document
                    self._calibration = calibration
                    self._manual.update(active=False, status="complete", error=None)
                    self._calibrated_motion = motion
                    self._motion = None
                    self._manual_identity = None
                manager.close(stop=False)
            except Exception as error:
                if geometry_pending and isinstance(error, AutomationError) and error.code in {"invalid_value", "calibration_height"}:
                    self.resume_alignment()
                    with self._lock:
                        self._manual.update(status="aligning", error={"code": error.code, "message": str(error), "path": error.path},
                                            message="标定尚未完成：四点记录保留，请核对当前位置与记录后重试")
                    raise
                self._block_keyboard(error)
            return self.snapshot()

    def _cancel_keyboard_calibration(self, motion):
        with self._alignment_operation:
            with self._lock:
                self._driver(motion)
            self.freeze_alignment()
            manager = self._keyboard_manager()
            try:
                with self._lock:
                    self._check_manual_identity(motion)
                motion.cancel_keyboard_alignment()
                with self._lock:
                    self._persist(interrupted=False)
                    self._manual.update(active=False, status="cancelled", error=None)
                    self._motion = None
                    self._manual_identity = None
                manager.close(stop=False)
            except Exception as error:
                self._block_keyboard(error)
            return self.snapshot()

    def checkpoint_reference(self, motion, reference_before_stop=None):
        with self._lock:
            self._idle()
            if not self._calibration_matches(self._document) or motion is None:
                return self.snapshot()
            try:
                if self._four_point(self._calibration):
                    identity = self._powered_identity(motion.snapshot())
                    if (identity["connection_token"] != self._calibration["connection_token"]
                            or identity["profiles"] != self._calibration["reference"]["profiles"]):
                        raise ValueError("Powered reference checkpoint belongs to a different session")
                current = (motion.reference_identity() if reference_before_stop is None
                           else motion.parked_reference_checkpoint(copy.deepcopy(reference_before_stop)))
                saved = self._calibration["reference"]
                if any(current[field] != saved[field] for field in ("motion", "reference_counts", "profiles")):
                    raise ValueError("Reference identity changed")
                calibration = {**self._calibration, "reference": current}
                self._persist(calibration=calibration, replace_calibration=True)
                self._calibration = calibration
            except (ValueError, KeyError, TypeError, AttributeError) as error:
                raise AutomationError("calibration_reference", f"Cannot save reference checkpoint: {error}", "calibration", 409) from error
            return self.snapshot()
