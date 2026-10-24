"""Rigid CAD correspondence derived only from cached encoder feedback."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import time
import uuid
from contextlib import suppress
from pathlib import Path

from .emm_motion import MotionError
from .motion_limits import _binding, binding_matches, validate_limit_record


REGISTRATION_VERSION = "digital-twin-registration-1"
DATUM_VERSION = "digital-twin-datum-1"
DATUM_ANCHORS = {"left_up": {"x_center_mm": -75, "lift_mm": 50},
                 "right_down": {"x_center_mm": 75, "lift_mm": 0}}
DATUM_BOUNDARIES = {"left_up": {"X": "x_left", "Z": "z_up"},
                    "right_down": {"X": "x_right", "Z": "z_down"}}
DATUM_REFERENCE = {"x_offset_mm": 49.5, "lift_mm": 35}
BINDING_FIELDS = {"motion", "profiles", "connection_token", "stop_generation"}
TOLERANCE_MM = 0.1
MAX_FEEDBACK_AGE_S = 2
CAD_FIELDS = {"x_left_mm", "x_right_mm", "z_up_lift_mm", "z_down_lift_mm"}


def _error(code, message):
    from .automation import AutomationError

    raise AutomationError(code, message)


def _finite(value):
    try:
        return (not isinstance(value, bool) and isinstance(value, (int, float))
                and math.isfinite(value))
    except OverflowError:
        return False


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def _cad_bounds(value):
    if (not isinstance(value, dict) or set(value) != CAD_FIELDS
            or not all(_finite(number) for number in value.values())):
        _error("invalid_twin_bounds", "Provide exactly four finite CAD boundary coordinates")
    if value["x_left_mm"] >= value["x_right_mm"] or value["z_up_lift_mm"] <= value["z_down_lift_mm"]:
        _error("invalid_twin_bounds", "CAD boundaries must follow X left/right and Z up/down order")
    return copy.deepcopy(value)


def _spans(record, bounds):
    raw = record["boundaries"]
    axes = record["motion"]["axes"]
    physical = {"X": (raw["x_left"] - raw["x_right"]) * axes["X"]["lead_mm"] / 65536,
                "Z": (raw["z_down"] - raw["z_up"]) * axes["Z"]["lead_mm"] / 65536}
    cad = {"X": bounds["x_right_mm"] - bounds["x_left_mm"],
           "Z": bounds["z_up_lift_mm"] - bounds["z_down_lift_mm"]}
    residuals = {axis: cad[axis] - physical[axis] for axis in ("X", "Z")}
    if any(not math.isfinite(value) for values in (physical, cad, residuals) for value in values.values()):
        _error("invalid_twin_bounds", "CAD and encoder spans must be finite")
    if any(abs(value) > TOLERANCE_MM + 1e-9 for value in residuals.values()):
        _error("twin_span_mismatch", "CAD and encoder spans differ by more than the 0.1 mm display registration tolerance")
    return physical, cad, residuals


def _registration(value):
    if isinstance(value, dict) and value.get("version") == DATUM_VERSION:
        return _datum_registration(value)
    expected = {"version", "created_unix_s", "geometry_sha256", "limits_sha256", "limits_record",
                "cad_bounds", "tolerance_mm", "physical_span_mm", "cad_span_mm", "residuals_mm"}
    if (not isinstance(value, dict) or set(value) != expected
            or value["version"] != REGISTRATION_VERSION
            or not _finite(value["created_unix_s"]) or value["created_unix_s"] <= 0
            or not _finite(value["tolerance_mm"]) or value["tolerance_mm"] != TOLERANCE_MM):
        _error("invalid_twin_registration", "A complete supported twin registration is required")
    for field in ("geometry_sha256", "limits_sha256"):
        digest = value[field]
        if (not isinstance(digest, str) or len(digest) != 64
                or any(character not in "0123456789abcdef" for character in digest)):
            _error("invalid_twin_registration", "Registration fingerprints must be SHA256 hex strings")
    record = validate_limit_record(value["limits_record"])
    if _hash(record) != value["limits_sha256"]:
        _error("invalid_twin_registration", "The saved operating limit fingerprint differs from its record")
    bounds = _cad_bounds(value["cad_bounds"])
    calculated = _spans(record, bounds)
    for field, values in zip(("physical_span_mm", "cad_span_mm", "residuals_mm"), calculated):
        actual = value[field]
        if (not isinstance(actual, dict) or set(actual) != {"X", "Z"}
                or not all(_finite(number) for number in actual.values()) or actual != values):
            _error("invalid_twin_registration", "Saved registration spans or residuals are inconsistent")
    return copy.deepcopy(value)


def _raw_counts(value):
    if (not isinstance(value, dict) or set(value) != {"X", "Z"}
            or any(isinstance(number, bool) or not isinstance(number, int) or abs(number) > 0xFFFFFFFF
                   for number in value.values())):
        _error("invalid_twin_registration", "Both datum raw counts must be bounded integers")


def _nominal_binding(binding):
    for axis, travel in (("X", 150), ("Z", 50)):
        config = binding["motion"]["axes"][axis]
        if config["travel_mm"] != travel or config["positive_direction"] != 0:
            _error("twin_datum_incompatible", "Nominal CAD endpoints require X150/Z50 travel and confirmed direction 0")


def _datum_registration(value):
    expected = {"version", "mode", "datum", "created_unix_s", "geometry_sha256", "binding", "anchor_counts",
                "cad_anchor", "basis", "display_only", "accuracy_unverified"}
    if (not isinstance(value, dict) or set(value) != expected or value["version"] != DATUM_VERSION
            or not isinstance(value["datum"], str) or value["datum"] not in DATUM_ANCHORS
            or value["mode"] != f"{value['datum']}_datum" or value["basis"] != "nominal_module_endpoints"
            or value["display_only"] is not True or value["accuracy_unverified"] is not True
            or not _finite(value["created_unix_s"]) or value["created_unix_s"] <= 0):
        _error("invalid_twin_registration", "A complete supported display datum is required")
    digest = value["geometry_sha256"]
    if (not isinstance(digest, str) or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)):
        _error("invalid_twin_registration", "The CAD fingerprint must be a SHA256 hex string")
    binding = value["binding"]
    if not isinstance(binding, dict) or set(binding) != BINDING_FIELDS:
        _error("invalid_twin_registration", "An exact complete datum binding is required")
    _binding(binding)
    _nominal_binding(binding)
    _raw_counts(value["anchor_counts"])
    anchor = value["cad_anchor"]
    expected_anchor = DATUM_ANCHORS[value["datum"]]
    if (not isinstance(anchor, dict) or set(anchor) != set(expected_anchor)
            or not all(_finite(number) for number in anchor.values()) or anchor != expected_anchor):
        _error("invalid_twin_registration", "The selected nominal corner CAD anchor is required")
    return copy.deepcopy(value)


def _datum_source(limits, datum):
    if not isinstance(limits, dict):
        _error("twin_datum_required", "Read both selected corner boundaries for this connection before registering the display datum")
    boundaries = DATUM_BOUNDARIES[datum]
    draft = limits.get("draft")
    if draft is not None:
        expected = {"version", "captures", *BINDING_FIELDS}
        if (not isinstance(draft, dict) or set(draft) != expected
                or draft["version"] != "absolute-motion-limits-draft-1"):
            _error("twin_datum_required", "A supported complete boundary capture draft is required")
        binding = {field: copy.deepcopy(draft[field]) for field in BINDING_FIELDS}
        _binding(binding)
        captures = draft["captures"]
        if (not isinstance(captures, dict) or not set(boundaries.values()).issubset(captures)
                or not set(captures).issubset({"x_left", "x_right", "z_up", "z_down"})):
            _error("twin_datum_required", "Read both selected corner boundaries before registering the display datum")
        for capture in captures.values():
            if (not isinstance(capture, dict) or set(capture) != {"raw_counts", "captured_unix_s"}
                    or not _finite(capture["captured_unix_s"]) or capture["captured_unix_s"] <= 0):
                _error("twin_datum_required", "Complete timestamped boundary captures are required")
            _raw_counts(capture["raw_counts"])
        counts = {axis: captures[boundary]["raw_counts"][axis] for axis, boundary in boundaries.items()}
        return binding, counts
    if limits.get("active") is not True or limits.get("state") != "active":
        _error("twin_datum_required", "Read the selected corner boundaries or activate a verified saved boundary record")
    record = validate_limit_record(limits.get("record"))
    return ({field: copy.deepcopy(record[field]) for field in BINDING_FIELDS},
            {axis: record["boundaries"][boundary] for axis, boundary in boundaries.items()})


class DigitalTwin:
    def __init__(self, state_dir, geometry_path):
        self.path = Path(state_dir) / "digital-twin-registration.json"
        self.geometry_path = Path(geometry_path)
        self._record = None
        self._error = None
        self._geometry_hash = None
        self._reference_pose = None
        try:
            if self.path.exists():
                self._record = _registration(json.loads(self.path.read_text(encoding="utf-8")))
        except Exception as error:
            self._error = {"code": "twin_registration_corrupt", "message": str(error)}

    def _geometry(self):
        payload = self.geometry_path.read_bytes()
        digest = hashlib.sha256(payload).hexdigest()
        if digest != self._geometry_hash:
            data = json.loads(payload)
            reference = data.get("reference_pose") if isinstance(data, dict) else None
            if (not isinstance(data, dict) or data.get("schema_version") != "1.0" or data.get("units") != "mm"
                    or not isinstance(reference, dict) or set(reference) != {"x_offset_mm", "lift_mm"}
                    or not all(_finite(number) for number in reference.values())):
                _error("twin_geometry_invalid", "A supported millimeter CAD model with a finite reference pose is required")
            self._reference_pose = copy.deepcopy(reference)
            self._geometry_hash = digest
        return digest, self._reference_pose

    def _result(self, state, error=None, pose=None):
        return copy.deepcopy({"state": state, "registered": self._record is not None,
                              "fresh": state == "synced", "pose": pose,
                              "registration": self._record, "error": error})

    def _context(self, motion, limits):
        if not isinstance(motion, dict) or motion.get("connected") is not True:
            return "disconnected", "twin_disconnected", "Motion feedback is disconnected", None
        if motion.get("faulted") is not False:
            return "error", "twin_motion_fault", "The motion connection has a fault", None
        if motion.get("feedback_error"):
            return "error", "twin_feedback_error", "The latest motion feedback read failed", None
        if not isinstance(limits, dict) or limits.get("active") is not True or limits.get("state") != "active":
            return "stale", "twin_limits_required", "Verified active four-boundary limits are required", None
        try:
            record = validate_limit_record(limits.get("record"))
            profiles = {axis: {field: cached.get(field) for field in record["profiles"][axis]}
                        for axis, cached in motion.get("axes", {}).items()}
            identity = {"port": motion.get("port"), "baud": motion.get("baud"),
                        "axes": {axis: cached.get("mechanical") for axis, cached in motion.get("axes", {}).items()}}
            if (not binding_matches(record, motion) or _hash(profiles) != _hash(record["profiles"])
                    or _hash(identity) != _hash(record["motion"])):
                return "stale", "twin_binding_changed", "Connection or motor profile differs from the captured boundaries", None
            if motion.get("absolute_limits") != record:
                return "stale", "twin_limits_changed", "The current drive limits differ from the captured boundaries", None
        except Exception as error:
            return "error", "twin_limits_invalid", str(error), None
        return None, None, None, record

    def _feedback(self, motion):
        axes = motion.get("axes")
        if not isinstance(axes, dict) or set(axes) != {"X", "Z"}:
            return None
        ages = {}
        now = time.monotonic()
        for axis, state in axes.items():
            if not isinstance(state, dict):
                return None
            age = state.get("feedback_age_s")
            if "feedback_age_s" not in state:
                updated = state.get("updated_monotonic")
                age = now - updated if _finite(updated) else None
            raw = state.get("raw_count")
            if (state.get("telemetry_valid") is not True or not _finite(age) or not 0 <= age <= MAX_FEEDBACK_AGE_S
                    or isinstance(raw, bool) or not isinstance(raw, int) or abs(raw) > 0xFFFFFFFF):
                return None
            ages[axis] = age
        return ages

    def _datum_context(self, motion, binding):
        if not isinstance(motion, dict) or motion.get("connected") is not True:
            return "disconnected", "twin_disconnected", "Motion feedback is disconnected"
        if motion.get("faulted") is not False:
            return "error", "twin_motion_fault", "The motion connection has a fault"
        if motion.get("feedback_error"):
            return "error", "twin_feedback_error", "The latest motion feedback read failed"
        try:
            profiles = {axis: {field: cached.get(field) for field in binding["profiles"][axis]}
                        for axis, cached in motion.get("axes", {}).items()}
            identity = {"port": motion.get("port"), "baud": motion.get("baud"),
                        "axes": {axis: cached.get("mechanical") for axis, cached in motion.get("axes", {}).items()}}
            matches = (binding_matches(binding, motion)
                       and _hash(profiles) == _hash(binding["profiles"])
                       and _hash(identity) == _hash(binding["motion"]))
        except Exception:
            matches = False
        if not matches:
            return "stale", "twin_binding_changed", "Connection or motor profile differs from the display datum"
        return None, None, None

    def snapshot(self, motion, limits):
        if self._error:
            return self._result("error", self._error)
        try:
            digest, reference = self._geometry()
        except Exception as error:
            return self._result("error", {"code": "twin_geometry_invalid", "message": str(error)})
        if self._record is None:
            return self._result("unregistered")
        if digest != self._record["geometry_sha256"]:
            return self._result("stale", {"code": "twin_geometry_changed", "message": "The CAD model changed; confirm its correspondence again"})
        datum = self._record["version"] == DATUM_VERSION
        if datum:
            if reference != DATUM_REFERENCE:
                return self._result("error", {"code": "twin_datum_incompatible", "message": "Nominal CAD endpoints require the X49.5/lift35 reference pose"})
            state, code, message = self._datum_context(motion, self._record["binding"])
        else:
            state, code, message, record = self._context(motion, limits)
        if code:
            return self._result(state, {"code": code, "message": message})
        if not datum and _hash(record) != self._record["limits_sha256"]:
            return self._result("stale", {"code": "twin_limits_changed", "message": "The four-boundary record changed; confirm CAD correspondence again"})
        ages = self._feedback(motion)
        if ages is None:
            return self._result("stale", {"code": "twin_feedback_stale", "message": "Both axes need valid encoder feedback no older than 2 seconds"})
        if datum:
            raw = self._record["anchor_counts"]
            axes = self._record["binding"]["motion"]["axes"]
            anchor = self._record["cad_anchor"]
            x_center = anchor["x_center_mm"] - (motion["axes"]["X"]["raw_count"] - raw["X"]) * axes["X"]["lead_mm"] / 65536
            lift = anchor["lift_mm"] - (motion["axes"]["Z"]["raw_count"] - raw["Z"]) * axes["Z"]["lead_mm"] / 65536
        else:
            raw = record["boundaries"]
            axes = record["motion"]["axes"]
            bounds = self._record["cad_bounds"]
            x_center = bounds["x_right_mm"] - (motion["axes"]["X"]["raw_count"] - raw["x_right"]) * axes["X"]["lead_mm"] / 65536
            lift = bounds["z_up_lift_mm"] - (motion["axes"]["Z"]["raw_count"] - raw["z_up"]) * axes["Z"]["lead_mm"] / 65536
        pose = {"scene_x_mm": x_center - reference["x_offset_mm"],
                "scene_z_mm": lift - reference["lift_mm"], "x_center_mm": x_center,
                "lift_mm": lift, "feedback_age_s": ages, "source": "encoder_feedback"}
        if not all(_finite(pose[field]) for field in ("scene_x_mm", "scene_z_mm", "x_center_mm", "lift_mm")):
            return self._result("error", {"code": "twin_pose_invalid", "message": "The encoder-to-CAD conversion is not finite"})
        return self._result("synced", pose=pose)

    def snapshot_experiment(self, motion, guided):
        from .automation_devices import calibration_status

        record = guided.get("calibration")
        result = {"state": "unregistered", "registered": False, "fresh": False,
                  "pose": None, "registration": None, "error": None}
        if not isinstance(record, dict) or record.get("four_point") is not True:
            return result
        result.update(state="stale", registered=True)
        if calibration_status(guided, motion)["display_valid"] is not True:
            return result
        try:
            digest, reference = self._geometry()
            ages = self._feedback(motion)
            if ages is None:
                return result
            axes = record["motion"]["axes"]
            span = (record["points"]["last"]["raw_counts"]["X"] - record["points"]["first"]["raw_counts"]["X"]) * axes["X"]["lead_mm"] / 65536
            if abs(span - 99) > TOLERANCE_MM:
                _error("twin_column_span", "The physical columns do not match the 99 mm CAD span; no model scaling is applied")
            x_center = 49.5 - (motion["axes"]["X"]["raw_count"] - record["points"]["first"]["raw_counts"]["X"]) * axes["X"]["lead_mm"] / 65536
            lift = -(motion["axes"]["Z"]["raw_count"] - record["points"]["working"]["raw_counts"]["Z"]) * axes["Z"]["lead_mm"] / 65536
            pose = {"scene_x_mm": x_center - reference["x_offset_mm"],
                    "scene_z_mm": lift - reference["lift_mm"], "x_center_mm": x_center,
                    "lift_mm": lift, "feedback_age_s": ages, "source": "encoder_feedback"}
            if not all(_finite(pose[field]) for field in ("scene_x_mm", "scene_z_mm", "x_center_mm", "lift_mm")):
                _error("twin_pose_invalid", "The calibrated encoder-to-CAD conversion is not finite")
            binding = {"connection_token": record["connection_token"], "motion": copy.deepcopy(record["motion"]),
                       "profiles": copy.deepcopy(record["reference"]["profiles"])}
            registration = {"version": "digital-twin-experiment-1", "calibration_id": record["id"],
                            "binding": binding, "geometry_sha256": digest,
                            "basis": "physical_columns_nominal_working_z", "display_only": True,
                            "accuracy_unverified": True}
            result.update(state="synced", fresh=True, pose=pose, registration=registration)
        except Exception as error:
            result.update(state="error", error={"code": getattr(error, "code", "twin_calibration_invalid"), "message": str(error)})
        return result

    def register(self, motion, limits, cad_bounds, confirmed=False):
        if confirmed is not True:
            _error("twin_confirmation_required", "Confirm the four CAD coordinates correspond to the captured real boundaries")
        bounds = _cad_bounds(cad_bounds)
        state, code, message, record = self._context(motion, limits)
        if code:
            _error(code, message)
        if self._feedback(motion) is None:
            _error("twin_feedback_stale", "Both axes need valid encoder feedback no older than 2 seconds")
        try:
            digest, reference = self._geometry()
        except Exception as error:
            _error("twin_geometry_invalid", str(error))
        physical, cad, residuals = _spans(record, bounds)
        registration = _registration({"version": REGISTRATION_VERSION, "created_unix_s": time.time(),
                                     "geometry_sha256": digest, "limits_sha256": _hash(record),
                                     "limits_record": record, "cad_bounds": bounds, "tolerance_mm": TOLERANCE_MM,
                                     "physical_span_mm": physical, "cad_span_mm": cad, "residuals_mm": residuals})
        return self._save(registration, motion, limits)

    def register_datum(self, motion, limits, confirmed=False, datum="left_up"):
        if confirmed is not True:
            _error("twin_confirmation_required", "Confirm the selected corner uses nominal CAD endpoints for display only")
        if not isinstance(datum, str) or datum not in DATUM_ANCHORS:
            _error("invalid_twin_datum", "Select the left/up or right/down display datum")
        try:
            binding, counts = _datum_source(limits, datum)
        except (MotionError, TypeError, ValueError, KeyError, OverflowError) as error:
            _error("twin_datum_required", str(error))
        if limits.get("draft") is None:
            state, code, message, record = self._context(motion, limits)
            if code:
                _error(code, message)
        state, code, message = self._datum_context(motion, binding)
        if code:
            _error(code, message)
        if self._feedback(motion) is None:
            _error("twin_feedback_stale", "Both axes need valid encoder feedback no older than 2 seconds")
        _nominal_binding(binding)
        try:
            digest, reference = self._geometry()
        except Exception as error:
            _error("twin_geometry_invalid", str(error))
        if reference != DATUM_REFERENCE:
            _error("twin_datum_incompatible", "Nominal CAD endpoints require the X49.5/lift35 reference pose")
        registration = _datum_registration({"version": DATUM_VERSION, "mode": f"{datum}_datum", "datum": datum,
                         "created_unix_s": time.time(), "geometry_sha256": digest,
                         "binding": binding, "anchor_counts": counts, "cad_anchor": copy.deepcopy(DATUM_ANCHORS[datum]),
                         "basis": "nominal_module_endpoints", "display_only": True, "accuracy_unverified": True})
        return self._save(registration, motion, limits)

    def _save(self, registration, motion, limits):
        temporary = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(json.dumps(registration, indent=2, ensure_ascii=False, allow_nan=False), encoding="utf-8")
            temporary.replace(self.path)
        except Exception as error:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)
            _error("twin_registration_save_failed", str(error))
        self._record = registration
        self._error = None
        return self.snapshot(motion, limits)
