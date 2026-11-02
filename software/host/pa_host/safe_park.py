"""A calibrated lift-and-translate plan, without immersion or acquisition."""
import copy

from .automation import AutomationError, compile_experiment


def compile_safe_park(service, motion, column):
    if type(column) is not int or not 1 <= column <= 12:
        raise AutomationError('invalid_value', 'Parking column must be an integer from 1 to 12', 'column')
    document = copy.deepcopy(service.snapshot()['document'])
    document.update(schema_version='guided-1.1', name=f'抬升并停在第{column}列上方（不下降、不采集）',
                    motion_only=True, endurance=False, pbs_column=None,
                    execution={'mode': 'custom', 'count': 1, 'route': [column]})
    document['defaults'].update(dwell_s=.1, settle_s=0)
    for item in document['columns']:
        item.update(enabled=item['index'] == column, duration_s=.1, windows={})
    # Compile through the existing current-session calibration check first.
    experiment = service.compile(document, mode='live', motion=motion)['experiment']
    experiment['program'] = [{'op': 'move_to_column', 'column': column}]
    experiment.pop('execution', None)
    result = compile_experiment(experiment)
    assert [s['op'] for s in result['steps']] == ['move_z', 'move_x']
    assert result['steps'][0]['resolved_params']['purpose'] == 'safe_before_x'
    assert not any(s['resolved_params'].get('acquire') for s in result['steps'])
    return result
