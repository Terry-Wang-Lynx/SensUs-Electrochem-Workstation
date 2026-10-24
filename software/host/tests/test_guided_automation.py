from __future__ import annotations

import copy
import importlib
import json

import pytest

from pa_host.automation import AutomationError, default_experiment
from test_emm_motion import config_reply


def guided_module():
    return importlib.import_module("pa_host.guided_automation")


class CalibrationMotion:
    manual_calibration_supported = True

    def __init__(self, document):
        self.motion = copy.deepcopy(document["bindings"]["motion"])
        self.counts = {"X": 0, "Z": 0}
        self.reference = {"X": 0, "Z": 0}
        self.manual = False
        self.referenced = False
        self.calls = []
        self.error = None

    def snapshot(self):
        return {"connected": True, "faulted": False, "referenced": self.referenced,
                "port": self.motion["port"], "baud": self.motion["baud"],
                "axes": {axis: {"mechanical": settings, "raw_count": self.counts[axis],
                                "reference_count": self.reference[axis],
                                "referenced": self.referenced, "telemetry_valid": True,
                                "enabled": not self.manual, "enable_confirmed": not self.manual}
                         for axis, settings in self.motion["axes"].items()}}

    def refresh(self):
        return self.snapshot()

    def begin_manual_calibration(self):
        self.calls.append("begin")
        self.manual = True
        self.referenced = False
        return self.capture_manual_position()

    def capture_manual_position(self):
        if self.error:
            raise self.error
        if not self.manual:
            raise ValueError("Not manual")
        return {"raw_counts": copy.deepcopy(self.counts), "motion": copy.deepcopy(self.motion)}

    def finish_manual_calibration(self, reference_counts):
        self.calls.append("finish")
        if self.error:
            raise self.error
        self.reference = copy.deepcopy(reference_counts)
        self.manual = False
        self.referenced = True
        return self.reference_identity()

    def cancel_manual_calibration(self):
        self.calls.append("cancel")
        if self.error:
            raise self.error
        self.manual = False
        self.referenced = False
        return self.snapshot()

    def reference_identity(self):
        if not self.referenced:
            raise ValueError("Both axes require a known software reference")
        return {"version": "emm-reference-1", "motion": copy.deepcopy(self.motion),
                "reference_counts": copy.deepcopy(self.reference),
                "raw_counts": copy.deepcopy(self.counts),
                "profiles": {axis: {"firmware_version": 107, "hardware_version": 769,
                                    "power_flag_baseline": 0,
                                    "config": {"microstep": 16, "step_angle": 1.8, "pulses_per_rev": 3200,
                                               "response_mode": 1, "stall_protect": 1, "position_window_deg": 0.8,
                                               "raw_hex": config_reply(settings["address"]).hex(),
                                               "options_hex": f"{settings['address']:02x}1a06006b"}}
                             for axis, settings in self.motion["axes"].items()}}

    def restore_reference(self, reference):
        self.calls.append("restore")
        if reference["motion"] != self.motion or reference["raw_counts"] != self.counts:
            raise ValueError("Reference checkpoint differs")
        self.reference = copy.deepcopy(reference["reference_counts"])
        self.referenced = True
        return self.snapshot()

    def stop_all(self):
        self.referenced = False
        self.reference = {"X": None, "Z": None}
        return {"stop_confirmed": True}

    def parked_reference_checkpoint(self, before_stop):
        if self.error:
            raise self.error
        if before_stop["motion"] != self.motion or any(abs(self.counts[axis] - before_stop["raw_counts"][axis]) > 1
                                                       for axis in self.counts):
            raise ValueError("Post-STOP parked encoder identity differs")
        return {**copy.deepcopy(before_stop), "raw_counts": copy.deepcopy(self.counts)}


def calibrated(tmp_path, *, negative=False):
    module = guided_module()
    service = module.GuidedAutomation(tmp_path)
    document = module.default_guided_experiment()
    service.save(document)
    motion = CalibrationMotion(document)
    service.begin_calibration(motion)
    motion.counts = {"X": 99 * 32768 if negative else 0, "Z": 6 * 32768}
    service.capture_calibration(motion, "first")
    motion.counts = {"X": 0 if negative else 99 * 32768, "Z": 8 * 32768}
    service.capture_calibration(motion, "last")
    motion.counts = {"X": 0 if negative else 99 * 32768, "Z": 0}
    service.capture_calibration(motion, "safe")
    service.finish_calibration(motion)
    return service, document, motion


def test_default_has_fixed_physical_columns_and_electrodes():
    document = guided_module().default_guided_experiment()
    assert document["schema_version"] == "guided-1.0"
    assert [column["index"] for column in document["columns"]] == list(range(1, 13))
    assert [column["name"] for column in document["columns"]] == list("ABCDEFGHIJKL")
    assert all(column["enabled"] and column["duration_s"] == 180 and column["windows"] == {}
               for column in document["columns"])
    assert [electrode["id"] for electrode in document["electrodes"]] == list("ABCDEFGH")
    assert all(electrode["enabled"] for electrode in document["electrodes"])
    assert document["defaults"] == {field: default_experiment()["defaults"][field] for field in
                                    ("descend_speed_mm_s", "ascend_speed_mm_s", "translate_speed_mm_s",
                                     "settle_s", "dwell_s")}


def test_save_roundtrip_does_not_start_calibration(tmp_path):
    module = guided_module()
    service = module.GuidedAutomation(tmp_path)
    document = module.default_guided_experiment()
    document["columns"][6]["name"] = "10微摩尔每升"
    service.save(document)
    restored = module.GuidedAutomation(tmp_path).snapshot()
    assert restored["document"] == document
    assert restored["calibration"] is None
    assert restored["manual"]["active"] is False


@pytest.mark.parametrize("selected", [(2, 7, 11), (7,)])
@pytest.mark.parametrize("negative", [False, True])
def test_compile_preserves_physical_spacing_and_nullable_concentration(tmp_path, selected, negative):
    service, document, motion = calibrated(tmp_path, negative=negative)
    for column in document["columns"]:
        column["enabled"] = column["index"] in selected
        column["name"] = "任意名称"
    compiled = service.compile(document, mode="live", motion=motion)
    assert compiled["experiment"]["schema_version"] == "1.1"
    assert compiled["experiment"]["run_policy"] == "guided"
    assert compiled["experiment"]["calibration_id"] == service.snapshot()["calibration"]["id"]
    moves = [step for step in compiled["steps"] if step["op"] == "move_x"]
    assert [step["column"] for step in moves] == list(selected)
    assert [step["resolved_params"]["target_mm"] for step in moves] == [
        (100 - (column - 1) * 9) if negative else 1 + (column - 1) * 9 for column in selected]
    assert all(step["concentration"] is None for step in compiled["steps"])
    assert compiled["experiment"]["defaults"]["z_immersed_mm"] == 7
    assert compiled["experiment"]["defaults"]["z_safe_mm"] == 1


def test_selection_and_labels_do_not_recalibrate(tmp_path):
    service, document, motion = calibrated(tmp_path)
    previous = service.snapshot()["calibration"]
    calls = list(motion.calls)
    document["columns"][0]["enabled"] = False
    document["columns"][1]["name"] = "任意重复"
    document["columns"][2]["name"] = "任意重复"
    service.save(document)
    assert service.snapshot()["calibration"] == previous
    assert motion.calls == calls


def test_channel_windows_defaults_and_duration_inheritance(tmp_path):
    service = guided_module().GuidedAutomation(tmp_path)
    document = guided_module().default_guided_experiment()
    document["defaults"]["dwell_s"] = 12
    for column in document["columns"]:
        column["duration_s"] = None
        column["enabled"] = column["index"] == 7
    for electrode in document["electrodes"]:
        electrode["enabled"] = electrode["id"] in ("A", "H")
    document["columns"][6]["windows"] = {"H": {"start_s": 3, "duration_s": 5}}
    compiled = service.compile(document)
    dwell = next(step for step in compiled["steps"] if step["op"] == "dwell")
    assert dwell["resolved_params"]["windows"] == {"A": {"start_s": 0, "duration_s": 12},
                                                    "H": {"start_s": 3, "duration_s": 5}}
    assert dwell["resolved_params"]["acquire"] == ["A", "H"]
    assert dwell["resolved_params"]["channel_masks"] == {"A": 1, "B": 8}
    assert compiled["experiment"]["calibration_id"] == "synthetic"
    assert any("synthetic" in warning.lower() for warning in compiled["warnings"])


@pytest.mark.parametrize("change", [
    lambda document: document.update(calibration={"x_first_mm": 0}),
    lambda document: document["columns"][0].update(index=True),
    lambda document: document["columns"][0].update(enabled=1),
    lambda document: document["columns"][0].update(name=""),
    lambda document: document["columns"][0].update(duration_s=-1),
    lambda document: document["columns"][0].update(windows={"A": {"start_s": -1, "duration_s": 1}}),
    lambda document: document["columns"][0].update(windows={"A": {"start_s": 170, "duration_s": 11}}),
    lambda document: document["columns"][0].update(windows={"I": {"start_s": 0, "duration_s": 1}}),
    lambda document: document["columns"][0].update(windows={"A": {"start_s": 181, "duration_s": 0}}),
    lambda document: document["electrodes"][0].update(enabled="yes"),
    lambda document: document["columns"].pop(),
    lambda document: document["electrodes"].pop(),
    lambda document: [column.update(enabled=False) for column in document["columns"]],
    lambda document: [electrode.update(enabled=False) for electrode in document["electrodes"]],
])
def test_invalid_guided_documents_fail_without_overwriting_saved_state(tmp_path, change):
    service = guided_module().GuidedAutomation(tmp_path)
    document = service.snapshot()["document"]
    before = copy.deepcopy(document)
    change(document)
    with pytest.raises(AutomationError):
        service.save(document)
    assert service.snapshot()["document"] == before


def test_live_requires_saved_calibration_and_synthetic_never_authorizes_live(tmp_path):
    service = guided_module().GuidedAutomation(tmp_path)
    document = service.snapshot()["document"]
    service.compile(document)
    with pytest.raises(AutomationError, match="calibration"):
        service.compile(document, mode="live", motion=CalibrationMotion(document))


def test_captures_fixed_first_last_and_only_first_z_for_working_height(tmp_path):
    service, document, motion = calibrated(tmp_path)
    calibration = service.snapshot()["calibration"]
    assert calibration["points"]["first"]["physical_column"] == 1
    assert calibration["points"]["last"]["physical_column"] == 12
    assert calibration["z_working_mm"] == 7
    assert calibration["z_safe_mm"] == 1
    assert calibration["reference_margin_mm"] == {"X": 1, "Z": 1}
    assert calibration["column_pitch_mm"] == 9
    assert motion.calls == ["begin", "finish"]


def test_saved_reference_restores_only_matching_checkpoint(tmp_path):
    service, document, motion = calibrated(tmp_path, negative=True)
    restored = guided_module().GuidedAutomation(tmp_path)
    motion.referenced = False
    compiled = restored.compile(document, mode="live", motion=motion)
    assert compiled["experiment"]["defaults"]["column_pitch_mm"] == -9
    assert motion.calls[-1] == "restore"
    motion.referenced = False
    motion.counts["X"] += 100
    with pytest.raises(AutomationError, match="reference"):
        restored.compile(document, mode="live", motion=motion)


def test_changed_motion_settings_invalidate_calibration(tmp_path):
    service, document, motion = calibrated(tmp_path)
    document["bindings"]["motion"]["axes"]["X"]["lead_mm"] = 3
    service.save(document)
    assert service.snapshot()["calibration"]["usable"] is False
    with pytest.raises(AutomationError, match="calibration"):
        service.compile(document, mode="live", motion=motion)


def test_missing_points_and_equal_endpoints_cannot_finish(tmp_path):
    service = guided_module().GuidedAutomation(tmp_path)
    motion = CalibrationMotion(service.snapshot()["document"])
    service.begin_calibration(motion)
    with pytest.raises(AutomationError):
        service.finish_calibration(motion)
    for point in ("first", "last", "safe"):
        service.capture_calibration(motion, point)
    with pytest.raises(AutomationError):
        service.finish_calibration(motion)
    assert "finish" not in motion.calls


def test_cancel_keeps_previous_calibration_but_invalidates_its_reference(tmp_path):
    service, document, motion = calibrated(tmp_path)
    calibration_id = service.snapshot()["calibration"]["id"]
    service.begin_calibration(motion)
    service.cancel_calibration(motion)
    snapshot = service.snapshot()
    assert snapshot["manual"]["active"] is False
    assert snapshot["calibration"]["id"] == calibration_id
    assert snapshot["calibration"]["usable"] is False


def test_failed_relock_blocks_legacy_motion_and_survives_restart(tmp_path):
    from pa_host.emm_motion import MotionError

    service = guided_module().GuidedAutomation(tmp_path)
    motion = CalibrationMotion(service.snapshot()["document"])
    service.begin_calibration(motion)
    motion.error = MotionError("manual_relock_unqualified", "No qualified reference relock")
    with pytest.raises(AutomationError):
        service.cancel_calibration(motion)
    assert service.snapshot()["manual"]["active"] is True
    assert service.snapshot()["manual"]["status"] == "blocked"
    restored = guided_module().GuidedAutomation(tmp_path).snapshot()
    assert restored["manual"]["active"] is True
    assert restored["manual"]["status"] == "blocked"


def test_capture_requires_explicit_manual_session_and_same_driver(tmp_path):
    service = guided_module().GuidedAutomation(tmp_path)
    motion = CalibrationMotion(service.snapshot()["document"])
    with pytest.raises(AutomationError):
        service.capture_calibration(motion, "first")
    service.begin_calibration(motion)
    with pytest.raises(AutomationError):
        service.capture_calibration(CalibrationMotion(service.snapshot()["document"]), "first")
    with pytest.raises(AutomationError):
        service.capture_calibration(motion, "middle")


def test_zero_duration_window_excludes_only_that_channel_and_preserves_identity(tmp_path):
    service = guided_module().GuidedAutomation(tmp_path)
    document = service.snapshot()["document"]
    for column in document["columns"]:
        column["enabled"] = column["index"] == 1
    for electrode in document["electrodes"]:
        electrode["enabled"] = electrode["id"] in ("A", "H")
    document["columns"][0]["windows"] = {"A": {"start_s": 0, "duration_s": 0},
                                         "B": {"start_s": 0, "duration_s": 10}}
    compiled = service.compile(document)
    dwell = next(step for step in compiled["steps"] if step["op"] == "dwell")
    assert dwell["resolved_params"]["acquire"] == ["H"]
    assert dwell["resolved_params"]["channel_masks"] == {"A": 0, "B": 8}
    document["columns"][0]["windows"]["H"] = {"start_s": 0, "duration_s": 0}
    with pytest.raises(AutomationError):
        service.compile(document)


def test_synthetic_preview_fits_configured_travel_without_becoming_live_reference(tmp_path):
    service = guided_module().GuidedAutomation(tmp_path)
    document = service.snapshot()["document"]
    document["bindings"]["motion"]["axes"]["X"]["travel_mm"] = 25
    document["bindings"]["motion"]["axes"]["Z"]["travel_mm"] = 2
    service.save(document)
    compiled = service.compile(document)
    assert all(0 <= step["resolved_params"]["target_mm"] <= (25 if step["op"] == "move_x" else 2)
               for step in compiled["steps"] if step["op"].startswith("move_"))
    assert service.snapshot()["calibration"] is None


def test_live_reference_mismatch_does_not_silently_rebase_saved_geometry(tmp_path):
    service, document, motion = calibrated(tmp_path)
    previous = service.snapshot()["calibration"]
    motion.reference["X"] += 1
    with pytest.raises(AutomationError, match="reference"):
        service.compile(document, mode="live", motion=motion)
    assert service.snapshot()["calibration"] == previous


def test_checkpoint_persists_known_new_parked_counts_without_changing_base(tmp_path):
    service, document, motion = calibrated(tmp_path)
    previous = service.snapshot()["calibration"]
    motion.counts["X"] -= 32768
    service.checkpoint_reference(motion)
    restored = guided_module().GuidedAutomation(tmp_path)
    motion.referenced = False
    restored.compile(document, mode="live", motion=motion)
    calibration = restored.snapshot()["calibration"]
    assert calibration["id"] == previous["id"]
    assert calibration["reference"]["reference_counts"] == previous["reference"]["reference_counts"]
    assert calibration["reference"]["raw_counts"] == motion.counts


def test_post_stop_checkpoint_persists_new_parked_counts_without_restoring_motion(tmp_path):
    service, document, motion = calibrated(tmp_path)
    previous = service.snapshot()["calibration"]
    motion.counts["X"] -= 32768
    before_stop = motion.reference_identity()
    assert motion.stop_all()["stop_confirmed"] is True
    motion.counts["X"] += 1
    service.checkpoint_reference(motion, before_stop)
    assert motion.referenced is False
    assert motion.reference == {"X": None, "Z": None}
    checkpoint = service.snapshot()["calibration"]
    assert checkpoint["reference"]["reference_counts"] == previous["reference"]["reference_counts"]
    assert checkpoint["reference"]["raw_counts"] == motion.counts
    restored = guided_module().GuidedAutomation(tmp_path)
    restored.compile(document, mode="live", motion=motion)
    assert restored.snapshot()["calibration"]["id"] == previous["id"]


def test_post_stop_checkpoint_rejects_changed_saved_base_and_keeps_previous_checkpoint(tmp_path):
    service, document, motion = calibrated(tmp_path)
    previous = service.snapshot()["calibration"]
    before_stop = motion.reference_identity()
    motion.stop_all()
    before_stop["reference_counts"]["X"] += 1
    with pytest.raises(AutomationError, match="reference"):
        service.checkpoint_reference(motion, before_stop)
    assert service.snapshot()["calibration"] == previous
    assert motion.referenced is False


@pytest.mark.parametrize("corrupt", [
    lambda calibration: "bad-calibration",
    lambda calibration: {},
    lambda calibration: {field: value for field, value in calibration.items() if field != "id"},
    lambda calibration: {**calibration, "x_first_mm": float("nan")},
    lambda calibration: {**calibration, "column_pitch_mm": calibration["column_pitch_mm"] + 1},
    lambda calibration: {**calibration, "z_safe_mm": calibration["z_working_mm"]},
    lambda calibration: {**calibration, "reference_valid": "yes"},
    lambda calibration: {**calibration, "reference_margin_mm": {"X": 0, "Z": 1}},
    lambda calibration: {**calibration, "points": {}},
    lambda calibration: {**calibration, "points": {**calibration["points"], "first": {"physical_column": 2, "raw_counts": {"X": 0, "Z": 0}}}},
    lambda calibration: {**calibration, "reference": {**calibration["reference"], "raw_counts": {"X": "0", "Z": 0}}},
    lambda calibration: {**calibration, "reference": {**calibration["reference"], "reference_counts": {"X": 0, "Z": 0}}},
    lambda calibration: {**calibration, "reference": {**calibration["reference"], "profiles": {"X": {}, "Z": {}}}},
])
def test_malformed_persisted_calibration_blocks_startup_without_crashing_snapshot(tmp_path, corrupt):
    service, document, motion = calibrated(tmp_path)
    path = tmp_path / "guided-automation.json"
    saved = json.loads(path.read_text(encoding="utf-8"))
    saved["calibration"] = corrupt(saved["calibration"])
    path.write_text(json.dumps(saved), encoding="utf-8")
    restored = guided_module().GuidedAutomation(tmp_path)
    snapshot = restored.snapshot()
    assert snapshot["calibration"] is None
    assert snapshot["manual"]["active"] is True
    assert snapshot["manual"]["status"] == "blocked"
    assert snapshot["manual"]["error"]["code"] == "state_read_failed"
    with pytest.raises(AutomationError):
        restored.compile(document, mode="live", motion=motion)
