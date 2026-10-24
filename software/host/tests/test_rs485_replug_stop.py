import copy
import functools
import time

import pytest

from pa_host import gui_server
from pa_host.alignment_jog import AlignmentJog
from pa_host.automation import AutomationController, default_experiment
from pa_host.emm_motion import MotionError, SharedEmmBus
from pa_host.guided_automation import GuidedAutomation
from test_guided_integration import dispatch, guided_app
from test_powered_manual_driver import PoweredManualSerial, powered_bus
from test_keyboard_alignment_driver import keyboard_bus


@pytest.mark.parametrize("feedback", ["missing_count", "missing_config", "invalid", "stale"])
def test_keyboard_snapshot_omits_boundaries_without_current_feedback(feedback):
    bus, serial, _, _ = keyboard_bus()
    bus._manual["direction_bounds"] = {"X": {"upper": bus._state["X"]["raw_count"]}}
    state = bus._state["X"]
    if feedback == "missing_count":
        state["raw_count"] = None
    elif feedback == "missing_config":
        state["config"] = None
    elif feedback == "invalid":
        state["telemetry_valid"] = False
    else:
        state["updated_monotonic"] = time.monotonic() - 3
    bus._last_stop = {"stop_confirmed": False}
    bus._faulted = True
    before = list(serial.writes)
    snapshot = bus.snapshot()
    assert snapshot["manual"]["boundaries"] == []
    assert snapshot["stop_confirmed"] is False
    assert snapshot["faulted"] is True
    assert serial.writes == before


def test_keyboard_direction_budget_rejects_unknown_count():
    bus, serial, _, _ = keyboard_bus()
    state = {**bus._state["X"], "raw_count": None}
    before = list(serial.writes)
    with pytest.raises(MotionError) as caught:
        bus._keyboard_direction_budget("X", 0, state)
    assert caught.value.code == "invalid_telemetry"
    assert serial.writes == before


def disconnected_write(frame):
    raise OSError(6, "Device not configured")


def test_os_detached_serial_is_not_reported_as_connected_and_stop_stays_unknown():
    bus, serial, events = powered_bus(enabled=True)
    serial.write = disconnected_write
    result = bus.stop_all()
    assert result["stop_confirmed"] is False
    assert bus.snapshot()["connected"] is False
    assert bus.snapshot()["faulted"] is True
    assert serial.closed


def test_explicit_recovery_stop_reopens_only_saved_bus_without_moving(tmp_path):
    serials = []

    def serial_factory(**options):
        serial = PoweredManualSerial(**options)
        serial.statuses = {1: 3, 2: 3}
        serials.append(serial)
        return serial

    def motion_factory(port, baud, axes, **options):
        bus = SharedEmmBus(port, baud, axes, serial_factory=serial_factory, **options)
        bus._command_gap_s = 0
        return bus

    controller = AutomationController(tmp_path / "state", tmp_path / "runs", tmp_path,
                                      motion_factory=motion_factory, motion_feedback_interval_s=60)
    settings = copy.deepcopy(default_experiment()["bindings"]["motion"])
    settings["port"] = "fake-usb-replug"
    controller.connect_motion(settings)
    controller._stop_motion_feedback()
    original_token = controller._motion.snapshot()["connection_token"]
    serials[0].write = disconnected_write
    controller.stop_motion()
    assert controller.snapshot()["recovery_required"]
    controller.stop()
    controller._thread.join(3)
    assert not controller._thread.is_alive()
    result = controller.snapshot()
    assert result["stop_confirmed"] is True and result["recovery_required"] is False
    assert len(serials) == 2
    assert controller._motion.port == settings["port"]
    assert controller._motion.snapshot()["connection_token"] != original_token
    assert not controller._motion.snapshot()["manual"]["active"]
    assert not any(frame[1] in (0xFD, 0xF3, 0xF6, 0x0A, 0x9A) for serial in serials for frame in serial.writes)
    controller._motion.close()


def test_replug_stop_retires_old_keyboard_session_without_reviving_old_points(guided_app, tmp_path, monkeypatch):
    serials = []

    def serial_factory(**options):
        serial = PoweredManualSerial(**options)
        serial.statuses = {1: 3, 2: 3}
        serials.append(serial)
        return serial

    def motion_factory(port, baud, axes, **options):
        bus = SharedEmmBus(port, baud, axes, serial_factory=serial_factory, **options)
        bus._command_gap_s = 0
        return bus

    monkeypatch.setattr("pa_host.guided_automation.AlignmentJog", functools.partial(AlignmentJog, start_threads=False))
    controller = AutomationController(tmp_path / "state", tmp_path / "runs", tmp_path,
                                      motion_factory=motion_factory, motion_feedback_interval_s=60)
    service = GuidedAutomation(tmp_path / "guided", require_four_point=True,
        alignment_error_callback=lambda error, result: gui_server._alignment_fault(controller, error, result))
    document = service.snapshot()["document"]
    document["bindings"]["motion"]["port"] = "fake-usb-replug"
    service.save(document)
    controller.connect_motion(document["bindings"]["motion"])
    controller._stop_motion_feedback()
    guided_app.automation, guided_app.guided_automation = controller, service
    old = dispatch("guided/calibration/begin", {"control_mode": "keyboard", "motion_confirmed": True})
    dispatch("guided/calibration/capture", {"point": "first"})
    serials[0].write = disconnected_write
    with pytest.raises(Exception):
        service._alignment._halt("fault", force=True, error=MotionError("serial_error", "USB removed"))
    dispatch("motion/stop")
    assert controller.snapshot()["stop_confirmed"]
    manual = service.snapshot()["manual"]
    assert manual["points"] == {}
    assert manual["error"]["code"] == "manual_interrupted"
    assert manual.get("session_token") is None
    restarted = dispatch("guided/calibration/begin", {"control_mode": "keyboard", "motion_confirmed": True})
    assert restarted["manual"]["status"] == "aligning"
    assert restarted["manual"]["session_token"] != old["manual"]["session_token"]
    assert not any(frame[1] in (0xFD, 0xF3, 0xF6, 0x0A, 0x9A) for serial in serials for frame in serial.writes)
    service._alignment.retire()
    controller._motion.close()
