import copy
import time

import pytest

from pa_host.automation import AutomationController, AutomationError, compile_experiment
from pa_host.guided_automation import GuidedAutomation, default_guided_experiment


def document(count=200,duration=120):
    d=default_guided_experiment()
    d.update(schema_version='guided-1.1',endurance=True,pbs_column=7,
             execution={'mode':'custom','count':count,'route':[7,6,5,4,5,6]})
    for c in d['columns']:c.update(enabled=c['index'] in (4,5,6,7),duration_s=duration)
    return d


def test_200_pingpong_cycles_compile_without_extending_normal_limits(tmp_path):
    service=GuidedAutomation(tmp_path)
    d=document();plan=service.compile(d)
    dwells=[s for s in plan['steps'] if s['op']=='dwell']
    assert len(dwells)==1200
    assert [s['column'] for s in dwells]==[7,6,5,4,5,6]*200
    assert all(s['resolved_params']['seconds']==120 for s in dwells)
    assert all(s['resolved_params']['channel_masks']=={'A':15,'B':15} for s in dwells)
    assert len([s for s in plan['steps'] if s.get('pbs_storage')])==4
    assert dwells[-1]['cycle_index']==200
    changed=copy.deepcopy(d);changed['endurance']=False
    with pytest.raises(AutomationError):service.compile(changed)
    with pytest.raises(AutomationError):service.compile(d,mode='live')


@pytest.mark.parametrize('flag', [1,'true',None])
def test_endurance_flag_must_be_boolean(tmp_path,flag):
    d=document(1);d['endurance']=flag
    with pytest.raises(AutomationError):GuidedAutomation(tmp_path).compile(d)


def test_72_hour_cap_and_no_acquisition_motion_flag_are_preserved(tmp_path):
    d=document(200,3600)
    with pytest.raises(AutomationError,match='72'):
        GuidedAutomation(tmp_path).compile(d)
    d=document(1);plan=GuidedAutomation(tmp_path).compile(d)
    exp=plan['experiment'];exp['motion_only']=True
    with pytest.raises(AutomationError,match='cannot acquire'):compile_experiment(exp)


def test_simulated_custom_cycles_have_separate_visits_and_final_pbs(tmp_path):
    from test_guided_runner import wait_terminal
    plan=GuidedAutomation(tmp_path/'guided').compile(document(3,.125))
    controller=AutomationController(tmp_path/'state',tmp_path/'runs',tmp_path,simulation_scale=0)
    controller.start(plan['experiment'],'simulate',plan['plan_hash'],'endurance-simulated')
    controller._thread.join(15)
    s=wait_terminal(controller)
    assert s['state']=='completed',s
    assert s['progress']['completed_visits']==18 and s['progress']['total_cycles']==3
    assert s['pbs_storage']=={'column':7,'immersed':True,'output_state':'stopped'}
    assert 71*3600<controller._deadline-time.monotonic()<=72*3600


def test_user_stop_does_not_restart_or_return_to_pbs(tmp_path):
    from test_guided_runner import wait_terminal
    plan=GuidedAutomation(tmp_path/'guided').compile(document(5,.125))
    controller=AutomationController(tmp_path/'state',tmp_path/'runs',tmp_path,simulation_scale=.1)
    controller.start(plan['experiment'],'simulate',plan['plan_hash'],'endurance-user-stop')
    controller.stop()
    s=wait_terminal(controller)
    assert s['state']=='stopped' and s['stop_confirmed']
    assert not s.get('pbs_storage')
    worker=controller._thread;time.sleep(.05)
    assert controller._thread is worker and not worker.is_alive()


def test_custom_repeat_controls_preserve_the_endurance_count():
    from test_automation_gui import evaluate_browser
    result=evaluate_browser("""
(async()=>{HighThroughput.activate();await tick();
elements.get('htExecutionMode').value='custom';elements.get('htExecutionMode').dispatchEvent(new Event('change'));
input('htExecutionCount','200');input('htExecutionRoute','7,6,5,4,5,6');
await click('htApplyExecution');
console.log(JSON.stringify({writes:requests.filter(x=>x.route==='guided/experiment').map(x=>x.body),hidden:elements.get('htExecutionCountLabel').hidden}));})();
""",before_source="experiment.schema_version='guided-1.1';experiment.endurance=true;experiment.execution={mode:'custom',count:200,route:[7,6,5,4,5,6]};")
    assert result['writes'][-1]['document']['execution']=={'mode':'custom','count':200,'route':[7,6,5,4,5,6]}
    assert result['hidden'] is False
