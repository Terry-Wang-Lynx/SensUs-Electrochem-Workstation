from __future__ import annotations

import pytest

from pa_host.emm_motion import MotionError
from pa_host.motion_limits import AbsoluteMotionLimits
from test_emm_motion import make_bus
from test_motion_limits import capture_four, saved_limits


def ready_limited_bus(tmp_path):
    service, bus, serial = saved_limits(tmp_path)
    bus._move_timeout = 0.8
    serial.positions = {1: 32768, 2: 0}
    bus.confirm_reference()
    bus.enable("X")
    bus.enable("Z")
    return service, bus, serial


def test_required_driver_rejects_missing_limits_before_fd(tmp_path):
    bus, opened = make_bus(require_absolute_limits=True)
    bus.connect()
    bus.confirm_reference()
    bus.enable("X")
    with pytest.raises(MotionError) as caught:
        bus.move_to("X", 1, 1)
    assert caught.value.code == "absolute_limits_required"
    assert not any(frame[1] == 0xFD for frame in opened[0].writes)


@pytest.mark.parametrize("target", [11.1, 11.0, 10.997])
def test_requested_quantized_and_arrival_margin_cannot_exceed_boundary(tmp_path, target):
    service, bus, serial = ready_limited_bus(tmp_path)
    with pytest.raises(MotionError) as caught:
        bus.move_to("X", target, 1)
    assert caught.value.code == "absolute_limit_violation"
    assert not any(frame[1] == 0xFD for frame in serial.writes)


def test_logical_reference_change_does_not_move_raw_limits(tmp_path):
    service, bus, serial = ready_limited_bus(tmp_path)
    serial.positions[1] = 393000
    bus.confirm_reference()
    with pytest.raises(MotionError):
        bus.move_to("X", 1, 1)
    assert not any(frame[1] == 0xFD for frame in serial.writes)


def test_inward_move_from_captured_outer_boundary_is_allowed(tmp_path):
    service, bus, serial = ready_limited_bus(tmp_path)
    serial.positions[1] = 0
    bus.confirm_reference()
    result = bus.move_to("X", 1, 1)
    assert result["position"]["X"] == pytest.approx(1, abs=0.005)


@pytest.mark.parametrize("failure", ["drift", "read"])
def test_move_checks_uncommanded_axis_and_preserves_strict_stop(tmp_path, failure):
    service, bus, serial = ready_limited_bus(tmp_path)
    def on_move():
        if failure == "drift":
            serial.positions[2] = 65537
        else:
            serial.overrides[2, 0x36] = b""
    serial.on_move = on_move
    with pytest.raises(MotionError) as caught:
        bus.move_to("X", 1, 1)
    assert caught.value.stop_result is not None
    assert caught.value.stop_result["stop_confirmed"] is (failure == "drift")
    assert {frame[0] for frame in serial.writes if frame[1] == 0xFE} == {1, 2}


def test_capture_is_readonly_and_stop_race_cannot_install_record(tmp_path):
    bus, opened = make_bus(require_absolute_limits=True)
    bus.connect()
    serial = opened[0]
    service = AbsoluteMotionLimits(tmp_path)
    capture_four(service, bus, serial)
    stopped = False
    original_callback = bus._callback
    def callback(event):
        nonlocal stopped
        if not stopped and event.get("kind") == "wire" and event.get("direction") == "rx":
            stopped = True
            bus.stop_all()
        if original_callback:
            original_callback(event)
    bus._callback = callback
    with pytest.raises(MotionError):
        service.save(bus, clearance_confirmed=True)
    assert not service.snapshot(bus)["active"]


def test_busy_monitor_does_not_fault_or_send_stop(tmp_path):
    service, bus, serial = ready_limited_bus(tmp_path)
    with bus._operation():
        with pytest.raises(MotionError) as caught:
            bus.check_absolute_limits()
    assert caught.value.code == "motion_busy"
    assert not any(frame[1] == 0xFE for frame in serial.writes)


def test_monitor_detects_drift_and_carries_stop_result(tmp_path):
    service, bus, serial = ready_limited_bus(tmp_path)
    serial.positions[2] = -65537
    with pytest.raises(MotionError) as caught:
        bus.check_absolute_limits()
    assert caught.value.code == "absolute_limit_violation"
    assert caught.value.stop_result["stop_confirmed"] is True


def test_confirmed_stop_preserves_installed_raw_bounds_on_same_connection(tmp_path):
    service, bus, serial = ready_limited_bus(tmp_path)
    assert bus.stop_all()["stop_confirmed"] is True
    assert service.snapshot(bus)["active"]
    bus.confirm_reference()
    bus.enable("X")
    bus.move_to("X", 1, 1)
    assert service.snapshot(bus)["active"]


def test_capture_rejects_moving_axes_without_any_write_operation(tmp_path):
    bus, opened = make_bus()
    bus.connect()
    serial = opened[0]
    serial.speeds[2] = 1
    with pytest.raises(MotionError) as caught:
        bus.capture_limit_position()
    assert caught.value.code == "axis_moving"
    assert {frame[1] for frame in serial.writes} <= {0x1F, 0x42, 0x1A, 0x36, 0x35, 0x3A}


def test_quantized_target_must_preserve_margin_even_when_request_fits(tmp_path):
    service, bus, serial = ready_limited_bus(tmp_path)
    with pytest.raises(MotionError) as caught:
        bus.move_to("X", 10.9955, 1)
    assert caught.value.code == "absolute_limit_violation"
    assert not any(frame[1] == 0xFD for frame in serial.writes)


def test_monitor_checks_uncommanded_raw_position_after_logical_reference_change(tmp_path):
    service, bus, serial = ready_limited_bus(tmp_path)
    serial.positions[2] = 65537
    bus.confirm_reference()
    with pytest.raises(MotionError) as caught:
        bus.check_absolute_limits()
    assert caught.value.code == "absolute_limit_violation"
    assert caught.value.stop_result["stop_confirmed"] is True


@pytest.mark.parametrize("failure", ["drift", "read"])
def test_preflight_feedback_failure_faults_stops_and_preserves_confirmation(tmp_path, failure):
    service, bus, serial = ready_limited_bus(tmp_path)
    if failure == "drift":
        serial.positions[2] = 65537
    else:
        serial.overrides[2, 0x36] = b""
    with pytest.raises(MotionError) as caught:
        service.preflight(bus, [])
    assert caught.value.stop_result is not None
    assert caught.value.stop_result["stop_confirmed"] is (failure == "drift")


def test_supported_profile_change_is_detected_before_movement(tmp_path):
    service, bus, serial = ready_limited_bus(tmp_path)
    raw = bytearray.fromhex(bus.snapshot()["axes"]["Z"]["config"]["raw_hex"])
    raw[9] = 8
    serial.overrides[2, 0x42] = bytes(raw)
    with pytest.raises(MotionError) as caught:
        bus.move_to("X", 1, 1)
    assert caught.value.code == "absolute_limits_stale"
    assert not any(frame[1] == 0xFD for frame in serial.writes)


def test_stop_after_last_precommand_read_cannot_rearm_or_raise_arithmetic_error(tmp_path):
    service, bus, serial = ready_limited_bus(tmp_path)
    original = bus._read_axis
    reads = 0
    def interrupted_read(axis, **options):
        nonlocal reads
        result = original(axis, **options)
        if options.get("interruptible"):
            reads += 1
            if reads == 3:
                bus.stop_all()
        return result
    bus._read_axis = interrupted_read
    with pytest.raises(MotionError) as caught:
        bus.move_to("X", 1, 1)
    assert caught.value.code == "cancelled"
    assert not any(frame[1] == 0xFD for frame in serial.writes)
