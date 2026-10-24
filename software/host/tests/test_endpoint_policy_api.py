import copy

import pytest

from pa_host import gui_server
from pa_host.experiment_progress import Progress
from test_guided_integration import guided_app


def test_automatic_endpoint_policy_is_visible_without_motion(guided_app):
    capabilities = gui_server._automation_dispatch("GET", "capabilities", None, {})
    policy = capabilities["automatic_motion_bus"]["endpoint_approach"]
    assert policy["final_speed_cap_mm_s"] == 0.5
    assert policy["interior_clearance_mm"] == 1
    assert policy["requested_target_unchanged"] is True
    assert policy["guard_tolerance_unchanged"] is True
    assert policy["keyboard_jog_affected"] is False
    guided_app.automation._motion.assert_not_called()


def test_remaining_estimate_includes_slow_landing_with_original_target():
    calibration = {"x_first_mm": 1, "x_last_mm": 100, "z_safe_mm": 1, "z_working_mm": 7,
                   "motion": {"axes": {axis: {"lead_mm": 2} for axis in ("X", "Z")}},
                   "reference": {"profiles": {axis: {"config": {"pulses_per_rev": 3200,
                       "position_window_deg": 0.8}} for axis in ("X", "Z")}}}
    steps = [{"id": "first", "op": "move_x", "column": 1,
              "resolved_params": {"axis": "X", "target_mm": 1, "speed_mm_s": 20}}]
    before = copy.deepcopy(steps)
    progress = Progress(steps, {"X": 100, "Z": 1}, started=0, calibration=calibration)
    assert progress.snapshot("running", 0, None, 0)["remaining_s"] == pytest.approx(98 / 20 + 1 / 0.5)
    assert steps == before


def test_window_fault_message_is_actionable_and_preserves_raw_cause(tmp_path, monkeypatch):
    from test_automatic_arrival_settling import natural_controller
    from test_guided_runner import wait_terminal

    controller, bus, serial, record, document, compiled, checkpoints = natural_controller(tmp_path, monkeypatch)
    serial.drift_outside = True
    controller.start(document, "live", compiled["plan_hash"], "window-fault-message")
    result = wait_terminal(controller)
    assert result["state"] == "error" and result["stop_confirmed"] is True
    assert "X 轴位置反馈超出" in result["error"]["message"]
    assert "先确认双轴停止" in result["error"]["message"]
    assert result["error"]["cause"]["message"] == "X feedback left the four-point experiment window"
