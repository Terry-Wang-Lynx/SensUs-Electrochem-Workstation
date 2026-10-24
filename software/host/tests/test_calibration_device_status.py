import copy

from test_calibrated_motion_policy import calibrated_inputs


def device_inputs():
    _compiled, motion, calibration = calibrated_inputs()
    calibration.update(usable=True, four_point=True)
    return {"four_point_required": True, "manual": {"active": False}, "calibration": calibration}, motion


def test_status_uses_current_complete_four_point_identity_and_fresh_feedback():
    from pa_host.automation_devices import calibration_status

    guided, motion = device_inputs()
    before = copy.deepcopy((guided, motion))
    result = calibration_status(guided, motion)
    assert result["ready"] is True and result["display_valid"] is True
    assert result["state"] == "ready"
    assert (guided, motion) == before
    motion["axes"]["X"]["speed_rpm"] = 2
    assert calibration_status(guided, motion)["ready"] is False
    assert calibration_status(guided, motion)["display_valid"] is True


def test_cached_registration_rejects_profile_token_reference_and_stale_damage():
    from pa_host.automation_devices import calibration_status

    for damage in ("token", "profile", "reference", "age", "manual", "old"):
        guided, motion = device_inputs()
        if damage == "token":
            motion["connection_token"] = "new-token"
        elif damage == "profile":
            motion["axes"]["X"]["config"]["microstep"] = 32
        elif damage == "reference":
            motion["axes"]["X"]["reference_count"] += 1
        elif damage == "age":
            motion["axes"]["Z"]["feedback_age_s"] = 5
        elif damage == "manual":
            guided["manual"]["active"] = True
        else:
            guided["calibration"]["points"].pop("working")
        result = calibration_status(guided, motion)
        assert result["ready"] is False, damage
        assert result["display_valid"] is False, damage


def test_sidebar_distinguishes_powered_release_and_single_calibration():
    from test_gui_dom_contract import _evaluate_chart_js

    result = _evaluate_chart_js(["highThroughputDeviceStatus"], """
const devices={motion:{connected:true,faulted:false,axes:{X:{enabled:false,raw_count:0,speed_rpm:0,telemetry_valid:true,feedback_age_s:0.1},Z:{enabled:true,raw_count:1,speed_rpm:0,telemetry_valid:true,feedback_age_s:0.1}}},calibration:{required:true,ready:false,state:'calibrating'}};
""", "highThroughputDeviceStatus(devices)")
    assert "已松轴" in result["AxisX"]["text"]
    assert "已锁轴" in result["AxisZ"]["text"]
    assert "实验标定" in result["Limits"]["text"]
    assert "四边界" not in result["Limits"]["text"]


def test_sidebar_marks_deferred_peer_feedback_without_hiding_real_faults():
    from test_gui_dom_contract import _evaluate_chart_js

    result = _evaluate_chart_js(["highThroughputDeviceStatus"], """
const devices={motion:{connected:true,faulted:false,automatic_motion_axis:'X',axes:{X:{enabled:true,raw_count:100,speed_rpm:60,telemetry_valid:true,feedback_age_s:0.1},Z:{enabled:true,raw_count:200,speed_rpm:0,telemetry_valid:true,feedback_age_s:20}}}};
const moving=highThroughputDeviceStatus(devices);
devices.motion.faulted=true;
const faulted=highThroughputDeviceStatus(devices);
""", "({moving,faulted})")
    assert "X 自动移动中" in result["moving"]["Motion"]["text"]
    assert "待 X 停稳后复核" in result["moving"]["AxisZ"]["text"]
    assert "20.0 s" in result["moving"]["AxisZ"]["text"]
    assert result["moving"]["AxisZ"]["tone"] == "warning"
    assert "X 自动移动中" in result["moving"]["CompactSummary"]["text"]
    assert "反馈未知" in result["faulted"]["AxisZ"]["text"]
    assert result["faulted"]["Motion"]["tone"] == "error"
