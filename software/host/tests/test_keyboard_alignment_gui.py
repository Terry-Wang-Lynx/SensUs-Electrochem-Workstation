import pytest

from test_automation_gui import evaluate, evaluate_browser
from test_single_session_gui import SESSION_BOOTSTRAP


INPUT_BOOTSTRAP = """
const calls=[],timers=new Map();let timerId=0,identity=0;
let context={active:true,session_token:'keyboard-session'};
let startGate=null,stopGate=null;const snapshots=[],errors=[];
const keyboard=ui.createKeyboardAlignmentInput({
 request:(route,body)=>{calls.push({route,body});return route.endsWith('/start')&&startGate?startGate:route.endsWith('/stop')&&stopGate?stopGate:Promise.resolve({manual:{active:true,control_mode:'keyboard',session_token:context.session_token,alignment:{status:route.endsWith('/stop')?'idle':'holding',press_id:body.press_id}}});},
 context:()=>context,onSnapshot:value=>snapshots.push(value),onError:error=>errors.push(error.code),
 createId:()=> 'press-'+(++identity),setInterval:callback=>{timers.set(++timerId,callback);return timerId;},clearInterval:id=>timers.delete(id),
});
const key=(value,extra={})=>({key:value,code:value,repeat:false,target:{tagName:'BODY'},preventDefault(){this.prevented=true;},...extra});
const flush=()=>new Promise(resolve=>setImmediate(resolve));
const starts=()=>calls.filter(call=>call.route.endsWith('/start'));
const stops=()=>calls.filter(call=>call.route.endsWith('/stop'));
"""


def evaluate_input(script):
    return evaluate(INPUT_BOOTSTRAP + script)


def test_keyboard_hold_sends_semantic_axis_direction_and_one_heartbeat_per_tick():
    result = evaluate_input("""
(async()=>{
 keyboard.keydown(key('a'));await flush();
 keyboard.keydown(key('a',{repeat:true}));
 for(const callback of timers.values())callback();await flush();
 keyboard.keyup(key('a'));await flush();
 for(const callback of timers.values())callback();await flush();
 console.log(JSON.stringify({calls,timers:timers.size}));
})();
""")
    assert result["calls"] == [
        {"route": "guided/calibration/jog/start", "body": {
            "session_token": "keyboard-session", "press_id": "press-1", "axis": "X", "direction": "left",
        }},
        {"route": "guided/calibration/jog/heartbeat", "body": {
            "session_token": "keyboard-session", "press_id": "press-1", "sequence": 1,
        }},
        {"route": "guided/calibration/jog/stop", "body": {
            "session_token": "keyboard-session", "press_id": "press-1",
        }},
    ]
    assert result["timers"] == 0


def test_keyboard_never_starts_outside_calibration_or_from_text_input_or_modified_keys():
    result = evaluate_input("""
(async()=>{
 for(const target of [{tagName:'INPUT'},{tagName:'textarea'},{tagName:'SELECT'},{tagName:'DIV',isContentEditable:true},{tagName:'SPAN',closest:()=>({})}])keyboard.keydown(key('w',{target}));
 for(const flags of [{ctrlKey:true},{altKey:true},{metaKey:true},{isComposing:true},{keyCode:229},{repeat:true}])keyboard.keydown(key('w',flags));
 context.active=false;keyboard.keydown(key('w'));await flush();
 console.log(JSON.stringify({calls,timers:timers.size}));
})();
""")
    assert result == {"calls": [], "timers": 0}


def test_two_direction_keys_stop_until_every_key_is_released():
    result = evaluate_input("""
(async()=>{
 keyboard.keydown(key('ArrowLeft'));await flush();keyboard.keydown(key('d'));await flush();
 keyboard.keyup(key('ArrowLeft'));keyboard.keydown(key('w'));await flush();
 const blocked=starts().length;keyboard.keyup(key('d'));keyboard.keyup(key('w'));
 keyboard.keydown(key('w'));await flush();keyboard.keyup(key('w'));await flush();
 console.log(JSON.stringify({blocked,starts:starts().map(call=>call.body),stops:stops().length}));
})();
""")
    assert result["blocked"] == 1
    assert result["starts"][-1]["axis"] == "Z"
    assert result["starts"][-1]["direction"] == "up"
    assert result["stops"] == 2


def test_release_before_start_response_tombstones_client_id_and_never_renews():
    result = evaluate_input("""
(async()=>{
 let resolveStart;startGate=new Promise(resolve=>resolveStart=resolve);
 keyboard.keydown(key('s'));await flush();keyboard.keyup(key('s'));await flush();
 const earlyStop=stops()[0];
 resolveStart({manual:{active:true,control_mode:'keyboard',session_token:'keyboard-session',alignment:{status:'holding',press_id:'press-1'}}});await flush();
 for(const callback of timers.values())callback();await flush();
 console.log(JSON.stringify({earlyStop,timers:timers.size,snapshots:snapshots.map(value=>value.manual.alignment.status),calls}));
})();
""")
    assert result["earlyStop"]["body"]["press_id"] == "press-1"
    assert result["timers"] == 0
    assert result["snapshots"] == ["idle"]
    assert len(result["calls"]) == 2


def test_release_before_start_dispatch_sends_neither_motion_nor_stop():
    result = evaluate_input("""
(async()=>{
 keyboard.keydown(key('a'));keyboard.keyup(key('a'));await flush();
 console.log(JSON.stringify({calls,timers:timers.size,errors}));
})();
""")
    assert result == {"calls": [], "timers": 0, "errors": []}


def test_new_direction_waits_for_previous_stop_confirmation_before_start():
    result = evaluate_input("""
(async()=>{
 keyboard.keydown(key('a'));await flush();
 let completeStop;stopGate=new Promise(resolve=>completeStop=resolve);
 keyboard.keyup(key('a'));await flush();keyboard.keydown(key('d'));await flush();
 keyboard.sync({manual:{alignment:{status:'stopping'}}});await flush();
 const whileStopping=starts().length;
 completeStop({manual:{alignment:{status:'idle'}}});await flush();
 console.log(JSON.stringify({whileStopping,afterStop:starts().map(call=>call.body.direction),errors}));
})();
""")
    assert result == {"whileStopping": 1, "afterStop": ["left", "right"], "errors": []}


def test_direction_released_while_waiting_for_old_stop_never_starts_later():
    result = evaluate_input("""
(async()=>{
 keyboard.keydown(key('a'));await flush();
 let completeStop;stopGate=new Promise(resolve=>completeStop=resolve);
 keyboard.keyup(key('a'));await flush();keyboard.keydown(key('d'));keyboard.keyup(key('d'));await flush();
 completeStop({manual:{alignment:{status:'idle'}}});await flush();
 console.log(JSON.stringify({directions:starts().map(call=>call.body.direction),timers:timers.size,errors}));
})();
""")
    assert result == {"directions": ["left"], "timers": 0, "errors": []}


def test_failed_previous_stop_cannot_release_waiting_direction_into_motion():
    result = evaluate_input("""
(async()=>{
 keyboard.keydown(key('a'));await flush();
 let failStop;stopGate=new Promise((resolve,reject)=>failStop=reject);
 keyboard.keyup(key('a'));await flush();keyboard.keydown(key('d'));await flush();
 failStop({code:'backend_stop_failed',message:'STOP unconfirmed'});await flush();
 console.log(JSON.stringify({directions:starts().map(call=>call.body.direction),errors}));
})();
""")
    assert result["directions"] == ["left"]
    assert "backend_stop_failed" in result["errors"]


def test_blur_hidden_tab_and_escape_clear_input_and_do_not_accept_late_start():
    result = evaluate_input("""
(async()=>{
 for(const reason of ['blur','hidden','tab','collapse','focus','settings','capture','finish','cancel','pagehide']){
  keyboard.keydown(key('ArrowRight'));await flush();await keyboard.stop(reason);
  keyboard.keydown(key('ArrowRight',{repeat:true}));
 }
 keyboard.keydown(key('w'));await flush();keyboard.keydown(key('Escape'));await flush();
 context.active=false;keyboard.sync();
 console.log(JSON.stringify({starts:starts().length,stops:stops().length,timers:timers.size}));
})();
""")
    assert result == {"starts": 11, "stops": 11, "timers": 0}


def test_context_loss_and_server_hold_cap_stop_requiring_release():
    result = evaluate_input("""
(async()=>{
 keyboard.keydown(key('a'));await flush();context.active=false;keyboard.sync();await flush();
 context.active=true;keyboard.keydown(key('a'));await flush();
 keyboard.sync({manual:{active:true,control_mode:'keyboard',session_token:context.session_token,alignment:{status:'frozen',press_id:null}}});await flush();
 keyboard.keydown(key('d'));await flush();const beforeRelease=starts().length;
 keyboard.keyup(key('a'));keyboard.keyup(key('d'));keyboard.keydown(key('d'));await flush();
 console.log(JSON.stringify({beforeRelease,starts:starts().length,stops:stops().length}));
})();
""")
    assert result == {"beforeRelease": 2, "starts": 3, "stops": 2}


def test_modifier_pressed_during_hold_stops_even_when_modifier_is_not_direction_key():
    result = evaluate_input("""
(async()=>{
 keyboard.keydown(key('a'));await flush();keyboard.keydown(key('Control',{ctrlKey:true}));await flush();
 for(const callback of timers.values())callback();await flush();
 console.log(JSON.stringify({starts:starts().length,stops:stops().length,timers:timers.size}));
})();
""")
    assert result == {"starts": 1, "stops": 1, "timers": 0}


def test_pointer_and_key_combination_requires_both_released_before_restart():
    result = evaluate_input("""
(async()=>{
 keyboard.pointerdown('7','d');await flush();keyboard.keydown(key('w'));await flush();
 keyboard.pointerup('7');keyboard.keydown(key('a'));await flush();const blocked=starts().length;
 keyboard.keyup(key('w'));keyboard.keyup(key('a'));keyboard.pointerdown('8','s');await flush();keyboard.pointerup('8');await flush();
 console.log(JSON.stringify({blocked,starts:starts().map(call=>call.body.direction),stops:stops().length}));
})();
""")
    assert result == {"blocked": 1, "starts": ["right", "down"], "stops": 2}


def test_canceled_id_capacity_blocks_new_presses_until_a_new_session_without_eviction():
    result = evaluate_input("""
(async()=>{
 for(let press=0;press<4096;press++){keyboard.keydown(key('a'));await flush();await keyboard.stop('release');}
 keyboard.keydown(key('d'));await flush();const full=starts().length;
 context.session_token='new-keyboard-session';keyboard.sync();keyboard.keydown(key('d'));await flush();
 console.log(JSON.stringify({full,afterNewSession:starts().length,last:starts().at(-1).body.session_token}));
})();
""")
    assert result == {"full": 4096, "afterNewSession": 4097, "last": "new-keyboard-session"}


@pytest.mark.parametrize("server_status", ["stopping", "closed"])
def test_server_stop_and_closed_states_cancel_local_renewal(server_status):
    result = evaluate_input("""
(async()=>{
 keyboard.keydown(key('a'));await flush();
 keyboard.sync({manual:{alignment:{status:""" + repr(server_status) + """}}});await flush();
 console.log(JSON.stringify({stops:stops().length,timers:timers.size}));
})();
""")
    assert result == {"stops": 1, "timers": 0}


def test_speed_defaults_and_operator_ceiling_obey_hard_maximum():
    result = evaluate("""
const invalid=[];
for(const values of [[0,1],[0.5,0.4],[0.5,2.1],[NaN,1],[0.5,Infinity]]){
 try{ui.validateJogSettings(...values);}catch(error){invalid.push(error.code);}
}
console.log(JSON.stringify({defaults:ui.validateJogSettings(),allowed:ui.validateJogSettings(2,2),invalid}));
""")
    assert result["defaults"] == {"speed_mm_s": 0.5, "max_speed_mm_s": 1}
    assert result["allowed"] == {"speed_mm_s": 2, "max_speed_mm_s": 2}
    assert result["invalid"] == ["JOG_SPEED_INVALID"] * 5


KEYBOARD_BOOTSTRAP = SESSION_BOOTSTRAP + """
sessionGuided.manual.control_mode='keyboard';sessionGuided.manual.session_token='keyboard-session';
sessionGuided.manual.alignment={status:'idle',session_token:'keyboard-session',press_id:null,settings:{speed_mm_s:0.5,max_speed_mm_s:1,hard_max_speed_mm_s:2,heartbeat_ms:100,lease_ms:350,max_hold_s:30,max_hold_mm:10}};
controllerSnapshot.motion.axes={X:sessionAxis(true),Z:sessionAxis(true)};
controllerSnapshot.motion.manual={active:true,control_mode:'keyboard',session_token:'keyboard-session',connection_token:sessionToken,polling_axis:null};
controllerSnapshot.stop_confirmed=true;controllerSnapshot.recovery_required=false;
const keyboardWindow=new EventTarget();globalThis.addEventListener=keyboardWindow.addEventListener.bind(keyboardWindow);
globalThis.document.addEventListener=keyboardWindow.addEventListener.bind(keyboardWindow);
globalThis.document.hasFocus=()=>true;globalThis.document.visibilityState='visible';globalThis.document.hidden=false;
const keyboardFetch=globalThis.fetch;
let keyboardGlobalStopGate=null;
globalThis.fetch=async(path,options)=>{
 const route=path.replace('/api/automation/v1/','');const body=options.body?JSON.parse(options.body):null;
 if(route==='stop'&&keyboardGlobalStopGate)return keyboardGlobalStopGate;
 if(route==='devices')return limitsResponse({motion:controllerSnapshot.motion});
 if(route==='guided/calibration/jog/stop'){
  requests.push({route,body});return limitsResponse({...sessionGuided,manual:{...sessionGuided.manual,alignment:{...sessionGuided.manual.alignment,status:'idle',stop_result:{stop_confirmed:true}}}});
 }
 if(route.startsWith('guided/calibration/jog/')){requests.push({route,body});return limitsResponse(sessionGuided);}
 return keyboardFetch(path,options);
};
const dispatchKey=(type,key,extra={})=>{const event=new Event(type);Object.assign(event,{key,code:key,repeat:false,...extra});keyboardWindow.dispatchEvent(event);};
"""


def evaluate_keyboard_page(script):
    return evaluate_browser(script, before_source=KEYBOARD_BOOTSTRAP)


def test_reset_button_is_available_in_fault_and_does_not_auto_start_calibration():
    bootstrap = KEYBOARD_BOOTSTRAP + """
const resetFetch=globalThis.fetch;
globalThis.fetch=async(path,options)=>{
 if(path==='/api/automation/v1/motion/reset'){
  requests.push({route:'motion/reset',body:JSON.parse(options.body)});
  controllerSnapshot={...controllerSnapshot,state:'stopped',busy:false,stop_confirmed:true,recovery_required:false};
  sessionGuided.manual={...sessionGuided.manual,active:false,status:'idle',points:{},error:null,session_token:null,
   alignment:{status:'idle',session_token:null}};
  return limitsResponse({state:controllerSnapshot,guided:sessionGuided,motion:controllerSnapshot.motion,message:'设备状态已重置'});
 }
 return resetFetch(path,options);
};
"""
    result = evaluate_browser("""
(async()=>{
 root.classList.toggle('active',true);sessionGuided.manual.active=true;sessionGuided.manual.status='blocked';
 controllerSnapshot={...controllerSnapshot,state:'error',busy:true,stop_confirmed:false,recovery_required:true};
 HighThroughput.activate();await tick();
 if(!elements.has('htResetDevices')){console.log(JSON.stringify({supported:false}));return;}
 const enabled=!elements.get('htResetDevices').disabled;
 await click('htResetDevices');await tick();
 console.log(JSON.stringify({supported:true,enabled,reset:requests.filter(item=>item.route==='motion/reset').length,
  starts:requests.filter(item=>item.route.endsWith('/jog/start')||item.route==='guided/calibration/begin').length,
  errorHidden:elements.get('htError').hidden}));
})();
""", before_source=bootstrap)
    assert result == {"supported": True, "enabled": True, "reset": 1, "starts": 0, "errorHidden": True}


@pytest.mark.parametrize("axis,direction,key,reverse,blocked_button", [
    ("X", "left", "a", "d", "htJogLeft"), ("X", "right", "d", "a", "htJogRight"),
    ("Z", "up", "w", "s", "htJogUp"), ("Z", "down", "s", "w", "htJogDown")])
def test_direction_boundary_disables_only_outward_control_and_allows_reverse(axis, direction, key, reverse, blocked_button):
    result = evaluate_keyboard_page("""
(async()=>{
 root.classList.toggle('active',true);sessionGuided.manual.active=true;sessionGuided.manual.status='aligning';
 sessionGuided.manual.alignment.boundaries=[{axis:AXIS,direction:DIRECTION,at_limit:true,kind:'software'}];
 HighThroughput.activate();await tick();await click('htCalibrationToggle');
 const disabled=['htJogUp','htJogDown','htJogLeft','htJogRight'].filter(id=>elements.get(id).disabled);
 const caption=elements.get('htJogStatus').textContent;
 dispatchKey('keydown',KEY);await tick();dispatchKey('keyup',KEY);await tick();
 const before=requests.filter(item=>item.route.endsWith('/jog/start')).length;
 dispatchKey('keydown',REVERSE);await tick();
 console.log(JSON.stringify({disabled,caption,before,after:requests.filter(item=>item.route.endsWith('/jog/start')).length}));
})();
""".replace("AXIS", repr(axis)).replace("DIRECTION", repr(direction)).replace("REVERSE", repr(reverse)).replace("KEY", repr(key)))
    assert result["disabled"] == [blocked_button]
    assert result["before"] == 0 and result["after"] == 1
    assert "软件边界" in result["caption"] and "反方向" in result["caption"]


@pytest.mark.parametrize("kind,explanation", [("operator", "你设置的方向保护"), ("protocol", "控制器计数范围")])
def test_boundary_message_identifies_the_source_not_a_mechanical_stop(kind, explanation):
    result = evaluate_keyboard_page("""
(async()=>{
 root.classList.toggle('active',true);sessionGuided.manual.active=true;sessionGuided.manual.status='aligning';
 sessionGuided.manual.alignment.boundaries=[{axis:'X',direction:'left',at_limit:true,kind:KIND}];
 HighThroughput.activate();await tick();await click('htCalibrationToggle');
 console.log(JSON.stringify({caption:elements.get('htJogStatus').textContent}));
})();
""".replace("KIND", repr(kind)))
    assert explanation in result["caption"]
    assert "按键 A" in result["caption"]
    assert "不是机械" in result["caption"]


STOP_HANDOFF_BOOTSTRAP = KEYBOARD_BOOTSTRAP + """
Date.now=()=>100000;
controllerSnapshot.motion.manual={active:true,control_mode:'keyboard',session_token:'keyboard-session',connection_token:sessionToken,polling_axis:null};
const deviceEnvelope=motion=>({available:true,error:null,received_at_ms:Date.now(),snapshot:{motion:JSON.parse(JSON.stringify(motion))}});
globalThis.PAHighThroughputDevices=deviceEnvelope(controllerSnapshot.motion);
let completeJogStop,completeDevices;
const jogStopGate=new Promise(resolve=>completeJogStop=resolve);
const devicesGate=new Promise(resolve=>completeDevices=resolve);
const handoffFetch=globalThis.fetch;
const clone=value=>JSON.parse(JSON.stringify(value));
globalThis.fetch=async(path,options)=>{
 const route=path.replace('/api/automation/v1/','');
 if(route==='devices'){requests.push({route,body:null,method:options.method});return devicesGate;}
 if(route==='guided/calibration/jog/start'){
  const body=JSON.parse(options.body);requests.push({route,body});
  sessionGuided.manual.alignment={...sessionGuided.manual.alignment,status:'holding',axis:body.axis,direction:body.direction,press_id:body.press_id};
  return limitsResponse(clone(sessionGuided));
 }
 if(route==='guided/calibration/jog/stop'){
  requests.push({route,body:JSON.parse(options.body)});return jogStopGate;
 }
 return handoffFetch(path,options);
};
const startDirections=()=>requests.filter(item=>item.route.endsWith('/jog/start')).map(item=>item.body.direction);
const beginLongHold=async()=>{
 root.classList.toggle('active',true);sessionGuided.manual.active=true;sessionGuided.manual.status='aligning';
 HighThroughput.activate();await tick();await click('htCalibrationToggle');dispatchKey('keydown','a');await tick();
 controllerSnapshot.motion.manual.polling_axis='X';
 controllerSnapshot.motion.axes.Z={...controllerSnapshot.motion.axes.Z,telemetry_valid:false,feedback_age_s:4,status:3};
 HighThroughput.acceptDevices(deviceEnvelope(controllerSnapshot.motion));await tick();
 dispatchKey('keyup','a');await tick();dispatchKey('keydown','w');await tick();
};
const confirmJogStop=async()=>{
 sessionGuided.manual.alignment={...sessionGuided.manual.alignment,status:'idle',press_id:null,last_stop_reason:'released',stop_result:{stop_confirmed:true}};
 completeJogStop(limitsResponse(clone(sessionGuided)));await tick();
};
const stoppedDevices=()=>{
 const motion=clone(controllerSnapshot.motion);
 motion.axes={X:sessionAxis(true),Z:sessionAxis(true)};motion.manual.polling_axis=null;
 return {motion};
};
"""


def test_long_hold_switches_axis_after_confirmed_stop_and_cached_devices_without_timer():
    result = evaluate_browser("""
(async()=>{
 await beginLongHold();const beforeStop=startDirections();
 await confirmJogStop();const beforeDevices=startDirections();
 completeDevices(limitsResponse(stoppedDevices()));await tick();
 console.log(JSON.stringify({beforeStop,beforeDevices,afterDevices:startDirections(),
  reads:requests.filter(item=>item.route==='devices').map(item=>item.method)}));
})();
""", before_source=STOP_HANDOFF_BOOTSTRAP)
    assert result == {"beforeStop": ["left"], "beforeDevices": ["left"],
                      "afterDevices": ["left", "up"], "reads": ["GET"]}


def test_released_queued_direction_never_restarts_after_cached_devices_arrive():
    result = evaluate_browser("""
(async()=>{
 await beginLongHold();await confirmJogStop();dispatchKey('keyup','w');await tick();
 completeDevices(limitsResponse(stoppedDevices()));await tick();
 console.log(JSON.stringify({directions:startDirections(),read:requests.some(item=>item.route==='devices')}));
})();
""", before_source=STOP_HANDOFF_BOOTSTRAP)
    assert result == {"directions": ["left"], "read": True}


@pytest.mark.parametrize("queued_taps", [1, 5, 20])
def test_releasing_unsent_direction_during_real_stop_does_not_latch_false_stop_error(queued_taps):
    bootstrap = STOP_HANDOFF_BOOTSTRAP + """
const replayFetch=globalThis.fetch;
globalThis.fetch=async(path,options)=>{
 const route=path.replace('/api/automation/v1/','');
 if(route==='guided/calibration/jog/stop'&&requests.some(item=>item.route===route)){
  requests.push({route,body:JSON.parse(options.body)});
  const stopping=clone(sessionGuided);
  stopping.manual.alignment={...stopping.manual.alignment,status:'stopping',stop_result:{stop_confirmed:true}};
  return limitsResponse(stopping);
 }
 return replayFetch(path,options);
};
"""
    result = evaluate_browser("""
(async()=>{
 await beginLongHold();dispatchKey('keyup','w');await tick();
 for(let tap=1;tap<QUEUED_TAPS;tap++){
  dispatchKey('keydown','d');await tick();dispatchKey('keyup','d');await tick();
 }
 const errorDuringStop=elements.get('htErrorDetailText').textContent;
 await confirmJogStop();completeDevices(limitsResponse(stoppedDevices()));await tick();
 dispatchKey('keydown','d');await tick();
 console.log(JSON.stringify({errorDuringStop,directions:startDirections(),
  stops:requests.filter(item=>item.route.endsWith('/jog/stop')).length}));
})();
""".replace("QUEUED_TAPS", str(queued_taps)), before_source=bootstrap)
    assert result == {"errorDuringStop": "", "directions": ["left", "right"], "stops": 1}


@pytest.mark.parametrize("invalid", [
    "delete fresh.motion;",
    "fresh.motion.connection_token='another-connection';",
    "fresh.motion.manual.session_token='another-session';",
    "fresh.motion.axes.Z.feedback_age_s=3;",
    "delete fresh.motion.axes.Z.feedback_age_s;",
    "fresh.motion.axes.Z.telemetry_valid=false;",
    "fresh.motion.axes.Z.status=7;",
    "fresh.motion.axes.Z.speed_rpm=1;",
])
def test_stop_handoff_rejects_invalid_or_unrelated_cached_evidence(invalid):
    result = evaluate_browser("""
(async()=>{
 await beginLongHold();await confirmJogStop();const fresh=stoppedDevices();
 """ + invalid + """
 completeDevices(limitsResponse(fresh));await tick();
 console.log(JSON.stringify({directions:startDirections(),read:requests.some(item=>item.route==='devices')}));
})();
""", before_source=STOP_HANDOFF_BOOTSTRAP)
    assert result == {"directions": ["left"], "read": True}


@pytest.mark.parametrize("enabled", [False, True])
def test_late_stop_devices_cannot_overwrite_a_new_connection(enabled):
    result = evaluate_browser("""
(async()=>{
 await beginLongHold();await confirmJogStop();const oldDevices=stoppedDevices();
 const newer=stoppedDevices();newer.motion.connection_token='new-connection';newer.motion.manual.connection_token='new-connection';
 newer.motion.axes={X:sessionAxis(""" + str(enabled).lower() + """),Z:sessionAxis(""" + str(enabled).lower() + """)};
 globalThis.PAHighThroughputDevices=deviceEnvelope(newer.motion);HighThroughput.acceptDevices(globalThis.PAHighThroughputDevices);await tick();
 completeDevices(limitsResponse(oldDevices));await tick();
 console.log(JSON.stringify({directions:startDirections(),connection:globalThis.PAHighThroughputDevices.snapshot.motion.connection_token,
  axisStatus:elements.get('htCalibrationAxisStatus').textContent,read:requests.some(item=>item.route==='devices')}));
})();
""", before_source=STOP_HANDOFF_BOOTSTRAP)
    assert result["directions"] == ["left"]
    assert result["connection"] == "new-connection"
    assert ("已锁轴" in result["axisStatus"]) is enabled
    assert result["read"]


def test_guided_idle_poll_cannot_drop_queued_direction_before_stop_devices_arrive():
    result = evaluate_browser("""
(async()=>{
 await beginLongHold();await confirmJogStop();
 HighThroughput.activate();await tick();const beforeDevices=startDirections();
 completeDevices(limitsResponse(stoppedDevices()));await tick();
 console.log(JSON.stringify({beforeDevices,afterDevices:startDirections()}));
})();
""", before_source=STOP_HANDOFF_BOOTSTRAP)
    assert result == {"beforeDevices": ["left"], "afterDevices": ["left", "up"]}


def test_confirmed_stop_with_serial_fault_does_not_claim_stop_is_unknown():
    result = evaluate_browser("""
(async()=>{
 await beginLongHold();const failed=clone(sessionGuided);
 failed.manual.status='blocked';
 failed.manual.alignment={...failed.manual.alignment,status:'blocked',stop_result:{stop_confirmed:true},
  error:{code:'response_length',message:'Unexpected serial response length',wire:{pending_hex:'0100ee6b'}}};
 completeJogStop(limitsResponse(failed));await tick();
 console.log(JSON.stringify({detail:elements.get('htErrorDetailText').textContent,
  message:elements.get('htError').textContent,directions:startDirections()}));
})();
""", before_source=STOP_HANDOFF_BOOTSTRAP)
    assert "双轴已确认停止" in result["message"]
    assert "命令格式错误" in result["message"]
    assert "停止尚未确认" not in result["message"]
    assert "JOG_STOPPED_FAULT" in result["detail"]
    assert result["directions"] == ["left"]


def test_device_fetch_latency_counts_toward_feedback_age():
    result = evaluate_browser("""
(async()=>{
 await beginLongHold();await confirmJogStop();const fresh=stoppedDevices();
 Date.now=()=>103000;completeDevices(limitsResponse(fresh));await tick();
 console.log(JSON.stringify({directions:startDirections(),read:requests.some(item=>item.route==='devices')}));
})();
""", before_source=STOP_HANDOFF_BOOTSTRAP)
    assert result == {"directions": ["left"], "read": True}


@pytest.mark.parametrize("invalid", [
    "delete stopped.manual.alignment.stop_result;",
    "stopped.manual.alignment.stop_result.stop_confirmed=false;",
    "stopped.manual.alignment.status='stopping';",
    "stopped.manual.session_token='old-session';",
])
def test_unconfirmed_stop_cannot_fetch_devices_or_release_queued_direction(invalid):
    result = evaluate_browser("""
(async()=>{
 await beginLongHold();const stopped=clone(sessionGuided);
 stopped.manual.alignment={...stopped.manual.alignment,status:'idle',stop_result:{stop_confirmed:true}};
 """ + invalid + """
 completeJogStop(limitsResponse(stopped));await tick();
 completeDevices(limitsResponse(stoppedDevices()));await tick();
 console.log(JSON.stringify({directions:startDirections(),read:requests.some(item=>item.route==='devices')}));
})();
""", before_source=STOP_HANDOFF_BOOTSTRAP)
    assert result == {"directions": ["left"], "read": False}


def test_real_page_polling_previous_stop_does_not_drop_the_next_held_direction():
    bootstrap = KEYBOARD_BOOTSTRAP + """
let completeJogStop;
const jogStopGate=new Promise(resolve=>completeJogStop=resolve);
const originalFetch=globalThis.fetch;
globalThis.fetch=async(path,options)=>{
 const route=path.replace('/api/automation/v1/','');
 if(route==='guided')return limitsResponse(JSON.parse(JSON.stringify(sessionGuided)));
 if(route==='guided/calibration/jog/stop'){
  requests.push({route,body:JSON.parse(options.body)});return jogStopGate;
 }
 return originalFetch(path,options);
};
"""
    result = evaluate_browser("""
(async()=>{
 root.classList.toggle('active',true);sessionGuided.manual.active=true;sessionGuided.manual.status='aligning';
 HighThroughput.activate();await tick();await click('htCalibrationToggle');
 dispatchKey('keydown','a');await tick();dispatchKey('keyup','a');await tick();dispatchKey('keydown','d');await tick();
 const whileStopping=requests.filter(item=>item.route.endsWith('/jog/start')).length;
 sessionGuided.manual.alignment.status='stopping';HighThroughput.activate();await tick();
 sessionGuided.manual.alignment.status='idle';sessionGuided.manual.alignment.stop_result={stop_confirmed:true};
 completeJogStop(limitsResponse(JSON.parse(JSON.stringify(sessionGuided))));await tick();
 console.log(JSON.stringify({whileStopping,directions:requests.filter(item=>item.route.endsWith('/jog/start')).map(item=>item.body.direction)}));
})();
""", before_source=bootstrap)
    assert result == {"whileStopping": 1, "directions": ["left", "right"]}


def test_ordinary_keyboard_calibration_has_clearance_confirmation_and_explicit_begin_payload():
    result = evaluate_keyboard_page("""
(async()=>{
 root.classList.toggle('active',true);HighThroughput.activate();await tick();
 const supported=elements.has('htCalibrationMotionConfirmed');if(!supported){console.log(JSON.stringify({supported}));return;}
 await click('htCalibrationToggle');
 const disabled=elements.get('htCalibrationBegin').disabled;
 elements.get('htCalibrationMotionConfirmed').checked=true;change('htCalibrationMotionConfirmed');await click('htCalibrationBegin');
 console.log(JSON.stringify({supported,disabled,begin:requests.filter(request=>request.route==='guided/calibration/begin'),
 speed:elements.get('htJogSpeed').value,max:elements.get('htJogMaxSpeed').value,
 legacyHidden:elements.get('htLegacyManualConfirmations').hidden,
 stopDisabled:!!elements.get('htStop').disabled}));
})();
""")
    assert result["supported"]
    assert result["disabled"]
    assert result["begin"] == [{"route": "guided/calibration/begin", "body": {
        "control_mode": "keyboard", "motion_confirmed": True,
    }}]
    assert result["speed"] == "0.5"
    assert result["max"] == "1"
    assert result["legacyHidden"]
    assert result["stopDisabled"] is False


def test_page_keyboard_handlers_require_visible_focused_calibration_and_stop_on_blur():
    result = evaluate_keyboard_page("""
(async()=>{
 root.classList.toggle('active',true);HighThroughput.activate();await tick();
 dispatchKey('keydown','a');await tick();const before=requests.filter(request=>request.route.endsWith('/jog/start')).length;
 if(!elements.has('htCalibrationMotionConfirmed')){console.log(JSON.stringify({supported:false,before}));return;}
 await click('htCalibrationToggle');elements.get('htCalibrationMotionConfirmed').checked=true;change('htCalibrationMotionConfirmed');await click('htCalibrationBegin');
 dispatchKey('keydown','a');await tick();keyboardWindow.dispatchEvent(new Event('blur'));await tick();
 console.log(JSON.stringify({supported:true,before,jogs:requests.filter(request=>request.route.includes('/jog/')).map(request=>request.route),stopDisabled:!!elements.get('htStop').disabled}));
})();
""")
    assert result["supported"]
    assert result["before"] == 0
    assert result["jogs"] == ["guided/calibration/jog/start", "guided/calibration/jog/stop"]
    assert result["stopDisabled"] is False


def test_global_stop_revokes_local_keyboard_entitlement_before_response():
    result = evaluate_keyboard_page("""
(async()=>{
 root.classList.toggle('active',true);HighThroughput.activate();await tick();
 await click('htCalibrationToggle');elements.get('htCalibrationMotionConfirmed').checked=true;change('htCalibrationMotionConfirmed');await click('htCalibrationBegin');
 dispatchKey('keydown','a');await tick();
 let resolveStop;keyboardGlobalStopGate=new Promise(resolve=>resolveStop=resolve);
 elements.get('htStop').dispatchEvent(new Event('click'));dispatchKey('keydown','d');await tick();
 const during=requests.filter(request=>request.route.endsWith('/jog/start')).length;
 const actionsBlocked=['htCaptureFirst','htJogSpeed','htJogApply'].every(id=>elements.get(id).disabled);
 resolveStop(limitsResponse(controllerSnapshot));await tick();
 console.log(JSON.stringify({during,actionsBlocked,stops:requests.filter(request=>request.route.endsWith('/jog/stop')).length}));
})();
""")
    assert result == {"during": 1, "actionsBlocked": True, "stops": 1}


def test_interrupted_keyboard_session_can_explicitly_restart_after_confirmed_stop():
    result = evaluate_keyboard_page("""
(async()=>{
 root.classList.toggle('active',true);sessionGuided.manual={...sessionGuided.manual,active:true,status:'blocked',error:{code:'manual_interrupted'}};
 controllerSnapshot.stop_confirmed=true;controllerSnapshot.recovery_required=false;
 HighThroughput.activate();await tick();
 elements.get('htCalibrationMotionConfirmed').checked=true;change('htCalibrationMotionConfirmed');
 const enabled=!elements.get('htCalibrationBegin').disabled;await click('htCalibrationBegin');
 console.log(JSON.stringify({enabled,begin:requests.filter(request=>request.route==='guided/calibration/begin').length,
 saves:requests.filter(request=>request.route==='guided/experiment').length}));
})();
""")
    assert result == {"enabled": True, "begin": 1, "saves": 0}


def test_keyboard_begin_can_explicitly_connect_saved_port_but_recovery_stays_blocked():
    result = evaluate_keyboard_page("""
(async()=>{
 root.classList.toggle('active',true);experiment.bindings.motion={port:'saved-port',baud:115200,axes:{X:{lead_mm:2},Z:{lead_mm:2}}};controllerSnapshot.motion.connected=false;
 HighThroughput.activate();await tick();elements.get('htCalibrationMotionConfirmed').checked=true;change('htCalibrationMotionConfirmed');
 const disconnectedEnabled=!elements.get('htCalibrationBegin').disabled;await click('htCalibrationBegin');
 sessionGuided.manual.active=false;controllerSnapshot.recovery_required=true;HighThroughput.activate();await tick();
 elements.get('htCalibrationMotionConfirmed').checked=true;change('htCalibrationMotionConfirmed');
 console.log(JSON.stringify({disconnectedEnabled,begin:requests.filter(request=>request.route==='guided/calibration/begin').length,recoveryBlocked:elements.get('htCalibrationBegin').disabled}));
})();
""")
    assert result == {"disconnectedEnabled": True, "begin": 1, "recoveryBlocked": True}


@pytest.mark.parametrize("stop_action", [
    "document.hidden=true;keyboardWindow.dispatchEvent(new Event('visibilitychange'));",
    "keyboardWindow.dispatchEvent(new Event('pagehide'));",
    "keyboardWindow.dispatchEvent(new Event('focusin'));",
    "keyboardWindow.dispatchEvent(new Event('compositionstart'));",
    "HighThroughput.deactivate();",
    "await click('htCalibrationHide');",
    "input('htJogSpeed','0.6');",
    "await click('htCaptureFirst');",
])
def test_page_lifecycle_and_control_changes_stop_active_input(stop_action):
    result = evaluate_keyboard_page("""
(async()=>{
 root.classList.toggle('active',true);HighThroughput.activate();await tick();
 await click('htCalibrationToggle');elements.get('htCalibrationMotionConfirmed').checked=true;change('htCalibrationMotionConfirmed');await click('htCalibrationBegin');
 dispatchKey('keydown','a');await tick();
 """ + stop_action + """
 await tick();dispatchKey('keydown','a',{repeat:true});await tick();
 console.log(JSON.stringify({starts:requests.filter(request=>request.route.endsWith('/jog/start')).length,
 stops:requests.filter(request=>request.route.endsWith('/jog/stop')).length}));
})();
""")
    assert result == {"starts": 1, "stops": 1}


def test_settings_apply_stops_first_then_sends_session_bound_values_and_invalid_values_never_send():
    result = evaluate_keyboard_page("""
(async()=>{
 root.classList.toggle('active',true);HighThroughput.activate();await tick();
 await click('htCalibrationToggle');elements.get('htCalibrationMotionConfirmed').checked=true;change('htCalibrationMotionConfirmed');await click('htCalibrationBegin');
 dispatchKey('keydown','a');await tick();input('htJogSpeed','0.7');input('htJogMaxSpeed','1.2');await click('htJogApply');
 const first=requests.filter(request=>request.route.includes('/jog/'));
 input('htJogMaxSpeed','2.1');await click('htJogApply');
 console.log(JSON.stringify({first,configCount:requests.filter(request=>request.route.endsWith('/jog/config')).length,error:elements.get('htError').textContent}));
})();
""")
    assert [request["route"].rsplit("/", 1)[-1] for request in result["first"]] == ["start", "stop", "config"]
    assert result["first"][-1]["body"] == {
        "session_token": "keyboard-session", "speed_mm_s": 0.7, "max_speed_mm_s": 1.2,
    }
    assert result["configCount"] == 1
    assert "速度" in result["error"]
    assert "JOG_SPEED_INVALID" not in result["error"]


def test_new_hold_does_not_display_an_old_hold_limit_as_a_current_stop():
    result = evaluate_keyboard_page("""
(async()=>{
 root.classList.toggle('active',true);sessionGuided.manual.active=true;
 sessionGuided.manual.alignment={...sessionGuided.manual.alignment,status:'holding',press_id:'current',last_stop_reason:'hold_limit',axis:'X',direction:'left'};
 HighThroughput.activate();await tick();console.log(JSON.stringify({status:elements.get('htJogStatus').textContent}));
})();
""")
    assert "正在按住对位" in result["status"]
    assert "已到持续按住上限" not in result["status"]


@pytest.mark.parametrize("invalid", ["", "active_stale", "wrong_session", "wrong_axis", "peer_fault"])
def test_long_hold_uses_current_driver_ownership_when_only_the_held_axis_is_stale(invalid):
    changes = {
        "": "",
        "active_stale": "controllerSnapshot.motion.axes.X.telemetry_valid=false;controllerSnapshot.motion.axes.X.feedback_age_s=3;",
        "wrong_session": "controllerSnapshot.motion.manual.session_token='old-session';",
        "wrong_axis": "controllerSnapshot.motion.manual.polling_axis='Z';",
        "peer_fault": "controllerSnapshot.motion.axes.Z.status=7;",
    }
    result = evaluate_keyboard_page("""
(async()=>{
 root.classList.toggle('active',true);sessionGuided.manual.active=true;sessionGuided.manual.status='aligning';
 HighThroughput.activate();await tick();await click('htCalibrationToggle');
 dispatchKey('keydown','a');await tick();
 const press=requests.find(item=>item.route.endsWith('/jog/start')).body.press_id;
 sessionGuided.manual.alignment={...sessionGuided.manual.alignment,status:'holding',press_id:press,axis:'X',direction:'left'};
 controllerSnapshot.motion.manual={active:true,control_mode:'keyboard',session_token:'keyboard-session',connection_token:controllerSnapshot.motion.connection_token,polling_axis:'X'};
 controllerSnapshot.motion.axes.Z={...controllerSnapshot.motion.axes.Z,telemetry_valid:false,feedback_age_s:3,status:3};
 """ + changes[invalid] + """
 HighThroughput.activate();await tick();
 const stopped=requests.filter(item=>item.route.endsWith('/jog/stop')).length;
 dispatchKey('keyup','a');await tick();
 console.log(JSON.stringify({stopped,totalStops:requests.filter(item=>item.route.endsWith('/jog/stop')).length}));
})();
""")
    assert result == {"stopped": 0 if not invalid else 1, "totalStops": 1}


def test_controller_busy_owned_by_keyboard_calibration_keeps_jog_settings_and_capture_available():
    result = evaluate_keyboard_page("""
(async()=>{
 root.classList.toggle('active',true);sessionGuided.manual={...sessionGuided.manual,active:true,status:'aligning'};
 controllerSnapshot={...controllerSnapshot,busy:true,state:'stopped',stop_confirmed:true,recovery_required:false};
 HighThroughput.activate();await tick();await click('htCalibrationToggle');
 const controls=['htJogLeft','htJogSpeed','htJogMaxSpeed','htJogApply','htCaptureFirst'].map(id=>elements.get(id).disabled);
 dispatchKey('keydown','a');await tick();dispatchKey('keyup','a');await tick();
 input('htJogSpeed','0.6');await click('htJogApply');await click('htCaptureFirst');
 console.log(JSON.stringify({controls,operations:requests.filter(request=>request.route.includes('/jog/')||request.route.endsWith('/calibration/capture')).map(request=>request.route.split('/').at(-1)),
 experimentBlocked:elements.get('htRun').disabled,globalStopEnabled:!elements.get('htStop').disabled}));
})();
""")
    assert result == {"controls": [False] * 5, "operations": ["start", "stop", "config", "capture"],
                      "experimentBlocked": True, "globalStopEnabled": True}


@pytest.mark.parametrize("unsafe_controller", [
    "{busy:true,state:'running',stop_confirmed:true,recovery_required:false}",
    "{busy:true,state:'preparing',stop_confirmed:true,recovery_required:false}",
    "{busy:true,state:'paused',stop_confirmed:true,recovery_required:false}",
    "{busy:true,state:'waiting_operator',stop_confirmed:true,recovery_required:false}",
    "{busy:true,state:'waiting_recovery',stop_confirmed:true,recovery_required:false}",
    "{busy:true,state:'stopping',stop_confirmed:true,recovery_required:false}",
    "{busy:true,state:'stopped',stop_confirmed:true,recovery_required:true}",
    "{busy:true,state:'stopped',stop_confirmed:false,recovery_required:false}",
])
def test_keyboard_calibration_never_exempts_auto_work_or_unknown_stop_from_busy_guard(unsafe_controller):
    result = evaluate_keyboard_page("""
(async()=>{
 root.classList.toggle('active',true);sessionGuided.manual={...sessionGuided.manual,active:true,status:'aligning'};
 controllerSnapshot={...controllerSnapshot,...""" + unsafe_controller + """};
 HighThroughput.activate();await tick();await click('htCalibrationToggle');dispatchKey('keydown','a');await tick();
 console.log(JSON.stringify({blocked:['htJogLeft','htJogSpeed','htJogMaxSpeed','htJogApply','htCaptureFirst'].every(id=>elements.get(id).disabled),
 starts:requests.filter(request=>request.route.endsWith('/jog/start')).length,globalStopEnabled:!elements.get('htStop').disabled}));
})();
""")
    assert result == {"blocked": True, "starts": 0, "globalStopEnabled": True}


def test_interrupted_startup_without_owned_keyboard_token_only_offers_explicit_retry_and_cancel():
    result = evaluate_keyboard_page("""
(async()=>{
 root.classList.toggle('active',true);
 sessionGuided.manual={...sessionGuided.manual,active:true,status:'blocked',session_token:null,error:{code:'manual_interrupted',message:'Interrupted manual calibration requires explicit safe recovery'},alignment:{...sessionGuided.manual.alignment,session_token:null}};
 controllerSnapshot={...controllerSnapshot,busy:true,state:'stopped',stop_confirmed:true,recovery_required:false};
 controllerSnapshot.motion.axes={X:sessionAxis(false),Z:sessionAxis(false)};
 HighThroughput.activate();await tick();await click('htCalibrationToggle');
 const beforeConfirm=elements.get('htCalibrationBegin').disabled;
 elements.get('htCalibrationMotionConfirmed').checked=true;change('htCalibrationMotionConfirmed');
 dispatchKey('keydown','a');await tick();await click('htJogApply');await click('htCaptureFirst');
 console.log(JSON.stringify({beforeConfirm,retryEnabled:!elements.get('htCalibrationBegin').disabled,
 invalidActionsDisabled:['htCaptureFirst','htCaptureLast','htCaptureWorking','htCaptureSafe','htCalibrationFinish','htJogSpeed','htJogMaxSpeed','htJogApply','htJogLeft'].every(id=>elements.get(id).disabled),
 cancelEnabled:!elements.get('htCalibrationCancel').disabled,stopEnabled:!elements.get('htStop').disabled,
 progress:elements.get('htCalibrationProgress').textContent,writes:requests.filter(request=>request.route.includes('/calibration/')&&request.body!==null)}));
})();
""")
    assert result["beforeConfirm"]
    assert result["retryEnabled"]
    assert result["invalidActionsDisabled"]
    assert result["cancelEnabled"]
    assert result["stopEnabled"]
    assert "上次标定已中断，请重新进入键盘标定；勿沿用旧点位" in result["progress"]
    assert "Interrupted manual" not in result["progress"]
    assert result["writes"] == []


@pytest.mark.parametrize("invalid_session", [
    "sessionGuided.manual.alignment.session_token='other-session';",
    "sessionGuided.manual.session_token=null;",
    "sessionGuided.manual.status='blocked';",
    "sessionGuided.manual.alignment.status='blocked';",
    "controllerSnapshot.motion.axes.X.enabled=false;",
    "controllerSnapshot.motion.axes.Z.telemetry_valid=false;",
])
def test_capture_settings_and_jog_require_owned_healthy_keyboard_session(invalid_session):
    result = evaluate_keyboard_page("""
(async()=>{
 root.classList.toggle('active',true);sessionGuided.manual={...sessionGuided.manual,active:true,status:'aligning'};
 controllerSnapshot={...controllerSnapshot,busy:true,state:'stopped',stop_confirmed:true,recovery_required:false};
 """ + invalid_session + """
 HighThroughput.activate();await tick();await click('htCalibrationToggle');dispatchKey('keydown','a');await tick();
 await click('htJogApply');await click('htCaptureFirst');
 console.log(JSON.stringify({blocked:['htCaptureFirst','htCaptureLast','htCaptureWorking','htCaptureSafe','htJogSpeed','htJogMaxSpeed','htJogApply','htJogLeft'].every(id=>elements.get(id).disabled),
 writes:requests.filter(request=>request.route.includes('/calibration/')&&request.body!==null),cancelEnabled:!elements.get('htCalibrationCancel').disabled}));
})();
""")
    assert result == {"blocked": True, "writes": [], "cancelEnabled": True}
