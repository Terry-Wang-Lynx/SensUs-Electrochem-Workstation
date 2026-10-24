import json

import pytest

from test_automation_gui import evaluate_browser


LIMITS_BOOTSTRAP = """
globalThis.limitsSnapshot={state:'missing',active:false,calibrating:false,record:null,draft:null,error:null};
globalThis.limitsReadFailure=false;
globalThis.limitsReadGate=null;
globalThis.controllerSnapshot={state:'idle',busy:false,motion:{connected:true},channels:[],events:[]};
const ordinaryFetch=globalThis.fetch;
const limitsResponse=value=>({ok:true,json:async()=>JSON.parse(JSON.stringify(value))});
globalThis.fetch=async(path,options)=>{
 const route=path.replace('/api/automation/v1/','');
 const body=options.body?JSON.parse(options.body):null;
 if(route==='state')return limitsResponse(controllerSnapshot);
 if(route==='stop'){requests.push({route,body});return limitsResponse({stop_confirmed:true});}
 if(!route.startsWith('limits'))return ordinaryFetch(path,options);
 requests.push({route,body});
 if(route==='limits'&&limitsReadFailure)throw new Error('limits unavailable');
 if(route==='limits'&&limitsReadGate){const delayed=limitsReadGate;limitsReadGate=null;return delayed;}
 if(route==='limits/begin')limitsSnapshot={...limitsSnapshot,state:'calibrating',active:false,calibrating:true,draft:{version:'absolute-motion-limits-draft-1',captures:{}},error:null};
 if(route==='limits/capture')limitsSnapshot.draft.captures[body.boundary]={raw_counts:{X:800,Z:300},captured_unix_s:100};
 if(route==='limits/save')limitsSnapshot={state:'active',active:true,calibrating:false,record:{version:'absolute-motion-limits-1',boundaries:{x_left:1310720,x_right:0,z_up:0,z_down:655360},motion:{port:'offline',baud:115200,axes:{X:{lead_mm:2},Z:{lead_mm:2}}},clearance_confirmed:true},draft:null,error:null};
 if(route==='limits/cancel')limitsSnapshot={...limitsSnapshot,state:'stale',active:false,calibrating:false,draft:null};
 return limitsResponse(limitsSnapshot);
};
globalThis.change=id=>elements.get(id).dispatchEvent(new Event('change'));
globalThis.captureIds=['x_left','x_right','z_up','z_down'].map(boundary=>'htLimitCapture-'+boundary);
"""


def evaluate_limits(script):
    return evaluate_browser(script, before_source=LIMITS_BOOTSTRAP)


def test_limits_start_folded_and_never_capture_or_move_on_page_activation():
    result = evaluate_limits("""
(async()=>{
 HighThroughput.activate();await tick();
 const supported=elements.has('htLimitsPanel');
 console.log(JSON.stringify({supported,...(supported?{
  folded:elements.get('htLimitsPanel').hidden,runDisabled:elements.get('htRun').disabled,
  beginDisabled:elements.get('htLimitsBegin').disabled,
  captureDisabled:captureIds.map(id=>elements.get(id).disabled),
  saveDisabled:elements.get('htLimitsSave').disabled,
  writes:requests.filter(request=>request.body!==null)
 }:{})}));
})();
""")
    assert result["supported"]
    assert result["folded"]
    assert result["runDisabled"] is False
    assert result["beginDisabled"] is False
    assert result["captureDisabled"] == [True, True, True, True]
    assert result["saveDisabled"]
    assert result["writes"] == []


def test_limits_four_captures_send_only_named_edges_then_require_clearance_to_save():
    result = evaluate_limits("""
(async()=>{
 HighThroughput.activate();await tick();
 if(!elements.has('htLimitsBegin')){console.log(JSON.stringify({supported:false}));return;}
 await click('htLimitsToggle');await click('htLimitsBegin');
 const during={blocked:['htRun','htSave','htEnableX','htEnableZ','htMotionConnect','htMotionReference','htCalibrationBegin'].map(id=>elements.get(id).disabled),
  stopDisabled:!!elements.get('htStop').disabled,saveDisabled:elements.get('htLimitsSave').disabled};
 for(const id of captureIds)await click(id);
 const beforeClearance=elements.get('htLimitsSave').disabled;
 await click('htLimitsSave');
 const savesWithoutClearance=requests.filter(request=>request.route==='limits/save').length;
 elements.get('htLimitsClearance').checked=true;change('htLimitsClearance');
 const afterClearance=elements.get('htLimitsSave').disabled;
 await click('htLimitsSave');
 console.log(JSON.stringify({supported:true,during,beforeClearance,savesWithoutClearance,afterClearance,
  writes:requests.filter(request=>request.body!==null),summary:elements.get('htLimitsSummary').textContent,
  cancelDisabled:elements.get('htLimitsCancel').disabled,runDisabled:elements.get('htRun').disabled}));
})();
""")
    assert result["supported"]
    assert result["during"] == {"blocked": [True] * 7, "stopDisabled": False, "saveDisabled": True}
    assert result["beforeClearance"]
    assert result["savesWithoutClearance"] == 0
    assert result["afterClearance"] is False
    assert result["writes"] == [
        {"route": "limits/begin", "body": {}},
        {"route": "limits/capture", "body": {"boundary": "x_left"}},
        {"route": "limits/capture", "body": {"boundary": "x_right"}},
        {"route": "limits/capture", "body": {"boundary": "z_up"}},
        {"route": "limits/capture", "body": {"boundary": "z_down"}},
        {"route": "limits/save", "body": {"clearance_confirmed": True}},
    ]
    assert "当前连接有效" in result["summary"]
    assert result["cancelDisabled"]
    assert result["runDisabled"] is False


def test_limits_cancel_discards_clearance_and_does_not_reactivate_saved_record():
    result = evaluate_limits("""
(async()=>{
 limitsSnapshot={state:'stale',active:false,calibrating:false,record:{boundaries:{x_left:800,x_right:0,z_up:0,z_down:300}},draft:null,error:null};
 HighThroughput.activate();await tick();
 if(!elements.has('htLimitsBegin')){console.log(JSON.stringify({supported:false}));return;}
 await click('htLimitsBegin');elements.get('htLimitsClearance').checked=true;change('htLimitsClearance');
 await click('htLimitsCancel');
 const simulationDisabled=elements.get('htRun').disabled;
 elements.get('htMode').value='live';change('htMode');elements.get('htLiveConfirmed').checked=true;change('htLiveConfirmed');
 console.log(JSON.stringify({supported:true,simulationDisabled,liveDisabled:elements.get('htRun').disabled,
  clearance:elements.get('htLimitsClearance').checked,summary:elements.get('htLimitsSummary').textContent,
  writes:requests.filter(request=>request.body!==null)}));
})();
""")
    assert result["supported"]
    assert result["simulationDisabled"] is False
    assert result["liveDisabled"]
    assert result["clearance"] is False
    assert "重新读取四点" in result["summary"]
    assert result["writes"] == [{"route": "limits/begin", "body": {}}, {"route": "limits/cancel", "body": {}}]


@pytest.mark.parametrize("limit_state", [
    {"state": "missing", "active": False, "calibrating": False, "record": None, "draft": None, "error": None},
    {"state": "stale", "active": False, "calibrating": False, "record": {"version": "absolute-motion-limits-1"}, "draft": None, "error": None},
    {"state": "error", "active": False, "calibrating": False, "record": None, "draft": None, "error": {"code": "LIMITS_CORRUPT", "message": "record corrupt"}},
])
def test_missing_stale_or_corrupt_limits_block_live_but_leave_simulation_available(limit_state):
    result = evaluate_limits("""
(async()=>{
 limitsSnapshot=LIMIT_FIXTURE;
 HighThroughput.activate();await tick();
 const simulationDisabled=elements.get('htRun').disabled;
 elements.get('htMode').value='live';change('htMode');elements.get('htLiveConfirmed').checked=true;change('htLiveConfirmed');
 console.log(JSON.stringify({simulationDisabled,liveDisabled:elements.get('htRun').disabled,stopDisabled:!!elements.get('htStop').disabled}));
})();
""".replace("LIMIT_FIXTURE", json.dumps(limit_state)))
    assert result == {"simulationDisabled": False, "liveDisabled": True, "stopDisabled": False}


def test_limit_poll_failure_revokes_cached_live_gate_without_replacing_experiment_draft():
    result = evaluate_limits("""
(async()=>{
 limitsSnapshot={state:'active',active:true,calibrating:false,record:{span_mm:{X:40,Z:20}},draft:null,error:null};
 HighThroughput.activate();await tick();
 input('htName','local experiment draft');
 const draft=elements.get('htJson').value;
 elements.get('htMode').value='live';change('htMode');elements.get('htLiveConfirmed').checked=true;change('htLiveConfirmed');
 const beforeFailure=elements.get('htRun').disabled;
 limitsReadFailure=true;HighThroughput.activate();await tick();
 const failedLive=elements.get('htRun').disabled;
 elements.get('htMode').value='simulate';change('htMode');
 console.log(JSON.stringify({beforeFailure,failedLive,simulationDisabled:elements.get('htRun').disabled,
  name:elements.get('htName').value,preserved:elements.get('htJson').value===draft,
  writes:requests.filter(request=>request.body!==null)}));
})();
""")
    assert result["beforeFailure"] is False
    assert result["failedLive"]
    assert result["simulationDisabled"] is False
    assert result["name"] == "local experiment draft"
    assert result["preserved"]
    assert result["writes"] == []


def test_experiment_busy_disables_limit_begin_while_stop_stays_available():
    result = evaluate_limits("""
(async()=>{
 controllerSnapshot={...controllerSnapshot,state:'running',busy:true};
 HighThroughput.activate();await tick();
 if(!elements.has('htLimitsBegin')){console.log(JSON.stringify({supported:false}));return;}
 const beginDisabled=elements.get('htLimitsBegin').disabled;
 await click('htLimitsBegin');await click('htStop');
 console.log(JSON.stringify({supported:true,beginDisabled,stopDisabled:!!elements.get('htStop').disabled,
  writes:requests.filter(request=>request.body!==null)}));
})();
""")
    assert result["supported"]
    assert result["beginDisabled"]
    assert result["stopDisabled"] is False
    assert result["writes"] == [{"route": "stop", "body": {}}]


def test_three_captured_edges_cannot_save_and_zero_is_a_valid_read_count():
    result = evaluate_limits("""
(async()=>{
 HighThroughput.activate();await tick();await click('htLimitsBegin');
 await click('htLimitCapture-x_left');await click('htLimitCapture-x_right');await click('htLimitCapture-z_down');
 elements.get('htLimitsClearance').checked=true;change('htLimitsClearance');
 const incomplete=elements.get('htLimitsSave').disabled;await click('htLimitsSave');
 const prematureSaves=requests.filter(request=>request.route==='limits/save').length;
 limitsSnapshot.draft.captures.z_up={raw_counts:{X:0,Z:0},captured_unix_s:101};
 HighThroughput.activate();await tick();
 elements.get('htLimitsClearance').checked=true;change('htLimitsClearance');
 console.log(JSON.stringify({incomplete,prematureSaves,complete:!elements.get('htLimitsSave').disabled,
  zeroLabel:elements.get('htLimitValue-z_up').textContent}));
})();
""")
    assert result["incomplete"]
    assert result["prematureSaves"] == 0
    assert result["complete"]
    assert "已读取" in result["zeroLabel"]
    assert "0" in result["zeroLabel"]


def test_poll_started_before_begin_cannot_replace_active_capture_draft():
    result = evaluate_limits("""
(async()=>{
 HighThroughput.activate();await tick();
 const oldSnapshot=JSON.parse(JSON.stringify(limitsSnapshot));
 let finishOldRead;
 limitsReadGate=new Promise(resolve=>{finishOldRead=()=>resolve(limitsResponse(oldSnapshot));});
 HighThroughput.activate();await tick();await click('htLimitsBegin');
 finishOldRead();await tick();
 console.log(JSON.stringify({beginDisabled:elements.get('htLimitsBegin').disabled,
  captureDisabled:captureIds.map(id=>elements.get(id).disabled),
  runDisabled:elements.get('htRun').disabled,cancelDisabled:elements.get('htLimitsCancel').disabled,
  summary:elements.get('htLimitsSummary').textContent}));
})();
""")
    assert result["beginDisabled"]
    assert result["captureDisabled"] == [False] * 4
    assert result["runDisabled"]
    assert result["cancelDisabled"] is False
    assert "读取中" in result["summary"]


def test_disconnected_bus_explains_connection_needed_before_begin():
    result = evaluate_limits("""
(async()=>{
 controllerSnapshot.motion={connected:false};
 HighThroughput.activate();await tick();
 console.log(JSON.stringify({beginDisabled:elements.get('htLimitsBegin').disabled,
  progress:elements.get('htLimitsProgress').textContent,
  writes:requests.filter(request=>request.body!==null)}));
})();
""")
    assert result["beginDisabled"]
    assert "请先在高级设置连接X/Z共享RS485滑台，再开始读取" in result["progress"]
    assert "不会自动连接或使能" in result["progress"]
    assert result["writes"] == []
