import copy
import functools
import inspect
import json
from pathlib import Path

import pytest

from pa_host import keyboard_fast_jog
from pa_host.alignment_jog import AlignmentJog
from pa_host.automation import AutomationError
from pa_host.emm_motion import SharedEmmBus
from pa_host.guided_automation import GuidedAutomation
from test_emm_motion import AXES
from test_keyboard_responsiveness import SummarySerial


CASES = json.loads((Path(__file__).parent / "fixtures" / "w-after-three-captures-20261024.json").read_text())


class CapturedHoldSerial(SummarySerial):
    def __init__(self, case):
        super().__init__(timeout=.2, write_timeout=.2)
        self.case = case
        self.replaying = False
        self.move_frames = []
        self.feedback_index = 0

    def write(self, frame):
        address, opcode = frame[:2]
        if self.replaying and opcode == 0xFD:
            expected = self.case["commands"][len(self.move_frames)]
            assert frame.hex() == expected["command"]
            self.move_frames.append(frame)
            self.writes.append(frame)
            self.feedback_index = 0
            self.targets[0x33][address] = expected["target"]
            self.buffer.extend(bytes([address, opcode, 2, 0x6B]))
            return len(frame)
        if self.replaying and self.move_frames and opcode == 0x43 and address == 2:
            readings = self.case["commands"][len(self.move_frames) - 1]["feedback"]
            assert self.feedback_index < len(readings), "Unexpected read beyond the captured motion trace"
            reading = readings[self.feedback_index]
            self.feedback_index += 1
            self.positions[address], self.speeds[address], self.statuses[address] = reading["raw"], reading["rpm"], reading["status"]
        if self.replaying and opcode == 0xFE:
            self.replaying = False
            self.speeds = {1: 0, 2: 0}
            self.statuses = {1: 3, 2: 3}
            for targets in self.targets.values():
                targets.update(self.positions)
        return super().write(frame)


@pytest.mark.parametrize("iteration", range(5))
@pytest.mark.parametrize("case", CASES, ids=lambda case: case["name"])
@pytest.mark.parametrize("old_guard", [True, False], ids=["original-fails", "fixed-passes"])
def test_three_captures_then_w_reproduces_each_real_incident_deterministically(tmp_path, monkeypatch, case, old_guard, iteration):
    if old_guard:
        namespace = dict(vars(keyboard_fast_jog))
        source = inspect.getsource(keyboard_fast_jog.run_fast_alignment)
        old_source = source.replace("_target_representation_budget(counts_per_pulse)", "max(1, counts_per_pulse / 2)")
        assert old_source != source
        exec(compile(old_source, "<captured-original-target-guard>", "exec"), namespace)
        monkeypatch.setattr(keyboard_fast_jog, "run_fast_alignment", namespace["run_fast_alignment"])
    monkeypatch.setattr("pa_host.guided_automation.AlignmentJog",
                        functools.partial(AlignmentJog, start_threads=False, clock=lambda: 0))
    serial = CapturedHoldSerial(case)
    axes = copy.deepcopy(AXES)
    for settings in axes.values():
        settings.update(positive_direction=0, max_speed_mm_s=20)
    bus = SharedEmmBus("fake-three-point-w", 115200, axes, serial_factory=lambda **settings: serial)
    bus._command_gap_s = 0
    bus.connect()
    bus._responsive_keyboard = True
    service = GuidedAutomation(tmp_path, require_four_point=True)
    document = service.snapshot()["document"]
    document["bindings"]["motion"] = {"port": bus.port, "baud": bus.baud, "axes": copy.deepcopy(bus.axes)}
    service.save(document)
    service.begin_keyboard_calibration(bus, confirmed=True)
    for point in ("first", "last", "working"):
        for axis, raw_count in case["captures"][point].items():
            address = axes[axis]["address"]
            serial.positions[address] = raw_count
            for targets in serial.targets.values():
                targets[address] = raw_count
        service.capture_calibration(bus, point)
    captured = copy.deepcopy(service.snapshot()["manual"]["points"])
    token = service.snapshot()["manual"]["session_token"]
    service.configure_jog({"session_token": token, "speed_mm_s": 20, "max_speed_mm_s": 20, "fast_motion_confirmed": True})
    for axis, reading in case["initial"].items():
        address = axes[axis]["address"]
        serial.positions[address] = reading["raw"]
        for targets in serial.targets.values():
            targets[address] = reading["target"]
    serial.writes.clear()
    serial.replaying = True
    manager = service._alignment
    original_event = bus._event

    def event(kind, **fields):
        original_event(kind, **fields)
        if kind == "keyboard_fast_target" and len(serial.move_frames) == len(case["commands"]):
            manager._active["cancel_event"].set()

    bus._event = event
    press = {"session_token": token, "press_id": f"w-{iteration}"}
    service.start_jog({**press, "axis": "Z", "direction": "up"})
    try:
        if old_guard:
            with pytest.raises(AutomationError) as caught:
                manager._step_once()
            assert caught.value.code == "manual_target_mismatch"
            assert manager.snapshot()["status"] == "blocked"
        else:
            manager._step_once()
            service.stop_jog(press)
            assert manager.snapshot()["status"] == "idle"
            assert not bus.snapshot()["faulted"]
        assert service.snapshot()["manual"]["points"] == captured
        assert manager.snapshot()["stop_result"]["stop_confirmed"]
        assert [frame.hex() for frame in serial.move_frames] == [command["command"] for command in case["commands"]]
        assert not any(frame[1] in (0xF3, 0xF6, 0x0A, 0x9A) for frame in serial.writes)
    finally:
        if manager.snapshot()["status"] == "idle":
            service.freeze_alignment()
        manager.retire()
