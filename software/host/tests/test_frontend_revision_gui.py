import json
from pathlib import Path

import pytest

from test_automation_gui import evaluate_browser
from test_keyboard_alignment_gui import KEYBOARD_BOOTSTRAP


BLOCKED_CAPTURE = json.loads((Path(__file__).parent / "fixtures" / "calibration-three-point-blocked-20261024.json").read_text())
BLOCKED_BOOTSTRAP = KEYBOARD_BOOTSTRAP + """
const blockedCapture=CAPTURE;
sessionGuided=blockedCapture.guided;controllerSnapshot=blockedCapture.state;
globalThis.PAHighThroughputDevices={available:true,error:null,received_at_ms:Date.now(),snapshot:blockedCapture.devices};
const capturedButtons=()=>['First','Last','Working','Safe'].map(point=>{const button=elements.get('htCapture'+point);return {
 text:button.textContent,title:button.title,green:button.classList.contains('ht-calibration-recorded'),
 retained:button.classList.contains('ht-calibration-retained'),disabled:button.disabled};});
""".replace("CAPTURE", json.dumps(BLOCKED_CAPTURE, ensure_ascii=False))

REVISION_BOOTSTRAP = KEYBOARD_BOOTSTRAP + """
const intervalCallbacks=[];globalThis.setInterval=(callback,delay)=>{intervalCallbacks.push({callback,delay});return intervalCallbacks.length;};
let health={diagnostic_session:'backend-1',frontend_revision:'revision-1',hardware_busy:false};
let healthReadError=false,reloads=0;globalThis.location={reload(){reloads++;}};
const versionFetch=globalThis.fetch;
globalThis.fetch=async(path,options)=>{
 if(path==='/api/health'){
  requests.push({route:'health',body:null});if(healthReadError)throw new Error('Server unavailable');
  return limitsResponse(health);
 }
 return versionFetch(path,options);
};
const pollVersion=async()=>{for(const timer of intervalCallbacks.filter(timer=>timer.delay===5000))timer.callback();await tick();};
const notice=()=>({supported:elements.has('htFrontendUpdate'),hidden:elements.get('htFrontendUpdate')?.hidden,
 disabled:elements.get('htFrontendReload')?.disabled,text:elements.get('htFrontendUpdateText')?.textContent,
 build:elements.get('htFrontendVersion')?.textContent,reloads});
"""


def test_actual_blocked_session_shows_three_retained_points_without_green_or_completion():
    result = evaluate_browser("""
(async()=>{root.classList.toggle('active',true);HighThroughput.activate();await tick();
 console.log(JSON.stringify({buttons:capturedButtons(),progress:elements.get('htCalibrationProgress').textContent,
 banner:elements.get('htCalibrationCompletion').textContent,writes:requests.filter(item=>item.body!==null)}));})();
""", before_source=BLOCKED_BOOTSTRAP)
    assert [button["retained"] for button in result["buttons"]] == [True, True, True, False]
    assert all(not button["green"] and button["disabled"] for button in result["buttons"])
    assert all("✓" in button["text"] and "待恢复核对" in button["text"] for button in result["buttons"][:3])
    assert "-4099551" in result["buttons"][0]["title"]
    assert "2950251" in result["buttons"][2]["title"]
    assert "3 / 4" in result["progress"]
    assert "已完成" not in result["banner"]
    assert result["writes"] == []


@pytest.mark.parametrize("change", [
    "sessionGuided.manual.active=false;sessionGuided.manual.status='cancelled';",
    "controllerSnapshot.motion.connection_token='new-connection';blockedCapture.devices.motion.connection_token='new-connection';",
    "sessionGuided.manual.session_token='new-session';",
])
def test_retained_points_do_not_follow_cancelled_or_different_session(change):
    result = evaluate_browser("""
(async()=>{root.classList.toggle('active',true);HighThroughput.activate();await tick();const before=capturedButtons();
 CHANGE
 HighThroughput.activate();await tick();console.log(JSON.stringify({before,after:capturedButtons()}));})();
""".replace("CHANGE", change), before_source=BLOCKED_BOOTSTRAP)
    assert sum(button["retained"] for button in result["before"]) == 3
    assert all(not button["retained"] and not button["green"] for button in result["after"])


@pytest.mark.parametrize("change", ["health.diagnostic_session='backend-2';", "health.frontend_revision='revision-2';"])
def test_backend_restart_or_frontend_change_warns_without_reload_or_hardware_write(change):
    result = evaluate_browser("""
(async()=>{HighThroughput.activate();await tick();await pollVersion();const original=notice();
 CHANGE
 await pollVersion();await pollVersion();console.log(JSON.stringify({original,updated:notice(),writes:requests.filter(item=>item.body!==null)}));})();
""".replace("CHANGE", change), before_source=REVISION_BOOTSTRAP)
    assert result["original"]["supported"] is True
    assert result["original"]["hidden"] is True
    assert "界面" in result["original"]["build"] and "2026-10-24" in result["original"]["build"]
    assert result["updated"]["hidden"] is False
    assert "后台已更新" in result["updated"]["text"]
    assert "不会清空后台采点" in result["updated"]["text"]
    assert result["updated"]["reloads"] == 0
    assert result["writes"] == []


def test_reload_requires_user_click_and_fresh_stop_confirmation():
    result = evaluate_browser("""
(async()=>{HighThroughput.activate();await tick();await pollVersion();health.frontend_revision='revision-2';controllerSnapshot.stop_confirmed=false;
 HighThroughput.activate();await tick();await pollVersion();if(!notice().supported){console.log(JSON.stringify({supported:false}));return;}
 await click('htFrontendReload');const blocked=notice();controllerSnapshot.stop_confirmed=true;HighThroughput.activate();await tick();
 await click('htFrontendReload');console.log(JSON.stringify({supported:true,blocked,after:notice(),writes:requests.filter(item=>item.body!==null)}));})();
""", before_source=REVISION_BOOTSTRAP)
    assert result["supported"] is True
    assert result["blocked"]["disabled"] is True and result["blocked"]["reloads"] == 0
    assert result["after"]["reloads"] == 1
    assert result["writes"] == []


def test_version_notice_never_stops_held_input_and_reload_waits_for_release():
    result = evaluate_browser("""
(async()=>{root.classList.toggle('active',true);sessionGuided.manual.active=true;sessionGuided.manual.status='aligning';HighThroughput.activate();await tick();await click('htCalibrationToggle');await pollVersion();
 dispatchKey('keydown','a');await tick();const before=requests.filter(item=>item.route.endsWith('/jog/stop')).length;
 health.diagnostic_session='backend-2';await pollVersion();if(!notice().supported){console.log(JSON.stringify({supported:false}));return;}
 await click('htFrontendReload');const holding={...notice(),stops:requests.filter(item=>item.route.endsWith('/jog/stop')).length-before};
 dispatchKey('keyup','a');await tick();await click('htFrontendReload');
 console.log(JSON.stringify({supported:true,holding,after:notice()}));})();
""", before_source=REVISION_BOOTSTRAP)
    assert result["supported"] is True
    assert result["holding"]["disabled"] is True
    assert result["holding"]["reloads"] == 0 and result["holding"]["stops"] == 0
    assert result["after"]["reloads"] == 1


def test_other_hardware_busy_blocks_reload_without_reset():
    result = evaluate_browser("""
(async()=>{HighThroughput.activate();await tick();await pollVersion();health.frontend_revision='revision-2';health.hardware_busy=true;await pollVersion();
 if(!notice().supported){console.log(JSON.stringify({supported:false}));return;}
 await click('htFrontendReload');console.log(JSON.stringify({...notice(),writes:requests.filter(item=>item.body!==null)}));})();
""", before_source=REVISION_BOOTSTRAP)
    assert result["supported"] is True
    assert result["disabled"] is True and result["reloads"] == 0
    assert result["writes"] == []


def test_unknown_nested_stop_result_blocks_reload_even_when_controller_flag_is_true():
    result = evaluate_browser("""
(async()=>{sessionGuided.manual.active=true;sessionGuided.manual.status='blocked';sessionGuided.manual.alignment.status='blocked';
 sessionGuided.manual.alignment.stop_result={stop_confirmed:false};HighThroughput.activate();await tick();await pollVersion();
 health.diagnostic_session='backend-2';await pollVersion();await click('htFrontendReload');console.log(JSON.stringify(notice()));})();
""", before_source=REVISION_BOOTSTRAP)
    assert result["disabled"] is True and result["reloads"] == 0


def test_lost_health_connection_preserves_warning_and_disables_manual_reload():
    result = evaluate_browser("""
(async()=>{HighThroughput.activate();await tick();await pollVersion();health.frontend_revision='revision-2';await pollVersion();
 healthReadError=true;await pollVersion();await click('htFrontendReload');console.log(JSON.stringify({...notice(),writes:requests.filter(item=>item.body!==null)}));})();
""", before_source=REVISION_BOOTSTRAP)
    assert result["hidden"] is False
    assert result["disabled"] is True and result["reloads"] == 0
    assert result["writes"] == []
