"""Read-only recovery from a finalized encoder checkpoint and confirmed STOP.

Never reconstruct geometry from client coordinates or waive an unknown STOP.
The last finalized run and its hashes supply the checkpoint, not the caller.
"""
import copy
import hashlib
import json
from pathlib import Path

from .automation import AutomationError


def arrival_checkpoint(run_dir):
    root = Path(run_dir)
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    error = manifest.get("error") or {}
    cause = error.get("cause") or {}
    # Guided calibration preflight errors are serialized directly; motion
    # transaction errors are wrapped in cause. Preserve an explicit nested
    # hardware fault instead of overriding it with an outer calibration code.
    recovery_code = cause.get("code") if cause else error.get("code")
    if (manifest.get("mode") != "live" or manifest.get("synthetic") is not False
            or manifest.get("stop_confirmed") is not True
            or manifest.get("state") not in ("completed", "stopped", "error")
            or (manifest.get("state") == "error" and
                recovery_code not in (
                    "move_timeout", "transaction_timeout", "cancelled", "calibration_motion", "invalid_telemetry"))
            or any(board.get("stop_confirmed") is not True or board.get("output_state") != "stopped"
                   for board in manifest.get("boards", {}).values())):
        raise ValueError("A finalized healthy completion or confirmed motion-timeout STOP is required")
    for name in ("events.jsonl", "motion-calibration.json", "plan.json"):
        if hashlib.sha256((root / name).read_bytes()).hexdigest() != manifest["files"].get(name):
            raise ValueError("Finalized arrival evidence has changed")
    record = json.loads((root / "motion-calibration.json").read_text(encoding="utf-8"))
    step = settled = stopped = parked = None
    counts = {}
    expected_targets = {}
    arrivals = {}
    for line in (root / "events.jsonl").read_text(encoding="utf-8").splitlines():
        event = json.loads(line)
        if event.get("event") == "step_started":
            step, settled, stopped, counts = event["step"], None, None, {}
            if step["op"] in ("move_x", "move_z"):
                expected_targets[step["resolved_params"]["axis"]] = step["resolved_params"]["target_mm"]
                arrivals.pop(step["resolved_params"]["axis"], None)
        if event.get("event") == "motion_naturally_parked":
            proof = event.get("proof", {})
            if proof.get("basis") == "natural_finite_completion":
                parked = proof.get("reference_identity")
        detail = event.get("detail", {})
        if event.get("event") != "motion_wire":
            continue
        if detail.get("kind") == "finite_motion_settled":
            settled = detail
        if detail.get("kind") == "arrived":
            arrivals[detail["axis"]] = detail
            expected_targets[detail["axis"]] = detail["target_mm"]
        if (settled or parked) and detail.get("kind") == "wire" and detail.get("direction") == "tx":
            if detail.get("opcode") in (0xFD, 0xF6, 0xF3, 0x0A, 0x9A):
                # A later finite phase needs its own natural-completion proof.
                settled = parked = None
                for axis, config in record["motion"]["axes"].items():
                    if config["address"] == detail.get("address"):
                        arrivals.pop(axis, None)
        if detail.get("kind") == "wire" and detail.get("direction") == "rx" and detail.get("opcode") == 0x43:
            frame = bytes.fromhex(detail["frame_hex"])
            if len(frame) != 31 or frame[2:4] != bytes([31, 9]) or frame[-1] != 0x6B:
                raise ValueError("Invalid stopped encoder evidence")
            def signed(start, size):
                if frame[start] not in (0, 1):
                    raise ValueError("Invalid encoder sign")
                return int.from_bytes(frame[start + 1:start + 1 + size], "big") * (-1 if frame[start] else 1)
            counts[frame[0]] = {"raw": signed(18, 4), "rpm": signed(15, 2), "status": frame[29]}
        if detail.get("kind") == "wire" and detail.get("direction") == "rx" and detail.get("opcode") in (0x36, 0x35, 0x3A):
            frame = bytes.fromhex(detail["frame_hex"])
            opcode = detail["opcode"]
            sample = counts.setdefault(frame[0], {})
            if frame[-1] != 0x6B or frame[1] != opcode or len(frame) != {0x36: 8, 0x35: 6, 0x3A: 4}[opcode]:
                raise ValueError("Invalid separate stopped telemetry")
            if opcode == 0x3A:
                sample["status"] = frame[2]
            else:
                if frame[2] not in (0, 1):
                    raise ValueError("Invalid stopped telemetry sign")
                sample["raw" if opcode == 0x36 else "rpm"] = int.from_bytes(frame[3:-1], "big") * (-1 if frame[2] else 1)
        if detail.get("kind") == "stopped":
            stopped = detail
    if parked is not None and manifest["state"] in ("completed", "stopped"):
        if any(parked[field] != record["reference"][field] for field in ("motion", "reference_counts", "profiles")):
            raise ValueError("Natural parking proof belongs to another reference")
        return record, copy.deepcopy(parked)
    # A timeout can interrupt a finite move before arrival was proved. A fresh
    # acknowledged STOP still supplies a valid absolute encoder checkpoint;
    # this restores coordinates only, never marks that move as completed.
    telemetry_dwell_stop = (manifest["state"] == "error" and recovery_code == "invalid_telemetry"
                            and step is not None and step["op"] == "dwell"
                            and set(arrivals) == {"X", "Z"} and stopped is not None
                            and stopped.get("stop_confirmed") is True)
    if recovery_code == "invalid_telemetry" and not telemetry_dwell_stop:
        raise ValueError("Telemetry recovery requires both proved arrivals at a stationary dwell STOP")
    timed_out_in_window = (manifest["state"] == "error" and recovery_code != "invalid_telemetry" and stopped is not None
                           and stopped.get("stop_confirmed") is True and set(counts) >=
                           {c["address"] for c in record["motion"]["axes"].values()})
    stopped_after_arrival = manifest["state"] == "stopped" and set(arrivals) == {"X", "Z"}
    if not telemetry_dwell_stop and not stopped_after_arrival and not timed_out_in_window and (not step or step["op"] not in ("move_x", "move_z") or not settled or settled.get("axis") != step["resolved_params"].get("axis")
            or settled.get("basis") != "natural_finite_completion" or not stopped
            or stopped.get("stop_confirmed") is not True):
        raise ValueError("No complete naturally settled arrival and confirmed STOP proof")
    checkpoint = copy.deepcopy(record["reference"])
    for axis, config in record["motion"]["axes"].items():
        sample = counts[config["address"]]
        if sample["rpm"] != 0 or sample["status"] & 0x3C or sample["status"] & 3 != 3:
            raise ValueError("The recorded axis was not healthy, locked and stationary")
        expected_mm = expected_targets.get(axis)
        sign = -1 if config["positive_direction"] else 1
        expected = (checkpoint["reference_counts"][axis] + sign * expected_mm * 65536 / config["lead_mm"]
                    if expected_mm is not None else checkpoint["raw_counts"][axis])
        tolerance = max(1, checkpoint["profiles"][axis]["config"]["position_window_deg"] * 65536 / 360)
        if timed_out_in_window:
            low, high = (sorted((record["x_first_mm"], record["x_last_mm"])) if axis == "X"
                         else (record["z_safe_mm"], record["z_working_mm"]))
            position = sign * (sample["raw"] - checkpoint["reference_counts"][axis]) * config["lead_mm"] / 65536
            window = tolerance * config["lead_mm"] / 65536
            if config.get("max_speed_mm_s", 0) > 20:
                window = max(window, 0.25)
            if not low - window <= position <= high + window:
                raise ValueError("Stopped encoder is outside the captured experiment window")
        elif abs(sample["raw"] - expected) > tolerance:
            raise ValueError("The stopped axis is outside the proved target/safe height")
        if not telemetry_dwell_stop and not stopped_after_arrival and not timed_out_in_window and axis == settled["axis"] and abs(settled["target_signature"][0] - expected) > tolerance:
            raise ValueError("The arrival proof belongs to another target")
        checkpoint["raw_counts"][axis] = sample["raw"]
    return record, checkpoint


def recover_arrival(controller, service, *, position_confirmed=False):
    if position_confirmed is not True:
        raise AutomationError("position_confirmation", "Confirm the physical column and clear raised electrodes first", status=409)
    with controller._lock, service._lock:
        controller._idle()
        service._idle()
        state = controller.snapshot()
        if (state.get("stop_confirmed") is not True or state.get("recovery_required")
                or controller._motion is None):
            raise AutomationError("stop_unconfirmed", "Connect and confirm STOP before arrival recovery", status=409)
        live_root = controller.runs_dir / "live"
        candidates = [p for p in live_root.iterdir() if p.is_dir() and (p / "manifest.json").is_file()]
        latest = max(candidates, key=lambda p: (p / "manifest.json").stat().st_mtime_ns) if candidates else None
        run_id = state.get("run_id") or (latest.name if latest else "")
        if not run_id or Path(run_id).name != run_id or "/" in run_id or "\\" in run_id:
            raise AutomationError("recovery_evidence", "No last-run evidence is available", status=409)
        if latest is None or latest.name != run_id or latest.resolve().parent != live_root.resolve():
            raise AutomationError("recovery_evidence", "Only the latest finalized live run may authorize recovery", status=409)
        try:
            record, checkpoint = arrival_checkpoint(controller.runs_dir / "live" / run_id)
            saved = service._calibration
            # Recover this exact saved geometry only; a newer calibration wins.
            if (saved is None or any(saved.get(k) != record.get(k) for k in
                    ("id", "motion", "points", "x_first_mm", "x_last_mm", "z_safe_mm", "z_working_mm"))):
                raise ValueError("The saved calibration no longer matches the failed run")
            motion = controller._motion
            motion.recover_stopped_reference(checkpoint, verified_position_window=True)
            if controller._motion_factory is None:
                prepare = getattr(motion, "enable_responsive_keyboard", None)
                if callable(prepare):
                    prepare()
            current = motion.reference_identity()
            calibration = {**copy.deepcopy(saved), "reference": current, "reference_valid": True,
                           "connection_token": motion.snapshot()["connection_token"]}
            service._persist(calibration=calibration, replace_calibration=True)
            service._calibration = calibration
            service._calibrated_motion = motion
            controller._state.update(state="stopped", message="Verified arrival reference recovered",
                                     error=None, stop_confirmed=True)
            controller._persist()
            return {"guided": service.snapshot(), "state": controller.snapshot(),
                    "message": "编码器、目标寄存器、驱动配置和停止证据核验通过，已恢复原四点标定；未移动电机。"}
        except (ValueError, KeyError, TypeError, OSError) as error:
            raise AutomationError("recovery_evidence", f"Arrival recovery refused: {error}", status=409) from error
