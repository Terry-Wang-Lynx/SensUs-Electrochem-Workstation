import json
import re

from node_test_utils import run_node
import pytest

from test_absolute_limits_gui import LIMITS_BOOTSTRAP
from test_automation_gui import evaluate_apparatus, evaluate_browser
from test_gui_dom_contract import GUI_DIR, _evaluate_chart_js, _extract_js_function


TWIN_BOOTSTRAP = LIMITS_BOOTSTRAP + """
globalThis.twinSnapshot={state:'unregistered',registered:false,fresh:false,pose:null,registration:null,error:null};
globalThis.twinFailure=false;
globalThis.controllerSnapshot={...controllerSnapshot,stop_confirmed:true,recovery_required:false};
const cloneTwin=value=>JSON.parse(JSON.stringify(value));
const twinMechanical=address=>({address,lead_mm:2,travel_mm:150,positive_direction:0,max_speed_mm_s:1});
const twinProfile=address=>({firmware_version:1,hardware_version:1,power_flag_baseline:0,config:{microstep:16,step_angle:1.8,pulses_per_rev:3200,response_mode:1,stall_protect:1,position_window_deg:0.8,raw_hex:address.toString(16).padStart(2,'0')+'00'.repeat(31)+'6b',options_hex:address.toString(16).padStart(2,'0')+'0000006b'}});
globalThis.twinLimitRecord={version:'absolute-motion-limits-1',created_unix_s:100,motion:{port:'offline',baud:115200,axes:{X:twinMechanical(1),Z:twinMechanical(2)}},profiles:{X:twinProfile(1),Z:twinProfile(2)},connection_token:'00000000-0000-0000-0000-000000000001',stop_generation:0,boundaries:{x_left:655360,x_right:0,z_up:0,z_down:327680},clearance_confirmed:true};
globalThis.PAHighThroughputDevices={available:true,received_at_ms:Date.now(),snapshot:{motion:{connected:true,faulted:false,feedback_error:null,port:'offline',baud:115200,connection_token:twinLimitRecord.connection_token,stop_generation:0,absolute_limits:cloneTwin(twinLimitRecord),axes:{X:{...twinProfile(1),mechanical:twinMechanical(1),raw_count:0,telemetry_valid:true,feedback_age_s:0.1},Z:{...twinProfile(2),mechanical:twinMechanical(2),raw_count:0,telemetry_valid:true,feedback_age_s:0.2}}},limits:cloneTwin(limitsSnapshot)}};
globalThis.prepareTwinContext=()=>{limitsSnapshot={state:'active',active:true,calibrating:false,record:cloneTwin(twinLimitRecord),draft:null,error:null};PAHighThroughputDevices.snapshot.limits=cloneTwin(limitsSnapshot);};
globalThis.twinDatumRegistration=(datum='left_up')=>({version:'digital-twin-datum-1',mode:datum+'_datum',datum,created_unix_s:100,geometry_sha256:'a'.repeat(64),basis:'nominal_module_endpoints',display_only:true,accuracy_unverified:true,binding:Object.fromEntries(['motion','profiles','connection_token','stop_generation'].map(field=>[field,cloneTwin(twinLimitRecord[field])])),anchor_counts:datum==='left_up'?{X:7,Z:0}:{X:-1,Z:-9},cad_anchor:datum==='left_up'?{x_center_mm:-75,lift_mm:50}:{x_center_mm:75,lift_mm:0}});
globalThis.prepareDatumContext=()=>{limitsSnapshot={state:'calibrating',active:false,calibrating:true,record:null,draft:{version:'absolute-motion-limits-draft-1',...cloneTwin(twinDatumRegistration().binding),captures:{x_left:{raw_counts:{X:7,Z:0},captured_unix_s:100},z_up:{raw_counts:{X:7,Z:0},captured_unix_s:101}}},error:null};PAHighThroughputDevices.snapshot.limits=cloneTwin(limitsSnapshot);PAHighThroughputDevices.snapshot.motion.absolute_limits=null;controllerSnapshot.busy=true;};
globalThis.datumTwinSnapshot=(datum='left_up')=>({state:'synced',registered:true,fresh:true,pose:datum==='left_up'?{scene_x_mm:-124.5,scene_z_mm:15,x_center_mm:-75,lift_mm:50,feedback_age_s:{X:0.1,Z:0.2},source:'encoder_feedback'}:{scene_x_mm:25.5,scene_z_mm:-35,x_center_mm:75,lift_mm:0,feedback_age_s:{X:0.1,Z:0.2},source:'encoder_feedback'},registration:twinDatumRegistration(datum),error:null});
const twinFetch=globalThis.fetch;
globalThis.fetch=async(path,options)=>{
 const route=path.replace('/api/automation/v1/','');
 if(route==='guided/preview')return limitsResponse({plan_hash:'physical-priority-plan',steps:[{op:'move_x',resolved_params:{target_mm:123}}],duration_s:2});
 if(!route.startsWith('twin'))return twinFetch(path,options);
 requests.push({route,body:options.body?JSON.parse(options.body):null});
 if(twinFailure)throw new Error('feedback unavailable');
 if(route==='twin/register')twinSnapshot={state:'synced',registered:true,fresh:true,pose:{scene_x_mm:-3,scene_z_mm:2,x_center_mm:46.5,lift_mm:37,feedback_age_s:{X:0.2,Z:0.3},source:'encoder_feedback'},registration:{tolerance_mm:0.1,limits_record:cloneTwin(twinLimitRecord)},error:null};
 if(route==='twin/register-datum')twinSnapshot=datumTwinSnapshot(JSON.parse(options.body).datum);
 return limitsResponse(twinSnapshot);
};
globalThis.sceneCalls=[];
globalThis.HighThroughputScene={create:async()=>({resize(){},setColumns(){},setElectrodes(){},selectColumn(){},cameraView(view){sceneCalls.push(['camera',view]);},stopPreview(){sceneCalls.push(['stop']);},preview(){sceneCalls.push(['preview']);},setPhysicalAttached(value){sceneCalls.push(['attached',value]);},setFeedbackPose(value){sceneCalls.push(['feedback',value]);},clearFeedbackPose(){sceneCalls.push(['clear']);}})};
"""


def evaluate_twin(script):
    return evaluate_browser(script, before_source=TWIN_BOOTSTRAP)


def test_sidebar_distinguishes_fresh_encoder_feedback_stale_and_bound_boards():
    result = _evaluate_chart_js(
        ["highThroughputDeviceStatus", "renderHighThroughputDevices"],
        """
const nodes=Object.fromEntries(['Motion','AxisX','AxisZ','BoardA','BoardB','Limits','CompactSummary','CompactAxisX','CompactAxisZ','CompactBoardA','CompactBoardB','CompactLimits'].map(key=>['htDevices'+key,{textContent:'',className:''}]));
const $=id=>nodes[id];
const devices={motion:{connected:true,port:'COM4',baud:115200,faulted:false,feedback_error:null,axes:{X:{address:1,raw_count:0,speed_rpm:0,feedback_age_s:0.2,telemetry_valid:true},Z:{address:2,raw_count:-5,speed_rpm:0,feedback_age_s:0.4,telemetry_valid:true}}},boards:{A:{address:'UUID-A',state:'bound_unverified',channel_ids:['A','B','C','D']},B:{address:'',state:'unbound',channel_ids:['E','F','G','H']}},limits:{state:'missing',active:false}};
renderHighThroughputDevices(devices);const fresh=JSON.parse(JSON.stringify(nodes));
renderHighThroughputDevices(devices,null,3);const aged=JSON.parse(JSON.stringify(nodes));
devices.motion.axes.X.feedback_age_s=4;devices.boards.A.state='acquiring';
renderHighThroughputDevices(devices);const stale=JSON.parse(JSON.stringify(nodes));
renderHighThroughputDevices(devices,new Error('offline'));const failed=JSON.parse(JSON.stringify(nodes));
""",
        "{fresh,aged,stale,failed}",
    )
    assert "已读通" in result["fresh"]["htDevicesMotion"]["textContent"]
    assert "0 count" in result["fresh"]["htDevicesAxisX"]["textContent"]
    assert "0 RPM" in result["fresh"]["htDevicesAxisX"]["textContent"]
    assert "0.2 s" in result["fresh"]["htDevicesAxisX"]["textContent"]
    assert "未验证连接" in result["fresh"]["htDevicesBoardA"]["textContent"]
    assert "未绑定" in result["fresh"]["htDevicesBoardB"]["textContent"]
    assert "未标定" in result["fresh"]["htDevicesLimits"]["textContent"]
    assert "已读通" in result["fresh"]["htDevicesCompactSummary"]["textContent"]
    assert "未绑定" in result["fresh"]["htDevicesCompactSummary"]["textContent"]
    for phase in ("fresh", "aged", "stale", "failed"):
        for device in ("AxisX", "AxisZ", "BoardA", "BoardB", "Limits"):
            assert result[phase]["htDevicesCompact" + device] == result[phase]["htDevices" + device]
    assert "反馈过期" in result["aged"]["htDevicesAxisX"]["textContent"]
    assert "ok" not in result["aged"]["htDevicesMotion"]["className"]
    assert "反馈过期" in result["stale"]["htDevicesAxisX"]["textContent"]
    assert "ok" not in result["stale"]["htDevicesAxisX"]["className"]
    assert "采集中" in result["stale"]["htDevicesBoardA"]["textContent"]
    assert all("ok" not in value["className"] for value in result["failed"].values())


def test_compact_devices_disclosure_is_in_main_flow_and_visible_on_small_screens():
    html = (GUI_DIR / "index.html").read_text()
    styles = (GUI_DIR / "styles.css").read_text()
    identifiers = re.findall(r'\bid="([^"]+)"', html)
    assert len(identifiers) == len(set(identifiers))
    disclosure = re.search(r'<details\b[^>]*id="htDevicesCompact"[^>]*>(.*?)</details>', html, re.DOTALL)
    assert disclosure is not None
    assert " open" not in disclosure.group(0).split(">", 1)[0]
    assert html.index("</aside>") < disclosure.start() < html.index('id="globalError"')
    assert "高通量设备" in disclosure.group(1)
    assert all(f'id="htDevicesCompact{device}"' in disclosure.group(1) for device in ("Summary", "AxisX", "AxisZ", "BoardA", "BoardB", "Limits"))
    assert len(re.findall(r'<p\b', disclosure.group(1))) == 4
    assert re.search(r'\.ht-devices-compact\s*\{[^}]*display\s*:\s*none', styles)
    assert re.search(r'@media\s*\(max-width\s*:\s*620px\)\s*\{\s*\.ht-devices-compact\s*\{[^}]*display\s*:\s*block', styles)
    assert not re.search(r'\.ht-devices-compact(?:\s|\{|\.)[^}]*position\s*:\s*(?:fixed|absolute)', styles)


def test_desktop_sidebar_scrolls_when_both_device_panels_exceed_viewport_height():
    styles = (GUI_DIR / "styles.css").read_text()
    assert re.search(r'@media\s*\(min-width\s*:\s*981px\)\s*\{\s*\.sidebar\s*\{[^}]*overflow-y\s*:\s*auto', styles)


def test_global_device_poll_fetches_cache_only_and_propagates_read_failure():
    source = (GUI_DIR / "app.js").read_text()
    names = ["highThroughputDeviceStatus", "renderHighThroughputDevices", "refreshHighThroughputDevices"]
    if not all(f"function {name}(" in source for name in names):
        raise AssertionError("Global cached high-throughput status polling is not implemented")
    functions = "\n".join(_extract_js_function(source, name) for name in names)
    script = functions + """
const nodes=new Proxy({}, {get(target,key){return target[key]||(target[key]={textContent:'',className:''})}});
const $=id=>nodes[id];const window={HighThroughput:{acceptDevices(value){this.last=value;}}};
const state={exiting:false};let highThroughputDevicesPending=false;const paths=[];let fail=false;
const api=async path=>{paths.push(path);if(fail)throw new Error('offline');return {motion:{connected:true},boards:{},limits:{}};};
(async()=>{await refreshHighThroughputDevices();const connected=window.PAHighThroughputDevices;fail=true;await refreshHighThroughputDevices();console.log(JSON.stringify({paths,connected,failed:window.PAHighThroughputDevices,propagated:window.HighThroughput.last}));})();
"""
    completed = run_node(script, capture_output=True, text=True, check=True)
    result = json.loads(completed.stdout)
    assert result["paths"] == ["/api/automation/v1/devices"] * 2
    assert result["connected"]["available"]
    assert result["connected"]["snapshot"]["motion"]["connected"]
    assert result["failed"]["available"] is False
    assert result["propagated"]["available"] is False


def test_twin_correspondence_starts_blank_and_missing_limits_block_registration():
    result = evaluate_twin("""
(async()=>{
 HighThroughput.activate();await tick();
 const supported=elements.has('htTwinRegister');
 if(!supported){console.log(JSON.stringify({supported}));return;}
 const ids=['htTwinXLeft','htTwinXRight','htTwinZUp','htTwinZDown'];
 const blank=ids.map(id=>elements.get(id).value);
 await click('htTwinRegister');
 console.log(JSON.stringify({supported,blank,disabled:elements.get('htTwinRegister').disabled,
 status:elements.get('htTwinRegisterStatus').textContent,pose:elements.get('htTwinStatus').textContent,
 writes:requests.filter(request=>request.body!==null),sceneCalls}));
})();
""")
    assert result["supported"]
    assert result["blank"] == [""] * 4
    assert result["disabled"]
    assert "四边界" in result["status"]
    assert "未配准" in result["pose"]
    assert result["writes"] == []
    assert not any(call[0] == "feedback" for call in result["sceneCalls"])


def test_registration_handler_sends_only_explicit_cad_correspondence():
    result = evaluate_twin("""
(async()=>{
 prepareTwinContext();
 HighThroughput.activate();await tick();
 if(!elements.has('htTwinRegister')){console.log(JSON.stringify({supported:false}));return;}
 input('htTwinXLeft',-10.5);input('htTwinXRight',9.5);input('htTwinZUp',40);input('htTwinZDown',30);
 await click('htTwinRegister');const beforeConfirm=requests.filter(request=>request.route==='twin/register').length;
 elements.get('htTwinConfirm').checked=true;change('htTwinConfirm');await click('htTwinRegister');
 console.log(JSON.stringify({supported:true,beforeConfirm,writes:requests.filter(request=>request.body!==null),
 status:elements.get('htTwinStatus').textContent,sceneCalls}));
})();
""")
    assert result["supported"]
    assert result["beforeConfirm"] == 0
    assert result["writes"] == [{"route": "twin/register", "body": {"cad_bounds": {
        "x_left_mm": -10.5, "x_right_mm": 9.5, "z_up_lift_mm": 40, "z_down_lift_mm": 30,
    }, "confirmed": True}}]
    assert "编码器反馈" in result["status"]
    assert "0.3 s" in result["status"]
    assert any(call[0] == "feedback" for call in result["sceneCalls"])


def test_left_up_datum_sends_only_confirmation_while_two_boundaries_keep_motion_blocked():
    result = evaluate_twin("""
(async()=>{
 prepareDatumContext();HighThroughput.activate();await tick();
 if(!elements.has('htTwinRegisterDatum')){console.log(JSON.stringify({supported:false}));return;}
 const before={disabled:elements.get('htTwinRegisterDatum').disabled,fields:['htTwinXLeft','htTwinXRight','htTwinZUp','htTwinZDown'].map(id=>elements.get(id).value)};
 await click('htTwinRegisterDatum');const unconfirmed=requests.filter(request=>request.body!==null);
 elements.get('htTwinDatumConfirm').checked=true;change('htTwinDatumConfirm');
 const enabled=!elements.get('htTwinRegisterDatum').disabled;await click('htTwinRegisterDatum');
 console.log(JSON.stringify({supported:true,before,unconfirmed,enabled,writes:requests.filter(request=>request.body!==null),
 blocked:['htRun','htLimitsSave','htEnableX','htEnableZ','htTwinRegister'].map(id=>elements.get(id).disabled),
 confirmation:elements.get('htTwinDatumConfirm').checked,captures:Object.keys(limitsSnapshot.draft.captures),
 status:elements.get('htTwinStatus').textContent,registration:elements.get('htTwinDatumStatus').textContent,sceneCalls}));
})();
""")
    assert result["supported"]
    assert result["before"] == {"disabled": True, "fields": [""] * 4}
    assert result["unconfirmed"] == []
    assert result["enabled"]
    assert result["writes"] == [{"route": "twin/register-datum", "body": {"datum": "left_up", "confirmed": True}}]
    assert result["blocked"] == [True] * 5
    assert result["confirmation"] is False
    assert result["captures"] == ["x_left", "z_up"]
    assert "编码器反馈 · 名义CAD端点基准" in result["status"]
    assert "非机械精度验收" in result["status"]
    assert "名义CAD端点基准" in result["registration"]
    assert result["sceneCalls"][-1][0] == "feedback"


def test_left_up_datum_can_use_current_active_four_boundary_record():
    result = evaluate_twin("""
(async()=>{
 prepareTwinContext();HighThroughput.activate();await tick();
 if(!elements.has('htTwinRegisterDatum')){console.log(JSON.stringify({supported:false}));return;}
 elements.get('htTwinDatumConfirm').checked=true;change('htTwinDatumConfirm');
 const enabled=!elements.get('htTwinRegisterDatum').disabled;await click('htTwinRegisterDatum');
 console.log(JSON.stringify({supported:true,enabled,writes:requests.filter(request=>request.body!==null)}));
})();
""")
    assert result["supported"]
    assert result["enabled"]
    assert result["writes"] == [{"route": "twin/register-datum", "body": {"datum": "left_up", "confirmed": True}}]


def test_right_down_datum_defaults_to_only_trusted_current_corner_and_sends_no_counters():
    result = evaluate_twin("""
(async()=>{
 prepareDatumContext();
 limitsSnapshot.draft.captures={x_right:{raw_counts:{X:-1,Z:-9},captured_unix_s:100},z_down:{raw_counts:{X:-1,Z:-9},captured_unix_s:101}};
 PAHighThroughputDevices.snapshot.limits=cloneTwin(limitsSnapshot);
 HighThroughput.activate();await tick();
 if(!elements.has('htTwinDatumCorner')){console.log(JSON.stringify({supported:false}));return;}
 const corner=elements.get('htTwinDatumCorner').value;
 elements.get('htTwinDatumConfirm').checked=true;change('htTwinDatumConfirm');
 const enabled=!elements.get('htTwinRegisterDatum').disabled;await click('htTwinRegisterDatum');
 console.log(JSON.stringify({supported:true,corner,enabled,writes:requests.filter(request=>request.body!==null),
 last:sceneCalls.filter(call=>call[0]==='feedback').at(-1),status:elements.get('htTwinStatus').textContent,
 blocked:['htRun','htLimitsSave','htEnableX','htEnableZ'].map(id=>elements.get(id).disabled)}));
})();
""")
    assert result["supported"]
    assert result["corner"] == "right_down"
    assert result["enabled"]
    assert result["writes"] == [{"route": "twin/register-datum", "body": {"datum": "right_down", "confirmed": True}}]
    assert result["last"][1]["x_center_mm"] == 75
    assert result["last"][1]["lift_mm"] == 0
    assert "名义CAD端点基准" in result["status"]
    assert "非机械精度验收" in result["status"]
    assert result["blocked"] == [True] * 4


def test_changing_datum_corner_clears_confirmation_and_never_reuses_opposite_captures():
    result = evaluate_twin("""
(async()=>{
 prepareDatumContext();HighThroughput.activate();await tick();
 if(!elements.has('htTwinDatumCorner')){console.log(JSON.stringify({supported:false}));return;}
 elements.get('htTwinDatumConfirm').checked=true;change('htTwinDatumConfirm');
 elements.get('htTwinDatumCorner').value='right_down';change('htTwinDatumCorner');
 const confirmed=elements.get('htTwinDatumConfirm').checked;
 elements.get('htTwinDatumConfirm').checked=true;change('htTwinDatumConfirm');
 const disabled=elements.get('htTwinRegisterDatum').disabled,canChooseAnother=!elements.get('htTwinDatumCorner').disabled;await click('htTwinRegisterDatum');
 console.log(JSON.stringify({supported:true,confirmed,disabled,canChooseAnother,writes:requests.filter(request=>request.body!==null)}));
})();
""")
    assert result["supported"]
    assert result["confirmed"] is False
    assert result["disabled"]
    assert result["canChooseAnother"]
    assert result["writes"] == []


def test_datum_registration_allows_confirmed_stopped_state_during_limit_capture():
    result = evaluate_twin("""
(async()=>{
 prepareDatumContext();controllerSnapshot.state='stopped';controllerSnapshot.stop_confirmed=true;
 HighThroughput.activate();await tick();
 elements.get('htTwinDatumConfirm').checked=true;change('htTwinDatumConfirm');
 const enabled=!elements.get('htTwinRegisterDatum').disabled;await click('htTwinRegisterDatum');
 console.log(JSON.stringify({enabled,writes:requests.filter(request=>request.body!==null),
 blocked:['htRun','htEnableX','htEnableZ'].map(id=>elements.get(id).disabled)}));
})();
""")
    assert result["enabled"]
    assert result["writes"] == [{"route": "twin/register-datum", "body": {"datum": "left_up", "confirmed": True}}]
    assert result["blocked"] == [True] * 3


@pytest.mark.parametrize("change", [
    "delete limitsSnapshot.draft.captures.x_left;",
    "delete limitsSnapshot.draft.captures.z_up;",
    "limitsSnapshot.draft.captures.z_up.raw_counts.Z=null;",
    "limitsSnapshot.draft.captures.x_left.raw_counts.X=1.5;",
    "limitsSnapshot.draft.connection_token='old-connection';",
    "limitsSnapshot.draft.profiles.X.config.microstep=32;",
    "PAHighThroughputDevices.available=false;",
    "PAHighThroughputDevices.received_at_ms=Date.now()-4000;",
    "delete PAHighThroughputDevices.received_at_ms;",
    "PAHighThroughputDevices.snapshot.motion.connected=false;",
    "PAHighThroughputDevices.snapshot.motion.faulted=true;",
    "PAHighThroughputDevices.snapshot.motion.feedback_error={message:'offline'};",
    "PAHighThroughputDevices.snapshot.motion.axes.Z.telemetry_valid=false;",
    "PAHighThroughputDevices.snapshot.motion.axes.Z.feedback_age_s=3;",
    "PAHighThroughputDevices.snapshot.motion.axes.Z.raw_count=null;",
    "PAHighThroughputDevices.snapshot.motion.axes.X.raw_count=0.5;",
    "controllerSnapshot.state='running';",
    "controllerSnapshot.state='stopped';controllerSnapshot.stop_confirmed=false;",
    "controllerSnapshot.recovery_required=true;",
    "limitsReadFailure=true;",
])
def test_left_up_datum_requires_trusted_captures_and_healthy_current_motion(change):
    result = evaluate_twin("""
(async()=>{
 prepareDatumContext();
""" + change + """
 HighThroughput.activate();await tick();
 if(!elements.has('htTwinRegisterDatum')){console.log(JSON.stringify({supported:false}));return;}
 elements.get('htTwinDatumConfirm').checked=true;change('htTwinDatumConfirm');
 const disabled=elements.get('htTwinRegisterDatum').disabled;await click('htTwinRegisterDatum');
 console.log(JSON.stringify({supported:true,disabled,writes:requests.filter(request=>request.body!==null),sceneCalls}));
})();
""")
    assert result["supported"]
    assert result["disabled"]
    assert result["writes"] == []
    assert not any(call[0] == "feedback" for call in result["sceneCalls"])


def test_independent_datum_follows_hand_turn_and_stop_across_limit_draft_and_record_changes():
    result = evaluate_twin("""
(async()=>{
 prepareDatumContext();twinSnapshot=datumTwinSnapshot();HighThroughput.activate();await tick();
 const before=sceneCalls.filter(call=>call[0]==='feedback').length;
 const next=cloneTwin(PAHighThroughputDevices);
 next.snapshot.motion.axes.X.raw_count=-32761;next.snapshot.motion.axes.Z.raw_count=16384;next.snapshot.motion.stop_generation++;
 next.snapshot.limits={state:'active',active:true,calibrating:false,record:cloneTwin(twinLimitRecord),draft:null,error:null};
 next.snapshot.motion.absolute_limits=cloneTwin(twinLimitRecord);next.snapshot.motion.absolute_limits.boundaries.x_left+=100;
 HighThroughput.acceptDevices(next);
 const afterContext=sceneCalls.filter(call=>call[0]==='feedback').length;
 twinSnapshot.pose={...twinSnapshot.pose,scene_x_mm:-123.5,scene_z_mm:14.5,x_center_mm:-74,lift_mm:49.5};
 limitsSnapshot={state:'missing',active:false,calibrating:false,record:null,draft:null,error:null};
 HighThroughput.activate();await tick();
 console.log(JSON.stringify({before,afterContext,followed:sceneCalls.filter(call=>call[0]==='feedback').at(-1),
 status:elements.get('htTwinStatus').textContent,pose:elements.get('htPose').textContent}));
})();
""")
    assert result["before"] > 0
    assert result["afterContext"] == result["before"] + 1
    assert result["followed"][1]["scene_x_mm"] == -123.5
    assert result["followed"][1]["scene_z_mm"] == 14.5
    assert "编码器反馈 · 名义CAD端点基准" in result["status"]
    assert "非机械精度验收" in result["pose"]


@pytest.mark.parametrize("change", [
    "next.available=false;",
    "next.received_at_ms=Date.now()-4000;",
    "delete next.received_at_ms;",
    "next.snapshot.motion.connection_token='new-connection';",
    "next.snapshot.motion.axes.X.config.microstep=32;",
    "next.snapshot.motion.axes.Z.mechanical.lead_mm=4;",
    "next.snapshot.motion.faulted=true;",
    "next.snapshot.motion.feedback_error={message:'offline'};",
    "next.snapshot.motion.connected=false;",
    "next.snapshot.motion.axes.Z.telemetry_valid=false;",
    "next.snapshot.motion.axes.Z.raw_count=null;",
])
def test_independent_datum_invalidates_immediately_on_cache_connection_or_profile_fault(change):
    result = evaluate_twin("""
(async()=>{
 prepareDatumContext();twinSnapshot=datumTwinSnapshot();HighThroughput.activate();await tick();
 const before=sceneCalls.filter(call=>call[0]==='feedback').length;
 const next=cloneTwin(PAHighThroughputDevices);
""" + change + """
 HighThroughput.acceptDevices(next);
 console.log(JSON.stringify({before,after:sceneCalls.filter(call=>call[0]==='feedback').length,
 status:elements.get('htTwinStatus').textContent,pose:elements.get('htPose').textContent,last:sceneCalls.at(-1)}));
})();
""")
    assert result["before"] > 0
    assert result["after"] == result["before"]
    assert "未知" in result["status"]
    assert "参考" in result["pose"]
    assert result["last"] == ["clear"]


def test_independent_datum_never_uses_plan_target_as_encoder_feedback():
    result = evaluate_twin("""
(async()=>{
 prepareDatumContext();twinSnapshot=datumTwinSnapshot();HighThroughput.activate();await tick();
 const before=sceneCalls.filter(call=>call[0]==='feedback').length;
 twinSnapshot.pose.source='plan_target';
 controllerSnapshot.position={X:123,Z:456};controllerSnapshot.mode='simulate';
 HighThroughput.activate();await tick();await click('htAnimate');
 console.log(JSON.stringify({before,after:sceneCalls.filter(call=>call[0]==='feedback').length,
 sceneCalls,status:elements.get('htTwinStatus').textContent,pose:elements.get('htPose').textContent}));
})();
""")
    assert result["before"] > 0
    assert result["after"] == result["before"]
    assert not any(call[0] == "preview" for call in result["sceneCalls"])
    assert "未知" in result["status"]
    assert "参考" in result["pose"]


def test_physical_pose_uses_twin_feedback_and_becomes_unknown_on_failure():
    result = evaluate_twin("""
(async()=>{
 prepareTwinContext();controllerSnapshot.position={X:123,Z:456};controllerSnapshot.mode='simulate';
 twinSnapshot={state:'synced',registered:true,fresh:true,pose:{scene_x_mm:-7,scene_z_mm:3,x_center_mm:42.5,lift_mm:38,feedback_age_s:{X:0.1,Z:0.4},source:'encoder_feedback'},registration:{limits_record:cloneTwin(twinLimitRecord)},error:null};
 HighThroughput.activate();await tick();
 if(!elements.has('htTwinStatus')){console.log(JSON.stringify({supported:false}));return;}
 const followed=sceneCalls.filter(call=>call[0]==='feedback');
 await click('htCompile');
 await click('htAnimate');await click('htCameraPlate');
 twinFailure=true;HighThroughput.activate();await tick();
 console.log(JSON.stringify({supported:true,followed,sceneCalls,status:elements.get('htTwinStatus').textContent,pose:elements.get('htPose').textContent,animateDisabled:elements.get('htAnimate').disabled}));
})();
""")
    assert result["supported"]
    assert result["followed"][-1][1]["scene_x_mm"] == -7
    assert result["followed"][-1][1]["scene_z_mm"] == 3
    assert not any(call[0] == "preview" for call in result["sceneCalls"])
    assert ["camera", "plate"] in result["sceneCalls"]
    assert "未知" in result["status"]
    assert "参考" in result["pose"]
    assert result["animateDisabled"]


def test_feedback_pose_moves_rigid_groups_and_blocks_preview_overwrite():
    result = evaluate_apparatus("""
if(!assembly.setFeedbackPose||!assembly.setPhysicalAttached){console.log(JSON.stringify({supported:false}));process.exit(0);}
assembly.setPreviewPose({x:30,z:8});assembly.setPhysicalAttached(true);
assembly.setFeedbackPose({scene_x_mm:-7,scene_z_mm:3,feedback_age_s:{X:0.1,Z:0.4},source:'encoder_feedback'});
const followed={x:assembly.xGroup.position.x,z:assembly.zGroup.position.z,fixed:assembly.fixedGroup.position.toArray()};
assembly.setPreviewPose({x:123,z:456});
const afterPreview={x:assembly.xGroup.position.x,z:assembly.zGroup.position.z};
const rejected=assembly.setFeedbackPose({scene_x_mm:NaN,scene_z_mm:2,source:'encoder_feedback'});
assembly.clearFeedbackPose();
console.log(JSON.stringify({supported:true,followed,afterPreview,rejected,unknown:[assembly.xGroup.position.x,assembly.zGroup.position.z]}));
""")
    assert result["supported"]
    assert result["followed"] == {"x": -7, "z": 3, "fixed": [0, 0, 0]}
    assert result["afterPreview"] == {"x": -7, "z": 3}
    assert result["rejected"] is False
    assert result["unknown"] == [0, 0]


def test_stale_global_motion_cache_invalidates_previously_fresh_twin_pose():
    result = evaluate_twin("""
(async()=>{
 prepareTwinContext();twinSnapshot={state:'synced',registered:true,fresh:true,pose:{scene_x_mm:-7,scene_z_mm:3,x_center_mm:42.5,lift_mm:38,feedback_age_s:{X:0.1,Z:0.4},source:'encoder_feedback'},registration:{limits_record:cloneTwin(twinLimitRecord)},error:null};
 HighThroughput.activate();await tick();
 if(!elements.has('htTwinStatus')){console.log(JSON.stringify({supported:false}));return;}
 const before=sceneCalls.filter(call=>call[0]==='feedback').length;
 PAHighThroughputDevices.received_at_ms=Date.now()-4000;
 HighThroughput.acceptDevices(PAHighThroughputDevices);
 console.log(JSON.stringify({supported:true,before,after:sceneCalls.filter(call=>call[0]==='feedback').length,
 status:elements.get('htTwinStatus').textContent,pose:elements.get('htPose').textContent,last:sceneCalls.at(-1)}));
})();
""")
    assert result["supported"]
    assert result["before"] > 0
    assert result["after"] == result["before"]
    assert "未知" in result["status"]
    assert "参考" in result["pose"]
    assert result["last"] == ["clear"]


def test_disconnect_replaces_last_encoder_caption_with_unknown_reference():
    result = evaluate_twin("""
(async()=>{
 prepareTwinContext();twinSnapshot={state:'synced',registered:true,fresh:true,pose:{scene_x_mm:-7,scene_z_mm:3,x_center_mm:42.5,lift_mm:38,feedback_age_s:{X:0.1,Z:0.4},source:'encoder_feedback'},registration:{limits_record:cloneTwin(twinLimitRecord)},error:null};
 HighThroughput.activate();await tick();
 if(!elements.has('htTwinStatus')){console.log(JSON.stringify({supported:false}));return;}
 const before=elements.get('htPose').textContent;
 PAHighThroughputDevices.snapshot.motion.connected=false;
 twinSnapshot={state:'disconnected',registered:true,fresh:false,pose:null,registration:{},error:null};
 HighThroughput.acceptDevices(PAHighThroughputDevices);HighThroughput.activate();await tick();
 console.log(JSON.stringify({supported:true,before,status:elements.get('htTwinStatus').textContent,pose:elements.get('htPose').textContent}));
})();
""")
    assert result["supported"]
    assert "编码器反馈" in result["before"]
    assert "已断开" in result["status"]
    assert "未知" in result["pose"]
    assert "参考" in result["pose"]
    assert "编码器反馈" not in result["pose"]


@pytest.mark.parametrize("change", [
    "next.snapshot.motion.connection_token='new-bus';",
    "next.snapshot.limits.record.boundaries.x_left+=100;next.snapshot.motion.absolute_limits=cloneTwin(next.snapshot.limits.record);",
    "next.snapshot.limits.active=false;next.snapshot.limits.state='calibrating';next.snapshot.limits.calibrating=true;",
    "next.snapshot.motion.axes.X.config.microstep=32;",
    "next.snapshot.motion.axes.X.mechanical.lead_mm=4;",
])
def test_global_context_change_immediately_rejects_cached_twin_pose(change):
    result = evaluate_twin("""
(async()=>{
 prepareTwinContext();twinSnapshot={state:'synced',registered:true,fresh:true,pose:{scene_x_mm:-7,scene_z_mm:3,x_center_mm:42.5,lift_mm:38,feedback_age_s:{X:0.1,Z:0.4},source:'encoder_feedback'},registration:{limits_record:cloneTwin(twinLimitRecord)},error:null};
 HighThroughput.activate();await tick();
 const before=sceneCalls.filter(call=>call[0]==='feedback').length;
 const next=cloneTwin(PAHighThroughputDevices);
""" + change + """
 HighThroughput.acceptDevices(next);
 console.log(JSON.stringify({before,after:sceneCalls.filter(call=>call[0]==='feedback').length,
 status:elements.get('htTwinStatus').textContent,registration:elements.get('htTwinRegisterStatus').textContent,pose:elements.get('htPose').textContent,last:sceneCalls.at(-1)}));
})();
""")
    assert result["before"] > 0
    assert result["after"] == result["before"]
    assert "未知" in result["status"]
    assert "反馈有效" not in result["registration"]
    assert "参考" in result["pose"]
    assert result["last"] == ["clear"]


def test_starting_new_boundary_capture_immediately_clears_cached_twin_pose():
    result = evaluate_twin("""
(async()=>{
 prepareTwinContext();twinSnapshot={state:'synced',registered:true,fresh:true,pose:{scene_x_mm:-7,scene_z_mm:3,x_center_mm:42.5,lift_mm:38,feedback_age_s:{X:0.1,Z:0.4},source:'encoder_feedback'},registration:{limits_record:cloneTwin(twinLimitRecord)},error:null};
 HighThroughput.activate();await tick();
 const before=sceneCalls.filter(call=>call[0]==='feedback').length;
 await click('htLimitsBegin');
 console.log(JSON.stringify({before,after:sceneCalls.filter(call=>call[0]==='feedback').length,
 status:elements.get('htTwinStatus').textContent,pose:elements.get('htPose').textContent}));
})();
""")
    assert result["before"] > 0
    assert result["after"] == result["before"]
    assert "未知" in result["status"]
    assert "参考" in result["pose"]


def test_hand_turn_and_stop_preserve_matching_context_with_reordered_record_keys():
    result = evaluate_twin("""
(async()=>{
 prepareTwinContext();twinSnapshot={state:'synced',registered:true,fresh:true,pose:{scene_x_mm:-7,scene_z_mm:3,x_center_mm:42.5,lift_mm:38,feedback_age_s:{X:0.1,Z:0.4},source:'encoder_feedback'},registration:{limits_record:cloneTwin(twinLimitRecord)},error:null};
 HighThroughput.activate();await tick();
 const before=sceneCalls.filter(call=>call[0]==='feedback').length;
 const reorder=value=>value&&typeof value==='object'&&!Array.isArray(value)?Object.fromEntries(Object.keys(value).reverse().map(key=>[key,reorder(value[key])])):value;
 const next=reorder(cloneTwin(PAHighThroughputDevices));
 next.snapshot.motion.axes.X.raw_count=32768;next.snapshot.motion.axes.Z.raw_count=-300;
 next.snapshot.motion.stop_generation++;
 HighThroughput.acceptDevices(next);
 console.log(JSON.stringify({before,after:sceneCalls.filter(call=>call[0]==='feedback').length,
 status:elements.get('htTwinStatus').textContent,pose:elements.get('htPose').textContent}));
})();
""")
    assert result["before"] > 0
    assert result["after"] == result["before"] + 1
    assert "编码器反馈" in result["status"]
    assert "编码器反馈" in result["pose"]
