import pytest

from pa_host.automation import AutomationError
from pa_host.guided_automation import GuidedAutomation, default_guided_experiment


def document(motion_only=True):
    result = default_guided_experiment()
    result.update(schema_version='guided-1.1', motion_only=motion_only,
                  execution={'mode': 'custom', 'count': 1, 'route': [4, 3]})
    for c in result['columns']:
        c.update(enabled=c['index'] in [3, 4], duration_s=.2,
                 windows={ch: {'start_s': 0, 'duration_s': 0} for ch in 'ABCDEFGH'})
    return result


def test_motion_check_uses_same_motion_guards_without_any_acquisition(tmp_path):
    service = GuidedAutomation(tmp_path)
    plan = service.compile(document(), mode='simulate')
    assert not any(s['resolved_params'].get('acquire') or s['op'] == 'dwell' for s in plan['steps'])
    assert [s['column'] for s in plan['steps'] if s['op'] == 'move_x'] == [4, 3]
    assert len([s for s in plan['steps'] if s['op'] == 'move_z']) == 6
    assert any('Motion-only' in w for w in plan['warnings'])
    # Selecting this mode grants no new authority to move uncalibrated hardware.
    with pytest.raises(AutomationError) as error:
        service.compile(document(), mode='live')
    assert error.value.code == 'calibration_required'


@pytest.mark.parametrize('value', [False, 'true', 1, None])
def test_motion_only_must_be_explicit_and_does_not_relax_normal_measurements(tmp_path, value):
    with pytest.raises(AutomationError):
        GuidedAutomation(tmp_path).compile(document(value), mode='simulate')


def test_long_finite_motion_plan_supports_500_cycles(tmp_path):
    d = document()
    d['execution'] = {'mode': 'repeat', 'count': 500, 'route': None}
    plan = GuidedAutomation(tmp_path).compile(d, mode='simulate')
    assert len(plan['steps']) == 6000
    assert plan['experiment']['motion_only'] is True
    assert not any(s['resolved_params'].get('acquire') for s in plan['steps'])
    from pa_host.experiment_progress import Progress
    progress = Progress(plan['steps'], {'X': 0, 'Z': 0}, plan['experiment']['execution'], started=0, motion_only=True)
    state = progress.snapshot('running', 0, None, 0)
    assert state['total_cycles'] == 500 and state['total_visits'] == 1000


def test_motion_flag_cannot_extend_a_plan_that_contains_measurement(tmp_path):
    from pa_host.automation import compile_experiment
    plan = GuidedAutomation(tmp_path).compile(document(), mode='simulate')
    experiment = plan['experiment']
    # The larger capacity is exclusive to plans without acquisition commands.
    experiment['program'][2] = {'op': 'dwell', 'seconds': 30, 'acquire': ['A']}
    with pytest.raises(AutomationError, match='cannot acquire'):
        compile_experiment(experiment)
