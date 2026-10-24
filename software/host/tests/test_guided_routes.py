import copy

import pytest

from pa_host.automation import AutomationError, compile_experiment
from pa_host.guided_automation import GuidedAutomation, _translated, _validate_document, default_guided_experiment
from test_guided_automation import calibrated
from test_guided_integration import dispatch, guided_app


def routed_document(mode="custom", route=None, count=1):
    document = default_guided_experiment()
    document["schema_version"] = "guided-1.1"
    document["execution"] = {"mode": mode, "count": count, "route": route}
    document["defaults"]["dwell_s"] = 5
    for column in document["columns"]:
        column["duration_s"] = 5
    return document


def visits(compiled):
    return [step["column"] for step in compiled["steps"] if step["op"] == "dwell"]


def test_legacy_document_and_plan_are_unchanged_by_validation():
    document = default_guided_experiment()
    before = compile_experiment(_translated(document))
    validated = _validate_document(document)
    assert validated == document and "execution" not in validated
    assert compile_experiment(_translated(validated)) == before


def test_three_rounds_of_selected_columns_include_safe_wrap_and_unique_visits(tmp_path):
    document = routed_document("repeat", count=3)
    route = [1, 3, 5, 7, 9]
    for column in document["columns"]:
        column["enabled"] = column["index"] in route
    service = GuidedAutomation(tmp_path)
    service.save(document)
    compiled = service.compile(document)
    assert visits(compiled) == route * 3
    assert compiled["experiment"]["execution"] == {"mode": "repeat", "count": 3, "route": route}
    assert len({step["id"] for step in compiled["steps"]}) == len(compiled["steps"])
    assert [step["cycle_index"] for step in compiled["steps"] if step["op"] == "dwell"] == [1] * 5 + [2] * 5 + [3] * 5
    for index, step in enumerate(compiled["steps"]):
        if step["op"] == "move_x":
            assert compiled["steps"][index - 1]["op"] == "move_z"
            assert compiled["steps"][index - 1]["resolved_params"]["purpose"] == "safe_before_x"
    assert compiled["steps"][-1]["column"] == 9
    assert compiled["steps"][-1]["resolved_params"]["purpose"] == "withdraw"


def test_custom_order_preserves_arbitrary_first_column_duplicates_and_parameters(tmp_path):
    route = [9, 3, 3, 12, 1, 7, 5, 4, 3, 1]
    document = routed_document(route=route)
    document["columns"][2]["windows"] = {"A": {"start_s": 1, "duration_s": 3}}
    before = copy.deepcopy(document)
    service = GuidedAutomation(tmp_path)
    service.save(document)
    compiled = service.compile(document)
    assert visits(compiled) == route
    assert len(compiled["experiment"]["columns"]) == len(set(route))
    assert document == before and service.snapshot()["document"] == before
    dwells = [step for step in compiled["steps"] if step["op"] == "dwell"]
    assert all(step["resolved_params"]["seconds"] == 5 for step in dwells)
    assert all(step["resolved_params"]["windows"]["A"] == {"start_s": 1, "duration_s": 3}
               for step in dwells if step["column"] == 3)
    assert [step["visit_index"] for step in dwells] == list(range(1, len(route) + 1))


@pytest.mark.parametrize("route", [[], [0], [13], [True], [1.0], ["1"], None, "13579"])
def test_invalid_custom_routes_are_rejected_before_any_run(route):
    with pytest.raises(AutomationError) as caught:
        _validate_document(routed_document(route=route))
    assert caught.value.path.startswith("execution")


@pytest.mark.parametrize("mode,count,route", [
    ("infinite", 1, None), ("repeat", True, None), ("repeat", 0, None), ("repeat", 501, None),
    ("once", 2, None), ("custom", 2, [1]), ("once", 1, [1]), ("repeat", 3, [1]),
])
def test_invalid_mode_count_and_ambiguous_route_combinations_are_rejected(mode, count, route):
    with pytest.raises(AutomationError) as caught:
        _validate_document(routed_document(mode, route, count))
    assert caught.value.path.startswith("execution")


def test_custom_path_cannot_run_a_disabled_column():
    document = routed_document(route=[1, 3])
    document["columns"][2]["enabled"] = False
    with pytest.raises(AutomationError) as caught:
        _validate_document(document)
    assert caught.value.path.startswith("execution.route")


def test_new_schema_requires_execution_and_legacy_cannot_hide_an_execution_extension():
    document = routed_document("once")
    document.pop("execution")
    with pytest.raises(AutomationError):
        _validate_document(document)
    document = routed_document("repeat", count=2)
    document["schema_version"] = "guided-1.0"
    with pytest.raises(AutomationError):
        _validate_document(document)


def test_routes_and_counts_change_the_preview_hash():
    original = routed_document(route=[1, 3, 5])
    reordered = copy.deepcopy(original)
    reordered["execution"]["route"] = [5, 3, 1]
    assert compile_experiment(_translated(_validate_document(original)))["plan_hash"] != compile_experiment(
        _translated(_validate_document(reordered)))["plan_hash"]


def test_repetition_retains_existing_expanded_plan_limit():
    document = routed_document("repeat", count=100)
    for column in document["columns"]:
        column["enabled"] = column["index"] == 1
    with pytest.raises(AutomationError) as caught:
        _validate_document(document)
    assert caught.value.code == "plan_limit"


def test_changing_mode_route_or_count_does_not_invalidate_saved_calibration(tmp_path):
    service, document, motion = calibrated(tmp_path)
    calibration = copy.deepcopy(service.snapshot()["calibration"])
    document.update(schema_version="guided-1.1", execution={"mode": "custom", "count": 1, "route": [7, 2, 7]})
    service.save(document)
    compiled = service.compile(document, mode="live", motion=motion)
    assert visits(compiled) == [7, 2, 7]
    assert service.snapshot()["calibration"] == calibration


def test_capabilities_advertise_finite_modes_and_order_preservation(guided_app):
    capabilities = dispatch("capabilities", method="GET")["guided_execution"]
    assert capabilities["modes"] == ["once", "repeat", "custom"]
    assert capabilities["document_versions"] == ["guided-1.0", "guided-1.1"]
    assert capabilities["preserve_order_and_duplicates"] is True


def test_real_api_simulation_uses_exact_route_and_rejects_old_preview(guided_app, tmp_path):
    from pa_host.automation import AutomationController
    from test_guided_runner import wait_terminal

    guided_app.guided_automation = GuidedAutomation(tmp_path / "guided")
    guided_app.automation = AutomationController(tmp_path / "state", tmp_path / "runs", tmp_path,
                                                simulation_scale=0)
    document = routed_document(route=[9, 3, 3, 1])
    dispatch("guided/experiment", {"document": document})
    preview = dispatch("guided/preview", {"document": document, "mode": "simulate"})
    altered = copy.deepcopy(document)
    altered["execution"]["route"] = [1, 3, 9]
    with pytest.raises(AutomationError) as caught:
        dispatch("guided/run", {"document": altered, "mode": "simulate",
                               "plan_hash": preview["plan_hash"], "idempotency_key": "stale-route"})
    assert caught.value.code == "plan_changed"
    assert guided_app.automation.snapshot()["state"] == "idle"
    dispatch("guided/run", {"document": document, "mode": "simulate",
                           "plan_hash": preview["plan_hash"], "idempotency_key": "custom-route"})
    result = wait_terminal(guided_app.automation)
    assert result["state"] == "completed" and result["progress"]["percent"] == 100
    assert result["progress"]["completed_visits"] == 4
    assert result["position"]["X"] == preview["experiment"]["defaults"]["x_first_mm"]
