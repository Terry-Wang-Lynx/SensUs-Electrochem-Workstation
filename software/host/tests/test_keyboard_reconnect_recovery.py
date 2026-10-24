import pytest

from pa_host import gui_server
from pa_host.automation import AutomationController, AutomationError
from pa_host.emm_motion import MotionError, SharedEmmBus
from pa_host.guided_automation import GuidedAutomation
from test_guided_integration import guided_app
from test_keyboard_alignment_integration import dispatch
from test_powered_manual_driver import PoweredManualSerial
from test_keyboard_fast_jog import WireClock


@pytest.mark.parametrize("stop_fails", [False, True])
@pytest.mark.parametrize("transaction_latency", [0, 0.03])
def test_blocked_old_session_rechecks_stop_on_new_connection_before_restart(guided_app, tmp_path, stop_fails,
                                                                         monkeypatch, transaction_latency):
    serials = []
    clock = WireClock()
    monkeypatch.setattr("pa_host.emm_motion.time", clock)

    def factory(port, baud, axes, **options):
        serial = PoweredManualSerial(timeout=0.2, write_timeout=0.2)
        serial.statuses = {1: 3, 2: 3}
        original_write = serial.write
        original_read = serial.read

        def write(frame):
            clock.sleep(transaction_latency)
            return original_write(frame)

        def read(count):
            clock.sleep(0.001)
            return original_read(count)

        serial.write = write
        serial.read = read
        serials.append(serial)
        return SharedEmmBus(port, baud, axes, serial_factory=lambda **settings: serial,
                            **options)

    controller = AutomationController(tmp_path / "automation", tmp_path / "runs", tmp_path,
                                      motion_factory=factory, motion_feedback_interval_s=0.05)
    service = GuidedAutomation(tmp_path / "guided", require_four_point=True,
        alignment_error_callback=lambda error, result: gui_server._alignment_fault(controller, error, result))
    document = service.snapshot()["document"]
    document["bindings"]["motion"]["port"] = "fake-reconnect-recovery"
    service.save(document)
    controller.connect_motion(document["bindings"]["motion"])
    guided_app.automation, guided_app.guided_automation = controller, service
    try:
        began = dispatch("guided/calibration/begin", {"control_mode": "keyboard", "motion_confirmed": True})
        old_token = began["manual"]["session_token"]
        with pytest.raises(AutomationError, match="Unexpected serial response length"):
            service._alignment._halt("fault", force=True, error=MotionError("response_length", "Unexpected serial response length"))
        controller._manual_calibration_active = False
        controller.disconnect_motion()
        controller.connect_motion(document["bindings"]["motion"])
        controller._manual_calibration_active = True
        serial = serials[-1]
        serial.writes.clear()
        assert controller._motion.snapshot()["stop_confirmed"] is None
        if stop_fails:
            serial.overrides[(2, 0xFE)] = b""
            with pytest.raises(AutomationError) as caught:
                dispatch("guided/calibration/begin", {"control_mode": "keyboard", "motion_confirmed": True})
            assert caught.value.code == "alignment_stop_unconfirmed"
            assert controller.snapshot()["recovery_required"] is True
            assert not any(frame[1] in (0xF3, 0xFD) for frame in serial.writes)
            assert service.snapshot()["manual"]["session_token"] == old_token
        else:
            restarted = dispatch("guided/calibration/begin", {"control_mode": "keyboard", "motion_confirmed": True})
            assert restarted["manual"]["status"] == "aligning"
            assert restarted["manual"]["session_token"] != old_token
            assert restarted["manual"]["points"] == {}
            assert {frame[0] for frame in serial.writes if frame[1] == 0xFE} == {1, 2}
            assert not any(frame[1] in (0xF3, 0xFD, 0x0A, 0x9A) for frame in serial.writes)
    finally:
        if service._alignment is not None:
            service._alignment.retire()
        if controller._motion is not None:
            serials[-1].overrides.clear()
            controller._motion.close()
        controller._stop_motion_feedback()
