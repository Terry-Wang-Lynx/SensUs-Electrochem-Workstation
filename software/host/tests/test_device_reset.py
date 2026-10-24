import copy

import pytest

from pa_host.automation import AutomationError
from test_guided_integration import dispatch, guided_app
from test_keyboard_stop_recovery import recovery_app


def test_device_reset_clears_session_and_old_points_but_preserves_document(recovery_app):
    controller, service, serials = recovery_app
    document = copy.deepcopy(service.snapshot()["document"])
    old_token = controller._motion.snapshot()["connection_token"]
    assert service.snapshot()["manual"]["points"]
    result = dispatch("motion/reset")
    assert result["state"]["stop_confirmed"] is True
    assert result["state"]["recovery_required"] is False
    assert result["motion"]["connected"] is True and result["motion"]["faulted"] is False
    assert result["motion"]["connection_token"] != old_token
    manual = service.snapshot()["manual"]
    assert manual["active"] is False and manual["status"] == "idle"
    assert manual["points"] == {} and manual["error"] is None
    assert service.snapshot()["document"] == document
    assert not any(frame[1] in (0xFD, 0xF3, 0xF6, 0x0A, 0x9A) for serial in serials for frame in serial.writes)


def test_device_reset_cannot_erase_unknown_stop_or_allow_motion(recovery_app):
    controller, service, serials = recovery_app
    serials[-1].overrides[2, 0xFE] = b""
    with pytest.raises(AutomationError) as caught:
        dispatch("motion/reset")
    assert caught.value.code == "device_reset_stop_unknown"
    assert controller.snapshot()["recovery_required"]
    assert not controller.snapshot()["stop_confirmed"]
    assert service.snapshot()["manual"]["active"]
    assert not any(frame[1] in (0xFD, 0xF3, 0xF6) for serial in serials for frame in serial.writes)


def test_reset_can_carry_explicit_upper_protection_without_starting_calibration(recovery_app):
    controller, service, serials = recovery_app
    result = dispatch("motion/reset", {"boundary_at_current": {"axis": "Z", "direction": "up"}})
    assert result["guided"]["manual"]["boundary_at_current"] == {"axis": "Z", "direction": "up"}
    assert result["guided"]["manual"]["active"] is False
