import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from pa_host.automation import AutomationError
from pa_host.gui_server import AppState


@pytest.mark.parametrize('damage', [None, 'unverified', 'running', 'fault', 'io', 'cancelled'])
def test_preflight_requires_verified_idle_boards_without_output_commands(damage):
    result = {'protocol_verified': damage != 'unverified',
              'firmware': {'status': {'mask': 1 if damage == 'running' else 0,
                                     'fault': 1 if damage == 'fault' else 0,
                                     'io': 1 if damage == 'io' else 0}}}
    app = AppState.__new__(AppState)
    app.automation = SimpleNamespace(_stop_event=threading.Event())
    app.ble_connections = SimpleNamespace(connect=Mock(return_value=result))
    if damage == 'cancelled':
        app.automation._stop_event.set()
    if damage:
        with pytest.raises(AutomationError):
            app._preflight_automation_boards(['board-A', 'board-B'])
    else:
        app._preflight_automation_boards(['board-A', 'board-B'])
        assert app.ble_connections.connect.call_count == 2
    if damage == 'cancelled':
        app.ble_connections.connect.assert_not_called()
