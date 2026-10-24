import copy
import threading
import time

import pytest

from pa_host.emm_motion import MotionError, SharedEmmBus
from test_emm_motion import AXES
from test_powered_manual_driver import PoweredManualSerial


class FastSerial(PoweredManualSerial):
    def __init__(self, **options):
        super().__init__(**options)
        self.statuses = {1: 3, 2: 3}
        self.on_fast_move = None
        self.lookahead = []

    def write(self, frame):
        address, opcode = frame[:2]
        if opcode != 0xFD or (address, opcode) in self.overrides:
            return super().write(frame)
        assert frame[10:] == bytes([0, 0, 0x6B])
        self.writes.append(frame)
        delta = round(int.from_bytes(frame[6:10], "big") * 65536 / 3200) * (-1 if frame[2] else 1)
        target = self.targets[0x33][address] + delta
        self.lookahead.append(abs(target - self.positions[address]) * 2 / 65536)
        self.positions[address] = target
        self.targets[0x33][address] = self.targets[0x34][address] = target
        self.buffer.extend(bytes([address, opcode, 2, 0x6B]))
        if self.on_fast_move:
            self.on_fast_move(address)
        return len(frame)


def fast_bus():
    serial = FastSerial(timeout=0.2, write_timeout=0.2)
    axes = copy.deepcopy(AXES)
    for axis in axes.values():
        axis["max_speed_mm_s"] = 20
    bus = SharedEmmBus("fake-fast-jog", 115200, axes, serial_factory=lambda **options: serial)
    bus._command_gap_s = 0
    bus.connect()
    bus.begin_keyboard_alignment(confirmed=True)
    serial.writes.clear()
    return bus, serial


@pytest.mark.parametrize("axis,direction", [("X", 0), ("X", 1), ("Z", 0), ("Z", 1)])
def test_fast_hold_uses_600rpm_and_bounded_continuous_position_targets(axis, direction):
    bus, serial = fast_bus()
    result = bus.alignment_fast_hold(axis, direction, 20, 20, 10, 30)
    moves = [(index, frame) for index, frame in enumerate(serial.writes) if frame[1] == 0xFD]
    assert len(moves) >= 2
    assert all(int.from_bytes(frame[3:5], "big") == 600 and frame[5] == 250 for index, frame in moves)
    assert all(frame[10] == 0 for index, frame in moves)
    assert max(serial.lookahead) <= 6
    assert not any(frame[1] == 0xFE for frame in serial.writes[moves[0][0]:moves[-1][0]])
    assert sum(int.from_bytes(frame[6:10], "big") for index, frame in moves) * 2 / 3200 <= 10
    assert result["segment_mm"] <= 10 and result["hold_exhausted"] is True
    assert bus.snapshot()["faulted"] is False
    assert not any(frame[1] in (0xF3, 0xF6, 0x0A, 0x9A) for frame in serial.writes)


def test_fast_hold_release_stops_without_faulting_or_reenabling():
    bus, serial = fast_bus()
    cancelled = threading.Event()
    serial.on_fast_move = lambda address: cancelled.set()
    with pytest.raises(MotionError) as caught:
        bus.alignment_fast_hold("X", 0, 20, 20, 10, 30, cancel_event=cancelled)
    assert caught.value.code == "cancelled"
    assert caught.value.stop_result["stop_confirmed"] is True
    assert bus.snapshot()["faulted"] is False
    assert bus.snapshot()["manual"]["active"] is True
    assert len([frame for frame in serial.writes if frame[1] == 0xFD]) == 1
    assert {frame[0] for frame in serial.writes if frame[1] == 0xFE} == {1, 2}


@pytest.mark.parametrize("exit_reason", ["release", "time_limit"])
@pytest.mark.parametrize("changed", ["held_axis", "profile"])
def test_fast_hold_audits_held_axis_after_stopping_on_every_exit(exit_reason, changed):
    bus, serial = fast_bus()
    cancellation = threading.Event()

    def change_and_stop(address):
        if changed == "held_axis":
            serial.positions[2] += 1000
            serial.targets[0x33][2] = serial.targets[0x34][2] = serial.positions[2]
        else:
            profile = bytearray.fromhex(bus.snapshot()["axes"]["Z"]["config"]["raw_hex"])
            profile[9] = 32
            serial.overrides[2, 0x42] = bytes(profile)
        if exit_reason == "release":
            cancellation.set()
        else:
            time.sleep(0.55)

    serial.on_fast_move = change_and_stop
    with pytest.raises(MotionError) as caught:
        bus.alignment_fast_hold("X", 0, 20, 20, 10, 0.5 if exit_reason == "time_limit" else 30,
                                cancel_event=cancellation)
    assert caught.value.code in ("calibration_motion", "manual_profile_changed")
    assert caught.value.stop_result["stop_confirmed"] is True
    assert bus.snapshot()["faulted"] is True


def test_release_during_post_command_target_audit_does_not_fault_on_a_changed_stop_target():
    bus, serial = fast_bus()
    cancellation = threading.Event()
    original_target = bus._read_manual_target
    interrupted = False

    def stop_before_target_result(axis, opcode, **options):
        nonlocal interrupted
        target = original_target(axis, opcode, **options)
        if serial.lookahead and not interrupted:
            interrupted = True
            cancellation.set()
            assert bus.stop_keyboard_alignment()["stop_confirmed"]
            return target + 1000
        return target

    bus._read_manual_target = stop_before_target_result
    with pytest.raises(MotionError) as caught:
        bus.alignment_fast_hold("X", 0, 20, 20, 10, 30, cancel_event=cancellation)
    assert caught.value.code == "cancelled"
    assert bus.snapshot()["faulted"] is False
    assert bus.snapshot()["manual"]["active"] is True


def test_cancel_does_not_reuse_a_previous_stop_when_a_new_global_stop_is_pending():
    bus, serial = fast_bus()
    cancellation = threading.Event()

    def pending_global_stop(address):
        with bus._state_lock:
            bus._stop_generation += 1
            bus._stop_event.set()
            bus._manual.update(active=False, generation=None)
        cancellation.set()

    serial.on_fast_move = pending_global_stop
    try:
        with pytest.raises(MotionError) as caught:
            bus.alignment_fast_hold("X", 0, 20, 20, 10, 30, cancel_event=cancellation)
        assert caught.value.code == "cancelled"
        assert caught.value.stop_result["stop_confirmed"] is False
    finally:
        bus.stop_all()


@pytest.mark.parametrize("interruption", ["after_owned_stop", "during_stopped_audit"])
def test_release_refreshes_stop_evidence_if_global_stop_supersedes_its_owned_stop(interruption):
    bus, serial = fast_bus()
    cancellation = threading.Event()
    serial.on_fast_move = lambda address: cancellation.set()
    original_stop = bus.stop_keyboard_alignment
    original_states = bus._keyboard_states

    def pending_global_stop():
        with bus._state_lock:
            bus._stop_generation += 1
            bus._stop_event.set()
            bus._manual.update(active=False, generation=None)

    def owned_stop():
        result = original_stop()
        if interruption == "after_owned_stop":
            pending_global_stop()
        return result

    def stopped_states(**options):
        states = original_states(**options)
        if options.get("targets") is False and interruption == "during_stopped_audit":
            pending_global_stop()
        return states

    bus.stop_keyboard_alignment = owned_stop
    bus._keyboard_states = stopped_states
    try:
        with pytest.raises(MotionError) as caught:
            bus.alignment_fast_hold("X", 0, 20, 20, 10, 30, cancel_event=cancellation)
        assert caught.value.code == "cancelled"
        assert caught.value.stop_result["stop_confirmed"] is False
        assert bus._current_stop_result()["stop_confirmed"] is False
    finally:
        bus.stop_all()


@pytest.mark.parametrize("driver", ["fine", "fast"])
def test_release_cannot_publish_its_stop_after_a_concurrent_global_stop_starts(driver):
    from test_keyboard_alignment_driver import keyboard_bus

    cancellation = threading.Event()
    if driver == "fast":
        bus, serial = fast_bus()
        serial.on_fast_move = lambda address: cancellation.set()
        run = lambda: bus.alignment_fast_hold("X", 0, 20, 20, 10, 30, cancel_event=cancellation)
    else:
        bus, serial, _, _ = keyboard_bus()
        serial.on_move = cancellation.set
        run = lambda: bus.alignment_step("X", 0, 0.5, 0.12, 1, cancel_event=cancellation)
    original_stop = bus.stop_keyboard_alignment
    global_stop = None

    def owned_stop():
        nonlocal global_stop
        result = original_stop()
        generation = bus._stop_generation
        bus._stop_lock.acquire()
        global_stop = threading.Thread(target=bus.stop_all)
        global_stop.start()
        deadline = time.monotonic() + 1
        while bus._stop_generation == generation and time.monotonic() < deadline:
            time.sleep(0.001)
        assert bus._stop_generation != generation
        return result

    bus.stop_keyboard_alignment = owned_stop
    try:
        with pytest.raises(MotionError) as caught:
            run()
        assert caught.value.code == "cancelled"
        assert bus._current_stop_result()["stop_confirmed"] is False
        assert caught.value.stop_result["stop_confirmed"] is False
    finally:
        if global_stop is not None:
            bus._stop_lock.release()
            global_stop.join(3)
            assert not global_stop.is_alive()


def test_time_limit_cannot_publish_stop_if_global_stop_starts_after_its_audit(monkeypatch):
    from pa_host import keyboard_fast_jog

    bus, serial = fast_bus()
    serial.on_fast_move = lambda address: time.sleep(0.55)
    original_audit = keyboard_fast_jog._audit_stopped_axes

    def interrupted_audit(*args):
        states = original_audit(*args)
        with bus._state_lock:
            bus._stop_generation += 1
            bus._stop_event.set()
            bus._manual.update(active=False, generation=None)
        return states

    monkeypatch.setattr(keyboard_fast_jog, "_audit_stopped_axes", interrupted_audit)
    try:
        with pytest.raises(MotionError) as caught:
            bus.alignment_fast_hold("X", 0, 20, 20, 10, 0.5)
        assert caught.value.code == "cancelled"
        assert caught.value.stop_result["stop_confirmed"] is False
    finally:
        bus.stop_all()


@pytest.mark.parametrize("failure", ["held_axis", "target", "profile"])
def test_fast_hold_does_not_continue_after_drift_or_changed_drive_state(failure):
    bus, serial = fast_bus()

    def change_after_move(address):
        if failure == "held_axis":
            serial.positions[2] += 1000
        elif failure == "target":
            serial.targets[0x33][1] += 1000
        else:
            changed = bytearray.fromhex(bus.snapshot()["axes"]["Z"]["config"]["raw_hex"])
            changed[9] = 32
            serial.overrides[2, 0x42] = bytes(changed)

    serial.on_fast_move = change_after_move
    with pytest.raises(MotionError) as caught:
        bus.alignment_fast_hold("X", 0, 20, 20, 10, 30)
    assert caught.value.stop_result["stop_confirmed"] is True
    assert bus.snapshot()["faulted"] is True


@pytest.mark.parametrize("speed,maximum,distance,duration", [(20.01, 20, 10, 30), (20, 20, 10.1, 30),
    (20, 20, 10, 30.1), (20, 10, 10, 30), (float("nan"), 20, 10, 30)])
def test_invalid_fast_hold_never_writes_a_motion_command(speed, maximum, distance, duration):
    bus, serial = fast_bus()
    with pytest.raises(MotionError):
        bus.alignment_fast_hold("X", 0, speed, maximum, distance, duration)
    assert not any(frame[1] == 0xFD for frame in serial.writes)


class SlewingSerial(FastSerial):
    def __init__(self, **options):
        super().__init__(**options)
        self.goals = {}
        self.updated = time.monotonic()
        self.stop_positions = []

    def write(self, frame):
        now = time.monotonic()
        for address, goal in self.goals.items():
            before = self.positions[address]
            distance = goal - before
            change = min(abs(distance), (now - self.updated) * 20 * 65536 / 2)
            self.positions[address] = round(before + (change if distance >= 0 else -change))
            moving = self.positions[address] != goal
            if not moving and self.speeds[address]:
                self.stop_positions.append(self.positions[address])
            self.speeds[address] = (600 if distance >= 0 else -600) if moving else 0
            self.statuses[address] = 1 if moving else 3
        self.updated = now
        address, opcode = frame[:2]
        before = self.positions[address]
        result = super().write(frame)
        if opcode == 0xFD:
            self.goals[address] = self.positions[address]
            self.positions[address] = before
            self.speeds[address] = -600 if frame[2] else 600
            self.statuses[address] = 1
        return result


class SharedBusDuringMotionSerial(SlewingSerial):
    def __init__(self, **options):
        super().__init__(**options)
        self.active_address = None
        self.peer_queries_before_stop = []

    def write(self, frame):
        address, opcode = frame[:2]
        if opcode == 0xFD:
            self.active_address = address
        elif opcode == 0xFE and address == self.active_address:
            self.active_address = None
        rejected = self.active_address is not None and address != self.active_address and opcode != 0xFE
        active_address = self.active_address
        result = super().write(frame)
        if rejected:
            self.peer_queries_before_stop.append(frame)
            self.buffer.extend(bytes([active_address, 0, 0xEE, 0x6B]))
        return result


@pytest.mark.parametrize("axis,direction", [("X", 0), ("Z", 1)])
def test_fast_hold_quiesces_active_motor_before_querying_the_shared_bus_peer(axis, direction):
    serial = SharedBusDuringMotionSerial(timeout=0.2, write_timeout=0.2)
    axes = copy.deepcopy(AXES)
    for settings in axes.values():
        settings["max_speed_mm_s"] = 20
    bus = SharedEmmBus("fake-shared-fast-bus", 115200, axes, serial_factory=lambda **options: serial)
    bus.connect()
    bus.begin_keyboard_alignment(confirmed=True)
    serial.writes.clear()
    result = bus.alignment_fast_hold(axis, direction, 20, 20, 10, 30)
    assert result["hold_exhausted"]
    assert serial.peer_queries_before_stop == []
    assert bus.snapshot()["faulted"] is False
    final_move = max(index for index, frame in enumerate(serial.writes) if frame[1] == 0xFD)
    audit = serial.writes[final_move + 1:]
    stop_index = next(index for index, frame in enumerate(audit) if frame[1] == 0xFE)
    peer_index = next(index for index, frame in enumerate(audit) if frame[0] != axes[axis]["address"])
    assert stop_index < peer_index


def test_rolling_fast_targets_keep_a_noninstant_drive_moving_at_production_wire_cadence():
    serial = SlewingSerial(timeout=0.2, write_timeout=0.2)
    axes = copy.deepcopy(AXES)
    for axis in axes.values():
        axis["max_speed_mm_s"] = 20
    bus = SharedEmmBus("fake-slewing-jog", 115200, axes, serial_factory=lambda **options: serial)
    bus.connect()
    bus.begin_keyboard_alignment(confirmed=True)
    start_count = serial.positions[1]
    result = bus.alignment_fast_hold("X", 0, 20, 20, 10, 30)
    assert result["hold_exhausted"]
    assert serial.stop_positions
    assert all((count - start_count) * 2 / 65536 >= result["segment_mm"] - 0.01 for count in serial.stop_positions)
    assert max(serial.lookahead) <= 6
