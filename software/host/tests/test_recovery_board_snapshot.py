import json
import time

import pytest

from pa_host.automation import AutomationController
from test_automation import automation, live_document


@pytest.mark.parametrize("failed", [None, "negative", "exception"])
def test_stale_global_stop_requires_fresh_board_proof_after_restart(tmp_path, failed):
    document = live_document(automation())
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    boards = {
        slot: {"address": f"board-{slot.lower()}", "phase": "retrying",
               "stop_confirmed": False, "output_state": "unknown", "attempt_id": slot}
        for slot in ("A", "B")
    }
    (state_dir / "automation.json").write_text(json.dumps({
        "experiment": document,
        "state": {"mode": "live", "state": "stopped", "stop_confirmed": True,
                  "run_id": "original", "boards": boards},
        "ownership": {"boards": [], "motion": False, "motion_config": None},
    }))
    calls = []

    def stop(address):
        calls.append(address)
        if address == "board-b" and failed == "exception":
            raise RuntimeError("No acknowledgement")
        return {"stop_confirmed": not (address == "board-b" and failed == "negative")}

    controller = AutomationController(state_dir, tmp_path / "runs", tmp_path,
                                      board_stop_factory=stop)
    before = controller.snapshot()
    assert before["run_id"] == "original"
    assert before["recovery_required"] and not before["stop_confirmed"]
    assert calls == []  # Loading persisted state does not command hardware.
    controller.stop()
    controller._thread.join(3)
    assert not controller._thread.is_alive()
    state = controller.snapshot()
    assert calls == ["board-a", "board-b"]
    assert state["boards"]["A"]["stop_confirmed"] is True
    assert state["boards"]["A"]["output_state"] == "stopped"
    assert state["boards"]["A"]["attempt_id"] == "A"
    assert state["boards"]["B"]["stop_confirmed"] is (failed is None)
    assert state["stop_confirmed"] is (failed is None)
    assert state["recovery_required"] is (failed is not None)


def test_recovery_stop_does_not_confirm_another_board(tmp_path):
    controller = AutomationController(tmp_path / "state", tmp_path / "runs", tmp_path)
    controller._state["boards"] = {
        "A": {"address": "board-a", "stop_confirmed": False, "output_state": "unknown"},
        "B": {"address": "board-b", "stop_confirmed": False, "output_state": "unknown"},
    }
    controller._record_recovery_board_stop("BOARD-A", True)
    assert controller.snapshot()["boards"]["A"]["stop_confirmed"]
    assert not controller.snapshot()["boards"]["B"]["stop_confirmed"]
