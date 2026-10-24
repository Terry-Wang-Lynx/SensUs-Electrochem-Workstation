import json

from test_calibration_device_status import device_inputs


def test_single_experiment_calibration_automatically_maps_feedback_without_extra_setup(tmp_path):
    from pa_host.digital_twin import DigitalTwin

    guided, motion = device_inputs()
    record = guided["calibration"]
    record["points"]["first"] = {"raw_counts": {"X": 327680, "Z": 0}}
    record["points"]["last"] = {"raw_counts": {"X": 3571712, "Z": 0}}
    record["points"]["working"] = {"raw_counts": {"X": 0, "Z": 262144}}
    motion["axes"]["X"]["raw_count"] = 327680
    motion["axes"]["Z"]["raw_count"] = 65536
    geometry = tmp_path / "geometry.json"
    geometry.write_text(json.dumps({"schema_version": "1.0", "units": "mm",
                                   "reference_pose": {"x_offset_mm": 49.5, "lift_mm": 35}}))
    twin = DigitalTwin(tmp_path, geometry)
    result = twin.snapshot_experiment(motion, guided)
    assert result["state"] == "synced"
    assert result["pose"]["scene_x_mm"] == 0
    assert result["pose"]["lift_mm"] == 6
    assert result["registration"]["version"] == "digital-twin-experiment-1"
    assert result["registration"]["calibration_id"] == record["id"]
    assert not twin.path.exists()
    motion["axes"]["X"]["raw_count"] += 32768
    assert twin.snapshot_experiment(motion, guided)["pose"]["scene_x_mm"] == -1
    record["points"]["last"]["raw_counts"]["X"] = -2916352
    assert twin.snapshot_experiment(motion, guided)["pose"] is None
    record["points"]["last"]["raw_counts"]["X"] = 3571712
    motion["connection_token"] = "after-reconnect"
    assert twin.snapshot_experiment(motion, guided)["pose"] is None


def test_gui_uses_automatic_experiment_basis_without_legacy_limit_registration():
    from test_automation_gui import evaluate_browser
    from test_live_twin_gui import TWIN_BOOTSTRAP

    bootstrap = TWIN_BOOTSTRAP + """
PAHighThroughputDevices.snapshot.calibration={required:true,ready:true,display_valid:true};
const experimentFetch=fetch;
globalThis.fetch=async(path,options)=>path.endsWith('/guided')?limitsResponse({document:experiment,four_point_required:true,
calibration:{id:'same-four-point',usable:true,four_point:true,connection_token:twinLimitRecord.connection_token,points:{first:{},last:{},working:{},safe:{}}},manual:{active:false}}):experimentFetch(path,options);
twinSnapshot={state:'synced',registered:true,fresh:true,error:null,
registration:{version:'digital-twin-experiment-1',calibration_id:'same-four-point',binding:cloneTwin(twinLimitRecord)},
pose:{scene_x_mm:0,scene_z_mm:-29,x_center_mm:49.5,lift_mm:6,feedback_age_s:{X:0.1,Z:0.1},source:'encoder_feedback'}};
"""
    result = evaluate_browser("""
(async()=>{HighThroughput.activate();await tick();console.log(JSON.stringify({
status:elements.get('htTwinStatus').textContent,feedback:sceneCalls.filter(call=>call[0]==='feedback'),writes:requests.filter(request=>request.body)}));})();
""", before_source=bootstrap)
    assert "实验四点基准" in result["status"]
    assert result["feedback"]
    assert result["writes"] == []
