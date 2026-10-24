from __future__ import annotations

import argparse
import asyncio
import csv
import json
import time
from pathlib import Path

import pytest

from pa_host import collect_ble, v52_ble
from pa_host.record import CSV_COLUMNS
from test_v52_ble import fake_bleak


def run_args(tmp_path):
    return argparse.Namespace(
        address="test", settings=json.dumps({"duration_s": 10, "analysis_channel": 2}),
        out=tmp_path / "raw.csv", all_channels=tmp_path / "all-channels.csv",
        monitor=tmp_path / "monitor.csv", audit=tmp_path / "audit.jsonl",
        raw_log=tmp_path / "raw.log", cmd_file=tmp_path / "commands.txt",
        state_file=tmp_path / "state.json", run_token="test-token",
    )


def csv_rows(path):
    with path.open(newline="") as handle:
        return list(csv.DictReader(handle))


def test_selected_channel_mask_controls_real_start_and_expected_samples(collector_env, monkeypatch):
    fake, args = collector_env
    monkeypatch.setattr(collect_ble, "NO_DATA_MIN_SECONDS", 10.0)
    args.channel_mask = 5
    args.settings = {"duration_s": 10, "analysis_channel": 1}
    fake.start_lines = ["D,50,1000,1,100,1000000,4", "D,51,1000,3,100,2000000,4"]
    assert asyncio.run(collect_ble.collect(args)) == 0
    assert "START 5" in fake.instances[0].commands
    assert "START 15" not in fake.instances[0].commands
    assert {row["channel"] for row in csv_rows(args.all_channels)} == {"1", "3"}


@pytest.mark.parametrize("mask", [0, -1, 16, True])
def test_invalid_channel_mask_never_opens_ble(collector_env, mask):
    fake, args = collector_env
    args.channel_mask = mask
    assert asyncio.run(collect_ble.collect(args)) == 1
    assert not fake.instances


def test_analysis_channel_must_be_in_sampling_mask(collector_env):
    fake, args = collector_env
    args.channel_mask = 1
    assert asyncio.run(collect_ble.collect(args)) == 1
    assert not fake.instances


def test_two_board_gate_waits_for_matching_token(collector_env, monkeypatch):
    fake, args = collector_env
    monkeypatch.setattr(collect_ble, "NO_DATA_MIN_SECONDS", 10.0)
    args.start_gate = args.out.parent / "start-gate.json"

    async def scenario():
        task = asyncio.create_task(collect_ble.collect(args))
        try:
            for attempt in range(1000):
                if args.state_file.exists() and json.loads(args.state_file.read_text()).get("config_confirmed"):
                    break
                await asyncio.sleep(0.001)
            assert "START 15" not in fake.instances[0].commands
            args.start_gate.write_text(json.dumps({"run_token": "other-run"}))
            await asyncio.sleep(0.005)
            assert "START 15" not in fake.instances[0].commands
            args.start_gate.write_text(json.dumps({"run_token": args.run_token}))
            return await task
        finally:
            if not task.done():
                args.cmd_file.write_text("STOP\n")
                await task

    assert asyncio.run(scenario()) == 0


def test_settings_input_cannot_alias_state_output(collector_env):
    fake, args = collector_env
    args.settings = args.state_file
    original = '{"duration_s":10}'
    args.settings.write_text(original)
    assert asyncio.run(collect_ble.collect(args)) == 1
    assert args.settings.read_text() == original
    assert not fake.instances


def test_unreleased_gate_times_out_without_start_and_confirms_stop(collector_env, monkeypatch):
    fake, args = collector_env
    args.start_gate = args.out.parent / "gate.json"
    monkeypatch.setattr(collect_ble, "START_GATE_TIMEOUT_S", 0.005)
    assert asyncio.run(collect_ble.collect(args)) == 1
    assert not any(command.startswith("START") for command in fake.instances[0].commands)
    result = json.loads(args.state_file.read_text())
    assert result["stop_confirmed"]
    assert "gate" in result["error"]


def test_stop_during_gate_wait_never_starts(collector_env):
    fake, args = collector_env
    args.start_gate = args.out.parent / "gate.json"

    async def scenario():
        task = asyncio.create_task(collect_ble.collect(args))
        for attempt in range(1000):
            if args.state_file.exists() and json.loads(args.state_file.read_text()).get("config_confirmed"):
                break
            await asyncio.sleep(0.001)
        args.cmd_file.write_text("STOP\n")
        return await asyncio.wait_for(task, 1)

    assert asyncio.run(scenario()) == 3
    assert not any(command.startswith("START") for command in fake.instances[0].commands)
    assert json.loads(args.state_file.read_text())["stop_confirmed"]


def test_disabled_channel_frame_is_rejected(collector_env):
    fake, args = collector_env
    args.channel_mask = 2
    assert asyncio.run(collect_ble.collect(args)) == 1
    assert "channel_mask" in json.loads(args.state_file.read_text())["error"]


@pytest.fixture
def collector_env(fake_bleak, monkeypatch, tmp_path):
    monkeypatch.setattr(v52_ble, "COMMAND_TIMEOUT_S", 1.0)
    monkeypatch.setattr(collect_ble, "POLL_INTERVAL_S", 0.001)
    monkeypatch.setattr(collect_ble, "MAX_RUN_SECONDS", 0.025)
    monkeypatch.setattr(collect_ble, "NO_DATA_MIN_SECONDS", 0.015)
    monkeypatch.setattr(collect_ble, "NO_DATA_PERIOD_MULTIPLIER", 0.01)
    fake_bleak.start_lines = [
        "D,50,1000,1,100,1000000,4", "D,51,1000,2,100,-1000000,4",
        "D,52,1000,3,100,2000000,4", "D,53,1000,4,100,3000000,4",
        "M,VDD,0,2000,3000000,0", "M,TEMP,0,5000,25000,0",
    ]
    return fake_bleak, run_args(tmp_path)


def test_collector_preserves_all_channels_and_global_selected_sequence(collector_env, monkeypatch):
    fake, args = collector_env
    monkeypatch.setattr(collect_ble, "NO_DATA_MIN_SECONDS", 10.0)
    assert asyncio.run(collect_ble.collect(args)) == 0
    state = json.loads(args.state_file.read_text())
    assert state["phase"] == "completed"
    assert state["run_token"] == "test-token"
    assert state["config_confirmed"] and state["stop_confirmed"]
    assert state["samples_by_channel"] == {"1": 1, "2": 1, "3": 1, "4": 1}
    selected = csv_rows(args.out)
    assert list(selected[0]) == CSV_COLUMNS
    assert selected[0]["seq"] == "51"
    assert selected[0]["fa_fw"] == "-1000000"
    assert selected[0]["epoch"] == ""
    assert selected[0]["sat"] == selected[0]["ovf"] == "0"
    assert len(csv_rows(args.all_channels)) == 4
    assert [row["unit"] for row in csv_rows(args.monitor)] == ["uV", "milliC"]
    assert fake.instances[0].commands[-2:] == ["STOP", "STATUS"]
    assert "D,51," in args.raw_log.read_text()


def test_collector_user_stop_ack_is_distinct_exit_and_ignores_other_commands(collector_env):
    fake, args = collector_env

    def stop(client):
        args.cmd_file.write_text("START 15\nDAC A 0\nSTOP\n")

    fake.start_hook = stop
    assert asyncio.run(collect_ble.collect(args)) == 3
    state = json.loads(args.state_file.read_text())
    assert state["phase"] == "stopped" and state["stop_confirmed"]
    assert fake.instances[0].commands.count("START 15") == 1
    assert "DAC A 0" not in fake.instances[0].commands


@pytest.mark.parametrize("extra, message", [
    (["D,55,1001,1,100,1000000,4"], "sequence"),
    (["D,51,1001,1,100,1000000,4"], "sequence"),
    (["D,54,1001,1,100,1000000,6"], "overflow"),
    (["E,FIFO,1"], "event"),
    (["D,54,1001,5,100,1000000,4"], "range"),
    (["unknown frame"], "unknown"),
    (["x" * 1100], "frame"),
])
def test_collector_faults_preserve_files_and_attempt_stop(collector_env, extra, message):
    fake, args = collector_env
    fake.start_lines += extra
    assert asyncio.run(collect_ble.collect(args)) == 1
    state = json.loads(args.state_file.read_text())
    assert state["phase"] == "error" and state["stop_confirmed"]
    assert message in state["error"].lower()
    assert len(csv_rows(args.all_channels)) >= 4
    assert fake.instances[0].commands[-1] == "STOP"


def test_collector_no_data_fails_and_stops(collector_env):
    fake, args = collector_env
    fake.start_lines = []
    assert asyncio.run(collect_ble.collect(args)) == 1
    state = json.loads(args.state_file.read_text())
    assert "no data" in state["error"].lower()
    assert state["stop_confirmed"]


def test_collector_missing_channel_fails_even_when_other_channel_streams(collector_env, monkeypatch):
    fake, args = collector_env
    monkeypatch.setattr(collect_ble, "NO_DATA_MIN_SECONDS", 10.0)
    fake.start_lines = fake.start_lines[:3]
    assert asyncio.run(collect_ble.collect(args)) == 1
    assert "channel" in json.loads(args.state_file.read_text())["error"].lower()


def test_guided_missing_channel_and_sequence_gaps_preserve_other_channels(collector_env, monkeypatch):
    fake, args = collector_env
    args.fault_policy = "guided"
    args.attempt_id = "attempt-2"
    monkeypatch.setattr(collect_ble, "NO_DATA_MIN_SECONDS", 10)
    fake.start_lines = ["D,50,1000,1,100,1000000,4", "D,53,1001,3,100,2000000,6"]
    assert asyncio.run(collect_ble.collect(args)) == 0
    state = json.loads(args.state_file.read_text())
    assert state["attempt_id"] == "attempt-2"
    assert state["stop_confirmed"]
    assert state["channel_quality"]["2"] == "missing"
    assert state["channel_quality"]["3"] == "flagged"
    assert len(csv_rows(args.all_channels)) == 2
    assert "sequence_gap" in args.audit.read_text()


def test_guided_gate_expired_before_start_never_drives_outputs(collector_env):
    fake, args = collector_env
    args.fault_policy = "guided"
    args.attempt_id = "expired-attempt"
    args.start_gate = args.out.parent / "gate.json"
    args.start_gate.write_text(json.dumps({"run_token": args.run_token, "attempt_id": args.attempt_id,
                                         "deadline_unix_s": time.time() - 1}))
    assert asyncio.run(collect_ble.collect(args)) == 3
    assert not any(command.startswith("START") for command in fake.instances[0].commands)
    assert json.loads(args.state_file.read_text())["stop_confirmed"]


def test_collector_duration_is_measured_from_successful_start_ack(collector_env, monkeypatch):
    fake, args = collector_env
    monkeypatch.setattr(collect_ble, "NO_DATA_MIN_SECONDS", 10)
    fake.start_hook = lambda client: time.sleep(0.035)
    assert asyncio.run(collect_ble.collect(args)) == 0
    state = json.loads(args.state_file.read_text())
    assert state["finished_unix_s"] - state["start_ack_unix_s"] >= 0.02
    assert state["start_ack_monotonic_s"] > 0


def test_guided_pending_start_is_cancelled_at_shared_deadline(collector_env, monkeypatch):
    fake, args = collector_env
    args.fault_policy = "guided"
    args.attempt_id = "late-attempt"
    args.start_gate = args.out.parent / "gate.json"
    args.start_gate.write_text(json.dumps({"run_token": args.run_token, "attempt_id": args.attempt_id,
                                         "deadline_unix_s": time.time() + 0.02}))
    original = fake.write_gatt_char

    async def delayed_start(client, characteristic, payload, response):
        if payload == b"START 15\n":
            await asyncio.sleep(0.04)
        await original(client, characteristic, payload, response)

    monkeypatch.setattr(fake, "write_gatt_char", delayed_start)
    assert asyncio.run(collect_ble.collect(args)) == 3
    assert not any(command.startswith("START") for command in fake.instances[0].commands)
    state = json.loads(args.state_file.read_text())
    assert "start_ack_unix_s" not in state
    assert state["stop_confirmed"]


def test_collector_start_timeout_still_attempts_stop(collector_env, monkeypatch):
    fake, args = collector_env
    monkeypatch.setattr(v52_ble, "COMMAND_TIMEOUT_S", 0.25)
    fake.responses["START 15"] = []
    assert asyncio.run(collect_ble.collect(args)) == 1
    state = json.loads(args.state_file.read_text())
    assert "START" in state["error"]
    assert state["stop_confirmed"]
    assert fake.instances[0].commands[-1] == "STOP"


def test_collector_stop_timeout_is_not_reported_as_success(collector_env, monkeypatch):
    fake, args = collector_env
    monkeypatch.setattr(collect_ble, "NO_DATA_MIN_SECONDS", 10.0)
    monkeypatch.setattr(v52_ble, "COMMAND_TIMEOUT_S", 0.25)
    fake.start_hook = lambda client: fake.responses.update({"STOP": []})
    assert asyncio.run(collect_ble.collect(args)) == 1
    state = json.loads(args.state_file.read_text())
    assert state["phase"] == "error"
    assert not state["stop_confirmed"]
    assert "STOP" in state["error"]


def test_collector_disconnect_preserves_output_and_reports_unconfirmed_stop(collector_env):
    fake, args = collector_env

    def disconnect(client):
        client.is_connected = False
        client.disconnected_callback(client)

    fake.start_hook = disconnect
    assert asyncio.run(collect_ble.collect(args)) == 1
    state = json.loads(args.state_file.read_text())
    assert "disconnect" in state["error"].lower()
    assert not state["stop_confirmed"]
    assert len(csv_rows(args.all_channels)) == 4


def test_collector_bad_config_never_starts_and_preserves_current_run_token(collector_env):
    fake, args = collector_env
    fake.responses["STATUS"] = ["OK STATUS mask=0"]
    assert asyncio.run(collect_ble.collect(args)) == 1
    state = json.loads(args.state_file.read_text())
    assert not state["config_confirmed"]
    assert state["run_token"] == args.run_token
    assert "START 15" not in fake.instances[0].commands


def test_collector_stop_requested_during_configuration_never_starts(collector_env):
    fake, args = collector_env
    args.cmd_file.write_text("STOP\n")
    assert asyncio.run(collect_ble.collect(args)) == 3
    assert "START 15" not in fake.instances[0].commands
    assert json.loads(args.state_file.read_text())["stop_confirmed"]


def test_collector_discards_preconfiguration_samples(collector_env, monkeypatch):
    fake, args = collector_env
    monkeypatch.setattr(collect_ble, "NO_DATA_MIN_SECONDS", 10.0)
    fake.responses["STOP"] = ["D,1,50,2,100,99999999,4", "OK STOP"]
    fake.start_hook = lambda client: fake.responses.pop("STOP")
    assert asyncio.run(collect_ble.collect(args)) == 0
    assert [row["seq"] for row in csv_rows(args.out)] == ["51"]


def test_collector_does_not_overwrite_existing_files(collector_env):
    fake, args = collector_env
    args.out.write_text("existing\n")
    assert asyncio.run(collect_ble.collect(args)) == 1
    assert args.out.read_text() == "existing\n"
    assert not fake.instances


def test_collector_alias_state_path_never_overwrites_raw_file(collector_env):
    fake, args = collector_env
    args.out.write_text("existing\n")
    args.state_file = args.out
    assert asyncio.run(collect_ble.collect(args)) == 1
    assert args.out.read_text() == "existing\n"
    assert not fake.instances


def test_collector_error_during_final_stop_is_not_hidden(collector_env, monkeypatch):
    fake, args = collector_env
    monkeypatch.setattr(collect_ble, "NO_DATA_MIN_SECONDS", 10.0)
    fake.start_hook = lambda client: fake.responses.update({"STOP": ["E,FIFO,1", "OK STOP"]})
    assert asyncio.run(collect_ble.collect(args)) == 1
    state = json.loads(args.state_file.read_text())
    assert state["stop_confirmed"] and state["phase"] == "error"
    assert "event" in state["error"]


def test_collector_final_status_detects_tx_drop_without_next_sample(collector_env, monkeypatch):
    fake, args = collector_env
    monkeypatch.setattr(collect_ble, "NO_DATA_MIN_SECONDS", 10.0)
    original = fake.write_gatt_char

    async def alter_final_status(client, characteristic, payload, response):
        if payload == b"STATUS\n" and "START 15" in client.commands:
            client.emit([f"C,{channel},50,2,0,0,1,0" for channel in range(1, 5)]
                        + [f"A,{name},{code},{code * 375}" for name, code in client.dacs.items()]
                        + ["OK STATUS mask=0 period=0 fault=0 io=0 seq=54 overflow=0 tx_drop=1 ble=1"])
            return
        await original(client, characteristic, payload, response)

    monkeypatch.setattr(fake, "write_gatt_char", alter_final_status)
    assert asyncio.run(collect_ble.collect(args)) == 1
    assert "tx_drop" in json.loads(args.state_file.read_text())["error"]


def test_collector_detects_lost_first_sample_against_confirmed_status(collector_env, monkeypatch):
    fake, args = collector_env
    monkeypatch.setattr(collect_ble, "NO_DATA_MIN_SECONDS", 10.0)
    fake.start_lines = [
        line.replace(f"D,{50 + index},", f"D,{51 + index},") if line.startswith("D,") else line
        for index, line in enumerate(fake.start_lines)
    ]
    assert asyncio.run(collect_ble.collect(args)) == 1
    assert "sequence" in json.loads(args.state_file.read_text())["error"]


def test_collector_detects_lost_final_sample_against_stopped_status(collector_env, monkeypatch):
    fake, args = collector_env
    monkeypatch.setattr(collect_ble, "NO_DATA_MIN_SECONDS", 10.0)
    fake.start_hook = lambda client: setattr(client, "sequence", client.sequence + 1)
    assert asyncio.run(collect_ble.collect(args)) == 1
    assert "sequence" in json.loads(args.state_file.read_text())["error"]


def test_collector_cli_loads_settings_json_file(collector_env, monkeypatch):
    fake, args = collector_env
    monkeypatch.setattr(collect_ble, "NO_DATA_MIN_SECONDS", 10.0)
    settings_path = args.out.parent / "ble-settings.json"
    settings_path.write_text(args.settings, encoding="utf-8")
    arguments = ["--address", args.address, "--settings", str(settings_path),
                 "--run-token", args.run_token]
    for name in ("out", "all_channels", "monitor", "audit", "raw_log", "cmd_file", "state_file"):
        arguments.extend(["--" + name.replace("_", "-"), str(getattr(args, name))])
    assert collect_ble.main(arguments) == 0
    state = json.loads(args.state_file.read_text())
    assert state["phase"] == "completed"
    assert state["config"]["settings"]["analysis_channel"] == 2
    assert state["stop_confirmed"]
    assert "START 15" in fake.instances[0].commands


def test_collector_failure_also_explains_error_on_stderr(collector_env, capsys):
    fake, args = collector_env
    args.settings = "not JSON"
    assert asyncio.run(collect_ble.collect(args)) == 1
    state = json.loads(args.state_file.read_text())
    assert state["error"] in capsys.readouterr().err
    assert not fake.instances
