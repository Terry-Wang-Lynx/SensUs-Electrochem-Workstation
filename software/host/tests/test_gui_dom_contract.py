import json
import re
import subprocess
from pathlib import Path


GUI_DIR = Path(__file__).parents[1] / "pa_host" / "gui"


def _extract_js_function(source: str, name: str) -> str:
    start = source.index(f"function {name}(")
    if source[max(0, start - 6):start] == "async ":
        start -= 6
    opening = source.index("{", start)
    depth = 0
    for offset in range(opening, len(source)):
        if source[offset] == "{":
            depth += 1
        elif source[offset] == "}":
            depth -= 1
            if depth == 0:
                return source[start:offset + 1]
    raise AssertionError(f"Unterminated JavaScript function: {name}")


def _evaluate_chart_js(names: list[str], setup: str, result: str) -> object:
    source = (GUI_DIR / "app.js").read_text(encoding="utf-8")
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

    assert 'id="deviceTransport"' in html
    assert 'id="exitApp"' in html
    assert "transport_label" in app
    assert "/api/shutdown" in app
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
  devices: {devices: [{id: 'j1', name: 'J-Link SN 1', selectable: true}], probing: false},
};
renderHardwareConnection(); const connected = capture();
state.devices = {devices: [], probing: false};
renderHardwareConnection(); const disconnected = capture();
state.devices = {devices: [
  {id: 'j1', name: 'J-Link SN 1', selectable: true},
  {id: 'u1', name: 'USB Board 1', selectable: true},
], probing: false};
renderHardwareConnection(); const multiple = capture();
""",
        "{connected,disconnected,multiple}",
    )

    assert states == {
        "connected": {
            "dot": "status-dot ok",
            "title": "硬件已连接",
            "detail": "J-Link SN 1 · MAX30131",
        },
        "disconnected": {
            "dot": "status-dot",
            "title": "硬件未连接",
            "detail": "未发现 USB DATA 或 J-Link · MAX30131",
        },
        "multiple": {
            "dot": "status-dot warning",
            "title": "已发现 2 个硬件",
            "detail": "请在右上角选择本次使用的设备 · MAX30131",
        },
    }


def test_settings_apply_has_long_timeout_and_reload_safe_progress_polling() -> None:
    app = (GUI_DIR / "app.js").read_text(encoding="utf-8")

    assert "SETTINGS_APPLY_TIMEOUT_MS = 900000" in app
    assert "timeoutMs:SETTINGS_APPLY_TIMEOUT_MS" in app
    assert "function ensureSettingsProgressPolling()" in app
    assert "state.settings?.state!=='applying'" in app
    assert "api('/api/settings',{timeoutMs:3000})" in app
    assert "$('applySettings').disabled=applying" in app
    assert "if(applying)ensureSettingsProgressPolling()" in app


def test_measurement_start_validates_inputs_keeps_errors_and_waits_for_gate() -> None:
    html = (GUI_DIR / "index.html").read_text(encoding="utf-8")
    app = (GUI_DIR / "app.js").read_text(encoding="utf-8")

    assert 'id="sampleName" placeholder="例如：未知样品 01" required' in html
    assert 'id="knownConcentration" type="number" min="0" step="any"' in html
    assert 'placeholder="请输入标定浓度" required' in html
    assert "MEASUREMENT_START_TIMEOUT_MS = 45000" in app
    assert "timeoutMs:MEASUREMENT_START_TIMEOUT_MS" in app
    assert "function measurementInputIssue()" in app
    assert "function clearMeasurementInputState()" in app
    assert "else if(!state.measureRequestError)$('measureError').hidden=true" in app
    assert 'input[aria-invalid="true"]' in (GUI_DIR / "styles.css").read_text(encoding="utf-8")


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
    assert "function confirmAndSwitchWorkspace(path)" in app
    assert "window.confirm('确认切换工作区？')" in app
    assert "$('saveDirectory').addEventListener('blur'" in app
    assert "event.key!=='Enter'" in app
    assert "batch_name:''" in app
    assert "function workspaceReady()" in app
    assert "!workspaceReady()" in app
    assert "workspace_available" in app
    assert "save_dir:$('saveDirectory').value" not in app


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


def test_debug_overlay_controls_remain_clickable_in_empty_and_narrow_states() -> None:
    html = (GUI_DIR / "index.html").read_text(encoding="utf-8")
    css = (GUI_DIR / "styles.css").read_text(encoding="utf-8")

    assert html.count("20260817-workspace-picker") == 2
    assert ".empty-chart{position:absolute;inset:0;display:grid;place-items:center;color:#8a969a;font-size:12px;pointer-events:none}" in css
    assert ".chart-legend{position:absolute;z-index:2;" in css
    assert "@media(max-width:900px)" in css
    assert ".dbg-chart .panel-head{align-items:flex-start;flex-direction:column;gap:10px}" in css


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
