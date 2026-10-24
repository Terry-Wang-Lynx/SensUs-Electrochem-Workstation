from __future__ import annotations

import copy
import time

import pytest

from pa_host.emm_motion import MotionError, SharedEmmBus
from test_emm_motion import AXES, FakeSerial, MotionTestClock, config_reply, signed


class PoweredManualSerial(FakeSerial):
    def __init__(self, **options):
        super().__init__(**options)
        self.targets = {opcode: copy.deepcopy(self.positions) for opcode in (0x33, 0x34)}
        self.on_enable = None

    def write(self, frame):
        address, opcode = frame[:2]
        if (address, opcode) in self.overrides:
            return super().write(frame)
        if opcode in (0x33, 0x34):
            self.writes.append(frame)
            self.buffer.extend(bytes([address, opcode]) + signed(self.targets[opcode][address], 4) + b"\x6b")
            return len(frame)
        if opcode == 0xF3 and frame[3] == 0:
            self.writes.append(frame)
            self.statuses[address] &= ~1
            self.buffer.extend(bytes([address, opcode, 2, 0x6B]))
            return len(frame)
        if opcode == 0xFE and not self.statuses[address] & 1:
            self.writes.append(frame)
            self.buffer.extend(bytes([address, opcode, 0xE2, 0x6B]))
            return len(frame)
        result = super().write(frame)
        if opcode == 0xFD and self.move_completes:
            for targets in self.targets.values():
                targets.update(self.positions)
        if opcode == 0xF3 and frame[3] == 1 and self.on_enable is not None:
            self.on_enable(address)
        return result


def powered_bus(*, enabled=False):
    serial = PoweredManualSerial(timeout=0.2, write_timeout=0.2)
    if enabled:
        serial.statuses = {1: 3, 2: 3}
    events = []
    bus = SharedEmmBus("fake-rs485", 115200, copy.deepcopy(AXES), serial_factory=lambda **options: serial,
                       event_callback=events.append, transaction_timeout_s=0.2)
    bus._command_gap_s = 0
    bus.connect()
    serial.writes.clear()
    return bus, serial, events


def test_disabled_e2_close_confirms_stationary_feedback_without_inventing_ack():
    bus, serial, events = powered_bus()
    result = bus.close()
    assert result["stop_confirmed"] is True
    assert result["errors"] == []
    assert serial.closed and not bus.snapshot()["faulted"]
    for axis in ("X", "Z"):
        assert result["axes"][axis]["stop_acknowledged"] is False
        assert result["axes"][axis]["stop_reply"] == "e2"
        assert result["axes"][axis]["stop_basis"] == "disabled_feedback"
    assert events[-1]["kind"] == "stopped"
    assert {frame[1] for frame in serial.writes} == {0xFE, 0x36, 0x35, 0x3A}


@pytest.mark.parametrize("failure", ["enabled", "moving", "drifting", "fault", "missing_speed", "bad_sign", "other_rejection"])
def test_disabled_e2_never_substitutes_for_uncertain_or_unhealthy_stop(failure):
    bus, serial, _ = powered_bus()
    serial.overrides[1, 0xFE] = bytes.fromhex("01fee26b")
    if failure == "enabled":
        serial.statuses[1] = 3
    elif failure == "moving":
        serial.speeds[1] = 1
    elif failure == "drifting":
        def drifting(frame):
            serial.positions[1] += 10
            return bytes([1, 0x36]) + signed(serial.positions[1], 4) + b"\x6b"
        serial.overrides[1, 0x36] = drifting
    elif failure == "fault":
        serial.statuses[1] = 6
    elif failure == "missing_speed":
        serial.overrides[1, 0x35] = b""
    elif failure == "bad_sign":
        serial.overrides[1, 0x36] = bytes.fromhex("013602000000006b")
    else:
        serial.overrides[1, 0xFE] = bytes.fromhex("01feee6b")
    result = bus.stop_all()
    assert result["stop_confirmed"] is False
    assert result["axes"]["X"]["stop_confirmed"] is False
    assert result["axes"]["Z"]["stop_confirmed"] is True
    assert bus.snapshot()["faulted"]
    assert [frame[0] for frame in serial.writes if frame[1] == 0xFE] == [1, 2]


def test_disabled_e2_rejects_cached_samples_without_new_reads(monkeypatch):
    bus, serial, _ = powered_bus()
    cached = bus.snapshot()["axes"]
    monkeypatch.setattr(bus, "_read_axis", lambda axis, **options: copy.deepcopy(cached[axis]))
    result = bus.stop_all()
    assert result["stop_confirmed"] is False
    assert result["errors"]


@pytest.mark.parametrize("release_confirmed,z_supported", [(False, True), (True, False), (1, True), (True, 1), ("true", True), (True, None)])
def test_real_manual_capture_requires_literal_release_and_z_support_confirmations(release_confirmed, z_supported):
    bus, serial, _ = powered_bus(enabled=True)
    with pytest.raises(MotionError) as caught:
        bus.begin_manual_calibration(release_confirmed=release_confirmed, z_supported=z_supported)
    assert caught.value.code == "manual_release_confirmation"
    assert serial.writes == []
    assert bus.snapshot()["manual"]["active"] is False


def test_powered_manual_begin_stops_before_release_and_capture_binds_live_identity():
    bus, serial, events = powered_bus(enabled=True)
    started = bus.begin_manual_calibration(release_confirmed=True, z_supported=True)
    disables = [frame for frame in serial.writes if frame[1] == 0xF3]
    assert disables == [bytes.fromhex("01f3ab00006b"), bytes.fromhex("02f3ab00006b")]
    assert [frame[0] for frame in serial.writes[:2]] == [1, 2]
    assert all(frame[1] == 0xFE for frame in serial.writes[:2])
    assert started["connection_token"] == bus.snapshot()["connection_token"]
    assert started["profiles"]["X"]["config"]["position_window_deg"] == 0.8
    assert started["motion"] == {"port": "fake-rs485", "baud": 115200, "axes": AXES}
    assert bus.snapshot()["manual"]["capture_supported"] is True
    assert bus.snapshot()["manual"]["supported"] is False
    serial.positions[1] += 32768
    serial.writes.clear()
    captured = bus.capture_manual_position()
    assert captured["raw_counts"] == {"X": 98304, "Z": -32768}
    assert captured["profiles"] == started["profiles"]
    assert captured["connection_token"] == started["connection_token"]
    assert {frame[1] for frame in serial.writes} <= {0x1F, 0x42, 0x1A, 0x36, 0x35, 0x3A}
    assert events[-1]["kind"] == "manual_captured"
    for operation in (lambda: bus.enable("X"), lambda: bus.confirm_reference(), lambda: bus.move_to("X", 1, 0.5)):
        with pytest.raises(MotionError) as caught:
            operation()
        assert caught.value.code == "manual_calibration_active"


def test_manual_begin_does_not_release_when_stop_is_unknown():
    bus, serial, _ = powered_bus(enabled=True)
    serial.overrides[1, 0xFE] = bytes.fromhex("01fee26b")
    with pytest.raises(MotionError) as caught:
        bus.begin_manual_calibration(release_confirmed=True, z_supported=True)
    assert caught.value.code == "stop_unconfirmed"
    assert caught.value.stop_result["stop_confirmed"] is False
    assert not any(frame[1] == 0xF3 for frame in serial.writes)


def test_partial_release_fault_retains_manual_ownership_and_never_reenables():
    bus, serial, _ = powered_bus(enabled=True)
    serial.overrides[2, 0xF3] = bytes.fromhex("02f3e26b")
    with pytest.raises(MotionError):
        bus.begin_manual_calibration(release_confirmed=True, z_supported=True)
    snapshot = bus.snapshot()
    assert snapshot["manual"]["active"] is True
    assert snapshot["manual"]["status"] == "blocked"
    assert snapshot["faulted"] is True
    assert serial.statuses[1] == 2 and serial.statuses[2] == 3
    assert not any(frame[1] == 0xF3 and frame[3] == 1 for frame in serial.writes)
    assert [frame[0] for frame in serial.writes if frame[1] == 0xFE][-2:] == [1, 2]


def test_capture_rejects_profile_change_without_releasing_manual_ownership():
    bus, serial, _ = powered_bus()
    bus.begin_manual_calibration(release_confirmed=True, z_supported=True)
    serial.overrides[1, 0x1F] = bytes.fromhex("011f006c03016b")
    with pytest.raises(MotionError) as caught:
        bus.capture_manual_position()
    assert caught.value.code == "manual_profile_changed"
    assert bus.snapshot()["manual"]["active"] is True
    assert bus.snapshot()["manual"]["status"] == "blocked"


@pytest.mark.parametrize("operation", ["finish", "cancel"])
def test_relock_checks_both_target_registers_twice_before_enable(operation):
    bus, serial, events = powered_bus()
    bus.begin_manual_calibration(release_confirmed=True, z_supported=True)
    serial.positions[1] += 32768
    for targets in serial.targets.values():
        targets.update(serial.positions)
    serial.writes.clear()
    if operation == "finish":
        result = bus.finish_manual_calibration({"X": 65536, "Z": -32768})
        assert result["raw_counts"] == {"X": 98304, "Z": -32768}
        assert bus.snapshot()["position"] == {"X": 1.0, "Z": 0.0}
        assert bus.snapshot()["referenced"] is True
    else:
        bus.cancel_manual_calibration()
        assert bus.snapshot()["referenced"] is False
    enable_index = next(index for index, frame in enumerate(serial.writes) if frame[1] == 0xF3)
    before_enable = serial.writes[:enable_index]
    for address in (1, 2):
        for opcode in (0x33, 0x34):
            assert sum(frame[:2] == bytes([address, opcode]) for frame in before_enable) == 2
    assert [frame for frame in serial.writes if frame[1] == 0xF3] == [
        bytes.fromhex("01f3ab01006b"), bytes.fromhex("02f3ab01006b")]
    assert not any(frame[1] in (0xFD, 0x0A, 0x93, 0x9A) for frame in serial.writes)
    assert bus.snapshot()["manual"]["active"] is False
    assert serial.statuses == {1: 3, 2: 3}
    guarded = next(event for event in events if event["kind"] == "manual_relock_guard")
    assert guarded["target_samples"][0]["X"] == {"target_count": 98304, "instantaneous_target_count": 98304}
    assert guarded["tolerance_counts"]["X"] == pytest.approx(145.63555555555556)


@pytest.mark.parametrize("address,opcode", [(1, 0x33), (1, 0x34), (2, 0x33), (2, 0x34)])
def test_any_misaligned_target_blocks_all_enable_commands(address, opcode):
    bus, serial, _ = powered_bus()
    bus.begin_manual_calibration(release_confirmed=True, z_supported=True)
    serial.targets[opcode][address] += 200
    serial.writes.clear()
    with pytest.raises(MotionError) as caught:
        bus.finish_manual_calibration({"X": 65536, "Z": -32768})
    assert caught.value.code == "manual_target_mismatch"
    assert not any(frame[1] == 0xF3 for frame in serial.writes)
    assert bus.snapshot()["manual"]["active"] is True
    assert bus.snapshot()["manual"]["status"] == "blocked"


@pytest.mark.parametrize("response", [
    b"", bytes.fromhex("0133e26b"), bytes.fromhex("023300000100006b"),
    bytes.fromhex("013400000100006b"), bytes.fromhex("013302000100006b"),
    bytes.fromhex("0133000001000000"), bytes.fromhex("013300000100006b00"),
])
def test_missing_rejected_or_malformed_target_feedback_never_enables(response):
    bus, serial, _ = powered_bus()
    bus.begin_manual_calibration(release_confirmed=True, z_supported=True)
    serial.overrides[1, 0x33] = response
    serial.writes.clear()
    with pytest.raises(MotionError) as caught:
        bus.cancel_manual_calibration()
    assert caught.value.code == "manual_target_unverified"
    assert not any(frame[1] == 0xF3 for frame in serial.writes)
    assert bus.snapshot()["manual"]["active"] is True


def test_second_target_sample_is_required_and_can_block_relock():
    bus, serial, _ = powered_bus()
    bus.begin_manual_calibration(release_confirmed=True, z_supported=True)
    replies = iter([bytes.fromhex("013300000100006b"), bytes.fromhex("013300000200006b")])
    serial.overrides[1, 0x33] = lambda frame: next(replies)
    serial.writes.clear()
    with pytest.raises(MotionError) as caught:
        bus.cancel_manual_calibration()
    assert caught.value.code == "manual_target_mismatch"
    assert not any(frame[1] == 0xF3 for frame in serial.writes)


@pytest.mark.parametrize("failure", ["jump", "enable_rejected", "enable_unconfirmed", "moving"])
def test_relock_failure_stops_both_axes_and_preserves_blocked_ownership(failure):
    bus, serial, _ = powered_bus()
    bus.begin_manual_calibration(release_confirmed=True, z_supported=True)
    if failure == "jump":
        serial.on_enable = lambda address: serial.positions.update({address: serial.positions[address] + 200})
    elif failure == "enable_rejected":
        serial.overrides[1, 0xF3] = bytes.fromhex("01f3e26b")
    elif failure == "enable_unconfirmed":
        serial.overrides[1, 0xF3] = bytes.fromhex("01f3026b")
    else:
        serial.on_enable = lambda address: serial.speeds.update({address: 1})
    serial.writes.clear()
    with pytest.raises(MotionError) as caught:
        bus.finish_manual_calibration({"X": 65536, "Z": -32768})
    assert caught.value.stop_result is not None
    assert [frame[0] for frame in serial.writes if frame[1] == 0xFE] == [1, 2]
    assert bus.snapshot()["manual"]["active"] is True
    assert bus.snapshot()["manual"]["status"] == "blocked"
    assert bus.snapshot()["referenced"] is False
    assert not any(frame[0] == 2 and frame[1] == 0xF3 for frame in serial.writes)


@pytest.mark.parametrize("offset,allowed", [(145, True), (146, False), (-145, True), (-146, False)])
def test_relock_target_window_uses_verified_arrival_angle_without_rounding_up(offset, allowed):
    bus, serial, _ = powered_bus()
    bus.begin_manual_calibration(release_confirmed=True, z_supported=True)
    serial.targets[0x33][1] += offset
    serial.writes.clear()
    if allowed:
        assert bus.finish_manual_calibration({"X": 65536, "Z": -32768})["raw_counts"]["X"] == 65536
        assert bus.snapshot()["manual"]["active"] is False
    else:
        with pytest.raises(MotionError) as caught:
            bus.finish_manual_calibration({"X": 65536, "Z": -32768})
        assert caught.value.code == "manual_target_mismatch"
        assert not any(frame[1] == 0xF3 for frame in serial.writes)


def test_invalid_reference_is_rejected_before_any_relock_enable():
    bus, serial, _ = powered_bus()
    bus.begin_manual_calibration(release_confirmed=True, z_supported=True)
    serial.writes.clear()
    with pytest.raises(MotionError) as caught:
        bus.finish_manual_calibration({"X": 65537, "Z": -32768})
    assert caught.value.code == "travel_limit"
    assert not any(frame[1] == 0xF3 for frame in serial.writes)


def test_stop_at_relock_guard_cannot_be_cleared_or_followed_by_enable():
    bus, serial, _ = powered_bus()
    bus.begin_manual_calibration(release_confirmed=True, z_supported=True)
    serial.writes.clear()

    def stop_at_guard(event):
        if event["kind"] == "manual_relock_guard":
            bus.stop_all()

    bus._callback = stop_at_guard
    with pytest.raises(MotionError) as caught:
        bus.cancel_manual_calibration()
    assert caught.value.code == "cancelled"
    assert not any(frame[1] == 0xF3 for frame in serial.writes)
    assert bus._stop_event.is_set()
    assert bus.snapshot()["manual"]["active"] is True
    assert bus.snapshot()["manual"]["status"] == "blocked"


def test_stop_during_begin_profile_reads_prevents_release(monkeypatch):
    bus, serial, _ = powered_bus(enabled=True)
    identity = bus._identity

    def stopped_identity(axis):
        identity(axis)
        if axis == "Z":
            bus.stop_all()

    monkeypatch.setattr(bus, "_identity", stopped_identity)
    with pytest.raises(MotionError) as caught:
        bus.begin_manual_calibration(release_confirmed=True, z_supported=True)
    assert caught.value.code == "cancelled"
    assert not any(frame[1] == 0xF3 for frame in serial.writes)
    assert bus._stop_event.is_set()


@pytest.mark.parametrize("change", ["profile", "power", "window"])
def test_relock_refreshes_profile_and_power_identity_before_target_checks(change):
    bus, serial, _ = powered_bus()
    bus.begin_manual_calibration(release_confirmed=True, z_supported=True)
    if change == "profile":
        serial.overrides[1, 0x1F] = bytes.fromhex("011f006c03016b")
    elif change == "power":
        serial.statuses[1] |= 0x80
    else:
        from test_emm_motion import config_reply
        response = bytearray(config_reply(1))
        response[30:32] = bytes.fromhex("0013")
        serial.overrides[1, 0x42] = response
    serial.writes.clear()
    with pytest.raises(MotionError):
        bus.cancel_manual_calibration()
    assert not any(frame[1] in (0xF3, 0x33, 0x34) for frame in serial.writes)
    assert bus.snapshot()["manual"]["active"] is True


def test_manual_capture_rejects_cached_feedback(monkeypatch):
    bus, serial, _ = powered_bus()
    bus.begin_manual_calibration(release_confirmed=True, z_supported=True)
    cached = bus.snapshot()["axes"]
    monkeypatch.setattr(bus, "_read_axis", lambda axis, **options: copy.deepcopy(cached[axis]))
    serial.writes.clear()
    with pytest.raises(MotionError) as caught:
        bus.capture_manual_position()
    assert caught.value.code == "invalid_telemetry"
    assert not any(frame[1] == 0xF3 for frame in serial.writes)
    assert bus.snapshot()["manual"]["status"] == "blocked"


def test_manual_target_reader_rejects_non_read_opcodes_without_wire_write():
    bus, serial, _ = powered_bus()
    bus.begin_manual_calibration(release_confirmed=True, z_supported=True)
    serial.writes.clear()
    with pytest.raises(MotionError) as caught:
        bus._read_manual_target("X", 0x0A)
    assert caught.value.code == "manual_target_unverified"
    assert serial.writes == []


def calibrated_powered_bus(tmp_path):
    from pa_host.guided_automation import GuidedAutomation

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
    for targets in serial.targets.values():
        targets.update(serial.positions)
    service.finish_calibration(bus)
    record = service.trusted_calibration(bus)
    serial.writes.clear()
    return bus, serial, events, record


def test_installed_experiment_calibration_verifies_current_profiles_and_pins_its_mapping(tmp_path):
    bus, serial, _, record = calibrated_powered_bus(tmp_path)
    installed = bus.install_experiment_calibration(record)
    assert installed["experiment_calibration"]["id"] == record["id"]
    assert [(frame[0], frame[1]) for frame in serial.writes if frame[1] == 0x42] == [(1, 0x42), (2, 0x42)]
    assert not any(frame[1] in (0xFD, 0xF3, 0x0A, 0x93, 0x9A) for frame in serial.writes)
    record["x_last_mm"] = 150
    assert bus.snapshot()["experiment_calibration"]["x_last_mm"] == 100
    assert bus.install_experiment_calibration(None)["experiment_calibration"] is None


@pytest.mark.parametrize("stage", ["install", "move"])
def test_changed_microstep_after_calibration_stops_before_any_position_command(tmp_path, stage):
    bus, serial, _, record = calibrated_powered_bus(tmp_path)
    if stage == "move":
        bus.install_experiment_calibration(record)
    response = bytearray(config_reply(1))
    response[9] = 32
    serial.overrides[1, 0x42] = response
    serial.writes.clear()
    with pytest.raises(MotionError) as caught:
        if stage == "install":
            bus.install_experiment_calibration(record)
        else:
            bus.move_to("X", 55, 0.5)
    assert caught.value.code == "calibration_motion"
    assert caught.value.stop_result["stop_confirmed"] is True
    assert not any(frame[1] == 0xFD for frame in serial.writes)
    assert [frame[0] for frame in serial.writes if frame[1] == 0xFE] == [1, 2]
    assert bus.snapshot()["faulted"] is True


@pytest.mark.parametrize("damage", ["connection", "reference", "profile", "legacy", "geometry"])
def test_install_rejects_untrusted_calibration_record_before_position_commands(tmp_path, damage):
    bus, serial, _, record = calibrated_powered_bus(tmp_path)
    if damage == "connection":
        record["connection_token"] = "another-connection"
    elif damage == "reference":
        record["reference"]["reference_counts"]["X"] += 1
    elif damage == "profile":
        record["reference"]["profiles"]["X"]["config"]["microstep"] = 32
    elif damage == "legacy":
        record["points"].pop("working")
    else:
        record["x_last_mm"] = 150
    with pytest.raises(MotionError) as caught:
        bus.install_experiment_calibration(record)
    assert caught.value.code == "calibration_motion"
    assert caught.value.stop_result is not None
    assert not any(frame[1] == 0xFD for frame in serial.writes)


@pytest.mark.parametrize("damage", ["disabled", "fault", "power", "outside", "moving", "drifting", "missing_speed"])
def test_x_move_verifies_uncommanded_z_after_natural_arrival_and_rejects_bad_feedback(tmp_path, damage):
    bus, serial, _, record = calibrated_powered_bus(tmp_path)
    bus.install_experiment_calibration(record)

    def changed_z():
        if damage == "disabled":
            serial.statuses[2] = 2
        elif damage == "fault":
            serial.statuses[2] = 7
        elif damage == "power":
            serial.statuses[2] |= 0x80
        elif damage == "outside":
            serial.positions[2] = -8 * 32768
        elif damage == "moving":
            serial.speeds[2] = 1
        elif damage == "drifting":
            serial.positions[2] -= 200
        else:
            serial.overrides[2, 0x35] = b""

    serial.on_move = changed_z
    serial.writes.clear()
    with pytest.raises(MotionError) as caught:
        bus.move_to("X", 55, 0.5)
    assert caught.value.code != "move_timeout"
    assert caught.value.stop_result is not None
    stops = [frame[0] for frame in serial.writes if frame[1] == 0xFE]
    assert stops[0] == 1
    assert set(stops) == {1, 2}
    move_index = next(index for index, frame in enumerate(serial.writes) if frame[1] == 0xFD)
    first_peer_read = next(index for index, frame in enumerate(serial.writes[move_index:], move_index) if frame[0] == 2)
    assert all(frame[0] == 1 for frame in serial.writes[move_index:first_peer_read])
    assert sum(frame[:2] == bytes([1, 0x34]) for frame in serial.writes[move_index:first_peer_read]) >= 2
    assert not any(frame[1] == 0xFE for frame in serial.writes[move_index:first_peer_read])
    assert bus.snapshot()["faulted"] is True
    assert bus.snapshot()["referenced"] is False


@pytest.mark.parametrize("axis,target", [("X", 0.99999), ("X", 100.00001), ("Z", 0.99999), ("Z", 7.00001)])
def test_experiment_target_request_cannot_use_arrival_window_to_expand_calibration(tmp_path, axis, target):
    bus, serial, _, record = calibrated_powered_bus(tmp_path)
    bus.install_experiment_calibration(record)
    serial.writes.clear()
    with pytest.raises(MotionError) as caught:
        bus.move_to(axis, target, 0.5)
    assert caught.value.code == "calibration_motion"
    assert not any(frame[1] == 0xFD for frame in serial.writes)
    assert caught.value.stop_result is not None


@pytest.mark.parametrize("target,offset", [(100, 6), (1, -6)])
def test_quantized_experiment_endpoints_stay_inside_at_arbitrary_encoder_phase(tmp_path, target, offset):
    bus, serial, _, record = calibrated_powered_bus(tmp_path)
    bus.install_experiment_calibration(record)
    serial.positions[1] += offset
    result = bus.move_to("X", target, 0.5)
    quantized = result["axes"]["X"]["quantized_target_mm"]
    assert 1 <= quantized <= 100
    assert abs(quantized - target) < 0.000625
    assert result["axes"]["X"]["arrival_tolerance_mm"] == pytest.approx(2 * 0.8 / 360)


def test_experiment_stop_preserves_mapping_and_requires_reference_before_another_move(tmp_path):
    bus, serial, _, record = calibrated_powered_bus(tmp_path)
    bus.install_experiment_calibration(record)
    assert bus.stop_all()["stop_confirmed"] is True
    assert bus.snapshot()["experiment_calibration"]["id"] == record["id"]
    serial.writes.clear()
    with pytest.raises(MotionError) as caught:
        bus.move_to("X", 55, 0.5)
    assert caught.value.code == "not_referenced"
    assert not any(frame[1] == 0xFD for frame in serial.writes)


def test_stopped_calibration_can_reconfirm_existing_lock_without_enabling_again(tmp_path):
    bus, serial, _, record = calibrated_powered_bus(tmp_path)
    bus.install_experiment_calibration(record)
    parked = bus.reference_identity()
    assert bus.stop_all()["stop_confirmed"] is True
    bus.restore_reference(parked)
    assert not any(state["enable_confirmed"] for state in bus.snapshot()["axes"].values())
    serial.writes.clear()
    bus.install_experiment_calibration(record)
    assert all(state["enable_confirmed"] for state in bus.snapshot()["axes"].values())
    assert not any(frame[1] in (0xF3, 0xFD, 0x0A, 0x93, 0x9A) for frame in serial.writes)
    assert bus.move_to("X", 55, 0.5)["axes"]["X"]["position_mm"] == 55


@pytest.mark.parametrize("damage", ["disabled", "target", "missing", "moving"])
def test_stopped_calibration_cannot_reconfirm_unknown_or_misaligned_lock(tmp_path, damage):
    bus, serial, _, record = calibrated_powered_bus(tmp_path)
    parked = bus.reference_identity()
    bus.stop_all()
    bus.restore_reference(parked)
    if damage == "disabled":
        serial.statuses[2] = 2
    elif damage == "target":
        serial.targets[0x34][2] += 200
    elif damage == "missing":
        serial.overrides[2, 0x33] = b""
    else:
        serial.speeds[2] = 1
    serial.writes.clear()
    with pytest.raises(MotionError):
        bus.install_experiment_calibration(record)
    assert not any(state["enable_confirmed"] for state in bus.snapshot()["axes"].values())
    assert not any(frame[1] in (0xF3, 0xFD) for frame in serial.writes)


@pytest.mark.parametrize("host_preemption", [0, 0.3])
def test_calibrated_polling_profile_reads_respect_the_existing_move_deadline(tmp_path, host_preemption, monkeypatch):
    bus, serial, _, record = calibrated_powered_bus(tmp_path)
    bus.install_experiment_calibration(record)
    clock = MotionTestClock()
    clock.attach(monkeypatch, serial)
    monkeypatch.setattr("pa_host.calibrated_motion.time", clock)
    original_wait = bus._stop_event.wait

    def wait(timeout=None):
        clock.sleep(timeout or 0)
        return original_wait(0)

    monkeypatch.setattr(bus._stop_event, "wait", wait)
    arrived = bus.move_to("X", 54, 0.5)
    assert arrived["position"]["X"] == 54
    bus._move_timeout = 0.05
    serial.overrides[1, 0x42] = b""
    profile_deadline = clock.monotonic() + bus._move_timeout
    with pytest.raises(MotionError) as profile_failure:
        bus._read_experiment_axes(record, verify_profiles=True, deadline=profile_deadline)
    assert profile_failure.value.code == "transaction_timeout"
    assert clock.monotonic() == pytest.approx(profile_deadline, abs=0.001)
    serial.overrides.clear()

    def moved():
        serial.overrides.update({(1, 0x42): b""})
        time.sleep(host_preemption)

    serial.on_move = moved
    started = clock.monotonic()
    with pytest.raises(MotionError) as caught:
        bus.move_to("X", 55, 0.5)
    assert caught.value.code == "move_timeout"
    assert clock.monotonic() - started < 0.18
    assert caught.value.stop_result is not None
    assert caught.value.stop_result["stop_confirmed"]


def test_calibrated_moves_compare_current_mechanical_settings_to_the_trusted_record(tmp_path):
    bus, serial, _, record = calibrated_powered_bus(tmp_path)
    bus.install_experiment_calibration(record)
    bus.axes["X"]["lead_mm"] = 4
    serial.writes.clear()
    with pytest.raises(MotionError) as caught:
        bus.move_to("X", 55, 0.5)
    assert caught.value.code == "calibration_motion"
    assert not any(frame[1] == 0xFD for frame in serial.writes)
    assert caught.value.stop_result is not None
