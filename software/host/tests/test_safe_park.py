import copy
import pytest

from pa_host.automation import AutomationError
from pa_host.safe_park import compile_safe_park
from test_guided_integration import guided_app


@pytest.mark.parametrize('column', [1, 7, 12])
def test_safe_park_keeps_real_calibration_and_never_immerses(tmp_path, monkeypatch, guided_app, column):
    from test_guided_run_entry import run_entry_session
    service, bus, serial, record, controller, factory, document = run_entry_session(tmp_path, monkeypatch, guided_app)
    before = copy.deepcopy(service.snapshot())
    plan = compile_safe_park(service, bus, column)
    assert [s['op'] for s in plan['steps']] == ['move_z', 'move_x']
    assert plan['steps'][0]['resolved_params']['target_mm'] == record['z_safe_mm']
    assert plan['steps'][1]['resolved_params']['target_mm'] == pytest.approx(record['x_first_mm']+(column-1)*record['column_pitch_mm'])
    assert plan['experiment']['motion_only'] is True
    assert plan['experiment']['calibration_id'] == record['id']
    assert not any(s['resolved_params'].get('acquire') for s in plan['steps'])
    assert service.snapshot()['document'] == before['document']
    assert not factory.children


@pytest.mark.parametrize('column', [0, 13, True, 1.5, '1', None])
def test_safe_park_rejects_invalid_columns(column):
    with pytest.raises(AutomationError):
        compile_safe_park(None, None, column)


def test_safe_park_still_requires_live_calibration(tmp_path):
    from pa_host.guided_automation import GuidedAutomation
    with pytest.raises(AutomationError) as e:
        compile_safe_park(GuidedAutomation(tmp_path), None, 1)
    assert e.value.code == 'calibration_required'


def test_safe_park_api_executes_only_lift_and_translation(tmp_path, monkeypatch, guided_app):
    from pa_host import gui_server
    from test_guided_run_entry import run_entry_session
    from test_guided_runner import wait_terminal
    service, bus, serial, record, controller, factory, document = run_entry_session(tmp_path, monkeypatch, guided_app)
    service.save(document)
    result = gui_server._automation_dispatch('POST', 'guided/park',
                                            {'column': 1, 'idempotency_key': 'park-once'}, {})
    controller._thread.join(10)
    final = wait_terminal(controller)
    assert final['run_id'] == result['run_id']
    assert final['state'] == 'completed' and final['stop_confirmed'] is True
    assert bus.snapshot()['position']['Z'] == pytest.approx(record['z_safe_mm'], abs=.005)
    assert bus.snapshot()['position']['X'] == pytest.approx(record['x_first_mm'], abs=.005)
    assert not factory.children


def test_safe_park_is_discoverable_with_exact_request_fields():
    from pa_host.automation import openapi_document
    api = openapi_document()
    schema = api['paths']['/api/automation/v1/guided/park']['post']['requestBody']['content']['application/json']['schema']
    assert schema['required'] == ['column', 'idempotency_key']
    assert schema['additionalProperties'] is False
    assert schema['properties']['column'] == {'type': 'integer', 'minimum': 1, 'maximum': 12}
