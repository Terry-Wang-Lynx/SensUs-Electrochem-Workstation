from __future__ import annotations

import copy
import json

import pytest

from pa_host.automation import AutomationError
from pa_host.emm_motion import MotionError
from pa_host.guided_automation import GuidedAutomation
from test_guided_automation import CalibrationMotion, calibrated
from test_powered_manual_driver import powered_bus


class PoweredCalibrationMotion(CalibrationMotion):
    manual_capture_supported = True
    manual_calibration_supported = False

    def __init__(self, document):
        super().__init__(document)
        self.connection_token = "powered-session-1"
        self.referenced = True
        self.profiles = super().reference_identity()["profiles"]
        self.referenced = False
        self.confirmations = None

    def snapshot(self):
        state = super().snapshot()
        state["connection_token"] = self.connection_token
        for axis, profile in self.profiles.items():
            state["axes"][axis].update(copy.deepcopy(profile))
        return state

    def begin_manual_calibration(self, *, release_confirmed=False, z_supported=False):
        self.confirmations = (release_confirmed, z_supported)
        if release_confirmed is not True or z_supported is not True:
            raise MotionError("manual_release_confirmation", "Both release and Z support need explicit confirmation")
        return super().begin_manual_calibration()

    def capture_manual_position(self):
        return {**super().capture_manual_position(), "connection_token": self.connection_token,
                "profiles": copy.deepcopy(self.profiles)}

    def reference_identity(self):
        return {**super().reference_identity(), "profiles": copy.deepcopy(self.profiles)}


def four_point_session(tmp_path, *, finish=True, omit=()):
    service = GuidedAutomation(tmp_path, require_four_point=True)
    document = service.snapshot()["document"]
    motion = PoweredCalibrationMotion(document)
    service.begin_calibration(motion, release_confirmed=True, z_supported=True)
    captures = {"first": {"X": 0, "Z": 0}, "last": {"X": 99 * 32768, "Z": 2 * 32768},
                "working": {"X": 40 * 32768, "Z": 6 * 32768},
                "safe": {"X": 40 * 32768, "Z": 0}}
    for point, counts in captures.items():
        if point in omit:
            continue
        motion.counts = counts
        service.capture_calibration(motion, point)
    if finish:
        service.finish_calibration(motion)
    return service, document, motion


def test_four_independent_captures_use_working_z_and_physical_column_spacing(tmp_path):
    service, document, motion = four_point_session(tmp_path)
    calibration = service.snapshot()["calibration"]
    assert service.snapshot()["four_point_required"] is True
    assert calibration["four_point"] is True
    assert calibration["connection_token"] == "powered-session-1"
    assert calibration["points"]["first"]["physical_column"] == 1
    assert calibration["points"]["last"]["physical_column"] == 12
    assert calibration["points"]["working"]["physical_column"] is None
    assert calibration["z_working_mm"] == 7
    assert calibration["z_safe_mm"] == 1
    assert calibration["reference_margin_mm"] == {"X": 1, "Z": 1}
    assert motion.confirmations == (True, True)
    for column in document["columns"]:
        column["enabled"] = column["index"] == 7
    compiled = service.compile(document, mode="live", motion=motion)
    move = next(step for step in compiled["steps"] if step["op"] == "move_x")
    assert move["column"] == 7
    assert move["resolved_params"]["target_mm"] == 55
    assert compiled["experiment"]["defaults"]["z_immersed_mm"] == 7


@pytest.mark.parametrize("release_confirmed,z_supported", [(False, True), (True, False), (1, True), (True, "true")])
def test_production_begin_needs_both_literal_safety_confirmations(tmp_path, release_confirmed, z_supported):
    service = GuidedAutomation(tmp_path, require_four_point=True)
    motion = PoweredCalibrationMotion(service.snapshot()["document"])
    with pytest.raises(AutomationError) as caught:
        service.begin_calibration(motion, release_confirmed=release_confirmed, z_supported=z_supported)
    assert caught.value.code == "manual_release_confirmation"
    assert service.snapshot()["manual"]["active"] is False
    assert motion.calls == []


def test_strict_finish_requires_separate_working_capture(tmp_path):
    service, document, motion = four_point_session(tmp_path, finish=False, omit=("working",))
    with pytest.raises(AutomationError) as caught:
        service.finish_calibration(motion)
    assert caught.value.code == "calibration_points"
    assert service.snapshot()["manual"]["required_points"] == ["first", "last", "working", "safe"]
    assert service.snapshot()["manual"]["active"] is True
    assert "finish" not in motion.calls


@pytest.mark.parametrize("change", [
    lambda motion: setattr(motion, "connection_token", "new-power-session"),
    lambda motion: motion.profiles["X"].update(hardware_version=770),
    lambda motion: motion.profiles["Z"].update(power_flag_baseline=128),
])
def test_capture_identity_change_blocks_mixing_points_and_preserves_evidence(tmp_path, change):
    service = GuidedAutomation(tmp_path, require_four_point=True)
    motion = PoweredCalibrationMotion(service.snapshot()["document"])
    service.begin_calibration(motion, release_confirmed=True, z_supported=True)
    service.capture_calibration(motion, "first")
    first = service.snapshot()["manual"]["points"]["first"]
    change(motion)
    with pytest.raises(AutomationError):
        service.capture_calibration(motion, "last")
    state = service.snapshot()
    assert state["manual"]["status"] == "blocked"
    assert state["manual"]["points"] == {"first": first}
    assert "finish" not in motion.calls


def test_finish_rechecks_connection_before_attempting_relock(tmp_path):
    service, document, motion = four_point_session(tmp_path, finish=False)
    motion.connection_token = "reconnected"
    with pytest.raises(AutomationError):
        service.finish_calibration(motion)
    assert "finish" not in motion.calls
    assert service.snapshot()["manual"]["status"] == "blocked"


def test_relock_qualification_failure_keeps_four_points_blocked_and_cannot_run(tmp_path):
    service, document, motion = four_point_session(tmp_path, finish=False)
    previous = service.snapshot()["manual"]["points"]
    motion.error = MotionError("manual_relock_target", "Target register differs from actual encoder")
    with pytest.raises(AutomationError) as caught:
        service.finish_calibration(motion)
    assert caught.value.code == "manual_relock_target"
    state = service.snapshot()
    assert state["manual"]["status"] == "blocked"
    assert state["manual"]["active"] is True
    assert state["manual"]["points"] == previous
    assert state["calibration"] is None
    with pytest.raises(AutomationError):
        service.compile(document, mode="live", motion=motion)


def test_four_point_persistence_keeps_independent_z_and_rejects_new_connection(tmp_path):
    service, document, motion = four_point_session(tmp_path)
    restored = GuidedAutomation(tmp_path, require_four_point=True)
    assert restored.snapshot()["calibration"]["z_working_mm"] == 7
    restored.compile(document, mode="simulate")
    motion.connection_token = "new-connection"
    with pytest.raises(AutomationError):
        restored.compile(document, mode="live", motion=motion)
    assert "restore" not in motion.calls


def test_legacy_three_point_record_remains_viewable_and_simulates_but_cannot_authorize_strict_live(tmp_path):
    service, document, motion = calibrated(tmp_path)
    restored = GuidedAutomation(tmp_path, require_four_point=True)
    assert restored.snapshot()["calibration"]["id"] == service.snapshot()["calibration"]["id"]
    assert restored.snapshot()["calibration"]["usable"] is False
    assert restored.snapshot()["calibration"]["four_point"] is False
    assert restored.compile(document)["experiment"]["defaults"]["z_immersed_mm"] == 7
    with pytest.raises(AutomationError) as caught:
        restored.compile(document, mode="live", motion=motion)
    assert caught.value.code == "calibration_required"


@pytest.mark.parametrize("change", [
    lambda motion: setattr(motion, "connection_token", "other-session"),
    lambda motion: motion.reference.update(X=motion.reference["X"] + 1),
    lambda motion: motion.profiles["X"].update(power_flag_baseline=128),
])
def test_trusted_record_requires_same_connection_reference_and_profiles(tmp_path, change):
    service, document, motion = four_point_session(tmp_path)
    trusted = service.trusted_calibration(motion)
    trusted["points"]["working"]["raw_counts"]["Z"] = 0
    assert service.snapshot()["calibration"]["z_working_mm"] == 7
    change(motion)
    with pytest.raises(AutomationError):
        service.trusted_calibration(motion)
    with pytest.raises(AutomationError):
        service.compile(document, mode="live", motion=motion)


def test_invalid_four_point_saved_record_cannot_be_downgraded_to_legacy(tmp_path):
    service, document, motion = four_point_session(tmp_path)
    path = tmp_path / "guided-automation.json"
    saved = json.loads(path.read_text(encoding="utf-8"))
    saved["calibration"].pop("connection_token")
    path.write_text(json.dumps(saved), encoding="utf-8")
    restored = GuidedAutomation(tmp_path, require_four_point=True).snapshot()
    assert restored["calibration"] is None
    assert restored["manual"]["status"] == "blocked"


def test_cached_snapshot_detects_new_connection_without_reading_hardware(tmp_path):
    service, document, motion = four_point_session(tmp_path)
    assert service.snapshot()["calibration"]["usable"] is True
    motion.error = MotionError("no_io", "Snapshot must not read hardware")
    motion.connection_token = "after-reconnection"
    state = service.snapshot()
    assert state["calibration"]["usable"] is False
    assert state["calibration"]["status"] == "stale"


def test_same_session_stop_checkpoint_keeps_four_point_geometry_and_can_restore(tmp_path):
    service, document, motion = four_point_session(tmp_path)
    motion.counts = {"X": 30 * 32768, "Z": 0}
    before_stop = motion.reference_identity()
    motion.stop_all()
    service.checkpoint_reference(motion, before_stop)
    assert service.snapshot()["calibration"]["usable"] is True
    compiled = service.compile(document, mode="live", motion=motion)
    assert compiled["experiment"]["defaults"]["z_immersed_mm"] == 7
    assert motion.calls[-1] == "restore"


def test_new_connection_cannot_replace_four_point_parked_checkpoint(tmp_path):
    service, document, motion = four_point_session(tmp_path)
    saved = service.snapshot()["calibration"]
    motion.connection_token = "other-connection"
    with pytest.raises(AutomationError):
        service.checkpoint_reference(motion)
    assert service.snapshot()["calibration"]["reference"] == saved["reference"]


def test_identity_change_during_relock_preserves_blocked_capture_evidence(tmp_path):
    service, document, motion = four_point_session(tmp_path, finish=False)
    original_finish = motion.finish_manual_calibration

    def finish_after_connection_change(reference_counts):
        result = original_finish(reference_counts)
        motion.connection_token = "unexpected-reconnection"
        return result

    motion.finish_manual_calibration = finish_after_connection_change
    with pytest.raises(AutomationError):
        service.finish_calibration(motion)
    state = service.snapshot()
    assert state["manual"]["status"] == "blocked"
    assert set(state["manual"]["points"]) == {"first", "last", "working", "safe"}
    assert state["calibration"] is None


def test_identity_change_during_live_verification_cannot_return_a_trusted_record(tmp_path):
    service, document, motion = four_point_session(tmp_path)
    original_identity = motion.reference_identity

    def identity_after_connection_change():
        result = original_identity()
        motion.connection_token = "unexpected-reconnection"
        return result

    motion.reference_identity = identity_after_connection_change
    with pytest.raises(AutomationError):
        service.trusted_calibration(motion)


def test_safe_height_is_checked_against_independent_working_height(tmp_path):
    service, document, motion = four_point_session(tmp_path, finish=False)
    motion.counts = {"X": 40 * 32768, "Z": 7 * 32768}
    service.capture_calibration(motion, "safe")
    with pytest.raises(AutomationError) as caught:
        service.finish_calibration(motion)
    assert caught.value.code == "calibration_height"
    assert "finish" not in motion.calls


@pytest.mark.parametrize("change", [
    lambda calibration: calibration.update(z_working_mm=calibration["x_first_mm"]),
    lambda calibration: calibration["points"]["working"].update(physical_column=1),
    lambda calibration: calibration.update(connection_token=""),
    lambda calibration: calibration["points"].pop("safe"),
])
def test_corrupt_four_point_geometry_or_identity_blocks_saved_state(tmp_path, change):
    service, document, motion = four_point_session(tmp_path)
    path = tmp_path / "guided-automation.json"
    saved = json.loads(path.read_text(encoding="utf-8"))
    change(saved["calibration"])
    path.write_text(json.dumps(saved), encoding="utf-8")
    restored = GuidedAutomation(tmp_path, require_four_point=True).snapshot()
    assert restored["calibration"] is None
    assert restored["manual"]["status"] == "blocked"


def test_snapshot_does_not_claim_released_before_driver_confirmed_release(tmp_path):
    service = GuidedAutomation(tmp_path, require_four_point=True)
    motion = PoweredCalibrationMotion(service.snapshot()["document"])
    during_begin = []
    original_begin = motion.begin_manual_calibration

    def observed_begin(**confirmations):
        during_begin.append(service.snapshot()["manual"]["status"])
        return original_begin(**confirmations)

    motion.begin_manual_calibration = observed_begin
    service.begin_calibration(motion, release_confirmed=True, z_supported=True)
    assert during_begin == ["releasing"]
    assert service.snapshot()["manual"]["status"] == "released"


@pytest.mark.parametrize("field,value", [("enabled", False), ("enable_confirmed", False), ("enabled", 1)])
def test_finish_requires_actual_enabled_axis_confirmation(tmp_path, field, value):
    service, document, motion = four_point_session(tmp_path, finish=False)
    original_snapshot = motion.snapshot

    def unconfirmed_snapshot():
        state = original_snapshot()
        if "finish" in motion.calls:
            state["axes"]["Z"][field] = value
        return state

    motion.snapshot = unconfirmed_snapshot
    with pytest.raises(AutomationError) as caught:
        service.finish_calibration(motion)
    assert caught.value.code == "enable_unconfirmed"
    assert service.snapshot()["manual"]["status"] == "blocked"
    assert service.snapshot()["manual"]["active"] is True
    assert service.snapshot()["calibration"] is None


@pytest.mark.parametrize("matching_targets", [True, False])
def test_four_point_model_with_real_driver_obeys_target_guard_without_position_commands(tmp_path, matching_targets):
    bus, serial, events = powered_bus(enabled=True)
    service = GuidedAutomation(tmp_path, require_four_point=True)
    document = service.snapshot()["document"]
    document["bindings"]["motion"] = {"port": bus.port, "baud": bus.baud, "axes": copy.deepcopy(bus.axes)}
    service.save(document)
    service.begin_calibration(bus, release_confirmed=True, z_supported=True)
    for point, positions in (("first", {1: 0, 2: 0}), ("last", {1: 99 * 32768, 2: -2 * 32768}),
                             ("working", {1: 40 * 32768, 2: -6 * 32768}), ("safe", {1: 40 * 32768, 2: 0})):
        serial.positions = positions
        service.capture_calibration(bus, point)
    if matching_targets:
        for targets in serial.targets.values():
            targets.update(serial.positions)
        service.finish_calibration(bus)
        assert service.snapshot()["calibration"]["usable"] is True
        compiled = service.compile(document, mode="live", motion=bus)
        assert compiled["experiment"]["defaults"]["z_immersed_mm"] == 7
        assert compiled["experiment"]["defaults"]["z_safe_mm"] == 1
        assert service.trusted_calibration(bus)["connection_token"] == bus.snapshot()["connection_token"]
    else:
        with pytest.raises(AutomationError) as caught:
            service.finish_calibration(bus)
        assert caught.value.code == "manual_target_mismatch"
        assert service.snapshot()["manual"]["status"] == "blocked"
        assert not any(frame[1] == 0xF3 and frame[3] == 1 for frame in serial.writes)
    assert not any(frame[1] in (0xFD, 0x0A, 0x93, 0x9A) for frame in serial.writes)
    bus.close()
