import copy
import json
import time
from pathlib import Path

import pytest

from pa_host import automation
from test_guided_runner import guided_document, wait_terminal


def test_execution_descriptor_is_verified_and_steps_identified():
    document = guided_document()
    document["program"] = [{"op": "repeat", "count": 2, "body": document["program"]}]
    document["execution"] = {"mode": "repeat", "count": 2, "route": [2]}
    compiled = automation.compile_experiment(document)
    visits = [step for step in compiled["steps"] if step["op"] == "dwell"]
    assert [(step["cycle_index"], step["path_index"], step["visit_index"]) for step in visits] == [(1, 1, 1), (2, 1, 2)]
    assert len({step["fingerprint"] for step in visits}) == 2
    changed = copy.deepcopy(document)
    changed["execution"]["route"] = [3]
    with pytest.raises(automation.AutomationError):
        automation.compile_experiment(changed)


def test_repeated_custom_visits_have_independent_samples_and_manifest(tmp_path):
    document = guided_document()
    document["program"] *= 2
    document["execution"] = {"mode": "custom", "count": 1, "route": [2, 2]}
    compiled = automation.compile_experiment(document)
    controller = automation.AutomationController(tmp_path / "state", tmp_path / "runs", tmp_path, simulation_scale=0)
    controller.start(document, "simulate", compiled["plan_hash"], "custom")
    state = wait_terminal(controller)
    assert state["state"] == "completed"
    assert state["progress"]["percent"] == 100
    assert state["progress"]["completed_visits"] == 2
    directory = Path(state["manifest_path"]).parent
    samples = [json.loads(line) for line in (directory / "raw-samples.jsonl").read_text().splitlines()]
    assert {sample["visit_index"] for sample in samples} == {1, 2}
    assert len({sample["step_id"] for sample in samples}) == 2
    manifest = json.loads((directory / "manifest.json").read_text())
    assert manifest["execution"] == document["execution"]
    assert len(manifest["visits"]) == 2


@pytest.mark.parametrize("execution", [
    {"mode": "infinite", "count": 1, "route": [2]},
    {"mode": "once", "count": 2, "route": [2]},
    {"mode": "repeat", "count": 501, "route": [2]},
    {"mode": "custom", "count": 1, "route": [True]},
    {"mode": {}, "count": 1, "route": [2]},
    {"mode": [], "count": 1, "route": [2]},
    {"mode": True, "count": 1, "route": [2]},
    {"mode": "repeat", "count": True, "route": [2]},
    {"mode": "custom", "count": 1, "route": [2.0]},
    {"mode": "custom", "count": 1, "route": [0]},
])
def test_invalid_execution_is_rejected(execution):
    document = guided_document()
    document["execution"] = execution
    with pytest.raises(automation.AutomationError):
        automation.compile_experiment(document)


def test_legacy_steps_are_not_annotated():
    compiled = automation.compile_experiment(guided_document())
    assert all("visit_index" not in step for step in compiled["steps"])
    assert automation.compile_experiment(compiled["experiment"])["plan_hash"] == compiled["plan_hash"]


def test_start_ack_for_previous_visit_is_rejected(tmp_path):
    controller = automation.AutomationController(tmp_path / "state", tmp_path / "runs", tmp_path)
    controller._state["column_timing"] = {"step_id": "current"}
    with pytest.raises(automation.AutomationError, match="another visit"):
        controller._guided_started({"step_id": "previous", "slot": "A"}, {}, {"id": "current"})


def test_paused_cycle_counts_completed_safe_withdrawal(tmp_path):
    document = guided_document()
    document["execution"] = {"mode": "repeat", "count": 2, "route": [2]}
    document["program"] = [{"op": "repeat", "count": 2, "body": document["program"]}]
    controller = automation.AutomationController(tmp_path / "state", tmp_path / "runs", tmp_path, simulation_scale=0.001)
    compiled = automation.compile_experiment(document)
    controller.start(document, "simulate", compiled["plan_hash"], "paused-cycle")
    controller.pause()
    deadline = time.monotonic() + 3
    while controller.snapshot()["state"] != "paused" and time.monotonic() < deadline:
        time.sleep(0.002)
    state = controller.snapshot()
    try:
        assert state["state"] == "paused"
        assert state["progress"]["completed_visits"] == 1
        assert state["progress"]["completed_steps"] == state["step_index"] + 1
        assert state["progress"]["cycle_index"] == 1
        assert state["progress"]["remaining_s"] is None
        assert state["progress"]["estimated_finish_unix_s"] is None
    finally:
        controller.resume()
        final = wait_terminal(controller)
    assert final["state"] == "completed"
    assert final["progress"]["completed_visits"] == 2


def test_execution_cannot_assign_two_dwells_to_one_visit():
    document = guided_document()
    document["execution"] = {"mode": "custom", "count": 1, "route": [2, 2]}
    document["program"].insert(3, copy.deepcopy(document["program"][2]))
    document["program"].extend([{"op": "move_to_column", "column": 2}, {"op": "withdraw"}])
    with pytest.raises(automation.AutomationError, match="visit"):
        automation.compile_experiment(document)


def test_simulated_dwell_reports_bounded_partial_progress(tmp_path):
    document = guided_document()
    document["execution"] = {"mode": "once", "count": 1, "route": [2]}
    compiled = automation.compile_experiment(document)
    controller = automation.AutomationController(tmp_path / "state", tmp_path / "runs", tmp_path, simulation_scale=0)
    timings = []
    original_sample = controller._sample

    def observe_sample(*args, **kwargs):
        timings.append(controller.snapshot()["column_timing"]["elapsed_s"])
        return original_sample(*args, **kwargs)

    controller._sample = observe_sample
    controller.start(document, "simulate", compiled["plan_hash"], "partial-progress")
    assert wait_terminal(controller)["state"] == "completed"
    assert len(set(timings)) == 24
    assert all(0 < elapsed <= 12 for elapsed in timings)
