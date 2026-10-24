"""V5.2 NUS protocol and verified fixed-potential configuration."""

from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass
from typing import Any, Callable

NUS_SERVICE = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
NUS_RX = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"
NUS_TX = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"
PROTOCOL = "v52-0.1"
COMMAND_TIMEOUT_S = 5.0
PERIOD_MS = {0: 124, 1: 242, 2: 476, 3: 945, 4: 1882, 5: 3757,
             6: 7507, 7: 15007, 8: 30007, 9: 60007, 10: 120007}
OFFSET_CODES = {"0nA": 0, "10pct": 1, "20pct": 2, "50pct": 3,
                "10nA": 4, "20nA": 5, "40nA": 6, "80nA": 7}
DEFAULTS = {
    "method": "it", "potential_v": -0.2, "initial_potential_v": -0.2,
    "working_electrode_v": 0.4, "prestep_s": 0.0, "duration_s": 180.0,
    "adaptive_stop": False, "target_rate_hz": 8.0, "sens_period_code": 0,
    "fit_window_s": 5.0, "fsr_nA": 50, "offset_mode": "20pct", "analysis_channel": 1,
}


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
    if (not 1 <= len(command) <= 159
            or any(ord(character) < 32 or ord(character) > 126 for character in command)):
        raise ValueError("command must be 1..159 printable ASCII characters")
    return (command + "\n").encode("ascii")


class BleSession:
    def __init__(self, address: str, on_line: Callable[[str, float], None] | None = None,
                 command_timeout: float | None = None):
        self.address = address
        self.on_line = on_line
        self.command_timeout = COMMAND_TIMEOUT_S if command_timeout is None else command_timeout
        self.decoder = LineDecoder()
        self.disconnected = asyncio.Event()
        self.responses: asyncio.Queue[str] = asyncio.Queue(maxsize=64)
        self.lock = asyncio.Lock()
        self.active_command: str | None = None
        self.failure: Exception | None = None
        self.client = None

    async def __aenter__(self):
        from bleak import BleakClient

        self.client = BleakClient(self.address, disconnected_callback=lambda client: self.disconnected.set(),
                                  timeout=10.0)
        await self.client.__aenter__()
        try:
            await self.client.start_notify(NUS_TX, self._notification)
        except BaseException:
            await self.client.__aexit__(*__import__("sys").exc_info())
            raise
        return self

    async def __aexit__(self, *args):
        return await self.client.__aexit__(*args)

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
                    lines.append(line)
                    expected = "OK " + self.active_command
                    if line == expected or line.startswith(expected + " "):
                        return lines
                    if self.active_command != "STATUS" or not line.startswith(("C,", "A,")):
                        raise RuntimeError(f"unexpected reply to {command}: {line}")
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
    requested = {"we_uv": round(settings["working_electrode_v"] * 1_000_000),
                 "re_uv": round((settings["working_electrode_v"] - settings["potential_v"]) * 1_000_000)}
    requested["potential_uv"] = requested["we_uv"] - requested["re_uv"]
    await session.command(f"DAC A {requested['we_uv']}")
    await session.command(f"DAC B {requested['re_uv']}")
    await session.command(f"PERIOD {settings['sens_period_code']}")
    channel_config = {"fsr_nA": settings["fsr_nA"], "offset_code": OFFSET_CODES[settings["offset_mode"]],
                      "conv_time_code": 0, "we_dac": 0, "re_dac": 1, "cp": 0}
    for channel in range(1, 5):
        await session.command(f"CONFIG {channel} " + " ".join(map(str, channel_config.values())))
    status = parse_status(await session.command("STATUS"))
    if status["mask"] != 0 or status["period"] != settings["sens_period_code"] or status["fault"] or status["io"]:
        raise RuntimeError("STATUS run/period/fault mismatch")
    if any(config != channel_config for config in status["channels"].values()):
        raise RuntimeError("STATUS channel configuration mismatch")
    actual = {"we_uv": status["dacs"]["A"]["uv"], "re_uv": status["dacs"]["B"]["uv"]}
    if any(actual[key] != quantize_dac(requested[key]) for key in ("we_uv", "re_uv")):
        raise RuntimeError("STATUS DAC configuration mismatch")
    actual["potential_uv"] = actual["we_uv"] - actual["re_uv"]
    return {"info": info, "status": status, "requested": requested, "actual": actual,
            "settings": settings, "config_confirmed": True}


def scan_devices(timeout: float = 4.0) -> list[dict[str, Any]]:
    async def scan():
        from bleak import BleakScanner

        devices = await BleakScanner.discover(timeout=timeout, return_adv=True)
        return [{"id": f"ble:{device.address}", "kind": "ble", "transport": "ble",
                 "address": device.address, "name": advertisement.local_name or device.name or "NUS device",
                 "rssi": advertisement.rssi, "board_profile": "v52", "selectable": True}
                for device, advertisement in devices.values()
                if device.name == "pA-V5.2" or advertisement.local_name == "pA-V5.2"
                or NUS_SERVICE in [value.lower() for value in advertisement.service_uuids]]

    if not math.isfinite(timeout) or not 0 < timeout <= 30:
        raise ValueError("scan timeout must be positive and at most 30 seconds")

    async def bounded_scan():
        try:
            return await asyncio.wait_for(scan(), timeout=timeout + 20.0)
        except TimeoutError as error:
            raise TimeoutError("Bluetooth scan timed out during startup or discovery; check Bluetooth permission and retry") from error

    return asyncio.run(bounded_scan())


def probe_device(address: str) -> dict[str, Any]:
    async def probe():
        async with BleSession(address) as session:
            info = parse_info(await session.command("INFO"))
            status = parse_status(await session.command("STATUS"))
            return {"info": info, "status": status, "protocol_verified": True, "board_profile": "v52"}

    return asyncio.run(probe())


def configure_device(address: str, settings: dict[str, Any]) -> dict[str, Any]:
    normalized = validate_settings(settings)

    async def configure():
        async with BleSession(address) as session:
            return await configure_session(session, normalized)

    return asyncio.run(configure())


def stop_device(address: str) -> dict[str, Any]:
    async def stop():
        async with BleSession(address) as session:
            reply = await session.command("STOP", cleanup=True)
            return {"stop_confirmed": True, "reply": reply[-1]}

    return asyncio.run(stop())
