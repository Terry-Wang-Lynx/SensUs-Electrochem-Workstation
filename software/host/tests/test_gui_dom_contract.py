import json
import re
import subprocess
from pathlib import Path

from PIL import Image

from pa_host.gui_server import OFFSET_OPTIONS


GUI_DIR = Path(__file__).parents[1] / "pa_host" / "gui"


def _extract_js_function(source: str, name: str) -> str:
    start = source.index(f"function {name}(")
    if source[max(0, start - 6):start] == "async ":
        start -= 6
    # 先跳过参数表再找函数体的 `{`:`function row(point={},index=0)` 那种默认值里就有一个
    # `{`,直接 index("{") 会把它当函数体开头,depth 在参数表里就归零,摘出来的是半截签名
    # (静默的:断言字符串永远匹配不上,读起来像"实现里没写这句")。
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


def _extract_js_const(source: str, name: str) -> str:
    """取出 `const NAME=...;` 整行,原样注入 node 脚本。

    `_evaluate_chart_js` 只会摘函数,而被测函数常引用模块级常量(调色板、角色表)。
    照抄源码里的那一行,测试用到的就是**真值**,不是复制一份会跟着漂的字面量。
    """
    match = re.search(rf"^const {name}=.*$", source, re.MULTILINE)
    assert match, f"app.js 里找不到 const {name}"
    return match.group(0)


def _evaluate_chart_js(names: list[str], setup: str, result: str) -> object:
    return _evaluate_gui_js("app.js", names, setup, result)


def _evaluate_gui_js(
    file_name: str, names: list[str], setup: str, result: str,
) -> object:
    source = (GUI_DIR / file_name).read_text(encoding="utf-8")
    functions = "\n".join(_extract_js_function(source, name) for name in names)
    script = f"{functions}\n{setup}\nconsole.log(JSON.stringify({result}));"
    completed = subprocess.run(
        ["node", "-e", script], check=True, capture_output=True, text=True,
    )
    return json.loads(completed.stdout)


def test_app_references_existing_html_ids() -> None:
    app = (GUI_DIR / "app.js").read_text(encoding="utf-8")
    html = (GUI_DIR / "index.html").read_text(encoding="utf-8")

    referenced = set(re.findall(r"\$\('([^']+)'\)", app))
    declared = set(re.findall(r'\bid="([^"]+)"', html))

    assert referenced <= declared, f"Missing DOM ids: {sorted(referenced - declared)}"


def test_gui_exposes_dynamic_transport_and_graceful_exit_controls() -> None:
    html = (GUI_DIR / "index.html").read_text(encoding="utf-8")
    app = (GUI_DIR / "app.js").read_text(encoding="utf-8")
    styles = (GUI_DIR / "styles.css").read_text(encoding="utf-8")
    swift = (
        Path(__file__).parents[3] / "macos" / "Sources" / "main.swift"
    ).read_text(encoding="utf-8")

    assert 'id="deviceTransport"' in html
    assert 'id="exitApp"' in html
    assert "transport_label" in app
    assert "/api/shutdown" in app
    assert "messageHandlers?.sensusApp" in app
    assert "nativeApp.postMessage('quit')" in app
    assert 'name: "sensusApp"' in swift
    assert 'action == "quit"' in swift
    assert 'payload["action"] as? String == "browseWorkspace"' in swift
    assert "let panel = NSOpenPanel()" in swift
    assert "panel.canChooseDirectories = true" in swift
    assert "webView.uiDelegate = self" in swift
    assert "runJavaScriptConfirmPanelWithMessage" in swift
    assert "runJavaScriptTextInputPanelWithPrompt" in swift
    assert "var onServerRecovered: ((URL) -> Void)?" in swift
    assert "self.onServerRecovered?(url)" in swift
    assert "backend.onServerRecovered = { [weak self] url in" in swift
    assert 'URL(string: "compact", relativeTo: backend.serverURL)' in swift
    assert ".exit-button" in styles
    assert "J-Link · MAX30131" not in html


def test_sidebar_hardware_state_uses_actual_device_discovery() -> None:
    states = _evaluate_chart_js(
        ["renderHardwareConnection"],
        """
const nodes = {
  deviceDot: {className: ''},
  deviceState: {textContent: ''},
  deviceTransport: {textContent: ''},
  deviceStatus: {title: ''},
};
const $ = id => nodes[id];
const capture = () => ({
  dot: nodes.deviceDot.className,
  title: nodes.deviceState.textContent,
  detail: nodes.deviceTransport.textContent,
});
let state = {
  measurement: {state: 'idle'},
  devices: {devices: [{id: 'j1', kind: 'jlink', name: 'J-Link SN 1', selectable: true, target_state: 'unknown'}], probing: false},
};
renderHardwareConnection(); const probeOnly = capture();
state.devices.devices[0].target_state = 'reachable';
renderHardwareConnection(); const connected = capture();
state.devices = {devices: [], probing: false};
renderHardwareConnection(); const disconnected = capture();
state.devices = {devices: [
  {id: 'j1', kind: 'jlink', name: 'J-Link SN 1', selectable: true, target_state: 'reachable'},
  {id: 'u1', kind: 'usb', name: 'USB Board 1', selectable: true, target_state: 'reachable'},
], probing: false};
renderHardwareConnection(); const multiple = capture();
state.devices.selected_device_id = 'u1';
renderHardwareConnection(); const selectedMultiple = capture();
state.devices = {devices: [], selected_device:{id:'u1',kind:'usb',name:'USB Board 1',present:false,target_detail:'USB 已断开'}, selected_device_id:'u1', probing:false};
renderHardwareConnection(); const staleSelected = capture();
state.devices = {devices: [{id:'j1',kind:'jlink',name:'J-Link SN 1',selectable:true,target_state:'unknown'}], probing:true};
renderHardwareConnection(); const checking = capture();
state.devices = {devices: [{id:'j1',kind:'jlink',name:'J-Link SN 1',selectable:true,target_state:'unreachable',target_failure:'probe_communication',target_detail:'探头 USB 通信超时'}], probing:false};
renderHardwareConnection(); const probeTimeout = capture();
""",
        "{probeOnly,connected,disconnected,multiple,selectedMultiple,staleSelected,checking,probeTimeout}",
    )

    assert states == {
        "probeOnly": {
            "dot": "status-dot warning",
            "title": "J-Link 探头已连接",
            "detail": "目标板连接尚未确认",
        },
        "connected": {
            "dot": "status-dot ok",
            "title": "目标板已连接",
            "detail": "J-Link SN 1 · MAX30131",
        },
        "disconnected": {
            "dot": "status-dot",
            "title": "硬件未连接",
            "detail": "未发现 USB DATA 或 J-Link",
        },
        "multiple": {
            "dot": "status-dot warning",
            "title": "已发现 2 个硬件",
            "detail": "请在右上角选择本次使用的设备",
        },
        "selectedMultiple": {
            "dot": "status-dot ok",
            "title": "硬件已连接",
            "detail": "USB Board 1 · MAX30131",
        },
        "staleSelected": {
            "dot": "status-dot error",
            "title": "所选设备已断开",
            "detail": "USB 已断开",
        },
        "checking": {
            "dot": "status-dot probing",
            "title": "正在核对目标板",
            "detail": "J-Link SN 1",
        },
        "probeTimeout": {
            "dot": "status-dot error",
            "title": "J-Link 通信异常",
            "detail": "探头 USB 通信超时",
        },
    }


def test_hardware_actions_require_a_verified_target() -> None:
    states = _evaluate_chart_js(
        ["hardwareOperationStatus"],
        """
let state = {devices: {devices: [], probing: true}};
const probing = hardwareOperationStatus();
state.devices = {devices: [{id:'j1',kind:'jlink',selectable:true,target_state:'unreachable',target_detail:'探头 USB 通信超时'}], probing:false};
const unreachable = hardwareOperationStatus();
state.devices.devices[0].target_state = 'reachable';
const jlink = hardwareOperationStatus();
state.devices = {devices: [{id:'u1',kind:'usb',selectable:true,target_state:'reachable'}], probing:false};
const usb = hardwareOperationStatus();
state.devices = {devices: [
  {id:'j1',kind:'jlink',selectable:true,target_state:'reachable'},
  {id:'u1',kind:'usb',selectable:true,target_state:'reachable'},
], probing:false};
const multiple = hardwareOperationStatus();
state.devices.selected_device_id = 'u1';
const selected = hardwareOperationStatus();
state.devices = {devices: [], selected_device:{id:'u1',kind:'usb',selectable:true,present:false}, selected_device_id:'u1', probing:false};
const disconnected = hardwareOperationStatus();
""",
        "{probing,unreachable,jlink,usb,multiple,selected,disconnected}",
    )

    assert states["probing"] == {"ready": False, "message": "正在核对硬件连接"}
    assert states["unreachable"]["ready"] is False
    assert states["unreachable"]["message"] == "探头 USB 通信超时"
    assert states["jlink"] == {"ready": True, "message": ""}
    assert states["usb"] == {"ready": True, "message": ""}
    assert states["multiple"] == {
        "ready": False,
        "message": "请先选择本次使用的设备",
    }
    assert states["selected"] == {"ready": True, "message": ""}
    assert states["disconnected"] == {"ready": False, "message": "所选设备已断开"}


def test_settings_apply_has_long_timeout_and_reload_safe_progress_polling() -> None:
    app = (GUI_DIR / "app.js").read_text(encoding="utf-8")
    html = (GUI_DIR / "index.html").read_text(encoding="utf-8")

    assert "SETTINGS_APPLY_TIMEOUT_MS = 900000" in app
    assert "timeoutMs:SETTINGS_APPLY_TIMEOUT_MS" in app
    assert "function ensureSettingsProgressPolling()" in app
    assert "state.settingsApplyActive" in app
    assert "state.settingsApplySequence" in app
    assert "data.state==='applying'" in app
    assert "api('/api/settings',{timeoutMs:3000})" in app
    assert "settingsFailureState(state.settings,requested" in app
    assert 'id="settingsError"' in html
    assert "function updateSettingsApplyState()" in app
    assert "button.disabled=applying||measurementBusy||scheduleBusy||deviceBusy||!hardware.ready" in app
    assert "if(applying)ensureSettingsProgressPolling()" in app


def test_it_quick_presets_cover_both_electrodes_at_both_potentials() -> None:
    html = (GUI_DIR / "index.html").read_text(encoding="utf-8")
    app = (GUI_DIR / "app.js").read_text(encoding="utf-8")
    keys = ["needle-ox", "printed-ox", "needle-red", "printed-red"]
    evaluated = _evaluate_chart_js(
        ["itPresetSettings", "itPresetMatches"],
        f"const keys={json.dumps(keys)};",
        "Object.fromEntries(keys.map(key=>[key,{settings:itPresetSettings(key),"
        "lit:keys.filter(other=>itPresetMatches(itPresetSettings(key),other))}]))",
    )

    shared = {
        "method": "it", "prestep_s": 0, "adaptive_stop": False,
        "sens_period_code": 0, "target_rate_hz": 10, "fit_window_s": 20,
        "offset_mode": "20pct",
    }
    # 氧化的工作电位两种电极**不同**(微针 +0.4V / 丝网印刷 +0.2V);还原两者都是 -0.2V
    oxidation = {**shared, "working_electrode_v": 1.2, "duration_s": 180}
    reduction = {
        **shared, "initial_potential_v": -0.2, "potential_v": -0.2,
        "working_electrode_v": 0.25, "duration_s": 120,
    }
    needle = {"fsr_nA": 50, "offset_nA": 10}
    printed = {"fsr_nA": 1000, "offset_nA": 200}
    expected = {
        "needle-ox": {**oxidation, **needle, "initial_potential_v": 0.4,
                      "potential_v": 0.4, "label": "微针 +0.4V"},
        "printed-ox": {**oxidation, **printed, "initial_potential_v": 0.2,
                       "potential_v": 0.2, "label": "丝网印刷 +0.2V"},
        "needle-red": {**reduction, **needle, "label": "微针 -0.2V"},
        "printed-red": {**reduction, **printed, "label": "丝网印刷 -0.2V"},
    }

    for key, settings in expected.items():
        assert evaluated[key]["settings"] == settings, key
        assert f'data-it-preset="{key}"' in html
        assert settings["label"] in html
        # 🔴 还原那一对(-0.2V)**只**靠量程区分。itPresetMatches 只比设置值、不记
        # "点了哪个",所以量程差异一旦被抹掉,那两个按钮会一起高亮。
        assert evaluated[key]["lit"] == [key], key
        # 预设里的 offset_nA 必须与后端按 20% FSR 折算的值一致,否则表单与回读对不上
        ratio = OFFSET_OPTIONS[settings["offset_mode"]][1]
        assert settings["offset_nA"] == round(settings["fsr_nA"] * ratio), key

    preset_handler = _extract_js_function(app, "applyItPreset")
    assert "settingsChanged()" in preset_handler
    assert "/api/settings/apply" not in preset_handler


def test_every_it_quick_preset_passes_the_backend_validator() -> None:
    """四个预设必须能原样通过 `SettingsController.validate`。

    预设是前端硬编码的字面量,而真正的约束在后端:RE = V_WE − E 必须落在
    DAC 的 0.008–1.535 V、offset 必须小于满量程、拟合窗口不能超过时长。
    改电位最容易踩的就是 RE 越界 —— 例如 +0.4V 配 V_WE 0.25V ⇒ RE = −0.15 V,
    界面上看不出任何异常,点「应用条件」才报错。

    这条同时把 offset_nA 交给后端算:断言前端字面量 == 后端按 20% FSR 折算的值。
    """
    from pa_host.gui_server import SettingsController

    keys = ["needle-ox", "printed-ox", "needle-red", "printed-red"]
    presets = _evaluate_chart_js(
        ["itPresetSettings"],
        f"const keys={json.dumps(keys)};",
        "Object.fromEntries(keys.map(key=>[key,itPresetSettings(key)]))",
    )

    for key in keys:
        preset = dict(presets[key])
        preset.pop("label")
        expected_offset = preset.pop("offset_nA")
        settings = SettingsController.validate(preset)      # 越界会抛 ValueError
        reference_electrode_v = (
            settings["working_electrode_v"] - settings["potential_v"]
        )
        assert 0.008 <= reference_electrode_v <= 1.535, (
            f"{key}: RE={reference_electrode_v:.3f} V 超出 DAC 范围"
        )
        assert settings["offset_nA"] == expected_offset, key
        assert settings["offset_nA"] < settings["fsr_nA"], key
        for field in ("potential_v", "working_electrode_v", "duration_s", "fsr_nA"):
            assert settings[field] == preset[field], (key, field)

    # 微针氧化是四个里 RE 最低的一档,顺手把它的实际值钉住(+0.4 配 V_WE 1.2 ⇒ 0.800)
    needle_ox = presets["needle-ox"]
    assert round(needle_ox["working_electrode_v"] - needle_ox["potential_v"], 3) == 0.8


def test_it_quick_preset_populates_the_form_without_applying_hardware() -> None:
    result = _evaluate_chart_js(
        ["itPresetSettings", "applyItPreset"],
        """
const nodes={potentialV:{},workingElectrodeV:{},durationS:{},adaptiveStop:{},sensPeriodCode:{},sampleRateHz:{},fitWindowS:{},fsrNA:{},offsetNA:{}};
const $=id=>nodes[id];const state={settings:{},settingsApplyActive:false,method:'cv'};
let changed=0;const notices=[];const settingsChanged=()=>{changed+=1};const toast=message=>notices.push(message);
const applied=applyItPreset('printed-red');
const values=Object.fromEntries(Object.entries(nodes).map(([key,node])=>[key,key==='adaptiveStop'?node.checked:node.value]));
""",
        "{applied,method:state.method,changed,notices,values}",
    )

    assert result == {
        "applied": True,
        "method": "it",
        "changed": 1,
        "notices": ["丝网印刷 -0.2V 已载入，请点击“应用条件”"],
        "values": {
            "potentialV": -0.2,
            "workingElectrodeV": 0.25,
            "durationS": 120,
            "adaptiveStop": False,
            "sensPeriodCode": "0",
            "sampleRateHz": 10,
            "fitWindowS": 20,
            "fsrNA": "1000",
            "offsetNA": "20pct",
        },
    }


def test_settings_validation_failure_preserves_the_user_draft() -> None:
    result = _evaluate_chart_js(
        ["settingsInputIssue", "settingsErrorFieldIds", "settingsFailureState"],
        """
const current={settings:{method:'it',potential_v:.2,working_electrode_v:1.2},state:'applied',applied:true};
const requested={method:'it',potential_v:.4,working_electrode_v:.25,fsr_nA:50,offset_mode:'40nA'};
const issue=settingsInputIssue(requested);
const failure=settingsFailureState(current,requested,issue.message);
const mappedFields=settingsErrorFieldIds(issue.message);
""",
        "{issue,failure,mappedFields}",
    )

    assert result["issue"]["fields"] == ["potentialV", "workingElectrodeV"]
    assert "RE=-0.150 V" in result["issue"]["message"]
    assert result["failure"]["settings"] == {
        "method": "it",
        "potential_v": 0.4,
        "working_electrode_v": 0.25,
        "fsr_nA": 50,
        "offset_mode": "40nA",
    }
    assert result["failure"]["state"] == "error"
    assert result["failure"]["applied"] is False
    assert result["mappedFields"] == ["potentialV", "workingElectrodeV"]


def test_settings_progress_polling_ignores_a_stale_pre_apply_snapshot() -> None:
    source = (GUI_DIR / "app.js").read_text(encoding="utf-8")
    function = _extract_js_function(source, "ensureSettingsProgressPolling")
    script = f"""
{function}
const state={{
  exiting:false,settingsApplyActive:true,settingsApplySequence:7,
  settingsApplyRequestPending:true,settingsPollSequence:0,
}};
const replies=[
  {{state:'applied',settings:{{potential_v:.2}}}},
  {{state:'applying',settings:{{potential_v:.2}}}},
];
const timers=[],renders=[];
const setTimeout=(callback,delay)=>timers.push({{callback,delay}});
const api=async()=>replies.shift();
const readSettings=()=>({{potential_v:.4,working_electrode_v:.25}});
const renderSettings=data=>renders.push(data);
(async()=>{{
  ensureSettingsProgressPolling();
  const initialDelay=timers[0].delay;
  await timers.shift().callback();
  const afterStale=renders.length;
  const retryDelay=timers[0].delay;
  await timers.shift().callback();
  const appliedDraft=renders[0].settings;
  state.settingsApplyActive=false;
  await timers.shift().callback();
  console.log(JSON.stringify({{initialDelay,afterStale,retryDelay,appliedDraft,pollSequence:state.settingsPollSequence}}));
}})().catch(error=>{{console.error(error);process.exit(1)}});
"""
    completed = subprocess.run(
        ["node", "-e", script], check=True, capture_output=True, text=True,
    )

    assert json.loads(completed.stdout) == {
        "initialDelay": 250,
        "afterStale": 0,
        "retryDelay": 1000,
        "appliedDraft": {"potential_v": 0.4, "working_electrode_v": 0.25},
        "pollSequence": 0,
    }


def test_settings_progress_polling_resumes_an_apply_after_page_reload() -> None:
    source = (GUI_DIR / "app.js").read_text(encoding="utf-8")
    function = _extract_js_function(source, "ensureSettingsProgressPolling")
    script = f"""
{function}
const state={{
  exiting:false,settings:{{state:'applying'}},settingsApplyActive:false,
  settingsApplyRequestPending:false,settingsApplySequence:0,
  settingsPollSequence:0,settingsDirty:false,
}};
const replies=[
  {{state:'applying',settings:{{potential_v:.4,working_electrode_v:1.2}}}},
  {{state:'applied',applied:true,error:'',settings:{{potential_v:.4,working_electrode_v:1.2}}}},
];
const timers=[],renders=[],errors=[];let clears=0;
const setTimeout=(callback,delay)=>timers.push({{callback,delay}});
const api=async()=>replies.shift();
const readSettings=()=>({{potential_v:.4,working_electrode_v:1.2}});
const renderSettings=data=>{{renders.push(data);state.settings=data}};
const showSettingsError=error=>errors.push(error.message);
const clearSettingsError=()=>{{clears+=1}};
(async()=>{{
  ensureSettingsProgressPolling();
  const resumed={{active:state.settingsApplyActive,sequence:state.settingsApplySequence,delay:timers[0].delay}};
  await timers.shift().callback();
  await timers.shift().callback();
  console.log(JSON.stringify({{
    resumed,renders:renders.map(item=>item.state),errors,clears,
    active:state.settingsApplyActive,pending:state.settingsApplyRequestPending,
    dirty:state.settingsDirty,pollSequence:state.settingsPollSequence,
  }}));
}})().catch(error=>{{console.error(error);process.exit(1)}});
"""
    completed = subprocess.run(
        ["node", "-e", script], check=True, capture_output=True, text=True,
    )

    assert json.loads(completed.stdout) == {
        "resumed": {"active": True, "sequence": 1, "delay": 250},
        "renders": ["applying", "applied"],
        "errors": [],
        "clears": 1,
        "active": False,
        "pending": False,
        "dirty": False,
        "pollSequence": 0,
    }


def test_settings_apply_error_path_never_reloads_old_values() -> None:
    app = (GUI_DIR / "app.js").read_text(encoding="utf-8")
    start = app.index("$('applySettings').onclick")
    end = app.index("function makeCell", start)
    handler = app[start:end]

    assert "settingsFailureState" in handler
    assert "showSettingsError" in handler
    assert "if(state.settingsApplyActive)return" in handler
    assert "state.settingsApplyRequestPending=true" in handler
    assert "await api('/api/settings')" not in handler
    assert "try{renderWorkflow(await api('/api/workflow'))}" in handler
    assert "reportClientIssue('workflow_refresh_failed'" in handler
    assert handler.index("}finally{") < handler.index("try{renderWorkflow")


def test_schedule_and_settings_buttons_share_the_hardware_gate() -> None:
    states = _evaluate_chart_js(
        [
            "workspaceReady", "hardwareOperationStatus",
            "updateSettingsApplyState", "updateScheduleStartState",
        ],
        """
const nodes = {
  applySettings: {disabled: false, title: ''},
  startSchedule: {disabled: false, title: ''},
};
const $ = id => nodes[id];
let state = {
  workflow: {workspace_available: true},
  settings: {state: 'ready', applied: true},
  measurement: {busy: false},
  schedule: {active: false},
  devices: {busy: false, probing: false, devices: [
    {id:'j1', kind:'jlink', selectable:true, target_state:'unreachable'},
  ]},
};
const capture = () => ({
  apply: {disabled:nodes.applySettings.disabled,title:nodes.applySettings.title},
  schedule: {disabled:nodes.startSchedule.disabled,title:nodes.startSchedule.title},
});
updateSettingsApplyState(); updateScheduleStartState(); const unreachable = capture();
state.devices.devices[0].target_state = 'reachable';
updateSettingsApplyState(); updateScheduleStartState(); const ready = capture();
state.measurement.busy = true;
updateSettingsApplyState(); updateScheduleStartState(); const measuring = capture();
""",
        "{unreachable,ready,measuring}",
    )

    assert states["unreachable"]["apply"]["disabled"] is True
    assert "目标板无响应" in states["unreachable"]["apply"]["title"]
    assert states["unreachable"]["schedule"]["disabled"] is True
    assert states["ready"] == {
        "apply": {"disabled": False, "title": ""},
        "schedule": {"disabled": False, "title": ""},
    }
    assert states["measuring"]["apply"] == {
        "disabled": True,
        "title": "测量进行中，不能修改硬件条件",
    }
    assert states["measuring"]["schedule"]["disabled"] is True


def test_measurement_start_validates_inputs_keeps_errors_and_waits_for_gate() -> None:
    html = (GUI_DIR / "index.html").read_text(encoding="utf-8")
    app = (GUI_DIR / "app.js").read_text(encoding="utf-8")

    assert 'id="sampleName" placeholder="例如：未知样品 01" required' in html
    assert 'id="knownConcentration" type="number" min="0" step="any"' in html
    assert 'placeholder="请输入标定浓度" required' in html
    assert "MEASUREMENT_START_TIMEOUT_MS = 180000" in app
    assert "timeoutMs:MEASUREMENT_START_TIMEOUT_MS" in app
    assert "function measurementInputIssue()" in app
    assert "function clearMeasurementInputState()" in app
    assert "else if(!state.measureRequestError)$('measureError').hidden=true" in app
    assert 'input[aria-invalid="true"]' in (GUI_DIR / "styles.css").read_text(encoding="utf-8")


def test_measurement_start_pending_survives_stale_status_refresh() -> None:
    states = _evaluate_chart_js(
        ["workspaceReady", "hardwareOperationStatus", "updateStartState"],
        """
const button = {disabled:false,textContent:'',title:''};
const $ = id => { if(id==='startMeasure')return button; throw new Error(id); };
let state = {
  measurement:{state:'idle',busy:false}, measurementStartPending:true,
  workflow:{workspace_available:true,calibration_ready:true},
  settings:{applied:true}, method:'it', sampleRole:'calibration',
  devices:{devices:[{id:'j1',kind:'jlink',selectable:true,target_state:'reachable'}],probing:false},
};
const capture=()=>({disabled:button.disabled,text:button.textContent,title:button.title});
updateStartState(); const clicked=capture();
state.measurement={state:'idle',busy:false}; updateStartState(); const staleIdle=capture();
state.measurementStartPending=false; updateStartState(); const ready=capture();
state.measurement={state:'running',busy:true,operation_phase:'configuring',config_gate:{state:'checking'}};
updateStartState(); const configuring=capture();
""",
        "{clicked,staleIdle,ready,configuring}",
    )

    assert states["clicked"]["disabled"] is True
    assert states["clicked"]["text"] == "正在核对硬件配置…"
    assert states["staleIdle"] == states["clicked"]
    assert states["ready"] == {"disabled": False, "text": "开始标定测量", "title": ""}
    assert states["configuring"]["disabled"] is True
    assert states["configuring"]["text"] == "正在核对硬件配置…"


def test_compact_measurement_start_has_pending_guard_and_full_gate_timeout() -> None:
    compact = (GUI_DIR / "compact.js").read_text(encoding="utf-8")
    states = _evaluate_gui_js(
        "compact.js",
        ["hardwareOperationStatus", "updateButton"],
        """
const nodes={
  concentration:{value:'1'},sampleName:{value:'standard'},
  measureButton:{disabled:false,textContent:'',title:'',classList:{toggle(){}}},
};
const $=id=>nodes[id];
let state={
  measurement:{state:'idle'},measurementAction:null,
  settings:{applied:true,settings:{method:'it'}},
  workflow:{workspace_available:true,save_dir:'/data',calibration_ready:false},
  role:'calibration',devices:{probing:false,busy:false,devices:[]},
};
const capture=()=>({disabled:nodes.measureButton.disabled,text:nodes.measureButton.textContent,title:nodes.measureButton.title});
updateButton();const disconnected=capture();
state.devices.devices=[{id:'j1',kind:'jlink',selectable:true,target_state:'unreachable',target_detail:'目标板无响应'}];
updateButton();const unreachable=capture();
state.devices.devices[0].target_state='reachable';
updateButton();const ready=capture();
state.measurementAction='starting';
updateButton();const starting=capture();
state.measurementAction=null;state.measurement={state:'running',operation_phase:'running',config_gate:{state:'matched'}};
updateButton();const running=capture();
state.measurementAction='stopping';
updateButton();const stopping=capture();
""",
        "{disconnected,unreachable,ready,starting,running,stopping}",
    )

    assert "MEASUREMENT_START_TIMEOUT_MS = 180000" in compact
    assert "if (state.measurementAction) return" in compact
    assert "state.measurementAction = stopping ? 'stopping' : 'starting'" in compact
    assert "}, MEASUREMENT_START_TIMEOUT_MS)" in compact
    assert "payload.operation_phase === 'configuring'" in compact
    assert "api('/api/devices', {timeoutMs: 3000})" in compact
    assert states == {
        "disconnected": {
            "disabled": True, "text": "开始标定测量", "title": "未发现可用硬件",
        },
        "unreachable": {
            "disabled": True, "text": "开始标定测量", "title": "目标板无响应",
        },
        "ready": {"disabled": False, "text": "开始标定测量", "title": ""},
        "starting": {
            "disabled": True, "text": "正在核对硬件配置…", "title": "",
        },
        "running": {"disabled": False, "text": "停止测量", "title": ""},
        "stopping": {"disabled": True, "text": "正在停止…", "title": ""},
    }


def test_gui_exposes_diagnostics_and_frontend_error_boundaries() -> None:
    html = (GUI_DIR / "index.html").read_text(encoding="utf-8")
    app = (GUI_DIR / "app.js").read_text(encoding="utf-8")
    styles = (GUI_DIR / "styles.css").read_text(encoding="utf-8")

    for element_id in (
        "globalError", "openDiagnostics", "diagnosticsPanel",
        "diagnosticsList", "diagnosticsPath", "refreshDiagnostics",
        "downloadDiagnostics",
    ):
        assert f'id="{element_id}"' in html
    assert 'href="/api/diagnostics/download"' in html
    assert "/api/diagnostics?limit=120" in app
    assert "/api/diagnostics/client" in app
    assert "window.addEventListener('error'" in app
    assert "window.addEventListener('unhandledrejection'" in app
    assert "function reportClientIssue(" in app
    assert ".diagnostic-event{" in styles


def test_diagnostic_message_preserves_backend_id() -> None:
    messages = _evaluate_chart_js(
        ["diagnosticMessage"],
        """
const plain = diagnosticMessage(new Error('plain failure'));
const backend = new Error('backend failure'); backend.diagnosticId = 'D-123';
const identified = diagnosticMessage(backend);
""",
        "{plain,identified}",
    )

    assert messages == {
        "plain": "plain failure",
        "identified": "backend failure（诊断编号 D-123）",
    }


def test_measurement_input_issue_requires_calibration_metadata() -> None:
    issues = _evaluate_chart_js(
        ["measurementInputIssue"],
        """
const nodes = {sampleName:{value:''},knownConcentration:{value:''}};
const $ = id => nodes[id];
let state = {method:'it',sampleRole:'calibration'};
const missingName = measurementInputIssue();
nodes.sampleName.value = 'standard';
const missingConcentration = measurementInputIssue();
nodes.knownConcentration.value = '12.5';
const calibrationReady = measurementInputIssue();
state.sampleRole = 'test'; nodes.knownConcentration.value = '';
const testReady = measurementInputIssue();
""",
        "{missingName,missingConcentration,calibrationReady,testReady}",
    )

    assert issues == {
        "missingName": {"field": "sampleName", "message": "请填写样品名称"},
        "missingConcentration": {
            "field": "knownConcentration",
            "message": "请填写标定样品的已知浓度",
        },
        "calibrationReady": None,
        "testReady": None,
    }


def test_gui_exposes_manual_multi_device_picker() -> None:
    html = (GUI_DIR / "index.html").read_text(encoding="utf-8")
    app = (GUI_DIR / "app.js").read_text(encoding="utf-8")
    styles = (GUI_DIR / "styles.css").read_text(encoding="utf-8")

    for element_id in ("selectDevice", "deviceDialog", "refreshDevices", "deviceList"):
        assert f'id="{element_id}"' in html
    assert "/api/devices" in app
    assert "/api/devices/select" in app
    assert "/api/devices/jlink-driver/install" in app
    assert "准备 J-Link" in app
    assert "请点击右上角“选择设备”，再点击该 J-Link 的“准备 J-Link”" in app
    assert "会把所选探针的调试接口切换" in app
    assert "device-card" in styles


def test_known_concentration_stepper_has_stable_accessible_controls() -> None:
    html = (GUI_DIR / "index.html").read_text(encoding="utf-8")
    app = (GUI_DIR / "app.js").read_text(encoding="utf-8")
    styles = (GUI_DIR / "styles.css").read_text(encoding="utf-8")

    assert 'id="knownConcentration" type="number" min="0" step="any"' in html
    assert 'id="doubleKnownConcentration" type="button"' in html
    assert 'id="halveKnownConcentration" type="button"' in html
    assert 'aria-label="已知浓度乘二"' in html
    assert 'aria-label="已知浓度除以二"' in html
    assert "['sampleName','knownConcentration'].forEach" in app
    assert "clearMeasureError();previewFilename()" in app
    assert "new Event('input',{bubbles:true})" in app
    assert "points_revision:state.calibration?.points_revision??null" in app
    assert ".concentration-input-row{display:grid;grid-template-columns:minmax(0,1fr) auto" in styles
    assert ".concentration-stepper button{width:38px;min-width:38px" in styles
    assert "#concentrationField,.sample-controls .action-row" in styles


def test_known_concentration_scaling_handles_boundaries_without_invalid_values() -> None:
    result = _evaluate_chart_js(
        ["scaledConcentrationValue"],
        """
const values = {
  doubled: scaledConcentrationValue('1.25', 2),
  halved: scaledConcentrationValue('1.25', .5),
  roundTrip: scaledConcentrationValue(scaledConcentrationValue('0.1', 2), .5),
  zeroDouble: scaledConcentrationValue('0', 2),
  zeroHalf: scaledConcentrationValue('0', .5),
  blank: scaledConcentrationValue('  ', 2),
  invalid: scaledConcentrationValue('not-a-number', 2),
  negative: scaledConcentrationValue('-1', 2),
  overflow: scaledConcentrationValue('1e308', 2),
};
""",
        "values",
    )

    assert result == {
        "doubled": "2.5",
        "halved": "0.625",
        "roundTrip": "0.1",
        "zeroDouble": "0",
        "zeroHalf": "0",
        "blank": None,
        "invalid": None,
        "negative": None,
        "overflow": None,
    }


def test_known_concentration_stepper_dispatches_one_bubbling_input_event() -> None:
    result = _evaluate_chart_js(
        ["scaledConcentrationValue", "scaleKnownConcentration"],
        """
class Event { constructor(type, options={}) { this.type=type; this.bubbles=Boolean(options.bubbles); } }
const events = [], messages = [];
const input = {value:'1.25', dispatchEvent(event){events.push({type:event.type,bubbles:event.bubbles,value:this.value});}};
const $ = id => { if(id!=='knownConcentration')throw new Error(id); return input; };
const toast = message => messages.push(message);
const success = scaleKnownConcentration(2);
input.value = '';
const rejected = scaleKnownConcentration(.5);
""",
        "{success,rejected,events,messages,value:input.value}",
    )

    assert result == {
        "success": True,
        "rejected": False,
        "events": [{"type": "input", "bubbles": True, "value": "2.5"}],
        "messages": ["请先输入有效的非负浓度"],
        "value": "",
    }


def test_both_filter_panels_expose_only_the_shared_lowpass_configuration() -> None:
    html = (GUI_DIR / "index.html").read_text(encoding="utf-8")
    app = (GUI_DIR / "app.js").read_text(encoding="utf-8")
    required = {
        "mode", "lowpass_enabled", "lowpass_cutoff_hz", "lowpass_auto",
        "lowpass_order",
    }
    for key in required:
        assert html.count(f'data-filter-key="{key}"') == 2, key
    assert html.count('data-show-raw') == 2
    assert html.count('data-filter-apply') == 0
    assert html.count('>显示原数据</span>') == 2
    assert html.count('value="0.3" data-filter-key="lowpass_cutoff_hz"') == 2
    assert "function queueFilterSave(delay=450)" in app
    assert "async function flushFilterSave()" in app
    assert "state.filterSaving" in app
    assert "'/api/filter/apply'" in app
    assert "已自动保存" in app
    assert "data-filter-note" not in html
    assert "等待足够数据后计算滤波器" not in app
    assert "分析结果另存为 *-filtered.csv" not in app
    assert "notch" not in html.lower()


def test_calibration_chart_has_independent_point_visibility_controls() -> None:
    html = (GUI_DIR / "index.html").read_text(encoding="utf-8")

    assert 'id="showCalibrationPoints" type="checkbox" checked' in html
    assert 'id="showTestPoints" type="checkbox" checked' in html


def test_workspace_history_view_exposes_current_workspace_batches_and_safe_remove_controls() -> None:
    html = (GUI_DIR / "index.html").read_text(encoding="utf-8")
    app = (GUI_DIR / "app.js").read_text(encoding="utf-8")
    styles = (GUI_DIR / "styles.css").read_text(encoding="utf-8")

    assert 'data-view="history"' in html
    for element_id in (
        "view-history", "workspaceHistoryList", "historyFavoritesOnly",
        "refreshWorkspaceHistory", "workspaceHistoryError",
    ):
        assert f'id="{element_id}"' in html
    for endpoint in (
        "/api/history", "/api/history/open", "/api/history/favorite",
        "/api/history/remove",
    ):
        assert endpoint in app
    assert "原始测量目录和数据不会被删除" in app
    assert "workspaceHasUnsavedChanges" in app
    assert "discard_unsaved:unsaved" in app
    assert "current_batches" in app
    assert "historyWorkspaceSelect" not in app
    assert ".workspace-history-card.unavailable" in styles
    assert ".workspace-history-list{grid-template-columns:minmax(0,1fr)" in styles
    assert ".sidebar nav{grid-template-columns:repeat(5,1fr)}" in styles


def test_workspace_picker_and_hard_measurement_gate_are_exposed() -> None:
    html = (GUI_DIR / "index.html").read_text(encoding="utf-8")
    app = (GUI_DIR / "app.js").read_text(encoding="utf-8")

    assert 'id="browseWorkspace"' in html
    assert 'id="applySaveDirectory"' not in html
    assert "/api/workspace/browse" in app
    assert "browseWorkspaceDirectory" in app
    assert "sensusNativeWorkspaceBrowseResult" in app
    assert "function confirmAndSwitchWorkspace(path)" in app
    assert "window.confirm('确认切换工作区？')" in app
    assert "$('saveDirectory').addEventListener('blur'" in app
    assert "event.key!=='Enter'" in app
    assert "batch_name:''" in app
    assert "function workspaceReady()" in app
    assert "!workspaceReady()" in app
    assert "workspace_available" in app
    assert "save_dir:$('saveDirectory').value" not in app


def test_workspace_path_refresh_preserves_the_active_draft() -> None:
    source = (GUI_DIR / "app.js").read_text(encoding="utf-8")
    function = _extract_js_function(source, "syncWorkspacePathInput")
    script = f"""
{function}
const input={{value:'/draft'}};
const $=()=>input;
const state={{workspaceSwitchPromise:null}};
const document={{activeElement:input}};
syncWorkspacePathInput({{workspace_root:'/server'}});
const whileFocused=input.value;
document.activeElement={{}};
syncWorkspacePathInput({{workspace_root:'/server'}});
const afterBlur=input.value;
input.value='/switching';
state.workspaceSwitchPromise=Promise.resolve();
syncWorkspacePathInput({{workspace_root:'/stale'}});
const whileSwitching=input.value;
syncWorkspacePathInput({{workspace_root:'/confirmed'}},true);
console.log(JSON.stringify({{whileFocused,afterBlur,whileSwitching,forced:input.value}}));
"""
    completed = subprocess.run(
        ["node", "-e", script], check=True, capture_output=True, text=True,
    )
    assert json.loads(completed.stdout) == {
        "whileFocused": "/draft",
        "afterBlur": "/server",
        "whileSwitching": "/switching",
        "forced": "/confirmed",
    }


def test_workspace_picker_uses_native_bridge_with_backend_fallback() -> None:
    source = (GUI_DIR / "app.js").read_text(encoding="utf-8")
    functions = "\n".join(_extract_js_function(source, name) for name in (
        "handleNativeWorkspaceBrowseResult",
        "browseWorkspaceDirectory",
    ))
    script = f"""
{functions}
const WORKSPACE_BROWSE_TIMEOUT_MS=1000;
const nativeWorkspaceBrowseRequests=new Map();
const messages=[],requests=[];
const window={{webkit:{{messageHandlers:{{sensusApp:{{postMessage:value=>messages.push(value)}}}}}}}};
const api=async(path,options)=>{{requests.push({{path,options}});return {{selected:false,path:''}}}};
(async()=>{{
  const pending=browseWorkspaceDirectory('/initial');
  const message=messages[0];
  handleNativeWorkspaceBrowseResult({{request_id:message.request_id,selected:true,path:'/selected'}});
  const nativeResult=await pending;
  window.webkit=null;
  const fallbackResult=await browseWorkspaceDirectory('/fallback');
  console.log(JSON.stringify({{message,nativeResult,fallbackResult,requests}}));
}})().catch(error=>{{console.error(error);process.exit(1)}});
"""
    completed = subprocess.run(
        ["node", "-e", script], check=True, capture_output=True, text=True,
    )
    result = json.loads(completed.stdout)
    assert result["message"]["action"] == "browseWorkspace"
    assert result["message"]["initial_path"] == "/initial"
    assert result["message"]["request_id"]
    assert result["nativeResult"] == {"selected": True, "path": "/selected"}
    assert result["fallbackResult"] == {"selected": False, "path": ""}
    assert result["requests"][0]["path"] == "/api/workspace/browse"
    assert json.loads(result["requests"][0]["options"]["body"]) == {
        "initial_path": "/fallback",
    }


def test_workspace_switch_confirmation_is_single_step_and_cancelable() -> None:
    source = (GUI_DIR / "app.js").read_text(encoding="utf-8")
    functions = "\n".join(_extract_js_function(source, name) for name in (
        "configuredWorkspacePath",
        "restoreConfiguredWorkspacePath",
        "confirmAndSwitchWorkspace",
    ))
    script = f"""
{functions}
const nodes={{saveDirectory:{{value:'/old',disabled:false}},browseWorkspace:{{disabled:false}}}};
const $=id=>nodes[id];
let state={{workflow:{{workspace_root:'/old',save_dir:'/old/batch'}},workspaceSwitchPromise:null,calibration:null,historyPreview:null,historyCurves:[],historyCurveIds:[]}};
const confirmations=[false,true],requests=[],toasts=[],errors=[];
const window={{confirm:()=>confirmations.shift()}};
const post=async(path,payload)=>{{requests.push({{path,payload}});return {{workspace_root:'/new',save_dir:'/new/batch'}}}};
const api=async()=>({{points:[]}});
const renderWorkflow=data=>{{state.workflow=data;nodes.saveDirectory.value=data.workspace_root||data.save_dir||''}};
const renderCalibration=()=>{{}};
const refreshWorkspaceHistory=async()=>{{}};
const toast=message=>toasts.push(message);
const errorBox=(id,error)=>errors.push(String(error));
(async()=>{{
  nodes.saveDirectory.value='/cancelled';
  const cancelled=await confirmAndSwitchWorkspace('/cancelled');
  const restoredAfterCancel=nodes.saveDirectory.value;
  nodes.saveDirectory.value='/new';
  const switched=await confirmAndSwitchWorkspace('/new');
  const duplicate=await confirmAndSwitchWorkspace('/new');
  console.log(JSON.stringify({{cancelled,restoredAfterCancel,switched,duplicate,requests,toasts,errors,finalPath:nodes.saveDirectory.value}}));
}})().catch(error=>{{console.error(error);process.exit(1)}});
"""
    completed = subprocess.run(
        ["node", "-e", script], check=True, capture_output=True, text=True,
    )
    result = json.loads(completed.stdout)

    assert result == {
        "cancelled": False,
        "restoredAfterCancel": "/old",
        "switched": True,
        "duplicate": False,
        "requests": [{
            "path": "/api/workflow/config",
            "payload": {"save_dir": "/new", "batch_name": ""},
        }],
        "toasts": ["已切换工作区并新建批次"],
        "errors": [],
        "finalPath": "/new",
    }


def test_gui_exposes_history_curve_overlay_batch_naming_and_cross_add_controls() -> None:
    html = (GUI_DIR / "index.html").read_text(encoding="utf-8")
    app = (GUI_DIR / "app.js").read_text(encoding="utf-8")

    for element_id in (
        "toggleHistoryCurves", "historyCurvePanel", "historyCurveList",
        "refreshHistoryCurves", "selectAllHistoryCurves",
    ):
        assert f'id="{element_id}"' in html
    assert "batch_name" in app
    assert "/api/history/curves" in app
    assert "/api/history/curves/load" in app
    assert "function selectAllHistoryCurves(selected)" in app
    assert "toggle.indeterminate=selected>0&&selected<inputs.length" in app
    assert "/api/calibration/promote-validation" in app
    assert "/api/calibration/add-validation" in app
    assert "/api/calibration/validation/delete" in app
    assert "validation-row-actions" in app
    assert "integerX:true" in app
    assert '>切换工作区</button>' not in html
    assert '>新建批次</button>' in html
    assert "新建标定批次" not in html
    assert "前后端版本不一致，请退出并重新打开软件" in app
    assert "#chartWindow button,#dbgChartWindow button{min-width:48px" in (
        GUI_DIR / "styles.css"
    ).read_text(encoding="utf-8")
    assert ".sidebar nav{grid-template-columns:repeat(5,minmax(0,1fr))}" in (
        GUI_DIR / "styles.css"
    ).read_text(encoding="utf-8")


def test_history_curve_select_all_reflects_empty_partial_and_complete_states() -> None:
    states = _evaluate_chart_js(
        ["syncHistoryCurveSelectAll"],
        """
let inputs = [];
const toggle = {disabled: false, checked: false, indeterminate: false};
const list = {querySelectorAll: () => inputs};
const $ = id => id === 'selectAllHistoryCurves' ? toggle : list;
const capture = () => ({
  disabled: toggle.disabled,
  checked: toggle.checked,
  indeterminate: toggle.indeterminate,
});
syncHistoryCurveSelectAll(); const empty = capture();
inputs = [{checked: true}, {checked: false}];
syncHistoryCurveSelectAll(); const partial = capture();
inputs[1].checked = true;
syncHistoryCurveSelectAll(); const complete = capture();
""",
        "{empty,partial,complete}",
    )

    assert states == {
        "empty": {"disabled": True, "checked": False, "indeterminate": False},
        "partial": {"disabled": False, "checked": False, "indeterminate": True},
        "complete": {"disabled": False, "checked": True, "indeterminate": False},
    }


def test_app_update_button_is_visible_only_for_an_available_portable_update() -> None:
    html = (GUI_DIR / "index.html").read_text(encoding="utf-8")
    app = (GUI_DIR / "app.js").read_text(encoding="utf-8")
    assert 'id="appUpdate"' in html
    assert "/api/app-update/start" in app
    assert "/api/app-update/apply" in app

    result = _evaluate_chart_js(
        ["renderAppUpdate"],
        """
const button={hidden:false,disabled:false,textContent:'',title:''};
const $=id=>button;
let state={appUpdateRequested:false,appUpdate:null};
const capture=()=>({hidden:button.hidden,disabled:button.disabled,text:button.textContent});
renderAppUpdate({available:false,state:'idle'});const none=capture();
renderAppUpdate({available:true,state:'available',latest_version:'0.4.7'});const available=capture();
renderAppUpdate({available:true,state:'downloading',latest_version:'0.4.7',progress:.42});const downloading=capture();
renderAppUpdate({available:true,state:'ready',latest_version:'0.4.7'});const ready=capture();
""",
        "{none,available,downloading,ready}",
    )
    assert result == {
        "none": {"hidden": True, "disabled": False, "text": ""},
        "available": {"hidden": False, "disabled": False, "text": "↻ 更新"},
        "downloading": {"hidden": False, "disabled": True, "text": "↓ 42%"},
        "ready": {"hidden": False, "disabled": False, "text": "↻ 安装更新"},
    }


def test_history_status_label_maps_missing_and_corrupt_entries() -> None:
    values = _evaluate_chart_js(
        ["historyStatusLabel"],
        """
const values = ['available', 'missing', 'corrupt', 'other'].map(historyStatusLabel);
""",
        "values",
    )

    assert values == ["可用", "目录缺失", "数据损坏", "不可用"]


def test_ap_chart_keeps_the_literal_blue_boundary() -> None:
    app = (GUI_DIR / "app.js").read_text(encoding="utf-8")

    assert "zone==='blue'?[x-2,x+2]" in app


def test_ap_chart_zone_colors_keep_green_and_blue_distinct() -> None:
    app = (GUI_DIR / "app.js").read_text(encoding="utf-8")
    styles = (GUI_DIR / "styles.css").read_text(encoding="utf-8")

    assert "band('green','#cfe8dc');band('blue','#cfe1e8')" in app
    assert ".ap-swatch.blue{background:#cfe1e8}.ap-swatch.green{background:#cfe8dc}" in styles


def test_ap_chart_uses_a_fixed_zero_to_sixty_domain() -> None:
    domain = _evaluate_chart_js(
        ["apChartDomain"],
        """
const candidates = [
  {concentration_um: 100, predicted_concentration_um: 250},
  {concentration_um: 30, predicted_concentration_um: -20},
];
const domain = apChartDomain(candidates, candidates);
""",
        "domain",
    )

    assert domain == {"xMax": 60, "yMax": 60, "minValue": 0}


def test_ap_chart_is_square_and_visible_without_test_points() -> None:
    html = (GUI_DIR / "index.html").read_text(encoding="utf-8")
    app = (GUI_DIR / "app.js").read_text(encoding="utf-8")
    styles = (GUI_DIR / "styles.css").read_text(encoding="utf-8")

    assert ".ap-chart{width:640px" in styles
    assert "max-width:100%;height:auto;aspect-ratio:1/1;padding:0" in styles
    assert ".ap-chart canvas{display:block}" in styles
    assert ".ap-chart{height:330px}" not in styles
    assert "const m={l:52,r:18,t:18,b:52}" in app
    assert app.count("ctx.rect(m.l,m.t,w-m.l-m.r,h-m.t-m.b);ctx.clip()") == 2
    assert "apScoreEmpty" not in html
    assert "apScoreEmpty" not in app


def test_unknown_concentrations_are_excluded_from_coordinate_charts() -> None:
    included = _evaluate_chart_js(
        ["hasFiniteConcentration"],
        """
const points = [
  {concentration_um: null},
  {concentration_um: ''},
  {},
  {concentration_um: 0},
  {concentration_um: '5'},
];
""",
        "points.map(hasFiniteConcentration)",
    )

    assert included == [False, False, False, True, True]


def test_browser_inverse_matches_backend_selection_and_tolerance() -> None:
    values = _evaluate_chart_js(
        ["modelPredictAt"],
        """
const quadratic = {
  degree: 2, coefficients: [-1, 10, -25],
  concentration_min_um: 0, concentration_max_um: 10,
};
const shallowLinear = {
  degree: 1, coefficients: [1e-14, 0],
  concentration_min_um: 0, concentration_max_um: 10,
};
""",
        "[modelPredictAt(quadratic, -4), modelPredictAt(shallowLinear, 2e-14)]",
    )

    assert values == [7, 2]


def test_unknown_concentration_does_not_hide_an_uninvertible_point() -> None:
    has_uninvertible = _evaluate_chart_js(
        ["localApDetail", "localApScore", "hasUninvertibleApPoint"],
        """
const points = [
  {concentration_um: null, predicted_concentration_um: 5},
  {concentration_um: 5, predicted_concentration_um: null},
];
""",
        "hasUninvertibleApPoint(localApScore(points))",
    )

    assert has_uninvertible is True


def test_new_run_resets_only_the_fixed_it_chart_window() -> None:
    fixed, tail_20, tail_5, same_run = _evaluate_chart_js(
        ["syncChartWindowRun"],
        """
let state = {chartRunId: 'run-1', chartWindowFixed: true, chartWindowS: 500};
syncChartWindowRun('run-2');
const fixed = {...state};
state = {chartRunId: 'run-2', chartWindowFixed: false, chartWindowS: 20};
syncChartWindowRun('run-3');
const tail20 = {...state};
state = {chartRunId: 'run-3', chartWindowFixed: false, chartWindowS: 5};
syncChartWindowRun('run-4');
const tail5 = {...state};
state = {chartRunId: 'run-4', chartWindowFixed: true, chartWindowS: 600};
syncChartWindowRun('run-4');
const sameRun = {...state};
""",
        "[fixed, tail20, tail5, sameRun]",
    )

    assert fixed == {"chartRunId": "run-2", "chartWindowFixed": True, "chartWindowS": 300}
    assert tail_20["chartWindowS"] == 20
    assert tail_5["chartWindowS"] == 5
    assert same_run["chartWindowS"] == 600


def test_it_conditions_expose_only_one_automatic_stop_control() -> None:
    html = (GUI_DIR / "index.html").read_text(encoding="utf-8")

    assert html.count('id="adaptiveStop"') == 1
    assert html.count(">自动停止</span>") == 1
    assert "智能平台停止" not in html


def test_live_metric_strip_uses_backend_it_metrics_and_keeps_cv_separate() -> None:
    html = (GUI_DIR / "index.html").read_text(encoding="utf-8")
    app = (GUI_DIR / "app.js").read_text(encoding="utf-8")
    styles = (GUI_DIR / "styles.css").read_text(encoding="utf-8")
    strip = re.search(
        r'<section class="panel chart-panel">.*?'
        r'<div class="metric-strip">(.*?)</div>\s*'
        r'<div class="filter-panel"',
        html,
        re.S,
    )

    assert strip is not None
    assert strip.group(1).count("<div>") == 4
    for element_id in (
        "steadyCurrent", "steadySd", "metricTertiaryValue",
        "metricTrendState", "metricQuaternaryValue", "metricProgressDetail",
    ):
        assert html.count(f'id="{element_id}"') == 1
    assert "平稳仅代表斜率进入阈值，不代表自动停止全部门禁通过" in html
    it_renderer = _extract_js_function(app, "renderItMetricStrip")
    cv_renderer = _extract_js_function(app, "renderCvMetricStrip")
    for field in (
        "data.rolling_metrics", "steady_current_nA", "noise_nA",
        "slope_nA_per_s", "trend_state", "native_point_count",
        "progress_percent", "data.stability_eta?.display_text",
    ):
        assert field in it_renderer
    assert "data.data" not in it_renderer
    assert "consecutive_passes" not in it_renderer
    assert "required_consecutive_windows" not in it_renderer
    assert "adaptive?'预计稳定':'进度'" in it_renderer
    assert "个原生点" in it_renderer
    assert "'frozen'" in it_renderer
    assert "fmt(metrics.slope_nA_per_s,3)" in it_renderer
    assert "data.data" not in cv_renderer
    assert "data.rolling_metrics" in cv_renderer
    assert "metrics.native_point_count" in cv_renderer
    assert "metrics.expected_native_point_count" in cv_renderer
    assert "metrics.progress_percent" in cv_renderer
    assert "stability_eta" not in cv_renderer
    for state, symbol, label in (
        ("rising", "↑", "上升"), ("falling", "↓", "下降"),
        ("flat", "✓", "平稳"), ("insufficient", "·", "数据不足"),
    ):
        assert f"{state}:['{symbol}','{label}']" in app
    assert ".metric-trend.trend-rising,.metric-trend.trend-falling{color:var(--red)}" in styles
    assert ".metric-trend.trend-flat{color:var(--green-dark)}" in styles
    assert ".metric-trend.trend-insufficient{color:var(--muted)}" in styles


def test_metric_formatter_preserves_negative_zero_slope() -> None:
    values = _evaluate_chart_js(
        ["fmt"], "", "[fmt(-0, 3), fmt(-0.0001, 3), fmt(0, 3)]",
    )

    assert values == ["-0.000", "-0.000", "0.000"]


def test_adaptive_fit_window_stays_editable_and_drives_timing() -> None:
    app = (GUI_DIR / "app.js").read_text(encoding="utf-8")

    assert "$('fitWindowS').disabled=false" in app
    assert "$('fitWindowS').disabled=adaptive" not in app
    assert "adaptive?`I-T 智能平台检测与末 ${s.fit_window_s} 秒稳态分析`" in app
    assert "Math.max(plateauWindowDuration(plateau),Number(s.fit_window_s||0))" in app


def test_plateau_configuration_is_shared_by_two_collapsed_panels() -> None:
    html = (GUI_DIR / "index.html").read_text(encoding="utf-8")
    app = (GUI_DIR / "app.js").read_text(encoding="utf-8")
    keys = {
        "segment_duration_s", "segment_count", "absolute_tolerance_nA",
        "relative_tolerance", "scatter_multiplier", "minimum_coverage_ratio",
        "maximum_gap_periods", "required_consecutive_windows",
        "spike_scale_multiplier", "spike_neighbor_multiplier",
    }

    assert html.count("data-plateau-panel") == 2
    assert html.count("data-plateau-apply") == 2
    assert html.count("data-plateau-window") == 2
    for key in keys:
        assert html.count(f'data-plateau-key="{key}"') == 2, key
    assert not re.search(r"<details[^>]*data-plateau-panel[^>]*\bopen\b", html)
    assert "api('/api/plateau')" in app
    assert "post('/api/plateau/apply'" in app
    assert 'document.querySelectorAll(`[data-plateau-key="${input.dataset.plateauKey}"]`)' in app


def test_debug_chart_exposes_persisted_optional_layers_and_plateau_window() -> None:
    html = (GUI_DIR / "index.html").read_text(encoding="utf-8")
    app = (GUI_DIR / "app.js").read_text(encoding="utf-8")

    assert 'id="dbgShowPotential" type="checkbox" checked' in html
    assert 'id="dbgShowPlateau" type="checkbox" checked' in html
    assert "显示电位" in html
    assert "显示平台判定" in html
    assert 'data-window="plateau"' in html
    assert "debugShowPotential" in app
    assert "debugShowPlateau" in app
    assert "state.dbgShowPotential?[" in app
    assert "axis:'right'" in app


def test_debug_controls_become_read_only_during_a_formal_run() -> None:
    app = (GUI_DIR / "app.js").read_text(encoding="utf-8")

    assert "const readOnly = running && !d.debug_run;" in app
    assert "const readOnly = running && !d?.debug_run;" in app
    assert "正式测量中 · Debug 只读" in app
    assert "$(field.id).disabled = readOnly" in app
    assert "$('dbgStop').disabled = !running || stopping || readOnly" in app
    assert "!running || stopping || waiting || readOnly" in app
    assert "setPlateauFormalRunLock(readOnly)" in app
    assert "setPlateauFormalRunLock(running&&!Boolean(data.metadata?.debug))" in app
    assert "control.disabled=state.plateauBusy||state.plateauLockedByFormalRun" in app
    assert "if(state.plateauBusy||state.plateauLockedByFormalRun)return" in app
    assert "formalDebugPanel" not in app


def test_debug_actions_do_not_reuse_pre_action_snapshots() -> None:
    app = (GUI_DIR / "app.js").read_text(encoding="utf-8")

    assert "function invalidateDebugSnapshots()" in app
    assert "async function fetchDebugSnapshot(fresh=false)" in app
    assert app.count("fetchDebugSnapshot(true)") >= 3
    assert "let debugSessionStarted = Boolean(waiting && state.debug?.debug_run)" in app
    assert "if (debugSessionStarted)" in app
    assert "generation!==state.dbgSnapshotGeneration" in app


def test_debug_plateau_diagnostics_are_permanent_and_backend_driven() -> None:
    html = (GUI_DIR / "index.html").read_text(encoding="utf-8")
    app = (GUI_DIR / "app.js").read_text(encoding="utf-8")
    required_ids = {
        "dbgPlateauStatus", "dbgPlateauPasses", "dbgPlateauSlope",
        "dbgPlateauTrend", "dbgPlateauHalf", "dbgPlateauHalfMeans",
        "dbgPlateauScatter", "dbgPlateauSegments",
    }

    for element_id in required_ids:
        assert html.count(f'id="{element_id}"') == 1, element_id
    layers = _extract_js_function(app, "debugPlateauLayers")
    assert "evaluation.segment_means_nA" in layers
    assert "evaluation.segment_centres_s" in layers
    assert "evaluation.fit_intercept_nA" in layers
    assert "evaluation.first_half_mean_nA" in layers
    assert "evaluation.second_half_mean_nA" in layers
    assert "tolerance_nA" not in layers
    assert "stable=" not in layers
    assert "if(options.bands?.length)" in app


def test_debug_tail_stats_use_the_last_valid_points_only() -> None:
    result = _evaluate_chart_js(
        ["validTailStats"],
        "const mixed=validTailStats([1,2,999,4],[true,true,false,true],3);"
        "const even=validTailStats([2,4],[true,true],2);"
        "const empty=validTailStats([999],[false],20);",
        "{mixed,even,empty}",
    )

    assert result["mixed"]["median"] == 2
    assert abs(result["mixed"]["sd"] - (14 / 9) ** 0.5) < 1e-12
    assert result["mixed"]["count"] == 3
    assert result["even"]["median"] == 3
    assert result["empty"] is None


def test_debug_metric_strip_has_one_desktop_track_per_metric() -> None:
    html = (GUI_DIR / "index.html").read_text(encoding="utf-8")
    css = (GUI_DIR / "styles.css").read_text(encoding="utf-8")
    metric_strip = re.search(
        r'<section class="panel chart-panel dbg-chart">.*?'
        r'<div class="metric-strip">(.*?)</div>\s*'
        r'<div class="plateau-diagnostics"',
        html,
        re.S,
    )

    assert metric_strip is not None
    assert metric_strip.group(1).count("<div>") == 9
    assert ".dbg-chart .metric-strip{grid-template-columns:repeat(9,1fr)}" in css


def test_gui_javascript_passes_node_syntax_check() -> None:
    subprocess.run(
        ["node", "--check", str(GUI_DIR / "app.js")],
        check=True, capture_output=True, text=True,
    )


def test_flat_chart_bounds_expand_without_reassigning_constants() -> None:
    result = _evaluate_chart_js(
        ["finiteBounds", "paddedBounds"],
        "const flat=paddedBounds([[0,5],[1,5]]);const empty=paddedBounds([]);",
        "{flat,empty}",
    )

    assert result == {"flat": [3.76, 6.24], "empty": [0, 1]}


def test_measurement_chart_redraws_only_for_new_data_or_state() -> None:
    source = (GUI_DIR / "app.js").read_text(encoding="utf-8")
    functions = "\n".join(_extract_js_function(source, name) for name in (
        "measurementChartRevision", "scheduleMeasurementDraw",
    ))
    script = f"""
{functions}
const state={{measurementDrawRevision:null,measurementDrawPending:false}};
const frames=[];let draws=0;
const requestAnimationFrame=callback=>frames.push(callback);
const drawAll=()=>{{draws+=1}};
const first={{run_id:'run-1',state:'running',data:{{time_s:[.1],current_nA:[2]}}}};
scheduleMeasurementDraw(first);
scheduleMeasurementDraw(first);
const second={{run_id:'run-1',state:'running',data:{{time_s:[.1,.2],current_nA:[2,3]}}}};
scheduleMeasurementDraw(second);
const queued={{frames:frames.length,draws}};
frames.shift()();
scheduleMeasurementDraw(second);
const unchanged={{frames:frames.length,draws}};
scheduleMeasurementDraw({{...second,state:'completed'}});
const completed={{frames:frames.length,draws}};
console.log(JSON.stringify({{queued,unchanged,completed}}));
"""
    completed = subprocess.run(
        ["node", "-e", script], check=True, capture_output=True, text=True,
    )

    assert json.loads(completed.stdout) == {
        "queued": {"frames": 1, "draws": 0},
        "unchanged": {"frames": 0, "draws": 1},
        "completed": {"frames": 1, "draws": 1},
    }


def test_debug_overlay_controls_remain_clickable_in_empty_and_narrow_states() -> None:
    html = (GUI_DIR / "index.html").read_text(encoding="utf-8")
    css = (GUI_DIR / "styles.css").read_text(encoding="utf-8")

    assert html.count("20260817-v047") == 4
    assert ".empty-chart{position:absolute;inset:0;display:grid;place-items:center;color:#8a969a;font-size:12px;pointer-events:none}" in css
    assert ".chart-legend{position:absolute;z-index:2;" in css
    assert "@media(max-width:900px)" in css
    assert ".dbg-chart .panel-head{align-items:flex-start;flex-direction:column;gap:10px}" in css


def test_method_conditions_share_the_measurement_page_scrollbar() -> None:
    styles = (GUI_DIR / "styles.css").read_text(encoding="utf-8")

    assert ".control-content{height:auto;min-height:0;overflow:visible}" in styles
    assert (
        ".control-panel{display:grid;grid-template-rows:auto auto auto;"
        "align-self:stretch;overflow:visible}"
    ) in styles
    assert ".measure-control-panel{display:none;height:auto}" in styles
    assert "#view-measure .workspace-grid>.control-panel{height:390px" not in styles
    assert (
        "#view-measure.active{display:block;height:calc(100vh - 60px);"
        "min-height:0;overflow:auto;padding:10px 16px 82px}"
    ) in styles
    for nested_height in (
        "height:172px;min-height:172px",
        "height:300px;min-height:300px",
        "height:390px;min-height:390px",
    ):
        assert nested_height not in styles


def test_gui_uses_the_supplied_white_background_logo() -> None:
    html = (GUI_DIR / "index.html").read_text(encoding="utf-8")
    styles = (GUI_DIR / "styles.css").read_text(encoding="utf-8")
    logo_path = GUI_DIR / "sensus-logo.png"

    assert '<link rel="icon" type="image/png" href="/assets/sensus-logo.png' in html
    assert '<img class="brand-mark" src="/assets/sensus-logo.png' in html
    assert ".brand-mark{display:block;width:34px;height:34px;object-fit:contain;" in styles
    with Image.open(logo_path) as logo:
        assert logo.size == (1024, 1024)
        rgba = logo.convert("RGBA")
        assert rgba.getextrema()[3] == (255, 255)
        for corner in ((0, 0), (1023, 0), (0, 1023), (1023, 1023)):
            assert rgba.getpixel(corner) == (255, 255, 255, 255)


def test_plateau_input_bounds_match_backend_validation() -> None:
    html = (GUI_DIR / "index.html").read_text(encoding="utf-8")

    assert html.count("相对容差（1%=0.01）") == 2
    for key, minimum, maximum in (
        ("segment_duration_s", "0.5", "60"),
        ("segment_count", "2", "60"),
        ("absolute_tolerance_nA", "0", "1000"),
        ("relative_tolerance", "0", "1"),
        ("scatter_multiplier", "0", "100"),
        ("minimum_coverage_ratio", "0.01", "1"),
        ("maximum_gap_periods", "0.1", "100"),
        ("required_consecutive_windows", "1", "100"),
        ("spike_scale_multiplier", "0.1", "1000"),
        ("spike_neighbor_multiplier", "0.1", "1000"),
    ):
        pattern = rf'<input type="number" min="{minimum}" max="{maximum}"[^>]*data-plateau-key="{key}">'
        assert len(re.findall(pattern, html)) == 2, key


def test_post_routes_dispatch_to_existing_attributes() -> None:
    """POST 路由分派的 `APP.<method>(...)` 必须在 AppState 上真实存在。

    🔴 2026-08-19 回归钉子:新增 /api/calibration/validation/restore 时误写成
    `APP.calibration.restore_validation_point(...)`(邻居全是 `APP.xxx`),
    方法其实定义在 AppState 上 ⇒ 运行时 AttributeError ⇒ 该接口 500。
    单测直接调 app.restore_validation_point() 所以测不到 —— **路由层此前零覆盖**。

    只查一层 `APP.name(...)` 且 name 在类上是可调用属性;不解引用实例值
    (`APP.measurement.raw_log` 这类运行时才赋值的对象会误报)。
    """
    import ast

    from pa_host.gui_server import AppState

    source = (Path(__file__).resolve().parents[1] / "pa_host" / "gui_server.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    seen: set[str] = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "APP"):
            name = node.func.attr
            assert callable(getattr(AppState, name, None)), (
                f"gui_server.py 第 {node.lineno} 行调用了 APP.{name}(...),"
                f" 但 AppState 上没有这个可调用属性"
            )
            seen.add(name)
    # 关键路由必须在扫描范围内,否则这个测试等于没测
    for required in ("restore_validation_point", "delete_validation_point"):
        assert required in seen, f"路由里没找到 APP.{required}(...) 的调用"

def test_checkbox_reset_has_zero_specificity_and_precedes_component_rules() -> None:
    """全局 `input{width:100%;padding:9px 10px}` 会把复选框撑成大方块。

    2026-08-21 现场:「当前批次历史曲线」里复选框实测 **156px**,把文字挤到 103px
    并逐字换行(`.history-curve-option input{margin:0}` 只重置了 margin,漏了 width)。

    复位规则必须满足两条,否则会引入新的回归:
    1. 用 `:where()` 让特异度为 0 —— 否则 (0,1,1) 会盖掉
       `.point-selector{width:16px}` (0,1,0) 这类刻意设定的尺寸;
    2. 位置在全局 `input,select` **之后**(靠顺序赢它)、在组件规则**之前**
       (让组件规则靠特异度继续赢)。
    """
    css = (GUI_DIR / "styles.css").read_text(encoding="utf-8")
    reset = 'input:where([type="checkbox"],[type="radio"]){'
    assert reset in css, "缺少复选框复位规则(或被改成了带特异度的写法)"
    for prop in ("width:auto", "padding:0"):
        assert prop in css[css.index(reset):css.index(reset) + 220], f"复位规则缺 {prop}"
    global_rule = css.index("input,select{width:100%")
    assert css.index(reset) > global_rule, "复位规则必须排在全局 input,select 之后"
    # 真正要守的是"特异度为 0":选择器里不能出现 :where() 之外的属性/类选择器
    head = css[css.index(reset) - 0:css.index(reset) + len(reset)]
    assert head.count("[type=") == 2, "属性选择器必须全部包在 :where() 里"
    assert not head.startswith("."), "复位规则不能带类选择器"
    # 组件侧刻意设定的尺寸必须仍然存在(否则复选框会退回浏览器默认大小)
    for component in ('.point-selector{width:16px', '.checkbox-row input{width:16px'):
        assert component in css, f"{component} 被删了 —— 复位规则不负责恢复这些尺寸"


def test_history_curve_entries_carry_the_concentration_and_the_sample_name() -> None:
    """历史曲线条目必须同时给出样品名与浓度。

    2026-08-21 现场反馈:「历史曲线只有编号没有浓度」+「显示一下样品名称」。
    根因是样品名在实测里就是流水编号(归档索引里是 1/2/3/4),而浓度存在
    `known_concentration_um` 里却从没进过这个列表的 payload —— 于是同一批次
    四个标定点在界面上长得一模一样,只能靠时间戳分辨。
    """
    html = (GUI_DIR / "index.html").read_text(encoding="utf-8")
    css = (GUI_DIR / "styles.css").read_text(encoding="utf-8")
    rows = [
        {"sample_name": "1", "sample_role": "calibration",
         "known_concentration_um": 6.25, "finished_at": 0},
        {"sample_name": "3", "sample_role": "calibration",
         "known_concentration_um": 25.0, "finished_at": 0},
        {"sample_name": "未知样品 01", "sample_role": "test",
         "known_concentration_um": None, "finished_at": 0},
        {"sample_name": "", "run_id": "it_1", "sample_role": "cv",
         "known_concentration_um": "", "finished_at": 0},
        {"sample_name": "脏值", "sample_role": "test",
         "known_concentration_um": "abc", "finished_at": 0},
    ]
    app = (GUI_DIR / "app.js").read_text(encoding="utf-8")
    evaluated = _evaluate_chart_js(
        ["historyTimestamp", "concentrationLabel", "historyCurveTitle",
         "historyCurveDetail"],
        f"{_extract_js_const(app, 'HISTORY_CURVE_ROLES')}\nconst rows={json.dumps(rows)};",
        "rows.map(r=>[historyCurveTitle(r),historyCurveDetail(r).split(' · ')[0]])",
    )

    assert evaluated == [
        ["1 · 6.25 µM", "标定"],
        ["3 · 25 µM", "标定"],          # 25.0 不能显示成 "25.0 µM"
        ["未知样品 01", "测试"],          # 无浓度时只留名字,不留一个空的 " · "
        ["it_1", "CV"],                 # 名字为空才回落到 run_id
        ["脏值", "测试"],                # 脏值不能变成 "NaN µM"
    ]
    assert 'id="historyCurveLegend"' in html
    # 两行版式:标题(名+浓度)与副行(角色+时间)分开,否则一格 232px 装不下会重新挤在一起
    assert ".history-curve-option>span{display:grid" in css
    assert ".history-curve-option small{" in css


def test_history_curve_legend_rules_outrank_the_generic_chart_legend_rules() -> None:
    """图例规则必须带 `.chart-legend` 前缀,否则完全不生效。

    同文件里 `.chart-legend span{display:flex}` 与 `.chart-legend i{width:6px...}`
    都是 (0,1,1),裸类名 `.history-curve-legend` 只有 (0,1,0) —— **与顺序无关**地输。
    实测踩过:第一版写成裸类名,`display:contents` 被 `display:flex` 盖掉,
    整组图例被塞进一个 flex 项里。
    """
    css = (GUI_DIR / "styles.css").read_text(encoding="utf-8")

    assert ".chart-legend .history-curve-legend{display:contents}" in css
    assert ".chart-legend .history-curve-swatch{" in css
    for bare in re.finditer(r"(?<!\.chart-legend )\.history-curve-(legend|swatch)\{", css):
        raise AssertionError(f"图例规则缺 .chart-legend 前缀,赢不过 (0,1,1):{bare.group(0)}")
    # 叠加曲线可以选到 80 条,图例必须能换行并靠右收,否则会向左溢出画布
    legend_rule = css[css.index(".chart-legend{"):css.index(".chart-legend{") + 260]
    assert "flex-wrap:wrap" in legend_rule and "justify-content:flex-end" in legend_rule
    assert "max-width:calc(100% - 56px)" in legend_rule


def test_overlaid_history_curves_share_one_palette_with_their_legend() -> None:
    """线的颜色与图例色块必须取同一个数组同一个下标。

    图例指错曲线比没有图例更糟,而这在实现上只需要有人在 drawAll 里复制一份
    调色板字面量就会发生。同理,叠加集合只能算一次:两处各自 filter 会在
    "method 是 cv 但这一轮没有电位数据" 时选到不同的集合。
    """
    app = (GUI_DIR / "app.js").read_text(encoding="utf-8")
    draw = _extract_js_function(app, "drawAll")
    legend = _extract_js_function(app, "renderHistoryCurveLegend")

    assert "const HISTORY_CURVE_COLORS=[" in app
    assert "'#8b6f5a'" not in draw, "drawAll 里又出现了内联调色板字面量"
    pick = "HISTORY_CURVE_COLORS[index%HISTORY_CURVE_COLORS.length]"
    assert draw.count(pick) == 2, "IT/CV 两支都必须用共享调色板"
    assert pick in legend
    assert draw.count("state.historyCurves.filter(") == 1, "叠加集合只能算一次"
    assert "renderHistoryCurveLegend(overlays)" in draw

    # 调色板 6 色,第 7 条起会撞色 ⇒ 图例最多列 6 条,多出来的只报条数、不给色块
    palette = json.loads(
        _extract_js_const(app, "HISTORY_CURVE_COLORS")
        .split("=", 1)[1].rstrip(";").replace("'", '"')
    )
    rendered = _evaluate_chart_js(
        ["concentrationLabel", "historyCurveTitle", "renderHistoryCurveLegend"],
        _extract_js_const(app, "HISTORY_CURVE_COLORS") + """
const box={children:[],replaceChildren(){this.children=[]},appendChild(n){this.children.push(n)}};
const $=()=>box;
const document={createElement:()=>({className:'',style:{},parts:[],
  append(...n){this.parts.push(...n)},set textContent(v){this.parts.push(v)}}),
  createTextNode:value=>value};
renderHistoryCurveLegend(Array.from({length:9},(_,i)=>({sample_name:`s${i}`,known_concentration_um:i})));
""",
        "box.children.map(c=>c.parts.map(p=>typeof p==='string'?p:p.style.background))",
    )

    assert len(rendered) == len(palette) + 1, "每色一个色块 + 1 条溢出计数"
    assert rendered[0] == [palette[0], "s0 · 0 µM"]
    assert rendered[len(palette) - 1] == [
        palette[-1], f"s{len(palette) - 1} · {len(palette) - 1} µM",
    ]
    assert rendered[-1] == [f"+{9 - len(palette)} 条"]


def test_update_check_failure_is_visible_without_a_known_update() -> None:
    """更新**检查失败**时必须能在界面上看到。

    2026-08-21 现场:某台机器连续三天 156 次更新检查全部失败(冻结体缺 CA 证书),
    成功事件 0 条,而用户完全不知道 —— 因为旧代码把 error 也关在
    `data.available && (...)` 里,而检查失败时 available 必然是 false
    (根本没查到有没有新版),这一支永远走不到。
    """
    source = (GUI_DIR / "app.js").read_text(encoding="utf-8")
    body = _extract_js_function(source, "renderAppUpdate")
    visible = next(line for line in body.splitlines() if "const visible=" in line)
    assert "data?.state==='error'" in visible, "error 必须是独立的可见条件"
    error_pos = visible.index("data?.state==='error'")
    available_pos = visible.index("data?.available")
    assert error_pos > visible.index("||", available_pos), (
        "error 仍被关在 available 的与条件里 —— 检查失败时不可见"
    )


# ── 标定图逐点配色 ──────────────────────────────────────────────────────────────
# 每个点一个颜色、两族两个色系、颜色与表格行严格对应。三条会静默毁掉这个特性的失效模式:
#   ① 按 filter 后数组的下标取色 → 取消勾选/删点会让其余点整排换色;
#   ② 色值在"画图"和"表格色块"两处各写一份 → 迟早分叉,色块指错行比没色块更糟;
#   ③ 槽位用满后 `% palette.length` 循环复用 → 两个点同色,而人会确信自己找对了行。

# 🔴 实际使用的调色板。过检记录见下面 test_calibration_point_palettes_pass_the_dataviz_gates
_COOL_FAMILY = ["#0d7fae", "#1ab5a6", "#5b4289", "#8fa0f5", "#0e6936"]
_WARM_FAMILY = ["#a8368e", "#ee9420", "#8f3a1c", "#ef79aa", "#8b7b16"]

_POINT_COLOR_JS_NAMES = [
    "assignPointColorSlots", "pointColorPalette", "pointColor",
    "validationPointId", "refreshPointColorSwatches",
]


def _point_color_js_setup(app: str) -> str:
    """把配色相关的模块级常量原样注入 node,测的就是 app.js 里的真值。"""
    return "\n".join(
        _extract_js_const(app, name) for name in (
            "CALIBRATION_POINT_COLORS", "TEST_POINT_COLORS",
            "POINT_COLOR_OVERFLOW", "CALIBRATION_CURVE_COLOR", "POINT_COLOR_SLOTS",
        )
    )


_FAKE_POINTS_TABLE = """
// refreshPointColorSwatches 只碰这几样东西,照它需要的形状造个假表格
function fakeRow(pointId, selected){
  const swatch={style:{}}, box={classes:new Set(),
    classList:{toggle(name,on){on?box.classes.add(name):box.classes.delete(name)}}};
  return {dataset:{pointId}, swatch, box, selector:{checked:selected},
    querySelector(sel){return sel==='.point-color-swatch'?swatch
      :sel==='.point-number'?box:sel==='.point-selector'?this.selector:null}};
}
let TABLE=[];
const $=id=>({querySelectorAll:()=>TABLE});
const render=rows=>{TABLE=rows;refreshPointColorSwatches();
  return TABLE.map(tr=>[tr.dataset.pointId,tr.dataset.pointColor,tr.swatch.style.background,
    [...tr.box.classes]])};
"""


def test_calibration_point_palettes_pass_the_dataviz_gates() -> None:
    """两族调色板必须是过检值,并且两族不重叠、溢出灰不冒充第 6 个类别色。

    2026-08-21 用 dataviz 的 scripts/validate_palette.js 实跑,记录在此(脚本是技能的
    bundled 临时路径,会变,所以测试只钉 hex 与结论,不去调那个脚本):

      两族合并 10 色,`--mode light --surface "#ffffff"` → **ALL CHECKS PASS**(exit 0)
        Lightness band PASS · Chroma floor PASS
        CVD separation PASS(worst adjacent #a8368e↔#0e6936 ΔE 12.2 deutan)
        Normal-vision floor PASS(worst adjacent #1ab5a6↔#0d7fae ΔE 16.7)
        Contrast vs surface WARN/relief:#1ab5a6 2.56 / #8fa0f5 2.47 / #ee9420 2.36 /
          #ef79aa 2.63 —— relief 就是图正下方那两张带同色色块的表格
      每族再单独跑 `--pairs all`(散点该用的口径,不是默认的相邻对)→ 两族都 ALL CHECKS PASS
        冷族 worst all-pairs normal ΔE 16.7 / CVD 11.3;暖族 16.9 / 11.9(硬底线 15 / 6)

    走过的弯路(别再试):单色系做明暗阶梯过不了(彩度掉到下限以下读起来像灰,且相邻步
    normal ΔE 只有 8.5);从 dataviz 参考的 8 槽里挑 4 个暖色,怎么排都有一对相邻 ΔE<15
    (实测 #eb6834↔#e34948 只有 7.1 / CVD 5.6)。族内 all-pairs 的天花板就是 5 色 ——
    所以第 6 个点起走中性灰降级,不是"再挤一个颜色进来"。
    """
    app = (GUI_DIR / "app.js").read_text(encoding="utf-8")
    cool = json.loads(_extract_js_const(app, "CALIBRATION_POINT_COLORS")
                      .split("=", 1)[1].rstrip(";").replace("'", '"'))
    warm = json.loads(_extract_js_const(app, "TEST_POINT_COLORS")
                      .split("=", 1)[1].rstrip(";").replace("'", '"'))

    assert cool == _COOL_FAMILY, "冷族色值变了 —— 必须重跑 validate_palette.js 并更新过检记录"
    assert warm == _WARM_FAMILY, "暖族色值变了 —— 必须重跑 validate_palette.js 并更新过检记录"
    assert not set(cool) & set(warm), "两族色值重叠,一个点的颜色就不能反推它属于哪张表了"

    overflow = _extract_js_const(app, "POINT_COLOR_OVERFLOW").split("'")[1]
    assert overflow not in cool and overflow not in warm, "溢出灰不能是任一族的类别色"
    # 拟合曲线是模型不是数据类别:必须中性,不许落在暖族的色相弧上(原来的 #c77a18 就撞了)
    curve = _extract_js_const(app, "CALIBRATION_CURVE_COLOR").split("'")[1]
    assert curve not in cool and curve not in warm
    red, green, blue = (int(curve[i:i + 2], 16) for i in (1, 3, 5))
    assert max(red, green, blue) - min(red, green, blue) <= 24, f"拟合曲线 {curve} 不够中性"
    assert "'#c77a18'" not in _extract_js_function(app, "drawAll"), "拟合曲线仍是暖色 #c77a18"

    # AP 区域图那三个色是另一张图的,不许被这次改动带走
    ap = _extract_js_function(app, "drawApScoreChart")
    for kept in ("#28708c", "#c48720", "#b54455"):
        assert kept in ap, f"drawApScoreChart 丢了 {kept}"


def test_point_colour_never_recycles_a_slot_and_degrades_to_grey() -> None:
    """槽位用满后落中性灰,**绝不**循环复用颜色。

    `palette[i % palette.length]` 会让第 6 个点和第 1 个点同色 —— 人不会怀疑,
    只会照着颜色去看错的那一行。灰色是"这个点没有专属色,去看 # 序号"的显式信号。
    """
    app = (GUI_DIR / "app.js").read_text(encoding="utf-8")
    assert "%CALIBRATION_POINT_COLORS.length" not in app.replace(" ", "")
    assert "%TEST_POINT_COLORS.length" not in app.replace(" ", "")

    colours = _evaluate_chart_js(
        _POINT_COLOR_JS_NAMES,
        _point_color_js_setup(app) + _FAKE_POINTS_TABLE + """
const ids=Array.from({length:8},(_,i)=>`p${i}`);
assignPointColorSlots('calibration',ids);
assignPointColorSlots('test',ids);
""",
        "[ids.map(id=>pointColor('calibration',id)),ids.map(id=>pointColor('test',id)),"
        "POINT_COLOR_OVERFLOW]",
    )
    cool, warm, overflow = colours
    assert cool[:5] == _COOL_FAMILY
    assert warm[:5] == _WARM_FAMILY
    assert cool[5:] == [overflow] * 3, "第 6 个点起必须是中性灰,不是回头用第 1 个颜色"
    assert warm[5:] == [overflow] * 3
    # 未登记的 id 也必须落灰,不能返回 undefined 让 canvas 画出黑点
    assert _evaluate_chart_js(
        _POINT_COLOR_JS_NAMES,
        _point_color_js_setup(app) + _FAKE_POINTS_TABLE,
        "pointColor('calibration','never-seen')",
    ) == overflow


def test_point_colour_survives_deselecting_and_deleting_other_points() -> None:
    """🔴 稳定性:取消勾选或删掉一个点,**其余点的颜色一个都不许变**。

    这是整个特性的命门。按 `points.filter(p=>p.selected)` 之后的下标取色,
    取消勾选第 2 个点会让第 3、4、5 个点整排前移一格换色 —— 图上的点与表格行
    从此对不上,而界面看起来完全正常。
    """
    app = (GUI_DIR / "app.js").read_text(encoding="utf-8")
    baseline, after_deselect, after_delete, after_readd = _evaluate_chart_js(
        _POINT_COLOR_JS_NAMES,
        _point_color_js_setup(app) + _FAKE_POINTS_TABLE + """
const rows=['a','b','c','d','e'].map(id=>fakeRow(id,true));
const baseline=render(rows);
rows[1].selector.checked=false;             // 取消勾选第 2 个点
const afterDeselect=render(rows);
const afterDelete=render(rows.filter(tr=>tr.dataset.pointId!=='b'));   // 删掉第 2 个点
const afterReadd=render([...rows.filter(tr=>tr.dataset.pointId!=='b'),fakeRow('f',true)]);
""",
        "[baseline,afterDeselect,afterDelete,afterReadd]",
    )

    colour = {row[0]: row[1] for row in baseline}
    assert [colour[k] for k in "abcde"] == _COOL_FAMILY

    assert {row[0]: row[1] for row in after_deselect} == colour, "取消勾选换了别人的颜色"
    # 取消勾选只改色块的明暗(该点不画进图里),不改颜色
    assert [row[3] for row in after_deselect] == [[], ["is-unpicked"], [], [], []]

    assert {row[0]: row[1] for row in after_delete} == {
        k: colour[k] for k in "acde"
    }, "删掉一个点换了其余点的颜色 —— 说明颜色绑到了下标而不是 point_id"

    # 删掉的点把槽位还回来了:新点吃回那个槽位,而老点一个都没动
    readd = {row[0]: row[1] for row in after_readd}
    assert {k: readd[k] for k in "acde"} == {k: colour[k] for k in "acde"}
    assert readd["f"] == colour["b"], "槽位没回收 —— 只剩 5 个点时第 5 个点却吃了溢出灰"


def test_chart_point_colour_equals_its_table_row_swatch() -> None:
    """同一个点:图上的颜色 == 表格行色块的颜色。

    只靠一个来源保证 —— 两边都调 `pointColor(族, 稳定id)`。所以这里既钉行为
    (色块 / dataset.pointColor / pointColor() 三者一致),也钉源码
    (drawAll 不许自己写色值、不许按下标取)。
    """
    app = (GUI_DIR / "app.js").read_text(encoding="utf-8")
    draw = _extract_js_function(app, "drawAll")

    # 画图侧:颜色只能从 pointColor 取,键必须是稳定身份
    assert "color:pointColor('calibration',point.point_id)" in draw
    assert "color:pointColor('test',validationPointId(point,index))" in draw
    for banned in ("#28708c", "#b54455"):
        assert banned not in draw, f"drawAll 里又出现了内联点色 {banned}"
    # 🔴 反例:`(c.points||[]).filter(p=>p.selected)` 之后再 map 取色就是那个 bug
    assert ".filter(p=>p.selected)" not in draw
    assert "assignPointColorSlots('calibration',calibrationPoints.map(point=>point.point_id)" in draw

    # 表格侧:色块与 dataset.pointColor 都来自同一次 pointColor 调用
    swatches = _extract_js_function(app, "refreshPointColorSwatches")
    assert "pointColor('calibration',tr.dataset.pointId)" in swatches
    assert swatches.count("pointColor(") == 1, "色块颜色只能取一次,免得两处分叉"

    rendered = _evaluate_chart_js(
        _POINT_COLOR_JS_NAMES,
        _point_color_js_setup(app) + _FAKE_POINTS_TABLE + """
const rows=['a','b','c'].map(id=>fakeRow(id,true));
const table=render(rows);
// 画图侧对同一个 point_id 的取色(drawAll 里就是这一句)
const chart=rows.map(tr=>pointColor('calibration',tr.dataset.pointId));
""",
        "[table.map(r=>[r[1],r[2]]),chart]",
    )
    table, chart = rendered
    assert [row[0] for row in table] == chart, "表格色块与图上取到的颜色不是同一个"
    assert [row[1] for row in table] == chart, "写进 style.background 的不是同一个颜色"

    # 两张表都要有色块,且形状区分族属(deutan 下粉↔青 ΔE 只有 4.1,族属不能只靠色相)
    assert "colorSwatch.className='point-color-swatch'" in _extract_js_function(app, "row")
    validation = _extract_js_function(app, "renderValidation")
    assert "colorSwatch.className='point-color-swatch diamond'" in validation
    assert "marker:'diamond'" in draw


def test_calibration_legend_swatches_match_the_javascript_palettes() -> None:
    """图例的两条渐变色块必须逐色对上 JS 里的调色板。

    图例画的是"这一族有哪些颜色",一旦与 app.js 分叉,它就在骗人。
    """
    app = (GUI_DIR / "app.js").read_text(encoding="utf-8")
    css = (GUI_DIR / "styles.css").read_text(encoding="utf-8")

    for selector, family in (("calibration", _COOL_FAMILY), ("validation", _WARM_FAMILY)):
        rule = next(line for line in css.splitlines()
                    if line.startswith(f".legend-swatch.{selector}{{"))
        assert re.findall(r"#[0-9a-f]{6}", rule) == family, f"{selector} 图例与调色板分叉"
    fit = next(line for line in css.splitlines() if line.startswith(".legend-swatch.fit{"))
    assert _extract_js_const(app, "CALIBRATION_CURVE_COLOR").split("'")[1] in fit

    # 色块本体:圆点 / 菱形 + 未勾选压暗
    assert ".point-color-swatch{" in css
    assert ".point-color-swatch.diamond{" in css and "rotate(45deg)" in css
    assert ".point-number.is-unpicked .point-color-swatch{" in css


def test_shared_drawchart_marker_field_is_opt_in() -> None:
    """`marker` 必须是可选字段:不传就还是原来的圆点。

    drawChart 是 itChart / dbgChart / 标定图共用的,给标定图加菱形不能顺手改掉
    默认分支的调用参数 —— 那两张图的像素必须一个字节都不变。
    """
    app = (GUI_DIR / "app.js").read_text(encoding="utf-8")
    chart = _extract_js_function(app, "drawChart")

    assert "s.marker === 'diamond'" in chart
    assert "else ctx.arc(cx, cy, radius, 0, Math.PI * 2);" in chart, (
        "默认分支必须仍是同参数的 ctx.arc,否则 itChart 的圆点会变"
    )
    # pointRing 也必须是可选的:没传就一笔 stroke 都不许多画
    assert "if (s.pointRing) {" in chart

    # 只有标定图那两族 series 传 marker / pointRing;IT/CV/调试图的一个都不许带
    draw = _extract_js_function(app, "drawAll")
    assert draw.count("marker:") == 1, "marker 漏到了 IT/CV 的 series 上"
    assert draw.count("pointRing:") == 2, "pointRing 只该给标定点与测试点两族"
    debug = _extract_js_function(app, "drawDebug")
    assert "marker:" not in debug and "pointRing:" not in debug

    # 标定点先画、测试点后画 ⇒ 先画的那个必须更大,同浓度叠在一起时外圈才露得出来。
    # 2026-08-21 实测(Playwright 数 canvas 像素):最挤的三点重叠处,被压在最下面的
    # #8fa0f5 从 12px 提到 17px;11 个点色一个都没被完全吃掉。
    cal_radius = float(re.search(r"pointRadius:([\d.]+),pointRing", draw).group(1))
    test_radius = float(re.search(r"pointRadius:([\d.]+),marker:'diamond'", draw).group(1))
    assert cal_radius > test_radius, "后画的测试点更大 ⇒ 同浓度的标定点会被整块吃掉"
