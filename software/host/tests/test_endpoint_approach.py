import copy
import threading
import time

import pytest

from pa_host.emm_motion import MotionError
import test_calibration_actual_extent as extent
import test_keyboard_responsiveness as responsive
from test_emm_motion import signed
from test_live_motion_bus import LiveMotionSerial


CAPTURES = {
    "first": {"X": -7486637, "Z": 2000411},
    "working": {"X": -7486637, "Z": 2000409},
    "safe": {"X": -7486637, "Z": 959271},
    "last": {"X": -970683, "Z": 2020975},
}
PRE_STOP_TAIL = [
    (-7486310, -9, 91, 1),
    (-7486488, -7, 55, 1),
    (-7486698, -6, 132, 3),
    (-7486785, -4, 160, 1),
]


class EndpointIncidentSerial(LiveMotionSerial):
    def __init__(self, **options):
        super().__init__(**options)
        self.incident_active = False
        self.tail_index = 0
        self.before_first_stop = []

    def write(self, frame):
        address, opcode = frame[:2]
        if opcode == 0xFD and address == 1:
            result = super().write(frame)
            assert address == 1
            self.incident_active = True
            self.targets[0x33][1] = -7486629
            return result
        if self.incident_active and opcode == 0xFE:
            self.incident_active = False
            self.active_address = None
            self.finite_settling_address = None
            return super().write(frame)
        if self.incident_active and address == 1 and opcode == 0x43:
            assert self.tail_index < len(PRE_STOP_TAIL), "Captured pre-STOP trace exhausted"
            raw, rpm, error, status = PRE_STOP_TAIL[self.tail_index]
            self.tail_index += 1
            self.positions[1], self.speeds[1], self.statuses[1] = raw, rpm, status
            self.before_first_stop.append((raw, rpm, error, status))
            self.writes.append(frame)
            self.buffer.extend(bytes([1, 0x43, 31, 9]) + (24000).to_bytes(2, "big")
                               + (3).to_bytes(2, "big") + (raw & 0xFFFF).to_bytes(2, "big")
                               + signed(-7486629, 4) + signed(rpm, 2) + signed(raw, 4)
                               + signed(error, 4) + bytes([3, status, 0x6B]))
            return len(frame)
        return super().write(frame)


def incident_bus(tmp_path, monkeypatch, serial_class=EndpointIncidentSerial, enter_safe=True, direction=0):
    monkeypatch.setattr(extent, "CAPTURES", copy.deepcopy(CAPTURES))
    monkeypatch.setattr(responsive, "SummarySerial", serial_class)
    if direction == 0:
        service, bus, serial = extent.actual_session(tmp_path)
    else:
        bus, serial = responsive.responsive_bus()
        bus.cancel_keyboard_alignment()
        for axis in ("X", "Z"):
            bus.axes[axis]["positive_direction"] = direction
            bus._state[axis]["mechanical"] = copy.deepcopy(bus.axes[axis])
        service = extent.GuidedAutomation(tmp_path, require_four_point=True)
        document = service.snapshot()["document"]
        document["bindings"]["motion"] = {"port": bus.port, "baud": bus.baud, "axes": copy.deepcopy(bus.axes)}
        service.save(document)
        service.begin_keyboard_calibration(bus, confirmed=True)
        for point, counts in CAPTURES.items():
            for axis, count in counts.items():
                address = bus.axes[axis]["address"]
                serial.positions[address] = -count
                for targets in serial.targets.values():
                    targets[address] = -count
            service.capture_calibration(bus, point)
    count_sign = -1 if direction else 1
    serial.positions.update({1: -970685 * count_sign, 2: 694865 * count_sign})
    for targets in serial.targets.values():
        targets.update(serial.positions)
    service.finish_calibration(bus)
    record = service.trusted_calibration(bus)
    bus.install_experiment_calibration(record)
    assert record["reference"]["reference_counts"] == {"X": -7519405 * count_sign, "Z": 662097 * count_sign}
    assert record["x_first_mm"] == 1
    assert record["z_safe_mm"] == 9.06903076171875
    if enter_safe:
        bus.move_to("Z", record["z_safe_mm"], 0.5)
    serial.writes.clear()
    serial.events = []
    bus._callback = serial.events.append
    return bus, serial, record


def replay_incident(bus, serial, record):
    with pytest.raises(MotionError) as caught:
        bus._move_to_target("X", record["x_first_mm"], 20)
    return caught.value


def test_custom_first_incident_guard_fault_precedes_first_stop(tmp_path, monkeypatch):
    bus, serial, record = incident_bus(tmp_path, monkeypatch)
    error = replay_incident(bus, serial, record)
    assert error.code == "calibration_motion"
    assert serial.before_first_stop == PRE_STOP_TAIL
    assert (PRE_STOP_TAIL[-1][0] + 7519405) / 32768 == 0.9954833984375
    assert 1 - 0.9954833984375 > 2 * 0.8 / 360
    first_stop = next(index for index, frame in enumerate(serial.writes) if frame[1] == 0xFE)
    assert sum(frame[:2] == bytes([1, 0x43]) for frame in serial.writes[:first_stop]) >= 4
    assert {frame[0] for frame in serial.writes if frame[1] == 0xFE} == {1, 2}
    assert not any(event["kind"] == "arrived" for event in serial.events)
    assert bus.snapshot()["axes"]["X"]["target_mm"] == record["x_first_mm"]


def test_fast_endpoint_move_first_targets_an_interior_position(tmp_path, monkeypatch):
    bus, serial, record = incident_bus(tmp_path, monkeypatch, LiveMotionSerial)
    result = bus.move_to("X", record["x_first_mm"], 20)
    command = next(frame for frame in serial.writes if frame[1] == 0xFD)
    rpm = int.from_bytes(command[3:5], "big")
    pulses = int.from_bytes(command[6:10], "big")
    first_destination = -970685 - pulses * 65536 / 3200
    first_destination_mm = (first_destination + 7519405) / 32768
    assert rpm == 600
    assert first_destination_mm > record["x_first_mm"] + 0.0044444444444444444
    commands = [frame for frame in serial.writes if frame[1] == 0xFD]
    assert len(commands) == 2
    assert int.from_bytes(commands[-1][3:5], "big") == 15
    assert result["axes"]["X"]["target_mm"] == record["x_first_mm"]
    assert not any(frame[1] == 0xFE for frame in serial.writes)
    assert serial.collisions == []


@pytest.mark.parametrize("axis,endpoint,opposite", [
    ("X", "x_first_mm", "x_last_mm"),
    ("X", "x_last_mm", "x_first_mm"),
    ("Z", "z_safe_mm", "z_working_mm"),
    ("Z", "z_working_mm", "z_safe_mm"),
])
@pytest.mark.parametrize("direction", [0, 1])
def test_both_axes_and_both_endpoints_use_finite_fast_then_slow(tmp_path, monkeypatch, axis, endpoint, opposite, direction):
    bus, serial, record = incident_bus(tmp_path, monkeypatch, LiveMotionSerial, direction=direction)
    address = bus.axes[axis]["address"]
    reference = record["reference"]["reference_counts"][axis]
    serial.positions[address] = reference + round(record[opposite] * 32768) * (-1 if direction else 1)
    for targets in serial.targets.values():
        targets[address] = serial.positions[address]
    result = bus.move_to(axis, record[endpoint], 20)
    commands = [frame for frame in serial.writes if frame[1] == 0xFD]
    assert [int.from_bytes(frame[3:5], "big") for frame in commands] == [600, 15]
    assert result["axes"][axis]["target_mm"] == record[endpoint]
    assert not any(frame[1] == 0xFE for frame in serial.writes)
    assert serial.collisions == []


@pytest.mark.parametrize("distance", [0, 0.2, 0.9])
def test_close_endpoint_never_moves_away_to_create_approach(tmp_path, monkeypatch, distance):
    bus, serial, record = incident_bus(tmp_path, monkeypatch, LiveMotionSerial)
    serial.positions[1] = -7486637 + round(distance * 32768)
    for targets in serial.targets.values():
        targets[1] = serial.positions[1]
    result = bus.move_to("X", 1, 20)
    commands = [frame for frame in serial.writes if frame[1] == 0xFD]
    assert len(commands) == (0 if distance == 0 else 1)
    assert all(int.from_bytes(frame[3:5], "big") == 15 and frame[2] == 1 for frame in commands)
    assert result["axes"]["X"]["target_mm"] == 1
    assert not any(frame[1] == 0xFE for frame in serial.writes)


def test_startup_corridor_uses_only_exact_safe_target_at_slow_speed(tmp_path, monkeypatch):
    bus, serial, record = incident_bus(tmp_path, monkeypatch, LiveMotionSerial, enter_safe=False)
    result = bus.move_to("Z", record["z_safe_mm"], 20)
    commands = [frame for frame in serial.writes if frame[1] == 0xFD]
    assert len(commands) == 1
    assert int.from_bytes(commands[0][3:5], "big") == 15
    assert result["axes"]["Z"]["target_mm"] == record["z_safe_mm"]
    assert not any(frame[1] == 0xFE for frame in serial.writes)


@pytest.mark.parametrize("phase", [0, 1, 2])
def test_cancel_before_or_between_phases_never_sends_next_fd(tmp_path, monkeypatch, phase):
    bus, serial, record = incident_bus(tmp_path, monkeypatch, LiveMotionSerial)
    cancellation = threading.Event()
    if phase == 0:
        cancellation.set()
    else:
        def cancel_at_phase(event):
            if event["kind"] == "endpoint_approach_phase" and event["phase"] == phase:
                cancellation.set()
        bus._callback = cancel_at_phase
    with pytest.raises(MotionError) as caught:
        bus.move_to("X", record["x_first_mm"], 20, cancellation)
    assert caught.value.code == "cancelled"
    assert caught.value.stop_result["stop_confirmed"]
    assert len([frame for frame in serial.writes if frame[1] == 0xFD]) == max(0, phase - 1)
    assert not bus._completed_motion_targets


@pytest.mark.parametrize("change", ["peer_drift", "generation", "calibration"])
def test_changed_identity_or_held_peer_blocks_final_fd(tmp_path, monkeypatch, change):
    bus, serial, record = incident_bus(tmp_path, monkeypatch, LiveMotionSerial)
    def change_after_stage(event):
        if event["kind"] != "endpoint_approach_stage_arrived":
            return
        if change == "peer_drift":
            serial.positions[2] += 200
            for targets in serial.targets.values():
                targets[2] = serial.positions[2]
        elif change == "generation":
            bus.stop_all()
        else:
            bus._experiment_calibration["id"] = "changed"
    bus._callback = change_after_stage
    with pytest.raises(MotionError):
        bus.move_to("X", record["x_first_mm"], 20)
    assert len([frame for frame in serial.writes if frame[1] == 0xFD]) == 1
    assert not bus._completed_motion_targets


def test_whole_deadline_includes_preflight(tmp_path, monkeypatch):
    bus, serial, record = incident_bus(tmp_path, monkeypatch, LiveMotionSerial)
    bus._move_timeout = 0.001
    with pytest.raises(MotionError) as caught:
        bus.move_to("X", record["x_first_mm"], 20)
    assert caught.value.code == "move_timeout"
    assert not any(frame[1] == 0xFD for frame in serial.writes)


def test_deadline_after_initial_preflight_reports_move_timeout(tmp_path, monkeypatch):
    bus, serial, record = incident_bus(tmp_path, monkeypatch, LiveMotionSerial)
    read_axes = bus._read_experiment_axes
    reads = []
    def expire_second_preflight(*arguments, **options):
        reads.append(True)
        if len(reads) == 2:
            time.sleep(max(0, options["deadline"] - time.monotonic()) + 0.002)
            raise MotionError("transaction_timeout", "Injected exhausted phase preflight")
        return read_axes(*arguments, **options)
    monkeypatch.setattr(bus, "_read_experiment_axes", expire_second_preflight)
    bus._move_timeout = 0.15
    with pytest.raises(MotionError) as caught:
        bus.move_to("X", record["x_first_mm"], 20)
    assert len(reads) == 2
    assert caught.value.code == "move_timeout"
    assert not any(frame[1] == 0xFD for frame in serial.writes)


def test_unrepresentable_final_speed_rejects_before_any_fd(tmp_path, monkeypatch):
    bus, serial, record = incident_bus(tmp_path, monkeypatch, LiveMotionSerial)
    states = copy.deepcopy(bus.snapshot()["axes"])
    bounds = {"X": sorted((record["x_first_mm"], record["x_last_mm"])),
              "Z": (record["z_safe_mm"], record["z_working_mm"])}
    bus.axes["X"]["lead_mm"] = 60
    monkeypatch.setattr(bus, "_read_experiment_axes", lambda *arguments, **options: (states, bounds))
    with pytest.raises(MotionError) as caught:
        bus.move_to("X", record["x_first_mm"], 20)
    assert caught.value.code == "speed_range"
    assert not any(frame[1] == 0xFD for frame in serial.writes)


def test_compound_default_budget_cannot_exceed_eight_hours(tmp_path, monkeypatch):
    from pa_host import motion_approach

    bus, serial, record = incident_bus(tmp_path, monkeypatch, LiveMotionSerial)
    monkeypatch.setattr(motion_approach, "endpoint_phases", lambda *arguments, **options: [(100000, 20), (1, 0.5)])
    with pytest.raises(MotionError) as caught:
        bus.move_to("X", record["x_first_mm"], 20)
    assert caught.value.code == "move_duration"
    assert not any(frame[1] == 0xFD for frame in serial.writes)


@pytest.mark.parametrize("stop_fails", [False, True])
def test_cancellation_in_fast_motion_preserves_dual_stop_result(tmp_path, monkeypatch, stop_fails):
    bus, serial, record = incident_bus(tmp_path, monkeypatch, LiveMotionSerial)
    cancellation = threading.Event()
    serial.after_progress = lambda device: cancellation.set()
    if stop_fails:
        serial.overrides[2, 0xFE] = b""
    with pytest.raises(MotionError) as caught:
        bus.move_to("X", record["x_first_mm"], 20, cancellation)
    assert caught.value.code == "cancelled"
    assert caught.value.stop_result["stop_confirmed"] is not stop_fails
    assert len([frame for frame in serial.writes if frame[1] == 0xFD]) == 1
    assert {frame[0] for frame in serial.writes if frame[1] == 0xFE} == {1, 2}
    assert not bus._completed_motion_targets


def test_stage_completion_does_not_publish_logical_arrival(tmp_path, monkeypatch):
    bus, serial, record = incident_bus(tmp_path, monkeypatch, LiveMotionSerial)
    bus.move_to("X", record["x_first_mm"], 20)
    arrivals = [event for event in serial.events if event["kind"] == "arrived"]
    stages = [event for event in serial.events if event["kind"] == "endpoint_approach_stage_arrived"]
    assert len(stages) == len(arrivals) == 1
    assert stages[0]["target_mm"] == 2
    assert arrivals[0]["target_mm"] == record["x_first_mm"]
    proof = bus.verify_natural_park(record)
    assert proof["basis"] == "natural_finite_completion"
    assert not any(frame[1] == 0xFE for frame in serial.writes)


def test_compound_approach_retains_operation_lock_between_phases(tmp_path, monkeypatch):
    bus, serial, record = incident_bus(tmp_path, monkeypatch, LiveMotionSerial)
    attempts = []
    def attempt_peer_move(event):
        if event["kind"] == "endpoint_approach_phase" and event["phase"] == 2:
            try:
                bus.move_to("Z", record["z_safe_mm"], 0.5)
            except MotionError as error:
                attempts.append(error.code)
    bus._callback = attempt_peer_move
    bus.move_to("X", record["x_first_mm"], 20)
    assert attempts == ["motion_busy"]
    assert all(frame[0] == 1 for frame in serial.writes if frame[1] == 0xFD)
    assert not any(frame[1] == 0xFE for frame in serial.writes)


@pytest.mark.parametrize("current,target,bounds", [
    (0.998, 1, (1, 20)), (20.002, 20, (1, 20)),
    (1.009, 1, (1, 1.01)), (1, 1, (1, 20)),
])
def test_narrow_or_already_near_endpoint_only_uses_slow_exact_target(current, target, bounds):
    from pa_host.motion_approach import endpoint_phases

    assert endpoint_phases(current, target, 20, bounds, 0.000625, 2 * 0.8 / 360) == [(target, 0.5)]
