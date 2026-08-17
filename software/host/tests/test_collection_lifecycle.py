from __future__ import annotations

import signal
import socket
import subprocess
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from pa_host import collect, it_tool


class _CommandSocket:
    def __init__(self) -> None:
        self.sent: list[bytes] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def settimeout(self, _timeout: float) -> None:
        pass

    def sendall(self, payload: bytes) -> None:
        self.sent.append(payload)

    def recv(self, _size: int) -> bytes:
        return b"IT_READY\n"


def test_command_file_rejects_non_ascii_line_and_forwards_the_next(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    cmd_file = tmp_path / "commands.txt"
    cmd_file.write_text("SET label=测试\nGET\n", encoding="utf-8")
    sock = _CommandSocket()
    monkeypatch.setattr(collect.socket, "create_connection", lambda *_args, **_kw: sock)

    lines = collect.read_socket_lines(
        "127.0.0.1", 19021, trigger=None, cmd_file=cmd_file
    )

    assert next(lines) == "IT_READY"
    assert sock.sent == [b"GET\n"]
    assert "拒绝命令文件中的非 ASCII 命令" in capsys.readouterr().err


def test_trigger_argument_rejects_non_ascii_before_collection_starts() -> None:
    with pytest.raises(SystemExit) as exc_info:
        collect.main(["--out", "run.csv", "--socket", "127.0.0.1:19021",
                      "--trigger", "开始"])

    assert exc_info.value.code == 2


def test_armed_duration_starts_only_after_confirmed_acquisition() -> None:
    duration = collect._AcquisitionDuration(120.0)

    # ARMED configuration checks may take arbitrarily long without consuming
    # any of the requested acquisition duration.
    assert not duration.expired(10_000.0)

    duration.mark_started(10_000.0)
    assert not duration.expired(10_120.0)
    assert duration.expired(10_120.001)

    # Duplicate START markers must not extend an already-running collection.
    duration.mark_started(20_000.0)
    assert duration.expired(20_000.0)


def _memory_transport(tmp_path: Path) -> collect.JLinkMemoryRTT:
    transport = collect.JLinkMemoryRTT.__new__(collect.JLinkMemoryRTT)
    transport._lock = threading.Lock()
    transport._close_lock = threading.Lock()
    transport._cancel = threading.Event()
    transport._operation_done = threading.Event()
    transport._operation_done.set()
    transport._closed = False
    transport._temporary = tempfile.TemporaryDirectory(dir=tmp_path)
    transport._sequence = 0
    transport.up_descriptor = 0x20000100
    transport.down_descriptor = 0x20000200
    transport.up_buffer = 0x20001000
    transport.down_buffer = 0x20002000
    transport.up_size = 8
    transport.down_size = 8
    return transport


def test_jlink_memory_rtt_receives_wrapped_up_buffer_in_order(
    tmp_path: Path,
) -> None:
    transport = _memory_transport(tmp_path)
    commands_seen: list[str] = []
    transport._descriptor = lambda *, up: [0, transport.up_buffer, 8, 2, 6, 0]

    def run(*commands: str, **_kwargs) -> str:
        commands_seen.extend(commands)
        paths = [
            Path(command.split('"')[1]) for command in commands
            if command.startswith("savebin ")
        ]
        paths[0].write_bytes(b"ab")
        paths[1].write_bytes(b"cd")
        return f"{transport.up_descriptor + 16:08X} = 00000002\n"

    transport._run = run
    try:
        assert transport.recv() == b"abcd"
    finally:
        transport.close()

    assert any("0x20001006, 0x2" in command for command in commands_seen)
    assert any("0x20001000, 0x2" in command for command in commands_seen)
    assert any("w4 0x20000110, 0x00000002" in command
               for command in commands_seen)


def test_jlink_memory_rtt_writes_wrapped_down_buffer_and_verifies_pointer(
    tmp_path: Path,
) -> None:
    transport = _memory_transport(tmp_path)
    commands_seen: list[str] = []
    transport._descriptor = lambda *, up: [0, transport.down_buffer, 8, 6, 4, 0]

    def run(*commands: str, **_kwargs) -> str:
        commands_seen.extend(commands)
        paths = [
            Path(command.split('"')[1]) for command in commands
            if command.startswith("savebin ")
        ]
        paths[0].write_bytes(b"ab")
        paths[1].write_bytes(b"cd")
        return f"{transport.down_descriptor + 12:08X} = 00000002\n"

    transport._run = run
    try:
        transport.sendall(b"abcd")
    finally:
        transport.close()

    writes = [command for command in commands_seen if command.startswith("w1 ")]
    assert writes == [
        "w1 0x20002006, 0x61", "w1 0x20002007, 0x62",
        "w1 0x20002000, 0x63", "w1 0x20002001, 0x64",
    ]
    assert any("w4 0x2000020C, 0x00000002" in command
               for command in commands_seen)


def test_jlink_memory_rtt_rejects_unconfirmed_pointer_write(
    tmp_path: Path,
) -> None:
    transport = _memory_transport(tmp_path)
    transport._descriptor = lambda *, up: [0, transport.up_buffer, 8, 2, 0, 0]

    def run(*commands: str, **_kwargs) -> str:
        path = next(Path(command.split('"')[1]) for command in commands
                    if command.startswith("savebin "))
        path.write_bytes(b"ab")
        return f"{transport.up_descriptor + 16:08X} = 00000000\n"

    transport._run = run
    try:
        with pytest.raises(RuntimeError, match="上行读指针"):
            transport.recv()
    finally:
        transport.close()


def test_jlink_bridge_termination_interrupts_blocked_transport() -> None:
    class BlockingTransport:
        def __init__(self) -> None:
            self.entered = threading.Event()
            self.cancelled = threading.Event()
            self.closed = threading.Event()

        def sendall(self, _payload: bytes) -> None:
            pass

        def recv(self) -> bytes:
            self.entered.set()
            self.cancelled.wait(2)
            raise collect.JLinkOperationCancelled("stopped")

        def cancel(self) -> None:
            self.cancelled.set()

        def close(self) -> None:
            self.cancel()
            self.closed.set()

    transport = BlockingTransport()
    bridge = collect.JLinkMemoryRTTBridge(transport, 0)
    client = socket.create_connection(bridge._listener.getsockname(), timeout=1)
    assert transport.entered.wait(1)

    bridge.kill()
    assert bridge.wait(timeout=2) == 0
    assert transport.closed.is_set()
    assert bridge.poll() == 0
    client.close()


def test_jlink_bridge_control_socket_releases_an_orphan() -> None:
    class BlockingTransport:
        def __init__(self) -> None:
            self.cancelled = threading.Event()

        def sendall(self, _payload: bytes) -> None:
            pass

        def recv(self) -> bytes:
            self.cancelled.wait(2)
            raise collect.JLinkOperationCancelled("stopped")

        def cancel(self) -> None:
            self.cancelled.set()

        def close(self) -> None:
            self.cancel()

    transport = BlockingTransport()
    bridge = collect.JLinkMemoryRTTBridge(transport, 0, control_port=0)
    client = socket.create_connection(bridge._listener.getsockname(), timeout=1)
    assert bridge._control_listener is not None
    control = socket.create_connection(
        bridge._control_listener.getsockname(), timeout=1
    )
    control.sendall(collect.BRIDGE_SHUTDOWN_COMMAND)
    control.close()

    assert bridge.wait(timeout=2) == 0
    assert transport.cancelled.is_set()
    client.close()


def test_interruptible_jlink_script_terminates_active_commander(
    tmp_path: Path, monkeypatch,
) -> None:
    executable = tmp_path / "JLinkExe"
    executable.touch()
    cancel = threading.Event()

    class Process:
        returncode = None

        def __init__(self) -> None:
            self.terminated = False

        def communicate(self, timeout=None):
            if not self.terminated:
                cancel.set()
                raise subprocess.TimeoutExpired("JLinkExe", timeout)
            self.returncode = -15
            return "", ""

        def terminate(self) -> None:
            self.terminated = True

        def kill(self) -> None:
            self.terminated = True

    process = Process()
    monkeypatch.setattr(collect.subprocess, "Popen", lambda *_args, **_kwargs: process)

    with pytest.raises(collect.JLinkOperationCancelled):
        collect.run_jlink_script(
            "q\n", executable=executable, cancel_event=cancel,
        )

    assert process.terminated is True


def test_measure_forwards_early_sigterm_waits_and_returns_stop_code(monkeypatch) -> None:
    events: list[object] = []
    original_handler = object()
    active_handler: dict[int, object] = {}

    class Child:
        def send_signal(self, signum: int) -> None:
            events.append(("signal", signum))

        def wait(self) -> int:
            events.append("wait")
            return 7

    child = Child()

    def install_handler(signum: int, handler):
        previous = active_handler.get(signum, original_handler)
        active_handler[signum] = handler
        return previous

    def start_child(command):
        events.append(("spawn", command))
        # Exercise the race between OS process creation and assignment to ``child``.
        active_handler[signal.SIGTERM](signal.SIGTERM, None)
        return child

    monkeypatch.setattr(it_tool.signal, "signal", install_handler)
    monkeypatch.setattr(it_tool.subprocess, "Popen", start_child)
    args = SimpleNamespace(
        out=Path("run.csv"), duration=120.0, idle_timeout=25.0,
        cv=False, start_jlink=False, socket=None, cmd_file=None,
        cell_v=None, audit=None, raw_log=None, trigger=None,
    )

    assert it_tool._cmd_measure(args) == 3
    assert events[1:] == [("signal", signal.SIGTERM), "wait"]
    assert active_handler[signal.SIGTERM] is original_handler


def _cfg_snapshot(
    *, epoch: int, request_id: str | None, fsr: int = 2,
    verify_ok: int | None = 1,
) -> list[dict[str, object]]:
    request = {} if request_id is None else {"req": request_id}
    confirmed: dict[str, object] = {
        "kind": "CFG_CONFIRMED", "ep": epoch, **request,
    }
    if verify_ok is not None:
        confirmed["verify_ok"] = verify_ok
    return [
        {"kind": "CFG_APPLIED", "ep": epoch, "fsr": fsr, **request},
        {"kind": "CFG_DERIVED", "ep": epoch, "bits": 18 + fsr, **request},
        confirmed,
    ]


def _cfg_row(row: list[str]) -> dict[str, str]:
    return dict(zip(collect.CFG_EVENT_COLUMNS, row))


def test_cfg_accumulator_never_confirms_verify_failed_snapshot() -> None:
    accumulator = collect.CfgEventAccumulator()
    events = [
        {"kind": "CFG_FAULT", "ep": 4, "req": "failed",
         "cause": "verify_mismatch"},
        *_cfg_snapshot(epoch=4, request_id="failed", verify_ok=0),
    ]

    assert all(accumulator.feed(event) is None for event in events)
    assert accumulator.rows == []

    result = None
    for event in _cfg_snapshot(epoch=4, request_id="recheck", verify_ok=1):
        result = accumulator.feed(event)
    assert result is not None
    assert _cfg_row(result)["confirmed"] == "1"
    assert _cfg_row(result)["req"] == "recheck"


def test_cfg_accumulator_keeps_tagged_get_after_same_epoch_boot_snapshot() -> None:
    accumulator = collect.CfgEventAccumulator()

    for event in _cfg_snapshot(epoch=7, request_id=None, fsr=1, verify_ok=None):
        accumulator.feed(event)
    for event in _cfg_snapshot(epoch=7, request_id="formal", fsr=5):
        accumulator.feed(event)

    rows = [_cfg_row(row) for row in accumulator.rows]
    assert len(rows) == 2
    assert [(row["req"], row["fsr"]) for row in rows] == [
        ("", "1"), ("formal", "5"),
    ]


def test_cfg_accumulator_does_not_mix_interleaved_request_snapshots() -> None:
    accumulator = collect.CfgEventAccumulator()
    events = [
        {"kind": "CFG_APPLIED", "ep": 9, "req": "a", "fsr": 1},
        {"kind": "CFG_APPLIED", "ep": 9, "req": "b", "fsr": 5},
        {"kind": "CFG_DERIVED", "ep": 9, "req": "b", "bits": 23},
        {"kind": "CFG_DERIVED", "ep": 9, "req": "a", "bits": 19},
        {"kind": "CFG_CONFIRMED", "ep": 9, "req": "a", "verify_ok": 1},
        {"kind": "CFG_CONFIRMED", "ep": 9, "req": "b", "verify_ok": 1},
    ]
    for event in events:
        accumulator.feed(event)

    rows = {row["req"]: row for row in map(_cfg_row, accumulator.rows)}
    assert (rows["a"]["fsr"], rows["a"]["bits"]) == ("1", "19")
    assert (rows["b"]["fsr"], rows["b"]["bits"]) == ("5", "23")
