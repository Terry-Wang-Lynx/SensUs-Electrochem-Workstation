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


def test_shutdown_drains_and_closes_file_before_temporary_directory_cleanup(tmp_path):
    result = subprocess.run(
        [sys.executable, "-c", """
import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
sys.path[:] = json.loads(sys.argv[1])
from pa_host import keyboard_trace
handlers = []
original = keyboard_trace.logging.handlers.RotatingFileHandler
def tracked_handler(*args, **kwargs):
    handler = original(*args, **kwargs)
    handlers.append(handler)
    return handler
keyboard_trace.logging.handlers.RotatingFileHandler = tracked_handler
with TemporaryDirectory(dir=sys.argv[2]) as directory:
    keyboard_trace.configure(directory)
    for sequence in range(100):
        keyboard_trace.emit({'sequence': sequence})
    keyboard_trace.close()
    keyboard_trace.close()
    assert not keyboard_trace._worker.is_alive()
    assert handlers and all(handler.stream is None and handler._closed for handler in handlers)
    records = [json.loads(line) for line in (Path(directory) / 'keyboard-events.jsonl').read_text().splitlines()]
    assert [record['sequence'] for record in records] == list(range(100))
    keyboard_trace.emit({'sequence': 'after-close'})
    assert keyboard_trace.status()['queued'] == 0
""", json.dumps(sys.path), str(tmp_path)],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("failure", ["close_error", "full_queue"])
def test_shutdown_failures_do_not_hang_writer_or_control_thread(tmp_path, failure):
    result = subprocess.run(
        [sys.executable, "-c", """
import json, queue, sys, threading
sys.path[:] = json.loads(sys.argv[1])
from pa_host import keyboard_trace as trace
entered = threading.Event()
release = threading.Event()
class Stream:
    def write(self, message):
        entered.set()
        assert release.wait(5)
class Handler:
    stream = Stream()
    def shouldRollover(self, entry): return False
    def flush(self): pass
    def close(self):
        if sys.argv[3] == 'close_error': raise OSError('forced close failure')
trace.logging.handlers.RotatingFileHandler = lambda *args, **kwargs: Handler()
trace._queue = queue.Queue(maxsize=1)
trace.configure(sys.argv[2])
trace.emit({'sequence': 0})
assert entered.wait(2)
trace.emit({'sequence': 1})
closer = threading.Thread(target=trace.close)
closer.start()
for attempt in range(200):
    if trace._closed: break
    threading.Event().wait(.005)
assert trace._closed
emitted = threading.Event()
producer = threading.Thread(target=lambda: (trace.emit({'sequence': 2}), emitted.set()))
producer.start()
try:
    assert emitted.wait(.5), 'emit blocked behind shutdown queue insertion'
finally:
    release.set()
producer.join(2)
closer.join(2)
assert not closer.is_alive()
assert not trace._worker.is_alive()
assert trace._queue.unfinished_tasks == 0
assert trace.status()['write_error'] == ('forced close failure' if sys.argv[3] == 'close_error' else None)
""", json.dumps(sys.path), str(tmp_path), failure],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr
