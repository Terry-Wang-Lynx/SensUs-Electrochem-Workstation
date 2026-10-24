from __future__ import annotations

import copy

import pytest

from pa_host.emm_motion import MotionError, SharedEmmBus
from test_emm_motion import AXES, FakeSerial


class ManualSerial(FakeSerial):
    def write(self, frame):
        if frame[1] == 0xF3 and frame[3] == 0:
            self.writes.append(frame)
            self.statuses[frame[0]] &= ~1
            self.buffer.extend(bytes([frame[0], 0xF3, 2, 0x6B]))
            return len(frame)
        return super().write(frame)


class QualifiedReleaseBus(SharedEmmBus):
    manual_calibration_supported = True


def manual_bus(*, qualified=True):
    serial = ManualSerial(timeout=0.2, write_timeout=0.2)
    driver = QualifiedReleaseBus if qualified else SharedEmmBus
    bus = driver("fake-rs485", 115200, copy.deepcopy(AXES), serial_factory=lambda **options: serial,
                 transaction_timeout_s=0.2)
    bus.connect()
    bus.confirm_reference()
    bus.enable("X")
    bus.enable("Z")
    serial.writes.clear()
    return bus, serial


def test_real_driver_rejects_unqualified_manual_workflow_before_any_release():
    bus, serial = manual_bus(qualified=False)
    with pytest.raises(MotionError) as failure:
        bus.begin_manual_calibration()
    assert failure.value.code == "manual_relock_unqualified"
    assert serial.writes == []
    assert serial.statuses == {1: 3, 2: 3}
    assert bus.snapshot()["manual"]["supported"] is False
    assert bus.snapshot()["manual"]["active"] is False


def test_manual_release_and_capture_only_disable_and_read_commands():
    bus, serial = manual_bus()
    result = bus.begin_manual_calibration()
    assert result["raw_counts"] == {"X": 65536, "Z": -32768}
    assert serial.statuses == {1: 2, 2: 2}
    assert [frame for frame in serial.writes if frame[1] == 0xF3] == [
        bytes.fromhex("01f3ab00006b"), bytes.fromhex("02f3ab00006b")]
    assert {frame[1] for frame in serial.writes} <= {0xFE, 0xF3, 0x1F, 0x42, 0x1A, 0x36, 0x35, 0x3A}
    serial.positions[1] += 100000
    captured = bus.capture_manual_position()
    assert captured["raw_counts"]["X"] == 165536
    assert bus.snapshot()["referenced"] is False
    assert bus.snapshot()["manual"]["active"] is True


@pytest.mark.parametrize("operation", ["finish", "cancel"])
def test_unverified_target_registers_never_enable_or_return_to_old_target(operation):
    bus, serial = manual_bus()
    bus.begin_manual_calibration()
    serial.positions[1] += 100000
    serial.writes.clear()
    with pytest.raises(MotionError) as failure:
        if operation == "finish":
            bus.finish_manual_calibration({"X": 0, "Z": -32768})
        else:
            bus.cancel_manual_calibration()
    assert failure.value.code == "manual_target_unverified"
    assert not any(frame[1] in (0xFD, 0xF3, 0x0A, 0x93, 0x9A) for frame in serial.writes)
    assert serial.positions[1] == 165536
    assert bus.snapshot()["manual"]["status"] == "blocked"
    with pytest.raises(MotionError, match="manual"):
        bus.enable("X")
    with pytest.raises(MotionError, match="manual"):
        bus.confirm_reference()


def test_stop_invalidates_capture_generation_and_cannot_reenable():
    bus, serial = manual_bus()
    bus.begin_manual_calibration()
    bus.stop_all()
    with pytest.raises(MotionError, match="Stop|stop|cancel"):
        bus.capture_manual_position()
    with pytest.raises(MotionError):
        bus.enable("X")
    assert not any(frame[1] == 0xFD for frame in serial.writes)


def test_reference_identity_can_restore_only_unchanged_profiles_and_raw_checkpoint():
    bus, serial = manual_bus()
    identity = bus.reference_identity()
    bus.close()
    bus.connect()
    assert bus.snapshot()["referenced"] is False
    serial.writes.clear()
    restored = bus.restore_reference(identity)
    assert restored["referenced"] is True
    assert restored["axes"]["X"]["reference_count"] == 65536
    assert {frame[1] for frame in serial.writes} <= {0x1F, 0x42, 0x1A, 0x36, 0x35, 0x3A}
    assert restored["axes"]["X"]["enable_confirmed"] is False


def test_stop_during_reference_restore_cannot_be_cleared(monkeypatch):
    bus, serial = manual_bus()
    identity = bus.reference_identity()
    bus.close()
    bus.connect()
    stationary_states = bus._stationary_states

    def stopped_states():
        result = stationary_states()
        assert bus.stop_all()["stop_confirmed"] is True
        return result

    monkeypatch.setattr(bus, "_stationary_states", stopped_states)
    with pytest.raises(MotionError) as caught:
        bus.restore_reference(identity)
    assert caught.value.code == "cancelled"
    assert bus._stop_event.is_set()
    assert bus.snapshot()["referenced"] is False


@pytest.mark.parametrize("change", ["raw", "reference", "config", "mechanical", "power"])
def test_reference_restore_rejects_stale_or_changed_identity(change):
    bus, serial = manual_bus()
    identity = bus.reference_identity()
    bus.close()
    bus.connect()
    if change == "raw":
        serial.positions[1] += 3
    elif change == "reference":
        identity["reference_counts"]["X"] = "65536"
    elif change == "config":
        identity["profiles"]["X"]["firmware_version"] += 1
    elif change == "mechanical":
        identity["motion"]["axes"]["X"]["lead_mm"] = 3
    else:
        serial.statuses[1] |= 0x80
    with pytest.raises(MotionError):
        bus.restore_reference(identity)
    assert bus.snapshot()["referenced"] is False


def test_parked_checkpoint_after_stop_does_not_restore_reference_or_enable():
    bus, serial = manual_bus()
    serial.positions[1] += 32768
    before_stop = bus.reference_identity()
    assert bus.stop_all()["stop_confirmed"] is True
    serial.positions[1] += 1
    serial.writes.clear()
    generation = bus._stop_generation
    checkpoint = bus.parked_reference_checkpoint(before_stop)
    assert checkpoint["reference_counts"] == before_stop["reference_counts"]
    assert checkpoint["raw_counts"]["X"] == before_stop["raw_counts"]["X"] + 1
    assert checkpoint["profiles"] == before_stop["profiles"]
    assert bus._stop_generation == generation
    assert bus._stop_event.is_set()
    assert bus.snapshot()["stop_confirmed"] is True
    assert bus.snapshot()["referenced"] is False
    assert all(state["reference_count"] is None and not state["enable_confirmed"]
               for state in bus.snapshot()["axes"].values())
    assert {frame[1] for frame in serial.writes} <= {0x1F, 0x42, 0x1A, 0x36, 0x35, 0x3A}


@pytest.mark.parametrize("change", ["no_stop", "raw", "base", "profile", "power", "mechanical"])
def test_parked_checkpoint_rejects_changed_identity_without_falsifying_stop(change):
    bus, serial = manual_bus()
    before_stop = bus.reference_identity()
    if change != "no_stop":
        assert bus.stop_all()["stop_confirmed"] is True
    if change == "raw":
        serial.positions[1] += 2
    elif change == "base":
        before_stop["reference_counts"]["X"] = None
    elif change == "profile":
        before_stop["profiles"]["X"]["firmware_version"] += 1
    elif change == "power":
        serial.statuses[1] |= 0x80
    elif change == "mechanical":
        before_stop["motion"]["axes"]["X"]["lead_mm"] = 3
    with pytest.raises(MotionError):
        bus.parked_reference_checkpoint(before_stop)
    if change != "no_stop":
        assert bus.snapshot()["stop_confirmed"] is True
        assert bus.snapshot()["referenced"] is False
        assert bus._stop_event.is_set()


def test_parked_checkpoint_rejects_concurrent_stop_without_clearing_it(monkeypatch):
    bus, serial = manual_bus()
    before_stop = bus.reference_identity()
    bus.stop_all()
    stationary_states = bus._stationary_states

    def stopped_states():
        result = stationary_states()
        assert bus.stop_all()["stop_confirmed"] is True
        return result

    monkeypatch.setattr(bus, "_stationary_states", stopped_states)
    with pytest.raises(MotionError) as caught:
        bus.parked_reference_checkpoint(before_stop)
    assert caught.value.code == "cancelled"
    assert bus._stop_event.is_set()
    assert bus.snapshot()["stop_confirmed"] is True
    assert bus.snapshot()["referenced"] is False
