import pytest

from test_automation_gui import evaluate_browser


@pytest.mark.parametrize("execution,caption,path", [
    ({"mode": "repeat", "count": 3, "route": None}, "重复实验", "1 → 2"),
    ({"mode": "custom", "count": 1, "route": [10, 3, 10]}, "自定义路径", "10 → 3 → 10"),
])
def test_saved_execution_summary_is_read_without_writes(execution, caption, path):
    import json
    result = evaluate_browser("""
(async()=>{HighThroughput.activate();await tick();console.log(JSON.stringify({status:elements.get('htExecutionStatus').textContent,
writes:requests.filter(item=>item.body!==null)}));})();
""", before_source="experiment.schema_version='guided-1.1';experiment.execution=" + json.dumps(execution) + ";")
    assert caption in result["status"]
    assert path in result["status"]
    assert f'{execution["count"]} 轮' in result["status"]
    assert result["writes"] == []


def test_execution_apply_tick_clears_when_editing_and_poll_preserves_draft():
    result = evaluate_browser("""
(async()=>{HighThroughput.activate();await tick();await click('htApplyExecution');
const applied=elements.get('htApplyExecution').textContent;
elements.get('htExecutionMode').value='repeat';elements.get('htExecutionMode').dispatchEvent(new Event('change'));
input('htExecutionCount',4);HighThroughput.activate();await tick();
console.log(JSON.stringify({applied,dirty:elements.get('htApplyExecution').textContent,
count:elements.get('htExecutionCount').value,status:elements.get('htExecutionStatus').textContent}));})();
""")
    assert "✓" in result["applied"]
    assert "✓" not in result["dirty"]
    assert result["count"] == "4"
    assert "待应用" in result["status"]


def test_execution_explicit_apply_preserves_durations_and_saves_full_document():
    result = evaluate_browser("""
(async()=>{HighThroughput.activate();await tick();
elements.get('htExecutionMode').value='custom';elements.get('htExecutionMode').dispatchEvent(new Event('change'));
input('htExecutionRoute','1,3,10,3');
await click('htApplyExecution');
console.log(JSON.stringify({writes:requests.filter(item=>item.route==='guided/experiment').map(item=>item.body),status:elements.get('htExecutionStatus').textContent}));})();
""", before_source="for(const column of experiment.columns)column.duration_s=5;")
    document = result["writes"][-1]["document"]
    assert document["execution"] == {"mode": "custom", "count": 1, "route": [1, 3, 10, 3]}
    assert document["schema_version"] == "guided-1.1"
    assert [column["index"] for column in document["columns"] if column["enabled"]] == [1, 3, 10]
    assert all(column["duration_s"] == 5 for column in document["columns"])
    assert "已保存" in result["status"]


@pytest.mark.parametrize("route", ["0", "1,,3", "1.3", "-1", "1e1", "1,13", ""])
def test_invalid_custom_path_never_saves(route):
    import json
    result = evaluate_browser("""
(async()=>{HighThroughput.activate();await tick();
elements.get('htExecutionMode').value='custom';elements.get('htExecutionMode').dispatchEvent(new Event('change'));
input('htExecutionRoute',ROUTE);await click('htApplyExecution');
console.log(JSON.stringify({writes:requests.filter(item=>item.body!==null),error:elements.get('htError').textContent}));})();
""".replace("ROUTE", json.dumps(route)))
    assert result["writes"] == []
    assert "1,3,10,12" in result["error"]


@pytest.mark.parametrize("route,expected", [("10", [10]), ("11", [11]), ("12", [12]), ("13579", [1, 3, 5, 7, 9]), ("1,1", [1, 1])])
def test_custom_path_single_column_and_compact_digits_are_explicit(route, expected):
    import json
    result = evaluate_browser("""
(async()=>{HighThroughput.activate();await tick();elements.get('htExecutionMode').value='custom';
elements.get('htExecutionMode').dispatchEvent(new Event('change'));input('htExecutionRoute',ROUTE);await click('htApplyExecution');
console.log(JSON.stringify({execution:requests.find(item=>item.route==='guided/experiment').body.document.execution,
 status:elements.get('htExecutionStatus').textContent}));})();
""".replace("ROUTE", json.dumps(route)))
    assert result["execution"]["route"] == expected
    assert " → ".join(map(str, expected)) in result["status"]


def test_read_only_legacy_load_does_not_upgrade_or_save():
    result = evaluate_browser("""
(async()=>{HighThroughput.activate();await tick();console.log(JSON.stringify({version:experiment.schema_version,
 writes:requests.filter(item=>item.body!==null),progress:elements.get('htTotalRoute').textContent}));})();
""")
    assert result == {"version": "guided-1.0", "writes": [], "progress": "暂无本次进度"}


def test_busy_apply_and_save_handlers_refuse_programmatic_clicks():
    result = evaluate_browser("""
(async()=>{HighThroughput.activate();await tick();await click('htApplyExecution');await click('htSave');
console.log(JSON.stringify({writes:requests.filter(item=>item.body!==null),disabled:elements.get('htExecutionMode').disabled,error:elements.get('htError').textContent}));})();
""", before_source="const oldFetch=fetch;globalThis.fetch=async(path,options)=>path.endsWith('/state')?{ok:true,json:async()=>({state:'running',busy:true,channels:[],events:[]})}:oldFetch(path,options);")
    assert result["writes"] == []
    assert result["disabled"] is True
    assert "占用设备" in result["error"]


@pytest.mark.parametrize("state,suspended,expected", [("running", False, "预计剩余 10 s"), ("paused", True, "预计剩余时间挂起"), ("error", True, "未完成"), ("stopped", False, "未完成")])
def test_progress_uses_run_snapshot_and_suspends_eta(state, suspended, expected):
    import json
    bootstrap = """
const oldFetch=fetch;globalThis.fetch=async(path,options)=>path.endsWith('/state')?{ok:true,json:async()=>({state:STATE,busy:false,channels:[],events:[],progress:{percent:25,cycle_index:2,total_cycles:3,path_index:2,path_length:4,route:[9,3,3,1],elapsed_s:30,remaining_s:10,suspended:SUSPENDED}})}:oldFetch(path,options);
""".replace("STATE", json.dumps(state)).replace("SUSPENDED", json.dumps(suspended))
    result = evaluate_browser("""
(async()=>{HighThroughput.activate();await tick();console.log(JSON.stringify({percent:elements.get('htTotalPercent').textContent,
 route:elements.get('htTotalRoute').textContent,timing:elements.get('htTotalTiming').textContent}));})();
""", before_source=bootstrap)
    assert result["percent"] == "25.0%"
    assert "轮次 2 / 3" in result["route"]
    assert "当前物理列 3" in result["route"]
    assert expected in result["timing"]


def test_failed_execution_save_blocks_run_and_generic_save():
    result = evaluate_browser("""
(async()=>{HighThroughput.activate();await tick();elements.get('htExecutionMode').value='repeat';
elements.get('htExecutionMode').dispatchEvent(new Event('change'));input('htExecutionCount',3);await click('htApplyExecution');
await click('htRun');await click('htSave');console.log(JSON.stringify({status:elements.get('htExecutionStatus').textContent,
runDisabled:elements.get('htRun').disabled,writes:requests.filter(item=>item.body!==null)}));})();
""", before_source="const oldFetch=fetch;globalThis.fetch=async(path,options)=>path.endsWith('/guided/experiment')?{ok:false,json:async()=>({error:{message:'保存失败'}})}:oldFetch(path,options);")
    assert "未应用保存" in result["status"]
    assert result["runDisabled"] is True
    assert result["writes"] == []


def test_execution_changed_during_save_stays_unapplied():
    result = evaluate_browser("""
(async()=>{HighThroughput.activate();await tick();elements.get('htExecutionMode').value='repeat';
elements.get('htExecutionMode').dispatchEvent(new Event('change'));input('htExecutionCount',3);
await click('htApplyExecution');input('htExecutionCount',4);releaseSave();await tick();await tick();
console.log(JSON.stringify({status:elements.get('htExecutionStatus').textContent,disabled:elements.get('htRun').disabled}));})();
""", before_source="""
let releaseSave;const saveGate=new Promise(resolve=>releaseSave=resolve);const oldFetch=fetch;
globalThis.fetch=async(path,options)=>{if(path.endsWith('/guided/experiment'))await saveGate;return oldFetch(path,options);};
""")
    assert "当前修改仍待应用" in result["status"]
    assert result["disabled"] is True


@pytest.mark.parametrize("state", ["running", "error", "stopped"])
@pytest.mark.parametrize("percent", [99.99, 100])
def test_unfinished_progress_never_rounds_to_one_hundred(state, percent):
    import json
    bootstrap = """
const oldFetch=fetch;globalThis.fetch=async(path,options)=>path.endsWith('/state')?{ok:true,json:async()=>({state:STATE,busy:false,channels:[],events:[],progress:{percent:PERCENT}})}:oldFetch(path,options);
""".replace("STATE", json.dumps(state)).replace("PERCENT", str(percent))
    result = evaluate_browser("""
(async()=>{HighThroughput.activate();await tick();console.log(JSON.stringify({label:elements.get('htTotalPercent').textContent,value:elements.get('htTotalProgress').value}));})();
""", before_source=bootstrap)
    assert float(result["label"].rstrip("%")) < 100
    assert float(result["value"]) <= 99.9


def test_completed_progress_can_display_one_hundred():
    result = evaluate_browser("""
(async()=>{HighThroughput.activate();await tick();console.log(JSON.stringify({label:elements.get('htTotalPercent').textContent,value:elements.get('htTotalProgress').value}));})();
""", before_source="const oldFetch=fetch;globalThis.fetch=async(path,options)=>path.endsWith('/state')?{ok:true,json:async()=>({state:'completed',busy:false,channels:[],events:[],progress:{percent:100}})}:oldFetch(path,options);")
    assert result["label"] == "100.0%"
    assert float(result["value"]) == 100
