import json
import subprocess
from pathlib import Path


GUI = Path(__file__).parents[1] / "pa_host" / "gui"


def evaluate(script):
    if not (GUI / "high_throughput.js").exists():
        raise AssertionError("Automation editor is not implemented")
    completed = subprocess.run(
        ["node", "-e", f"const ui = require({json.dumps(str(GUI / 'high_throughput.js'))});\n{script}"],
        capture_output=True, text=True, check=True,
    )
    return json.loads(completed.stdout)


def evaluate_browser(script, source=None, before_source=""):
    bootstrap = """
const elements=new Map();
class Element extends EventTarget {
 constructor(tag){super();this.tagName=tag;this.children=[];this.value='';this.checked=false;this.hidden=false;this.dataset={};this.attributes={};this.className='';this.textContent='';const classes=new Set();this.classList={toggle:(name,on)=>on?classes.add(name):classes.delete(name),contains:name=>classes.has(name)};}
 set id(value){this._id=value;elements.set(value,this);} get id(){return this._id;}
 set value(value){this._value=String(value);} get value(){return this._value;}
 set innerHTML(value){for(const match of value.matchAll(/<([a-z]+)[^>]*\\bid="([^"]+)"[^>]*>/g)){const child=new Element(match[1]);child.id=match[2];child.hidden=/\\bhidden\\b/.test(match[0]);this.children.push(child);}}
 setAttribute(name,value){this.attributes[name]=value;}
 append(...children){this.children.push(...children);}
 replaceChildren(...children){this.children=children;}
 get options(){return this.children;}
 get childElementCount(){return this.children.length;}
 get firstElementChild(){return this.children[0];}
 querySelector(selector){if(selector.startsWith('#'))return elements.get(selector.slice(1));return this.children.find(child=>selector.startsWith('.')?child.className.split(' ').includes(selector.slice(1)):child.tagName===selector)||this.children.map(child=>child.querySelector(selector)).find(Boolean);}
 querySelectorAll(){return [];}
}
const root=new Element('div');root.id='view-throughput';
globalThis.document={getElementById:id=>elements.get(id),createElement:tag=>new Element(tag)};
globalThis.requestAnimationFrame=()=>0;globalThis.setInterval=()=>0;
const requests=[];
globalThis.fetch=async(path,options)=>{
 const route=path.replace('/api/automation/v1/','');
 requests.push({route,body:options.body?JSON.parse(options.body):null});
 const response=route==='guided'?{document:experiment,calibration:null,manual:{active:false}}:route==='state'?{state:'idle',busy:false,channels:[],events:[]}:route.startsWith('events?')?{events:[]}:route==='guided/experiment'?{document:JSON.parse(options.body).document}:null;
 if(response===null)throw new Error('Unexpected endpoint '+route);
 return {ok:true,json:async()=>response};
};
const tick=()=>new Promise(resolve=>setImmediate(resolve));
const input=(id,value)=>{const field=elements.get(id);field.value=String(value);field.dispatchEvent(new Event('input'));};
const click=async id=>{elements.get(id).dispatchEvent(new Event('click'));await tick();};
"""
    source = source if source is not None else (GUI / "high_throughput.js").read_text()
    completed = subprocess.run(
        ["node", "-e", GUIDED_DOCUMENT + bootstrap + before_source + f"\nconst browserModule={{exports:{{}}}};new Function('module',{json.dumps(source)})(browserModule);const ui=browserModule.exports;\n" + script],
        capture_output=True, text=True,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


DOCUMENT = """
const experiment = {schema_version:'1.0', name:'run', defaults:{dwell_s:180},
  columns:[{index:1,solution:'PBS',concentration:{value:0,unit:'uM'},overrides:{}}],
  program:[], safety:{motion_policy_confirmed:false}};
"""


def test_column_selection_is_read_only_and_edits_invalidate_compiled_plan():
    result = evaluate(DOCUMENT + """
const model=ui.createModel(experiment);
const original=model.serialize();
model.acceptPlan({plan_hash:'hash',steps:[]}, original);
model.selectColumn(8);
const selection={unchanged:model.serialize()===original,compiled:!!model.plan};
model.updateColumn(8,{solution:'<img src=x onerror=alert(1)>',concentration:{value:5,unit:'mM'},overrides:{dwell_s:30}});
console.log(JSON.stringify({selection,plan:model.plan,columns:model.document.columns}));
""")
    assert result["selection"] == {"unchanged": True, "compiled": True}
    assert result["plan"] is None
    assert result["columns"][1]["index"] == 8
    assert result["columns"][1]["solution"] == "<img src=x onerror=alert(1)>"


def test_late_preview_cannot_replace_newer_document_plan():
    result = evaluate(DOCUMENT + """
const model=ui.createModel(experiment), previous=model.serialize();
model.edit(document=>document.defaults.dwell_s=20);
let error;
try{model.acceptPlan({plan_hash:'old'},previous);}catch(failure){error=failure.code;}
console.log(JSON.stringify({error,plan:model.plan}));
""")
    assert result == {"error": "STALE_PLAN", "plan": None}


def test_run_requires_current_preview_and_explicit_live_confirmation():
    result = evaluate(DOCUMENT + """
const model=ui.createModel(experiment), failures=[];
try{model.runPayload('simulate',false,'run-1');}catch(error){failures.push(error.code);}
model.acceptPlan({plan_hash:'hash',steps:[]},model.serialize());
const simulated=model.runPayload('simulate',false,'run-1');
try{model.runPayload('live',false,'run-1');}catch(error){failures.push(error.code);}
try{model.runPayload('live',true,'run-1');}catch(error){failures.push(error.code);}
model.edit(document=>document.safety.motion_policy_confirmed=true);
model.acceptPlan({plan_hash:'new-hash',steps:[]},model.serialize());
try{model.runPayload('live',true,'run-2');}catch(error){failures.push(error.code);}
model.edit(document=>document.safety.electrical_policy_confirmed=true);
model.acceptPlan({plan_hash:'new-hash',steps:[]},model.serialize());
const live=model.runPayload('live',true,'run-2');
console.log(JSON.stringify({failures,simulated,live}));
""")
    assert result["failures"] == ["PREVIEW_REQUIRED", "LIVE_CONFIRMATION_REQUIRED", "MOTION_POLICY_REQUIRED", "ELECTRICAL_POLICY_REQUIRED"]
    assert result["simulated"]["mode"] == "simulate"
    assert result["simulated"]["plan_hash"] == "hash"
    assert result["live"]["idempotency_key"] == "run-2"


def test_action_buttons_emit_declarative_actions_with_selected_acquisition_channels():
    result = evaluate("""
const actions=['move_to_column','immerse','dwell','withdraw','wait','wait_operator','repeat']
 .map(op=>ui.actionFor(op,8,['A','C','E','G']));
let blocked;try{ui.actionFor('eval',1,[]);}catch(error){blocked=error.code;}
console.log(JSON.stringify({actions,blocked}));
""")
    assert result["actions"][0] == {"op": "move_to_column", "column": 8}
    assert result["actions"][2] == {"op": "dwell", "acquire": ["A", "C", "E", "G"]}
    assert result["actions"][-1] == {"op": "repeat", "count": 2, "body": [{"op": "wait", "seconds": 2}]}
    assert result["blocked"] == "UNKNOWN_ACTION"


def test_api_client_preserves_structured_error_and_uses_versioned_json_payload():
    result = evaluate("""
(async()=>{
 const requests=[];
 const client=ui.createApi(async(path, options)=>{
  requests.push({path,method:options.method,body:JSON.parse(options.body)});
  return {ok:false,status:409,json:async()=>({error:{code:'PLAN_MISMATCH',message:'changed',path:'plan_hash',request_id:'req-5'}})};
 });
 let failure;try{await client('run',{plan_hash:'abc'});}catch(error){failure={code:error.code,message:error.message,path:error.path,request_id:error.request_id};}
 console.log(JSON.stringify({requests,failure}));
})();
""")
    assert result["requests"] == [{"path": "/api/automation/v1/run", "method": "POST", "body": {"plan_hash": "abc"}}]
    assert result["failure"] == {"code": "PLAN_MISMATCH", "message": "changed", "path": "plan_hash", "request_id": "req-5"}


def test_unverified_motion_position_never_becomes_preview_position():
    result = evaluate("""
console.log(JSON.stringify([
 ui.positionCaption({mode:'simulate',position:{X:9,Z:6}}),
 ui.positionCaption({mode:'live',position:{X:null,Z:null}}),
 ui.positionCaption({mode:'live',position:{X:0,Z:2}})
]));
""")
    assert "模拟" in result[0]
    assert "未确认" in result[1]
    assert "0.00" in result[2]


def test_channel_readout_uses_latest_sample_and_preserves_zero():
    result = evaluate("""
console.log(JSON.stringify([
 ui.channelReadout({current_nA:[2,0],time_s:[1,2],latest_current_nA:0}),
 ui.channelReadout({current_nA:[2,3],time_s:[1,2]}),
 ui.channelReadout({current_nA:[],time_s:[]}),
 ui.channelReadout({current_nA:4,time_s:8})
]));
""")
    assert result == [
        {"current": 0, "time": 2}, {"current": 3, "time": 2},
        {"current": None, "time": None}, {"current": 4, "time": 8},
    ]


def test_motion_reconnect_is_available_only_for_disconnected_recovery():
    result = evaluate("""
console.log(JSON.stringify([
 ui.canConnectMotion({busy:false}),
 ui.canConnectMotion({busy:true}),
 ui.canConnectMotion({busy:true,recovery_required:true,motion:{connected:false}}),
 ui.canConnectMotion({busy:true,recovery_required:true,motion:{connected:true}})
]));
""")
    assert result == [True, False, True, False]


def test_scene_preview_obeys_compiled_axis_targets_not_fixed_column_pitch():
    script = f"""
const scene = require({json.dumps(str(GUI / 'high_throughput_3d.js'))});
const defaults = {{x_first_mm:4,column_pitch_mm:10,z_safe_mm:2}};
console.log(JSON.stringify([
 scene.previewTarget({{op:'move_x',column:8,resolved_params:{{target_mm:74}}}}, defaults, {{x:0,z:0}}),
 scene.previewTarget({{op:'move_z',resolved_params:{{target_mm:8}}}}, defaults, {{x:-74,z:0}}),
 scene.previewTarget({{op:'wait',resolved_params:{{seconds:2}}}}, defaults, {{x:-74,z:-6}})
]));
"""
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True)
    assert json.loads(result.stdout) == [{"x": -74, "z": 0}, {"x": -74, "z": -6}, {"x": -74, "z": -6}]


def test_scene_builds_pickable_96_well_plate_and_preserves_both_cad_boards():
    if not (GUI / "high_throughput_3d.js").exists():
        raise AssertionError("Interactive apparatus scene is not implemented")
    completed = subprocess.run(
        ["node", "-e", f"""
const scene = require({json.dumps(str(GUI / 'high_throughput_3d.js'))});
const three = require({json.dumps(str(GUI / 'three.min.js'))});
const geometry = require({json.dumps(str(GUI / 'ht-geometry.json'))});
const cad = require({json.dumps(str(GUI / 'ht-apparatus.json'))});
const assembly = scene.buildApparatus(three,geometry,cad);
const bounds=new three.Box3().setFromObject(assembly.group);
console.log(JSON.stringify({{
 columns:assembly.columns.map(column=>column.userData.column),
 wells:assembly.wells.length,boards:assembly.boards.map(board=>board.userData.cadMeshCount),
 finite:[bounds.min.x,bounds.min.y,bounds.min.z,bounds.max.x,bounds.max.y,bounds.max.z].every(Number.isFinite),
}}));
"""], capture_output=True, text=True, check=True,
    )
    result = json.loads(completed.stdout)
    assert result["columns"] == list(range(1, 13))
    assert result["wells"] == 96
    assert result["boards"] == [104, 104]
    assert result["finite"]


def evaluate_apparatus(script):
    completed = subprocess.run(
        ["node", "-e", f"""
const scene = require({json.dumps(str(GUI / 'high_throughput_3d.js'))});
const three = require({json.dumps(str(GUI / 'three.min.js'))});
const boards = require({json.dumps(str(GUI / 'ht-geometry.json'))});
const cad = require({json.dumps(str(GUI / 'ht-apparatus.json'))});
const assembly = scene.buildApparatus(three,boards,cad);
{script}
"""], capture_output=True, text=True, check=True,
    )
    return json.loads(completed.stdout)


GUIDED_DOCUMENT = """
const experiment = {schema_version:'guided-1.0',name:'Template 1',output_dir:'',
 defaults:{dwell_s:180,settle_s:2},bindings:{},safety:{},
 columns:Array.from({length:12},(_,offset)=>({index:offset+1,name:String.fromCharCode(65+offset),enabled:true,duration_s:null,windows:{}})),
 electrodes:'ABCDEFGH'.split('').map(id=>({id,name:id,enabled:true}))};
"""


def test_guided_selection_keeps_physical_ids_and_free_names_without_concentration():
    result = evaluate(GUIDED_DOCUMENT + """
const model=ui.createGuidedModel(experiment);
for(const column of model.document.columns)model.setColumnEnabled(column.index,[2,7,11].includes(column.index));
model.updateColumn(7,{name:'10微摩尔每升'});
model.setElectrodeEnabled('B',false);
model.setElectrodeEnabled('G',false);
const before=model.serialize();model.selectColumn(11);
console.log(JSON.stringify({columns:model.selectedColumns().map(column=>[column.index,column.name]),
 channels:model.selectedElectrodes().map(item=>item.id),unchanged:before===model.serialize(),
 advanced:model.advancedOpen,concentration:model.document.columns.some(column=>'concentration' in column)}));
""")
    assert result == {"columns": [[2, "B"], [7, "10微摩尔每升"], [11, "K"]],
                      "channels": ["A", "C", "D", "E", "F", "H"], "unchanged": True,
                      "advanced": False, "concentration": False}


def test_guided_windows_inherit_duration_and_reject_overflow_without_mutating_draft():
    result = evaluate(GUIDED_DOCUMENT + """
const model=ui.createGuidedModel(experiment),errors=[];
const inherited=model.effectiveWindow(7,'A');
model.setColumnDuration(7,120);
model.setWindow(7,'A',{start_s:20,duration_s:80});
const before=model.serialize();
try{model.setWindow(7,'B',{start_s:30,duration_s:100});}catch(error){errors.push(error.code);}
try{model.setColumnDuration(7,90);}catch(error){errors.push(error.code);}
const unchanged=before===model.serialize();
model.setWindow(7,'A',null);model.setColumnDuration(7,null);
console.log(JSON.stringify({inherited,errors,unchanged,restored:model.effectiveWindow(7,'A')}));
""")
    assert result == {"inherited": {"start_s": 0, "duration_s": 180},
                      "errors": ["WINDOW_INVALID", "WINDOW_INVALID"], "unchanged": True,
                      "restored": {"start_s": 0, "duration_s": 180}}


def test_guided_run_previews_current_draft_and_blocks_edits_during_preview():
    result = evaluate(GUIDED_DOCUMENT + """
(async()=>{
 const model=ui.createGuidedModel(experiment),requests=[];
 model.updateColumn(2,{name:'自由标签'});
 const payload=await ui.prepareGuidedRun(model,async(path,body)=>{
  requests.push({path,body});return {plan_hash:'current',steps:[]};
 },'simulate',false,'run-1');
 let stale;
 try{await ui.prepareGuidedRun(model,async()=>{model.setColumnEnabled(12,false);return {plan_hash:'old'};},'simulate',false,'run-2');}
 catch(error){stale=error.code;}
 console.log(JSON.stringify({requests,payload,stale,plan:model.plan}));
})();
""")
    assert result["requests"][0]["path"] == "guided/preview"
    assert result["requests"][0]["body"]["mode"] == "simulate"
    assert result["requests"][0]["body"]["document"]["columns"][1]["name"] == "自由标签"
    assert result["payload"]["document"]["schema_version"] == "guided-1.0"
    assert result["payload"]["plan_hash"] == "current"
    assert "experiment" not in result["payload"]
    assert result["stale"] == "STALE_PLAN"
    assert result["plan"] is None


def test_guided_live_run_requires_explicit_confirmation_and_same_mode_plan():
    result = evaluate(GUIDED_DOCUMENT + """
const model=ui.createGuidedModel(experiment),errors=[];
model.document.safety={motion_policy_confirmed:true,electrical_policy_confirmed:true};
model.acceptPlan({plan_hash:'simulation'},model.serialize(),'simulate');
try{model.runPayload('live',true,'key');}catch(error){errors.push(error.code);}
model.acceptPlan({plan_hash:'live'},model.serialize(),'live');
try{model.runPayload('live',false,'key');}catch(error){errors.push(error.code);}
model.setElectrodeEnabled('H',false);
console.log(JSON.stringify({errors,plan:model.plan}));
""")
    assert result == {"errors": ["PREVIEW_REQUIRED", "LIVE_CONFIRMATION_REQUIRED"], "plan": None}


def test_shared_sampling_apply_preserves_board_identity_and_analysis_settings():
    result = evaluate(GUIDED_DOCUMENT + """
experiment.bindings.boards={
 A:{address:'UUID-board-a',settings:{potential_v:-0.2,analysis_channel:1,fit_window_s:5,initial_potential_v:-0.2}},
 B:{address:'UUID-board-b',settings:{potential_v:0.2,analysis_channel:4,fit_window_s:8,initial_potential_v:0.2}}};
const model=ui.createGuidedModel(experiment);
const before=ui.sharedSamplingParameters(model.document);
model.acceptPlan({plan_hash:'old'},model.serialize(),'simulate');
model.applySharedSampling({potential_v:0.2,working_electrode_v:1.2,sens_period_code:10,fsr_nA:1000,offset_mode:'20pct'});
console.log(JSON.stringify({before,after:ui.sharedSamplingParameters(model.document),boards:model.document.bindings.boards,
 plan:model.plan,original:experiment.bindings.boards.A.settings.potential_v}));
""")
    assert result["before"]["shared"] is False
    assert result["before"]["differences"] == ["potential_v"]
    assert result["after"]["shared"] is True
    assert result["after"]["settings"] == {"potential_v": 0.2, "working_electrode_v": 1.2,
                                          "sens_period_code": 10, "fsr_nA": 1000, "offset_mode": "20pct"}
    assert result["boards"]["A"]["address"] == "UUID-board-a"
    assert result["boards"]["B"]["address"] == "UUID-board-b"
    assert result["boards"]["A"]["settings"]["analysis_channel"] == 1
    assert result["boards"]["A"]["settings"]["initial_potential_v"] == -0.2
    assert result["boards"]["B"]["settings"]["analysis_channel"] == 4
    assert result["boards"]["B"]["settings"]["fit_window_s"] == 8
    assert result["boards"]["B"]["settings"]["initial_potential_v"] == 0.2
    assert result["plan"] is None
    assert result["original"] == -0.2


def test_shared_sampling_rejects_invalid_dac_range_period_fsr_and_offset_atomically():
    result = evaluate(GUIDED_DOCUMENT + """
experiment.bindings.boards={A:{address:'a',settings:{}},B:{address:'b',settings:{}}};
const model=ui.createGuidedModel(experiment),before=model.serialize(),errors=[];
const base={potential_v:-0.2,working_electrode_v:0.4,sens_period_code:0,fsr_nA:50,offset_mode:'20pct'};
for(const override of [{potential_v:0.5},{potential_v:0.4,working_electrode_v:0.25},{sens_period_code:11},{fsr_nA:4000},{offset_mode:'80nA'}]){
 try{model.applySharedSampling({...base,...override});}catch(error){errors.push(error.code);}
}
console.log(JSON.stringify({errors,unchanged:before===model.serialize(),defaults:ui.sharedSamplingParameters(model.document).settings}));
""")
    assert result["errors"] == ["SAMPLING_INVALID"] * 5
    assert result["unchanged"] is True
    assert result["defaults"] == {"potential_v": -0.2, "working_electrode_v": 0.4,
                                  "sens_period_code": 0, "fsr_nA": 50, "offset_mode": "20pct"}


BINDING_AFTER_SHARED_SCENARIO = """
(async()=>{
 experiment.bindings.boards={A:{address:'uuid-a',settings:{analysis_channel:1}},B:{address:'uuid-b',settings:{analysis_channel:4}}};
 experiment.bindings.motion={port:'offline',baud:115200,axes:{X:{address:1,lead_mm:2,travel_mm:150,positive_direction:0,max_speed_mm_s:10},Z:{address:2,lead_mm:2,travel_mm:50,positive_direction:0,max_speed_mm_s:10}}};
 HighThroughput.activate();await tick();
 input('htSharedPotential',0.2);input('htSharedWorkingElectrode',1.2);input('htSharedPeriod',3);input('htSharedFsr',1000);input('htSharedOffset','20pct');
 await click('htApplySharedSampling');
 const advanced=['A','B'].map(slot=>JSON.parse(elements.get('htBoardSettings-'+slot).value));
 input('htBoardAddress-A','uuid-a-new');elements.get('htMotionPolicy').checked=true;elements.get('htMotionPolicy').dispatchEvent(new Event('input'));
 await click('htApplyBindings');await click('htSave');
 console.log(JSON.stringify({advanced,saved:requests.findLast(request=>request.route==='guided/experiment')?.body.document,
 error:elements.get('htError').hidden?null:elements.get('htError').textContent}));
})();
"""


def test_shared_sampling_survives_later_address_only_binding_edit_in_browser_handlers():
    result = evaluate_browser(BINDING_AFTER_SHARED_SCENARIO)
    assert result["error"] is None
    for index, slot in enumerate("AB"):
        assert result["advanced"][index]["potential_v"] == 0.2
        assert result["advanced"][index]["fsr_nA"] == 1000
        board = result["saved"]["bindings"]["boards"][slot]
        assert board["address"] == ("uuid-a-new" if slot == "A" else "uuid-b")
        assert board["settings"]["potential_v"] == 0.2
        assert board["settings"]["fsr_nA"] == 1000
        assert board["settings"]["sens_period_code"] == 3
        assert board["settings"]["analysis_channel"] == (1 if slot == "A" else 4)


def test_clearing_window_duration_with_zero_delay_restores_live_duration_inheritance():
    result = evaluate_browser("""
(async()=>{
 experiment.columns[0].windows={A:{start_s:0,duration_s:5}};
 HighThroughput.activate();await tick();
 const delay=elements.get('htDelay-A').value;
 input('htWindow-A','');await click('htSave');
 const cleared=requests.findLast(request=>request.route==='guided/experiment').body.document;
 input('htDefaultDuration',240);await click('htSave');
 const inherited=requests.findLast(request=>request.route==='guided/experiment').body.document;
 input('htColumnDuration',12);await click('htSave');
 const overridden=requests.findLast(request=>request.route==='guided/experiment').body.document;
 console.log(JSON.stringify({delay,cleared:cleared.columns[0].windows,
 inherited:ui.createGuidedModel(inherited).effectiveWindow(1,'A'),
 overridden:ui.createGuidedModel(overridden).effectiveWindow(1,'A'),
 error:elements.get('htError').hidden?null:elements.get('htError').textContent}));
})();
""")
    assert result["delay"] == "0"
    assert result["cleared"] == {}
    assert result["inherited"] == {"start_s": 0, "duration_s": 240}
    assert result["overridden"] == {"start_s": 0, "duration_s": 12}
    assert result["error"] is None


def test_board_mapping_uses_bound_identity_and_fixed_channel_ids():
    result = evaluate(GUIDED_DOCUMENT + """
experiment.bindings.boards={A:{address:'uuid-a',settings:{analysis_channel:4}},B:{address:'uuid-b',settings:{analysis_channel:2}}};
experiment.electrodes[1].enabled=false;
console.log(JSON.stringify(ui.boardElectrodeMapping(experiment)));
""")
    assert result == [{"slot": "A", "address": "uuid-a", "channels": [{"electrode": "A", "channel": 1}, {"electrode": "B", "channel": 2}, {"electrode": "C", "channel": 3}, {"electrode": "D", "channel": 4}]},
                      {"slot": "B", "address": "uuid-b", "channels": [{"electrode": "E", "channel": 1}, {"electrode": "F", "channel": 2}, {"electrode": "G", "channel": 3}, {"electrode": "H", "channel": 4}]}]


def test_duration_caption_distinguishes_column_override_from_template_inheritance():
    result = evaluate(GUIDED_DOCUMENT + """
const model=ui.createGuidedModel(experiment);
model.setColumnDuration(7,12);
model.edit(document=>document.defaults.dwell_s=240);
console.log(JSON.stringify([ui.columnDurationCaption(model.document,2),ui.columnDurationCaption(model.document,7)]));
""")
    assert result[0]["effective_s"] == 240
    assert result[0]["inherited"] is True
    assert result[1]["effective_s"] == 12
    assert result[1]["inherited"] is False
    assert "12" in result[1]["summary"]
    assert "240" in result[1]["summary"]


def test_calibration_caption_obeys_authoritative_usable_false_despite_old_valid_flags():
    result = evaluate("""
console.log(JSON.stringify([
 ui.calibrationReferenceCaption({usable:false,valid:true,reference_valid:true,status:'stale'}),
 ui.calibrationReferenceCaption({usable:true,status:'ready'})
]));
""")
    assert "失效" in result[0]
    assert "可用" not in result[0]
    assert "可用" in result[1]


def test_guided_clock_waits_for_start_ack_and_reports_pause_after_safe_lift():
    result = evaluate("""
console.log(JSON.stringify([
 ui.guidedTimingCaption({busy:true,state:'running',column_timing:{column:7,duration_s:180,started_unix_s:null,remaining_s:180}}),
 ui.guidedTimingCaption({busy:true,state:'running',column_timing:{column:7,duration_s:180,started_unix_s:100,elapsed_s:100,remaining_s:80}}),
 ui.guidedTimingCaption({busy:true,state:'paused',column_timing:{column:7,duration_s:180,started_unix_s:100,elapsed_s:180,remaining_s:0}}),
 ui.guidedTimingCaption({busy:true,state:'running',column_timing:{column:7,duration_s:180,started_unix_s:100}})
]));
""")
    assert "等待停稳" in result[0]["clock"]
    assert "180.0" not in result[0]["clock"]
    assert result[1]["clock"] == "第 7 列 · 剩余 80.0 s"
    assert "暂停" in result[2]["clock"]
    assert "安全高度" in result[2]["detail"]
    assert "未确认" in result[3]["clock"]


def test_guided_clock_does_not_reuse_previous_column_timing_during_transfer():
    result = evaluate("""
console.log(JSON.stringify(ui.guidedTimingCaption({busy:true,state:'running',column:7,message:'move_x',
 column_timing:{column:2,duration_s:12,started_unix_s:100,elapsed_s:12,remaining_s:0}})));
""")
    assert "等待停稳" in result["clock"]
    assert "第 2 列" not in result["clock"]


def test_electrode_visibility_hides_only_selected_electrode_and_wire_without_reflow():
    result = evaluate_apparatus("""
const before=assembly.parts.map(part=>({name:part.name,position:part.position.toArray(),vertices:Array.from(part.geometry.attributes.position.array)}));
if(!assembly.setElectrodes){console.log(JSON.stringify({supported:false}));process.exit(0);}
assembly.setElectrodes('ABCDEFGH'.split('').map(id=>({id,enabled:!['B','F'].includes(id)})));
const electrodes=assembly.parts.filter(part=>part.name.endsWith('_Electrode_REFERENCE')).map(part=>[part.name[0],part.visible]);
const wires=assembly.wires.map(part=>[part.userData.electrode,part.visible]);
const hiddenCarriers=assembly.parts.filter(part=>!part.name.endsWith('_Electrode_REFERENCE')&&!part.visible).map(part=>part.name);
const changed=assembly.parts.some((part,index)=>JSON.stringify({name:part.name,position:part.position.toArray(),vertices:Array.from(part.geometry.attributes.position.array)})!==JSON.stringify(before[index]));
assembly.setElectrodes([{id:'F',enabled:true}]);
console.log(JSON.stringify({supported:true,electrodes,wires,hiddenCarriers,changed,reenabled:assembly.group.getObjectByName('F_Electrode_REFERENCE').visible}));
""")
    assert result["supported"]
    assert result["electrodes"] == [[channel, channel not in "BF"] for channel in "ABCDEFGH"]
    assert result["wires"] == [[channel, channel not in "BF"] for channel in "ABCDEFGH"]
    assert result["hiddenCarriers"] == []
    assert result["changed"] is False
    assert result["reenabled"] is True


def test_native_parts_use_peer_motion_groups_without_reapplying_export_transforms():
    result = evaluate_apparatus("""
const groups = {fixed:assembly.fixedGroup,x:assembly.xGroup,z:assembly.zGroup};
const failures=[];
for (const part of cad.parts) {
 const rendered=assembly.group.getObjectByName(part.name);
 if(!rendered){failures.push(part.name+':missing');continue;}
 if(rendered.parent!==groups[part.motion])failures.push(part.name+':parent');
 if(rendered.position.length()!==0)failures.push(part.name+':translated_twice');
 if(rendered.geometry.attributes.position.count!==part.vertices.length)failures.push(part.name+':replacement_geometry');
 if(rendered.userData.source!==part.source)failures.push(part.name+':source_lost');
}
console.log(JSON.stringify({failures,peers:Object.values(groups).every(group=>group?.parent===assembly.group)}));
""")
    assert result == {"failures": [], "peers": True}


def test_x_motion_moves_tray_and_wells_without_moving_z_frame_arm_or_boards():
    result = evaluate_apparatus("""
if(!assembly.setPreviewPose){console.log(JSON.stringify({supported:false}));process.exit(0);}
const center = object=>{assembly.group.updateMatrixWorld(true);return new three.Box3().setFromObject(object).getCenter(new three.Vector3()).toArray();};
const objects=['Base_PRINT','Z_Backframe_PRINT','Z50_Body_ENVELOPE','Tray_PRINT','96well_REFERENCE','X_Carriage_REFERENCE','Orange_Arm_Dual_Cradle_PRINT'].map(name=>assembly.group.getObjectByName(name));
objects.push(assembly.boards[0],assembly.boards[1],assembly.wells[0],assembly.columns[0]);
const before=objects.map(center);
assembly.setPreviewPose(scene.previewTarget({op:'move_x',resolved_params:{target_mm:63}},{z_safe_mm:0},{x:0,z:0}));
console.log(JSON.stringify({supported:true,deltas:objects.map((object,index)=>center(object).map((value,axis)=>value-before[index][axis]))}));
""")
    assert result["supported"]
    assert result["deltas"] == [[0, 0, 0]] * 3 + [[-63, 0, 0]] * 3 + [[0, 0, 0]] * 3 + [[-63, 0, 0]] * 2


def test_z_motion_moves_only_arm_electrodes_and_both_boards():
    result = evaluate_apparatus("""
if(!assembly.setPreviewPose){console.log(JSON.stringify({supported:false}));process.exit(0);}
const center = object=>{assembly.group.updateMatrixWorld(true);return new three.Box3().setFromObject(object).getCenter(new three.Vector3()).toArray();};
const names=['Base_PRINT','Z_Backframe_PRINT','Z50_Body_ENVELOPE','Tray_PRINT','96well_REFERENCE','Z_Carriage_REFERENCE','Orange_Arm_Dual_Cradle_PRINT','A_Electrode_REFERENCE'];
const objects=names.map(name=>assembly.group.getObjectByName(name));
objects.push(assembly.boards[0],assembly.boards[1],assembly.wells[0]);
const before=objects.map(center);
assembly.setPreviewPose(scene.previewTarget({op:'move_z',resolved_params:{target_mm:8}},{z_safe_mm:2},{x:0,z:0}));
console.log(JSON.stringify({supported:true,deltas:objects.map((object,index)=>center(object).map((value,axis)=>value-before[index][axis]))}));
""")
    assert result["supported"]
    assert result["deltas"] == [[0, 0, 0]] * 5 + [[0, 0, -6]] * 5 + [[0, 0, 0]]


def test_each_native_well_column_aligns_with_all_eight_stationary_electrode_centers():
    result = evaluate_apparatus("""
if(!assembly.setPreviewPose){console.log(JSON.stringify({supported:false}));process.exit(0);}
const errors=[];
for(let column=1;column<=12;column++){
 assembly.setPreviewPose({x:-(column-1)*9,z:0});
 assembly.group.updateMatrixWorld(true);
 for(const well of assembly.wells.filter(well=>well.userData.column===column)){
  const electrode=assembly.group.getObjectByName(well.userData.row+'_Electrode_REFERENCE');
  const center=new three.Box3().setFromObject(electrode).getCenter(new three.Vector3());
  const mouth=well.getWorldPosition(new three.Vector3());
  errors.push(Math.hypot(center.x-mouth.x,center.y-mouth.y));
 }
}
console.log(JSON.stringify({supported:true,count:errors.length,max_error_mm:Math.max(...errors)}));
""")
    assert result["supported"]
    assert result["count"] == 96
    assert result["max_error_mm"] < 0.00001
