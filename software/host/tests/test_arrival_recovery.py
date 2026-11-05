import copy
import hashlib
import json
import time

import pytest

from pa_host.arrival_recovery import arrival_checkpoint
from pa_host.emm_motion import MotionError
from test_automatic_arrival_settling import arrival_bus
from test_emm_motion import signed
from test_powered_manual_driver import powered_bus


def test_first_stopped_sample_near_travel_deadline_can_finish_confirmation(tmp_path, monkeypatch):
    bus, serial, record = arrival_bus(tmp_path, monkeypatch)
    read = bus._read_axis
    delayed = []
    def late_first_arrival(axis, **options):
        if axis == 'X' and serial.trace_index == 3 and not delayed:
            delayed.append(True)
            time.sleep(max(0, options['deadline'] - time.monotonic() - .02))
        return read(axis, **options)
    monkeypatch.setattr(bus, '_read_axis', late_first_arrival)
    result = bus.move_to('X', record['x_first_mm'], 20)
    assert delayed
    assert result['axes']['X']['speed_rpm'] == 0
    assert serial.events[-1]['kind'] == 'arrived'
    assert not any(frame[1] == 0xFE for frame in serial.writes)


def test_final_readback_gets_fresh_budget_after_natural_completion(tmp_path, monkeypatch):
    bus, serial, record = arrival_bus(tmp_path, monkeypatch)
    settle = bus._settle_automatic_axis
    def late_settle(*args, **kwargs):
        result = settle(*args, **kwargs)
        # Replay the observed failure: completed trajectory, almost no time left
        # for the following identity/profile query. No motion is replayed.
        time.sleep(max(0, kwargs['deadline'] - time.monotonic() - 0.01))
        return result
    monkeypatch.setattr(bus, '_settle_automatic_axis', late_settle)
    result = bus.move_to('X', record['x_first_mm'], 20)
    assert result['axes']['X']['speed_rpm'] == 0
    assert serial.events[-1]['kind'] == 'arrived'
    assert not any(frame[1] == 0xFE for frame in serial.writes)


def test_explicit_motion_deadline_is_not_extended():
    bus, serial, _ = powered_bus(enabled=True)
    try:
        bus._move_timeout = 0.5
        assert bus._arrival_verification_deadline(123.0) == 123.0
        bus._move_timeout = None
        assert bus._arrival_verification_deadline(time.monotonic(), 123.0) == 123.0
    finally:
        bus.close()


@pytest.mark.parametrize('damage', [None, 'encoder', 'target', 'profile', 'stop'])
def test_recover_reference_is_read_only_and_rejects_changed_identity(damage):
    bus, serial, _ = powered_bus(enabled=True)
    bus.confirm_reference()
    identity = bus.reference_identity()
    bus.stop_all()
    if damage == 'encoder':
        serial.positions[1] += 20
    elif damage == 'target':
        serial.targets[0x34][1] += 300
    elif damage == 'profile':
        identity['profiles']['X']['firmware_version'] += 1
    elif damage == 'stop':
        bus._last_stop['stop_confirmed'] = False
    serial.writes.clear()
    try:
        if damage:
            with pytest.raises(MotionError):
                bus.recover_stopped_reference(identity)
            assert not bus.snapshot()['referenced']
        else:
            state = bus.recover_stopped_reference(identity)
            assert state['referenced']
            assert all(s['enable_confirmed'] for s in state['axes'].values())
        assert not any(f[1] in (0xFD, 0xFE, 0xF3, 0xF6, 0x0A, 0x9A) for f in serial.writes)
    finally:
        bus.close()


def evidence(tmp_path):
    config = {'address': 1, 'lead_mm': 2, 'positive_direction': 0}
    record = {'motion': {'axes': {'X': config, 'Z': {**config, 'address': 2}}},
              'x_first_mm': 1, 'x_last_mm': 12, 'z_safe_mm': 1, 'z_working_mm': 20,
              'reference': {'reference_counts': {'X': 0, 'Z': 0}, 'raw_counts': {'X': 0, 'Z': 32768},
              'profiles': {a: {'config': {'position_window_deg': 0.8}} for a in ('X', 'Z')}}}
    events = [{'event': 'step_started', 'step': {'op': 'move_x', 'resolved_params': {'axis': 'X', 'target_mm': 3}}},
              {'event': 'motion_wire', 'detail': {'kind': 'finite_motion_settled', 'axis': 'X',
               'basis': 'natural_finite_completion', 'target_signature': [98304]}}]
    for address, raw in ((1, 98304), (2, 32768)):
        frame = (bytes([address, 0x43, 31, 9]) + b'\0' * 6 + signed(raw, 4)
                 + signed(0, 2) + signed(raw, 4) + signed(0, 4) + bytes([3, 3, 0x6B]))
        assert len(frame) == 31
        events.append({'event': 'motion_wire', 'detail': {'kind': 'wire', 'direction': 'rx',
                       'opcode': 0x43, 'frame_hex': frame.hex()}})
    events.append({'event': 'motion_wire', 'detail': {'kind': 'stopped', 'stop_confirmed': True}})
    (tmp_path / 'events.jsonl').write_text('\n'.join(json.dumps(e) for e in events), encoding='utf-8')
    (tmp_path / 'motion-calibration.json').write_text(json.dumps(record), encoding='utf-8')
    (tmp_path / 'plan.json').write_text('{}', encoding='utf-8')
    manifest = {'mode': 'live', 'synthetic': False, 'state': 'error', 'stop_confirmed': True,
                'error': {'cause': {'code': 'move_timeout'}}, 'boards': {},
                'files': {n: hashlib.sha256((tmp_path / n).read_bytes()).hexdigest() for n in
                          ('events.jsonl', 'motion-calibration.json', 'plan.json')}}
    (tmp_path / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
    return manifest


@pytest.mark.parametrize('damage', [None, 'hash', 'stop', 'cause', 'acquired'])
def test_checkpoint_requires_intact_finalized_arrival_proof(tmp_path, damage):
    manifest = evidence(tmp_path)
    if damage == 'hash':
        (tmp_path / 'events.jsonl').write_text('changed', encoding='utf-8')
    elif damage == 'stop':
        manifest['stop_confirmed'] = False
    elif damage == 'cause':
        manifest['error']['cause']['code'] = 'axis_fault'
    elif damage == 'acquired':
        manifest['boards'] = {'A': {}}
    (tmp_path / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
    if damage:
        with pytest.raises(ValueError):
            arrival_checkpoint(tmp_path)
    else:
        _, checkpoint = arrival_checkpoint(tmp_path)
        assert checkpoint['raw_counts'] == {'X': 98304, 'Z': 32768}


def rewrite_manifest(tmp_path, manifest):
    manifest['files'] = {n: hashlib.sha256((tmp_path / n).read_bytes()).hexdigest()
                         for n in manifest['files']}
    (tmp_path / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')


@pytest.mark.parametrize('damage', [None, 'missing_axis', 'changed_target', 'moving', 'not_dwell', 'later_move', 'hard_fault'])
def test_telemetry_dwell_stop_requires_exact_arrived_targets(tmp_path, damage):
    manifest = evidence(tmp_path)
    manifest['error'] = {'code': 'invalid_telemetry'}
    events = [json.loads(line) for line in (tmp_path / 'events.jsonl').read_text().splitlines()]
    arrivals = [{'event': 'motion_wire', 'detail': {'kind': 'arrived', 'axis': a, 'target_mm': t}}
                for a, t in [('X', 3), ('Z', 1)]]
    if damage == 'missing_axis':
        arrivals.pop()
    if damage == 'changed_target':
        arrivals[0]['detail']['target_mm'] = 4
    if damage == 'moving':
        frame = bytearray.fromhex(events[2]['detail']['frame_hex'])
        frame[15:18] = signed(1, 2)
        events[2]['detail']['frame_hex'] = frame.hex()
    if damage == 'hard_fault':
        manifest['error']['cause'] = {'code': 'axis_fault'}
    events[2:2] = arrivals
    if damage == 'later_move':
        events.append({'event': 'step_started', 'step': {'op': 'move_x', 'resolved_params': {'axis': 'X', 'target_mm': 3}}})
    events.append({'event': 'step_started', 'step': {'op': 'wait' if damage == 'not_dwell' else 'dwell'}})
    # STOP telemetry follows the dwell; the last STOP is required independently.
    events.extend(events[2+len(arrivals):4+len(arrivals)])
    events.append({'event': 'motion_wire', 'detail': {'kind': 'stopped', 'stop_confirmed': True}})
    (tmp_path / 'events.jsonl').write_text('\n'.join(json.dumps(e) for e in events))
    rewrite_manifest(tmp_path, manifest)
    if damage:
        with pytest.raises(ValueError):
            arrival_checkpoint(tmp_path)
    else:
        _, checkpoint = arrival_checkpoint(tmp_path)
        assert checkpoint['raw_counts'] == {'X': 98304, 'Z': 32768}


@pytest.mark.parametrize('damage', [None, 'outside', 'moving', 'unlocked', 'drive_fault', 'missing_axis'])
@pytest.mark.parametrize('cause', ['move_timeout', 'transaction_timeout', 'cancelled', 'calibration_motion'])
def test_timeout_before_arrival_preserves_known_in_window_stop_coordinates(tmp_path, damage, cause):
    manifest = evidence(tmp_path)
    manifest['error']['cause']['code'] = cause
    events = [json.loads(line) for line in (tmp_path / 'events.jsonl').read_text().splitlines()]
    events = [e for e in events if e.get('detail', {}).get('kind') != 'finite_motion_settled']
    frame = bytearray.fromhex(events[1]['detail']['frame_hex'])
    # Travel was interrupted before target 3 mm, at 2 mm. Recovery restores
    # that checkpoint and original origin, never claims the move completed.
    frame[18:23] = signed(65536, 4)
    if damage == 'outside':
        frame[18:23] = signed(20 * 32768, 4)
    elif damage == 'moving':
        frame[15:18] = signed(1, 2)
    elif damage == 'unlocked':
        frame[29] = 2
    elif damage == 'drive_fault':
        frame[29] |= 4
    events[1]['detail']['frame_hex'] = frame.hex()
    if damage == 'missing_axis':
        events.pop(2)
    (tmp_path / 'events.jsonl').write_text('\n'.join(json.dumps(e) for e in events))
    rewrite_manifest(tmp_path, manifest)
    if damage:
        with pytest.raises((ValueError, KeyError)):
            arrival_checkpoint(tmp_path)
    else:
        _, checkpoint = arrival_checkpoint(tmp_path)
        assert checkpoint['raw_counts'] == {'X': 65536, 'Z': 32768}
        assert checkpoint['reference_counts'] == {'X': 0, 'Z': 0}


@pytest.mark.parametrize('state', ['completed', 'stopped'])
def test_verified_natural_park_can_be_recovered_without_repeating_four_points(tmp_path, state):
    manifest = evidence(tmp_path)
    record = json.loads((tmp_path / 'motion-calibration.json').read_text())
    record['reference']['motion'] = record['motion']
    (tmp_path / 'motion-calibration.json').write_text(json.dumps(record))
    checkpoint = copy.deepcopy(record['reference'])
    checkpoint['raw_counts'] = {'X': 98304, 'Z': 32768}
    with (tmp_path / 'events.jsonl').open('a') as handle:
        handle.write('\n' + json.dumps({'event': 'motion_naturally_parked',
            'proof': {'basis': 'natural_finite_completion', 'reference_identity': checkpoint}}))
    manifest.update(state=state, error=None, boards={'A': {'stop_confirmed': True, 'output_state': 'stopped'}})
    rewrite_manifest(tmp_path, manifest)
    _, restored = arrival_checkpoint(tmp_path)
    assert restored == checkpoint


def test_descent_arrival_timeout_uses_its_own_target_and_held_axis(tmp_path):
    manifest = evidence(tmp_path)
    events = [json.loads(line) for line in (tmp_path / 'events.jsonl').read_text().splitlines()]
    events[0]['step'] = {'op': 'move_z', 'resolved_params': {'axis': 'Z', 'target_mm': 1}}
    events[1]['detail'].update(axis='Z', target_signature=[32768])
    # X was already aligned before this descent started.
    record = json.loads((tmp_path / 'motion-calibration.json').read_text())
    record['reference']['raw_counts']['X'] = 98304
    (tmp_path / 'motion-calibration.json').write_text(json.dumps(record))
    (tmp_path / 'events.jsonl').write_text('\n'.join(json.dumps(e) for e in events))
    rewrite_manifest(tmp_path, manifest)
    _, checkpoint = arrival_checkpoint(tmp_path)
    assert checkpoint['raw_counts'] == {'X': 98304, 'Z': 32768}


def test_interrupted_run_recovered_to_stopped_reuses_verified_axis_arrivals(tmp_path):
    manifest = evidence(tmp_path)
    events = [json.loads(line) for line in (tmp_path / 'events.jsonl').read_text().splitlines()]
    events.extend([{'event': 'motion_wire', 'detail': {'kind': 'arrived', 'axis': axis, 'target_mm': value}}
                   for axis, value in [('X', 3), ('Z', 1)]])
    (tmp_path / 'events.jsonl').write_text('\n'.join(json.dumps(e) for e in events))
    manifest.update(state='stopped', error=None)
    rewrite_manifest(tmp_path, manifest)
    _, checkpoint = arrival_checkpoint(tmp_path)
    assert checkpoint['raw_counts'] == {'X': 98304, 'Z': 32768}


def test_relative_move_after_natural_completion_tolerates_stationary_target_window_offset(tmp_path, monkeypatch):
    bus, serial, record = arrival_bus(tmp_path, monkeypatch)
    state = copy.deepcopy(bus.snapshot()['axes']['Z'])
    state['reference_count'] = -675736
    state['raw_count'] = -642962
    # Replay 20261102T034335-e3399b93e5: Phase 1 settled naturally at z=2.0 mm with a
    # 13-count servo offset (-610213 raw vs -610200 target), so Phase 2 to z=1.0 mm
    # (1599 pulses = 32747.52 counts) produced quantized_target -> expected = -642960.52
    # while the drive accumulated relative to its prior target (-610200 - 32748 = -642948).
    quantized_target = (-610213 - (-675736)) * (2.0 / 65536) - 1599 * (2.0 / 3200)
    state['input_target_count'] = -642948
    state['position_error_count'] = state['input_target_count'] - state['raw_count']
    assert bus._automatic_target_observation('Z', state, quantized_target, record) == (-642948,)
    # A target shifted beyond position_window + representation_budget must still fault.
    state['input_target_count'] = -642700
    state['raw_count'] = -642700
    state['position_error_count'] = 0
    with pytest.raises(MotionError) as caught:
        bus._automatic_target_observation('Z', state, quantized_target, record)
    assert caught.value.code == 'calibration_motion'


@pytest.mark.parametrize("error,accepted", [
    ({"code": "calibration_motion", "message": "X feedback is unavailable"}, True),
    ({"code": "axis_fault"}, False),
    ({"code": "calibration_motion", "cause": {"code": "axis_fault"}}, False),
])
@pytest.mark.parametrize("damage", [None, "stop", "hash"])
def test_direct_guided_error_uses_same_finalized_stop_evidence(tmp_path, error, accepted, damage):
    manifest = evidence(tmp_path)
    manifest["error"] = error
    if damage == "stop":
        manifest["stop_confirmed"] = False
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    if damage == "hash":
        (tmp_path / "events.jsonl").write_text("changed", encoding="utf-8")
    if accepted and damage is None:
        _, checkpoint = arrival_checkpoint(tmp_path)
        assert checkpoint["raw_counts"] == {"X": 98304, "Z": 32768}
    else:
        with pytest.raises(ValueError):
            arrival_checkpoint(tmp_path)


@pytest.mark.parametrize("always_expire", [False, True])
def test_refresh_reobserves_aged_axis_without_weakening_freshness(monkeypatch, always_expire):
    bus, serial, _ = powered_bus(enabled=True)
    read = bus._read_axis
    visits = []
    def delayed_second_axis(axis, **kwargs):
        result = read(axis, **kwargs)
        visits.append(axis)
        if axis == "Z" and (always_expire or visits.count("Z") == 1):
            bus._state["X"]["updated_monotonic"] = time.monotonic() - 3
        if always_expire and axis == "X" and visits.count("X") > 1:
            bus._state["Z"]["updated_monotonic"] = time.monotonic() - 3
        return result
    monkeypatch.setattr(bus, "_read_axis", delayed_second_axis)
    serial.writes.clear()
    try:
        if always_expire:
            with pytest.raises(MotionError, match="fresh feedback"):
                bus.refresh()
        else:
            result = bus.refresh()
            assert all(a["telemetry_valid"] for a in result["axes"].values())
            assert visits == ["X", "Z", "X"]
        assert not any(f[1] in (0xFD, 0xFE, 0xF3, 0xF6, 0x0A, 0x9A) for f in serial.writes)
    finally:
        bus.close()


@pytest.mark.parametrize("drift_counts,accepted", [(8, True), (15, False)])
def test_install_experiment_calibration_allows_submicron_holding_settle_within_stability_window(
        tmp_path, monkeypatch, drift_counts, accepted):
    from test_powered_manual_driver import calibrated_powered_bus

    bus, serial, _, record = calibrated_powered_bus(tmp_path)
    original_read_manual_target = bus._read_manual_target
    shifted = []

    def settle_during_target_read(axis, opcode, **kwargs):
        if axis == "X" and opcode == 0x33 and not shifted:
            shifted.append(True)
            serial.positions[1] += drift_counts
        return original_read_manual_target(axis, opcode, **kwargs)

    monkeypatch.setattr(bus, "_read_manual_target", settle_during_target_read)
    try:
        if accepted:
            installed = bus.install_experiment_calibration(record)
            assert installed["experiment_calibration"]["id"] == record["id"]
            assert bus.snapshot()["faulted"] is False
        else:
            with pytest.raises(MotionError) as caught:
                bus.install_experiment_calibration(record)
            assert caught.value.code == "calibration_motion"
    finally:
        bus.close()


