import copy

import pytest

from pa_host.automation import AutomationError
from pa_host.guided_automation import GuidedAutomation
from test_keyboard_responsiveness import responsive_bus
from test_guided_integration import guided_app


CAPTURES = {
    "first": {"X": -4081441, "Z": 1635143},
    "last": {"X": 3616307, "Z": 1635143},
    "working": {"X": 3616307, "Z": 3008197},
    "safe": {"X": 3616307, "Z": 438549},
}


def actual_session(tmp_path):
    bus, serial = responsive_bus()
    bus.cancel_keyboard_alignment()
    for axis in ("X", "Z"):
        bus.axes[axis]["positive_direction"] = 0
        bus._state[axis]["mechanical"] = copy.deepcopy(bus.axes[axis])
    service = GuidedAutomation(tmp_path, require_four_point=True)
    document = service.snapshot()["document"]
    document["bindings"]["motion"] = {"port": bus.port, "baud": bus.baud, "axes": copy.deepcopy(bus.axes)}
    service.save(document)
    service.begin_keyboard_calibration(bus, confirmed=True)
    for point, counts in CAPTURES.items():
        for axis, count in counts.items():
            address = bus.axes[axis]["address"]
            serial.positions[address] = count
            for targets in serial.targets.values():
                targets[address] = count
        service.capture_calibration(bus, point)
    serial.writes.clear()
    return service, bus, serial


def test_actual_failed_capture_set_finishes_compiles_and_installs_without_moving(tmp_path):
    service, bus, serial = actual_session(tmp_path)
    try:
        before = bus.snapshot()
        result = service.finish_calibration(bus)
        assert result["calibration"]["usable"]
        assert result["manual"]["status"] == "complete"
        motion = result["document"]["bindings"]["motion"]
        assert motion["axes"]["X"]["travel_mm"] == 2 + (3616307 + 4081441) / 32768
        assert motion["axes"]["Z"]["travel_mm"] == 2 + (3008197 - 438549) / 32768
        assert bus.snapshot()["connection_token"] == before["connection_token"]
        for axis in ("X", "Z"):
            assert bus.snapshot()["axes"][axis]["mechanical"] == motion["axes"][axis]
            assert bus.snapshot()["axes"][axis]["config"] == before["axes"][axis]["config"]
        compiled = service.compile(result["document"], mode="live", motion=bus)
        assert max(step["resolved_params"].get("target_mm", 0) for step in compiled["steps"]) > 150
        bus.install_experiment_calibration(service.trusted_calibration(bus))
        assert not any(frame[1] in (0xFD, 0xF3, 0xF6, 0x0A, 0x9A) for frame in serial.writes)
        restored = GuidedAutomation(tmp_path, require_four_point=True)
        assert restored.snapshot()["document"] == result["document"]
        assert restored.snapshot()["calibration"]["points"] == result["calibration"]["points"]
    finally:
        if service.snapshot()["manual"]["active"]:
            service._alignment.close()


def test_actual_geometry_failure_preserves_points_and_resumes_idle_feedback(tmp_path):
    service, bus, serial = actual_session(tmp_path)
    service._manual["points"]["safe"]["raw_counts"]["Z"] = CAPTURES["working"]["Z"] + 100
    service.freeze_alignment()
    try:
        with pytest.raises(AutomationError):
            service.finish_calibration(bus)
        state = service.snapshot()
        assert len(state["manual"]["points"]) == 4
        assert state["manual"]["alignment"]["status"] == "idle"
        assert state["manual"]["error"]["code"] == "calibration_height"
        assert not any(frame[1] in (0xFD, 0xF3, 0xF6) for frame in serial.writes)
    finally:
        service._alignment.close()


def test_coordinate_headroom_does_not_allow_experiment_beyond_recorded_endpoints(tmp_path):
    from pa_host.calibrated_motion import validate_calibrated_motion

    service, bus, serial = actual_session(tmp_path)
    result = service.finish_calibration(bus)
    compiled = service.compile(result["document"], mode="live", motion=bus)
    record = service.trusted_calibration(bus)
    assert validate_calibrated_motion(compiled, bus.snapshot(), record)
    for axis in ("X", "Z"):
        altered = copy.deepcopy(compiled)
        step = next(step for step in altered["steps"] if step["op"] == "move_" + axis.lower())
        step["resolved_params"]["target_mm"] = result["document"]["bindings"]["motion"]["axes"][axis]["travel_mm"]
        with pytest.raises(AutomationError, match="window"):
            validate_calibrated_motion(altered, bus.snapshot(), record)
    assert not any(frame[1] == 0xFD for frame in serial.writes)


def test_dispatch_keeps_controller_driver_and_saved_coordinate_identity_in_sync(tmp_path, guided_app):
    from pa_host import gui_server
    from pa_host.automation import AutomationController

    service, bus, serial = actual_session(tmp_path)
    controller = AutomationController(tmp_path / "controller", tmp_path / "runs", tmp_path)
    controller._motion = bus
    controller._motion_config = copy.deepcopy(service.snapshot()["document"]["bindings"]["motion"])
    controller._recovery_motion_config = copy.deepcopy(controller._motion_config)
    controller._manual_calibration_active = True
    guided_app.automation, guided_app.guided_automation = controller, service
    result = gui_server._automation_dispatch("POST", "guided/calibration/finish", {}, {})
    assert controller._motion_config == result["document"]["bindings"]["motion"]
    assert controller._recovery_motion_config == controller._motion_config
    assert controller._manual_calibration_active is False
    assert controller._motion_config["axes"] == bus.axes
    assert not any(frame[1] in (0xFD, 0xF3, 0xF6) for frame in serial.writes)
    from pa_host.emm_motion import SharedEmmBus
    from test_keyboard_responsiveness import SummarySerial

    def reconnect(port, baud, axes, **options):
        replacement = SummarySerial(timeout=.2, write_timeout=.2)
        replacement.positions = {1: CAPTURES["safe"]["X"], 2: CAPTURES["safe"]["Z"]}
        for targets in replacement.targets.values():
            targets.update(replacement.positions)
        connection = SharedEmmBus(port, baud, axes, serial_factory=lambda **settings: replacement, **options)
        connection._responsive_keyboard = True
        connection._command_gap_s = 0
        return connection

    controller._motion_factory = reconnect
    reset = gui_server._reset_automation_devices(controller, service)
    assert reset["motion"]["axes"]["X"]["mechanical"]["travel_mm"] == controller._motion_config["axes"]["X"]["travel_mm"]
    try:
        assert service.begin_keyboard_calibration(controller._motion, confirmed=True)["manual"]["active"]
    finally:
        service.cancel_calibration(controller._motion)
        controller.disconnect_motion()
