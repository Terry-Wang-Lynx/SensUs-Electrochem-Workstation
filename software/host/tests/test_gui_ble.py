import json
import re
from pathlib import Path

from node_test_utils import run_node
import pytest

GUI_DIR = Path(__file__).parents[1] / "pa_host" / "gui"


def _extract_js_function(source, name):
    start = source.index(f"function {name}(")
    if source[max(0, start - 6):start] == "async ":
        start -= 6
    params = source.index("(", start)
    depth = 0
    for offset in range(params, len(source)):
        if source[offset] == "(":
            depth += 1
        elif source[offset] == ")":
            depth -= 1
            if depth == 0:
                params = offset
                break
    opening = source.index("{", params)
    depth = 0
    for offset in range(opening, len(source)):
        if source[offset] == "{":
            depth += 1
        elif source[offset] == "}":
            depth -= 1
            if depth == 0:
                return source[start:offset + 1]
    raise AssertionError(f"Unterminated JavaScript function: {name}")


def _evaluate_gui_js(file_name, names, setup, result):
    source = (GUI_DIR / file_name).read_text(encoding="utf-8")
    functions = "\n".join(_extract_js_function(source, name) for name in names)
    script = f"{functions}\n{setup}\nconsole.log(JSON.stringify({result}));"
    completed = run_node(script, check=True, capture_output=True, text=True)
    return json.loads(completed.stdout)


@pytest.mark.parametrize("file_name", ["app.js", "compact.js"])
def test_ble_discovery_requires_protocol_verification_before_measurement(file_name):
    result = _evaluate_gui_js(
        file_name, ["hardwareOperationStatus"],
        """
const device={id:'ble:board',kind:'ble',transport:'ble',name:'pA-V5.2',
  address:'board',selectable:true,protocol_verified:false};
const state={devices:{devices:[device]}};
const discovered=hardwareOperationStatus();
state.devices.selected_device=device;
device.protocol_verified=true;
device.advertising=false;
const verified=hardwareOperationStatus();
device.present=false;
const disconnected=hardwareOperationStatus();
""", "{discovered,verified,disconnected}",
    )
    assert result["discovered"]["ready"] is False
    assert "蓝牙" in result["discovered"]["message"]
    assert result["verified"]["ready"] is True
    assert result["disconnected"]["ready"] is False


def test_ble_sidebar_reports_verification_without_claiming_usb_model():
    result = _evaluate_gui_js(
        "app.js", ["renderHardwareConnection", "deviceCardDetail"],
        """
const nodes=Object.fromEntries(['deviceDot','deviceState','deviceTransport','deviceStatus'].map(id=>[id,{}]));
const $=id=>nodes[id];
const device={id:'ble:board',kind:'ble',name:'pA-V5.2',address:'board',rssi:-61,selectable:true,protocol_verified:false};
const state={devices:{devices:[device]},measurement:{state:'idle'}};
renderHardwareConnection();
const discovered={title:nodes.deviceState.textContent,detail:deviceCardDetail(device)};
state.devices.selected_device=device;device.protocol_verified=true;
renderHardwareConnection();
const verified={title:nodes.deviceState.textContent,detail:nodes.deviceTransport.textContent};
""", "{discovered,verified}",
    )
    assert "发现" in result["discovered"]["title"]
    assert "待验证" in result["discovered"]["detail"]
    assert "-61 dBm" in result["discovered"]["detail"]
    assert "board" in result["discovered"]["detail"]
    assert "已验证" in result["verified"]["title"]
    assert "MAX30134" in result["verified"]["detail"]
    assert "MAX30131" not in result["verified"]["detail"]


@pytest.mark.parametrize("scan,devices,title,detail", [
    ({"state": "not_scanned", "count": 0}, [], "蓝牙尚未扫描", "请开启蓝牙"),
    ({"state": "scanning", "count": 0}, [], "正在扫描蓝牙", "V5.2"),
    ({"state": "complete", "count": 0}, [], "未发现蓝牙采集板", "请开启蓝牙"),
    ({"state": "error", "count": 0, "error": "permission denied"}, [], "蓝牙扫描失败", "权限"),
    ({"state": "error", "count": 1, "error": "permission denied"}, [
        {"id": "ble:cached", "kind": "ble", "address": "cached", "selectable": True},
    ], "蓝牙扫描失败", "权限"),
    ({"state": "complete", "count": 2}, [
        {"id": "ble:first", "kind": "ble", "name": "pA-V5.2", "address": "first", "selectable": True},
        {"id": "ble:second", "kind": "ble", "name": "pA-V5.2", "address": "second", "selectable": True},
    ], "发现 2 块蓝牙采集板", "上次扫描"),
])
def test_ble_sidebar_distinguishes_not_scanned_empty_error_and_two_boards(scan, devices, title, detail):
    result = _evaluate_gui_js("app.js", ["renderHardwareConnection"], """
const nodes=Object.fromEntries(['deviceDot','deviceState','deviceTransport','deviceStatus'].map(id=>[id,{}]));
const $=id=>nodes[id];
const state={devices:PAYLOAD,measurement:{state:'idle'}};
renderHardwareConnection();
""".replace("PAYLOAD", json.dumps({"transport": "ble", "probing": True, "ble_scan": scan, "devices": devices})),
        "{title:nodes.deviceState.textContent,detail:nodes.deviceTransport.textContent}")
    assert result["title"] == title
    assert detail in result["detail"]
    assert "J-Link" not in result["detail"] and "USB DATA" not in result["detail"]


def test_settings_payload_preserves_ble_channel_identity_without_changing_legacy():
    result = _evaluate_gui_js(
        "app.js", ["isBleProfile", "readSettings"],
        """
const values={potentialV:'-.2',workingElectrodeV:'.4',durationS:'30',sensPeriodCode:'0',sampleRateHz:'10',fitWindowS:'20',
  fsrNA:'50',offsetNA:'20pct',analysisChannel:'3',cvLowV:'-.6',cvHighV:'.6',cvScanRate:'.1',cvCycles:'2',cvQuietS:'2',cvEisFsrUA:'20'};
const $=id=>({value:values[id],checked:false});
const state={method:'it',settings:{settings:{}},devices:{selected_device:{kind:'usb'}}};
const legacy=readSettings();
state.devices.selected_device={kind:'ble',board_profile:'v52'};
const ble=readSettings();
""", "{legacy,ble}",
    )
    assert not {"board_profile", "protocol", "analysis_channel"} & result["legacy"].keys()
    assert result["ble"]["board_profile"] == "v52"
    assert result["ble"]["protocol"] == "v52-0.1"
    assert result["ble"]["analysis_channel"] == 3
    assert result["ble"]["potential_v"] == -0.2
    assert result["ble"]["working_electrode_v"] == 0.4
    assert result["ble"]["prestep_s"] == 0


DOM_SETUP = """
function node(){return {hidden:false,disabled:false,value:'',textContent:'',dataset:{},children:[],style:{},
  classList:{toggle(){},add(){},remove(){}},setAttribute(){},
  append(...children){this.children.push(...children)},replaceChildren(...children){this.children=children},
  querySelectorAll(){return []}}}
const nodes=new Proxy({}, {get(target,key){return target[key]||(target[key]=node())}});
const $=id=>nodes[id];
const cvButton=node();cvButton.dataset.method='cv';
const itButton=node();itButton.dataset.method='it';
nodes.methodMode.querySelectorAll=()=>[itButton,cvButton];
const legacyPanels=[node(),node()];
const eisOption=node();eisOption.value='4000';
const dcOption=node();dcOption.value='50';
nodes.fsrNA.querySelectorAll=()=>[dcOption,eisOption];
const document={createElement:()=>node(),querySelectorAll:selector=>selector.includes('dbg-')?legacyPanels:[],
  querySelector:()=>node(),visibilityState:'visible'};
const SETTINGS_INPUT_IDS=['potentialV','workingElectrodeV','durationS','sensPeriodCode','sampleRateHz','fsrNA','offsetNA','analysisChannel'];
const state={method:'it',settings:{settings:{board_profile:'v52',analysis_channel:2}},devices:{selected_device:{kind:'ble',protocol_verified:true}},measurement:{busy:false}};
"""


def test_ble_capabilities_restrict_only_board_controls_and_lock_channel_during_run():
    result = _evaluate_gui_js(
        "app.js", ["isBleProfile", "renderBleControls"],
        DOM_SETUP + """
renderBleControls();
const idle={cv:cvButton.disabled,range:eisOption.disabled,dc:dcOption.disabled,channel:nodes.analysisChannel.disabled,
  legacy:legacyPanels.every(panel=>panel.hidden),notice:nodes.bleCapabilities.hidden,updater:nodes.appUpdate.disabled};
state.measurement.busy=true;renderBleControls();
const running={channel:nodes.analysisChannel.disabled,voltage:nodes.workingElectrodeV.disabled,scan:nodes.scanBle.disabled,stop:nodes.stopBle.disabled};
state.measurement.busy=false;state.devices.selected_device={kind:'usb'};state.settings.settings={};renderBleControls();
const usb={cv:cvButton.disabled,range:eisOption.disabled,legacy:legacyPanels.some(panel=>panel.hidden),notice:nodes.bleCapabilities.hidden};
""", "{idle,running,usb}",
    )
    assert result["idle"] == {"cv": True, "range": True, "dc": False, "channel": False, "legacy": True, "notice": False, "updater": False}
    assert result["running"] == {"channel": True, "voltage": True, "scan": True, "stop": True}
    assert result["usb"] == {"cv": False, "range": False, "legacy": False, "notice": True}


def test_ble_offset_options_are_explicit_and_return_to_legacy_values():
    result = _evaluate_gui_js(
        "app.js", ["isBleProfile", "renderBoardOptions"],
        DOM_SETUP + """
renderBoardOptions();
const bleOffsets=nodes.offsetNA.children.map(option=>[option.value,option.textContent]);
const blePeriods=nodes.sensPeriodCode.children.map(option=>option.value);
state.devices.selected_device={kind:'usb'};state.settings.settings={};renderBoardOptions();
const usbOffsets=nodes.offsetNA.children.map(option=>option.value);
const usbPeriods=nodes.sensPeriodCode.children.map(option=>option.value);
""", "{bleOffsets,blePeriods,usbOffsets,usbPeriods}",
    )
    assert [entry[0] for entry in result["bleOffsets"]][:4] == ["10nA", "20nA", "40nA", "80nA"]
    assert result["bleOffsets"][0][1] == "10 nA"
    assert result["bleOffsets"][1][1] == "20 nA"
    assert result["blePeriods"] == [str(code) for code in range(11)]
    assert result["usbOffsets"][:4] == ["9nA", "19nA", "40nA", "80nA"]
    assert result["usbPeriods"] == [str(code) for code in range(6)]


def test_ble_four_channel_rendering_keeps_invalid_points_and_monitor_units_visible():
    result = _evaluate_gui_js(
        "app.js", ["isBleProfile", "fmt", "bleMonitorValue", "renderBleMeasurement", "drawBleChannels"],
        DOM_SETUP + """
const charts=[];
const drawChart=(canvas,series,options)=>charts.push({series,options});
const payload={transport:'ble',settings:{board_profile:'v52',analysis_channel:2},ble:{analysis_channel:2,
  channels:[{channel:1,time_s:[0,1],current_nA:[1,2],valid:[true,false],latest_current_nA:2},
    {channel:2,time_s:[0,1],current_nA:[-3,-4],valid:[true,true],latest_current_nA:-4},
    {channel:3,time_s:[],current_nA:[],valid:[],latest_current_nA:null},
    {channel:4,time_s:[0],current_nA:[5],valid:[true],latest_current_nA:5}],
  monitor:{'VDD:0':{quantity:'VDD',channel:0,value:3000000,unit:'uV',flags:0,host_unix_s:100,stale:true},
    'TEMP:0':{quantity:'TEMP',channel:0,value:25375,unit:'milliC',flags:0},
    'WE:2':{quantity:'WE',channel:2,value:400125,unit:'uV',flags:1}},
  state:'error',stop_confirmed:false,error:'BLE disconnected',all_channels_path:'/run/all-channels.csv',monitor_path:'/run/monitor.csv'}};
state.measurement=payload;
renderBleMeasurement(payload);drawBleChannels(payload);
""", "{charts,reading:nodes.bleCurrent2.textContent,empty:nodes.bleCurrent3.textContent,monitor:nodes.bleMonitor.children.map(row=>row.textContent),status:nodes.bleRunState.textContent,paths:nodes.bleFiles.textContent}",
    )
    assert len(result["charts"]) == 4
    assert result["charts"][0]["series"][0]["points"] == [[0, 1]]
    assert result["charts"][0]["series"][1]["points"] == [[1, 2]]
    assert result["charts"][1]["series"][0]["points"] == [[0, -3], [1, -4]]
    assert result["reading"] == "-4.000"
    assert result["empty"] == "--"
    assert any("3.000000 V" in entry and "旧读数" in entry for entry in result["monitor"])
    assert any("25.375 °C" in entry for entry in result["monitor"])
    assert any("CH2" in entry and "0.400125 V" in entry and "flags=1" in entry for entry in result["monitor"])
    assert "仍可能" in result["status"]
    assert "all-channels.csv" in result["paths"]
    assert "monitor.csv" in result["paths"]


def test_new_channel_samples_trigger_redraw_even_when_analysis_channel_does_not_change():
    result = _evaluate_gui_js(
        "app.js", ["measurementChartRevision"],
        """
const payload={run_id:'run',state:'running',data:{time_s:[0],current_nA:[2]},
  ble:{channels:[{channel:1,time_s:[0],current_nA:[2]},{channel:4,time_s:[],current_nA:[]}]}};
const before=measurementChartRevision(payload);
payload.ble.channels[1].time_s.push(.1);payload.ble.channels[1].current_nA.push(4);
const after=measurementChartRevision(payload);
""", "{before,after}",
    )
    assert result["before"] != result["after"]


def test_compact_condition_identifies_selected_ble_analysis_channel():
    result = _evaluate_gui_js(
        "compact.js", ["signed", "renderSettings"],
        """
const nodes={conditionText:{},sampleControls:{},bleNote:{}};
const $=id=>nodes[id];const state={};const updateButton=()=>{};
renderSettings({settings:{board_profile:'v52',analysis_channel:4,method:'it',potential_v:-.2,fsr_nA:50}});
const ble=nodes.conditionText.textContent;
renderSettings({settings:{method:'it',potential_v:.2,fsr_nA:500}});
const usb=nodes.conditionText.textContent;
""", "{ble,usb}",
    )
    assert "CH4" in result["ble"]
    assert "未校准" in result["ble"]
    assert result["usb"] == "I-T · +0.20 V · 500 nA"


def test_ble_scan_is_explicit_and_stop_requires_acknowledgement():
    source = (GUI_DIR / "app.js").read_text(encoding="utf-8")
    functions = "\n".join(_extract_js_function(source, name) for name in ["renderHardwareConnection", "scanBleDevices", "stopBleDevice"])
    script = functions + """
const nodes=new Proxy({}, {get(target,key){return target[key]||(target[key]={disabled:false,textContent:'',hidden:true})}});
const $=id=>nodes[id];const state={};const calls=[];const messages=[];
const renderBleControls=()=>{};const renderDeviceList=value=>{state.devices=value};
const refreshMeasurement=async()=>{};const toast=value=>messages.push(value);const diagnosticMessage=error=>error.message;
let confirmed=false;
const api=async(path,options)=>{calls.push([path,options.method]);return path.endsWith('/scan')?{devices:[]}:{stop_confirmed:confirmed,message:'STOP request sent'}};
(async()=>{await scanBleDevices();await stopBleDevice();const failed=nodes.deviceDialogBusy.textContent;
confirmed=true;await stopBleDevice();console.log(JSON.stringify({calls,messages,failed}));})().catch(error=>{console.error(error);process.exit(1)});
"""
    completed = run_node(script, check=True, capture_output=True, text=True)
    result = json.loads(completed.stdout)
    assert result["calls"] == [["/api/devices/ble/scan", "POST"], ["/api/devices/ble/stop", "POST"], ["/api/devices/ble/stop", "POST"]]
    assert "确认" in result["failed"]
    assert len(result["messages"]) == 1


def test_visible_ble_panel_gives_each_measurement_section_its_own_content_height():
    css = (GUI_DIR / "styles.css").read_text(encoding="utf-8")
    selector = r"#view-measure\.active:has\(\s*>\s*#blePanel:not\(\[hidden\]\)\)"
    rule = re.search(selector + r"\s*\{([^}]+)\}", css)
    assert rule, "Visible BLE adds a third section; the old flexible middle row can collapse to zero"
    declarations = dict(
        declaration.strip().split(":", 1)
        for declaration in rule.group(1).split(";")
        if ":" in declaration
    )
    assert declarations["grid-template-rows"].strip() == "auto auto auto"
    assert declarations["align-content"].strip() == "start"


def test_ble_copy_distinguishes_raw_capture_from_resampled_analysis():
    source = (GUI_DIR / "app.js").read_text(encoding="utf-8")
    assert "四路原始文件不重采样" in source
    assert "分析导出按 ${fmt(s.target_rate_hz,2)} Hz 重采样" in source
    assert "输出频率不做重采样" not in source
