import threading
from types import SimpleNamespace

import test_guided_runner as guided_runner


def completed_worker():
    worker = threading.Thread(target=lambda: None)
    worker.start()
    worker.join()
    return worker


def install_handoff_clock(monkeypatch, transition):
    clock = SimpleNamespace(now=0, waits=0)

    def sleep(seconds):
        clock.now += seconds
        clock.waits += 1
        transition()

    clock.monotonic = lambda: clock.now
    clock.sleep = sleep
    monkeypatch.setattr(guided_runner, "time", clock)
    return clock


def test_wait_terminal_waits_for_published_but_unstarted_stop_worker(monkeypatch):
    state = {"state": "stopping", "busy": True, "stop_confirmed": False}
    controller = SimpleNamespace(_thread=threading.Thread(target=lambda: state.update(
        state="stopped", busy=False, stop_confirmed=True)), snapshot=lambda: dict(state))

    def finish_stop():
        controller._thread.start()
        controller._thread.join()

    clock = install_handoff_clock(monkeypatch, finish_stop)
    result = guided_runner.wait_terminal(controller)
    assert clock.waits == 1
    assert result == {"state": "stopped", "busy": False, "stop_confirmed": True}


def test_wait_terminal_rechecks_worker_identity_after_terminal_snapshot(monkeypatch):
    state = {"state": "completed", "busy": False, "stop_confirmed": True}
    controller = SimpleNamespace(_thread=completed_worker())
    replacement = threading.Thread(target=lambda: state.update(state="stopped", busy=False, stop_confirmed=True))

    def snapshot():
        observed = dict(state)
        if controller._thread is not replacement:
            controller._thread = replacement
            state.update(state="stopping", busy=True, stop_confirmed=False)
        return observed

    controller.snapshot = snapshot

    def finish_stop():
        replacement.start()
        replacement.join()

    clock = install_handoff_clock(monkeypatch, finish_stop)
    result = guided_runner.wait_terminal(controller)
    assert clock.waits == 1
    assert result == {"state": "stopped", "busy": False, "stop_confirmed": True}


def test_wait_terminal_preserves_terminal_unknown_stop_ownership(monkeypatch):
    state = {"state": "error", "busy": True, "stop_confirmed": False, "recovery_required": True}
    controller = SimpleNamespace(_thread=completed_worker(), snapshot=lambda: dict(state))
    clock = install_handoff_clock(monkeypatch, lambda: None)
    assert guided_runner.wait_terminal(controller) == state
    assert clock.waits == 0
