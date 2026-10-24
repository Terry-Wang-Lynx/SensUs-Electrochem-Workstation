from __future__ import annotations

import copy
import hashlib
import json
import time
from pathlib import Path

import pytest

from pa_host.automation import AutomationError


CAD_BOUNDS = {"x_left_mm": 43.5, "x_right_mm": 55.5,
              "z_up_lift_mm": 40, "z_down_lift_mm": 36}


def cached_inputs():
    mechanical = {
        "X": {"address": 1, "lead_mm": 2, "travel_mm": 150,
              "positive_direction": 0, "max_speed_mm_s": 10},
        "Z": {"address": 2, "lead_mm": 2, "travel_mm": 50,
              "positive_direction": 0, "max_speed_mm_s": 10},
    }
    profiles = {}
    for axis, config in mechanical.items():
        address = config["address"]
        raw = bytes([address, 0x42]) + bytes(30) + b"\x6b"
        profiles[axis] = {"firmware_version": 107, "hardware_version": 769,
                          "power_flag_baseline": 0,
                          "config": {"microstep": 16, "step_angle": 1.8,
                                     "pulses_per_rev": 3200, "response_mode": 1,
                                     "stall_protect": 1, "position_window_deg": 0.8,
                                     "raw_hex": raw.hex(),
                                     "options_hex": bytes([address, 0x1A, 6, 0, 0x6B]).hex()}}
    record = {"version": "absolute-motion-limits-1", "created_unix_s": 1792713600,
              "motion": {"port": "cached-only", "baud": 115200, "axes": mechanical},
              "profiles": profiles, "connection_token": "55a508c4-2b3e-4217-a2d8-e77712370a28",
              "stop_generation": 0, "clearance_confirmed": True,
              "boundaries": {"x_left": 393216, "x_right": 0,
                             "z_up": -65536, "z_down": 65536}}
    motion = {"connected": True, "port": "cached-only", "baud": 115200,
              "connection_token": record["connection_token"], "stop_generation": 0,
              "faulted": False, "feedback_error": None, "absolute_limits": copy.deepcopy(record),
              "axes": {axis: {"mechanical": copy.deepcopy(mechanical[axis]),
                              **copy.deepcopy(profiles[axis]), "address": mechanical[axis]["address"],
                              "raw_count": 0, "feedback_age_s": 0.25, "telemetry_valid": True,
                              "enabled": True, "referenced": False, "speed_rpm": 0,
                              "status": 3, "position_mm": None}
                       for axis in ("X", "Z")}}
    limits = {"state": "active", "active": True, "record": copy.deepcopy(record), "error": None}
    return motion, limits


@pytest.fixture
def twin_inputs(tmp_path):
    from pa_host.digital_twin import DigitalTwin

    geometry = tmp_path / "apparatus.json"
    geometry.write_text(json.dumps({"schema_version": "1.0", "units": "mm", "parts": [],
                                    "reference_pose": {"x_offset_mm": 49.5, "lift_mm": 35}}))
    motion, limits = cached_inputs()
    return DigitalTwin(tmp_path / "state", geometry), motion, limits, geometry


def test_missing_registration_does_not_invent_cad_correspondence(twin_inputs):
    twin, motion, limits, geometry = twin_inputs
    assert twin.snapshot(motion, limits) == {"state": "unregistered", "registered": False,
                                            "fresh": False, "pose": None,
                                            "registration": None, "error": None}
    assert not twin.path.exists()


def test_registered_pose_uses_zero_raw_feedback_and_cad_anchors(twin_inputs):
    twin, motion, limits, geometry = twin_inputs
    before = copy.deepcopy((motion, limits))
    result = twin.register(motion, limits, CAD_BOUNDS, confirmed=True)
    assert result["state"] == "synced" and result["registered"] and result["fresh"]
    assert result["pose"] == {"scene_x_mm": 6, "scene_z_mm": 3,
                              "x_center_mm": 55.5, "lift_mm": 38,
                              "feedback_age_s": {"X": 0.25, "Z": 0.25},
                              "source": "encoder_feedback"}
    assert result["registration"]["geometry_sha256"] == hashlib.sha256(geometry.read_bytes()).hexdigest()
    assert result["registration"]["tolerance_mm"] == 0.1
    assert result["registration"]["residuals_mm"] == {"X": 0, "Z": 0}
    assert (motion, limits) == before


@pytest.mark.parametrize("counts,expected", [
    ((65536, 32768), (4, 2, 53.5, 37)),
    ((-65536, -98304), (8, 6, 57.5, 41)),
])
def test_hand_turn_changes_rigid_pose_with_encoder_signs(twin_inputs, counts, expected):
    twin, motion, limits, geometry = twin_inputs
    twin.register(motion, limits, CAD_BOUNDS, confirmed=True)
    motion["axes"]["X"]["raw_count"], motion["axes"]["Z"]["raw_count"] = counts
    motion["position"] = {"X": 999, "Z": 999}
    result = twin.snapshot(motion, limits)
    assert result["state"] == "synced"
    assert [result["pose"][field] for field in ("scene_x_mm", "scene_z_mm", "x_center_mm", "lift_mm")] == list(expected)


def test_stop_generation_change_retains_registration(twin_inputs):
    twin, motion, limits, geometry = twin_inputs
    twin.register(motion, limits, CAD_BOUNDS, confirmed=True)
    motion["stop_generation"] = 8
    assert twin.snapshot(motion, limits)["state"] == "synced"


@pytest.mark.parametrize("axis,field,value", [
    ("X", "feedback_age_s", 2.001), ("Z", "feedback_age_s", 2.001),
    ("X", "feedback_age_s", None), ("Z", "feedback_age_s", -1),
    ("Z", "feedback_age_s", float("nan")), ("X", "feedback_age_s", True),
    ("X", "telemetry_valid", False), ("Z", "raw_count", True),
    ("Z", "raw_count", None), ("X", "raw_count", 2**32),
])
def test_both_axes_require_valid_fresh_feedback(twin_inputs, axis, field, value):
    twin, motion, limits, geometry = twin_inputs
    twin.register(motion, limits, CAD_BOUNDS, confirmed=True)
    motion["axes"][axis][field] = value
    result = twin.snapshot(motion, limits)
    assert result["state"] == "stale" and result["registered"]
    assert not result["fresh"] and result["pose"] is None


def test_cache_monotonic_timestamp_supplies_age_without_driver_calls(twin_inputs, monkeypatch):
    twin, motion, limits, geometry = twin_inputs
    monkeypatch.setattr(time, "monotonic", lambda: 100)
    for state in motion["axes"].values():
        state.pop("feedback_age_s")
        state["updated_monotonic"] = 99.5
    result = twin.register(motion, limits, CAD_BOUNDS, confirmed=True)
    assert result["pose"]["feedback_age_s"] == {"X": 0.5, "Z": 0.5}
    motion["axes"]["Z"]["updated_monotonic"] = 97
    assert twin.snapshot(motion, limits)["pose"] is None


@pytest.mark.parametrize("damage,state,code", [
    ("disconnect", "disconnected", "twin_disconnected"),
    ("fault", "error", "twin_motion_fault"),
    ("feedback", "error", "twin_feedback_error"),
    ("reconnect", "stale", "twin_binding_changed"),
    ("profile", "stale", "twin_binding_changed"),
    ("mechanics", "stale", "twin_binding_changed"),
    ("limits", "stale", "twin_limits_required"),
    ("record", "stale", "twin_limits_changed"),
    ("installed", "stale", "twin_limits_changed"),
])
def test_identity_and_motion_failures_hide_pose(twin_inputs, damage, state, code):
    twin, motion, limits, geometry = twin_inputs
    twin.register(motion, limits, CAD_BOUNDS, confirmed=True)
    if damage == "disconnect":
        motion["connected"] = False
    elif damage == "fault":
        motion["faulted"] = True
    elif damage == "feedback":
        motion["feedback_error"] = {"code": "read_failed", "message": "bad telemetry"}
    elif damage == "reconnect":
        motion["connection_token"] = "f6546a4f-0818-48fc-ae5f-50e7f6f13ae4"
    elif damage == "profile":
        motion["axes"]["Z"]["config"]["microstep"] = 32
    elif damage == "mechanics":
        motion["axes"]["X"]["mechanical"]["lead_mm"] = 4
    elif damage == "limits":
        limits["active"] = False
    elif damage == "record":
        limits["record"]["created_unix_s"] += 1
    else:
        motion["absolute_limits"] = None
    result = twin.snapshot(motion, limits)
    assert result["state"] == state and result["registered"]
    assert result["pose"] is None and not result["fresh"]
    assert result["error"]["code"] == code


@pytest.mark.parametrize("bounds", [
    None, {}, {**CAD_BOUNDS, "raw_counts": {"X": 0, "Z": 0}},
    {**CAD_BOUNDS, "x_left_mm": True}, {**CAD_BOUNDS, "x_left_mm": float("nan")},
    {**CAD_BOUNDS, "x_left_mm": 10**1000},
    {**CAD_BOUNDS, "z_up_lift_mm": float("inf")},
    {**CAD_BOUNDS, "x_left_mm": 56}, {**CAD_BOUNDS, "z_down_lift_mm": 41},
])
def test_registration_rejects_missing_invalid_or_injected_cad_values(twin_inputs, bounds):
    twin, motion, limits, geometry = twin_inputs
    with pytest.raises(AutomationError) as caught:
        twin.register(motion, limits, bounds, confirmed=True)
    assert caught.value.code == "invalid_twin_bounds"
    assert not twin.path.exists()


def test_registration_needs_explicit_confirmation_and_active_real_limits(twin_inputs):
    twin, motion, limits, geometry = twin_inputs
    with pytest.raises(AutomationError) as caught:
        twin.register(motion, limits, CAD_BOUNDS)
    assert caught.value.code == "twin_confirmation_required"
    limits["active"] = False
    with pytest.raises(AutomationError) as caught:
        twin.register(motion, limits, CAD_BOUNDS, confirmed=True)
    assert caught.value.code == "twin_limits_required"
    assert not twin.path.exists()


def test_span_mismatch_is_rejected_instead_of_rescaling_cad(twin_inputs):
    twin, motion, limits, geometry = twin_inputs
    bounds = {**CAD_BOUNDS, "x_left_mm": -94.5}
    with pytest.raises(AutomationError) as caught:
        twin.register(motion, limits, bounds, confirmed=True)
    assert caught.value.code == "twin_span_mismatch"
    assert not twin.path.exists()


def test_small_cad_residual_is_recorded_without_changing_millimeter_scale(twin_inputs):
    twin, motion, limits, geometry = twin_inputs
    bounds = {**CAD_BOUNDS, "x_left_mm": 43.55, "z_down_lift_mm": 36.02}
    result = twin.register(motion, limits, bounds, confirmed=True)
    assert result["registration"]["residuals_mm"] == pytest.approx({"X": -0.05, "Z": -0.02})
    motion["axes"]["X"]["raw_count"] = 65536
    assert twin.snapshot(motion, limits)["pose"]["scene_x_mm"] == 4


def test_saved_registration_reloads_only_with_same_limits_geometry_and_binding(twin_inputs):
    from pa_host.digital_twin import DigitalTwin

    twin, motion, limits, geometry = twin_inputs
    twin.register(motion, limits, CAD_BOUNDS, confirmed=True)
    reloaded = DigitalTwin(twin.path.parent, geometry)
    assert reloaded.snapshot(motion, limits)["state"] == "synced"
    geometry.write_text(geometry.read_text() + " ")
    result = reloaded.snapshot(motion, limits)
    assert result["state"] == "stale" and result["pose"] is None
    assert result["error"]["code"] == "twin_geometry_changed"


@pytest.mark.parametrize("damage", ["syntax", "unknown", "boolean", "nan", "hash", "limits_hash", "residual"])
def test_corrupt_saved_registration_is_reported_without_constructor_failure(twin_inputs, damage):
    from pa_host.digital_twin import DigitalTwin

    twin, motion, limits, geometry = twin_inputs
    twin.register(motion, limits, CAD_BOUNDS, confirmed=True)
    record = json.loads(twin.path.read_text())
    if damage == "syntax":
        twin.path.write_text("{broken")
    else:
        if damage == "unknown":
            record["unexpected"] = True
        elif damage == "boolean":
            record["tolerance_mm"] = True
        elif damage == "nan":
            record["cad_bounds"]["x_left_mm"] = float("nan")
        elif damage == "hash":
            record["geometry_sha256"] = "not-a-hash"
        elif damage == "limits_hash":
            record["limits_sha256"] = "0" * 64
        else:
            record["residuals_mm"]["X"] = 0.09
        twin.path.write_text(json.dumps(record))
    result = DigitalTwin(twin.path.parent, geometry).snapshot(motion, limits)
    assert result["state"] == "error" and not result["registered"] and result["pose"] is None
    assert result["error"]["code"] == "twin_registration_corrupt"


def test_snapshot_records_are_detached_from_storage(twin_inputs):
    twin, motion, limits, geometry = twin_inputs
    result = twin.register(motion, limits, CAD_BOUNDS, confirmed=True)
    result["registration"]["cad_bounds"]["x_right_mm"] = 900
    result["pose"]["scene_x_mm"] = 900
    assert twin.snapshot(motion, limits)["pose"]["scene_x_mm"] == 6


def test_actual_geometry_reference_is_used_without_manufactured_motion_endpoints(tmp_path):
    from pa_host.digital_twin import DigitalTwin

    geometry = Path(__file__).parents[1] / "pa_host/gui/ht-apparatus.json"
    twin = DigitalTwin(tmp_path, geometry)
    motion, limits = cached_inputs()
    result = twin.register(motion, limits, CAD_BOUNDS, confirmed=True)
    assert result["pose"]["scene_x_mm"] == 6
    assert result["pose"]["scene_z_mm"] == 3
    assert result["registration"]["physical_span_mm"] == {"X": 12, "Z": 4}


def test_failed_registration_write_keeps_previous_saved_correspondence(twin_inputs, monkeypatch):
    twin, motion, limits, geometry = twin_inputs
    original = twin.register(motion, limits, CAD_BOUNDS, confirmed=True)
    saved = twin.path.read_bytes()
    replace = Path.replace

    def fail_registration_replace(source, target):
        if target == twin.path:
            raise OSError("storage unavailable")
        return replace(source, target)

    monkeypatch.setattr(Path, "replace", fail_registration_replace)
    bounds = {key: value + 1 for key, value in CAD_BOUNDS.items()}
    with pytest.raises(AutomationError) as caught:
        twin.register(motion, limits, bounds, confirmed=True)
    assert caught.value.code == "twin_registration_save_failed"
    assert twin.path.read_bytes() == saved
    assert twin.snapshot(motion, limits) == original
    assert not list(twin.path.parent.glob("*.tmp"))


def test_state_directory_failure_is_a_structured_error(twin_inputs):
    twin, motion, limits, geometry = twin_inputs
    twin.path.parent.write_text("not a directory")
    with pytest.raises(AutomationError) as caught:
        twin.register(motion, limits, CAD_BOUNDS, confirmed=True)
    assert caught.value.code == "twin_registration_save_failed"


@pytest.mark.parametrize("damage", ["missing", "syntax", "units", "reference", "boolean"])
def test_invalid_geometry_never_produces_a_pose_or_constructor_crash(twin_inputs, damage):
    twin, motion, limits, geometry = twin_inputs
    twin.register(motion, limits, CAD_BOUNDS, confirmed=True)
    if damage == "missing":
        geometry.unlink()
    elif damage == "syntax":
        geometry.write_text("{")
    else:
        data = json.loads(geometry.read_text())
        if damage == "units":
            data["units"] = "inch"
        elif damage == "reference":
            data.pop("reference_pose")
        else:
            data["reference_pose"]["lift_mm"] = True
        geometry.write_text(json.dumps(data))
    result = twin.snapshot(motion, limits)
    assert result["state"] == "error" and result["pose"] is None
    assert result["error"]["code"] == "twin_geometry_invalid"


def test_feedback_at_exact_two_second_age_is_still_fresh(twin_inputs):
    twin, motion, limits, geometry = twin_inputs
    for state in motion["axes"].values():
        state["feedback_age_s"] = 2
    assert twin.register(motion, limits, CAD_BOUNDS, confirmed=True)["fresh"]


def test_pose_arithmetic_overflow_is_unknown_and_json_serializable(twin_inputs):
    twin, motion, limits, geometry = twin_inputs
    for record in (limits["record"], motion["absolute_limits"]):
        record["motion"]["axes"]["X"]["lead_mm"] = 1e304
        record["boundaries"]["x_left"] = 1000
    motion["axes"]["X"]["mechanical"]["lead_mm"] = 1e304
    bounds = {**CAD_BOUNDS, "x_left_mm": -1.52587890625e302, "x_right_mm": 0}
    twin.register(motion, limits, bounds, confirmed=True)
    motion["axes"]["X"]["raw_count"] = 0xFFFFFFFF
    result = twin.snapshot(motion, limits)
    assert result["pose"] is None
    json.dumps(result, allow_nan=False)


def test_current_profile_guard_distinguishes_boolean_from_numeric_identity(twin_inputs):
    twin, motion, limits, geometry = twin_inputs
    twin.register(motion, limits, CAD_BOUNDS, confirmed=True)
    motion["axes"]["X"]["config"]["response_mode"] = True
    result = twin.snapshot(motion, limits)
    assert result["pose"] is None
    assert result["error"]["code"] == "twin_binding_changed"


@pytest.fixture
def datum_inputs(twin_inputs):
    twin, motion, limits, geometry = twin_inputs
    record = limits["record"]
    draft = {"version": "absolute-motion-limits-draft-1",
             **{field: copy.deepcopy(record[field]) for field in
                ("motion", "profiles", "connection_token", "stop_generation")},
             "captures": {
                 "x_left": {"raw_counts": {"X": 7, "Z": -30}, "captured_unix_s": 1792713600},
                 "z_up": {"raw_counts": {"X": 12, "Z": 0}, "captured_unix_s": 1792713601}}}
    limits.update(state="calibrating", active=False, record=None, draft=draft, calibrating=True)
    motion["absolute_limits"] = None
    motion["axes"]["X"]["raw_count"] = 7
    return twin, motion, limits, geometry


def test_datum_uses_trusted_left_up_captures_without_activating_motion_limits(datum_inputs):
    twin, motion, limits, geometry = datum_inputs
    before = copy.deepcopy((motion, limits))
    result = twin.register_datum(motion, limits, confirmed=True)
    assert result["state"] == "synced"
    assert result["pose"] == {"scene_x_mm": -124.5, "scene_z_mm": 15,
                              "x_center_mm": -75, "lift_mm": 50,
                              "feedback_age_s": {"X": 0.25, "Z": 0.25},
                              "source": "encoder_feedback"}
    registration = result["registration"]
    assert registration["version"] == "digital-twin-datum-1"
    assert registration["mode"] == "left_up_datum"
    assert registration["datum"] == "left_up"
    assert registration["basis"] == "nominal_module_endpoints"
    assert registration["display_only"] is True
    assert registration["accuracy_unverified"] is True
    assert registration["anchor_counts"] == {"X": 7, "Z": 0}
    assert registration["cad_anchor"] == {"x_center_mm": -75, "lift_mm": 50}
    assert registration["binding"] == {field: before[1]["draft"][field] for field in
                                       ("motion", "profiles", "connection_token", "stop_generation")}
    assert registration["geometry_sha256"] == hashlib.sha256(geometry.read_bytes()).hexdigest()
    assert json.loads(twin.path.read_text()) == registration
    assert (motion, limits) == before


def test_datum_hand_turn_and_stop_keep_rigid_millimeter_mapping(datum_inputs):
    twin, motion, limits, geometry = datum_inputs
    twin.register_datum(motion, limits, confirmed=True)
    motion["axes"]["X"]["raw_count"] = 7 - 65536
    motion["axes"]["Z"]["raw_count"] = 32768
    motion["stop_generation"] += 1
    motion["position"] = {"X": 999, "Z": 999}
    result = twin.snapshot(motion, limits)
    assert result["state"] == "synced"
    assert [result["pose"][field] for field in ("scene_x_mm", "scene_z_mm", "x_center_mm", "lift_mm")] == [-122.5, 14, -73, 49]


@pytest.mark.parametrize("damage", ["missing", "changed", "cleared", "invalid"])
def test_registered_datum_is_independent_of_later_operating_limits(datum_inputs, damage):
    twin, motion, limits, geometry = datum_inputs
    original = twin.register_datum(motion, limits, confirmed=True)
    if damage == "missing":
        limits = None
    elif damage == "changed":
        _, limits = cached_inputs()
        limits["record"]["boundaries"]["x_left"] += 1000
        motion["absolute_limits"] = copy.deepcopy(limits["record"])
    elif damage == "cleared":
        limits.clear()
    else:
        limits["draft"] = {"untrusted": True}
        motion["absolute_limits"] = {"untrusted": True}
    assert twin.snapshot(motion, limits) == original


def test_datum_accepts_verified_active_saved_limits_as_capture_source(twin_inputs):
    twin, motion, limits, geometry = twin_inputs
    result = twin.register_datum(motion, limits, confirmed=True)
    assert result["registration"]["anchor_counts"] == {"X": 393216, "Z": -65536}
    assert result["pose"]["x_center_mm"] == -63
    assert result["pose"]["lift_mm"] == 48


def test_datum_requires_explicit_confirmation(datum_inputs):
    twin, motion, limits, geometry = datum_inputs
    with pytest.raises(AutomationError) as caught:
        twin.register_datum(motion, limits)
    assert caught.value.code == "twin_confirmation_required"
    assert not twin.path.exists()


@pytest.mark.parametrize("damage,state,code", [
    ("disconnect", "disconnected", "twin_disconnected"),
    ("fault", "error", "twin_motion_fault"),
    ("feedback", "error", "twin_feedback_error"),
    ("reconnect", "stale", "twin_binding_changed"),
    ("profile", "stale", "twin_binding_changed"),
    ("mechanics", "stale", "twin_binding_changed"),
    ("boolean_profile", "stale", "twin_binding_changed"),
    ("stale", "stale", "twin_feedback_stale"),
    ("unknown", "stale", "twin_feedback_stale"),
])
def test_datum_identity_and_feedback_failures_hide_pose(datum_inputs, damage, state, code):
    twin, motion, limits, geometry = datum_inputs
    twin.register_datum(motion, limits, confirmed=True)
    if damage == "disconnect":
        motion["connected"] = False
    elif damage == "fault":
        motion["faulted"] = True
    elif damage == "feedback":
        motion["feedback_error"] = {"code": "read_failed"}
    elif damage == "reconnect":
        motion["connection_token"] = "f6546a4f-0818-48fc-ae5f-50e7f6f13ae4"
    elif damage == "profile":
        motion["axes"]["Z"]["config"]["microstep"] = 32
    elif damage == "mechanics":
        motion["axes"]["X"]["mechanical"]["lead_mm"] = 4
    elif damage == "boolean_profile":
        motion["axes"]["X"]["config"]["response_mode"] = True
    elif damage == "stale":
        motion["axes"]["Z"]["feedback_age_s"] = 2.001
    else:
        motion["axes"]["X"]["raw_count"] = None
    result = twin.snapshot(motion, None)
    assert result["state"] == state and result["registered"]
    assert result["pose"] is None and not result["fresh"]
    assert result["error"]["code"] == code


@pytest.mark.parametrize("damage", ["missing", "counts", "boolean", "capture", "profile", "token", "version"])
def test_datum_rejects_incomplete_invalid_or_mismatched_draft_capture(datum_inputs, damage):
    twin, motion, limits, geometry = datum_inputs
    draft = limits["draft"]
    if damage == "missing":
        draft["captures"].pop("z_up")
    elif damage == "counts":
        draft["captures"]["x_left"]["raw_counts"].pop("Z")
    elif damage == "boolean":
        draft["captures"]["x_left"]["raw_counts"]["X"] = True
    elif damage == "capture":
        draft["captures"]["z_up"]["captured_unix_s"] = float("nan")
    elif damage == "profile":
        draft["profiles"]["X"]["config"].pop("raw_hex")
    elif damage == "token":
        draft["connection_token"] = "f6546a4f-0818-48fc-ae5f-50e7f6f13ae4"
    else:
        draft["version"] = "unknown"
    with pytest.raises(AutomationError):
        twin.register_datum(motion, limits, confirmed=True)
    assert not twin.path.exists()


@pytest.mark.parametrize("axis,field,value", [
    ("X", "travel_mm", 149), ("Z", "travel_mm", 51),
    ("X", "positive_direction", 1), ("Z", "positive_direction", 1),
])
def test_nominal_datum_rejects_incompatible_mechanical_convention(datum_inputs, axis, field, value):
    twin, motion, limits, geometry = datum_inputs
    motion["axes"][axis]["mechanical"][field] = value
    limits["draft"]["motion"]["axes"][axis][field] = value
    with pytest.raises(AutomationError) as caught:
        twin.register_datum(motion, limits, confirmed=True)
    assert caught.value.code == "twin_datum_incompatible"
    assert not twin.path.exists()


def test_nominal_datum_rejects_different_cad_reference(datum_inputs):
    twin, motion, limits, geometry = datum_inputs
    data = json.loads(geometry.read_text())
    data["reference_pose"]["x_offset_mm"] = 50
    geometry.write_text(json.dumps(data))
    with pytest.raises(AutomationError) as caught:
        twin.register_datum(motion, limits, confirmed=True)
    assert caught.value.code == "twin_datum_incompatible"
    assert not twin.path.exists()


def test_datum_reloads_same_connection_and_rejects_changed_geometry(datum_inputs):
    from pa_host.digital_twin import DigitalTwin

    twin, motion, limits, geometry = datum_inputs
    original = twin.register_datum(motion, limits, confirmed=True)
    reloaded = DigitalTwin(twin.path.parent, geometry)
    assert reloaded.snapshot(motion, None) == original
    geometry.write_text(geometry.read_text() + " ")
    result = reloaded.snapshot(motion, None)
    assert result["error"]["code"] == "twin_geometry_changed"
    assert result["pose"] is None


@pytest.mark.parametrize("damage", ["unknown", "mode", "boolean", "basis", "anchor", "count", "binding", "version", "hash"])
def test_corrupt_saved_datum_never_becomes_live_pose(datum_inputs, damage):
    from pa_host.digital_twin import DigitalTwin

    twin, motion, limits, geometry = datum_inputs
    twin.register_datum(motion, limits, confirmed=True)
    record = json.loads(twin.path.read_text())
    if damage == "unknown":
        record["raw_counts"] = {"X": 1, "Z": 1}
    elif damage == "mode":
        record["mode"] = "four_boundaries"
    elif damage == "boolean":
        record["display_only"] = 1
    elif damage == "basis":
        record["basis"] = "measured_stops"
    elif damage == "anchor":
        record["cad_anchor"]["lift_mm"] = 49
    elif damage == "count":
        record["anchor_counts"]["X"] = True
    elif damage == "binding":
        record["binding"]["profiles"]["X"]["config"].pop("options_hex")
    elif damage == "version":
        record["version"] = "digital-twin-registration-1"
    else:
        record["geometry_sha256"] = "bad"
    twin.path.write_text(json.dumps(record))
    result = DigitalTwin(twin.path.parent, geometry).snapshot(motion, None)
    assert result["state"] == "error" and not result["registered"]
    assert result["pose"] is None
    assert result["error"]["code"] == "twin_registration_corrupt"


def test_failed_datum_save_preserves_previous_registration(datum_inputs, monkeypatch):
    twin, motion, limits, geometry = datum_inputs
    original = twin.register_datum(motion, limits, confirmed=True)
    saved = twin.path.read_bytes()
    limits["draft"]["captures"]["x_left"]["raw_counts"]["X"] = 10

    def fail_replace(source, target):
        raise OSError("storage unavailable")

    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(AutomationError) as caught:
        twin.register_datum(motion, limits, confirmed=True)
    assert caught.value.code == "twin_registration_save_failed"
    assert twin.path.read_bytes() == saved
    assert twin.snapshot(motion, None) == original
    assert not list(twin.path.parent.glob(".*.tmp"))


def test_right_down_datum_uses_only_new_same_connection_corner(datum_inputs):
    twin, motion, limits, geometry = datum_inputs
    limits["draft"]["captures"] = {
        "x_right": {"raw_counts": {"X": 0, "Z": 111}, "captured_unix_s": 1792713700},
        "z_down": {"raw_counts": {"X": 222, "Z": 9}, "captured_unix_s": 1792713701}}
    motion["axes"]["X"]["raw_count"] = 0
    motion["axes"]["Z"]["raw_count"] = 9
    result = twin.register_datum(motion, limits, confirmed=True, datum="right_down")
    assert result["registration"]["datum"] == "right_down"
    assert result["registration"]["mode"] == "right_down_datum"
    assert result["registration"]["anchor_counts"] == {"X": 0, "Z": 9}
    assert result["registration"]["cad_anchor"] == {"x_center_mm": 75, "lift_mm": 0}
    assert result["registration"]["accuracy_unverified"] is True
    assert result["pose"]["scene_x_mm"] == 25.5
    assert result["pose"]["scene_z_mm"] == -35
    motion["axes"]["X"]["raw_count"] = 65536
    motion["axes"]["Z"]["raw_count"] = 9 - 32768
    result = twin.snapshot(motion, None)
    assert result["state"] == "synced"
    assert result["pose"]["x_center_mm"] == 73
    assert result["pose"]["lift_mm"] == 1


def test_right_down_datum_uses_active_saved_boundaries_when_available(twin_inputs):
    twin, motion, limits, geometry = twin_inputs
    result = twin.register_datum(motion, limits, confirmed=True, datum="right_down")
    assert result["registration"]["anchor_counts"] == {"X": 0, "Z": 65536}
    assert result["pose"]["x_center_mm"] == 75
    assert result["pose"]["lift_mm"] == 2


def test_right_down_datum_does_not_reuse_opposite_corner_captures(datum_inputs):
    twin, motion, limits, geometry = datum_inputs
    with pytest.raises(AutomationError) as caught:
        twin.register_datum(motion, limits, confirmed=True, datum="right_down")
    assert caught.value.code == "twin_datum_required"
    assert not twin.path.exists()


@pytest.mark.parametrize("datum", [None, True, {}, [], "left_down", "unknown"])
def test_datum_rejects_unsupported_corner_selection(datum_inputs, datum):
    twin, motion, limits, geometry = datum_inputs
    with pytest.raises(AutomationError) as caught:
        twin.register_datum(motion, limits, confirmed=True, datum=datum)
    assert caught.value.code == "invalid_twin_datum"
    assert not twin.path.exists()


def test_saved_datum_rejects_corner_anchor_or_mode_mismatch(datum_inputs):
    from pa_host.digital_twin import DigitalTwin

    twin, motion, limits, geometry = datum_inputs
    twin.register_datum(motion, limits, confirmed=True)
    record = json.loads(twin.path.read_text())
    record["datum"] = "right_down"
    twin.path.write_text(json.dumps(record))
    result = DigitalTwin(twin.path.parent, geometry).snapshot(motion, None)
    assert result["error"]["code"] == "twin_registration_corrupt"
    assert result["pose"] is None


@pytest.mark.parametrize("damage", ["mechanical", "profiles", "config"])
def test_malformed_datum_capture_binding_has_structured_error(datum_inputs, damage):
    twin, motion, limits, geometry = datum_inputs
    draft = limits["draft"]
    if damage == "mechanical":
        draft["motion"]["axes"]["X"] = None
    elif damage == "profiles":
        draft["profiles"] = None
    else:
        draft["profiles"]["X"]["config"]["raw_hex"] = 2
    with pytest.raises(AutomationError):
        twin.register_datum(motion, limits, confirmed=True)
    assert not twin.path.exists()


def test_datum_saved_limit_source_must_still_be_installed_as_active(twin_inputs):
    twin, motion, limits, geometry = twin_inputs
    motion["absolute_limits"] = None
    with pytest.raises(AutomationError) as caught:
        twin.register_datum(motion, limits, confirmed=True)
    assert caught.value.code == "twin_limits_changed"
    assert not twin.path.exists()


def test_saved_nominal_datum_does_not_accept_incompatible_geometry_reference(datum_inputs):
    from pa_host.digital_twin import DigitalTwin

    twin, motion, limits, geometry = datum_inputs
    twin.register_datum(motion, limits, confirmed=True)
    data = json.loads(geometry.read_text())
    data["reference_pose"]["lift_mm"] = 40
    geometry.write_text(json.dumps(data))
    record = json.loads(twin.path.read_text())
    record["geometry_sha256"] = hashlib.sha256(geometry.read_bytes()).hexdigest()
    twin.path.write_text(json.dumps(record))
    result = DigitalTwin(twin.path.parent, geometry).snapshot(motion, None)
    assert result["error"]["code"] == "twin_datum_incompatible"
    assert result["pose"] is None


def test_power_flag_change_invalidates_datum_after_counter_reset(datum_inputs):
    twin, motion, limits, geometry = datum_inputs
    twin.register_datum(motion, limits, confirmed=True)
    motion["axes"]["Z"]["power_flag_baseline"] = 128
    motion["axes"]["Z"]["raw_count"] = 0
    result = twin.snapshot(motion, None)
    assert result["error"]["code"] == "twin_binding_changed"
    assert result["pose"] is None


def test_datum_capture_remains_usable_after_ordinary_stop(datum_inputs):
    twin, motion, limits, geometry = datum_inputs
    motion["stop_generation"] += 1
    result = twin.register_datum(motion, limits, confirmed=True)
    assert result["state"] == "synced"
    assert result["registration"]["anchor_counts"] == {"X": 7, "Z": 0}
