"""Unified pa-v52-a / pa-v52-b BLE NUS driver, CLI, and cross-process concurrency control."""

from __future__ import annotations

import argparse
import asyncio
import csv
import dataclasses
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import json
import math
import os
from pathlib import Path
import re
import sys
import threading
import time
from typing import Any, Callable
import uuid

NUS_SERVICE = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
NUS_RX = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"
NUS_TX = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"
PROTOCOL = "v52-0.1"
COMMAND_TIMEOUT_S = 5.0
DEFAULT_LOCK_TIMEOUT_S = 30.0
LOCK_DIR = Path("/tmp/pav52_ble_locks")

BOARD_NAMES = {
    "A": "pa-v52-a",
    "B": "pa-v52-b",
}
BOARD_ADDRESSES = {
    "A": "0BFF4120-96BD-1C2E-2209-F97DC133E4C5",
    "B": "338AF641-2475-CCFC-8ED0-DE2A353C9962",
}
ADDRESS_TO_SLOT = {address.casefold(): slot for slot, address in BOARD_ADDRESSES.items()}

PERIOD_MS = {0: 124, 1: 242, 2: 476, 3: 945, 4: 1882, 5: 3757,
             6: 7507, 7: 15007, 8: 30007, 9: 60007, 10: 120007}
OFFSET_CODES = {"0nA": 0, "10pct": 1, "20pct": 2, "50pct": 3,
                "10nA": 4, "20nA": 5, "40nA": 6, "80nA": 7}
DEFAULTS = {
    "method": "it", "potential_v": -0.2, "initial_potential_v": -0.2,
    "working_electrode_v": 1.2, "prestep_s": 0.0, "duration_s": 180.0,
    "adaptive_stop": False, "target_rate_hz": 8.0, "sens_period_code": 0,
    "fit_window_s": 5.0, "fsr_nA": 50, "offset_mode": "20pct", "analysis_channel": 1,
}

_IN_PROCESS_LOCKS_GUARD = threading.Lock()
_IN_PROCESS_LOCKS: dict[str, threading.Lock] = {}


def _norm_token(text: str | None) -> str:
    if not isinstance(text, str):
        return ""
    return text.strip().upper().replace(".", "").replace("-", "").replace("_", "")


def is_v52_name(name: str | None) -> bool:
    return _norm_token(name) in {"PAV52", "PAV52A", "PAV52B", "PPMV52A", "PPMV52B"}


def canonical_board_name(name: str | None = None, address: str | None = None) -> str:
    if isinstance(address, str) and address.strip():
        slot = ADDRESS_TO_SLOT.get(address.strip().casefold())
        if slot:
            return BOARD_NAMES[slot]
    norm = _norm_token(name)
    if norm in {"PAV52B", "PPMV52B"}:
        return BOARD_NAMES["B"]
    if norm in {"PAV52", "PAV52A", "PPMV52A"}:
        return BOARD_NAMES["A"]
    if isinstance(name, str) and name.strip():
        return name.strip()
    return BOARD_NAMES["A"]


def resolve_board_address(target: str) -> str:
    if not isinstance(target, str) or not target.strip():
        raise ValueError("Board target or BLE address is required")
    cleaned = target.strip()
    upper = cleaned.upper()
    if upper in BOARD_ADDRESSES:
        return BOARD_ADDRESSES[upper]
    norm = _norm_token(cleaned)
    if norm in {"PAV52A", "BOARDA", "BOARDPAV52A"}:
        return BOARD_ADDRESSES["A"]
    if norm in {"PAV52B", "BOARDB", "BOARDPAV52B"}:
        return BOARD_ADDRESSES["B"]
    return cleaned


def _lock_key_for_address(address: str) -> str:
    resolved = resolve_board_address(address)
    slot = ADDRESS_TO_SLOT.get(resolved.casefold())
    if slot:
        return BOARD_NAMES[slot]
    sanitized = "".join(ch if ch.isalnum() else "_" for ch in resolved)
    return f"BLE_{sanitized[:64]}"


def _pid_alive(pid: int) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _with_registry(mutator: Callable[[dict[str, Any]], Any]) -> Any:
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    guard_path = LOCK_DIR / "registry.guard"
    reg_path = LOCK_DIR / "registry.json"
    with guard_path.open("a+", encoding="utf-8") as guard_file:
        fcntl.flock(guard_file.fileno(), fcntl.LOCK_EX)
        try:
            try:
                data = json.loads(reg_path.read_text(encoding="utf-8"))
                if not isinstance(data, dict) or not isinstance(data.get("tokens"), list):
                    data = {"tokens": []}
            except (OSError, ValueError):
                data = {"tokens": []}
            now = time.time()
            alive_tokens = []
            for item in data["tokens"]:
                if isinstance(item, dict) and _pid_alive(int(item.get("pid", -1))):
                    alive_tokens.append(item)
            data["tokens"] = alive_tokens
            result = mutator(data)
            data["updated_unix_s"] = now
            tmp_path = LOCK_DIR / f".registry.{uuid.uuid4().hex}.tmp"
            tmp_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
            tmp_path.replace(reg_path)
            return result
        finally:
            fcntl.flock(guard_file.fileno(), fcntl.LOCK_UN)


def inspect_locks() -> dict[str, Any]:
    now = time.time()

    def _read(data: dict[str, Any]) -> list[dict[str, Any]]:
        return [dict(item) for item in data.get("tokens", [])]

    tokens = _with_registry(_read)
    boards: dict[str, dict[str, Any]] = {
        name: {
            "board": name,
            "slot": slot,
            "address": BOARD_ADDRESSES[slot],
            "state": "idle",
            "active": None,
            "waiting": [],
        }
        for slot, name in BOARD_NAMES.items()
    }
    for token_info in tokens:
        key = token_info.get("lock_key", "")
        if key not in boards:
            boards[key] = {
                "board": key,
                "slot": ADDRESS_TO_SLOT.get(str(token_info.get("address", "")).casefold(), ""),
                "address": token_info.get("address", ""),
                "state": "idle",
                "active": None,
                "waiting": [],
            }
        enriched = dict(token_info)
        if enriched.get("status") == "active":
            acq = float(enriched.get("acquired_unix_s") or enriched.get("requested_unix_s") or now)
            enriched["held_s"] = round(max(0.0, now - acq), 2)
            boards[key]["active"] = enriched
            boards[key]["state"] = "busy"
        else:
            req = float(enriched.get("requested_unix_s") or now)
            enriched["waiting_s"] = round(max(0.0, now - req), 2)
            boards[key]["waiting"].append(enriched)
    for board_entry in boards.values():
        board_entry["waiting"].sort(key=lambda item: float(item.get("requested_unix_s", 0.0)))
        for idx, waiter in enumerate(board_entry["waiting"], start=1):
            waiter["position"] = idx
    return {
        "timestamp_utc": datetime.fromtimestamp(now, timezone.utc).isoformat(),
        "boards": boards,
        "tokens": tokens,
    }


class BleLockLease:
    """OS-level + token-queue concurrency guard for a V5.2 board BLE connection."""

    def __init__(
        self,
        address: str,
        *,
        owner: str | None = None,
        purpose: str | None = None,
        timeout: float = DEFAULT_LOCK_TIMEOUT_S,
    ):
        self.address = resolve_board_address(address)
        self.lock_key = _lock_key_for_address(self.address)
        self.board_name = canonical_board_name(None, self.address)
        self.owner = (
            owner
            or os.environ.get("PAV52_OWNER")
            or os.environ.get("PAV52_AGENT_ID")
            or f"pid-{os.getpid()}:{Path(sys.argv[0]).name if sys.argv and sys.argv[0] else 'python'}"
        )
        self.purpose = purpose or "ble_session"
        self.timeout = timeout
        self.token = f"tok-{uuid.uuid4().hex[:8]}"
        self._fd: int | None = None
        self._thread_lock: threading.Lock | None = None
        self._thread_acquired = False

    def _register_waiting(self) -> None:
        now = time.time()
        entry = {
            "token": self.token,
            "lock_key": self.lock_key,
            "board": self.board_name,
            "address": self.address,
            "pid": os.getpid(),
            "owner": self.owner,
            "purpose": self.purpose,
            "status": "waiting",
            "requested_unix_s": now,
            "requested_utc": datetime.fromtimestamp(now, timezone.utc).isoformat(),
            "acquired_unix_s": None,
        }

        def _add(data: dict[str, Any]) -> None:
            data["tokens"] = [t for t in data["tokens"] if t.get("token") != self.token]
            data["tokens"].append(entry)

        _with_registry(_add)

    def _mark_active(self) -> None:
        now = time.time()

        def _activate(data: dict[str, Any]) -> None:
            for item in data["tokens"]:
                if item.get("token") == self.token:
                    item["status"] = "active"
                    item["acquired_unix_s"] = now
                    item["acquired_utc"] = datetime.fromtimestamp(now, timezone.utc).isoformat()

        _with_registry(_activate)

    def _unregister(self) -> None:
        def _remove(data: dict[str, Any]) -> None:
            data["tokens"] = [t for t in data["tokens"] if t.get("token") != self.token]

        try:
            _with_registry(_remove)
        except Exception:
            pass

    async def acquire(self) -> BleLockLease:
        LOCK_DIR.mkdir(parents=True, exist_ok=True)
        lock_file = LOCK_DIR / f"{self.lock_key}.lock"
        with _IN_PROCESS_LOCKS_GUARD:
            self._thread_lock = _IN_PROCESS_LOCKS.setdefault(self.lock_key, threading.Lock())
        self._register_waiting()
        deadline = time.monotonic() + max(0.0, float(self.timeout))
        fd = os.open(str(lock_file), os.O_CREAT | os.O_RDWR, 0o666)
        try:
            while True:
                if self._thread_lock.acquire(blocking=False):
                    self._thread_acquired = True
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        self._fd = fd
                        self._mark_active()
                        return self
                    except BlockingIOError:
                        self._thread_lock.release()
                        self._thread_acquired = False
                if time.monotonic() >= deadline:
                    snapshot = inspect_locks()["boards"].get(self.lock_key, {})
                    holder = snapshot.get("active")
                    waiting_len = len(snapshot.get("waiting", []))
                    raise TimeoutError(
                        f"BLE lock busy for {self.board_name} ({self.address}): "
                        f"active_holder={holder}, waiting_queue={waiting_len}"
                    )
                await asyncio.sleep(0.05)
        except BaseException:
            self._unregister()
            if self._thread_acquired and self._thread_lock is not None:
                self._thread_lock.release()
                self._thread_acquired = False
            try:
                os.close(fd)
            except OSError:
                pass
            raise

    def release(self) -> None:
        try:
            if self._fd is not None:
                try:
                    fcntl.flock(self._fd, fcntl.LOCK_UN)
                except OSError:
                    pass
                try:
                    os.close(self._fd)
                except OSError:
                    pass
                self._fd = None
        finally:
            if self._thread_acquired and self._thread_lock is not None:
                try:
                    self._thread_lock.release()
                except RuntimeError:
                    pass
                self._thread_acquired = False
            self._unregister()


def period_seconds(code: int) -> float:
    if code not in PERIOD_MS:
        raise ValueError("V5.2 period must be 0..10")
    return PERIOD_MS[code] / 1000.0


def quantize_dac(microvolts: int) -> int:
    return ((microvolts + 187) // 375) * 375


def validate_settings(settings: dict[str, Any]) -> dict[str, Any]:
    merged = {**DEFAULTS, **settings}

    def number(name: str) -> float:
        try:
            value = float(merged[name])
        except (ValueError, TypeError) as error:
            raise ValueError(f"{name} must be numeric") from error
        if not math.isfinite(value):
            raise ValueError(f"{name} must be finite")
        return value

    def integer(name: str) -> int:
        value = number(name)
        if value != math.trunc(value):
            raise ValueError(f"{name} must be an integer")
        return int(value)

    if merged["method"] != "it":
        raise ValueError("V5.2 supports I-t only")
    if number("prestep_s") != 0:
        raise ValueError("V5.2 does not support a pre-step")
    for field in ("conv_time_code", "conv_code", "charge_pump", "cp"):
        if field in merged and number(field) != 0:
            raise ValueError(f"V5.2 requires {field}=0")
    channel = integer("analysis_channel")
    period = integer("sens_period_code")
    fsr = integer("fsr_nA")
    if channel not in range(1, 5):
        raise ValueError("analysis_channel must be 1..4")
    if period not in PERIOD_MS:
        raise ValueError("V5.2 period must be 0..10")
    if fsr not in (50, 100, 250, 500, 1000, 2000):
        raise ValueError("unsupported V5.2 FSR")
    offset = str(merged["offset_mode"])
    if offset not in OFFSET_CODES:
        raise ValueError("unsupported V5.2 offset")
    offset_na = fsr * int(offset[:-3]) / 100 if offset.endswith("pct") else int(offset[:-2])
    if offset_na >= fsr:
        raise ValueError("offset must be below FSR")
    we_v = number("working_electrode_v")
    potential_v = number("potential_v")
    re_v = we_v - potential_v
    if not 0.25 <= we_v <= 1.535625 or not 0.1 <= re_v <= 1.535625:
        raise ValueError("WE/RE are outside the V5.2 DAC/headroom range")
    raw_channel_potentials = merged.get("channel_potentials_v")
    normalized_channel_potentials = None
    if raw_channel_potentials is not None:
        if not isinstance(raw_channel_potentials, dict):
            raise ValueError("channel_potentials_v must be an object mapping channels 1..4 to potentials")
        normalized_channel_potentials = {}
        for k, v in raw_channel_potentials.items():
            ch_key = str(k)
            if ch_key not in {"1", "2", "3", "4"}:
                raise ValueError(f"invalid channel key {k} in channel_potentials_v")
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(float(v)):
                raise ValueError(f"channel_potentials_v[{ch_key}] must be a finite number")
            p_val = float(v)
            ch_re_v = we_v - p_val
            if not -0.4 <= p_val <= 0.4 or not 0.1 <= ch_re_v <= 1.535625:
                raise ValueError(f"channel {ch_key} WE/RE is outside the V5.2 DAC/headroom range")
            normalized_channel_potentials[ch_key] = p_val
        if normalized_channel_potentials:
            for ch_idx in range(1, 5):
                normalized_channel_potentials.setdefault(str(ch_idx), potential_v)
            distinct_re_uv = {
                quantize_dac(round((we_v - p_val) * 1_000_000))
                for p_val in normalized_channel_potentials.values()
            }
            if len(distinct_re_uv) > 3:
                raise ValueError("A single V5.2 board supports at most 3 distinct WE-RE potentials (DAC B, C, D)")
        else:
            normalized_channel_potentials = None

    raw_channel_settings = merged.get("channel_settings")
    normalized_channel_settings = None
    if raw_channel_settings is not None:
        if not isinstance(raw_channel_settings, dict):
            raise ValueError("channel_settings must be an object mapping channels 1..4 to sampling settings")
        normalized_channel_settings = {}
        allowed_ch_keys = {"potential_v", "working_electrode_v", "fsr_nA", "offset_mode", "offset_nA"}
        for k, ch_cfg in raw_channel_settings.items():
            ch_key = str(k)
            if ch_key not in {"1", "2", "3", "4"}:
                raise ValueError(f"invalid channel key {k} in channel_settings")
            if not isinstance(ch_cfg, dict):
                raise ValueError(f"channel_settings[{ch_key}] must be an object")
            unexpected_ch = set(ch_cfg) - allowed_ch_keys
            if unexpected_ch:
                raise ValueError(f"unexpected field {sorted(unexpected_ch)[0]} in channel_settings[{ch_key}]")
            ch_we = we_v
            if ch_cfg.get("working_electrode_v") is not None:
                raw_we = ch_cfg["working_electrode_v"]
                if isinstance(raw_we, bool) or not isinstance(raw_we, (int, float)) or not math.isfinite(float(raw_we)):
                    raise ValueError(f"channel_settings[{ch_key}].working_electrode_v must be a finite number")
                ch_we = float(raw_we)
            ch_pot = normalized_channel_potentials[ch_key] if normalized_channel_potentials else potential_v
            if ch_cfg.get("potential_v") is not None:
                raw_pot = ch_cfg["potential_v"]
                if isinstance(raw_pot, bool) or not isinstance(raw_pot, (int, float)) or not math.isfinite(float(raw_pot)):
                    raise ValueError(f"channel_settings[{ch_key}].potential_v must be a finite number")
                ch_pot = float(raw_pot)
            ch_re = ch_we - ch_pot
            if not -0.4 <= ch_pot <= 0.4 or not 0.25 <= ch_we <= 1.535625 or not 0.1 <= ch_re <= 1.535625:
                raise ValueError(f"channel {ch_key} WE/RE is outside the V5.2 DAC/headroom range")
            ch_fsr = fsr
            if ch_cfg.get("fsr_nA") is not None:
                raw_fsr = ch_cfg["fsr_nA"]
                if isinstance(raw_fsr, bool) or not isinstance(raw_fsr, (int, float)) or not math.isfinite(float(raw_fsr)) or float(raw_fsr) != math.trunc(float(raw_fsr)):
                    raise ValueError(f"channel_settings[{ch_key}].fsr_nA must be an integer")
                ch_fsr = int(raw_fsr)
                if ch_fsr not in (50, 100, 250, 500, 1000, 2000):
                    raise ValueError(f"channel {ch_key} unsupported V5.2 FSR")
            ch_offset = offset
            if ch_cfg.get("offset_mode") is not None:
                ch_offset = str(ch_cfg["offset_mode"])
                if ch_offset not in OFFSET_CODES:
                    raise ValueError(f"channel {ch_key} unsupported V5.2 offset")
            ch_offset_na = ch_fsr * int(ch_offset[:-3]) / 100 if ch_offset.endswith("pct") else int(ch_offset[:-2])
            if ch_offset_na >= ch_fsr:
                raise ValueError(f"channel {ch_key} offset must be below FSR")
            normalized_channel_settings[ch_key] = {
                "potential_v": ch_pot,
                "working_electrode_v": ch_we,
                "fsr_nA": ch_fsr,
                "offset_mode": ch_offset,
                "offset_nA": ch_offset_na,
            }
        if normalized_channel_settings:
            for ch_idx in range(1, 5):
                ch_key = str(ch_idx)
                if ch_key not in normalized_channel_settings:
                    ch_pot = normalized_channel_potentials[ch_key] if normalized_channel_potentials else potential_v
                    ch_re = we_v - ch_pot
                    if not -0.4 <= ch_pot <= 0.4 or not 0.1 <= ch_re <= 1.535625:
                        raise ValueError(f"channel {ch_key} WE/RE is outside the V5.2 DAC/headroom range")
                    normalized_channel_settings[ch_key] = {
                        "potential_v": ch_pot,
                        "working_electrode_v": we_v,
                        "fsr_nA": fsr,
                        "offset_mode": offset,
                        "offset_nA": offset_na,
                    }
            distinct_we_q = {
                quantize_dac(round(cfg["working_electrode_v"] * 1_000_000))
                for cfg in normalized_channel_settings.values()
            }
            distinct_re_q = {
                quantize_dac(round((cfg["working_electrode_v"] - cfg["potential_v"]) * 1_000_000))
                for cfg in normalized_channel_settings.values()
            }
            if len(distinct_we_q) == 1 and len(distinct_re_q) > 3:
                raise ValueError("A single V5.2 board supports at most 3 distinct WE-RE potentials (DAC B, C, D)")
            if len(distinct_we_q | distinct_re_q) > 4:
                raise ValueError("A single V5.2 board has 4 shared DACs (DAC A-D); requested per-channel WE and RE voltages require more than 4 distinct DAC levels")
        else:
            normalized_channel_settings = None

    duration = number("duration_s")
    adaptive = merged["adaptive_stop"]
    if not isinstance(adaptive, bool):
        raise ValueError("adaptive_stop must be boolean")
    if not (0 <= duration <= 3600 if adaptive else 10 <= duration <= 3600):
        raise ValueError("duration_s must be 10..3600 (0..3600 with adaptive stop)")
    target_rate = number("target_rate_hz")
    fit_window = number("fit_window_s")
    if not 0.5 <= target_rate <= 10:
        raise ValueError("target_rate_hz must be 0.5..10")
    if not 1 <= fit_window <= (3600 if adaptive else duration):
        raise ValueError("fit_window_s must fit within the run")
    merged.update({
        "board_profile": "v52", "protocol": PROTOCOL, "analysis_channel": channel,
        "working_electrode_v": we_v, "potential_v": potential_v,
        "initial_potential_v": potential_v, "prestep_s": 0.0,
        "duration_s": duration, "adaptive_stop": adaptive, "target_rate_hz": target_rate,
        "fit_window_s": fit_window, "sens_period_code": period, "fsr_nA": fsr,
        "offset_mode": offset, "offset_nA": offset_na,
    })
    if normalized_channel_potentials is not None:
        merged["channel_potentials_v"] = {str(ch): normalized_channel_potentials[str(ch)] for ch in range(1, 5)}
    else:
        merged.pop("channel_potentials_v", None)
    if normalized_channel_settings is not None:
        merged["channel_settings"] = {str(ch): normalized_channel_settings[str(ch)] for ch in range(1, 5)}
    else:
        merged.pop("channel_settings", None)
    return merged


class LineDecoder:
    def __init__(self, limit: int = 1024):
        self.limit = limit
        self.buffer = bytearray()
        self.discarding = False
        self.errors = 0

    def feed(self, data: bytes | bytearray) -> list[str]:
        lines = []
        for byte in data:
            if byte == 10:
                if self.discarding:
                    self.errors += 1
                elif self.buffer:
                    try:
                        line = self.buffer.rstrip(b"\r").decode("ascii")
                        if any(ord(character) < 32 or ord(character) > 126 for character in line):
                            raise ValueError("non-printable frame")
                        lines.append(line)
                    except (UnicodeDecodeError, ValueError):
                        self.errors += 1
                self.buffer.clear()
                self.discarding = False
            elif not self.discarding:
                if len(self.buffer) >= self.limit:
                    self.discarding = True
                    self.buffer.clear()
                else:
                    self.buffer.append(byte)
        return lines


@dataclass(frozen=True)
class Sample:
    sequence: int
    uptime_ms: int
    channel: int
    counts: int
    current_fa: int
    flags: int

    @classmethod
    def parse(cls, line: str) -> Sample | None:
        if not line.startswith("D,"):
            return None
        fields = line.split(",")
        if len(fields) != 7:
            raise ValueError("invalid sample frame")
        sample = cls(*map(int, fields[1:]))
        if (not 0 <= sample.sequence < 2**64 or not 0 <= sample.uptime_ms < 2**32
                or not 1 <= sample.channel <= 4 or not 0 <= sample.counts <= 65535
                or not 0 <= sample.flags <= 7):
            raise ValueError("sample fields out of range")
        return sample


@dataclass(frozen=True)
class Monitor:
    quantity: str
    channel: int
    counts: int
    value: int
    flags: int

    @property
    def unit(self) -> str:
        return "milliC" if self.quantity == "TEMP" else "uV"

    @classmethod
    def parse(cls, line: str) -> Monitor | None:
        if not line.startswith("M,"):
            return None
        fields = line.split(",")
        if len(fields) != 6 or fields[1] not in {"VDD", "TEMP", "WE", "RE", "CE"}:
            raise ValueError("invalid monitor frame")
        measurement = cls(fields[1], *map(int, fields[2:]))
        global_quantity = measurement.quantity in {"VDD", "TEMP"}
        if ((global_quantity and measurement.channel != 0)
                or (not global_quantity and not 1 <= measurement.channel <= 4)
                or not 0 <= measurement.counts <= (65535 if measurement.quantity == "TEMP" else 4095)
                or not 0 <= measurement.flags <= 1):
            raise ValueError("monitor fields out of range")
        return measurement


def encode_command(command: str) -> bytes:
    command = command.strip(" \t")
    if (not 1 <= len(command) <= 159
            or any(ord(character) < 32 or ord(character) > 126 for character in command)):
        raise ValueError("command must be 1..159 printable ASCII characters")
    return (command + "\n").encode("ascii")


async def _windows_cached_device(address):
    """Resolve a real Windows device when advertisement discovery is empty.

    WinRT can establish GATT to a known peripheral without a fresh advertising
    report. The BLEDevice is derived from that OS identity; NUS and INFO/STATUS
    verification are still performed by the normal session and broker.
    """
    if sys.platform != "win32" or not re.fullmatch(r"[0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5}", address):
        return None
    from bleak.backends.device import BLEDevice
    from winrt.windows.devices.bluetooth import BluetoothLEDevice
    device = await asyncio.wait_for(BluetoothLEDevice.from_bluetooth_address_async(
        int(address.replace(":", ""), 16)), timeout=5)
    if device is None:
        return None
    try:
        return BLEDevice(address, device.name, {"winrt_device_id": device.device_id})
    finally:
        device.close()


class BleSession:
    def __init__(
        self,
        address: str,
        on_line: Callable[[str, float], None] | None = None,
        command_timeout: float | None = None,
        *,
        owner: str | None = None,
        purpose: str | None = None,
        lock_timeout: float = DEFAULT_LOCK_TIMEOUT_S,
    ):
        self.address = resolve_board_address(address)
        self.board_name = canonical_board_name(None, self.address)
        self.on_line = on_line
        self.command_timeout = COMMAND_TIMEOUT_S if command_timeout is None else command_timeout
        self.decoder = LineDecoder()
        self.disconnected = asyncio.Event()
        self.responses: asyncio.Queue[str] = asyncio.Queue(maxsize=64)
        self.lock = asyncio.Lock()
        self.active_command: str | None = None
        self._abandoned_start = False
        self.failure: Exception | None = None
        self.client = None
        self._lease = BleLockLease(
            self.address, owner=owner, purpose=purpose or "ble_session", timeout=lock_timeout
        )
        self.token = self._lease.token

    async def __aenter__(self):
        from bleak import BleakClient

        await self._lease.acquire()
        try:
            self.client = BleakClient(
                self.address,
                disconnected_callback=lambda client: self.disconnected.set(),
                timeout=10.0,
            )
            try:
                await self.client.__aenter__()
            except Exception as error:
                if sys.platform != "win32" or type(error).__name__ != "BleakDeviceNotFoundError":
                    raise
                target = await _windows_cached_device(self.address)
                if target is None:
                    raise
                self.disconnected.clear()
                self.client = BleakClient(
                    target,
                    disconnected_callback=lambda client: self.disconnected.set(),
                    timeout=10.0,
                )
                await self.client.__aenter__()
            try:
                await self.client.start_notify(NUS_TX, self._notification)
            except BaseException:
                await self.client.__aexit__(*sys.exc_info())
                raise
            return self
        except BaseException:
            self._lease.release()
            raise

    async def __aexit__(self, *args):
        try:
            if self.client is not None:
                return await self.client.__aexit__(*args)
            return False
        finally:
            self._lease.release()

    def _notification(self, characteristic, data):
        previous_errors = self.decoder.errors
        for line in self.decoder.feed(data):
            try:
                if self.on_line:
                    self.on_line(line, time.time())
                if line.startswith(("D,", "M,", "E,")):
                    continue
                if self.active_command:
                    self.responses.put_nowait(line)
            except Exception as error:
                self.failure = error
        if self.decoder.errors != previous_errors:
            self.failure = RuntimeError("invalid or oversized BLE frame")

    def check_health(self):
        if self.disconnected.is_set() or not self.client.is_connected:
            raise ConnectionError("V5.2 disconnected; board may still be sampling")
        if self.failure is not None:
            raise self.failure

    async def command(self, command: str, *, cleanup: bool = False) -> list[str]:
        payload = encode_command(command)
        async with self.lock:
            while not self.responses.empty():
                self.responses.get_nowait()
            self.active_command = command.split()[0]
            lines = []
            deadline = time.monotonic() + self.command_timeout
            try:
                for offset in range(0, len(payload), 20):
                    await asyncio.wait_for(
                        self.client.write_gatt_char(NUS_RX, payload[offset:offset + 20], response=True),
                        timeout=max(0.001, deadline - time.monotonic()))
                while True:
                    if cleanup:
                        if self.disconnected.is_set() or not self.client.is_connected:
                            raise ConnectionError("V5.2 disconnected before STOP confirmation")
                    else:
                        self.check_health()
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError(f"no complete response to {command}")
                    try:
                        line = await asyncio.wait_for(self.responses.get(), min(remaining, 0.05))
                    except TimeoutError:
                        continue
                    if line.startswith("ERR "):
                        raise RuntimeError(line)
                    # A cancelled/timed-out START can acknowledge after STOP
                    # has already been sent. It is not a STOP confirmation.
                    # Drain only this known outstanding reply; still require
                    # the actual OK STOP within the original bounded deadline.
                    if cleanup and self.active_command == "STOP" and self._abandoned_start and (line == "OK START" or line.startswith("OK START ")):
                        self._abandoned_start = False
                        continue
                    lines.append(line)
                    expected = "OK " + self.active_command
                    if line == expected or line.startswith(expected + " "):
                        if self.active_command in ("START", "STOP"):
                            self._abandoned_start = False
                        return lines
                    list_record = (self.active_command == "STATUS" and line.startswith(("C,", "A,"))
                                   or self.active_command == "HELP" and line.startswith("H,"))
                    if not list_record:
                        raise RuntimeError(f"unexpected reply to {command}: {line}")
            except (asyncio.CancelledError, TimeoutError):
                if self.active_command == "START":
                    self._abandoned_start = True
                raise
            finally:
                self.active_command = None


def _fields(line: str, prefix: str, required: set[str]) -> dict[str, str]:
    if not line.startswith(prefix + " "):
        raise ValueError(f"missing {prefix} fields")
    values = {}
    for token in line[len(prefix) + 1:].split():
        key, separator, value = token.partition("=")
        if not separator or key in values:
            raise ValueError(f"invalid {prefix} fields")
        values[key] = value
    if not required <= values.keys():
        raise ValueError(f"incomplete {prefix} fields")
    return values


def parse_info(lines: list[str]) -> dict[str, Any]:
    if len(lines) != 1:
        raise ValueError("invalid INFO response")
    values = _fields(lines[0], "OK INFO", {"fw", "part", "rev", "channels", "shared_dacs", "init"})
    if (values["fw"] != PROTOCOL or int(values["part"], 0) != 0x34
            or int(values["channels"]) != 4 or int(values["shared_dacs"]) != 4
            or int(values["init"]) != 0):
        raise RuntimeError("unsupported firmware/protocol or AFE initialization failure")
    return values


def parse_status(lines: list[str]) -> dict[str, Any]:
    if len(lines) != 9:
        raise ValueError("STATUS requires four channel and four DAC records")
    status = {key: int(value) for key, value in _fields(lines[-1], "OK STATUS", {
        "mask", "period", "fault", "io", "seq", "overflow", "tx_drop", "ble"}).items()}
    channels = {}
    dacs = {}
    for line in lines[:-1]:
        fields = line.split(",")
        if len(fields) == 8 and fields[0] == "C":
            channel = fields[1]
            if channel not in {"1", "2", "3", "4"} or channel in channels:
                raise ValueError("invalid STATUS channel")
            channels[channel] = dict(zip(
                ("fsr_nA", "offset_code", "conv_time_code", "we_dac", "re_dac", "cp"),
                map(int, fields[2:])))
        elif len(fields) == 4 and fields[0] == "A":
            name = fields[1]
            code, microvolts = map(int, fields[2:])
            if name not in "ABCD" or len(name) != 1 or name in dacs or not 0 <= code <= 4095 or microvolts != code * 375:
                raise ValueError("invalid STATUS DAC")
            dacs[name] = {"code": code, "uv": microvolts}
        else:
            raise ValueError("invalid STATUS record")
    if len(channels) != 4 or len(dacs) != 4:
        raise ValueError("incomplete STATUS channels/DACs")
    status.update(channels=channels, dacs=dacs)
    return status


async def configure_session(session: BleSession, settings: dict[str, Any]) -> dict[str, Any]:
    settings = validate_settings(settings)
    await session.command("STOP", cleanup=True)
    info = parse_info(await session.command("INFO"))
    we_uv = round(settings["working_electrode_v"] * 1_000_000)
    base_re_uv = round((settings["working_electrode_v"] - settings["potential_v"]) * 1_000_000)
    requested: dict[str, Any] = {
        "we_uv": we_uv,
        "re_uv": base_re_uv,
        "potential_uv": we_uv - base_re_uv,
    }
    before = parse_status(await session.command("STATUS"))
    if before["mask"] != 0 or before["fault"] or before["io"]:
        raise RuntimeError("STATUS is not healthy and stopped before configuration")
    ch_potentials = settings.get("channel_potentials_v")
    ch_settings = settings.get("channel_settings")
    if not ch_potentials and not ch_settings:
        # Stage a new bias on unused DACs. Rewriting active WE then RE DACs in
        # separate BLE commands otherwise applies the entire common-mode change
        # temporarily as electrode polarization (for example +1.0 V instead of
        # +0.2 V during a 0.4 -> 1.2 V common-mode correction).
        pairs = (("A", "B", 0, 1), ("C", "D", 2, 3))
        expected_we = quantize_dac(requested["we_uv"])
        expected_re = quantize_dac(requested["re_uv"])
        used = {config[key] for config in before["channels"].values() for key in ("we_dac", "re_dac")}
        pair = next((p for p in pairs if before["dacs"][p[0]]["uv"] == expected_we
                     and before["dacs"][p[1]]["uv"] == expected_re), None)
        if pair is None:
            pair = next((p for p in pairs if not {p[2], p[3]} & used), None)
            if pair is None:
                raise RuntimeError("No unused DAC pair to stage the requested bias without changing an active electrode voltage")
            await session.command(f"DAC {pair[0]} {requested['we_uv']}")
            await session.command(f"DAC {pair[1]} {requested['re_uv']}")
            staged = parse_status(await session.command("STATUS"))
            if (staged["mask"] != 0 or staged["fault"] or staged["io"]
                    or staged["channels"] != before["channels"]
                    or staged["dacs"][pair[0]]["uv"] != expected_we
                    or staged["dacs"][pair[1]]["uv"] != expected_re):
                raise RuntimeError("Staged DAC readback mismatch; electrode routing was not changed")
        await session.command(f"PERIOD {settings['sens_period_code']}")
        channel_config = {"fsr_nA": settings["fsr_nA"], "offset_code": OFFSET_CODES[settings["offset_mode"]],
                          "conv_time_code": 0, "we_dac": pair[2], "re_dac": pair[3], "cp": 0}
        for channel in range(1, 5):
            await session.command(f"CONFIG {channel} " + " ".join(map(str, channel_config.values())))
        status = parse_status(await session.command("STATUS"))
        if status["mask"] != 0 or status["period"] != settings["sens_period_code"] or status["fault"] or status["io"]:
            raise RuntimeError("STATUS run/period/fault mismatch")
        if any(config != channel_config for config in status["channels"].values()):
            raise RuntimeError("STATUS channel configuration mismatch")
        actual = {"we_uv": status["dacs"][pair[0]]["uv"], "re_uv": status["dacs"][pair[1]]["uv"]}
        if any(actual[key] != quantize_dac(requested[key]) for key in ("we_uv", "re_uv")):
            raise RuntimeError("STATUS DAC configuration mismatch")
        actual["potential_uv"] = actual["we_uv"] - actual["re_uv"]
        return {"info": info, "status": status, "requested": requested, "actual": actual,
                "settings": settings, "config_confirmed": True, "dac_pair": pair[0] + pair[1]}

    resolved_ch: dict[int, dict[str, Any]] = {}
    for ch in range(1, 5):
        ch_key = str(ch)
        ch_cfg = (ch_settings or {}).get(ch_key) or {}
        ch_we_v = float(ch_cfg.get("working_electrode_v", settings["working_electrode_v"]))
        ch_pot_v = float(ch_cfg.get("potential_v", (ch_potentials or {}).get(ch_key, settings["potential_v"])))
        ch_fsr = int(ch_cfg.get("fsr_nA", settings["fsr_nA"]))
        ch_offset_mode = str(ch_cfg.get("offset_mode", settings["offset_mode"]))
        ch_we_uv = round(ch_we_v * 1_000_000)
        ch_re_uv = round((ch_we_v - ch_pot_v) * 1_000_000)
        resolved_ch[ch] = {
            "we_uv": ch_we_uv,
            "re_uv": ch_re_uv,
            "fsr_nA": ch_fsr,
            "offset_code": OFFSET_CODES[ch_offset_mode],
        }

    distinct_we_q = {quantize_dac(resolved_ch[ch]["we_uv"]) for ch in range(1, 5)}
    all_dac_names = ("A", "B", "C", "D")
    channel_we_dac: dict[int, int] = {}
    channel_re_dac: dict[int, int] = {}
    programmed_dacs: dict[str, tuple[int, int]] = {}  # dac_letter -> (raw_uv, quantized_uv)

    if len(distinct_we_q) == 1:
        shared_we_uv = resolved_ch[1]["we_uv"]
        shared_we_q = quantize_dac(shared_we_uv)
        programmed_dacs["A"] = (shared_we_uv, shared_we_q)
        re_dac_names = ("B", "C", "D")
        re_dac_by_quantized: dict[int, tuple[int, str, int]] = {}
        base_q = quantize_dac(base_re_uv)
        if any(quantize_dac(resolved_ch[ch]["re_uv"]) == base_q for ch in range(1, 5)):
            re_dac_by_quantized[base_q] = (1, "B", base_re_uv)
        for ch in range(1, 5):
            ch_re_uv = resolved_ch[ch]["re_uv"]
            q_uv = quantize_dac(ch_re_uv)
            if q_uv not in re_dac_by_quantized:
                slot_idx = len(re_dac_by_quantized)
                dac_letter = re_dac_names[slot_idx]
                re_dac_by_quantized[q_uv] = (slot_idx + 1, dac_letter, ch_re_uv)
            channel_we_dac[ch] = 0
            channel_re_dac[ch] = re_dac_by_quantized[q_uv][0]
        await session.command(f"DAC A {shared_we_uv}")
        if "B" not in {item[1] for item in re_dac_by_quantized.values()}:
            await session.command(f"DAC B {base_re_uv}")
            programmed_dacs["B"] = (base_re_uv, base_q)
        for _dac_idx, dac_letter, raw_uv in sorted(re_dac_by_quantized.values(), key=lambda x: x[0]):
            await session.command(f"DAC {dac_letter} {raw_uv}")
            programmed_dacs[dac_letter] = (raw_uv, quantize_dac(raw_uv))
    else:
        dac_by_quantized: dict[int, tuple[int, str, int]] = {}
        for ch in range(1, 5):
            for role, raw_uv in (("we", resolved_ch[ch]["we_uv"]), ("re", resolved_ch[ch]["re_uv"])):
                q_uv = quantize_dac(raw_uv)
                if q_uv not in dac_by_quantized:
                    slot_idx = len(dac_by_quantized)
                    dac_letter = all_dac_names[slot_idx]
                    dac_by_quantized[q_uv] = (slot_idx, dac_letter, raw_uv)
                if role == "we":
                    channel_we_dac[ch] = dac_by_quantized[q_uv][0]
                else:
                    channel_re_dac[ch] = dac_by_quantized[q_uv][0]
        for _dac_idx, dac_letter, raw_uv in sorted(dac_by_quantized.values(), key=lambda x: x[0]):
            await session.command(f"DAC {dac_letter} {raw_uv}")
            programmed_dacs[dac_letter] = (raw_uv, quantize_dac(raw_uv))

    requested["channel_potentials_uv"] = {
        str(ch): resolved_ch[ch]["we_uv"] - resolved_ch[ch]["re_uv"]
        for ch in range(1, 5)
    }
    await session.command(f"PERIOD {settings['sens_period_code']}")

    expected_channels = {}
    for channel in range(1, 5):
        cfg = {
            "fsr_nA": resolved_ch[channel]["fsr_nA"],
            "offset_code": resolved_ch[channel]["offset_code"],
            "conv_time_code": 0,
            "we_dac": channel_we_dac[channel],
            "re_dac": channel_re_dac[channel],
            "cp": 0,
        }
        expected_channels[str(channel)] = cfg
        await session.command(f"CONFIG {channel} " + " ".join(map(str, cfg.values())))

    status = parse_status(await session.command("STATUS"))
    if status["mask"] != 0 or status["period"] != settings["sens_period_code"] or status["fault"] or status["io"]:
        raise RuntimeError("STATUS run/period/fault mismatch")
    if any(status["channels"].get(ch_key) != expected_cfg for ch_key, expected_cfg in expected_channels.items()):
        raise RuntimeError("STATUS channel configuration mismatch")
    for dac_letter, (_raw_uv, q_uv) in programmed_dacs.items():
        if status["dacs"][dac_letter]["uv"] != q_uv:
            raise RuntimeError("STATUS DAC configuration mismatch")
    actual: dict[str, Any] = {"we_uv": status["dacs"]["A"]["uv"], "re_uv": status["dacs"]["B"]["uv"]}
    actual["potential_uv"] = actual["we_uv"] - actual["re_uv"]
    actual["channel_potentials_uv"] = {
        str(ch): (
            status["dacs"][all_dac_names[channel_we_dac[ch]]]["uv"]
            - status["dacs"][all_dac_names[channel_re_dac[ch]]]["uv"]
        )
        for ch in range(1, 5)
    }
    actual["channel_settings"] = {
        str(ch): {
            "we_uv": status["dacs"][all_dac_names[channel_we_dac[ch]]]["uv"],
            "re_uv": status["dacs"][all_dac_names[channel_re_dac[ch]]]["uv"],
            "potential_uv": actual["channel_potentials_uv"][str(ch)],
            "fsr_nA": status["channels"][str(ch)]["fsr_nA"],
            "offset_code": status["channels"][str(ch)]["offset_code"],
        }
        for ch in range(1, 5)
    }
    return {"info": info, "status": status, "requested": requested, "actual": actual,
            "settings": settings, "config_confirmed": True, "dac_pair": "AB"}


async def scan(timeout: float = 4.0) -> list[tuple[Any, Any]]:
    from bleak import BleakScanner

    devices = await BleakScanner.discover(timeout=timeout, return_adv=True)
    return [
        (device, advertisement)
        for device, advertisement in devices.values()
        if is_v52_name(getattr(device, "name", None))
        or is_v52_name(getattr(advertisement, "local_name", None))
        or str(getattr(device, "address", "")).casefold() in ADDRESS_TO_SLOT
        or NUS_SERVICE in [value.lower() for value in getattr(advertisement, "service_uuids", []) or []]
    ]


def scan_devices(timeout: float = 4.0) -> list[dict[str, Any]]:
    async def _scan_dicts():
        pairs = await scan(timeout=timeout)
        rows = []
        for device, advertisement in pairs:
            raw_name = getattr(advertisement, "local_name", None) or getattr(device, "name", None)
            addr = getattr(device, "address", "")
            rows.append({
                "id": f"ble:{addr}",
                "kind": "ble",
                "transport": "ble",
                "address": addr,
                "name": canonical_board_name(raw_name, addr),
                "rssi": getattr(advertisement, "rssi", None),
                "board_profile": "v52",
                "selectable": True,
            })
        return rows

    if not math.isfinite(timeout) or not 0 < timeout <= 30:
        raise ValueError("scan timeout must be positive and at most 30 seconds")

    async def bounded_scan():
        try:
            return await asyncio.wait_for(_scan_dicts(), timeout=timeout + 20.0)
        except TimeoutError as error:
            raise TimeoutError("Bluetooth scan timed out during startup or discovery; check Bluetooth permission and retry") from error

    return asyncio.run(bounded_scan())


def probe_device(address: str, *, owner: str | None = None, lock_timeout: float = DEFAULT_LOCK_TIMEOUT_S) -> dict[str, Any]:
    resolved = resolve_board_address(address)

    async def probe():
        async with BleSession(resolved, owner=owner, purpose="probe", lock_timeout=lock_timeout) as session:
            info = parse_info(await session.command("INFO"))
            status = parse_status(await session.command("STATUS"))
            return {
                "board": session.board_name,
                "address": session.address,
                "token": session.token,
                "info": info,
                "status": status,
                "protocol_verified": True,
                "board_profile": "v52",
            }

    return asyncio.run(probe())


def configure_device(address: str, settings: dict[str, Any], *, owner: str | None = None, lock_timeout: float = DEFAULT_LOCK_TIMEOUT_S) -> dict[str, Any]:
    normalized = validate_settings(settings)
    resolved = resolve_board_address(address)

    async def configure():
        async with BleSession(resolved, owner=owner, purpose="configure", lock_timeout=lock_timeout) as session:
            return await configure_session(session, normalized)

    return asyncio.run(configure())


def stop_device(address: str, *, owner: str | None = None, lock_timeout: float = DEFAULT_LOCK_TIMEOUT_S) -> dict[str, Any]:
    resolved = resolve_board_address(address)

    async def stop():
        async with BleSession(resolved, owner=owner, purpose="stop", lock_timeout=lock_timeout) as session:
            reply = await session.command("STOP", cleanup=True)
            return {"stop_confirmed": True, "reply": reply[-1], "board": session.board_name, "address": session.address}

    return asyncio.run(stop())


async def run_session(args: Any) -> dict[str, Any]:
    target = getattr(args, "board", None) or getattr(args, "address", None)
    timeout = float(getattr(args, "timeout", 10.0))
    owner = getattr(args, "owner", None)
    lock_timeout = float(getattr(args, "lock_timeout", DEFAULT_LOCK_TIMEOUT_S))
    action = getattr(args, "action", "command")

    if not target:
        devices = await scan(timeout)
        matches = [
            device for device, advertisement in devices
            if is_v52_name(getattr(device, "name", None))
            or is_v52_name(getattr(advertisement, "local_name", None))
            or str(getattr(device, "address", "")).casefold() in ADDRESS_TO_SLOT
        ]
        if len(matches) != 1:
            raise RuntimeError(f"found {len(matches)} V5.2 boards; use --board A|B or --address to select one")
        address = matches[0].address
    else:
        address = resolve_board_address(target)

    output_file = None
    writer = None
    monitor_file = None
    monitor_writer = None
    sample_count = 0
    previous_sequence = None
    sequence_gaps = 0
    emitted_lines: list[str] = []

    if action == "record":
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        monitor_path = out_path.with_name(out_path.stem + ".monitor" + out_path.suffix)
        if out_path.exists() or monitor_path.exists():
            raise FileExistsError("capture or companion monitor file already exists")
        output_file = out_path.open("x", newline="", encoding="utf-8")
        try:
            monitor_file = monitor_path.open("x", newline="", encoding="utf-8")
        except Exception:
            output_file.close()
            raise
        writer = csv.writer(output_file)
        writer.writerow(["host_time", *[field.name for field in dataclasses.fields(Sample)]])
        output_file.flush()
        monitor_writer = csv.writer(monitor_file)
        monitor_writer.writerow(["host_time", "quantity", "channel", "counts", "value", "flags", "unit"])
        monitor_file.flush()

    def on_line(line: str, ts: float) -> None:
        nonlocal sample_count, previous_sequence, sequence_gaps
        emitted_lines.append(line)
        sample = Sample.parse(line)
        measurement = Monitor.parse(line)
        iso_now = datetime.fromtimestamp(ts, timezone.utc).isoformat()
        if sample is not None:
            if previous_sequence is not None and sample.sequence > previous_sequence + 1:
                sequence_gaps += sample.sequence - previous_sequence - 1
            previous_sequence = sample.sequence
            sample_count += 1
            if writer and output_file:
                writer.writerow([iso_now, *dataclasses.astuple(sample)])
                output_file.flush()
            elif action != "record":
                print(line, flush=True)
        elif measurement is not None:
            if monitor_writer and monitor_file:
                monitor_writer.writerow([iso_now, *dataclasses.astuple(measurement), measurement.unit])
                monitor_file.flush()
            elif action != "record":
                print(line, flush=True)

    try:
        async with BleSession(
            address,
            on_line,
            command_timeout=timeout,
            owner=owner,
            purpose=f"cli:{action}",
            lock_timeout=lock_timeout,
        ) as session:
            info_lines = await session.command("INFO")
            for l in info_lines:
                print(l, flush=True)
            if action == "command":
                for text in args.commands:
                    reply_lines = await session.command(text)
                    for l in reply_lines:
                        print(l, flush=True)
            elif action == "record":
                started = False
                try:
                    if not getattr(args, "no_start", False):
                        start_lines = await session.command(f"START {args.channels}")
                        for l in start_lines:
                            print(l, flush=True)
                        started = True
                    try:
                        await asyncio.wait_for(session.disconnected.wait(), timeout=args.seconds)
                        raise ConnectionError("board disconnected during recording")
                    except TimeoutError:
                        pass
                    session.check_health()
                finally:
                    if started and session.client and session.client.is_connected:
                        stop_lines = await session.command("STOP", cleanup=True)
                        for l in stop_lines:
                            print(l, flush=True)
                print(f"samples={sample_count} gaps={sequence_gaps} framing_errors={session.decoder.errors}", flush=True)
                if sample_count == 0:
                    raise RuntimeError("no samples received")
            return {
                "board": session.board_name,
                "address": session.address,
                "token": session.token,
                "samples": sample_count,
                "gaps": sequence_gaps,
                "lines": emitted_lines,
            }
    finally:
        if output_file:
            output_file.close()
        if monitor_file:
            monitor_file.close()


def _resolve_targets(board_arg: str | None, address_arg: str | None) -> list[str]:
    if board_arg and board_arg.upper() == "ALL":
        return [BOARD_ADDRESSES["A"], BOARD_ADDRESSES["B"]]
    if board_arg:
        return [resolve_board_address(board_arg)]
    if address_arg:
        return [resolve_board_address(address_arg)]
    return []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Unified pa-v52-a / pa-v52-b BLE driver with token-based concurrency control"
    )
    parser.add_argument(
        "--board",
        choices=["A", "B", "ALL", "a", "b", "all", "pa-v52-a", "pa-v52-b"],
        help="Board selector (A, B, ALL, pa-v52-a, or pa-v52-b)",
    )
    parser.add_argument("--address", help="Explicit BLE UUID/address or board alias")
    parser.add_argument("--owner", help="Caller/agent identifier for the BLE lock queue")
    parser.add_argument("--lock-timeout", type=float, default=DEFAULT_LOCK_TIMEOUT_S, help="Seconds to wait in lock queue")
    parser.add_argument("--timeout", type=float, default=10.0, help="BLE scan/command timeout in seconds")

    actions = parser.add_subparsers(dest="action", required=True)
    actions.add_parser("locks", help="Inspect current BLE lock holders and waiting queue")
    actions.add_parser("scan", help="Scan for pa-v52-a and pa-v52-b boards")
    actions.add_parser("probe", help="Read INFO and STATUS from one or both boards")

    control = actions.add_parser("command", help="Execute one or more ASCII commands")
    control.add_argument("commands", nargs="+", help='Quoted commands, e.g. "STATUS" "MONITOR"')

    record = actions.add_parser("record", help="Record current and monitor telemetry to CSV")
    record.add_argument("--output", type=Path, required=True)
    record.add_argument("--seconds", type=float, default=30.0)
    record.add_argument("--channels", type=int, choices=range(1, 16), default=15)
    record.add_argument("--no-start", action="store_true", help="Listen to an already running acquisition")

    args = parser.parse_args(argv)
    if args.timeout <= 0 or (args.action == "record" and args.seconds <= 0):
        parser.error("durations must be positive")

    try:
        if args.action == "locks":
            print(json.dumps(inspect_locks(), indent=2, ensure_ascii=False))
            return 0
        if args.action == "scan":
            locks = inspect_locks()["boards"]
            for row in scan_devices(min(args.timeout, 30.0)):
                bname = row["name"]
                bstate = locks.get(bname, {}).get("state", "idle")
                holder = locks.get(bname, {}).get("active")
                holder_str = f" [BUSY token={holder['token']} owner={holder['owner']}]" if holder else " [IDLE]"
                print(f"{row['address']}\t{bname}\t{row['rssi']} dBm{holder_str}")
            return 0
        if args.action == "probe":
            targets = _resolve_targets(args.board, args.address) or [BOARD_ADDRESSES["A"], BOARD_ADDRESSES["B"]]
            out = {}
            for t in targets:
                res = probe_device(t, owner=args.owner, lock_timeout=args.lock_timeout)
                out[res["board"]] = res
            print(json.dumps(out, indent=2, ensure_ascii=False))
            return 0
        if args.action == "command":
            targets = _resolve_targets(args.board, args.address)
            if len(targets) > 1:
                for t in targets:
                    sub_args = argparse.Namespace(**{**vars(args), "board": None, "address": t})
                    print(f"=== {canonical_board_name(None, t)} ({t}) ===", flush=True)
                    asyncio.run(run_session(sub_args))
            else:
                asyncio.run(run_session(args))
            return 0
        if args.action == "record":
            if args.board == "ALL":
                raise ValueError("record requires a single board (--board A or --board B)")
            asyncio.run(run_session(args))
            return 0
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
