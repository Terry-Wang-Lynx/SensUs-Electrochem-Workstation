"""Four-channel V5.2 acquisition with verified configuration and STOP."""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import math
import os
import signal
import sys
import time
from contextlib import ExitStack
from pathlib import Path
from typing import Any

from . import v52_ble
from .record import CSV_COLUMNS

ALL_CHANNEL_COLUMNS = ["host_unix_s", "seq", "dev_ms", "channel", "counts", "fa_fw", "flags"]
MONITOR_COLUMNS = ["host_unix_s", "quantity", "channel", "counts", "value", "flags", "unit"]
POLL_INTERVAL_S = 0.05
MAX_RUN_SECONDS = 3600.0
NO_DATA_MIN_SECONDS = 5.0
NO_DATA_PERIOD_MULTIPLIER = 3.0
START_GATE_TIMEOUT_S = 60.0


def _write_state(path: Path, state: dict[str, Any]):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _stop_requested(path: Path, stop_event: asyncio.Event) -> bool:
    if stop_event.is_set():
        return True
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                if line.strip() == "STOP":
                    return True
    except FileNotFoundError:
        pass
    return False


async def _wait_start_gate(args, session, stop_event):
    gate = getattr(args, "start_gate", None)
    if gate is None:
        return not _stop_requested(Path(args.cmd_file), stop_event)
    deadline = time.monotonic() + START_GATE_TIMEOUT_S
    while time.monotonic() < deadline:
        session.check_health()
        if _stop_requested(Path(args.cmd_file), stop_event):
            return False
        try:
            value = json.loads(Path(gate).read_text(encoding="utf-8"))
            if isinstance(value, dict) and value.get("run_token") == args.run_token:
                if getattr(args, "fault_policy", "strict") == "guided":
                    if value.get("attempt_id") != args.attempt_id:
                        await asyncio.sleep(POLL_INTERVAL_S)
                        continue
                    shared_deadline = value.get("deadline_unix_s")
                    if shared_deadline is not None:
                        if (isinstance(shared_deadline, bool) or not isinstance(shared_deadline, (int, float))
                                or not math.isfinite(shared_deadline)):
                            raise RuntimeError("invalid shared column deadline")
                        if time.time() >= shared_deadline:
                            return False
                        args.column_deadline_unix_s = shared_deadline
                return True
        except (OSError, ValueError):
            pass
        await asyncio.sleep(POLL_INTERVAL_S)
    raise TimeoutError("matching start gate was not released within deadline")


def _shared_column_deadline(args):
    deadline = getattr(args, "column_deadline_unix_s", None)
    gate = getattr(args, "start_gate", None)
    if gate is not None:
        try:
            value = json.loads(Path(gate).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return deadline
        if (not isinstance(value, dict) or value.get("run_token") != args.run_token
                or value.get("attempt_id") != args.attempt_id):
            raise RuntimeError("shared deadline gate does not belong to this attempt")
        supplied = value.get("deadline_unix_s")
        if supplied is not None:
            if (isinstance(supplied, bool) or not isinstance(supplied, (int, float)) or not math.isfinite(supplied)):
                raise RuntimeError("invalid shared column deadline")
            deadline = supplied if deadline is None else min(deadline, supplied)
            args.column_deadline_unix_s = deadline
    return deadline


async def _start_capture(args, session, channel_mask, stop_event):
    if getattr(args, "fault_policy", "strict") != "guided":
        await session.command(f"START {channel_mask}")
        return
    pending = asyncio.create_task(session.command(f"START {channel_mask}"))
    try:
        while not pending.done():
            deadline = _shared_column_deadline(args)
            if (_stop_requested(Path(args.cmd_file), stop_event)
                    or deadline is not None and time.time() >= deadline):
                raise asyncio.CancelledError()
            await asyncio.wait({pending}, timeout=POLL_INTERVAL_S)
        await pending
        deadline = _shared_column_deadline(args)
        if deadline is not None and time.time() >= deadline:
            raise asyncio.CancelledError()
    finally:
        if not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)


async def collect(args: argparse.Namespace, stop_event: asyncio.Event | None = None) -> int:
    stop_event = stop_event or asyncio.Event()
    state_path = Path(args.state_file)
    state = {"run_token": args.run_token, "phase": "configuring", "config_confirmed": False,
             "config": None, "error": None, "stop_confirmed": False,
             "samples_by_channel": {str(channel): 0 for channel in range(1, 5)}}
    guided = getattr(args, "fault_policy", "strict") == "guided"
    if guided:
        state.update(attempt_id=getattr(args, "attempt_id", None), error_kind=None,
                     channel_quality={str(channel): "waiting" for channel in range(1, 5)})
    stack = ExitStack()
    capture = False
    previous_sequence = None
    latest_by_channel = {}
    session = None
    exit_code = 1
    audit_handle = None
    state_path_safe = False

    def audit(event: str, **details):
        if audit_handle is not None:
            audit_handle.write(json.dumps({"host_unix_s": time.time(), "run_token": args.run_token,
                                           "event": event, **details}, ensure_ascii=False) + "\n")
            audit_handle.flush()

    def update_state(**changes):
        state.update(changes)
        if state_path_safe:
            _write_state(state_path, state)

    try:
        paths = [Path(getattr(args, name)) for name in ("out", "all_channels", "monitor", "audit", "raw_log")]
        protected_paths = [*paths, state_path, Path(args.cmd_file)]
        if isinstance(args.settings, Path):
            protected_paths.append(args.settings)
        if getattr(args, "start_gate", None) is not None:
            protected_paths.append(Path(args.start_gate))
        if len({path.resolve() for path in protected_paths}) != len(protected_paths):
            raise ValueError("collector output, state, and command paths must be distinct")
        state_path_safe = True
        update_state()
        if not args.run_token:
            raise ValueError("run_token is required")
        if guided and not getattr(args, "attempt_id", None):
            raise ValueError("guided collection requires an attempt_id")
        if isinstance(args.settings, Path):
            supplied_settings = json.loads(args.settings.read_text(encoding="utf-8"))
        elif isinstance(args.settings, str):
            supplied_settings = json.loads(args.settings)
        else:
            supplied_settings = args.settings
        settings = v52_ble.validate_settings(supplied_settings)
        channel_mask = getattr(args, "channel_mask", 15)
        if isinstance(channel_mask, bool) or not isinstance(channel_mask, int) or not 1 <= channel_mask <= 15:
            raise ValueError("channel_mask must be an integer from 1 to 15")
        expected_channels = [channel for channel in range(1, 5) if channel_mask & (1 << (channel - 1))]
        if settings["analysis_channel"] not in expected_channels:
            raise ValueError("analysis_channel must be enabled in channel_mask")
        update_state(channel_mask=channel_mask)
        handles = []
        for path in paths:
            path.parent.mkdir(parents=True, exist_ok=True)
            handles.append(stack.enter_context(path.open("x", encoding="utf-8", newline="")))
        selected_handle, all_handle, monitor_handle, audit_handle, raw_handle = handles
        selected_writer, all_writer, monitor_writer = [csv.writer(handle) for handle in handles[:3]]
        for writer, columns, handle in ((selected_writer, CSV_COLUMNS, selected_handle),
                                        (all_writer, ALL_CHANNEL_COLUMNS, all_handle),
                                        (monitor_writer, MONITOR_COLUMNS, monitor_handle)):
            writer.writerow(columns)
            handle.flush()

        def on_line(line: str, host_unix_s: float):
            nonlocal previous_sequence
            raw_handle.write(f"{host_unix_s:.6f} {line}\n")
            raw_handle.flush()
            sample = v52_ble.Sample.parse(line)
            measurement = v52_ble.Monitor.parse(line)
            if sample is not None:
                if not capture:
                    return
                if sample.channel not in expected_channels:
                    raise RuntimeError("sample received from a channel outside channel_mask")
                all_writer.writerow([f"{host_unix_s:.6f}", sample.sequence, sample.uptime_ms,
                                     sample.channel, sample.counts, sample.current_fa, sample.flags])
                all_handle.flush()
                if sample.channel == settings["analysis_channel"]:
                    selected_writer.writerow([f"{host_unix_s:.6f}", sample.sequence, sample.uptime_ms,
                                              sample.counts, sample.current_fa, sample.channel - 1, 1,
                                              (sample.flags >> 1) & 1, sample.flags & 1, "", "", "", ""])
                    selected_handle.flush()
                state["samples_by_channel"][str(sample.channel)] += 1
                latest_by_channel[sample.channel] = time.monotonic()
                if previous_sequence is not None and sample.sequence != previous_sequence + 1:
                    if guided:
                        audit("sequence_gap", previous_seq=previous_sequence, sequence=sample.sequence, channel=sample.channel)
                    else:
                        raise RuntimeError(f"global sequence discontinuity: {previous_sequence} -> {sample.sequence}")
                previous_sequence = sample.sequence
                if guided:
                    state["channel_quality"][str(sample.channel)] = "flagged" if sample.flags & 3 else "valid"
                    if sample.flags & 3:
                        audit("channel_quality", channel=sample.channel, flags=sample.flags)
                elif sample.flags & 2:
                    raise RuntimeError("FIFO overflow flag in current data")
            elif measurement is not None:
                if capture:
                    monitor_writer.writerow([f"{host_unix_s:.6f}", measurement.quantity, measurement.channel,
                                             measurement.counts, measurement.value, measurement.flags, measurement.unit])
                    monitor_handle.flush()
            elif line.startswith("E,"):
                if capture:
                    raise RuntimeError(f"V5.2 asynchronous event: {line}")
            elif not line.startswith(("OK ", "ERR ", "C,", "A,")):
                raise RuntimeError(f"unknown BLE frame: {line}")

        async with v52_ble.BleSession(args.address, on_line=on_line) as session:
            try:
                audit("configuring", settings=settings, address=args.address)
                config = await v52_ble.configure_session(session, settings)
                previous_sequence = config["status"]["seq"] - 1
                update_state(config_confirmed=True, config=config)
                audit("config_confirmed", config=config)
                if not await _wait_start_gate(args, session, stop_event):
                    exit_code = 3
                else:
                    capture = True
                    update_state(start_requested_unix_s=time.time())
                    await _start_capture(args, session, channel_mask, stop_event)
                    session.check_health()
                    started_at = time.monotonic()
                    acknowledgement = time.time()
                    update_state(phase="running", start_ack_unix_s=acknowledgement,
                                 start_ack_monotonic_s=started_at)
                    audit("started", start_requested_unix_s=state["start_requested_unix_s"],
                          start_ack_unix_s=acknowledgement, start_ack_monotonic_s=started_at)
                    duration = MAX_RUN_SECONDS if settings["adaptive_stop"] else min(settings["duration_s"], MAX_RUN_SECONDS)
                    deadline = started_at + duration
                    shared_deadline = getattr(args, "column_deadline_unix_s", None)
                    if guided and shared_deadline is not None:
                        deadline = min(deadline, started_at + max(0, shared_deadline - acknowledgement))
                    no_data_timeout = max(NO_DATA_MIN_SECONDS,
                                          v52_ble.period_seconds(settings["sens_period_code"]) * NO_DATA_PERIOD_MULTIPLIER)
                    while True:
                        session.check_health()
                        if _stop_requested(Path(args.cmd_file), stop_event):
                            exit_code = 3
                            break
                        now = time.monotonic()
                        if guided:
                            shared_deadline = _shared_column_deadline(args)
                            if shared_deadline is not None:
                                deadline = min(deadline, now + max(0, shared_deadline - time.time()))
                        missing = [channel for channel in expected_channels
                                   if now - latest_by_channel.get(channel, started_at) > no_data_timeout]
                        if missing:
                            if guided:
                                for channel in missing:
                                    if state["channel_quality"][str(channel)] != "missing":
                                        state["channel_quality"][str(channel)] = "missing"
                                        audit("channel_gap", channel=channel, reason="no_data")
                                update_state()
                            else:
                                raise RuntimeError(f"no data from channel(s) {missing} within {no_data_timeout:.3f}s")
                        if now >= deadline:
                            missing = [channel for channel in expected_channels if channel not in latest_by_channel]
                            if missing:
                                if guided:
                                    for channel in missing:
                                        state["channel_quality"][str(channel)] = "missing"
                                        audit("channel_gap", channel=channel, reason="no_samples_before_deadline")
                                else:
                                    raise RuntimeError(f"no data from channel(s) {missing} before run deadline")
                            exit_code = 0
                            break
                        await asyncio.sleep(min(POLL_INTERVAL_S, max(0.0, deadline - now)))
            except asyncio.CancelledError:
                exit_code = 3
                audit("interrupted")
            except Exception as error:
                state["error"] = str(error)
                if guided:
                    state["error_kind"] = "audit" if isinstance(error, OSError) and not isinstance(error, (ConnectionError, TimeoutError)) else "ble"
                exit_code = 1
                audit("error", error=str(error))
            finally:
                try:
                    await session.command("STOP", cleanup=True)
                    state["stop_confirmed"] = True
                    audit("stop_confirmed")
                except Exception as error:
                    state["error"] = "; ".join(filter(None, [state["error"], f"STOP unconfirmed: {error}"]))
                    exit_code = 1
                    audit("stop_unconfirmed", error=str(error))
                capture = False
                if session.failure is not None:
                    state["error"] = state["error"] or str(session.failure)
                    exit_code = 1
                if exit_code != 1 and state["stop_confirmed"]:
                    try:
                        final_status = v52_ble.parse_status(await session.command("STATUS"))
                        state["final_status"] = final_status
                        if final_status["mask"] or final_status["fault"] or final_status["io"]:
                            raise RuntimeError("final STATUS reports running/fault/io")
                        for counter in ("overflow", "tx_drop"):
                            if final_status[counter] != config["status"][counter]:
                                if guided:
                                    audit("counter_gap", counter=counter, previous=config["status"][counter], value=final_status[counter])
                                else:
                                    raise RuntimeError(f"final STATUS {counter} changed during capture")
                        if final_status["seq"] != previous_sequence + 1:
                            if guided:
                                audit("sequence_gap", previous_seq=previous_sequence, final_seq=final_status["seq"])
                            else:
                                raise RuntimeError("final STATUS sequence differs from last captured sample")
                    except Exception as error:
                        state["error"] = str(error)
                        exit_code = 1
                        audit("error", error=str(error))
    except Exception as error:
        state["error"] = str(error)
        if guided:
            state["error_kind"] = "audit" if isinstance(error, OSError) and not isinstance(error, (ConnectionError, TimeoutError)) else "ble"
        exit_code = 1
        audit("error", error=str(error))
    finally:
        phase = "error" if exit_code == 1 else "stopped" if exit_code == 3 else "completed"
        try:
            update_state(phase=phase, finished_unix_s=time.time())
            audit("finished", phase=phase, stop_confirmed=state["stop_confirmed"],
                  samples_by_channel=state["samples_by_channel"], error=state["error"])
        finally:
            stack.close()
    if exit_code == 1:
        print(f"V5.2 collection failed: {state['error'] or 'capture did not complete'}", file=sys.stderr)
    return exit_code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verified V5.2 BLE four-channel collector")
    parser.add_argument("--address", required=True)
    parser.add_argument("--settings", type=Path, required=True, help="path to normalized settings JSON file")
    for flag in ("out", "all-channels", "monitor", "audit", "raw-log", "cmd-file", "state-file"):
        parser.add_argument(f"--{flag}", type=Path, required=True)
    parser.add_argument("--run-token", required=True)
    parser.add_argument("--channel-mask", type=int, default=15)
    parser.add_argument("--start-gate", type=Path)
    parser.add_argument("--attempt-id")
    parser.add_argument("--fault-policy", choices=("strict", "guided"), default="strict")
    args = parser.parse_args(argv)

    async def run():
        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        installed = []
        for signum in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(signum, stop_event.set)
                installed.append(signum)
            except NotImplementedError:
                pass
        try:
            return await collect(args, stop_event)
        finally:
            for signum in installed:
                loop.remove_signal_handler(signum)

    try:
        return asyncio.run(run())
    except (KeyboardInterrupt, Exception) as error:
        print(f"V5.2 collector failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
