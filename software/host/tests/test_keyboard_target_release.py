import pytest

from pa_host.alignment_jog import AlignmentJog
from pa_host.emm_motion import MotionError
from test_keyboard_alignment_driver import keyboard_bus


@pytest.mark.parametrize("axis,address,direction", [("X", 1, "left"), ("Z", 2, "up")])
@pytest.mark.parametrize("opcode", [0x33, 0x34])
@pytest.mark.parametrize("after_response", [False, True])
def test_release_during_target_read_keeps_same_session_usable(axis, address, direction, opcode, after_response):
    bus, serial, events, started = keyboard_bus()
    faults = []
    manager = AlignmentJog(bus, started["session_token"], start_threads=False,
                           on_blocked=lambda error, result: faults.append((error, result)))
    press = {"session_token": started["session_token"], "press_id": "release-in-target-read"}
    manager.start({**press, "axis": axis, "direction": direction})
    original_transact = bus._transact
    interrupted = False

    def release_in_target_read(body, response_length, **options):
        nonlocal interrupted
        if body[:2] == bytes([address, opcode]) and not interrupted:
            interrupted = True
            if after_response:
                original_transact(body, response_length, **options)
            manager._active["cancel_event"].set()
            result = bus.stop_keyboard_alignment()
            assert result["stop_confirmed"] is True
            raise MotionError("cancelled", "Motion cancelled", stop_result=result)
        return original_transact(body, response_length, **options)

    bus._transact = release_in_target_read
    manager._step_once()
    assert interrupted
    manager.stop(press)
    assert manager.snapshot()["status"] == "idle"
    assert manager.snapshot()["stop_result"]["stop_confirmed"] is True
    assert bus.snapshot()["faulted"] is False
    assert bus.snapshot()["manual"]["session_token"] == started["session_token"]
    assert faults == []
    assert not any(frame[1] in (0xFD, 0xF3, 0xF6) for frame in serial.writes)

    next_press = {**press, "press_id": "press-after-release"}
    manager.start({**next_press, "axis": axis, "direction": direction})
    manager._step_once()
    manager.stop(next_press)
    assert manager.snapshot()["status"] == "idle"
    assert bus.snapshot()["faulted"] is False
    assert faults == []
    assert sum(frame[1] == 0xFD for frame in serial.writes) == 1
