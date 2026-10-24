import pytest

from pa_host import gui_server
from pa_host.automation import AutomationError
from test_guided_integration import guided_app


@pytest.mark.parametrize("release,support", [(True, True), (False, True), (True, False), (1, "true")])
def test_strict_begin_forwards_literal_release_and_support_confirmations(guided_app, release, support):
    guided_app.guided_automation._require_four_point = True
    guided_app.guided_automation.begin_calibration.return_value = {"manual": {"active": True}}
    gui_server._automation_dispatch("POST", "guided/calibration/begin",
                                    {"release_confirmed": release, "z_supported": support}, {})
    guided_app.guided_automation.begin_calibration.assert_called_once_with(
        guided_app.automation._motion, release_confirmed=release is True, z_supported=support is True)


def test_strict_raw_motion_cannot_skip_trusted_four_points(guided_app):
    from pa_host.automation import default_experiment

    guided_app.guided_automation._require_four_point = True
    document = default_experiment()
    document["program"] = [{"op": "move_to_column", "column": 1}]
    with pytest.raises(AutomationError) as caught:
        gui_server._automation_dispatch("POST", "run", {"experiment": document, "mode": "live",
                                        "plan_hash": "irrelevant", "idempotency_key": "raw-motion"}, {})
    assert caught.value.code == "guided_calibration_required"
    guided_app.automation.start.assert_not_called()


def test_raw_simulation_does_not_need_a_physical_calibration(guided_app):
    from pa_host.automation import default_experiment

    guided_app.guided_automation._require_four_point = True
    document = default_experiment()
    gui_server._automation_dispatch("POST", "run", {"experiment": document, "mode": "simulate",
                                    "plan_hash": "simulated", "idempotency_key": "simulation"}, {})
    guided_app.automation.start.assert_called_once()
