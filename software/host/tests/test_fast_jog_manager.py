import pytest

from pa_host.alignment_jog import AlignmentJog
from pa_host.automation import AutomationError
from test_alignment_jog import JogMotion, Clock, command, start


class FastJogMotion(JogMotion):
    def __init__(self):
        super().__init__()
        for axis in self.axes.values():
            axis["mechanical"]["max_speed_mm_s"] = 20
        self.fast_holds = []

    def alignment_fast_hold(self, axis, direction, speed, maximum, distance, duration, cancel_event=None):
        self.fast_holds.append((axis, direction, speed, maximum, distance, duration))
        return {"segment_mm": 150, "hold_exhausted": True, "stop_reason": "travel_limit"}


@pytest.mark.parametrize("speed", [20, 2.01])
def test_confirmed_fast_speed_dispatches_one_continuous_owned_hold(speed):
    motion, clock = FastJogMotion(), Clock()
    manager = AlignmentJog(motion, "session-1", clock=clock, start_threads=False)
    manager.configure({"session_token": "session-1", "speed_mm_s": speed, "max_speed_mm_s": 20,
                       "fast_motion_confirmed": True})
    start(manager)
    manager._step_once()
    assert motion.steps == []
    assert motion.fast_holds == [("X", 0, speed, 20, None, None)]
    assert manager.snapshot()["status"] == "idle"
    assert manager.snapshot()["last_stop_reason"] == "travel_limit"
    assert motion.stops == 1


@pytest.mark.parametrize("confirmation", [False, None, 1, "true"])
def test_fast_speed_requires_explicit_clearance_confirmation(confirmation):
    motion = FastJogMotion()
    manager = AlignmentJog(motion, "session-1", start_threads=False)
    with pytest.raises(AutomationError):
        manager.configure({"session_token": "session-1", "speed_mm_s": 20, "max_speed_mm_s": 20,
                           "fast_motion_confirmed": confirmation})
    assert not motion.fast_holds and not motion.steps


def test_fine_speed_with_a_20mm_ceiling_still_uses_original_finite_steps():
    motion = FastJogMotion()
    manager = AlignmentJog(motion, "session-1", start_threads=False)
    manager.configure({"session_token": "session-1", "speed_mm_s": 0.5, "max_speed_mm_s": 20,
                       "fast_motion_confirmed": True})
    start(manager)
    manager._step_once()
    assert not motion.fast_holds
    assert motion.steps[0]["max_speed_mm_s"] == 2
    manager.stop(command())
