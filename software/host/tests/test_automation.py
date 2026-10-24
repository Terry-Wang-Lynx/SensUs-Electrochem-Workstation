from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import time
from pathlib import Path

import pytest


def automation():
    assert importlib.util.find_spec("pa_host.automation") is not None, "automation backend is missing"
    from pa_host import automation as module
    return module


def test_compile_inserts_safe_z_before_x_and_resolves_overrides():
    module = automation()
    document = module.default_experiment()
    document["columns"][0]["overrides"] = {"dwell_s": 30, "descend_speed_mm_s": 0.75}
    document["program"] = [{"op": "move_to_column", "column": 1}, {"op": "immerse"},
                           {"op": "dwell", "seconds": 12, "acquire": ["A", "C", "F"]},
                           {"op": "withdraw", "ascend_speed_mm_s": 0.4}]
    result = module.compile_experiment(document)
    assert [step["op"] for step in result["steps"]][:3] == ["move_z", "move_x", "move_z"]
    assert result["steps"][0]["resolved_params"]["target_mm"] == 0
    assert result["steps"][2]["resolved_params"]["speed_mm_s"] == 0.75
    dwell = next(step for step in result["steps"] if step["op"] == "dwell")
    assert dwell["resolved_params"]["seconds"] == 12
    assert dwell["resolved_params"]["channel_masks"] == {"A": 5, "B": 2}
    assert result["steps"][-1]["resolved_params"]["speed_mm_s"] == 0.4
    assert module.compile_experiment(copy.deepcopy(document))["plan_hash"] == result["plan_hash"]
    document["columns"][0]["concentration"]["value"] = 5
    assert module.compile_experiment(document)["plan_hash"] != result["plan_hash"]


@pytest.mark.parametrize("change,code", [
    (lambda doc: doc["defaults"].update(z_immersed_mm=80), "invalid_value"),
    (lambda doc: doc["defaults"].update(descend_speed_mm_s=float("nan")), "invalid_value"),
    (lambda doc: doc["defaults"].update(translate_speed_mm_s=30), "invalid_value"),
    (lambda doc: doc["bindings"]["motion"]["axes"]["Z"].update(address=1), "invalid_value"),
    (lambda doc: doc["program"].insert(0, {"op": "python", "code": "print(1)"}), "invalid_action"),
    (lambda doc: doc.update(program=[{"op": "immerse"}]), "missing_column"),
    (lambda doc: doc.update(program=[{"op": "move_to_column", "column": 13}]), "invalid_column"),
    (lambda doc: doc.update(program=[{"op": "repeat", "count": 501, "body": [{"op": "wait", "seconds": 0}]}]), "plan_limit"),
    (lambda doc: doc.update(program=[{"op": "wait", "seconds": 28801}]), "plan_limit"),
    (lambda doc: doc["columns"][0].update(concentration={"value": -1, "unit": "uM"}), "invalid_value"),
])
def test_compile_rejects_unsafe_documents_before_runtime(change, code):
    module = automation()
    document = module.default_experiment()
    change(document)
    with pytest.raises(module.AutomationError) as caught:
        module.compile_experiment(document)
    assert caught.value.code == code
    assert caught.value.path


def wait_finished(controller):
    deadline = time.monotonic() + 4
    while controller.is_busy() and time.monotonic() < deadline:
        time.sleep(0.005)
    return controller.snapshot()


def test_simulation_records_provenance_without_instantiating_hardware(tmp_path):
    module = automation()
    def forbidden(*args, **kwargs):
        pytest.fail("simulation tried to access hardware")
    controller = module.AutomationController(tmp_path / "state", tmp_path / "runs", tmp_path,
                                            motion_factory=forbidden, process_factory=forbidden,
                                            simulation_scale=0)
    document = module.default_experiment()
    preview = controller.validate(document)
    assert not list((tmp_path / "runs").glob("**/*"))
    started = controller.start(document, mode="simulate", plan_hash=preview["plan_hash"], idempotency_key="first")
    state = wait_finished(controller)
    assert state["state"] == "completed"
    assert state["stop_confirmed"] is True
    assert all(channel["synthetic"] and channel["valid"] for channel in state["channels"])
    manifest_path = Path(state["manifest_path"])
    manifest = json.loads(manifest_path.read_text())
    samples_path = manifest_path.parent / "samples.jsonl"
    samples = [json.loads(line) for line in samples_path.read_text().splitlines()]
    assert {sample["electrode_id"] for sample in samples} == set("ABCDEFGH")
    sample = samples[0]
    assert sample["synthetic"] is True
    assert sample["concentration"] == {"value": 0, "unit": "uM"}
    assert all(sample[key] for key in ("device_fingerprint", "config_fingerprint", "step_fingerprint", "sample_fingerprint", "host_utc"))
    assert manifest["files"]["samples.jsonl"] == hashlib.sha256(samples_path.read_bytes()).hexdigest()
    again = controller.start(document, mode="simulate", plan_hash=preview["plan_hash"], idempotency_key="first")
    assert again["run_id"] == started["run_id"]
    assert len(list((tmp_path / "runs" / "simulate").iterdir())) == 1
    with pytest.raises(module.AutomationError, match="idempotency"):
        controller.start(document, mode="live", plan_hash=preview["plan_hash"], idempotency_key="first")


def test_live_missing_gates_and_hash_never_touch_hardware(tmp_path):
    module = automation()
    def forbidden(*args, **kwargs):
        pytest.fail("invalid run tried to access hardware")
    controller = module.AutomationController(tmp_path / "state", tmp_path / "runs", tmp_path,
                                            motion_factory=forbidden, process_factory=forbidden)
    document = module.default_experiment()
    with pytest.raises(module.AutomationError) as caught:
        controller.start(document, mode="simulate", plan_hash="wrong", idempotency_key="one")
    assert caught.value.code == "plan_changed"
    with pytest.raises(module.AutomationError) as caught:
        controller.start(document, mode="live", plan_hash=module.compile_experiment(document)["plan_hash"], idempotency_key="two")
    assert caught.value.code == "live_gate"
    assert not list((tmp_path / "runs").glob("**/*"))


def test_operator_wait_can_resume_or_stop_and_no_duplicate_execution(tmp_path):
    module = automation()
    controller = module.AutomationController(tmp_path / "state", tmp_path / "runs", tmp_path, simulation_scale=0)
    document = module.default_experiment()
    document["program"] = [{"op": "wait_operator", "message": "Replace plate"}]
    compiled = module.compile_experiment(document)
    first = controller.start(document, "simulate", compiled["plan_hash"], "manual")
    deadline = time.monotonic() + 2
    while controller.snapshot()["state"] != "waiting_operator" and time.monotonic() < deadline:
        time.sleep(0.005)
    assert controller.snapshot()["state"] == "waiting_operator"
    assert controller.start(document, "simulate", compiled["plan_hash"], "manual")["run_id"] == first["run_id"]
    with pytest.raises(module.AutomationError) as caught:
        controller.save(document)
    assert caught.value.status == 409
    controller.resume()
    assert wait_finished(controller)["state"] == "completed"
    controller.start(document, "simulate", compiled["plan_hash"], "manual2")
    controller.stop()
    assert wait_finished(controller)["state"] == "stopped"


class CollectorFactory:
    def __init__(self, *, failure_slot=None, unconfirmed_slot=None, stale_token=False, hold_gate=False):
        self.children = []
        self.failure_slot = failure_slot
        self.unconfirmed_slot = unconfirmed_slot
        self.stale_token = stale_token
        self.hold_gate = hold_gate
        self.gate_violation = False

    def __call__(self, command, **kwargs):
        options = dict(zip(command[3::2], command[4::2]))
        child = FakeCollector(self, options)
        self.children.append(child)
        return child


class FakeCollector:
    def __init__(self, owner, options):
        self.owner, self.options = owner, options
        self.slot = "A" if options["--address"] == "board-a" else "B"
        self.returncode = None
        self.started = False
        self.polls = 0
        self.state = {"run_token": "stale" if owner.stale_token else options["--run-token"],
                      "phase": "configuring", "config_confirmed": self.slot == "A", "stop_confirmed": False,
                      "config": {"settings": json.loads(Path(options["--settings"]).read_text()),
                                 "status": {"seq": 0}, "version": "V5.2"}, "error": None}
        self.write_state()
        Path(options["--all-channels"]).write_text("host_unix_s,seq,dev_ms,channel,counts,fa_fw,flags\n")

    def write_state(self):
        Path(self.options["--state-file"]).write_text(json.dumps(self.state))

    def poll(self):
        if self.returncode is not None:
            return self.returncode
        self.polls += 1
        if Path(self.options["--cmd-file"]).read_text().strip() == "STOP":
            self.state.update(phase="stopped", stop_confirmed=self.slot != self.owner.unconfirmed_slot)
            self.returncode = 3
        elif self.owner.failure_slot == self.slot and self.polls >= 2:
            self.state.update(phase="error", error="injected stream failure", stop_confirmed=self.slot != self.owner.unconfirmed_slot)
            self.returncode = 1
        elif Path(self.options["--start-gate"]).exists():
            gate = json.loads(Path(self.options["--start-gate"]).read_text())
            if gate["run_token"] != self.options["--run-token"] or not all(child.state["config_confirmed"] for child in self.owner.children):
                self.owner.gate_violation = True
            if not self.started:
                self.started = True
                self.state.update(phase="running", start_ack_unix_s=time.time())
                channels = [channel for channel in range(1, 5) if int(self.options["--channel-mask"]) & (1 << (channel - 1))]
                with Path(self.options["--all-channels"]).open("a") as handle:
                    for sequence, channel in enumerate(channels):
                        handle.write(f"{time.time()},{sequence},124,{channel},123,4500000,0\n")
            else:
                self.state.update(phase="completed", stop_confirmed=self.slot != self.owner.unconfirmed_slot)
                self.returncode = 0
        elif self.slot == "B" and self.polls >= 2 and not self.owner.hold_gate:
            self.state["config_confirmed"] = True
        self.write_state()
        return self.returncode

    def terminate(self):
        self.returncode = -15

    def kill(self):
        self.returncode = -9

    def wait(self, timeout=None):
        return self.poll()


def live_document(module, channels=None):
    document = module.default_experiment()
    document["bindings"]["boards"]["A"]["address"] = "board-a"
    document["bindings"]["boards"]["B"]["address"] = "board-b"
    document["safety"]["electrical_policy_confirmed"] = True
    document["program"] = [{"op": "dwell", "column": 1, "seconds": 10, "acquire": channels or ["A", "C", "F"]}]
    return document


def test_live_parallel_gates_and_real_channel_masks_with_complete_raw_provenance(tmp_path):
    module = automation()
    factory = CollectorFactory()
    controller = module.AutomationController(tmp_path / "state", tmp_path / "runs", tmp_path, process_factory=factory)
    document = live_document(module)
    controller.start(document, "live", module.compile_experiment(document)["plan_hash"], "paired")
    state = wait_finished(controller)
    assert state["state"] == "completed", state
    assert len(factory.children) == 2
    assert not factory.gate_violation
    assert [child.options["--channel-mask"] for child in factory.children] == ["5", "2"]
    assert all(child.started for child in factory.children)
    samples = [json.loads(line) for line in (Path(state["manifest_path"]).parent / "samples.jsonl").read_text().splitlines()]
    assert {sample["electrode_id"] for sample in samples} == {"A", "C", "F"}
    assert all(sample["current_fa"] == 4500000 and sample["counts"] == 123 and sample["uptime_ms"] == 124 for sample in samples)
    assert all(sample["synthetic"] is False for sample in samples)


def test_only_selected_board_spawns_but_live_bindings_must_be_unique(tmp_path):
    module = automation()
    factory = CollectorFactory()
    controller = module.AutomationController(tmp_path / "state", tmp_path / "runs", tmp_path, process_factory=factory)
    document = live_document(module, ["B", "D"])
    controller.start(document, "live", module.compile_experiment(document)["plan_hash"], "one-board")
    assert wait_finished(controller)["state"] == "completed"
    assert len(factory.children) == 1
    assert factory.children[0].options["--channel-mask"] == "10"
    document["bindings"]["boards"]["B"]["address"] = "BOARD-A"
    with pytest.raises(module.AutomationError) as caught:
        controller.start(document, "live", module.compile_experiment(document)["plan_hash"], "duplicate")
    assert caught.value.code == "live_gate"


@pytest.mark.parametrize("unconfirmed", [None, "A"])
def test_configuration_failure_stops_peer_and_unconfirmed_stop_keeps_ownership(tmp_path, unconfirmed):
    module = automation()
    factory = CollectorFactory(failure_slot="A", unconfirmed_slot=unconfirmed)
    controller = module.AutomationController(tmp_path / "state", tmp_path / "runs", tmp_path, process_factory=factory)
    document = live_document(module)
    document["program"].append({"op": "wait_operator", "message": "Must not reach this"})
    controller.start(document, "live", module.compile_experiment(document)["plan_hash"], "fails")
    deadline = time.monotonic() + 3
    while controller.snapshot()["state"] not in ("error", "completed") and time.monotonic() < deadline:
        time.sleep(0.005)
    state = controller.snapshot()
    assert state["state"] == "error"
    assert len(factory.children) == 2
    assert not any(child.started for child in factory.children)
    assert all(Path(child.options["--cmd-file"]).read_text().strip() == "STOP" for child in factory.children)
    assert state["stop_confirmed"] is (unconfirmed is None)
    assert controller.is_busy() is (unconfirmed is not None)
    assert state["step_index"] == 0


@pytest.mark.parametrize("change", [
    lambda doc: doc.update(script="ignored"),
    lambda doc: doc["bindings"]["motion"].update(home_on_connect=True),
    lambda doc: doc["bindings"]["motion"]["axes"]["X"].update(max_speed_mm_s=201),
    lambda doc: doc["bindings"]["boards"]["A"]["settings"].update(unknown_drive=True),
    lambda doc: doc["columns"][0].update(inject="ignored"),
    lambda doc: doc["electrodes"][0].update(id=1),
    lambda doc: doc.update(program=[{"op": "dwell", "column": 1, "seconds": 10}, {"op": "immerse"}]),
])
def test_unknown_ignored_fields_and_unpositioned_immersion_are_rejected(change):
    module = automation()
    document = module.default_experiment()
    change(document)
    with pytest.raises(module.AutomationError):
        module.compile_experiment(document)


def test_stale_collector_token_never_releases_gate_and_blocks_reuse(tmp_path):
    module = automation()
    factory = CollectorFactory(stale_token=True)
    controller = module.AutomationController(tmp_path / "state", tmp_path / "runs", tmp_path, process_factory=factory)
    document = live_document(module)
    controller.start(document, "live", module.compile_experiment(document)["plan_hash"], "stale")
    deadline = time.monotonic() + 2
    while controller.snapshot()["state"] != "error" and time.monotonic() < deadline:
        time.sleep(0.005)
    assert controller.snapshot()["state"] == "error"
    assert controller.is_busy()
    assert not any(Path(child.options["--start-gate"]).exists() for child in factory.children)


def test_simulation_state_marks_scale_and_terminal_events_do_not_mutate_artifacts(tmp_path):
    module = automation()
    controller = module.AutomationController(tmp_path / "state", tmp_path / "runs", tmp_path, simulation_scale=0)
    document = module.default_experiment()
    controller.start(document, "simulate", module.compile_experiment(document)["plan_hash"], "finish")
    state = wait_finished(controller)
    assert state["synthetic"] is True
    assert state["simulation_scale"] == 0
    manifest = Path(state["manifest_path"])
    events = manifest.parent / "events.jsonl"
    before = events.read_bytes()
    controller._event("independent_idle_action")
    assert events.read_bytes() == before


def test_schema_describes_bindings_concentrations_and_repeat_and_has_resolvable_openapi_refs():
    module = automation()
    schema = module.experiment_schema()
    assert schema["properties"]["columns"]["items"]["properties"]["concentration"]["required"] == ["value", "unit"]
    assert schema["properties"]["bindings"]["properties"]["boards"]["required"] == ["A", "B"]
    assert "body" in schema["$defs"]["action"]["properties"]
    api = module.openapi_document()
    experiment_schema = api["components"]["schemas"]["Experiment"]
    assert experiment_schema["properties"]["program"]["items"]["$ref"] == "#/components/schemas/Experiment/$defs/action"


def test_restart_recovers_only_via_explicit_stop_and_keeps_idempotency(tmp_path):
    module = automation()
    document = live_document(module)
    compiled = module.compile_experiment(document)
    factory = CollectorFactory(failure_slot="A", unconfirmed_slot="A")
    controller = module.AutomationController(tmp_path / "state", tmp_path / "runs", tmp_path, process_factory=factory)
    started = controller.start(document, "live", compiled["plan_hash"], "recovery")
    deadline = time.monotonic() + 2
    while controller.snapshot()["state"] != "error" and time.monotonic() < deadline:
        time.sleep(0.005)
    stops = []
    def board_stop(address):
        stops.append(address)
        return {"stop_confirmed": True}
    restarted = module.AutomationController(tmp_path / "state", tmp_path / "runs", tmp_path, board_stop_factory=board_stop)
    assert restarted.snapshot()["recovery_required"] is True
    assert restarted.is_busy()
    assert stops == []
    with pytest.raises(module.AutomationError):
        restarted.start(document, "live", compiled["plan_hash"], "new-run")
    restarted.stop()
    state = wait_finished(restarted)
    assert state["state"] == "stopped"
    assert state["stop_confirmed"] and not state["recovery_required"]
    assert stops == ["board-a", "board-b"]
    assert restarted.start(document, "live", compiled["plan_hash"], "recovery")["run_id"] == started["run_id"]


def test_recovery_allows_read_only_motion_reconnect_but_no_reference_or_enable(tmp_path):
    module = automation()
    document = module.default_experiment()
    document["bindings"]["motion"]["port"] = "fake-rs485"
    document["bindings"]["boards"]["A"]["address"] = "board-a"
    document["bindings"]["boards"]["B"]["address"] = "board-b"
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "automation.json").write_text(json.dumps({"experiment": document,
        "state": {"state": "running", "mode": "live", "stop_confirmed": False, "run_id": "crashed"}}))
    class ReadOnlyBus:
        def __init__(self, *args, **kwargs):
            self.connected = False
        def connect(self):
            self.connected = True
        def snapshot(self):
            return {"connected": self.connected, "referenced": False, "position": {"X": None, "Z": None}, "axes": {}}
        def stop_all(self):
            return {"stop_confirmed": True}
        def close(self):
            self.connected = False
    controller = module.AutomationController(state_dir, tmp_path / "runs", tmp_path,
        motion_factory=ReadOnlyBus, board_stop_factory=lambda address: {"stop_confirmed": True})
    assert controller.connect_motion(document["bindings"]["motion"])["motion"]["connected"]
    with pytest.raises(module.AutomationError):
        controller.reference_motion(True)
    with pytest.raises(module.AutomationError):
        controller.enable_motion("X")
    controller.stop()
    assert wait_finished(controller)["stop_confirmed"] is True


def test_cancel_during_configuration_never_releases_gate(tmp_path):
    module = automation()
    factory = CollectorFactory(hold_gate=True)
    controller = module.AutomationController(tmp_path / "state", tmp_path / "runs", tmp_path, process_factory=factory)
    document = live_document(module)
    controller.start(document, "live", module.compile_experiment(document)["plan_hash"], "cancel-config")
    deadline = time.monotonic() + 2
    while len(factory.children) < 2 and time.monotonic() < deadline:
        time.sleep(0.005)
    controller.stop()
    state = wait_finished(controller)
    assert state["state"] == "stopped"
    assert state["stop_confirmed"] is True
    assert len(factory.children) == 2 and not any(child.started for child in factory.children)
    assert not any(Path(child.options["--start-gate"]).exists() for child in factory.children)


def test_repeat_limits_and_fingerprints_distinguish_executions():
    module = automation()
    document = module.default_experiment()
    document["program"] = [{"op": "repeat", "count": 500, "body": [{"op": "wait", "seconds": 0}]}]
    steps = module.compile_experiment(document)["steps"]
    assert len(steps) == len({step["fingerprint"] for step in steps}) == 500
    body = [{"op": "wait", "seconds": 0}]
    for unused in range(3):
        body = [{"op": "repeat", "count": 1, "body": body}]
    document["program"] = body
    assert len(module.compile_experiment(document)["steps"]) == 1
    document["program"] = [{"op": "repeat", "count": 1, "body": body}]
    with pytest.raises(module.AutomationError) as caught:
        module.compile_experiment(document)
    assert caught.value.code == "plan_limit"


@pytest.mark.parametrize("change", [
    lambda doc: doc.update(program=[{"op": {"execute": "shell"}}]),
    lambda doc: doc["bindings"]["boards"]["A"]["settings"].update(working_electrode_v=True),
    lambda doc: doc["bindings"]["boards"]["A"]["settings"].update(board_profile="v51"),
    lambda doc: doc.update(program=[{"op": "move_to_column", "column": 1}, {"op": "wait"}]),
])
def test_all_malformed_action_and_settings_errors_are_structured(change):
    module = automation()
    document = module.default_experiment()
    change(document)
    with pytest.raises(module.AutomationError):
        module.compile_experiment(document)


def test_large_terminal_sample_file_is_drained_without_losing_rows(tmp_path):
    module = automation()
    class BulkCollector(FakeCollector):
        def poll(self):
            before = self.started
            result = super().poll()
            if self.started and not before:
                with Path(self.options["--all-channels"]).open("a") as handle:
                    for sequence in range(1, 6000):
                        handle.write(f"{time.time()},{sequence},124,1,123,4500000,0\n")
            return result
    class BulkFactory(CollectorFactory):
        def __call__(self, command, **kwargs):
            options = dict(zip(command[3::2], command[4::2]))
            child = BulkCollector(self, options)
            self.children.append(child)
            return child
    controller = module.AutomationController(tmp_path / "state", tmp_path / "runs", tmp_path, process_factory=BulkFactory())
    document = live_document(module, ["A"])
    controller.start(document, "live", module.compile_experiment(document)["plan_hash"], "bulk")
    state = wait_finished(controller)
    assert state["state"] == "completed", state["error"]
    samples = (Path(state["manifest_path"]).parent / "samples.jsonl").read_text().splitlines()
    assert len(samples) == 6000
    assert len(state["channels"][0]["time_s"]) == 2000


class MotionBus:
    def __init__(self, port, baud, axes, **kwargs):
        self.position = {"X": 0, "Z": 0}
        self.connected = False
        self.referenced = False
        self.enabled = {"X": False, "Z": False}
        self.stop_confirms = True
    def connect(self):
        self.connected = True
        return self.snapshot()
    def snapshot(self):
        return {"connected": self.connected, "referenced": self.referenced, "position": self.position.copy(),
                "axes": {axis: {"referenced": self.referenced, "enabled": enabled,
                                "enable_confirmed": enabled, "telemetry_valid": True}
                         for axis, enabled in self.enabled.items()}}
    def refresh(self):
        return self.snapshot()
    def confirm_reference(self):
        self.referenced = True
    def enable(self, axis):
        self.enabled[axis] = True
    def move_to(self, axis, target_mm, speed_mm_s, cancel_event=None):
        if not self.referenced or not self.enabled[axis]:
            raise RuntimeError("Unreferenced or disabled motion")
        self.position[axis] = target_mm
    def stop_all(self):
        if self.stop_confirms:
            self.referenced = False
        return {"stop_confirmed": self.stop_confirms}
    def close(self):
        self.connected = False


def test_live_motion_requires_explicit_reference_and_both_axis_enables(tmp_path):
    module = automation()
    controller = module.AutomationController(tmp_path / "state", tmp_path / "runs", tmp_path, motion_factory=MotionBus)
    document = module.default_experiment()
    document["bindings"]["motion"]["port"] = "fake-bus"
    document["program"] = [{"op": "move_to_column", "column": 8}]
    document["safety"] = {"motion_policy_confirmed": True, "electrical_policy_confirmed": True}
    plan_hash = module.compile_experiment(document)["plan_hash"]
    connected = controller.connect_motion(document["bindings"]["motion"])
    assert not connected["motion"]["referenced"]
    assert not any(axis["enabled"] for axis in connected["motion"]["axes"].values())
    with pytest.raises(module.AutomationError):
        controller.start(document, "live", plan_hash, "position")
    controller.reference_motion(True)
    controller.enable_motion("X")
    with pytest.raises(module.AutomationError):
        controller.start(document, "live", plan_hash, "position")
    controller.enable_motion("Z")
    controller.start(document, "live", plan_hash, "position")
    state = wait_finished(controller)
    assert state["state"] == "completed"
    assert state["motion"]["position"] == {"X": 63, "Z": 0}
    assert state["stop_confirmed"] is True


def test_unconfirmed_manual_motion_stop_survives_restart(tmp_path):
    module = automation()
    controller = module.AutomationController(tmp_path / "state", tmp_path / "runs", tmp_path, motion_factory=MotionBus)
    motion = module.default_experiment()["bindings"]["motion"]
    motion["port"] = "manual-bus"
    controller.connect_motion(motion)
    controller._motion.stop_confirms = False
    assert controller.stop_motion()["stop_confirmed"] is False
    restored = module.AutomationController(tmp_path / "state", tmp_path / "runs", tmp_path, motion_factory=MotionBus)
    assert restored.is_busy()
    assert restored.snapshot()["recovery_required"] is True
    restored.connect_motion(motion)
    restored.stop()
    assert wait_finished(restored)["stop_confirmed"] is True


def test_global_runtime_budget_interrupts_a_motion_step(tmp_path, monkeypatch):
    module = automation()
    class SlowMotion(MotionBus):
        def move_to(self, axis, target_mm, speed_mm_s, cancel_event=None):
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline and not cancel_event.is_set():
                time.sleep(0.005)
    document = module.default_experiment()
    document["bindings"]["motion"]["port"] = "slow-motion"
    document["defaults"].update(dwell_s=0, settle_s=0)
    document["program"] = [{"op": "move_to_column", "column": 1}]
    document["safety"] = {"motion_policy_confirmed": True, "electrical_policy_confirmed": True}
    plan_hash = module.compile_experiment(document)["plan_hash"]
    controller = module.AutomationController(tmp_path / "state", tmp_path / "runs", tmp_path, motion_factory=SlowMotion)
    controller.connect_motion(document["bindings"]["motion"])
    controller.reference_motion(True)
    controller.enable_motion("X")
    controller.enable_motion("Z")
    monkeypatch.setattr(module, "MAX_SECONDS", 0.03)
    started = time.monotonic()
    controller.start(document, "live", plan_hash, "bounded")
    state = wait_finished(controller)
    assert time.monotonic() - started < 0.3
    assert state["state"] == "error"
    assert state["stop_confirmed"] is True


@pytest.mark.parametrize("raises", [False, True])
def test_failed_disconnect_preserves_busy_ownership_and_allows_only_explicit_recovery(tmp_path, raises):
    module = automation()
    class UnconfirmedCloseBus(MotionBus):
        def close(self):
            self.connected = False
            if raises:
                from pa_host.emm_motion import MotionError
                raise MotionError("stop_unconfirmed", "No stationary STOP telemetry", stop_result={"stop_confirmed": False})
            return {"stop_confirmed": False}
    motion = module.default_experiment()["bindings"]["motion"]
    motion["port"] = "disconnect-bus"
    controller = module.AutomationController(tmp_path / "state", tmp_path / "runs", tmp_path, motion_factory=UnconfirmedCloseBus)
    controller.connect_motion(motion)
    with pytest.raises(module.AutomationError) as caught:
        controller.disconnect_motion()
    assert caught.value.code == "stop_unconfirmed"
    state = controller.snapshot()
    assert state["busy"] and state["recovery_required"] and not state["stop_confirmed"]
    assert controller._motion is not None
    with pytest.raises(module.AutomationError):
        controller.enable_motion("X")
    with pytest.raises(module.AutomationError):
        controller.disconnect_motion()
    restored = module.AutomationController(tmp_path / "state", tmp_path / "runs", tmp_path, motion_factory=MotionBus)
    assert restored.is_busy()
    different = copy.deepcopy(motion)
    different["port"] = "different-bus"
    with pytest.raises(module.AutomationError):
        restored.connect_motion(different)
    controller._motion_factory = MotionBus
    controller.connect_motion(motion)
    controller.stop()
    assert wait_finished(controller)["stop_confirmed"] is True
    restored.connect_motion(motion)
    restored.stop()
    assert wait_finished(restored)["stop_confirmed"] is True


def test_stop_driver_exception_retains_unknown_outputs_until_explicit_recovery(tmp_path):
    module = automation()
    class RaisingStopBus(MotionBus):
        def stop_all(self):
            raise OSError("serial cable lost")
    motion = module.default_experiment()["bindings"]["motion"]
    motion["port"] = "stop-bus"
    controller = module.AutomationController(tmp_path / "state", tmp_path / "runs", tmp_path, motion_factory=RaisingStopBus)
    controller.connect_motion(motion)
    with pytest.raises(module.AutomationError) as caught:
        controller.stop_motion()
    assert caught.value.code == "stop_unconfirmed"
    assert controller.snapshot()["recovery_required"]
    assert controller.is_busy()
    restored = module.AutomationController(tmp_path / "state", tmp_path / "runs", tmp_path, motion_factory=MotionBus)
    assert restored.is_busy()


@pytest.mark.parametrize("wait_action", [
    {"op": "wait", "column": 2, "seconds": 0},
    {"op": "wait_operator", "column": 2, "message": "Continue"},
])
def test_nonpositioning_wait_cannot_relabel_following_dwell_samples(wait_action):
    module = automation()
    document = module.default_experiment()
    document["program"] = [{"op": "move_to_column", "column": 1}, {"op": "immerse"},
                           wait_action, {"op": "dwell", "seconds": 10, "acquire": ["A"]}]
    with pytest.raises(module.AutomationError) as caught:
        module.compile_experiment(document)
    assert caught.value.path.startswith("program[2]")


def test_failed_connect_cleanup_retains_unknown_motion_ownership(tmp_path):
    module = automation()
    class FailedConnectBus(MotionBus):
        def connect(self):
            self.connected = True
            raise OSError("Read telemetry failed")
        def close(self):
            self.connected = False
            return {"stop_confirmed": False}
    controller = module.AutomationController(tmp_path / "state", tmp_path / "runs", tmp_path, motion_factory=FailedConnectBus)
    motion = module.default_experiment()["bindings"]["motion"]
    motion["port"] = "failed-connect-bus"
    with pytest.raises(module.AutomationError) as caught:
        controller.connect_motion(motion)
    assert caught.value.code == "stop_unconfirmed"
    assert controller.is_busy()
    restored = module.AutomationController(tmp_path / "state", tmp_path / "runs", tmp_path, motion_factory=MotionBus)
    assert restored.is_busy()


def test_manual_disconnect_recovery_does_not_rewrite_previous_simulation_manifest(tmp_path):
    module = automation()
    class UnconfirmedCloseBus(MotionBus):
        def close(self):
            self.connected = False
            return {"stop_confirmed": False}
    controller = module.AutomationController(tmp_path / "state", tmp_path / "runs", tmp_path,
                                            motion_factory=UnconfirmedCloseBus, simulation_scale=0)
    document = module.default_experiment()
    controller.start(document, "simulate", module.compile_experiment(document)["plan_hash"], "previous")
    manifest_path = Path(wait_finished(controller)["manifest_path"])
    previous = manifest_path.read_bytes()
    motion = document["bindings"]["motion"]
    motion["port"] = "manual-bus"
    controller.connect_motion(motion)
    with pytest.raises(module.AutomationError):
        controller.disconnect_motion()
    assert controller.snapshot()["manifest_path"] is None
    assert controller.snapshot()["run_id"] is None
    controller._motion_factory = MotionBus
    controller.connect_motion(motion)
    controller.stop()
    assert wait_finished(controller)["stop_confirmed"] is True
    assert manifest_path.read_bytes() == previous


def deny_file_writes(monkeypatch):
    original_open = Path.open
    def full_disk(path, mode="r", *args, **kwargs):
        if any(flag in mode for flag in "wax+"):
            raise OSError(28, "No space left on device")
        return original_open(path, mode, *args, **kwargs)
    monkeypatch.setattr(Path, "open", full_disk)


def test_disk_full_cannot_skip_peer_termination_or_motion_stop(tmp_path, monkeypatch):
    module = automation()
    factory = CollectorFactory(hold_gate=True)
    class ObservableStopBus(MotionBus):
        def stop_all(self):
            self.stationary = True
            return super().stop_all()
    controller = module.AutomationController(tmp_path / "state", tmp_path / "runs", tmp_path,
                                            process_factory=factory, motion_factory=ObservableStopBus)
    document = live_document(module)
    document["bindings"]["motion"]["port"] = "diskfull-motion"
    document["safety"]["motion_policy_confirmed"] = True
    document["program"].insert(0, {"op": "move_to_column", "column": 1})
    controller.connect_motion(document["bindings"]["motion"])
    controller.reference_motion(True)
    controller.enable_motion("X")
    controller.enable_motion("Z")
    controller.start(document, "live", module.compile_experiment(document)["plan_hash"], "diskfull")
    deadline = time.monotonic() + 2
    while len(factory.children) < 2 and time.monotonic() < deadline:
        time.sleep(0.005)
    assert len(factory.children) == 2
    monkeypatch.setattr(module, "STOP_TIMEOUT_S", 0)
    deny_file_writes(monkeypatch)
    controller.stop()
    deadline = time.monotonic() + 2
    while controller._thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.005)
    assert not controller._thread.is_alive()
    assert all(child.returncode is not None for child in factory.children)
    assert controller._motion.stationary is True
    assert controller.snapshot()["state"] == "error"
    assert controller.is_busy()
    monkeypatch.undo()
    restored = module.AutomationController(tmp_path / "state", tmp_path / "runs", tmp_path)
    assert restored.is_busy()


def test_recovery_disk_full_attempts_every_board_and_motion_even_if_orphan_signal_fails(tmp_path, monkeypatch):
    module = automation()
    document = live_document(module)
    document["bindings"]["motion"]["port"] = "recovery-motion"
    document["program"].insert(0, {"op": "move_to_column", "column": 1})
    run_dir = tmp_path / "runs" / "live" / "interrupted"
    command = run_dir / "step-0003" / "board-A" / "commands.txt"
    command.parent.mkdir(parents=True)
    command.write_text("")
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "automation.json").write_text(json.dumps({"experiment": document,
        "state": {"state": "running", "mode": "live", "stop_confirmed": False, "run_id": "interrupted",
                  "manifest_path": str(run_dir / "manifest.json")}}))
    board_stops = []
    def stop_board(address):
        board_stops.append(address)
        return {"stop_confirmed": True}
    class ObservableStopBus(MotionBus):
        def stop_all(self):
            self.stationary = True
            return super().stop_all()
    controller = module.AutomationController(state_dir, tmp_path / "runs", tmp_path,
                                            motion_factory=ObservableStopBus, board_stop_factory=stop_board)
    controller.connect_motion(document["bindings"]["motion"])
    deny_file_writes(monkeypatch)
    controller.stop()
    deadline = time.monotonic() + 2
    while controller._thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.005)
    assert not controller._thread.is_alive()
    assert board_stops == ["board-a", "board-b"]
    assert controller._motion.stationary is True
    assert controller.is_busy()
    assert controller.snapshot()["recovery_required"] is True


def test_failed_final_audit_event_cannot_leave_manifest_claiming_completion(tmp_path, monkeypatch):
    module = automation()
    controller = module.AutomationController(tmp_path / "state", tmp_path / "runs", tmp_path, simulation_scale=0)
    document = module.default_experiment()
    document["program"] = [{"op": "wait", "seconds": 0}]
    original_open = Path.open
    failure_injected = []
    def fail_final_event(path, mode="r", *args, **kwargs):
        if path.name == "events.jsonl" and mode == "a" and controller._state["state"] == "completed" and not failure_injected:
            failure_injected.append(True)
            raise OSError(28, "Final audit event could not be written")
        return original_open(path, mode, *args, **kwargs)
    monkeypatch.setattr(Path, "open", fail_final_event)
    controller.start(document, "simulate", module.compile_experiment(document)["plan_hash"], "audit-end")
    state = wait_finished(controller)
    assert failure_injected
    assert state["state"] == "error"
    manifest = json.loads(Path(state["manifest_path"]).read_text())
    assert manifest["state"] == "error"
    assert manifest["error"]["code"] == "audit_failed"
