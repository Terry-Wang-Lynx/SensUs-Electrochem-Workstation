from pathlib import Path

from pa_host import collect
from pa_host.collect import read_serial_lines


class FakeSerial:
    def __init__(self, *, port, baudrate, timeout, write_timeout):
        self.port = port
        self.baudrate = baudrate
        self.initial_timeout = timeout
        self.timeout = timeout
        self.write_timeout = write_timeout
        self.writes: list[bytes] = []
        self.chunks = [
            b"tail-of-an-old-CELL_V-line\nCFG_BOOT ep=1 ms=0 ",
            b"fw=v5.1-usb2-req reason=boot\nIT_START run=1 target_mv=200\n",
        ]

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self, size):
        return self.chunks.pop(0) if self.chunks else b""

    def write(self, data):
        self.writes.append(data)
        return len(data)

    def flush(self):
        return None


def test_serial_transport_preserves_line_protocol_and_sends_trigger() -> None:
    instances: list[FakeSerial] = []

    def factory(**kwargs):
        stream = FakeSerial(**kwargs)
        instances.append(stream)
        return stream

    lines = read_serial_lines(
        "/dev/cu.usbmodem-test", trigger="FRESH_START", serial_factory=factory
    )
    assert next(lines).startswith("CFG_BOOT ")
    assert next(lines) == "IT_START run=1 target_mv=200"
    lines.close()

    assert instances[0].port == "/dev/cu.usbmodem-test"
    assert instances[0].writes[0] == b"GET\nSTATUS\nSTART\n"


def test_serial_transport_forwards_append_only_command_file(tmp_path: Path) -> None:
    command_file = tmp_path / "cmd.txt"
    command_file.write_text("# comment\nGET\n", encoding="utf-8")
    instances: list[FakeSerial] = []

    def factory(**kwargs):
        stream = FakeSerial(**kwargs)
        instances.append(stream)
        return stream

    lines = read_serial_lines(
        "/dev/cu.usbmodem-test", cmd_file=command_file, serial_factory=factory
    )
    next(lines)
    lines.close()

    assert instances[0].writes == [b"GET\nSTATUS\n", b"GET\n"]


def test_serial_armed_mode_waits_for_command_file_start() -> None:
    instances: list[FakeSerial] = []

    def factory(**kwargs):
        stream = FakeSerial(**kwargs)
        instances.append(stream)
        return stream

    lines = read_serial_lines(
        "/dev/cu.usbmodem-test", trigger="ARMED", serial_factory=factory
    )
    assert next(lines).startswith("CFG_BOOT ")
    lines.close()

    assert instances[0].writes == [b"GET\nSTATUS\n"]
    assert instances[0].initial_timeout == collect.CONFIG_GATE_CMD_POLL_INTERVAL_S


def test_serial_armed_start_is_sent_once_while_marker_is_delayed(
    tmp_path: Path, monkeypatch
) -> None:
    command_file = tmp_path / "cmd.txt"
    command_file.write_text("START\n", encoding="utf-8")
    instances: list[FakeSerial] = []

    def factory(**kwargs):
        stream = FakeSerial(**kwargs)
        stream.chunks = [b"stale-tail\n", b"IT_READY\n", b"IT_READY\n"]
        instances.append(stream)
        return stream

    clock_calls = 0

    def advancing_clock() -> float:
        nonlocal clock_calls
        clock_calls += 1
        if clock_calls <= 7:
            return clock_calls * 0.1
        return 10.0 + clock_calls

    monkeypatch.setattr(collect.time, "monotonic", advancing_clock)
    lines = read_serial_lines(
        "/dev/cu.usbmodem-test", trigger="ARMED", cmd_file=command_file,
        serial_factory=factory,
    )

    assert next(lines) == "IT_READY"
    assert next(lines) == "IT_READY"
    lines.close()

    assert instances[0].writes == [b"GET\nSTATUS\n", b"START\n"]
    assert instances[0].initial_timeout == collect.CONFIG_GATE_CMD_POLL_INTERVAL_S
    assert instances[0].timeout == collect.SERIAL_READ_TIMEOUT_S


def test_serial_armed_recovers_start_marker_glued_to_config_line(
    tmp_path: Path,
) -> None:
    command_file = tmp_path / "cmd.txt"
    command_file.write_text("START\n", encoding="utf-8")
    instances: list[FakeSerial] = []

    def factory(**kwargs):
        stream = FakeSerial(**kwargs)
        stream.chunks = [
            b"stale-tail\n",
            b"CFG_CONFIRMED ep=2 req=abcIT_START run=9 target_mv=-100\n"
            b"S seq=3 ms=300 counts=7100 fa=-2 tag=0 auto=1 ovf=0 sat=0 ep=2\n",
        ]
        instances.append(stream)
        return stream

    lines = read_serial_lines(
        "/dev/cu.usbmodem-test", trigger="ARMED", cmd_file=command_file,
        serial_factory=factory,
    )

    assert next(lines) == "CFG_CONFIRMED ep=2 req=abc"
    assert next(lines) == "IT_START run=9 target_mv=-100"
    assert next(lines).startswith("S seq=3 ")
    lines.close()

    assert instances[0].writes == [b"GET\nSTATUS\n", b"START\n"]


def test_packaged_and_ipc_reads_stay_strict_utf8() -> None:
    """🔴 这两处**故意**保持严格 UTF-8,不许被顺手"一起容错掉"。

    分类原则(见 pa_host/textio.py 模块 docstring):容错只给**用户数据**。
    · `firmware.json` 是随包构建产物,内容只有 rtt_address 这类 ASCII,
      永不经过用户 locale ⇒ 解不开只意味着随包文件坏了,该立刻暴露成
      "找不到 RTT 地址",而不是被 gb18030 静默解成乱码再报个 KeyError。
    · 命令文件是上位机自己刚写的进程间管道,`_encode_firmware_command`
      本来就拒收非 ASCII ⇒ 读出非 UTF-8 说明有别的东西在写它。

    容错掩盖这两处 = 把"文件坏了"变成"行为诡异",排查成本高得多。
    """
    import ast

    source = (Path(collect.__file__)).read_text(encoding="utf-8")
    tree = ast.parse(source)
    tolerant = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = (node.func.attr if isinstance(node.func, ast.Attribute)
                else getattr(node.func, "id", ""))
        if name in {"read_text_tolerant", "read_csv_lines", "read_json_tolerant"}:
            tolerant.append(node.lineno)

    assert tolerant == [], (
        f"collect.py 出现了容错读(行 {tolerant})。若确实是用户数据请更新本测试的"
        "判据说明;若是随包资源/内部管道,请改回严格 UTF-8。"
    )
