import json
import threading
from types import SimpleNamespace

from test_automation_limits import controller_fixture


def test_manual_motor_reads_do_not_wait_for_the_page_state_lock(tmp_path):
    controller, document, serial, drivers = controller_fixture(tmp_path)
    bus = drivers[0]
    before = controller.events()["last_seq"]
    finished = threading.Event()
    results = []

    def read_axis():
        try:
            results.append(bus._read_axis("Z"))
        finally:
            finished.set()

    worker = threading.Thread(target=read_axis)
    try:
        with controller._lock:
            worker.start()
            assert finished.wait(1), "A page-state lock must not block serial feedback"
        worker.join(2)
        assert results[0]["telemetry_valid"]
        wire = [record for record in controller.events(before)["events"] if record["event"] == "motion_wire"]
        assert [record["detail"]["opcode"] for record in wire] == [0x36, 0x36, 0x35, 0x35, 0x3A, 0x3A]
        assert all(record["synthetic"] is False and record["run_id"] is None for record in wire)
    finally:
        worker.join(2)
        controller.disconnect_motion()


def test_unowned_wire_events_do_not_become_part_of_a_later_live_run(tmp_path):
    controller, document, serial, drivers = controller_fixture(tmp_path)
    controller.events()
    drivers[0]._callback({"kind": "wire", "direction": "tx", "opcode": 0x36,
                          "host_unix_s": 123.0, "marker": "before-live"})
    controller._run_dir = tmp_path / "later-live"
    controller._run_dir.mkdir()
    controller._state.update(mode="live", state="running", run_id="later-live")
    controller._owns_motion = True
    controller._thread = SimpleNamespace(is_alive=lambda: True)
    try:
        record = next(item for item in controller.events()["events"]
                      if item.get("detail", {}).get("marker") == "before-live")
        assert record["run_id"] is None and record["synthetic"] is False
        assert not (controller._run_dir / "events.jsonl").exists()
    finally:
        controller._thread = None
        controller._state.update(state="idle", mode=None)
        controller._owns_motion = False
        controller._run_dir = None
        controller.disconnect_motion()


def test_owned_live_wire_events_keep_the_existing_synchronous_audit(tmp_path):
    controller, document, serial, drivers = controller_fixture(tmp_path)
    controller.events()
    controller._run_dir = tmp_path / "live-audit"
    controller._run_dir.mkdir()
    controller._state.update(mode="live", state="running", run_id="live-audit")
    controller._owns_motion = True
    controller._thread = SimpleNamespace(is_alive=lambda: True)
    try:
        drivers[0]._callback({"kind": "wire", "direction": "tx", "opcode": 0x36, "marker": "live"})
        records = [json.loads(line) for line in (controller._run_dir / "events.jsonl").read_text().splitlines()]
        assert records[-1]["detail"]["marker"] == "live"
        assert records[-1]["run_id"] == "live-audit" and records[-1]["synthetic"] is False
    finally:
        controller._thread = None
        controller._state.update(state="idle", mode=None)
        controller._owns_motion = False
        controller._run_dir = None
        controller.disconnect_motion()


def test_pending_wire_overflow_remains_visible_to_event_cursors(tmp_path):
    controller, document, serial, drivers = controller_fixture(tmp_path)
    before = controller.events()["last_seq"]
    try:
        for marker in range(2050):
            drivers[0]._callback({"kind": "wire", "marker": marker})
        result = controller.events(before)
        assert len(result["events"]) == 2048
        assert result["events"][0]["detail"]["marker"] == 2
        assert result["last_seq"] == before + 2050
        assert result["truncated"] is True
        assert controller.events(result["last_seq"])["truncated"] is False
    finally:
        controller.disconnect_motion()
