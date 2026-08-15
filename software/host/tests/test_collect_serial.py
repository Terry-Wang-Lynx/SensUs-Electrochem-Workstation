from pathlib import Path

from pa_host.collect import read_serial_lines


class FakeSerial:
    def __init__(self, *, port, baudrate, timeout, write_timeout):
        self.port = port
        self.baudrate = baudrate
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
