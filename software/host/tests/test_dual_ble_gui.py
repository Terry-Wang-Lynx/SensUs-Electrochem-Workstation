import json
import subprocess

from test_automation_gui import evaluate_browser
from test_gui_ble import GUI_DIR, _evaluate_gui_js, _extract_js_function


BOARD_BOOTSTRAP = """
experiment.bindings.boards={A:{address:'front-uuid',settings:{fsr_nA:50}},B:{address:'rear-uuid',settings:{fsr_nA:100}}};
const boardTimers=[];globalThis.setInterval=callback=>{boardTimers.push(callback);return boardTimers.length;};
root.classList.toggle('active',true);
let boardSnapshot={devices:[{id:'ble:rear-uuid',kind:'ble',address:'rear-uuid',name:'pA-V5.2',connection_state:'disconnected'},
 {id:'ble:front-uuid',kind:'ble',address:'front-uuid',name:'pA-V5.2',connection_state:'connected',protocol_verified:true}],
 bindings:JSON.parse(JSON.stringify(experiment.bindings.boards)),connections:{'front-uuid':{state:'connected',protocol_verified:true},'rear-uuid':{state:'disconnected'}},busy:false};
let boardFailure=null,partialFailure=false,motorBusy=false;
const boardFetch=globalThis.fetch;
globalThis.fetch=async(path,options)=>{
 const route=path.replace('/api/automation/v1/',''),body=options.body?JSON.parse(options.body):null;
 if(route==='state'&&motorBusy)return {ok:true,json:async()=>({state:'idle',busy:true,channels:[],events:[]})};
 if(route==='guided'&&motorBusy)return {ok:true,json:async()=>({document:experiment,manual:{active:true,control_mode:'keyboard'}})};
 if(!route.startsWith('boards'))return boardFetch(path,options);
 requests.push({route,body});
 if(boardFailure)return {ok:false,status:409,json:async()=>({error:{code:'board_busy',message:boardFailure}})};
 if(route==='boards/bind')for(const slot of ['A','B'])boardSnapshot.bindings[slot].address=body.addresses[slot];
 if(route==='boards/connect')for(const address of body.addresses)boardSnapshot.connections[address]={state:partialFailure&&address==='rear-uuid'?'error':'connected',error:partialFailure&&address==='rear-uuid'?'rear verification failed':null};
 if(route==='boards/disconnect')for(const address of body.addresses)boardSnapshot.connections[address]={state:'disconnected'};
 return {ok:true,json:async()=>JSON.parse(JSON.stringify({...boardSnapshot,...(partialFailure&&route==='boards/connect'?{errors:{'rear-uuid':'rear verification failed'}}:{})}))};
};
const changeBoard=(slot,address)=>{elements.get('htBoardSelect-'+slot).value=address;elements.get('htBoardSelect-'+slot).dispatchEvent(new Event('change'));};
const openBoards=async()=>{HighThroughput.activate();await tick();await click('htMappingToggle');await tick();};
const controls=()=>['htBoardSelect-A','htBoardSelect-B','htBoardScan','htBoardBind','htBoardConnect','htBoardDisconnect-A','htBoardDisconnect-B'].every(id=>elements.has(id));
"""


def evaluate_boards(script):
    return evaluate_browser(script, before_source=BOARD_BOOTSTRAP)


def test_normal_panel_has_explicit_slot_selection_without_automatic_binding():
    result = evaluate_boards("""
(async()=>{await openBoards();if(!controls()){console.log(JSON.stringify({supported:false}));return;}
 console.log(JSON.stringify({supported:true,selected:['A','B'].map(slot=>elements.get('htBoardSelect-'+slot).value),
 options:elements.get('htBoardSelect-A').options.map(option=>option.textContent),
 headings:elements.get('htBoardMapping').children.map(card=>card.children[0].textContent),
 statuses:['A','B'].map(slot=>elements.get('htBoardConnection-'+slot).textContent),
 writes:requests.filter(request=>request.body!==null)}));})();
""")
    assert result["supported"]
    assert result["selected"] == ["front-uuid", "rear-uuid"]
    assert "远离 Z 轴电机" in result["headings"][0]
    assert any("pA-V5.2" in option and "front-uuid" in option for option in result["options"])
    assert any("pA-V5.2" in option and "rear-uuid" in option for option in result["options"])
    assert "已连接" in result["statuses"][0]
    assert "未连接" in result["statuses"][1]
    assert result["writes"] == []


def test_scan_and_poll_preserve_pending_swap_and_save_both_slots_atomically():
    result = evaluate_boards("""
(async()=>{await openBoards();if(!controls()){console.log(JSON.stringify({supported:false}));return;}
 changeBoard('A','rear-uuid');changeBoard('B','front-uuid');await click('htBoardScan');
 for(const callback of boardTimers)callback();await tick();
 const draft=['A','B'].map(slot=>elements.get('htBoardSelect-'+slot).value);
 await click('htBoardBind');
 console.log(JSON.stringify({supported:true,draft,binds:requests.filter(request=>request.route==='boards/bind'),
 document:JSON.parse(elements.get('htJson').value).bindings.boards}));})();
""")
    assert result["supported"]
    assert result["draft"] == ["rear-uuid", "front-uuid"]
    assert result["binds"] == [{"route": "boards/bind", "body": {"addresses": {"A": "rear-uuid", "B": "front-uuid"}}}]
    assert result["document"] == {"A": {"address": "rear-uuid", "settings": {"fsr_nA": 50}}, "B": {"address": "front-uuid", "settings": {"fsr_nA": 100}}}


def test_uuid_case_differences_do_not_duplicate_options_or_clear_selection():
    result = evaluate_boards("""
(async()=>{boardSnapshot.devices=[{id:'ble:FRONT-UUID',kind:'ble',address:'FRONT-UUID',name:'pA-V5.2'},...boardSnapshot.devices];
 await openBoards();const selector=elements.get('htBoardSelect-A');
 console.log(JSON.stringify({options:selector.options.map(option=>({value:option.value,text:option.textContent})),selected:selector.value,
 state:elements.get('htBoardConnection-A').textContent}));})();
""")
    assert len(result["options"]) == 3
    assert result["selected"] == "FRONT-UUID"
    assert result["options"][1]["text"] == "pA-V5.2 · FRONT-UUID"
    assert "已连接" in result["state"]


def test_saving_only_board_binding_keeps_clean_template_saved():
    result = evaluate_boards("""
(async()=>{await openBoards();changeBoard('A','rear-uuid');changeBoard('B','front-uuid');await click('htBoardBind');
 console.log(JSON.stringify({editor:elements.get('htEditorState').textContent,notice:elements.get('htBoardNotice').textContent}));})();
""")
    assert "已保存" in result["editor"]
    assert "尚未保存" not in result["editor"]
    assert "其它模板编辑" not in result["notice"]


def test_saving_board_binding_retains_other_unsaved_template_changes():
    result = evaluate_boards("""
(async()=>{await openBoards();input('htName','Still unsaved');changeBoard('A','rear-uuid');changeBoard('B','front-uuid');await click('htBoardBind');
 const unsaved={editor:elements.get('htEditorState').textContent,name:JSON.parse(elements.get('htJson').value).name};
 await click('htSave');changeBoard('A','front-uuid');changeBoard('B','rear-uuid');await click('htBoardBind');
 console.log(JSON.stringify({unsaved,saved:elements.get('htEditorState').textContent}));})();
""")
    assert result["unsaved"]["name"] == "Still unsaved"
    assert "尚未保存" in result["unsaved"]["editor"]
    assert "尚未保存" not in result["saved"]


def test_duplicate_binding_is_rejected_and_unused_slot_can_be_empty():
    result = evaluate_boards("""
(async()=>{await openBoards();if(!controls()){console.log(JSON.stringify({supported:false}));return;}
 changeBoard('B','FRONT-UUID');await click('htBoardBind');
 const duplicate={writes:requests.filter(request=>request.route==='boards/bind').length,error:elements.get('htBoardNotice').textContent};
 changeBoard('B','');await click('htBoardBind');await click('htBoardConnect');
 console.log(JSON.stringify({supported:true,duplicate,writes:requests.filter(request=>request.body!==null)}));})();
""")
    assert result["supported"]
    assert result["duplicate"]["writes"] == 0
    assert "同一" in result["duplicate"]["error"]
    assert result["writes"] == [
        {"route": "boards/bind", "body": {"addresses": {"A": "front-uuid", "B": ""}}},
        {"route": "boards/connect", "body": {"addresses": ["front-uuid"]}},
    ]


def test_partial_connect_failure_keeps_success_visible_and_disconnects_only_one():
    result = evaluate_boards("""
(async()=>{await openBoards();if(!controls()){console.log(JSON.stringify({supported:false}));return;}
 partialFailure=true;await click('htBoardConnect');
 const connected={front:elements.get('htBoardConnection-A').textContent,rear:elements.get('htBoardConnection-B').textContent,error:elements.get('htBoardNotice').textContent};
 await click('htBoardDisconnect-A');
 console.log(JSON.stringify({supported:true,connected,disconnects:requests.filter(request=>request.route==='boards/disconnect')}));})();
""")
    assert result["supported"]
    assert "已连接" in result["connected"]["front"]
    assert "rear verification failed" in result["connected"]["rear"]
    assert "rear verification failed" in result["connected"]["error"]
    assert result["disconnects"] == [{"route": "boards/disconnect", "body": {"addresses": ["front-uuid"]}}]


def test_motor_calibration_allows_board_connections_but_board_busy_blocks_them():
    result = evaluate_boards("""
(async()=>{motorBusy=true;await openBoards();if(!controls()){console.log(JSON.stringify({supported:false}));return;}
 const motorOnly={select:elements.get('htBoardSelect-A').disabled,connect:elements.get('htBoardConnect').disabled};
 await click('htBoardConnect');boardSnapshot.busy=true;
 for(const callback of boardTimers)callback();await tick();
 console.log(JSON.stringify({supported:true,motorOnly,blocked:elements.get('htBoardConnect').disabled,
 connects:requests.filter(request=>request.route==='boards/connect').length}));})();
""")
    assert result["supported"]
    assert result["motorOnly"] == {"select": False, "connect": False}
    assert result["blocked"] is True
    assert result["connects"] == 1


def test_hidden_panel_and_inactive_page_do_not_poll_board_connections():
    result = evaluate_boards("""
(async()=>{await openBoards();if(!controls()){console.log(JSON.stringify({supported:false}));return;}
 await click('htMappingToggle');let before=requests.filter(request=>request.route==='boards').length;
 for(const callback of boardTimers)callback();await tick();const hidden=requests.filter(request=>request.route==='boards').length-before;
 await click('htMappingToggle');root.classList.toggle('active',false);before=requests.filter(request=>request.route==='boards').length;
 for(const callback of boardTimers)callback();await tick();const inactive=requests.filter(request=>request.route==='boards').length-before;
 console.log(JSON.stringify({supported:true,hidden,inactive}));})();
""")
    assert result == {"supported": True, "hidden": 0, "inactive": 0}


def test_collector_state_and_unknown_output_override_standby_connection_state():
    result = evaluate_boards("""
(async()=>{boardSnapshot.runtime_boards={A:{address:'front-uuid',state:'acquiring'},B:{address:'rear-uuid',state:'reconnecting'}};
 boardSnapshot.connections['front-uuid'].state='disconnected';await openBoards();
 const acquiring=['A','B'].map(slot=>elements.get('htBoardConnection-'+slot).textContent);
 boardSnapshot.runtime_boards.A.state='error';boardSnapshot.connections['front-uuid'].state='connected';
 for(const callback of boardTimers)callback();await tick();
 console.log(JSON.stringify({acquiring,unknown:elements.get('htBoardConnection-A').textContent}));})();
""")
    assert "采集中" in result["acquiring"][0]
    assert "重连" in result["acquiring"][1]
    assert "输出状态未知" in result["unknown"]
    assert "已连接" not in result["unknown"]


def test_high_throughput_sidebar_reports_connected_and_connection_failure():
    result = _evaluate_gui_js("app.js", ["highThroughputDeviceStatus"], """
const connected=highThroughputDeviceStatus({boards:{A:{state:'connected',address:'front-uuid'},B:{state:'connected',address:'rear-uuid'}}});
const failure=highThroughputDeviceStatus({boards:{A:{state:'connected'},B:{state:'connection_error',connection_error:'rear timed out'}}});
""", "{connected,failure}")
    assert "已连接" in result["connected"]["BoardA"]["text"]
    assert "2/2" in result["connected"]["CompactSummary"]["text"]
    assert "rear timed out" in result["failure"]["BoardB"]["text"]
    assert result["failure"]["BoardB"]["tone"] == "error"


def test_board_endpoint_error_remains_visible_without_replacing_binding_draft():
    result = evaluate_boards("""
(async()=>{await openBoards();changeBoard('A','rear-uuid');changeBoard('B','front-uuid');boardFailure='采集仍在进行';await click('htBoardBind');
 console.log(JSON.stringify({error:elements.get('htBoardNotice').textContent,addresses:['A','B'].map(slot=>elements.get('htBoardSelect-'+slot).value),
 model:JSON.parse(elements.get('htJson').value).bindings.boards}));})();
""")
    assert result["error"] == "采集仍在进行"
    assert result["addresses"] == ["rear-uuid", "front-uuid"]
    assert result["model"]["A"]["address"] == "front-uuid"


def test_main_picker_connection_action_uses_address_without_selecting_measurement_target():
    source = (GUI_DIR / "app.js").read_text()
    script = _extract_js_function(source, "setBleDeviceConnection") + """
const requests=[],rendered=[],errors={};const state={devices:{},bleAction:null};
const api=async(path,options)=>{requests.push({path,body:JSON.parse(options.body)});return {devices:[],selected_device_id:'ble:front-uuid'};};
const $=()=>errors;const diagnosticMessage=error=>error.message;const renderDeviceList=value=>rendered.push(value);const renderBleControls=()=>{};const refreshDevices=async()=>{};
(async()=>{await setBleDeviceConnection('rear-uuid',true);await setBleDeviceConnection('rear-uuid',false);
console.log(JSON.stringify({requests,rendered,action:state.bleAction}));})();
"""
    completed = subprocess.run(["node", "-e", script], text=True, capture_output=True, check=True)
    result = json.loads(completed.stdout)
    assert result["requests"] == [
        {"path": "/api/devices/ble/connect", "body": {"address": "rear-uuid"}},
        {"path": "/api/devices/ble/disconnect", "body": {"address": "rear-uuid"}},
    ]
    assert result["action"] is None


def test_main_picker_connection_error_survives_subsequent_device_list_refresh():
    source = (GUI_DIR / "app.js").read_text()
    script = _extract_js_function(source, "setBleDeviceConnection") + """
const state={devices:{},bleAction:null};const notice={};
const api=async()=>{throw new Error('Board verification failed');};
const $=()=>notice;const diagnosticMessage=error=>error.message;
const renderDeviceList=()=>{},renderBleControls=()=>{},refreshDevices=async()=>{};
(async()=>{await setBleDeviceConnection('rear-uuid',true);console.log(JSON.stringify({error:state.bleConnectionError,notice:notice.textContent}));})();
"""
    completed = subprocess.run(["node", "-e", script], text=True, capture_output=True, check=True)
    result = json.loads(completed.stdout)
    assert result == {"error": "Board verification failed", "notice": "Board verification failed"}
