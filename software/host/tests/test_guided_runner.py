from __future__ import annotations

import copy
import json
import threading
import time
from pathlib import Path

import pytest

from pa_host import automation


def guided_document():
    document = automation.default_experiment()
    document.update(schema_version="1.1", run_policy="guided", calibration_id="calibration-test")
    document["defaults"].update(x_first_mm=110, column_pitch_mm=-9, settle_s=0)
    for column in document["columns"]:
        column["concentration"] = None
    document["program"] = [{"op": "move_to_column", "column": 2}, {"op": "immerse"},
                           {"op": "dwell", "seconds": 12, "acquire": ["A", "F"],
                            "windows": {"A": {"start_s": 3, "duration_s": 4}}}, {"op": "withdraw"}]
    return document


def test_guided_signed_pitch_and_nullable_concentration_preserve_physical_spacing_and_hash():
    document = guided_document()
    compiled = automation.compile_experiment(document)
    assert next(step for step in compiled["steps"] if step["op"] == "move_x")["resolved_params"]["target_mm"] == 101
    dwell = next(step for step in compiled["steps"] if step["op"] == "dwell")
    assert dwell["concentration"] is None
    assert dwell["resolved_params"]["windows"] == {"A": {"start_s": 3, "duration_s": 4},
                                                  "F": {"start_s": 0, "duration_s": 12}}
    changed = copy.deepcopy(document)
    changed["program"][2]["windows"]["A"]["start_s"] = 4
    assert automation.compile_experiment(changed)["plan_hash"] != compiled["plan_hash"]
    changed = copy.deepcopy(document)
    changed["calibration_id"] = "different-calibration"
    assert automation.compile_experiment(changed)["plan_hash"] != compiled["plan_hash"]


@pytest.mark.parametrize("change", [
    lambda document: document["defaults"].update(column_pitch_mm=0),
    lambda document: document["defaults"].update(x_first_mm=10),
    lambda document: document["program"][2]["windows"]["A"].update(duration_s=10),
    lambda document: document["program"][2]["windows"].update(B={"start_s": 0, "duration_s": 2}),
    lambda document: document.update(calibration_id=""),
    lambda document: document.update(run_policy="failfast"),
    lambda document: document["columns"][0].pop("concentration"),
    lambda document: document["program"][2].update(windows={"A": {"start_s": 0, "duration_s": 0},
                                                               "F": {"start_s": 0, "duration_s": 0}}),
])
def test_guided_rejects_invalid_windows_endpoints_and_identity(change):
    document = guided_document()
    change(document)
    with pytest.raises(automation.AutomationError):
        automation.compile_experiment(document)


def test_legacy_still_rejects_nullable_concentration_negative_pitch_and_guided_fields():
    for change in (lambda document: document["columns"][0].update(concentration=None),
                   lambda document: document["defaults"].update(column_pitch_mm=-9),
                   lambda document: document.update(run_policy="guided"),
                   lambda document: document["program"][2].update(windows={})):
        document = automation.default_experiment()
        change(document)
        with pytest.raises(automation.AutomationError):
            automation.compile_experiment(document)


def wait_terminal(controller):
    deadline = time.monotonic() + 3
    while controller._thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.002)
    assert not controller._thread.is_alive(), controller.snapshot()
    return controller.snapshot()


def test_guided_synthetic_windows_keep_raw_samples_without_inventing_concentration(tmp_path):
    document = guided_document()
    controller = automation.AutomationController(tmp_path / "state", tmp_path / "runs", tmp_path, simulation_scale=0)
    controller.start(document, "simulate", automation.compile_experiment(document)["plan_hash"], "synthetic")
    state = wait_terminal(controller)
    assert state["state"] == "completed", state
    directory = Path(state["manifest_path"]).parent
    samples = [json.loads(line) for line in (directory / "samples.jsonl").read_text().splitlines()]
    raw = [json.loads(line) for line in (directory / "raw-samples.jsonl").read_text().splitlines()]
    assert len(raw) == 48
    selected = [sample for sample in samples if sample["electrode_id"] == "A"]
    assert len(selected) == 8
    assert all(3 <= sample["elapsed_s"] < 7 and sample["concentration"] is None for sample in selected)
    assert all(sample["effective"] for sample in samples)
    assert any(not sample["effective"] for sample in raw)
    assert state["column_timing"]["remaining_s"] == 0


def test_board_identity_and_frame_channel_map_all_eight_electrodes_not_arrival_order(tmp_path):
    controller = automation.AutomationController(tmp_path / "state", tmp_path / "runs", tmp_path)
    document = automation.default_experiment()
    document["bindings"]["boards"]["A"]["address"] = "macos-uuid-first-board"
    document["bindings"]["boards"]["B"]["address"] = "macos-uuid-second-board"
    document["program"] = [{"op": "dwell", "column": 1, "seconds": 10}]
    compiled = automation.compile_experiment(document)
    step = compiled["steps"][0]
    controller._experiment = compiled["experiment"]
    controller._run_dir = tmp_path / "run"
    controller._run_dir.mkdir()
    controller._state.update(run_id="identity-test", channels=[
        {"id": channel, "time_s": [], "current_nA": []} for channel in "ABCDEFGH"])
    for slot in "BA":
        sample_path = tmp_path / f"{slot}.csv"
        sample_path.write_text("host_unix_s,seq,dev_ms,channel,counts,fa_fw,flags\n" + "".join(
            f"1792678900,{sequence},1234,{channel},{100 + channel},{channel * 1000000},0\n"
            for sequence, channel in enumerate((4, 1, 3, 2))))
        child = {"slot": slot, "mask": 15, "samples": sample_path, "offset": 0, "partial": "",
                 "header_read": False, "config": {"info": "same-protocol-name"}, "sample_counts": {},
                 "previous_seq": None, "first_host_time": None}
        controller._read_samples(child, step)
    records = [json.loads(line) for line in (controller._run_dir / "samples.jsonl").read_text().splitlines()]
    assert [record["electrode_id"] for record in records] == list("HEGFDACB")
    assert {(record["electrode_id"], record["board_slot"], record["channel"], record["board_address"])
            for record in records} == {
        (electrode, slot, channel, f"macos-uuid-{'first' if slot == 'A' else 'second'}-board")
        for slot, electrodes in (("A", "ABCD"), ("B", "EFGH"))
        for channel, electrode in enumerate(electrodes, 1)
    }
    assert len({record["sample_fingerprint"] for record in records}) == 8


class Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def time(self):
        return 1700000000 + self.now

    def sleep(self, seconds):
        self.now += seconds


class GuidedCollector:
    def __init__(self, owner, options):
        self.owner, self.options = owner, options
        self.slot = "A" if options["--address"] == "board-a" else "B"
        self.attempt = sum(child.slot == self.slot for child in owner.children) + 1
        self.returncode = None
        self.started = False
        self.sequence = 0
        self.state = {"run_token": options["--run-token"], "attempt_id": options["--attempt-id"],
                      "phase": "configuring", "config_confirmed": True, "stop_confirmed": False,
                      "config": {"settings": json.loads(Path(options["--settings"]).read_text()),
                                 "status": {"seq": 0, "mask": 0}, "info": "test"}, "error": None}
        Path(options["--all-channels"]).write_text("host_unix_s,seq,dev_ms,channel,counts,fa_fw,flags\n")
        self.write_state()

    def write_state(self):
        Path(self.options["--state-file"]).write_text(json.dumps(self.state))

    def poll(self):
        if self.returncode is not None:
            return self.returncode
        now = self.owner.clock.monotonic()
        if self.slot in self.owner.unavailable and now >= self.owner.loss_at:
            self.state.update(phase="error", error="BLE disconnected", error_kind="ble", stop_confirmed=False)
            self.returncode = 1
        elif self.slot == "A" and self.attempt == 1 and now >= self.owner.loss_at:
            self.state.update(phase="error", error="BLE disconnected", error_kind="ble", stop_confirmed=False)
            self.returncode = 1
        elif Path(self.options["--cmd-file"]).read_text().strip() == "STOP":
            self.state.update(phase="stopped", stop_confirmed=True)
            self.returncode = 3
        elif Path(self.options["--start-gate"]).exists() and now >= self.owner.start_at:
            if not self.started:
                self.started = True
                self.state.update(phase="running", start_requested_unix_s=self.owner.clock.time(),
                                  start_ack_unix_s=self.owner.clock.time(), start_ack_monotonic_s=now)
            with Path(self.options["--all-channels"]).open("a") as handle:
                for channel in range(1, 5):
                    if int(self.options["--channel-mask"]) & (1 << (channel - 1)):
                        handle.write(f"{self.owner.clock.time()},{self.sequence},{int(now * 1000)},{channel},123,4500000,0\n")
                        self.sequence += 1
        self.write_state()
        return self.returncode

    def terminate(self):
        self.returncode = -15

    def kill(self):
        self.returncode = -9

    def wait(self, timeout=None):
        return self.poll()


class GuidedFactory:
    def __init__(self, clock, *, loss_at=100, start_at=0, unavailable=()):
        self.clock, self.loss_at, self.start_at = clock, loss_at, start_at
        self.unavailable = unavailable
        self.children = []

    def __call__(self, command, **kwargs):
        options = dict(zip(command[3::2], command[4::2]))
        child = GuidedCollector(self, options)
        self.children.append(child)
        return child


def deterministic_controller(tmp_path, monkeypatch, *, unavailable=(), loss_at=100, start_at=0):
    clock = Clock()
    factory = GuidedFactory(clock, unavailable=unavailable, loss_at=loss_at, start_at=start_at)
    monkeypatch.setattr(automation, "time", clock)
    controller = automation.AutomationController(tmp_path / "state", tmp_path / "runs", tmp_path, process_factory=factory)
    document = guided_document()
    document["bindings"]["boards"]["A"]["address"] = "board-a"
    document["bindings"]["boards"]["B"]["address"] = "board-b"
    document["program"] = [{"op": "dwell", "column": 1, "seconds": 180, "acquire": ["A", "F"]}]
    compiled = automation.compile_experiment(document)
    controller._experiment = compiled["experiment"]
    controller._run_dir = tmp_path / "run"
    controller._run_dir.mkdir()
    controller._state.update(mode="live", run_id="test-run", stop_confirmed=False,
                             channels=[{"id": channel, "time_s": [], "current_nA": []} for channel in "AF"])
    controller._wait = lambda seconds: (controller._check_cancelled(), clock.sleep(max(seconds, 1)))
    return controller, factory, clock, compiled


def test_guided_retry_only_restarts_failed_board_and_keeps_first_ack_deadline(tmp_path, monkeypatch):
    controller, factory, clock, compiled = deterministic_controller(tmp_path, monkeypatch, start_at=10, loss_at=100)
    controller._acquire_guided(compiled["steps"][0])
    assert [child.slot for child in factory.children].count("B") == 1
    attempts = [child for child in factory.children if child.slot == "A"]
    assert len(attempts) == 2
    assert attempts[0].options["--run-token"] != attempts[1].options["--run-token"]
    gate = json.loads(Path(attempts[1].options["--start-gate"]).read_text())
    assert gate["deadline_unix_s"] == 1700000190
    assert gate["remaining_s"] <= 90
    timing = controller.snapshot()["column_timing"]
    assert timing["started_unix_s"] == 1700000010
    assert timing["deadline_unix_s"] == 1700000190
    assert timing["remaining_s"] == 0
    assert clock.now < 193
    samples = [json.loads(line) for line in (controller._run_dir / "samples.jsonl").read_text().splitlines()]
    assert any(sample["board_slot"] == "B" and sample["elapsed_s"] > 100 for sample in samples)
    assert len({sample["attempt_id"] for sample in samples if sample["board_slot"] == "A"}) == 2
    assert all(sample["elapsed_s"] < 180 for sample in samples)


def test_guided_retries_for_entire_long_column_after_extended_outage(tmp_path, monkeypatch):
    controller, factory, clock, compiled = deterministic_controller(tmp_path, monkeypatch, unavailable="A", loss_at=1)
    document = copy.deepcopy(compiled["experiment"])
    document["program"][0]["seconds"] = 600
    compiled = automation.compile_experiment(document)
    controller._experiment = compiled["experiment"]
    original_wait = controller._wait

    def wait_until_recovery(seconds):
        original_wait(seconds)
        if clock.now >= 300:
            factory.unavailable = ()

    controller._wait = wait_until_recovery
    controller._acquire_guided(compiled["steps"][0])
    samples = [json.loads(line) for line in (controller._run_dir / "samples.jsonl").read_text().splitlines()]
    assert any(sample["board_slot"] == "A" and 300 <= sample["elapsed_s"] < 600 for sample in samples)
    assert sum(child.slot == "B" for child in factory.children) == 1
    timing = controller.snapshot()["column_timing"]
    assert timing["deadline_unix_s"] - timing["started_unix_s"] == 600
    assert clock.now < 604


def test_guided_both_lost_finishes_row_and_retains_unknown_stop_without_global_failure(tmp_path, monkeypatch):
    controller, factory, clock, compiled = deterministic_controller(tmp_path, monkeypatch, unavailable="AB", loss_at=100)
    controller._acquire_guided(compiled["steps"][0])
    assert 180 <= clock.now < 183
    state = controller.snapshot()
    assert state["column_timing"]["remaining_s"] == 0
    assert all(state["boards"][slot]["output_state"] == "unknown" for slot in "AB")
    assert not controller._cleanup()


def test_guided_stale_attempt_identity_is_fatal_and_never_enters_effective_data(tmp_path, monkeypatch):
    controller, factory, clock, compiled = deterministic_controller(tmp_path, monkeypatch)
    original = factory.__class__.__call__

    def corrupt(owner, command, **kwargs):
        child = original(owner, command, **kwargs)
        child.state["attempt_id"] = "old-attempt"
        child.write_state()
        return child

    monkeypatch.setattr(GuidedFactory, "__call__", corrupt)
    with pytest.raises(automation.AutomationError) as caught:
        controller._acquire_guided(compiled["steps"][0])
    assert caught.value.code == "collector_identity"
    assert not (controller._run_dir / "samples.jsonl").exists()


def test_pause_is_effective_after_withdraw_and_resume_keeps_last_column(tmp_path):
    document = guided_document()
    document["program"].extend([{"op": "move_to_column", "column": 7}, {"op": "immerse"},
                                {"op": "dwell", "seconds": 10}, {"op": "withdraw"}])
    controller = automation.AutomationController(tmp_path / "state", tmp_path / "runs", tmp_path, simulation_scale=0.01)
    controller.start(document, "simulate", automation.compile_experiment(document)["plan_hash"], "pause")
    controller.pause()
    deadline = time.monotonic() + 3
    while controller.snapshot()["state"] != "paused" and time.monotonic() < deadline:
        time.sleep(0.002)
    state = controller.snapshot()
    assert state["state"] == "paused"
    assert state["column"] == 2
    assert state["position"] == {"X": 101, "Z": 0}
    controller.resume()
    state = wait_terminal(controller)
    assert state["state"] == "completed"
    assert state["position"] == {"X": 56, "Z": 0}


def test_guided_recovery_waits_at_safe_height_for_stop_before_next_x(tmp_path, monkeypatch):
    from test_automation import MotionBus

    controller, factory, clock, compiled = deterministic_controller(tmp_path, monkeypatch, unavailable="AB", loss_at=100)
    document = copy.deepcopy(compiled["experiment"])
    document["program"] = [{"op": "move_to_column", "column": 1}, {"op": "immerse"},
                           {"op": "dwell", "seconds": 180, "acquire": ["A", "F"]}, {"op": "withdraw"},
                           {"op": "move_to_column", "column": 2}]
    compiled = automation.compile_experiment(document)
    controller._experiment = compiled["experiment"]
    controller._motion = MotionBus("fake", 115200, {})
    controller._motion.connect()
    controller._motion.confirm_reference()
    controller._motion.enable("X")
    controller._motion.enable("Z")
    controller._state.update(position={"X": 0, "Z": 0})
    controller._set_ownership(compiled)
    controller._idempotency["recovery"] = {}
    observed = []

    def wait(seconds):
        controller._check_cancelled()
        if controller._state["state"] == "waiting_recovery":
            observed.append(controller._motion.position.copy())
            assert controller._motion.position == {"X": 110, "Z": 0}
            assert controller._state["column_timing"]["remaining_s"] == 0
            factory.unavailable = ()
        clock.sleep(max(seconds, 1))

    controller._wait = wait
    controller._execute(compiled, "recovery")
    assert observed
    assert controller.snapshot()["state"] == "completed"
    assert controller._motion.position == {"X": 101, "Z": 0}
    assert controller.snapshot()["stop_confirmed"]


def test_guided_short_dwell_keeps_requested_duration_and_safe_collector_settings():
    document = guided_document()
    document["program"][2].update(seconds=1, windows={})
    compiled = automation.compile_experiment(document)
    params = next(step["resolved_params"] for step in compiled["steps"] if step["op"] == "dwell")
    assert params["seconds"] == 1
    assert params["settings"]["A"]["duration_s"] == 10
    assert params["windows"]["A"]["duration_s"] == 1


def test_guided_zero_window_does_not_enable_that_electrode():
    document = guided_document()
    document["program"][2]["windows"]["A"] = {"start_s": 0, "duration_s": 0}
    params = next(step["resolved_params"] for step in automation.compile_experiment(document)["steps"] if step["op"] == "dwell")
    assert params["acquire"] == ["F"]
    assert params["channel_masks"] == {"A": 0, "B": 2}
    assert "A" not in params["settings"]


def test_guided_deadline_uses_earliest_available_ack_not_board_iteration_order(tmp_path, monkeypatch):
    controller, factory, clock, compiled = deterministic_controller(tmp_path, monkeypatch)
    step = compiled["steps"][0]
    controller._state["column_timing"] = {"column": 1, "step_id": step["id"], "duration_s": 180,
        "started_unix_s": None, "deadline_unix_s": None, "elapsed_s": 0, "remaining_s": 180}
    first = controller._guided_spawn(step, "A", 1)
    second = controller._guided_spawn(step, "B", 2)
    for child in (first, second):
        child["gate_released"] = True
    clock.sleep(1)
    factory.children[1].state.update(phase="running", start_ack_unix_s=1700000000, start_ack_monotonic_s=0)
    factory.children[1].write_state()
    controller._guided_started(first, {"start_ack_unix_s": 1700000001, "start_ack_monotonic_s": 1}, step)
    assert controller._state["column_timing"]["started_unix_s"] == 1700000000
    assert controller._state["column_timing"]["deadline_unix_s"] == 1700000180
    controller._stop_children()


def test_guided_late_board_and_unrecovered_loss_have_explicit_bounded_gaps(tmp_path, monkeypatch):
    controller, factory, clock, compiled = deterministic_controller(tmp_path, monkeypatch, unavailable="A", loss_at=101)
    original = GuidedCollector.poll

    def late_board(child):
        if child.slot == "B" and clock.monotonic() < 31 and not child.started:
            return None
        return original(child)

    monkeypatch.setattr(GuidedCollector, "poll", late_board)
    controller._acquire_guided(compiled["steps"][0])
    boards = controller.snapshot()["boards"]
    assert boards["B"]["gaps"][0]["start_s"] == 0
    assert boards["B"]["gaps"][0]["end_s"] == 30
    assert boards["A"]["gaps"][-1]["start_s"] == 100
    assert boards["A"]["gaps"][-1]["end_s"] == 180


def test_guided_checkpoint_runs_after_confirmed_motion_cleanup(tmp_path):
    from test_automation import MotionBus

    class ReferenceBus(MotionBus):
        def reference_identity(self):
            if not self.referenced:
                raise RuntimeError("Reference was invalidated")
            return {"reference_counts": {"X": 0, "Z": 0}, "raw_counts": {"X": 12, "Z": 18}}

    checkpoints = []
    controller = automation.AutomationController(tmp_path / "state", tmp_path / "runs", tmp_path,
        parked_callback=lambda motion, reference: checkpoints.append((motion.snapshot(), reference)))
    controller._motion = ReferenceBus("fake", 115200, {})
    controller._motion.connect()
    controller._motion.confirm_reference()
    controller._owns_motion = True
    controller._experiment = guided_document()
    controller._run_dir = tmp_path / "run"
    controller._run_dir.mkdir()
    compiled = automation.compile_experiment(controller._experiment)
    compiled["steps"] = []
    controller._state.update(mode="live", run_id="parked", stop_confirmed=False)
    controller._idempotency["parked"] = {}
    controller._execute(compiled, "parked")
    assert len(checkpoints) == 1
    assert checkpoints[0][0]["referenced"] is False
    assert checkpoints[0][1]["raw_counts"] == {"X": 12, "Z": 18}
    assert controller.snapshot()["state"] == "completed"


def test_guided_checkpoint_serial_callbacks_share_controller_lock_order(tmp_path):
    from test_automation import MotionBus

    contenders = []
    entered_during_checkpoint = []

    def probe_controller_lock():
        entered = threading.Event()

        def contender():
            with controller._lock:
                entered.set()

        worker = threading.Thread(target=contender)
        contenders.append(worker)
        worker.start()
        entered_during_checkpoint.append(entered.wait(0.03))
        controller._event("checkpoint_wire_callback")

    class ReferenceBus(MotionBus):
        def reference_identity(self):
            probe_controller_lock()
            return {"reference_counts": {"X": 0, "Z": 0}, "raw_counts": {"X": 12, "Z": 18}}

    def checkpoint(motion, reference):
        probe_controller_lock()

    controller = automation.AutomationController(tmp_path / "state", tmp_path / "runs", tmp_path,
        parked_callback=checkpoint)
    controller._motion = ReferenceBus("fake", 115200, {})
    controller._motion.connect()
    controller._motion.confirm_reference()
    controller._owns_motion = True
    controller._experiment = guided_document()
    controller._run_dir = tmp_path / "run"
    controller._run_dir.mkdir()
    compiled = automation.compile_experiment(controller._experiment)
    compiled["steps"] = []
    controller._state.update(mode="live", run_id="parked", stop_confirmed=False)
    controller._idempotency["parked"] = {}
    controller._execute(compiled, "parked")
    for worker in contenders:
        worker.join(timeout=1)
        assert not worker.is_alive()
    assert entered_during_checkpoint == [False, False]
    assert controller.snapshot()["state"] == "completed"


def test_guided_restart_signals_orphan_attempt_before_fresh_stop_connection(tmp_path):
    document = guided_document()
    document["bindings"]["boards"]["A"]["address"] = "board-a"
    document["bindings"]["boards"]["B"]["address"] = "board-b"
    directory = tmp_path / "runs" / "live" / "orphan"
    commands = directory / "step-0001" / "board-A" / "attempt-0001" / "commands.txt"
    commands.parent.mkdir(parents=True)
    commands.write_text("")
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "automation.json").write_text(json.dumps({"experiment": document,
        "state": {"state": "running", "mode": "live", "stop_confirmed": False,
                  "run_id": "orphan", "manifest_path": str(directory / "manifest.json")},
        "ownership": {"boards": ["board-a"], "motion": False}}))
    connections = []

    def board_stop(address):
        assert commands.read_text().strip() == "STOP"
        connections.append(address)
        return {"stop_confirmed": True}

    controller = automation.AutomationController(state_dir, tmp_path / "runs", tmp_path, board_stop_factory=board_stop)
    controller.stop()
    state = wait_terminal(controller)
    assert state["state"] == "stopped", state
    assert connections == ["board-a"]
