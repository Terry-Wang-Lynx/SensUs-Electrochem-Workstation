"""Bounded asynchronous keyboard/motor timeline; never writes on control threads."""

import copy
import json
import logging.handlers
import queue
import threading
import time
from pathlib import Path


_queue = queue.Queue(maxsize=4096)
_path = None
_dropped = 0
_error = None


def configure(directory):
    global _path
    if _path is not None:
        return
    _path = Path(directory) / "keyboard-events.jsonl"
    threading.Thread(target=_write, name="keyboard-trace-writer", daemon=True).start()


def emit(event):
    global _dropped
    if _path is None:
        return
    record = {"host_unix_s": time.time(), "host_monotonic_s": time.monotonic(),
              "dropped_total": _dropped, **copy.deepcopy(event)}
    try:
        _queue.put_nowait(record)
    except queue.Full:
        _dropped += 1


def status():
    return {"path": str(_path) if _path else None, "queued": _queue.qsize(),
            "dropped_total": _dropped, "write_error": _error}


def _write():
    global _error
    handler = None
    while True:
        record = _queue.get()
        try:
            if handler is None:
                _path.parent.mkdir(parents=True, exist_ok=True)
                handler = logging.handlers.RotatingFileHandler(_path, maxBytes=10 * 1024 * 1024,
                                                               backupCount=5, encoding="utf-8")
            message = json.dumps(record, ensure_ascii=False)
            entry = logging.LogRecord("keyboard", logging.INFO, "", 0, message, (), None)
            if handler.shouldRollover(entry):
                handler.doRollover()
            if handler.stream is None:
                handler.stream = handler._open()
            handler.stream.write(message + "\n")
            handler.flush()
        except Exception as error:
            _error = str(error)
        finally:
            _queue.task_done()
