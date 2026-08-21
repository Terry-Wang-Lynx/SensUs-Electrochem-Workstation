import json
import logging
import subprocess
from typing import Any

import pytest

from pa_host import jlink_usb


def test_windows_discovery_keeps_legacy_and_composite_probes_separate() -> None:
    rows = [
        {
            "instance_id": r"USB\VID_1366&PID_0101\000000123456",
            "friendly_name": "J-Link",
            "bus_description": "J-Link",
            "status": "Error",
            "problem_code": 28,
            "container_id": "legacy-container",
            "location": "Port_#0001.Hub_#0002",
        },
        {
            "instance_id": r"USB\VID_1366&PID_0105\000029734569",
            "friendly_name": "J-Link",
            "status": "OK",
            "problem_code": 0,
            "container_id": "new-container",
        },
        {
            "instance_id": r"USB\VID_1366&PID_0105&MI_02\000029734569",
            "friendly_name": "J-Link driver interface",
            "status": "Error",
            "problem_code": 28,
            "container_id": "new-container",
            "parent": r"USB\VID_1366&PID_0105\000029734569",
        },
    ]

    infos = sorted(jlink_usb._windows_infos(rows), key=lambda info: info.pid)

    assert [(item.pid, item.serial_number) for item in infos] == [
        (0x0101, "000000123456"),
        (0x0105, "000029734569"),
    ]
    assert infos[0].status == "Error"
    assert infos[0].problem_code == 28
    assert infos[1].interface == ""


def test_windows_discovery_uses_parent_serial_when_only_interface_is_visible() -> None:
    infos = jlink_usb._windows_infos([{
        "instance_id": r"USB\VID_1366&PID_0105&MI_02\7&ABC&0&0002",
        "friendly_name": "J-Link",
        "parent": r"USB\VID_1366&PID_0105\000029734569",
        "container_id": "new-container",
    }])

    assert len(infos) == 1
    assert infos[0].serial_number == "000029734569"
    assert infos[0].interface == "MI_02"


def test_windows_discovery_preserves_duplicate_serials_without_container_id() -> None:
    rows = [
        {
            "instance_id": r"USB\VID_1366&PID_0101\5&AAA&0&1",
            "serial_number": "000000123456",
            "friendly_name": "J-Link",
        },
        {
            "instance_id": r"USB\VID_1366&PID_0101\5&BBB&0&1",
            "serial_number": "000000123456",
            "friendly_name": "J-Link",
        },
    ]

    infos = jlink_usb._windows_infos(rows)

    assert len(infos) == 2
    assert {info.instance_id for info in infos} == {
        row["instance_id"] for row in rows
    }
    assert {info.serial_number for info in infos} == {"000000123456"}


def test_macos_ioreg_discovery_does_not_require_a_serial_port() -> None:
    payload = {
        "IORegistryEntryChildren": [{
            "IORegistryEntryName": "USB hub",
            "IORegistryEntryChildren": [{
                "IORegistryEntryID": 1234,
                "idVendor": 0x1366,
                "idProduct": 0x0101,
                "USB Product Name": "J-Link",
                "USB Vendor Name": "SEGGER",
                "USB Serial Number": "000000123456",
                "locationID": 0x01140000,
            }],
        }],
    }

    infos = jlink_usb._macos_infos(payload)

    assert len(infos) == 1
    assert infos[0].device == "ioreg:1234"
    assert infos[0].serial_number == "000000123456"
    assert infos[0].pid == 0x0101
    assert infos[0].location == "0x01140000"


def test_discovery_is_empty_on_unsupported_platform() -> None:
    assert jlink_usb.discover_jlink_usb_devices("linux") == []


# --- 可选路径的失败绝不能是致命的 ------------------------------------------
# 真机事故:Win11 上 PowerShell 枚举每轮超时,subprocess.TimeoutExpired
# 逃出所有调用方的 except,device-discovery 线程无限循环地死掉,
# probing 永远 true ⇒ 整块 USB 板永久不可用。


def _powershell_result(stdout: str, returncode: int = 0) -> Any:
    return subprocess.CompletedProcess(
        args=["powershell.exe"], returncode=returncode,
        stdout=stdout, stderr="",
    )


@pytest.mark.parametrize(
    "failure",
    [
        subprocess.TimeoutExpired(cmd="powershell.exe", timeout=6.0),
        OSError("powershell.exe not found"),
        MemoryError("解析代码自己炸了也不该弄死发现线程"),
    ],
    ids=["timeout", "oserror", "unexpected"],
)
def test_windows_discovery_never_raises_when_powershell_fails(
    monkeypatch: pytest.MonkeyPatch, failure: BaseException,
) -> None:
    def explode(*args: Any, **kwargs: Any) -> Any:
        raise failure

    monkeypatch.setattr(jlink_usb.subprocess, "run", explode)

    assert jlink_usb.discover_jlink_usb_devices(platform="win32") == []


@pytest.mark.parametrize(
    "stdout, returncode",
    [
        ("", 1),                      # returncode != 0 → RuntimeError
        ("not json at all", 0),       # json.JSONDecodeError
        ('{"instance_id": ', 0),      # 截断的 JSON
        ("42", 0),                    # 合法 JSON 但不是列表 → RuntimeError
    ],
    ids=["returncode", "garbage", "truncated", "scalar"],
)
def test_windows_discovery_never_raises_on_bad_output(
    monkeypatch: pytest.MonkeyPatch, stdout: str, returncode: int,
) -> None:
    monkeypatch.setattr(
        jlink_usb.subprocess, "run",
        lambda *a, **k: _powershell_result(stdout, returncode),
    )

    assert jlink_usb.discover_jlink_usb_devices(platform="win32") == []


def test_macos_discovery_never_raises_when_ioreg_times_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def explode(*args: Any, **kwargs: Any) -> Any:
        raise subprocess.TimeoutExpired(cmd="ioreg", timeout=8.0)

    monkeypatch.setattr(jlink_usb.subprocess, "run", explode)

    assert jlink_usb.discover_jlink_usb_devices(platform="darwin") == []


def test_macos_discovery_never_raises_on_malformed_plist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        jlink_usb.subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(
            args=["ioreg"], returncode=0, stdout=b"not a plist", stderr=b"",
        ),
    )

    assert jlink_usb.discover_jlink_usb_devices(platform="darwin") == []


@pytest.mark.parametrize(
    "stdout",
    ["[]", "", "   ", "null", "﻿[]"],
    ids=["empty-array", "empty-string", "whitespace", "null", "bom"],
)
def test_no_matching_device_parses_as_an_empty_list(stdout: str) -> None:
    """新 payload 在"没有匹配设备"时的每种可能输出都必须解析成空列表。"""
    assert jlink_usb._json_rows(stdout) == []


def test_windows_discovery_returns_empty_list_when_no_jlink_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 用户那台机器的真实情况:VID_1366 设备数 = 0。
    monkeypatch.setattr(
        jlink_usb.subprocess, "run", lambda *a, **k: _powershell_result("[]"),
    )

    assert jlink_usb.discover_jlink_usb_devices(platform="win32") == []


def test_windows_discovery_still_parses_a_present_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """宽捕获不许把正常路径也吞掉 —— 有设备时必须照旧解析出来。"""
    monkeypatch.setattr(
        jlink_usb.subprocess, "run", lambda *a, **k: _powershell_result(
            json.dumps([{
                "instance_id": r"USB\VID_1366&PID_0105\000029734569",
                "friendly_name": "J-Link",
                "status": "OK",
                "problem_code": 0,
                "container_id": "new-container",
            }]),
        ),
    )

    infos = jlink_usb.discover_jlink_usb_devices(platform="win32")

    assert [info.serial_number for info in infos] == ["000029734569"]


def test_discovery_timeout_is_well_below_the_discovery_poll_period() -> None:
    # 25s 与轮询周期同量级 ⇒ 超时窗口永远追不上自己。
    assert 0 < jlink_usb._WINDOWS_DISCOVERY_TIMEOUT_S <= 10
    assert 0 < jlink_usb._MACOS_DISCOVERY_TIMEOUT_S <= 10


def test_windows_payload_filters_on_the_provider_side(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """枚举必须服务端过滤,且仍取全部字段。"""
    captured: dict[str, Any] = {}

    def capture(argv: Any, **kwargs: Any) -> Any:
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return _powershell_result("[]")

    monkeypatch.setattr(jlink_usb.subprocess, "run", capture)

    assert jlink_usb._windows_payload() == []
    script = captured["argv"][-1]
    assert "Get-PnpDevice -PresentOnly -InstanceId 'USB\\VID_1366*'" in script
    # 无匹配不是失败:必须显式压掉 Get-PnpDevice 的 "No matching" 报错,
    # 否则脚本顶部的 $ErrorActionPreference='Stop' 会把它变成终止错误。
    assert "-ErrorAction SilentlyContinue" in script
    # 空结果必须是合法 JSON 空数组,不能靠管道(会输出空串/"null")。
    assert "ConvertTo-Json -InputObject" in script
    # 语义不变:十个字段一个都不能少。
    for field in (
        "instance_id", "friendly_name", "class_name", "status",
        "bus_description", "serial_number", "parent", "container_id",
        "location", "problem_code",
    ):
        assert field in script
    assert captured["kwargs"]["timeout"] == jlink_usb._WINDOWS_DISCOVERY_TIMEOUT_S


def test_discovery_failure_is_recorded_through_the_injected_sink(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """失败必须留痕 —— 静默吞异常等于把故障藏起来。"""
    events: list[tuple[Any, ...]] = []

    def explode(*args: Any, **kwargs: Any) -> Any:
        raise subprocess.TimeoutExpired(cmd="powershell.exe", timeout=6.0)

    monkeypatch.setattr(jlink_usb.subprocess, "run", explode)
    monkeypatch.setattr(
        jlink_usb, "_DIAGNOSTICS_SINK",
        lambda level, event, message, **ctx: events.append(
            (level, event, message, ctx)
        ),
    )

    assert jlink_usb.discover_jlink_usb_devices(platform="win32") == []
    assert len(events) == 1
    level, event, _message, context = events[0]
    assert level == "warning"
    assert event == "device.jlink.usb_discovery_failed"
    assert context["platform"] == "win32"
    assert "TimeoutExpired" in context["error"]


def test_a_broken_sink_cannot_break_device_discovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def explode(*args: Any, **kwargs: Any) -> Any:
        raise subprocess.TimeoutExpired(cmd="powershell.exe", timeout=6.0)

    def broken_sink(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("诊断落盘自己坏了")

    monkeypatch.setattr(jlink_usb.subprocess, "run", explode)
    monkeypatch.setattr(jlink_usb, "_DIAGNOSTICS_SINK", broken_sink)

    assert jlink_usb.discover_jlink_usb_devices(platform="win32") == []


def test_failure_falls_back_to_module_logging_without_a_sink(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    def explode(*args: Any, **kwargs: Any) -> Any:
        raise subprocess.TimeoutExpired(cmd="powershell.exe", timeout=6.0)

    monkeypatch.setattr(jlink_usb.subprocess, "run", explode)
    monkeypatch.setattr(jlink_usb, "_DIAGNOSTICS_SINK", None)

    with caplog.at_level(logging.WARNING, logger=jlink_usb.__name__):
        assert jlink_usb.discover_jlink_usb_devices(platform="win32") == []

    assert any(
        "TimeoutExpired" in record.getMessage() for record in caplog.records
    )


def test_set_diagnostics_sink_round_trips() -> None:
    original = jlink_usb._DIAGNOSTICS_SINK
    try:
        sink = lambda *a, **k: None  # noqa: E731
        jlink_usb.set_diagnostics_sink(sink)
        assert jlink_usb._DIAGNOSTICS_SINK is sink
        jlink_usb.set_diagnostics_sink(None)
        assert jlink_usb._DIAGNOSTICS_SINK is None
    finally:
        jlink_usb.set_diagnostics_sink(original)
