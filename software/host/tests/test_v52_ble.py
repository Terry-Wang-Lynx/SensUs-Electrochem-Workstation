from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace

import pytest

from pa_host import v52_ble


INFO = "OK INFO fw=v52-0.1 part=0x34 rev=0x84 channels=4 shared_dacs=4 init=0"


class FakeClient:
    instances = []
    responses = {}
    start_lines = []
    start_hook = None

    def __init__(self, address, disconnected_callback, timeout):
        self.address = address
        self.disconnected_callback = disconnected_callback
        self.is_connected = False
        self.buffer = bytearray()
        self.commands = []
        self.dacs = {"A": 1067, "B": 1600, "C": 0, "D": 0}
        self.channels = {channel: [50, 2, 0, 0, 1, 0] for channel in range(1, 5)}
        self.period = 0
        self.mask = 0
        self.sequence = 50
        self.instances.append(self)

    async def __aenter__(self):
        self.is_connected = True
        return self

    async def __aexit__(self, *args):
        self.is_connected = False

    async def start_notify(self, characteristic, callback):
        self.callback = callback

    def emit(self, lines):
        for line in lines:
            if line.startswith("D,"):
                self.sequence = max(self.sequence, int(line.split(",")[1]) + 1)
        self.callback(None, ("\n".join(lines) + "\n").encode())

    async def write_gatt_char(self, characteristic, payload, response):
        self.buffer.extend(payload)
        if not self.buffer.endswith(b"\n"):
            return
        command = self.buffer.decode().strip()
        self.buffer.clear()
        self.commands.append(command)
        parts = command.split()
        verb = parts[0]
        if command in self.responses:
            lines = self.responses[command]
        elif verb == "INFO":
            lines = [INFO]
        elif verb == "STATUS":
            lines = [f"C,{channel}," + ",".join(map(str, values))
                     for channel, values in self.channels.items()]
            lines += [f"A,{name},{code},{code * 375}" for name, code in self.dacs.items()]
            lines += [f"OK STATUS mask={self.mask} period={self.period} fault=0 io=0 seq={self.sequence} overflow=0 tx_drop=0 ble=1"]
        else:
            if verb == "DAC":
                self.dacs[parts[1]] = (int(parts[2]) + 187) // 375
            elif verb == "CONFIG":
                self.channels[int(parts[1])] = list(map(int, parts[2:]))
            elif verb == "PERIOD":
                self.period = int(parts[1])
            elif verb == "START":
                self.mask = int(parts[1])
            elif verb == "STOP":
                self.mask = 0
            lines = [f"OK {verb}"]
        if lines:
            self.emit(lines)
        if verb == "START":
            if self.start_lines:
                self.emit(self.start_lines)
            if self.start_hook:
                type(self).start_hook(self)


@pytest.fixture
def fake_bleak(monkeypatch):
    FakeClient.instances = []
    FakeClient.responses = {}
    FakeClient.start_lines = []
    FakeClient.start_hook = None
    monkeypatch.setitem(sys.modules, "bleak", SimpleNamespace(BleakClient=FakeClient))
    return FakeClient


def test_settings_defaults_quantization_and_common_keys():
    settings = v52_ble.validate_settings({"fit_window_s": 5, "experiment": "keep"})
    assert settings["board_profile"] == "v52"
    assert settings["protocol"] == "v52-0.1"
    assert settings["working_electrode_v"] == 0.4
    assert settings["potential_v"] == settings["initial_potential_v"] == -0.2
    assert settings["offset_mode"] == "20pct"
    assert settings["offset_nA"] == 10
    assert settings["analysis_channel"] == 1
    assert settings["experiment"] == "keep"
    assert v52_ble.quantize_dac(400000) == 400125


@pytest.mark.parametrize("change", [
    {"method": "cv"}, {"method": "eis"}, {"prestep_s": 1},
    {"fsr_nA": 4000}, {"offset_mode": "9nA"}, {"offset_mode": "80nA", "fsr_nA": 50},
    {"analysis_channel": 0}, {"analysis_channel": 1.5}, {"sens_period_code": 11},
    {"duration_s": 9}, {"duration_s": 3601}, {"duration_s": float("nan")},
    {"working_electrode_v": 0.1}, {"working_electrode_v": 1.6},
    {"potential_v": 0.4}, {"conv_time_code": 1}, {"charge_pump": 1},
])
def test_settings_reject_unsupported_or_unsafe_values(change):
    with pytest.raises(ValueError):
        v52_ble.validate_settings(change)


def test_settings_canonicalizes_unused_initial_and_bounds_adaptive():
    settings = v52_ble.validate_settings({"initial_potential_v": 0.2, "adaptive_stop": True,
                                          "duration_s": 0, "sens_period_code": 10})
    assert settings["initial_potential_v"] == -0.2
    assert settings["duration_s"] == 0
    assert settings["sens_period_code"] == 10


def test_decoder_reassembles_packets_and_discards_entire_oversize_line():
    decoder = v52_ble.LineDecoder(limit=12)
    assert decoder.feed(b"OK ST") == []
    assert decoder.feed(b"OP\r\nOK INFO\n") == ["OK STOP", "OK INFO"]
    assert decoder.feed(b"x" * 30 + b"OK STOP\nOK INFO\n") == ["OK INFO"]
    assert decoder.errors == 1
    assert decoder.feed(b"\xff\nOK STOP\n") == ["OK STOP"]
    assert decoder.errors == 2


def test_sample_and_monitor_preserve_sign_units_and_flags():
    sample = v52_ble.Sample.parse("D,22,1234,3,16,-9987793,4")
    assert sample.channel == 3
    assert sample.current_fa == -9987793
    assert sample.flags == 4
    assert v52_ble.Monitor.parse("M,TEMP,0,5000,25000,0").unit == "milliC"
    assert v52_ble.Monitor.parse("M,VDD,0,2000,3000000,0").unit == "uV"
    with pytest.raises(ValueError):
        v52_ble.Sample.parse("D,1,2,5,16,42,4")
    with pytest.raises(ValueError):
        v52_ble.Monitor.parse("M,VDD,1,2000,3000000,0")


def test_configure_requires_full_status_and_reports_actual_dacs(fake_bleak):
    result = v52_ble.configure_device("test", {"analysis_channel": 3})
    assert result["config_confirmed"] is True
    assert result["status"]["channels"]["3"]["fsr_nA"] == 50
    assert result["requested"]["we_uv"] == 400000
    assert result["actual"]["we_uv"] == 400125
    assert result["actual"]["potential_uv"] == -199875
    assert fake_bleak.instances[0].commands[0] == "STOP"
    assert "START 15" not in fake_bleak.instances[0].commands


@pytest.mark.parametrize("lines", [
    ["OK STATUS mask=0 period=0 fault=0 io=0 seq=0 overflow=0 tx_drop=0 ble=1"],
    ["C,1,50,2,0,0,1,0"] * 4 + ["OK STATUS mask=0 period=0 fault=0 io=0 seq=0 overflow=0 tx_drop=0 ble=1"],
])
def test_configure_rejects_ack_without_complete_current_status(fake_bleak, lines):
    fake_bleak.responses["STATUS"] = lines
    with pytest.raises((ValueError, RuntimeError), match="STATUS|channel|status"):
        v52_ble.configure_device("test", {})


def test_configure_rejects_valid_but_wrong_channel_configuration(fake_bleak, monkeypatch):
    original = fake_bleak.write_gatt_char

    async def wrong_config(client, characteristic, payload, response):
        if payload == b"STATUS\n":
            client.channels[4][0] = 100
        await original(client, characteristic, payload, response)

    monkeypatch.setattr(fake_bleak, "write_gatt_char", wrong_config)
    with pytest.raises(RuntimeError, match="channel|STATUS"):
        v52_ble.configure_device("test", {})


def test_session_rejects_wrong_ack_and_times_out_missing_ack(fake_bleak):
    async def exercise():
        async with v52_ble.BleSession("test", command_timeout=0.02) as session:
            fake_bleak.responses["INFO"] = ["OK STOP"]
            with pytest.raises(RuntimeError, match="unexpected"):
                await session.command("INFO")
            fake_bleak.responses["START 15"] = []
            with pytest.raises(TimeoutError):
                await session.command("START 15")
            assert await session.command("STOP") == ["OK STOP"]

    asyncio.run(exercise())


def test_probe_rejects_other_firmware_and_stop_verifies_ack(fake_bleak):
    fake_bleak.responses["INFO"] = [INFO.replace("v52-0.1", "legacy")]
    with pytest.raises(RuntimeError, match="firmware|protocol"):
        v52_ble.probe_device("test")
    fake_bleak.responses.clear()
    assert v52_ble.stop_device("test")["stop_confirmed"] is True


def test_scan_only_returns_explicit_nus_candidates(monkeypatch):
    async def discover(**kwargs):
        return {
            "one": (SimpleNamespace(name="pA-V5.2", address="one"),
                    SimpleNamespace(local_name=None, rssi=-42, service_uuids=[])),
            "two": (SimpleNamespace(name="Other", address="two"),
                    SimpleNamespace(local_name=None, rssi=-50, service_uuids=[])),
        }

    monkeypatch.setitem(sys.modules, "bleak", SimpleNamespace(BleakScanner=SimpleNamespace(discover=discover)))
    result = v52_ble.scan_devices(0.1)
    assert len(result) == 1
    assert result[0]["id"] == "ble:one"
    assert result[0]["rssi"] == -42
    assert result[0]["transport"] == result[0]["kind"] == "ble"


def test_period_seconds_supports_all_firmware_periods():
    assert v52_ble.period_seconds(0) == 0.124
    assert v52_ble.period_seconds(10) == 120.007
    with pytest.raises(ValueError):
        v52_ble.period_seconds(11)


def test_scan_timeout_explains_bluetooth_startup_failure(monkeypatch):
    async def discover(**kwargs):
        raise TimeoutError()

    monkeypatch.setitem(sys.modules, "bleak", SimpleNamespace(BleakScanner=SimpleNamespace(discover=discover)))
    with pytest.raises(TimeoutError, match="Bluetooth.*timed out"):
        v52_ble.scan_devices(1)
