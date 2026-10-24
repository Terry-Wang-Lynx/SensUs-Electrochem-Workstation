"""Persistent operating boundaries tied to one verified raw-counter connection."""

from __future__ import annotations

import copy
import json
import math
import time
import uuid
from pathlib import Path

from .emm_motion import MotionError, validate_axis_config


BOUNDARIES = {"x_left": "X", "x_right": "X", "z_up": "Z", "z_down": "Z"}
RECORD_VERSION = "absolute-motion-limits-1"


def _invalid(message):
    raise MotionError("invalid_absolute_limits", message)


def _counts(values, expected):
    if (not isinstance(values, dict) or set(values) != set(expected)
            or any(isinstance(value, bool) or not isinstance(value, int) or abs(value) > 0xFFFFFFFF
                   for value in values.values())):
        _invalid("Absolute limit counts require exact bounded integers")


def _binding(value):
    motion = value.get("motion")
    if (not isinstance(motion, dict) or set(motion) != {"port", "baud", "axes"}
            or not isinstance(motion["port"], str) or not motion["port"].strip()
            or isinstance(motion["baud"], bool) or not isinstance(motion["baud"], int)
            or not 1200 <= motion["baud"] <= 921600
            or not isinstance(motion["axes"], dict) or set(motion["axes"]) != {"X", "Z"}):
        _invalid("An exact motion identity is required")
    for axis, config in motion["axes"].items():
        if set(config) != {"address", "lead_mm", "travel_mm", "positive_direction", "max_speed_mm_s"}:
            _invalid("Unexpected mechanical profile fields")
        validate_axis_config(config, axis)
    if motion["axes"]["X"]["address"] == motion["axes"]["Z"]["address"]:
        _invalid("Axis addresses must be unique")
    token = value.get("connection_token")
    try:
        if not isinstance(token, str) or str(uuid.UUID(token)) != token:
            _invalid("A connection UUID is required")
    except (ValueError, AttributeError):
        _invalid("A connection UUID is required")
    generation = value.get("stop_generation")
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
        _invalid("A STOP generation is required")
    profiles = value.get("profiles")
    if not isinstance(profiles, dict) or set(profiles) != {"X", "Z"}:
        _invalid("Both complete motor profiles are required")
    for axis, profile in profiles.items():
        if not isinstance(profile, dict) or set(profile) != {"firmware_version", "hardware_version", "config", "power_flag_baseline"}:
            _invalid("A complete motor profile is required")
        for field in ("firmware_version", "hardware_version", "power_flag_baseline"):
            if isinstance(profile[field], bool) or not isinstance(profile[field], int):
                _invalid("Motor identity fields must be integers")
        if not 0 < profile["firmware_version"] < 65535 or not 0 <= profile["hardware_version"] < 65535 or profile["power_flag_baseline"] not in (0, 128):
            _invalid("Unsupported motor identity")
        config = profile["config"]
        expected = {"microstep", "step_angle", "pulses_per_rev", "response_mode", "stall_protect",
                    "position_window_deg", "raw_hex", "options_hex"}
        if not isinstance(config, dict) or set(config) != expected:
            _invalid("A complete drive configuration is required")
        for field in ("microstep", "pulses_per_rev", "response_mode", "stall_protect"):
            if isinstance(config[field], bool) or not isinstance(config[field], int):
                _invalid("Drive configuration integers are required")
        if not 1 <= config["microstep"] <= 256 or config["step_angle"] not in (0.9, 1.8) or config["response_mode"] != 1 or config["stall_protect"] not in (0, 1):
            _invalid("Unsupported drive configuration")
        if config["pulses_per_rev"] != config["microstep"] * (200 if config["step_angle"] == 1.8 else 400):
            _invalid("Drive pulse scale is inconsistent")
        window = config["position_window_deg"]
        if isinstance(window, bool) or not isinstance(window, (int, float)) or not math.isfinite(window) or not 0 <= window <= config["step_angle"]:
            _invalid("Unsupported drive arrival tolerance")
        for field, length in (("raw_hex", 33), ("options_hex", 5)):
            try:
                raw = bytes.fromhex(config[field])
            except (TypeError, ValueError):
                _invalid("Drive configuration bytes are invalid")
            if len(raw) != length or raw[0] != motion["axes"][axis]["address"] or raw[-1] != 0x6B:
                _invalid("Drive configuration bytes differ from their axis")


def validate_limit_record(record):
    expected = {"version", "created_unix_s", "motion", "profiles", "connection_token", "stop_generation",
                "boundaries", "clearance_confirmed"}
    if not isinstance(record, dict) or set(record) != expected or record["version"] != RECORD_VERSION:
        _invalid("A supported complete absolute limit record is required")
    timestamp = record["created_unix_s"]
    if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)) or not math.isfinite(timestamp) or timestamp <= 0:
        _invalid("A valid capture timestamp is required")
    if record["clearance_confirmed"] is not True:
        _invalid("Operating clearance confirmation is required")
    _binding(record)
    bounds = record["boundaries"]
    _counts(bounds, BOUNDARIES)
    if bounds["x_left"] <= bounds["x_right"] or bounds["z_down"] <= bounds["z_up"]:
        _invalid("Boundary order must follow confirmed X-left/Z-down raw-count increase")
    for axis, low, high in (("X", "x_right", "x_left"), ("Z", "z_up", "z_down")):
        config = record["profiles"][axis]["config"]
        margin = max(65536 / config["pulses_per_rev"] / 2, 1,
                     65536 * config["position_window_deg"] / 360)
        if bounds[high] - bounds[low] <= margin * 2:
            _invalid(f"{axis} boundary span must exceed the arrival tolerance on both sides")
    return copy.deepcopy(record)


def binding_matches(record, snapshot, *, generation=False):
    if not isinstance(snapshot, dict) or not snapshot.get("connected") or snapshot.get("faulted"):
        return False
    profiles = {axis: {field: copy.deepcopy(state.get(field)) for field in
                       ("firmware_version", "hardware_version", "config", "power_flag_baseline")}
                for axis, state in snapshot.get("axes", {}).items()}
    motion = {"port": snapshot.get("port"), "baud": snapshot.get("baud"),
              "axes": {axis: state.get("mechanical") for axis, state in snapshot.get("axes", {}).items()}}
    return (record["connection_token"] == snapshot.get("connection_token")
            and record["motion"] == motion and record["profiles"] == profiles
            and (not generation or record["stop_generation"] == snapshot.get("stop_generation")))


class AbsoluteMotionLimits:
    def __init__(self, state_dir):
        self.path = Path(state_dir) / "absolute-motion-limits.json"
        self._record = None
        self._draft = None
        self._active = False
        self._error = None
        try:
            if self.path.exists():
                self._record = validate_limit_record(json.loads(self.path.read_text(encoding="utf-8")))
        except Exception as error:
            self._error = {"code": "absolute_limits_corrupt", "message": str(error)}

    def snapshot(self, motion=None):
        current = None if motion is None else motion.snapshot()
        active = (self._active and self._record is not None and current is not None
                  and binding_matches(self._record, current)
                  and current.get("absolute_limits") == self._record)
        state = ("calibrating" if self._draft is not None else "error" if self._error else
                 "active" if active else "stale" if self._record is not None else "missing")
        return copy.deepcopy({"state": state, "active": bool(active), "calibrating": self._draft is not None,
                              "record": self._record, "draft": self._draft, "error": self._error})

    def begin(self, motion):
        self._active = False
        self._draft = None
        motion.install_absolute_limits(None)
        point = motion.capture_limit_position()
        self._draft = {"version": "absolute-motion-limits-draft-1",
                       **{field: copy.deepcopy(point[field]) for field in
                          ("motion", "profiles", "connection_token", "stop_generation")}, "captures": {}}
        self._error = None
        return self.snapshot(motion)

    def _session(self, motion):
        if self._draft is None:
            raise MotionError("absolute_limits_calibration", "Begin four-boundary capture first")
        if not binding_matches(self._draft, motion.snapshot(), generation=True):
            raise MotionError("absolute_limits_stale", "Connection, motor profile or STOP changed during capture")

    def capture(self, motion, boundary):
        if not isinstance(boundary, str) or boundary not in BOUNDARIES:
            raise MotionError("invalid_boundary", "Choose x_left, x_right, z_up or z_down")
        self._session(motion)
        point = motion.capture_limit_position()
        self._session(motion)
        if any(point[field] != self._draft[field] for field in ("motion", "profiles", "connection_token", "stop_generation")):
            raise MotionError("absolute_limits_stale", "Capture identity changed")
        self._draft["captures"][boundary] = {"raw_counts": copy.deepcopy(point["raw_counts"]),
                                                "captured_unix_s": time.time()}
        return self.snapshot(motion)

    def save(self, motion, clearance_confirmed=False):
        self._session(motion)
        if set(self._draft["captures"]) != set(BOUNDARIES):
            raise MotionError("absolute_limits_incomplete", "All four boundaries must be captured")
        if clearance_confirmed is not True:
            raise MotionError("absolute_limits_clearance", "Confirm safe operating clearance before saving")
        point = motion.capture_limit_position()
        self._session(motion)
        record = validate_limit_record({"version": RECORD_VERSION, "created_unix_s": time.time(),
                 **{field: copy.deepcopy(self._draft[field]) for field in
                    ("motion", "profiles", "connection_token", "stop_generation")},
                 "boundaries": {boundary: self._draft["captures"][boundary]["raw_counts"][axis]
                                for boundary, axis in BOUNDARIES.items()}, "clearance_confirmed": True})
        for axis, low, high in (("X", "x_right", "x_left"), ("Z", "z_up", "z_down")):
            if not record["boundaries"][low] <= point["raw_counts"][axis] <= record["boundaries"][high]:
                raise MotionError("absolute_limit_violation", f"{axis} current position is outside captured boundaries")
        temporary = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(json.dumps(record, indent=2, allow_nan=False), encoding="utf-8")
            self._session(motion)
            temporary.replace(self.path)
            self._record = record
            motion.install_absolute_limits(record)
            self._session(motion)
        except Exception:
            motion.install_absolute_limits(None)
            temporary.unlink(missing_ok=True)
            raise
        self._record = record
        self._draft = None
        self._active = True
        self._error = None
        return self.snapshot(motion)

    def cancel(self, motion):
        self._draft = None
        self._active = False
        if motion is not None:
            motion.install_absolute_limits(None)
        return self.snapshot(motion)

    def require(self, motion):
        if not self.snapshot(motion)["active"]:
            raise MotionError("absolute_limits_required", "Capture and save four operating boundaries for this connection")
        return copy.deepcopy(self._record)

    def preflight(self, motion, steps):
        self.require(motion)
        return motion.preflight_absolute_limits(steps)
