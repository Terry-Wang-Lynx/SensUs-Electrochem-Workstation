import pytest

from pa_host.automation import AutomationError
from pa_host.emm_motion import MotionError
from test_alignment_jog import jogging


@pytest.mark.parametrize("stop_confirmed", [True, False])
@pytest.mark.parametrize("first_stop_confirmed", [True, False])
def test_followup_reconnect_error_preserves_first_cause_but_never_masks_unknown_stop(stop_confirmed, first_stop_confirmed):
    manager, motion, _ = jogging()
    motion.stop_confirmed = first_stop_confirmed
    with pytest.raises(AutomationError):
        manager._halt("fault", force=True, error=MotionError("manual_target_mismatch", "Original target mismatch"))
    motion.stop_confirmed = stop_confirmed
    with pytest.raises(AutomationError):
        manager._halt("fault", force=True, error=MotionError("reconnect_required", "Motion fault requires reconnect"))
    state = manager.snapshot()
    assert state["stop_result"]["stop_confirmed"] is stop_confirmed
    if stop_confirmed and first_stop_confirmed:
        assert state["error"]["code"] == "manual_target_mismatch"
    else:
        assert state["error"]["code"] == "alignment_stop_unknown"
        assert state["error"]["cause"]["code"] == "manual_target_mismatch"


@pytest.mark.parametrize("first_stop_confirmed", [True, False])
def test_explicit_stop_retry_failure_keeps_original_cause(first_stop_confirmed):
    manager, motion, _ = jogging()
    motion.stop_confirmed = first_stop_confirmed
    with pytest.raises(AutomationError):
        manager._halt("fault", force=True, error=MotionError("manual_target_mismatch", "Original target mismatch"))
    motion.stop_confirmed = False
    with pytest.raises(AutomationError):
        manager._halt("global_stop", force=True)
    state = manager.snapshot()
    assert state["error"]["code"] == "alignment_stop_unknown"
    assert state["error"]["cause"]["code"] == "manual_target_mismatch"
