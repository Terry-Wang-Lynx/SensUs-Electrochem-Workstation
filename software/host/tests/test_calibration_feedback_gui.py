import json
import re
from pathlib import Path

import pytest

from test_automation_gui import evaluate_browser
from test_keyboard_alignment_gui import KEYBOARD_BOOTSTRAP


CAPTURED_API = json.loads((Path(__file__).parent / "fixtures" / "calibration-finish-failure-20261024.json").read_text())
CAPTURED_BOOTSTRAP = KEYBOARD_BOOTSTRAP + """
const actual=ACTUAL;
sessionGuided=actual.guided;controllerSnapshot=actual.state;
const receivedDevices=()=>({available:true,error:null,received_at_ms:Date.now(),snapshot:actual.devices});
globalThis.PAHighThroughputDevices=receivedDevices();
let resultGate=null,resultFailure=false;
const actualFetch=globalThis.fetch;
const backendHealthy=()=>{
 sessionGuided.manual.alignment.status='idle';controllerSnapshot.busy=false;
 for(const motion of [controllerSnapshot.motion,actual.devices.motion])for(const axis of ['X','Z']){motion.axes[axis].telemetry_valid=true;motion.axes[axis].feedback_age_s=0.05;}
 globalThis.PAHighThroughputDevices=receivedDevices();globalThis.HighThroughput?.acceptDevices(globalThis.PAHighThroughputDevices);
};
globalThis.fetch=async(path,options)=>{
 const route=path.replace('/api/automation/v1/','');
 if(route==='devices')return limitsResponse(actual.devices);
 if(route==='guided/calibration/finish'||route==='guided/calibration/capture'){
  requests.push({route,body:JSON.parse(options.body)});if(resultGate)await resultGate;
  if(resultFailure){sessionGuided.manual.alignment.status='frozen';return {ok:false,status:400,json:async()=>({error:{code:'invalid_value',message:'must be between 0 and 150',path:'calibration.last.X',request_id:'9af54fb50b8e470d9f32654516306019'}})};}
  if(route.endsWith('/finish')){sessionGuided.calibration={...completeCalibration(),connection_token:sessionGuided.manual.connection_token,points:JSON.parse(JSON.stringify(sessionGuided.manual.points))};sessionGuided.manual.active=false;sessionGuided.manual.status='complete';sessionGuided.document.bindings.motion.axes.X.travel_mm=236.9166259765625;sessionGuided.document.bindings.motion.axes.Z.travel_mm=80.41943359375;actual.devices.calibration.ready=true;}
  return limitsResponse(sessionGuided);
 }
 return actualFetch(path,options);
};
const observed=()=>({banner:elements.get('htCalibrationCompletion').textContent,hidden:elements.get('htCalibrationCompletion').hidden,
 failure:elements.get('htCalibrationCompletion').classList.contains('ht-calibration-failed'),
 progress:elements.get('htCalibrationProgress').textContent,
 buttons:['htCaptureFirst','htCaptureLast','htCaptureWorking','htCaptureSafe'].map(id=>({text:elements.get(id).textContent,recorded:elements.get(id).classList.contains('ht-calibration-recorded'),disabled:elements.get(id).disabled}))});
const openActual=async()=>{root.classList.toggle('active',true);HighThroughput.activate();await tick();await click('htCalibrationToggle');};
""".replace("ACTUAL", json.dumps(CAPTURED_API, ensure_ascii=False))


def evaluate_captured(script):
    return evaluate_browser(script, before_source=CAPTURED_BOOTSTRAP)


FEEDBACK_BOOTSTRAP = KEYBOARD_BOOTSTRAP + """
sessionGuided.manual={...sessionGuided.manual,active:true,status:'aligning',connection_token:sessionToken};
const captureControls={first:'htCaptureFirst',last:'htCaptureLast',working:'htCaptureWorking',safe:'htCaptureSafe'};
const captured=point=>{const button=elements.get(captureControls[point]);return {text:button.textContent,title:button.title,
 recorded:button.classList.contains('ht-calibration-recorded'),disabled:button.disabled};};
const feedback=()=>({buttons:Object.fromEntries(Object.keys(captureControls).map(point=>[point,captured(point)])),
 progress:elements.get('htCalibrationProgress').textContent,summary:elements.get('htCalibrationSummary').textContent,
 banner:elements.get('htCalibrationCompletion')?.textContent||'',hidden:elements.get('htCalibrationCompletion')?.hidden!==false});
const openCalibration=async()=>{root.classList.toggle('active',true);HighThroughput.activate();await tick();await click('htCalibrationToggle');};
const captureFetch=globalThis.fetch;let captureGate=null,captureError=false,recaptureCount=null;
globalThis.fetch=async(path,options)=>{
 const route=path.replace('/api/automation/v1/','');
 if(route==='guided/calibration/capture'){
  if(captureGate)await captureGate;
  if(captureError)return {ok:false,status:409,json:async()=>({error:{code:'capture_failed',message:'读取失败，请重试'}})};
  const response=await captureFetch(path,options);
  if(recaptureCount!==null){const point=JSON.parse(options.body).point;sessionGuided.manual.points[point].raw_counts[point==='first'||point==='last'?'X':'Z']=recaptureCount;return limitsResponse(sessionGuided);}
  return response;
 }
 if(route==='guided/calibration/cancel'){
  requests.push({route,body:JSON.parse(options.body)});sessionGuided.manual={...sessionGuided.manual,active:false,status:'cancelled'};return limitsResponse(sessionGuided);
 }
 return captureFetch(path,options);
};
"""


def evaluate_feedback(script):
    return evaluate_browser(script, before_source=FEEDBACK_BOOTSTRAP)


def test_page_load_restores_backend_captured_points_with_numeric_feedback():
    result = evaluate_feedback("""
(async()=>{sessionGuided.manual.points.first=capturedPoint('first');await openCalibration();
 console.log(JSON.stringify({feedback:feedback(),writes:requests.filter(request=>request.body!==null)}));})();
""")
    assert result["feedback"]["buttons"]["first"]["recorded"] is True
    assert "✓" in result["feedback"]["buttons"]["first"]["text"]
    assert "已记录" in result["feedback"]["buttons"]["first"]["text"]
    assert "重新读取" in result["feedback"]["buttons"]["first"]["text"]
    assert "32768" in result["feedback"]["buttons"]["first"]["title"]
    assert "1 / 4" in result["feedback"]["progress"]
    assert result["feedback"]["buttons"]["last"]["recorded"] is False
    assert result["writes"] == []


def test_capture_waits_for_backend_response_before_green_and_progress():
    result = evaluate_feedback("""
(async()=>{await openCalibration();let release;captureGate=new Promise(resolve=>release=resolve);
 await click('htCaptureWorking');const pending=feedback();release();await tick();await tick();
 console.log(JSON.stringify({pending,done:feedback()}));})();
""")
    assert result["pending"]["buttons"]["working"]["recorded"] is False
    assert result["pending"]["buttons"]["working"]["disabled"] is True
    assert result["done"]["buttons"]["working"]["recorded"] is True
    assert "Z" in result["done"]["buttons"]["working"]["title"]
    assert "360448" in result["done"]["buttons"]["working"]["title"]


def test_all_four_captured_require_explicit_finish_before_saved_completion():
    result = evaluate_feedback("""
(async()=>{await openCalibration();for(const id of Object.values(captureControls))await click(id);
 const waiting=feedback();await click('htCalibrationFinish');
 console.log(JSON.stringify({waiting,finished:feedback()}));})();
""")
    assert all(button["recorded"] for button in result["waiting"]["buttons"].values())
    assert "4 / 4" in result["waiting"]["progress"]
    assert "完成四点标定" in result["waiting"]["banner"]
    assert "待" in result["waiting"]["banner"]
    assert "已完成" not in result["waiting"]["banner"]
    assert result["waiting"]["hidden"] is False
    assert all(button["recorded"] and button["disabled"] for button in result["finished"]["buttons"].values())
    assert "已完成并保存" in result["finished"]["banner"]
    assert "当前连接可用" in result["finished"]["banner"]


def test_failed_capture_does_not_green_missing_point_or_remove_prior_success():
    result = evaluate_feedback("""
(async()=>{sessionGuided.manual.points.first=capturedPoint('first');await openCalibration();captureError=true;
 await click('htCaptureLast');console.log(JSON.stringify({feedback:feedback(),error:elements.get('htError').textContent}));})();
""")
    assert result["feedback"]["buttons"]["first"]["recorded"] is True
    assert result["feedback"]["buttons"]["last"]["recorded"] is False
    assert "1 / 4" in result["feedback"]["progress"]
    assert "读取失败" in result["error"]


def test_successful_recapture_replaces_raw_count_without_incrementing_progress():
    result = evaluate_feedback("""
(async()=>{sessionGuided.manual.points.first=capturedPoint('first');await openCalibration();recaptureCount=-7654;
 await click('htCaptureFirst');console.log(JSON.stringify(feedback()));})();
""")
    assert result["buttons"]["first"]["recorded"] is True
    assert "-7654" in result["buttons"]["first"]["title"]
    assert "32768" not in result["buttons"]["first"]["title"]
    assert "1 / 4" in result["progress"]


@pytest.mark.parametrize("change", [
    "sessionGuided.manual.active=false;sessionGuided.manual.status='cancelled';",
    "sessionGuided.manual.status='blocked';sessionGuided.manual.error={code:'manual_interrupted',message:'Interrupted'};",
    "sessionGuided.manual.connection_token='different-connection';",
    "sessionGuided.manual.session_token='different-session';",
    "sessionGuided.manual.points={};sessionGuided.manual.session_token='new-session';sessionGuided.manual.alignment.session_token='new-session';",
    "sessionGuided.manual.points.first={raw_counts:{X:null,Z:32768}};",
])
def test_cancel_reset_invalid_point_or_stale_session_does_not_preserve_green(change):
    result = evaluate_feedback("""
(async()=>{sessionGuided.manual.points.first=capturedPoint('first');await openCalibration();const before=feedback();
 CHANGE
 HighThroughput.activate();await tick();console.log(JSON.stringify({before,after:feedback()}));})();
""".replace("CHANGE", change))
    assert result["before"]["buttons"]["first"]["recorded"] is True
    assert not any(button["recorded"] for button in result["after"]["buttons"].values())
    assert "已完成并保存" not in result["after"]["banner"]


def test_cancel_and_new_begin_clear_existing_recorded_capture_feedback():
    result = evaluate_feedback("""
(async()=>{sessionGuided.manual.points.first=capturedPoint('first');await openCalibration();await click('htCalibrationCancel');const cancelled=feedback();
 elements.get('htCalibrationMotionConfirmed').checked=true;change('htCalibrationMotionConfirmed');await click('htCalibrationBegin');
 console.log(JSON.stringify({cancelled,restarted:feedback()}));})();
""")
    for state in result.values():
        assert not any(button["recorded"] for button in state["buttons"].values())
        assert "0 / 4" in state["progress"]


@pytest.mark.parametrize("change", [
    "sessionGuided.calibration.usable=false;sessionGuided.calibration.status='stale';",
    "sessionGuided.manual.status='cancelled';",
    "sessionGuided.manual.status='blocked';",
    "sessionGuided.calibration.connection_token='different-connection';",
])
def test_saved_points_become_neutral_when_calibration_is_stale_or_cancelled(change):
    result = evaluate_feedback("""
(async()=>{sessionGuided.manual.active=false;sessionGuided.manual.status='complete';sessionGuided.calibration=completeCalibration();await openCalibration();const saved=feedback();
 CHANGE
 HighThroughput.activate();await tick();
 console.log(JSON.stringify({saved,stale:feedback()}));})();
""".replace("CHANGE", change))
    assert all(button["recorded"] for button in result["saved"]["buttons"].values())
    assert not any(button["recorded"] for button in result["stale"]["buttons"].values())
    assert "已完成并保存" not in result["stale"]["banner"]


def test_finish_failure_never_displays_saved_completion():
    result = evaluate_feedback("""
(async()=>{await openCalibration();for(const id of Object.values(captureControls))await click(id);finishFailure=true;
 await click('htCalibrationFinish');console.log(JSON.stringify(feedback()));})();
""")
    assert "已完成并保存" not in result["banner"]
    assert not any(button["recorded"] for button in result["buttons"].values())


def test_actual_frozen_snapshot_retains_all_four_recorded_buttons_while_disabled():
    result = evaluate_captured("""
(async()=>{await openActual();console.log(JSON.stringify(observed()));})();
""")
    assert all(button["recorded"] and button["disabled"] for button in result["buttons"])
    assert "-4081441" in result["progress"]
    assert "3616307" in result["progress"]
    assert "3008197" in result["progress"]
    assert "438549" in result["progress"]


def test_disabled_recorded_capture_style_is_opaque_high_contrast():
    styles = (Path(__file__).parents[1] / "pa_host" / "gui" / "styles.css").read_text()
    rule = lambda selector: dict(item.strip().split(":", 1) for item in re.search(re.escape(selector) + r"\{([^}]+)\}", styles)[1].split(";") if item.strip())
    normal = rule(".ht-calibration-steps .ht-calibration-recorded")
    disabled = rule(".ht-calibration-steps .ht-calibration-recorded:disabled")
    assert float(disabled["opacity"]) == 1
    assert disabled["cursor"] == "not-allowed"
    assert normal["color"] in {"#fff", "#ffffff"}
    channels = [int(normal["background"].removeprefix("#")[offset:offset + 2], 16) / 255 for offset in (0, 2, 4)]
    linear = [value / 12.92 if value <= .04045 else ((value + .055) / 1.055) ** 2.4 for value in channels]
    luminance = sum(value * weight for value, weight in zip(linear, [.2126, .7152, .0722]))
    assert 1.05 / (luminance + .05) >= 7


@pytest.mark.parametrize("button,label", [("htCalibrationFinish", "保存"), ("htCaptureLast", "读取")])
def test_actual_snapshot_delayed_operation_stays_busy_through_cached_poll(button, label):
    result = evaluate_captured("""
(async()=>{backendHealthy();await openActual();let release;resultGate=new Promise(resolve=>release=resolve);
 await click(BUTTON);const initial=observed();HighThroughput.activate();await tick();const polled=observed();
 release();await tick();await tick();console.log(JSON.stringify({initial,polled,done:observed()}));})();
""".replace("BUTTON", json.dumps(button)))
    for state in [result["initial"], result["polled"]]:
        assert "正在" in state["banner"] and label in state["banner"]
        assert "已完成" not in state["banner"]
    if button == "htCalibrationFinish":
        assert "已完成并保存" in result["done"]["banner"]


def test_actual_finish_failure_is_inline_preserves_points_and_retry_clears_old_error():
    result = evaluate_captured("""
(async()=>{backendHealthy();await openActual();resultFailure=true;await click('htCalibrationFinish');const failed=observed();
 HighThroughput.activate();await tick();const polled=observed();
 backendHealthy();HighThroughput.activate();await tick();resultFailure=false;let release;resultGate=new Promise(resolve=>release=resolve);await click('htCalibrationFinish');const retrying=observed();
 release();await tick();await tick();console.log(JSON.stringify({failed,polled,retrying,done:observed()}));})();
""")
    for state in [result["failed"], result["polled"]]:
        assert state["failure"] is True
        assert "失败" in state["banner"]
        assert "第 12 列 X" in state["banner"] and "150" in state["banner"]
        assert "保留" in state["banner"]
        assert all(button["recorded"] for button in state["buttons"])
    assert result["retrying"]["failure"] is False
    assert "150" not in result["retrying"]["banner"]
    assert "正在" in result["retrying"]["banner"]
    assert "已完成并保存" in result["done"]["banner"]


def test_stale_page_does_not_overwrite_server_board_bindings_on_begin():
    result = evaluate_captured("""
(async()=>{sessionGuided.manual.active=false;sessionGuided.manual.status='idle';backendHealthy();await openActual();
 sessionGuided.document.bindings.boards.A.address='server-front';sessionGuided.document.bindings.boards.B.address='server-rear';
 elements.get('htCalibrationMotionConfirmed').checked=true;change('htCalibrationMotionConfirmed');await click('htCalibrationBegin');
 console.log(JSON.stringify(requests.filter(item=>item.route==='guided/experiment').map(item=>item.body.document.bindings.boards)));})();
""")
    assert len(result) == 1
    assert result[0]["A"]["address"] == "server-front"
    assert result[0]["B"]["address"] == "server-rear"


def test_explicit_advanced_address_edit_is_kept_while_untouched_slot_uses_latest_binding():
    result = evaluate_captured("""
(async()=>{sessionGuided.manual.active=false;sessionGuided.manual.status='idle';backendHealthy();await openActual();
 input('htBoardAddress-A','chosen-front');await click('htApplyBindings');
 sessionGuided.document.bindings.boards.B.address='server-rear';await click('htSave');
 console.log(JSON.stringify(requests.filter(item=>item.route==='guided/experiment').at(-1).body.document.bindings.boards));})();
""")
    assert result["A"]["address"] == "chosen-front"
    assert result["B"]["address"] == "server-rear"


def test_cached_server_binding_refresh_preserves_other_unsaved_template_edits():
    result = evaluate_captured("""
(async()=>{sessionGuided.manual.active=false;sessionGuided.manual.status='idle';backendHealthy();await openActual();input('htName','Unsaved local name');
 sessionGuided.document.bindings.boards.A.address='server-front';HighThroughput.activate();await tick();
 console.log(JSON.stringify({document:JSON.parse(elements.get('htJson').value),field:elements.get('htBoardAddress-A').value}));})();
""")
    assert result["document"]["name"] == "Unsaved local name"
    assert result["document"]["bindings"]["boards"]["A"]["address"] == "server-front"
    assert result["field"] == "server-front"


@pytest.mark.parametrize("address_edit", [False, True])
def test_json_template_edit_preserves_server_binding_except_intentionally_changed_slot(address_edit):
    result = evaluate_captured("""
(async()=>{sessionGuided.manual.active=false;sessionGuided.manual.status='idle';backendHealthy();await openActual();
 const draft=JSON.parse(elements.get('htJson').value);draft.name='JSON local name';if(EDIT)draft.bindings.boards.A.address='chosen-front';input('htJson',JSON.stringify(draft));
 sessionGuided.document.bindings.boards.A.address='server-front';sessionGuided.document.bindings.boards.B.address='server-rear';HighThroughput.activate();await tick();await click('htApplyJson');await click('htSave');
 console.log(JSON.stringify(requests.filter(item=>item.route==='guided/experiment').at(-1).body.document));})();
""".replace("EDIT", json.dumps(address_edit)))
    assert result["name"] == "JSON local name"
    assert result["bindings"]["boards"]["A"]["address"] == ("chosen-front" if address_edit else "server-front")
    assert result["bindings"]["boards"]["B"]["address"] == "server-rear"


def test_successful_finish_merges_saved_software_extents_into_current_model():
    result = evaluate_captured("""
(async()=>{backendHealthy();await openActual();await click('htCalibrationFinish');
 const document=JSON.parse(elements.get('htJson').value);
 console.log(JSON.stringify({axes:document.bindings.motion.axes,name:document.name,columns:document.columns,
 fields:['X','Z'].map(axis=>elements.get('htAxis-'+axis+'-travel_mm').value)}));})();
""")
    assert result["axes"]["X"]["travel_mm"] == 236.9166259765625
    assert result["axes"]["Z"]["travel_mm"] == 80.41943359375
    assert result["fields"] == ["236.9166259765625", "80.41943359375"]
    assert result["name"] == CAPTURED_API["guided"]["document"]["name"]
    assert result["columns"] == CAPTURED_API["guided"]["document"]["columns"]


@pytest.mark.parametrize("error_code", ["geometry_error", "invalid_value", "calibration_height", "calibration_points"])
def test_persisted_geometry_error_is_inline_on_reload_and_clears_after_correction(error_code):
    result = evaluate_captured("""
(async()=>{sessionGuided.manual.error={code:ERROR_CODE,message:'第 12 列 X 与第 1 列不一致',path:'calibration.last.X'};await openActual();const failed=observed();
 sessionGuided.manual.error=null;sessionGuided.manual.points.last.raw_counts.X=3500000;HighThroughput.activate();await tick();
 console.log(JSON.stringify({failed,corrected:observed()}));})();
""".replace("ERROR_CODE", json.dumps(error_code)))
    assert result["failed"]["failure"] is True
    assert "第 12 列 X" in result["failed"]["banner"]
    assert result["corrected"]["failure"] is False
    assert "失败" not in result["corrected"]["banner"]


def test_later_backend_cancel_clears_old_capture_success_banner():
    result = evaluate_captured("""
(async()=>{backendHealthy();await openActual();await click('htCaptureLast');
 sessionGuided.manual.active=false;sessionGuided.manual.status='cancelled';HighThroughput.activate();await tick();
 console.log(JSON.stringify(observed()));})();
""")
    assert result["hidden"] is True
    assert "✓" not in result["banner"]


def test_persisted_geometry_error_supersedes_prior_successful_capture_notice():
    result = evaluate_captured("""
(async()=>{backendHealthy();await openActual();await click('htCaptureLast');
 sessionGuided.manual.error={code:'geometry_error',message:'四点几何校验失败',path:'calibration.last.X'};HighThroughput.activate();await tick();
 console.log(JSON.stringify(observed()));})();
""")
    assert result["failure"] is True
    assert "四点几何校验失败" in result["banner"]
