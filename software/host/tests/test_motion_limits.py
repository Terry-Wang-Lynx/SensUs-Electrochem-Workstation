from __future__ import annotations

import copy
import json

import pytest

from pa_host.emm_motion import MotionError
from pa_host.motion_limits import AbsoluteMotionLimits
from test_emm_motion import make_bus


def capture_four(service, bus, serial):
    service.begin(bus)
    for boundary, address, count in (("x_left", 1, 393216), ("x_right", 1, 0),
                                      ("z_up", 2, -65536), ("z_down", 2, 65536)):
        serial.positions[address] = count
        service.capture(bus, boundary)


def saved_limits(tmp_path):
    bus, opened = make_bus(require_absolute_limits=True)
    bus.connect()
    service = AbsoluteMotionLimits(tmp_path)
    capture_four(service, bus, opened[0])
    service.save(bus, clearance_confirmed=True)
    return service, bus, opened[0]


def test_four_captures_save_without_reference_enable_or_drive_commands(tmp_path):
    service, bus, serial = saved_limits(tmp_path)
    result = service.snapshot(bus)
    assert result["state"] == "active" and result["active"]
    assert result["record"]["boundaries"] == {"x_left": 393216, "x_right": 0,
                                                "z_up": -65536, "z_down": 65536}
    assert bus.snapshot()["referenced"] is False
    assert {frame[1] for frame in serial.writes} <= {0x1F, 0x42, 0x1A, 0x36, 0x35, 0x3A}


def test_save_requires_all_four_correctly_ordered_boundaries_and_clearance(tmp_path):
    bus, opened = make_bus()
    bus.connect()
    service = AbsoluteMotionLimits(tmp_path)
    service.begin(bus)
    with pytest.raises(MotionError):
        service.capture(bus, "x_left=123")
    with pytest.raises(MotionError, match="four|Four"):
        service.save(bus, clearance_confirmed=True)
    capture_four(service, bus, opened[0])
    with pytest.raises(MotionError, match="clearance|余量"):
        service.save(bus)
    opened[0].positions[1] = 393216
    service.capture(bus, "x_right")
    with pytest.raises(MotionError, match="order|方向|span"):
        service.save(bus, clearance_confirmed=True)


def test_saved_record_shown_but_never_autoactivated_after_restart_or_reconnect(tmp_path):
    service, bus, serial = saved_limits(tmp_path)
    reloaded = AbsoluteMotionLimits(tmp_path)
    assert reloaded.snapshot(bus)["record"] == service.snapshot(bus)["record"]
    assert not reloaded.snapshot(bus)["active"]
    with pytest.raises(MotionError):
        reloaded.require(bus)
    token = bus.snapshot()["connection_token"]
    bus.close()
    bus.connect()
    assert bus.snapshot()["connection_token"] != token
    assert not service.snapshot(bus)["active"]
    with pytest.raises(MotionError):
        service.require(bus)


def test_snapshot_has_no_physical_io_and_returns_detached_records(tmp_path):
    service, bus, serial = saved_limits(tmp_path)
    writes = len(serial.writes)
    first = service.snapshot(bus)
    first["record"]["boundaries"]["x_left"] = 2**31
    assert service.snapshot(bus)["record"]["boundaries"]["x_left"] == 393216
    assert len(serial.writes) == writes


@pytest.mark.parametrize("damage", ["syntax", "unknown", "boolean", "reverse", "nan"])
def test_corrupt_storage_fails_closed(tmp_path, damage):
    service, bus, serial = saved_limits(tmp_path)
    path = tmp_path / "absolute-motion-limits.json"
    record = copy.deepcopy(service.snapshot(bus)["record"])
    if damage == "syntax":
        path.write_text("{broken", encoding="utf-8")
    else:
        if damage == "unknown":
            record["unexpected"] = True
        elif damage == "boolean":
            record["boundaries"]["x_left"] = True
        elif damage == "reverse":
            record["boundaries"]["x_right"] = record["boundaries"]["x_left"]
        else:
            record["created_unix_s"] = float("nan")
        path.write_text(json.dumps(record), encoding="utf-8")
    loaded = AbsoluteMotionLimits(tmp_path)
    assert loaded.snapshot(bus)["state"] == "error"
    with pytest.raises(MotionError):
        loaded.require(bus)


def test_stop_during_capture_does_not_authorize_save_and_cancel_stays_available(tmp_path):
    bus, opened = make_bus()
    bus.connect()
    service = AbsoluteMotionLimits(tmp_path)
    capture_four(service, bus, opened[0])
    bus.stop_all()
    with pytest.raises(MotionError):
        service.save(bus, clearance_confirmed=True)
    service.cancel(bus)
    assert not service.snapshot(bus)["calibrating"]
    assert not service.snapshot(bus)["active"]


def test_preflight_checks_every_raw_target_without_sending_movement(tmp_path):
    service, bus, serial = saved_limits(tmp_path)
    bus.confirm_reference()
    steps = [{"op": "move_x", "resolved_params": {"axis": "X", "target_mm": 1, "speed_mm_s": 1}},
             {"op": "move_z", "resolved_params": {"axis": "Z", "target_mm": 5, "speed_mm_s": 1}}]
    with pytest.raises(MotionError):
        service.preflight(bus, steps)
    assert not any(frame[1] == 0xFD for frame in serial.writes)


def test_save_interrupted_after_driver_install_does_not_rearm(tmp_path):
    bus, opened = make_bus(require_absolute_limits=True)
    bus.connect()
    service = AbsoluteMotionLimits(tmp_path)
    capture_four(service, bus, opened[0])
    original = bus.install_absolute_limits
    def interrupted_install(record):
        result = original(record)
        if record is not None:
            bus.stop_all()
        return result
    bus.install_absolute_limits = interrupted_install
    with pytest.raises(MotionError):
        service.save(bus, clearance_confirmed=True)
    assert not service.snapshot(bus)["active"]
    assert bus.snapshot()["absolute_limits"] is None


def test_changed_profile_during_capture_cannot_be_saved(tmp_path):
    bus, opened = make_bus()
    bus.connect()
    service = AbsoluteMotionLimits(tmp_path)
    capture_four(service, bus, opened[0])
    original = bus._state["Z"]["config"]["raw_hex"]
    changed = bytearray.fromhex(original)
    changed[-2] = 9
    opened[0].overrides[2, 0x42] = bytes(changed)
    with pytest.raises(MotionError):
        service.save(bus, clearance_confirmed=True)
    assert not service.snapshot(bus)["active"]


def test_snapshot_does_not_accept_boolean_profile_angle_from_storage(tmp_path):
    service, bus, serial = saved_limits(tmp_path)
    path = tmp_path / "absolute-motion-limits.json"
    record = service.snapshot(bus)["record"]
    record["profiles"]["X"]["config"]["step_angle"] = True
    path.write_text(json.dumps(record), encoding="utf-8")
    assert AbsoluteMotionLimits(tmp_path).snapshot()["state"] == "error"
