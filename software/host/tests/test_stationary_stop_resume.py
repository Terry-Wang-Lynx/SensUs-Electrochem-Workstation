import copy

import pytest

from pa_host.emm_motion import MotionError
from test_powered_manual_driver import powered_bus


@pytest.mark.parametrize('damage', [None, 'moved', 'target', 'profile', 'power', 'another_stop'])
def test_stationary_stop_checkpoint_is_read_only_and_rejects_changed_state(damage):
    bus, serial, events = powered_bus(enabled=True)
    bus.confirm_reference()
    bus.enable('X')
    bus.enable('Z')
    candidate = bus.stationary_reference_candidate()
    assert candidate
    bus.stop_all()
    original_counts = copy.deepcopy(candidate['identity']['reference_counts'])
    if damage == 'moved':
        serial.positions[1] += 500
    elif damage == 'target':
        serial.targets[0x34][1] += 500
    elif damage == 'profile':
        candidate['identity']['profiles']['X']['firmware_version'] += 1
    elif damage == 'power':
        serial.statuses[1] |= 0x80
    elif damage == 'another_stop':
        read = bus._read_axis
        triggered = []
        def concurrent_stop(axis, **options):
            state = read(axis, **options)
            if not triggered:
                triggered.append(True)
                bus._stop_generation += 1
            return state
        bus._read_axis = concurrent_stop
    serial.writes.clear()
    try:
        if damage:
            with pytest.raises(MotionError):
                bus.verify_stationary_stop_checkpoint(candidate)
            assert not bus.snapshot()['referenced']
        else:
            checkpoint = bus.verify_stationary_stop_checkpoint(candidate)
            assert checkpoint['reference_counts'] == original_counts
            assert not bus.snapshot()['referenced']
            assert bus._stop_event.is_set()
            assert not any(f[1] in (0xFD, 0xFE, 0xF3) for f in serial.writes)
            bus.recover_stopped_reference(checkpoint, verified_position_window=True)
            assert bus.snapshot()['referenced']
            assert not bus._stop_event.is_set()
        assert not any(f[1] in (0xFD, 0xFE, 0xF3) for f in serial.writes)
    finally:
        serial.statuses[1] &= 0x7F
        bus.close()


def test_small_encoder_settling_uses_existing_drive_window_only():
    bus, serial, _ = powered_bus(enabled=True)
    bus.confirm_reference()
    bus.enable('X')
    bus.enable('Z')
    identity = bus.reference_identity()
    bus.stop_all()
    serial.positions[1] += 4
    try:
        bus.recover_stopped_reference(identity, verified_position_window=True)
        assert bus.snapshot()['axes']['X']['reference_count'] == identity['reference_counts']['X']
    finally:
        bus.close()


@pytest.mark.parametrize('damage', ['moving', 'active_axis', 'fault', 'unreferenced', 'stale', 'manual'])
def test_stop_never_captures_a_reference_without_fresh_idle_proof(damage):
    bus, serial, _ = powered_bus(enabled=True)
    bus.confirm_reference()
    bus.enable('X')
    bus.enable('Z')
    if damage == 'moving':
        bus._state['X']['speed_rpm'] = 1
    elif damage == 'active_axis':
        bus._automatic_motion_axis = 'X'
    elif damage == 'fault':
        bus._faulted = True
    elif damage == 'unreferenced':
        bus._state['X']['referenced'] = False
    elif damage == 'stale':
        bus._state['X']['updated_monotonic'] -= 3
    else:
        bus._manual['active'] = True
    try:
        assert bus.stationary_reference_candidate() is None
    finally:
        bus._manual['active'] = False
        bus.close()


def test_completed_stop_then_new_live_preview_reuses_four_points(tmp_path, monkeypatch):
    from test_automatic_arrival_settling import natural_controller
    from test_guided_runner import wait_terminal
    controller, bus, serial, record, document, compiled, _ = natural_controller(tmp_path, monkeypatch)
    service = serial.guided_service
    controller._stopped_callback = service.checkpoint_stationary_stop
    controller.start(document, 'live', compiled['plan_hash'], 'before-stationary-stop')
    assert wait_terminal(controller)['state'] == 'completed'
    serial.trace_started = False  # This case has no FE-induced mechanical creep.
    controller.stop()
    stopped = wait_terminal(controller)
    assert stopped['state'] == 'stopped' and stopped['stop_confirmed']
    saved = service.snapshot()['calibration']
    assert saved['usable']
    assert saved['id'] == record['id']
    assert saved['reference']['reference_counts'] == record['reference']['reference_counts']
    serial.writes.clear()
    trusted = service.trusted_calibration(bus)
    assert trusted['id'] == record['id']
    assert bus.snapshot()['referenced']
    assert not any(f[1] in (0xFD, 0xFE, 0xF3) for f in serial.writes)


def test_missing_board_is_rejected_before_position_commands(tmp_path):
    from pa_host.automation import AutomationError, compile_experiment
    from test_automation_limits import controller_fixture, ready
    controller, document, serial, _ = controller_fixture(tmp_path, strict=False)
    ready(controller)
    def absent(addresses):
        raise AutomationError('board_unavailable', 'No verified INFO/STATUS')
    controller._board_preflight = absent
    document['program'] = [{'op': 'move_to_column', 'column': 1}, {'op': 'dwell', 'seconds': 10, 'acquire': ['A']}]
    plan = compile_experiment(document)
    serial.writes.clear()
    with pytest.raises(AutomationError) as caught:
        controller.start(document, 'live', plan['plan_hash'], 'missing-board-preflight')
    assert caught.value.code == 'board_unavailable'
    assert not any(frame[1] in (0xFD, 0xFE, 0xF3) for frame in serial.writes)


def test_pause_monitor_and_http_snapshots_share_one_serial_reader(tmp_path, monkeypatch):
    import threading
    import time
    from pa_host.automation import compile_experiment
    from test_automatic_arrival_settling import natural_controller
    from test_guided_runner import wait_terminal
    controller, bus, serial, _, document, _, _ = natural_controller(tmp_path, monkeypatch)
    document['program'] = [{'op': 'move_to_column', 'column': 1}, {'op': 'withdraw'},
                           {'op': 'move_to_column', 'column': 1}]
    plan = compile_experiment(document)
    controller.start(document, 'live', plan['plan_hash'], 'pause-reader-lock-order')
    controller.pause()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and controller.snapshot()['state'] == 'running':
        time.sleep(.02)
    assert controller.snapshot()['state'] == 'paused'
    serial.writes.clear()
    observed = []
    def reader():
        for _ in range(10):
            observed.append(controller.snapshot()['state'])
            time.sleep(.02)
    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    thread.join(3)
    assert not thread.is_alive(), 'paused monitor blocked snapshot requests'
    assert observed == ['paused'] * 10
    assert controller._limit_failure is None
    assert not any(frame[1] in (0xFD, 0xFE, 0xF3) for frame in serial.writes)
    controller.resume()
    assert wait_terminal(controller)['state'] == 'completed'
