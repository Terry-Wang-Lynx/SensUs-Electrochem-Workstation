import copy,hashlib,json,time
from pathlib import Path
import pytest
from pa_host.automation import AutomationController,AutomationError,default_experiment,compile_experiment,_hash

def controller(tmp_path):
    def forbidden(*a,**kw):pytest.fail('Historical replay must not touch hardware')
    return AutomationController(tmp_path/'state',tmp_path/'runs',tmp_path,motion_factory=forbidden,process_factory=forbidden)

def historical(c,key='old',size=2000):
    doc=default_experiment();compiled=compile_experiment(doc)
    result={'run_id':'finished-'+key,'state':'completed','stop_confirmed':True,'busy':False,
      'channels':[{'id':ch,'time_s':list(range(size)),'current_nA':[-.2*i for i in range(size)]} for ch in 'ABCDEFGH'],
      'error':None,'boards':{'A':{'output_state':'stopped'}},'manifest_path':'original/manifest.json'}
    c._idempotency[key]={'identity':_hash({'mode':'simulate','plan_hash':compiled['plan_hash']}),'run_id':result['run_id'],'result':copy.deepcopy(result)}
    return doc,compiled,result

def test_history_replay_roundtrips_exactly_without_launch_after_restart(tmp_path):
    c=controller(tmp_path);doc,plan,result=historical(c);c._persist()
    restarted=controller(tmp_path)
    assert restarted.start(doc,'simulate',plan['plan_hash'],'old')==result
    assert not (tmp_path/'runs').exists()
    with pytest.raises(AutomationError) as exc:
        restarted.start(doc,'live',plan['plan_hash'],'old')
    assert exc.value.code=='idempotency_conflict'

def test_checkpoint_does_not_grow_with_historical_curves_or_rewrite_payloads(tmp_path):
    c=controller(tmp_path)
    for i in range(10):historical(c,str(i))
    c._persist();path=tmp_path/'state/automation.json'
    assert path.stat().st_size<40000
    sidecars=list((path.parent/'idempotency-results').glob('*.json'))
    assert len(sidecars)==10 and sum(p.stat().st_size for p in sidecars)>500000
    times={p:p.stat().st_mtime_ns for p in sidecars}
    c._persist()
    assert {p:p.stat().st_mtime_ns for p in sidecars}==times
    assert all('result' not in e for e in json.loads(path.read_text())['idempotency'].values())

@pytest.mark.parametrize('damage',['missing','corrupt','traversal'])
def test_missing_or_changed_replay_evidence_fails_closed(tmp_path,damage):
    c=controller(tmp_path);doc,plan,_=historical(c);c._persist()
    ref=c._idempotency['old']['result_ref'];path=tmp_path/'state/idempotency-results'/(ref+'.json')
    if damage=='missing':path.unlink()
    if damage=='corrupt':path.write_text('{}')
    if damage=='traversal':c._idempotency['old']['result_ref']='../other'
    with pytest.raises(AutomationError) as exc:c.start(doc,'simulate',plan['plan_hash'],'old')
    assert exc.value.code=='idempotency_evidence'
    assert not (tmp_path/'runs').exists()

def test_legacy_inline_results_migrate_without_changing_response(tmp_path):
    c=controller(tmp_path);doc,plan,result=historical(c)
    path=tmp_path/'state/automation.json';path.parent.mkdir(parents=True,exist_ok=True)
    legacy={'experiment':doc,'state':{'stop_confirmed':True},'idempotency':c._idempotency}
    path.write_text(json.dumps(legacy),'utf-8')
    restarted=controller(tmp_path)
    assert restarted.start(doc,'simulate',plan['plan_hash'],'old')==result
    restarted._persist()
    assert controller(tmp_path).start(doc,'simulate',plan['plan_hash'],'old')==result

def test_same_run_replay_uses_fresh_state_and_updated_final_result_is_retained(tmp_path):
    c=controller(tmp_path);doc,plan,result=historical(c);c._persist()
    c._state.update(run_id=result['run_id'],state='stopped',stop_confirmed=True)
    assert c.start(doc,'simulate',plan['plan_hash'],'old')['state']=='stopped'
    result['state']='stopped';c._idempotency['old']['result']=result;c._persist()
    assert controller(tmp_path).start(doc,'simulate',plan['plan_hash'],'old')['state']=='stopped'
