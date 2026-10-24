import pytest

from test_automation_gui import evaluate_browser


BOOTSTRAP = """
Object.assign(experiment.defaults,{ascend_speed_mm_s:1,descend_speed_mm_s:.5,translate_speed_mm_s:2});
experiment.columns[0].duration_s=5;experiment.columns[0].windows={A:{start_s:1,duration_s:3}};
for(const column of experiment.columns.slice(1))column.duration_s=180;
let saveMode='success',releaseSave=null,serverBoards={A:{address:'server-a',settings:{}},B:{address:'server-b',settings:{}}};
const originalFetch=globalThis.fetch;
globalThis.fetch=async(path,options)=>{
 const route=path.replace('/api/automation/v1/','');
 if(route==='guided')return {ok:true,json:async()=>({document:{...experiment,bindings:{...experiment.bindings,boards:serverBoards}},calibration:null,manual:{active:false}})};
 if(route!=='guided/experiment')return originalFetch(path,options);
 const body=JSON.parse(options.body);requests.push({route,body});
 if(saveMode==='pending')await new Promise(resolve=>{releaseSave=resolve;});
 if(saveMode==='failure')throw new Error('保存失败：磁盘不可用');
 return {ok:true,json:async()=>saveMode==='malformed'?{}:{document:body.document}};
};
const feedback=()=>({button:elements.get('htApplyDefaults').textContent,disabled:elements.get('htApplyDefaults').disabled,
 status:elements.get('htDefaultsApplyStatus')?.textContent||'',className:elements.get('htApplyDefaults').className,
 runDisabled:elements.get('htRun').disabled,value:elements.get('htDefault-ascend_speed_mm_s').value});
"""


def run(script):
    return evaluate_browser("(async()=>{HighThroughput.activate();await tick();" + script + "})();", before_source=BOOTSTRAP)


def test_applying_defaults_persists_and_reports_actual_values():
    result = run("""
input('htDefault-ascend_speed_mm_s',1.5);input('htDefault-translate_speed_mm_s',0.7);
await click('htApplyDefaults');
console.log(JSON.stringify({...feedback(),saved:requests.findLast(request=>request.route==='guided/experiment')?.body.document}));
""")
    assert result["saved"]["defaults"]["ascend_speed_mm_s"] == 1.5
    assert result["saved"]["bindings"]["boards"]["A"]["address"] == "server-a"
    assert result["button"] == "✓ 已应用"
    assert "已保存，下一次实验生效" in result["status"]
    assert "单列覆盖优先" in result["status"]
    assert result["saved"]["columns"][0]["duration_s"] == 5
    assert result["saved"]["columns"][0]["windows"] == {"A": {"start_s": 1, "duration_s": 3}}
    assert all(column["duration_s"] == 180 and column["windows"] == {} for column in result["saved"]["columns"][1:])
    assert "1.5" in result["status"] and "0.7" in result["status"]
    assert "is-applied" in result["className"]


def test_pending_apply_is_disabled_and_edits_prevent_stale_success():
    result = run("""
saveMode='pending';input('htDefault-ascend_speed_mm_s',1.5);await click('htApplyDefaults');
const pending=feedback();input('htDefault-ascend_speed_mm_s',1.7);releaseSave();await tick();await tick();
console.log(JSON.stringify({pending,after:feedback(),saved:requests.findLast(request=>request.route==='guided/experiment').body.document}));
""")
    assert result["pending"]["disabled"] is True
    assert "应用中" in result["pending"]["button"]
    assert result["after"]["value"] == "1.7"
    assert "待应用" in result["after"]["button"]
    assert "is-applied" not in result["after"]["className"]
    assert result["after"]["runDisabled"] is True
    assert result["saved"]["defaults"]["ascend_speed_mm_s"] == 1.5


@pytest.mark.parametrize("mode", ["failure", "malformed"])
def test_failed_apply_keeps_draft_and_blocks_run(mode):
    result = run(f"""
saveMode='{mode}';input('htDefault-ascend_speed_mm_s',1.5);await click('htApplyDefaults');
console.log(JSON.stringify(feedback()));
""")
    assert "失败" in result["button"]
    assert "is-applied" not in result["className"]
    assert result["value"] == "1.5" and result["runDisabled"] is True


def test_edit_after_success_resets_applied_feedback():
    result = run("""
await click('htApplyDefaults');input('htDefault-ascend_speed_mm_s',1.8);
console.log(JSON.stringify(feedback()));
""")
    assert "待应用" in result["button"]
    assert "is-applied" not in result["className"]
    assert result["runDisabled"] is True


def test_leaving_page_during_save_never_marks_late_response_applied():
    result = run("""
saveMode='pending';input('htDefault-ascend_speed_mm_s',1.5);await click('htApplyDefaults');
HighThroughput.deactivate();releaseSave();await tick();await tick();console.log(JSON.stringify(feedback()));
""")
    assert "待应用" in result["button"]
    assert "is-applied" not in result["className"]
    assert result["runDisabled"] is True


def test_default_duration_in_success_summary_and_dirty_revision():
    result = run("""
input('htDefaultDuration',240);await click('htApplyDefaults');const saved=feedback();
input('htDefaultDuration',250);console.log(JSON.stringify({saved,dirty:feedback()}));
""")
    assert "240" in result["saved"]["status"] and result["saved"]["button"] == "✓ 已应用"
    assert "待应用" in result["dirty"]["button"]


def test_invalid_default_duration_cannot_claim_old_duration_applied():
    result = run("""
input('htDefaultDuration','bad');await click('htApplyDefaults');console.log(JSON.stringify({...feedback(),saves:requests.filter(request=>request.route==='guided/experiment').length}));
""")
    assert "失败" in result["button"]
    assert result["saves"] == 0 and result["runDisabled"] is True
