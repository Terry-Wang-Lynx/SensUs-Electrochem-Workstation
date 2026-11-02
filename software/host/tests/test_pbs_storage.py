import copy

import pytest

from pa_host.automation import AutomationError, compile_experiment
from pa_host.guided_automation import GuidedAutomation, default_guided_experiment
from test_guided_integration import guided_app


def document():
    d = default_guided_experiment()
    d.update(schema_version='guided-1.1', pbs_column=7,
             execution={'mode': 'repeat', 'count': 2, 'route': None})
    d['defaults']['settle_s'] = 0
    for col in d['columns']:
        col.update(enabled=col['index'] in (4,5,6), duration_s=1)
    return d


def test_return_once_after_whole_run_with_unselected_pbs_column(tmp_path):
    service = GuidedAutomation(tmp_path)
    d = document()
    service.save(d)
    plan = service.compile(d)
    storage = [s for s in plan['steps'] if s.get('pbs_storage')]
    regular = [s for s in plan['steps'] if not s.get('pbs_storage')]
    assert [s['column'] for s in regular if s['op']=='move_x'] == [4,5,6,4,5,6]
    assert [s['op'] for s in storage] == ['move_z','move_x','move_z']
    assert all(s['column']==7 and not s['resolved_params'].get('acquire') for s in storage)
    assert storage[0]['resolved_params']['purpose']=='safe_before_x'
    assert storage[-1]['resolved_params']['purpose']=='immerse'
    assert storage[-1]['visit_index']==6
    assert service.snapshot()['document']['pbs_column']==7
    assert GuidedAutomation(tmp_path).snapshot()['document']['pbs_column']==7
    assert next(c for c in plan['experiment']['columns'] if c['index']==7)['concentration']=={'value':0,'unit':'uM'}


@pytest.mark.parametrize('pbs', [True, 0, 13, 7.5, '7'])
def test_reject_invalid_pbs_column(tmp_path, pbs):
    d=document();d['pbs_column']=pbs
    with pytest.raises(AutomationError):GuidedAutomation(tmp_path).compile(d)


@pytest.mark.parametrize('damage', ['missing_lift','wrong_x','wrong_height','extra_acquisition'])
def test_rest_policy_does_not_waive_trajectory_validation(tmp_path, damage):
    exp=GuidedAutomation(tmp_path).compile(document())['experiment']
    if damage=='missing_lift':
        exp['program'][-2]={'op':'wait','seconds':0}
    elif damage=='wrong_x':
        exp['program'][-2]['column']=8
    elif damage=='wrong_height':
        exp['program'][-1]['z_immersed_mm']=5
    else:
        exp['program'].append({'op':'dwell','seconds':1,'acquire':['A']})
    with pytest.raises(AutomationError):compile_experiment(exp)


@pytest.mark.parametrize('interrupt', [None,'cancel','output_unknown'])
def test_supported_live_runner_parks_immersed_only_after_confirmed_board_stop(tmp_path, monkeypatch, guided_app, interrupt):
    from pa_host import gui_server
    from test_guided_run_entry import run_entry_session
    from test_guided_runner import wait_terminal
    service,bus,serial,record,controller,factory,d=run_entry_session(tmp_path,monkeypatch,guided_app)
    d.update(schema_version='guided-1.1',pbs_column=7,execution={'mode':'once','count':1,'route':None})
    controller._parked_callback=service.checkpoint_reference
    callback=controller._event
    marked=[]
    def event(kind,**fields):
        callback(kind,**fields)
        if kind=='step_started' and fields['step'].get('pbs_storage') and not marked:
            marked.append(len(serial.writes))
            if interrupt=='cancel':controller.stop()
            if interrupt=='output_unknown':controller._state['boards']['A'].update(output_state='unknown',stop_confirmed=False)
    controller._event=event
    plan=service.compile(d,mode='live',motion=bus)
    gui_server._automation_dispatch('POST','guided/run',{'document':d,'mode':'live','plan_hash':plan['plan_hash'],
        'idempotency_key':'pbs-'+str(interrupt)}, {})
    controller._thread.join(10)
    state=wait_terminal(controller)
    assert marked
    if interrupt:
        assert state['state'] in ('stopped','error')
        assert not state.get('pbs_storage')
        assert not any(f[1]==0xFD for f in serial.writes[marked[0]:])
    else:
        assert state['state']=='completed', state
        assert state['stop_confirmed']
        assert state['pbs_storage']=={'column':7,'immersed':True,'output_state':'stopped'}
        assert bus.snapshot()['position']['Z']==pytest.approx(record['z_working_mm'],abs=.005)
        assert bus.snapshot()['position']['X']==pytest.approx(record['x_first_mm']+6*record['column_pitch_mm'],abs=.005)
        assert service.snapshot()['calibration']['usable']
        assert len(factory.children)==2
