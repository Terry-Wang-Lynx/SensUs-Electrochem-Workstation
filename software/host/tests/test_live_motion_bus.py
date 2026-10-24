import copy
import threading
import time

import pytest

from pa_host.emm_motion import MotionError
import test_powered_manual_driver as powered
from test_emm_motion import config_reply, signed


class LiveMotionSerial(powered.PoweredManualSerial):
    def __init__(self, **options):
        super().__init__(**options)
        self.active_address = None
        self.destination = None
        self.remaining_polls = 0
        self.polls = 0
        self.collisions = []
        self.after_quiesce = None
        self.after_progress = None
        self.on_request = None
        self.finite_settling_address = None
        self.finite_reached_at = None
        self.finite_reached_count = None
        self.finite_zero_samples = 0
        self.finite_target_proof = set()
        self.finite_quiet_records = []

    def _finish_finite_quiet(self, address):
        if (self.finite_settling_address != address or self.finite_reached_at is None
                or self.finite_zero_samples < 2 or self.finite_target_proof != {0x33, 0x34}
                or time.monotonic() - self.finite_reached_at < 0.2
                or self.speeds[address] != 0 or not self.statuses[address] & 2
                or abs(self.positions[address] - self.finite_reached_count) > 1):
            return
        self.finite_quiet_records.append({"address": address, "index": len(self.writes) - 1,
                                          "quiet_s": time.monotonic() - self.finite_reached_at,
                                          "zero_samples": self.finite_zero_samples,
                                          "targets": set(self.finite_target_proof)})
        self.finite_settling_address = None
        if self.after_quiesce is not None:
            callback, self.after_quiesce = self.after_quiesce, None
            callback(self)

    def write(self, frame):
        address, opcode = frame[:2]
        if self.on_request is not None:
            self.on_request(self, frame)
        if opcode == 0xFD:
            assert frame[10:] == b"\x02\x00\x6b"
            self.writes.append(frame)
            delta = round(int.from_bytes(frame[6:10], "big") * 65536 / 3200)
            self.destination = self.positions[address] + delta * (-1 if frame[2] else 1)
            self.active_address = address
            self.finite_settling_address = None
            self.finite_reached_at = None
            self.finite_reached_count = None
            self.finite_zero_samples = 0
            self.finite_target_proof = set()
            self.remaining_polls = 4
            self.statuses[address] = 1
            self.speeds[address] = 15
            self.buffer.extend(bytes([address, opcode, 2, 0x6B]))
            return len(frame)
        active = self.active_address or self.finite_settling_address
        if address == active and opcode in (0x36, 0x43):
            self.polls += 1
            if self.remaining_polls > 0:
                self.remaining_polls -= 1
                self.positions[address] += round((self.destination - self.positions[address]) / (self.remaining_polls + 1))
                if self.remaining_polls == 0:
                    self.speeds[address] = 0
                    self.statuses[address] = 3
                    for targets in self.targets.values():
                        targets[address] = self.destination
            if self.after_progress is not None:
                self.after_progress(self)
            if self.remaining_polls == 0:
                self.active_address = None
                self.finite_settling_address = address
                if self.finite_reached_at is None:
                    self.finite_reached_at = time.monotonic()
                    self.finite_reached_count = self.positions[address]
                self.finite_zero_samples += 1
                if opcode == 0x43:
                    self.finite_target_proof.update((0x33, 0x34))
        if address == self.finite_settling_address and opcode in (0x33, 0x34):
            self.finite_target_proof.add(opcode)
        if address == active and opcode == 0xFE:
            self.active_address = None
            self.finite_settling_address = None
            if self.after_quiesce is not None:
                callback, self.after_quiesce = self.after_quiesce, None
                callback(self)
        if opcode == 0x43:
            assert frame == bytes([address, opcode, 0x7A, 0x6B])
            self.writes.append(frame)
            self.buffer.extend(bytes([address, opcode, 31, 9]) + (24000).to_bytes(2, "big")
                               + (3).to_bytes(2, "big") + (self.positions[address] & 0xFFFF).to_bytes(2, "big")
                               + signed(self.targets[0x33][address], 4) + signed(self.speeds[address], 2)
                               + signed(self.positions[address], 4)
                               + signed(self.targets[0x34][address] - self.positions[address], 4)
                               + bytes([3, self.statuses[address], 0x6B]))
            result = len(frame)
        else:
            result = super().write(frame)
        if address == active and opcode in (0x36, 0x43):
            self._finish_finite_quiet(address)
        if active is not None and address != active:
            collision = bytes([active, 0, 0xEE, 0x6B])
            self.collisions.append((frame, collision))
            self.buffer.extend(collision)
        return result


def live_bus(tmp_path, monkeypatch):
    monkeypatch.setattr(powered, "PoweredManualSerial", LiveMotionSerial)
    bus, serial, events, record = powered.calibrated_powered_bus(tmp_path)
    serial.positions[2] = -2 * 32768
    for targets in serial.targets.values():
        targets.update(serial.positions)
    bus.install_experiment_calibration(record)
    serial.writes.clear()
    return bus, serial, events, record


@pytest.mark.parametrize("phase", ["in_flight", "first_zero", "quiet_without_targets", "verified_quiet"])
def test_serial_fake_rejects_peer_reads_before_proved_finite_quiet(monkeypatch, phase):
    serial = LiveMotionSerial(timeout=0.2, write_timeout=0.2)
    clock = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    serial.write(bytes.fromhex("01fd00000f0a0000000a02006b"))
    if phase != "in_flight":
        for _sample in range(4):
            serial.write(bytes.fromhex("01366b"))
        assert serial.active_address is None
        assert serial.finite_settling_address == 1
        assert serial.speeds[1] == 0 and serial.statuses[1] & 2
    if phase in ("quiet_without_targets", "verified_quiet"):
        clock[0] = 0.25
        if phase == "verified_quiet":
            serial.write(bytes.fromhex("01336b"))
            serial.write(bytes.fromhex("01346b"))
        serial.write(bytes.fromhex("01366b"))
    serial.write(bytes.fromhex("021f6b"))
    if phase == "verified_quiet":
        assert serial.collisions == []
        assert serial.finite_quiet_records[-1]["quiet_s"] == 0.25
    else:
        assert len(serial.collisions) == 1
        assert serial.finite_quiet_records == []


@pytest.mark.parametrize("axis,direction", [("X", 1), ("X", -1), ("Z", 1), ("Z", -1)])
def test_calibrated_move_keeps_peer_off_bus_until_verified_natural_completion(tmp_path, monkeypatch, axis, direction):
    bus, serial, events, record = live_bus(tmp_path, monkeypatch)
    initial = bus.snapshot()["axes"][axis]["position_mm"]
    result = bus.move_to(axis, initial + direction, 0.5)
    assert abs(result["axes"][axis]["position_mm"] - initial - direction) < 0.005
    assert serial.polls >= 5
    assert serial.collisions == []
    move_index = next(index for index, frame in enumerate(serial.writes) if frame[1] == 0xFD)
    quiet = serial.finite_quiet_records[-1]
    quiet_index = quiet["index"]
    assert quiet["quiet_s"] >= 0.2 and quiet["zero_samples"] >= 2
    assert quiet["targets"] == {0x33, 0x34}
    assert not any(frame[1] == 0xFE for frame in serial.writes)
    address = bus.axes[axis]["address"]
    assert all(frame[0] == address for frame in serial.writes[move_index:quiet_index + 1])
    for address in (1, 2):
        for opcode in (0x1F, 0x42, 0x1A):
            assert any(frame[:2] == bytes([address, opcode]) for frame in serial.writes[:move_index])
            assert any(frame[:2] == bytes([address, opcode]) for frame in serial.writes[quiet_index + 1:])
    assert events[-1]["kind"] == "arrived"


@pytest.mark.parametrize("damage", ["held_drift", "post_profile", "range_exit"])
def test_calibrated_move_faults_still_stop_both_axes(tmp_path, monkeypatch, damage):
    bus, serial, events, record = live_bus(tmp_path, monkeypatch)
    if damage == "held_drift":
        serial.after_quiesce = lambda device: device.positions.__setitem__(2, device.positions[2] + 1000)
    elif damage == "post_profile":
        def change_profile(device):
            response = bytearray(config_reply(2))
            response[9] = 32
            device.overrides[2, 0x42] = bytes(response)
        serial.after_quiesce = change_profile
    else:
        serial.after_progress = lambda device: device.positions.__setitem__(1, -100 * 32768)
    with pytest.raises(MotionError) as caught:
        bus.move_to("X", 42, 0.5)
    assert caught.value.code == "calibration_motion"
    assert serial.collisions == []
    assert {frame[0] for frame in serial.writes if frame[1] == 0xFE} == {1, 2}
    assert not any(event["kind"] == "arrived" for event in events)


@pytest.mark.parametrize("axis", ["X", "Z"])
def test_cancel_stops_active_axis_before_peer_access(tmp_path, monkeypatch, axis):
    bus, serial, events, record = live_bus(tmp_path, monkeypatch)
    cancelled = threading.Event()
    serial.after_progress = lambda device: cancelled.set()
    address = bus.axes[axis]["address"]
    initial = bus.snapshot()["axes"][axis]["position_mm"]
    with pytest.raises(MotionError) as caught:
        bus.move_to(axis, initial + 1, 0.5, cancel_event=cancelled)
    assert caught.value.code == "cancelled"
    assert serial.collisions == []
    move_index = next(index for index, frame in enumerate(serial.writes) if frame[1] == 0xFD)
    peer_index = next(index for index, frame in enumerate(serial.writes) if index > move_index and frame[0] != address)
    assert any(frame[:2] == bytes([address, 0xFE]) for frame in serial.writes[move_index:peer_index])


def test_active_feedback_guard_accepts_honestly_aging_held_axis(tmp_path, monkeypatch):
    bus, serial, events, record = live_bus(tmp_path, monkeypatch)
    bus._timeout = 1.0
    real_monotonic = time.monotonic
    elapsed = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: real_monotonic() + elapsed[0])
    serial.after_progress = lambda device: elapsed.__setitem__(0, elapsed[0] + 0.6)
    initial = bus.snapshot()["axes"]["X"]["position_mm"]
    result = bus.move_to("X", initial + 1, 0.5)
    assert elapsed[0] > 2
    assert abs(result["axes"]["X"]["position_mm"] - initial - 1) < 0.005
    assert serial.collisions == []


def test_final_active_sample_detects_drift_during_last_peer_sample(tmp_path, monkeypatch):
    bus, serial, events, record = live_bus(tmp_path, monkeypatch)
    quiesced = []
    peer_samples = []
    serial.after_quiesce = lambda device: quiesced.append(True)
    def drift_after_active_sample(device, frame):
        if quiesced and frame[:2] == b"\x02\x36":
            peer_samples.append(True)
            if len(peer_samples) == 2:
                device.positions[1] += 1000
    serial.on_request = drift_after_active_sample
    with pytest.raises(MotionError) as caught:
        bus.move_to("X", 42, 0.5)
    assert caught.value.code == "calibration_motion"
    assert len(peer_samples) >= 2
    assert serial.collisions == []
    assert not any(event["kind"] == "arrived" for event in events)
    assert {frame[0] for frame in serial.writes if frame[1] == 0xFE} == {1, 2}


@pytest.mark.parametrize("axis", ["X", "Z"])
def test_calibrated_move_uses_verified_combined_telemetry(tmp_path, monkeypatch, axis):
    bus, serial, events, record = live_bus(tmp_path, monkeypatch)
    bus.enable_responsive_keyboard()
    serial.writes.clear()
    initial = bus.snapshot()["axes"][axis]["position_mm"]
    result = bus.move_to(axis, initial + 1, 0.5)
    assert abs(result["axes"][axis]["position_mm"] - initial - 1) < 0.005
    assert serial.polls >= 5
    assert serial.collisions == []
    move_index = next(index for index, frame in enumerate(serial.writes) if frame[1] == 0xFD)
    quiet = serial.finite_quiet_records[-1]
    assert quiet["quiet_s"] >= 0.2 and quiet["zero_samples"] >= 2
    assert quiet["targets"] == {0x33, 0x34}
    assert not any(frame[1] == 0xFE for frame in serial.writes)
    active_frames = serial.writes[move_index + 1:quiet["index"] + 1]
    assert all(frame[0] == bus.axes[axis]["address"] for frame in active_frames)
    assert all(frame[1] in (0x43, 0x33, 0x34) for frame in active_frames)


@pytest.mark.parametrize("wire_fault", [False, True])
def test_actual_controller_runs_calibrated_movement_and_wait_without_ble(tmp_path, monkeypatch, wire_fault):
    from pa_host.automation import AutomationController, compile_experiment, default_experiment
    from test_guided_runner import wait_terminal

    bus, serial, events, record = live_bus(tmp_path / "calibration", monkeypatch)
    controller = AutomationController(tmp_path / "controller", tmp_path / "runs", tmp_path)
    document = default_experiment()
    document.update(schema_version="1.1", run_policy="guided", calibration_id=record["id"])
    document["bindings"]["motion"] = copy.deepcopy(record["motion"])
    document["defaults"].update(x_first_mm=record["x_first_mm"], column_pitch_mm=9,
                                z_safe_mm=record["z_safe_mm"], z_immersed_mm=record["z_working_mm"], settle_s=0)
    document["safety"].update(motion_policy_confirmed=True, electrical_policy_confirmed=True)
    document["program"] = [{"op": "move_to_column", "column": 6}, {"op": "immerse"},
                            {"op": "wait", "seconds": 0.02}, {"op": "withdraw"}]
    controller._motion = bus
    controller._motion_config = copy.deepcopy(record["motion"])
    controller._motion_calibration_validator = lambda compiled: copy.deepcopy(record)
    if wire_fault:
        serial.positions[2] = 0
        for targets in serial.targets.values():
            targets[2] = 0
        movement_event_index = []
        def remember_position_command(device, frame):
            if frame[1] == 0xFD and not movement_event_index:
                movement_event_index.append(len(events))
        serial.on_request = remember_position_command
        def append_active_error(device):
            address = device.active_address
            device.overrides[address, 0x36] = lambda frame: (
                bytes([address, 0x36]) + signed(device.positions[address], 4) + b"\x6b"
                + bytes([address, 0, 0xEE, 0x6B]))
        serial.after_progress = append_active_error
        serial.after_quiesce = lambda device: device.overrides.clear()
    compiled = compile_experiment(document)
    controller.start(document, "live", compiled["plan_hash"], "actual-live-motion")
    controller._thread.join(10)
    result = wait_terminal(controller)
    assert serial.collisions == []
    assert result["stop_confirmed"] is True
    if wire_fault:
        assert result["state"] == "error", result
        failure = result["error"]
        assert failure["code"] == "execution_failed"
        assert any("\u4e00" <= character <= "\u9fff" for character in failure["message"])
        assert failure["cause"]["code"] == "response_length"
        assert failure["cause"]["message"] == "Unexpected serial response length"
        wire = failure["cause"]["wire"]
        assert wire["address"] == 1 and wire["opcode"] == 0x36
        assert wire["expected_length"] == 8
        assert wire["response_hex"].startswith("0136")
        assert wire["pending_hex"] == "0100ee6b"
        assert len([frame for frame in serial.writes if frame[1] == 0xFD]) == 1
        assert {frame[0] for frame in serial.writes if frame[1] == 0xFE} == {1, 2}
        assert movement_event_index
        assert not any(event["kind"] == "arrived" for event in events[movement_event_index[0]:])
    else:
        assert result["state"] == "completed", result
        assert len([frame for frame in serial.writes if frame[1] == 0xFD]) == 6
        assert len([event for event in events if event["kind"] == "arrived"]) == 4
        assert len([event for event in events if event["kind"] == "endpoint_approach_stage_arrived"]) == 2
        assert not any(frame[1] == 0xFE for frame in serial.writes)


@pytest.mark.parametrize("stage", ["quiesce", "postflight"])
def test_cancellation_during_final_verification_never_arrives(tmp_path, monkeypatch, stage):
    bus, serial, events, record = live_bus(tmp_path, monkeypatch)
    cancelled = threading.Event()
    quiesced = []
    def on_quiesce(device):
        quiesced.append(True)
        if stage == "quiesce":
            cancelled.set()
    serial.after_quiesce = on_quiesce
    def cancel_postflight(device, frame):
        if stage == "postflight" and quiesced and frame[:2] == b"\x02\x42":
            cancelled.set()
    serial.on_request = cancel_postflight
    with pytest.raises(MotionError) as caught:
        bus.move_to("X", 42, 0.5, cancel_event=cancelled)
    assert caught.value.code == "cancelled"
    assert quiesced
    assert serial.collisions == []
    assert not any(event["kind"] == "arrived" for event in events)
    assert {frame[0] for frame in serial.writes if frame[1] == 0xFE} == {1, 2}


def test_calibrated_move_with_installed_absolute_limits_keeps_active_bus_exclusive(tmp_path, monkeypatch):
    from pa_host.motion_limits import AbsoluteMotionLimits

    bus, serial, events, record = live_bus(tmp_path / "calibration", monkeypatch)
    limits = AbsoluteMotionLimits(tmp_path / "limits")
    initial = dict(serial.positions)
    limits.begin(bus)
    for boundary, address, count in (("x_left", 1, 120 * 32768), ("x_right", 1, -32768),
                                     ("z_up", 2, -10 * 32768), ("z_down", 2, 32768)):
        serial.positions[address] = count
        limits.capture(bus, boundary)
    serial.positions.update(initial)
    limits.save(bus, clearance_confirmed=True)
    assert bus.snapshot()["absolute_limits"] is not None
    serial.writes.clear()
    result = bus.move_to("X", 42, 0.5)
    assert abs(result["axes"]["X"]["position_mm"] - 42) < 0.005
    assert serial.collisions == []


def test_external_global_stop_quiesces_active_z_before_peer(tmp_path, monkeypatch):
    bus, serial, events, record = live_bus(tmp_path, monkeypatch)
    position_sent = threading.Event()
    resume_move = threading.Event()
    original_transact = bus._transact
    failures = []
    def pause_after_position_ack(body, *arguments, **options):
        response = original_transact(body, *arguments, **options)
        if body[1] == 0xFD:
            position_sent.set()
            assert resume_move.wait(3), "STOP test did not release movement worker"
        return response
    monkeypatch.setattr(bus, "_transact", pause_after_position_ack)
    def move():
        try:
            bus.move_to("Z", 4, 0.5)
        except MotionError as error:
            failures.append(error)
    worker = threading.Thread(target=move)
    worker.start()
    try:
        assert position_sent.wait(3), "Movement worker did not send FD"
        assert serial.active_address == 2
        stopped = bus.stop_all()
        stop_frames = [frame for frame in serial.writes if frame[1] == 0xFE]
        assert stop_frames[0][0] == 2
        assert stopped["stop_confirmed"] is True
        assert all(state["stop_confirmed"] is True for state in stopped["axes"].values())
        assert serial.collisions == []
    finally:
        resume_move.set()
        worker.join(3)
    assert not worker.is_alive()
    assert len(failures) == 1 and failures[0].code == "cancelled"
    assert len([frame for frame in serial.writes if frame[1] == 0xFD]) == 1
    assert not any(event["kind"] == "arrived" for event in events)
