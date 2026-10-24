from __future__ import annotations

import copy
import json
import threading
import time

import pytest

from pa_host.automation import AutomationController, default_experiment
from pa_host.emm_motion import SharedEmmBus
from test_emm_motion import FakeSerial


def connected_controller(tmp_path, interval=0):
    opened = []

    def factory(port, baud, axes, **options):
        serial = FakeSerial(timeout=0.05, write_timeout=0.05)
        serial.statuses = {1: 3, 2: 3}
        opened.append(serial)
        return SharedEmmBus(port, baud, axes, serial_factory=lambda **kwargs: serial,
                            transaction_timeout_s=0.05, **options)

    controller = AutomationController(tmp_path / "state", tmp_path / "runs", tmp_path,
                                      motion_factory=factory, motion_feedback_interval_s=interval)
    motion = default_experiment()["bindings"]["motion"]
    motion["port"] = "fake-feedback"
    controller.connect_motion(motion)
    return controller, opened, motion


def wait_for(predicate):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    assert predicate()


def test_cached_devices_never_open_or_refresh_and_library_worker_defaults_off(tmp_path):
    def forbidden(*args, **kwargs):
        pytest.fail("A cached status request attempted to open hardware")

    controller = AutomationController(tmp_path / "unused", tmp_path / "runs", tmp_path,
                                      motion_factory=forbidden)
    assert controller.device_status()["motion"]["connected"] is False
    assert controller.snapshot()["motion"]["connected"] is False
    controller, opened, motion = connected_controller(tmp_path)
    try:
        opened[0].writes.clear()
        for _ in range(3):
            controller.device_status()
            controller.snapshot()
        time.sleep(0.025)
        assert opened[0].writes == []
        assert controller.device_status()["motion"]["port"] == motion["port"]
    finally:
        controller.disconnect_motion()


def test_explicit_refresh_is_only_six_reads_and_preserves_profiles_and_hand_motion(tmp_path):
    controller, opened, _ = connected_controller(tmp_path)
    try:
        before = controller.snapshot()["motion"]
        opened[0].writes.clear()
        opened[0].positions[1] = -5
        opened[0].positions[2] = 1
        refreshed = controller.refresh_motion()["motion"]
        assert [frame.hex() for frame in opened[0].writes] == [
            "01366b", "01356b", "013a6b", "02366b", "02356b", "023a6b"]
        assert refreshed["axes"]["X"]["raw_count"] == -5
        assert refreshed["axes"]["Z"]["raw_count"] == 1
        assert refreshed["connection_token"] == before["connection_token"]
        assert refreshed["faulted"] is False
        assert refreshed["feedback_error"] is None
        for axis in ("X", "Z"):
            assert refreshed["axes"][axis]["config"] == before["axes"][axis]["config"]
            assert refreshed["axes"][axis]["mechanical"] == before["axes"][axis]["mechanical"]
            assert refreshed["axes"][axis]["referenced"] is False
            assert refreshed["axes"][axis]["enabled"] is True
            assert 0 <= refreshed["axes"][axis]["feedback_age_s"] < 0.5
        refreshed["axes"]["X"]["config"]["microstep"] = 255
        assert controller.device_status()["motion"]["axes"]["X"]["config"]["microstep"] == 16
    finally:
        controller.disconnect_motion()


def test_feedback_age_uses_monotonic_clock_and_cached_staleness(tmp_path, monkeypatch):
    controller, opened, _ = connected_controller(tmp_path)
    try:
        recorded = controller.snapshot()["motion"]["axes"]["X"]["updated_monotonic"]
        opened[0].writes.clear()
        with monkeypatch.context() as patch:
            patch.setattr(time, "monotonic", lambda: recorded + 3)
            patch.setattr(time, "time", lambda: 0)
            status = controller.device_status()["motion"]
        assert status["axes"]["X"]["feedback_age_s"] == pytest.approx(3)
        assert status["axes"]["X"]["telemetry_valid"] is False
        assert status["connected"] is True
        assert opened[0].writes == []
    finally:
        controller.disconnect_motion()


def test_feedback_failure_retains_raw_timestamp_and_specific_error_until_reconnect(tmp_path):
    controller, opened, motion = connected_controller(tmp_path)
    try:
        before = controller.snapshot()["motion"]["axes"]["X"]
        opened[0].overrides[1, 0x36] = bytes.fromhex("013602000000016b")
        status = controller.refresh_motion()["motion"]
        assert status["feedback_error"]["code"] == "invalid_telemetry"
        assert status["faulted"] is True
        assert status["axes"]["X"]["raw_count"] == before["raw_count"]
        assert status["axes"]["X"]["updated_monotonic"] == before["updated_monotonic"]
        assert status["axes"]["Z"]["telemetry_valid"] is False
        assert len(opened) == 1
        opened[0].overrides.clear()
        controller.disconnect_motion()
        controller.connect_motion(motion)
        assert len(opened) == 2
        assert controller.device_status()["motion"]["feedback_error"] is None
    finally:
        controller.disconnect_motion()


def test_worker_fault_stops_polling_without_reopening_or_refreshing_failed_sample(tmp_path):
    controller, opened, _ = connected_controller(tmp_path, interval=0.01)
    try:
        before = controller.snapshot()["motion"]["axes"]["X"]
        opened[0].overrides[1, 0x36] = bytes.fromhex("013602000000016b")
        wait_for(lambda: controller.device_status()["motion"]["feedback_error"] is not None)
        worker = controller._motion_feedback_thread
        worker.join(1)
        assert not worker.is_alive()
        writes = len(opened[0].writes)
        time.sleep(0.03)
        status = controller.device_status()["motion"]
        assert len(opened) == 1
        assert len(opened[0].writes) == writes
        assert status["feedback_error"]["code"] == "invalid_telemetry"
        assert status["axes"]["X"]["updated_monotonic"] == before["updated_monotonic"]
        assert status["axes"]["X"]["telemetry_valid"] is False
    finally:
        opened[0].overrides.clear()
        controller.disconnect_motion()


def test_hand_motion_does_not_invalidate_confirmed_reference_or_connection(tmp_path):
    controller, opened, _ = connected_controller(tmp_path)
    try:
        reference = controller.reference_motion(True)["motion"]
        opened[0].positions[1] += 32768
        opened[0].writes.clear()
        status = controller.refresh_motion()["motion"]
        assert status["referenced"] is True
        assert status["axes"]["X"]["reference_count"] == 65536
        assert status["axes"]["X"]["position_mm"] == 1
        assert status["connection_token"] == reference["connection_token"]
        assert {frame[1] for frame in opened[0].writes} == {0x36, 0x35, 0x3A}
    finally:
        controller.disconnect_motion()


def test_refresh_skips_live_motion_ownership_and_driver_busy_without_fault(tmp_path):
    controller, opened, _ = connected_controller(tmp_path)
    hold = threading.Event()
    owner = threading.Thread(target=hold.wait, daemon=True)
    owner.start()
    try:
        controller._thread = owner
        controller._owns_motion = True
        controller._state["mode"] = "live"
        opened[0].writes.clear()
        controller.refresh_motion()
        assert opened[0].writes == []
        hold.set()
        owner.join(1)
        with controller._motion._operation():
            skipped = controller.refresh_motion()["motion"]
        assert skipped["faulted"] is False
        assert skipped["feedback_error"] is None
        assert opened[0].writes == []
        controller._manual_calibration_active = True
        controller._limit_calibration_active = True
        controller.refresh_motion()
        assert len(opened[0].writes) == 6
    finally:
        hold.set()
        owner.join(1)
        controller._manual_calibration_active = False
        controller._limit_calibration_active = False
        controller.disconnect_motion()


def test_optional_worker_tracks_hand_turn_survives_stop_and_stops_at_disconnect(tmp_path, monkeypatch):
    controller, opened, _ = connected_controller(tmp_path, interval=0.01)
    worker = controller._motion_feedback_thread
    try:
        opened[0].positions[1] = -5
        wait_for(lambda: controller.device_status()["motion"]["axes"]["X"]["raw_count"] == -5)
        assert {frame[1] for frame in opened[0].writes} <= {0x1F, 0x42, 0x1A, 0x36, 0x35, 0x3A}
        controller.stop_motion()
        assert worker.is_alive()
        opened[0].positions[1] = 25
        wait_for(lambda: controller.device_status()["motion"]["axes"]["X"]["raw_count"] == 25)
        join_worker = worker.join

        def join_without_controller_lock(*args, **kwargs):
            assert not controller._lock._is_owned()
            return join_worker(*args, **kwargs)

        monkeypatch.setattr(worker, "join", join_without_controller_lock)
        controller.disconnect_motion()
        assert not worker.is_alive()
        assert opened[0].closed is True
        writes = len(opened[0].writes)
        time.sleep(0.03)
        assert len(opened[0].writes) == writes
        assert controller.device_status()["motion"]["connected"] is False
    finally:
        if controller._motion is not None:
            controller.disconnect_motion()


def test_old_worker_generation_cannot_read_or_fault_new_connection(tmp_path):
    controller, opened, motion = connected_controller(tmp_path, interval=0.01)
    old_driver = controller._motion
    old_generation = controller._motion_feedback_generation
    controller.disconnect_motion()
    controller.connect_motion(motion)
    try:
        old_driver.refresh = lambda: pytest.fail("An old feedback worker touched its retired bus")
        old_worker = threading.Thread(target=controller._motion_feedback_loop,
                                      args=(old_driver, old_generation, threading.Event()), daemon=True)
        old_worker.start()
        old_worker.join(1)
        assert not old_worker.is_alive()
        assert controller.device_status()["motion"]["feedback_error"] is None
        assert controller.device_status()["motion"]["connected"] is True
        assert len(opened) == 2
    finally:
        controller.disconnect_motion()


@pytest.mark.parametrize("phase,active,expected", [
    ("running", True, "acquiring"),
    ("configuring", True, "reconnecting"),
    ("retrying", True, "reconnecting"),
    ("stopped", True, "stopped_unverified"),
    ("running", False, "stopped_unverified"),
    ("error", True, "error"),
])
def test_board_runtime_requires_matching_live_identity_and_never_claims_a_link(tmp_path, phase, active, expected):
    controller = AutomationController(tmp_path / "state", tmp_path / "runs", tmp_path)
    bindings = {"A": {"address": "board-a"}, "B": {"address": ""}}
    controller._experiment["bindings"]["boards"] = copy.deepcopy(bindings)
    controller._state.update(mode="live", state="running" if active else "completed",
                             boards={"A": {"address": "board-a", "phase": phase,
                                            "start_ack_unix_s": 1, "stop_confirmed": True}})
    hold = threading.Event()
    owner = threading.Thread(target=hold.wait, daemon=True)
    if active:
        owner.start()
        controller._thread = owner
    try:
        boards = controller.device_status(bindings)["boards"]
        assert boards["A"] == {"address": "board-a", "state": expected, "channel_ids": list("ABCD")}
        assert boards["B"] == {"address": "", "state": "unbound", "channel_ids": list("EFGH")}
        changed = {"A": "new-board", "B": ""}
        assert controller.device_status(changed)["boards"]["A"]["state"] == "bound_unverified"
        controller._state["mode"] = "simulate"
        assert controller.device_status(bindings)["boards"]["A"]["state"] == "bound_unverified"
    finally:
        hold.set()
        if active:
            owner.join(1)


def test_old_verified_or_unidentified_board_runtime_is_not_live_connectivity(tmp_path):
    controller = AutomationController(tmp_path / "state", tmp_path / "runs", tmp_path)
    controller._state.update(mode="live", state="completed", boards={"A": {"phase": "running"}})
    assert controller.device_status({"A": "board-a"})["boards"]["A"]["state"] == "bound_unverified"


def test_synthetic_board_cannot_claim_live_runtime_even_if_slot_address_matches(tmp_path):
    controller = AutomationController(tmp_path / "state", tmp_path / "runs", tmp_path)
    controller._state.update(mode="live", state="completed", boards={
        "A": {"address": "board-a", "phase": "running", "synthetic": True}})
    assert controller.device_status({"A": "board-a"})["boards"]["A"]["state"] == "bound_unverified"


def test_recovery_worker_cannot_promote_old_running_board_to_acquiring(tmp_path):
    controller = AutomationController(tmp_path / "state", tmp_path / "runs", tmp_path)
    controller._state.update(mode="live", state="stopping", boards={
        "A": {"address": "board-a", "phase": "running", "start_ack_unix_s": 1,
              "stop_confirmed": False}})
    controller._recovery_required = True
    hold = threading.Event()
    owner = threading.Thread(target=hold.wait, daemon=True)
    owner.start()
    controller._thread = owner
    try:
        assert controller.device_status({"A": "board-a"})["boards"]["A"]["state"] == "error"
    finally:
        hold.set()
        owner.join(1)


def test_recovery_and_active_move_feedback_skips_have_no_read_side_effects(tmp_path):
    controller, opened, _ = connected_controller(tmp_path)
    try:
        opened[0].writes.clear()
        controller._recovery_required = True
        controller.refresh_motion()
        controller._recovery_required = False
        controller._motion_move_active = True
        controller.refresh_motion()
        assert opened[0].writes == []
        assert controller.device_status()["motion"]["feedback_error"] is None
    finally:
        controller._recovery_required = False
        controller._motion_move_active = False
        controller.disconnect_motion()


@pytest.mark.parametrize("mode,state,active,owns_motion,audited", [
    ("simulate", "completed", False, False, False),
    ("simulate", "running", True, False, False),
    ("live", "completed", False, False, False),
    ("live", "running", True, False, False),
    ("live", "running", True, True, True),
])
def test_physical_wire_events_use_physical_context_and_only_current_owned_live_run_audit(
        tmp_path, mode, state, active, owns_motion, audited):
    controller, opened, _ = connected_controller(tmp_path)
    directory = tmp_path / "audit-run"
    directory.mkdir()
    audit = directory / "events.jsonl"
    original = '{"initial": true}\n'
    audit.write_text(original)
    controller._run_dir = directory
    controller._state.update(mode=mode, state=state, run_id="prior-or-current")
    controller._owns_motion = owns_motion
    hold = threading.Event()
    owner = threading.Thread(target=hold.wait, daemon=True)
    if active:
        owner.start()
        controller._thread = owner
    controller._events.clear()
    try:
        opened[0].writes.clear()
        controller._motion.refresh()
        events = controller.events()["events"]
        assert events
        assert all(event["event"] == "motion_wire" and event["synthetic"] is False for event in events)
        assert {event["run_id"] for event in events} == ({"prior-or-current"} if audited else {None})
        assert {frame[1] for frame in opened[0].writes} == {0x36, 0x35, 0x3A}
        if audited:
            saved = [json.loads(line) for line in audit.read_text().splitlines()[1:]]
            assert saved == events
        else:
            assert audit.read_text() == original
    finally:
        hold.set()
        if active:
            owner.join(1)
        controller._run_dir = None
        controller._state["state"] = "completed"
        controller.disconnect_motion()
