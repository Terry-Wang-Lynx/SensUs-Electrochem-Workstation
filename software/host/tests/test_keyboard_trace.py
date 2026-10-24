import json
import threading
import subprocess
import sys

import pytest

from pa_host import keyboard_trace
from pa_host.emm_motion import MotionError
from test_keyboard_responsiveness import responsive_bus


def test_async_trace_correlates_commands_and_first_motion_feedback(tmp_path):
    result = subprocess.run(
        [sys.executable, "-c",
         "import json, sys; from pathlib import Path; "
         "sys.path[:] = json.loads(sys.argv[1]); "
         "from test_keyboard_trace import assert_async_trace; "
         "assert_async_trace(Path(sys.argv[2]))",
         json.dumps(sys.path), str(tmp_path)],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def assert_async_trace(tmp_path):
    keyboard_trace.configure(tmp_path)
    bus, serial = responsive_bus()
    bus._keyboard_trace_context = {"press_id": "trace-test", "session_token": "test-session", "axis": "X",
                                   "baseline_count": serial.positions[1], "command_sent": False, "motion_observed": False}
    cancelled = threading.Event()
    serial.on_fast_move = lambda address: cancelled.set()
    with pytest.raises(MotionError):
        bus.alignment_fast_hold("X", 0, 20, 20, 0.4, 5, cancel_event=cancelled)
    keyboard_trace._queue.join()
    records = [json.loads(line) for line in (tmp_path / "keyboard-events.jsonl").read_text().splitlines()]
    assert any(item.get("press_id") == "trace-test" and item.get("opcode") == 253 for item in records)
    assert any(item.get("press_id") == "trace-test" and item.get("kind") == "keyboard_first_motion_feedback" for item in records)
    assert keyboard_trace.status()["write_error"] is None
