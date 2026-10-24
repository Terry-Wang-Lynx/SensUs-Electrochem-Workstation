import copy
import threading
import time

import pytest

from pa_host.emm_motion import MotionError
import test_calibration_actual_extent as extent
import test_keyboard_responsiveness as responsive
from test_emm_motion import signed
from test_live_motion_bus import LiveMotionSerial


CAPTURES = {"first": {"X": -7243609, "Z": 2046655},
            "working": {"X": -7243609, "Z": 2046655},
            "last": {"X": -704225, "Z": 1833613},
            "safe": {"X": -704225, "Z": 572889}}
NORMAL_TAIL = [(-7243562, -6), (-7243715, -5), (-7243723, -1), (-7243723, 0), (-7243723, 0)]


class ArrivalSerial(LiveMotionSerial):
    def __init__(self, **options):
        super().__init__(**options)
        self.statuses = {1: 3, 2: 3}
        self.trace_started = False
        self.trace_index = 0
        self.quiet_since = None
        self.natural_complete = False
        self.fe_creep = False
        self.trajectory_unfinished = False
        self.drift_outside = False
        self.on_quiet = None

    def write(self, frame):
        address, opcode = frame[:2]
        if opcode == 0xFD:
            result = super().write(frame)
            assert address == 1
            self.trace_started = True
            self.targets[0x33][1] = self.targets[0x34][1] = -7243593
            return result
        if self.trace_started and address == 1 and opcode == 0xFE:
            self.fe_creep = True
            self.active_address = None
            self.positions[1] = -7243785
            self.targets[0x33][1] = self.targets[0x34][1] = -7243723
            self.speeds[1] = 0
            return super().write(frame)
        if self.trace_started and address == 1 and opcode == 0x43:
            if self.fe_creep:
                self.positions[1] = -7243785
                self.speeds[1] = -1 if self.trace_index < 1000 else 0
                self.trace_index = 1000
            else:
                raw, rpm = NORMAL_TAIL[min(self.trace_index, len(NORMAL_TAIL) - 1)]
                self.trace_index += 1
                self.positions[1], self.speeds[1] = raw, rpm
                if self.drift_outside and self.trace_index >= 4:
                    self.positions[1] = -7243785
                if rpm == 0:
                    self.quiet_since = self.quiet_since or time.monotonic()
                    if self.on_quiet is not None:
                        self.on_quiet(self)
                    self.natural_complete = time.monotonic() - self.quiet_since >= 0.12 and not self.trajectory_unfinished
                if self.trajectory_unfinished:
                    self.targets[0x34][1] = -7233593
            self.statuses[1] = 3
            self.writes.append(frame)
            self.buffer.extend(bytes([1, 0x43, 31, 9]) + (24000).to_bytes(2, "big")
                               + (3).to_bytes(2, "big") + (self.positions[1] & 0xFFFF).to_bytes(2, "big")
                               + signed(self.targets[0x33][1], 4) + signed(self.speeds[1], 2)
                               + signed(self.positions[1], 4)
                               + signed(self.targets[0x34][1] - self.positions[1], 4) + bytes([3, 3, 0x6B]))
            return len(frame)
        if self.trace_started and address == 2 and self.natural_complete:
            self.active_address = None
        return super().write(frame)


def arrival_bus(tmp_path, monkeypatch):
    monkeypatch.setattr(extent, "CAPTURES", copy.deepcopy(CAPTURES))
    monkeypatch.setattr(responsive, "SummarySerial", ArrivalSerial)
    service, bus, serial = extent.actual_session(tmp_path)
    service.finish_calibration(bus)
    record = service.trusted_calibration(bus)
    bus.install_experiment_calibration(record)
    bus.move_to = bus._move_to_target
    serial.guided_service = service
    serial.events = []
    bus._callback = serial.events.append
    assert record["reference"]["reference_counts"]["X"] == -7276377
    assert record["x_first_mm"] == 1
    assert bus.snapshot()["axes"]["X"]["config"]["position_window_deg"] == 0.8
    serial.writes.clear()
    return bus, serial, record


def test_naturally_completed_fd_keeps_original_target_without_arrival_fe(tmp_path, monkeypatch):
    bus, serial, record = arrival_bus(tmp_path, monkeypatch)
    result = bus.move_to("X", record["x_first_mm"], 20)
    assert result["axes"]["X"]["raw_count"] == -7243723
    assert result["axes"]["X"]["position_mm"] == pytest.approx(0.99652099609375)
    assert result["axes"]["X"]["arrival_tolerance_mm"] == pytest.approx(2 * 0.8 / 360)
    assert serial.targets[0x33][1] == -7243593
    assert serial.natural_complete is True
    assert not any(frame[1] == 0xFE for frame in serial.writes)
    assert serial.collisions == []
    assert serial.events[-1]["kind"] == "arrived"


def test_zero_rpm_and_reached_flag_do_not_prove_target_trajectory_completed(tmp_path, monkeypatch):
    bus, serial, record = arrival_bus(tmp_path, monkeypatch)
    serial.trajectory_unfinished = True
    bus._move_timeout = 0.6
    with pytest.raises(MotionError) as caught:
        bus.move_to("X", record["x_first_mm"], 20)
    assert caught.value.code == "move_timeout"
    assert {frame[0] for frame in serial.writes if frame[1] == 0xFE} == {1, 2}
    assert serial.collisions == []
    assert not any(event["kind"] == "arrived" for event in serial.events)


def test_drift_beyond_existing_drive_window_still_faults_and_stops(tmp_path, monkeypatch):
    bus, serial, record = arrival_bus(tmp_path, monkeypatch)
    serial.drift_outside = True
    with pytest.raises(MotionError) as caught:
        bus.move_to("X", record["x_first_mm"], 20)
    assert caught.value.code == "calibration_motion"
    assert caught.value.stop_result["stop_confirmed"] is True
    assert {frame[0] for frame in serial.writes if frame[1] == 0xFE} == {1, 2}
    assert serial.collisions == []
    assert not any(event["kind"] == "arrived" for event in serial.events)


def test_cancellation_during_natural_quiet_stops_both_axes(tmp_path, monkeypatch):
    bus, serial, record = arrival_bus(tmp_path, monkeypatch)
    cancelled = threading.Event()
    serial.on_quiet = lambda device: cancelled.set() if device.trace_index >= 6 else None
    with pytest.raises(MotionError) as caught:
        bus.move_to("X", record["x_first_mm"], 20, cancel_event=cancelled)
    assert caught.value.code == "cancelled"
    assert caught.value.stop_result["stop_confirmed"] is True
    assert {frame[0] for frame in serial.writes if frame[1] == 0xFE} == {1, 2}
    assert not serial.natural_complete
    assert serial.collisions == []
    assert not any(event["kind"] == "arrived" for event in serial.events)


def test_arrival_tolerance_does_not_authorize_target_outside_captured_window(tmp_path, monkeypatch):
    bus, serial, record = arrival_bus(tmp_path, monkeypatch)
    with pytest.raises(MotionError) as caught:
        bus.move_to("X", record["x_first_mm"] - 0.001, 20)
    assert caught.value.code == "calibration_motion"
    assert not any(frame[1] == 0xFD for frame in serial.writes)
    assert not any(event["kind"] == "arrived" for event in serial.events)


def parked_bus(tmp_path, monkeypatch):
    bus, serial, record = arrival_bus(tmp_path, monkeypatch)
    bus.move_to("Z", record["z_safe_mm"], 0.5)
    bus.move_to("X", record["x_first_mm"], 20)
    serial.writes.clear()
    return bus, serial, record


def test_natural_park_proof_reads_both_axes_without_fe_or_reference_changes(tmp_path, monkeypatch):
    bus, serial, record = parked_bus(tmp_path, monkeypatch)
    before = bus.snapshot()
    proof = bus.verify_natural_park(record)
    assert proof["basis"] == "natural_finite_completion"
    assert proof["connection_token"] == before["connection_token"]
    assert proof["stop_generation"] == before["stop_generation"]
    assert proof["reference_identity"]["reference_counts"] == record["reference"]["reference_counts"]
    assert set(proof["targets"]) == {"X", "Z"}
    after = bus.snapshot()
    assert after["referenced"] is True and not after["faulted"]
    assert after["stop_generation"] == before["stop_generation"]
    assert not any(frame[1] in (0xFD, 0xFE, 0xF3, 0xF6, 0x0A, 0x9A) for frame in serial.writes)
    for address in (1, 2):
        assert any(frame[:2] == bytes([address, 0x43]) for frame in serial.writes)
        assert any(frame[:2] == bytes([address, 0x42]) for frame in serial.writes)
    assert serial.collisions == []


def test_cancelled_natural_park_proof_never_fakes_completion_or_sends_stop(tmp_path, monkeypatch):
    bus, serial, record = parked_bus(tmp_path, monkeypatch)
    cancelled = threading.Event()
    cancelled.set()
    with pytest.raises(MotionError) as caught:
        bus.verify_natural_park(record, cancel_event=cancelled)
    assert caught.value.code == "cancelled"
    assert not any(frame[1] in (0xFD, 0xFE, 0xF3) for frame in serial.writes)


def natural_controller(tmp_path, monkeypatch):
    from pa_host.automation import AutomationController, compile_experiment, default_experiment

    bus, serial, record = arrival_bus(tmp_path / "calibration", monkeypatch)
    service = serial.guided_service
    document = default_experiment()
    document.update(schema_version="1.1", run_policy="guided", calibration_id=record["id"])
    document["bindings"]["motion"] = copy.deepcopy(record["motion"])
    document["defaults"].update(x_first_mm=record["x_first_mm"], column_pitch_mm=record["column_pitch_mm"],
                                z_safe_mm=record["z_safe_mm"], z_immersed_mm=record["z_working_mm"],
                                translate_speed_mm_s=20, settle_s=0)
    document["safety"].update(motion_policy_confirmed=True, electrical_policy_confirmed=True)
    document["program"] = [{"op": "move_to_column", "column": 1}]
    checkpoints = []
    def checkpoint(motion, identity):
        checkpoints.append(copy.deepcopy(identity))
        service.checkpoint_reference(motion, identity)
    controller = AutomationController(tmp_path / "controller", tmp_path / "runs", tmp_path,
                                      parked_callback=checkpoint)
    controller._motion = bus
    controller._motion_config = copy.deepcopy(record["motion"])
    controller._motion_calibration_validator = lambda planned: service.trusted_calibration(bus)
    return controller, bus, serial, record, document, compile_experiment(document), checkpoints


def test_successful_live_run_cleanup_uses_natural_park_without_fe_creep(tmp_path, monkeypatch):
    from test_guided_runner import wait_terminal

    controller, bus, serial, record, document, compiled, checkpoints = natural_controller(tmp_path, monkeypatch)
    controller.start(document, "live", compiled["plan_hash"], "natural-park-cleanup")
    result = wait_terminal(controller)
    assert result["state"] == "completed", result
    assert result["stop_confirmed"] is True
    assert bus.snapshot()["referenced"] is True
    assert bus.snapshot()["axes"]["X"]["raw_count"] == -7243723
    assert checkpoints == [None]
    saved = serial.guided_service.snapshot()["calibration"]
    assert saved["reference"]["reference_counts"] == record["reference"]["reference_counts"]
    assert saved["reference"]["raw_counts"]["X"] == -7243723
    assert not any(frame[1] in (0xFE, 0xF3) for frame in serial.writes)
    assert serial.collisions == []


@pytest.mark.parametrize("stage", ["park_event", "checkpoint_callback"])
@pytest.mark.parametrize("stop_fails", [False, True])
def test_stop_after_natural_proof_requires_fresh_dual_stop_and_preserves_unknown_ownership(tmp_path, monkeypatch, stage, stop_fails):
    from test_guided_runner import wait_terminal

    controller, bus, serial, record, document, compiled, checkpoints = natural_controller(tmp_path, monkeypatch)
    injected = []
    def inject_stop():
        if injected:
            return
        injected.append(True)
        if stop_fails:
            serial.overrides[2, 0xFE] = b""
        controller.stop()
    if stage == "park_event":
        emit = controller._event
        def stop_on_park_event(kind, **fields):
            emit(kind, **fields)
            if kind == "motion_naturally_parked":
                inject_stop()
        monkeypatch.setattr(controller, "_event", stop_on_park_event)
    else:
        checkpoint = controller._parked_callback
        def stop_in_checkpoint(motion, identity):
            checkpoint(motion, identity)
            inject_stop()
        monkeypatch.setattr(controller, "_parked_callback", stop_in_checkpoint)
    controller.start(document, "live", compiled["plan_hash"], "stop-after-natural-proof")
    result = wait_terminal(controller)
    assert injected
    assert result["state"] != "completed", result
    assert {frame[0] for frame in serial.writes if frame[1] == 0xFE} == {1, 2}
    assert len([frame for frame in serial.writes if frame[1] == 0xFD]) == 1
    assert serial.collisions == []
    if stop_fails:
        assert result["state"] == "error"
        assert result["stop_confirmed"] is False
        assert result["recovery_required"] is True
        assert controller._owns_motion is True
        assert controller.is_busy()
    else:
        assert result["stop_confirmed"] is True
        assert not result["recovery_required"]
    if stage == "park_event":
        assert not checkpoints
    else:
        assert checkpoints == [None]


@pytest.mark.parametrize("failure", ["drift", "global_stop"])
def test_live_cleanup_cannot_use_stale_natural_proof_and_falls_back_to_stop(tmp_path, monkeypatch, failure):
    from test_guided_runner import wait_terminal

    controller, bus, serial, record, document, compiled, checkpoints = natural_controller(tmp_path, monkeypatch)
    verify = bus.verify_natural_park
    proof_attempts = []
    def interrupted_proof(calibration, cancel_event=None):
        proof_attempts.append(True)
        if failure == "drift":
            serial.drift_outside = True
        else:
            controller.stop()
        return verify(calibration, cancel_event=cancel_event)
    monkeypatch.setattr(bus, "verify_natural_park", interrupted_proof)
    controller.start(document, "live", compiled["plan_hash"], "natural-proof-rejected")
    result = wait_terminal(controller)
    assert proof_attempts
    assert result["state"] != "completed", result
    assert result["stop_confirmed"] is True
    assert {frame[0] for frame in serial.writes if frame[1] == 0xFE} == {1, 2}
    assert not checkpoints
    assert serial.collisions == []


@pytest.mark.parametrize("stage", ["after_completed", "run_finished_event"])
def test_stop_after_completed_natural_run_reclaims_and_stops_motion(tmp_path, monkeypatch, stage):
    from test_guided_runner import wait_terminal

    controller, bus, serial, record, document, compiled, checkpoints = natural_controller(tmp_path, monkeypatch)
    requested = []
    if stage == "run_finished_event":
        emit = controller._event
        def stop_at_run_finished(kind, **fields):
            emit(kind, **fields)
            if kind == "run_finished" and fields.get("state") == "completed" and not requested:
                requested.append(True)
                controller.stop()
        monkeypatch.setattr(controller, "_event", stop_at_run_finished)
    controller.start(document, "live", compiled["plan_hash"], "stop-completed-natural-run")
    if stage == "after_completed":
        completed = wait_terminal(controller)
        assert completed["state"] == "completed" and completed["stop_confirmed"] is True
        assert not any(frame[1] == 0xFE for frame in serial.writes)
        requested.append(True)
        controller.stop()
    result = wait_terminal(controller)
    assert requested
    assert result["state"] == "stopped", result
    assert result["stop_confirmed"] is True
    assert {frame[0] for frame in serial.writes if frame[1] == 0xFE} == {1, 2}
    assert not result["recovery_required"]
    assert serial.collisions == []
