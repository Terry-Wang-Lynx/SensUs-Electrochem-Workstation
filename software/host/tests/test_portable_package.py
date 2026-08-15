import json
from pathlib import Path

from pa_host import collect, gui_server


def test_prebuilt_firmware_selection(tmp_path: Path, monkeypatch) -> None:
    build = tmp_path / "build"
    prebuilt = tmp_path / "prebuilt"
    build.mkdir()
    prebuilt.mkdir()
    (build / "zephyr.hex").write_text("built", encoding="ascii")
    (prebuilt / "zephyr.hex").write_text("prebuilt", encoding="ascii")
    settings = tmp_path / "gui_settings.json"
    settings.write_text(
        json.dumps({"firmware_source": "prebuilt"}), encoding="utf-8"
    )

    monkeypatch.setattr(gui_server, "FIRMWARE_BUILD_DIR", build)
    monkeypatch.setattr(gui_server, "FIRMWARE_PREBUILT_DIR", prebuilt)
    monkeypatch.setattr(gui_server, "SETTINGS_PATH", settings)

    assert gui_server._firmware_artifact("zephyr.hex") == prebuilt / "zephyr.hex"


def test_rtt_address_falls_back_to_prebuilt_metadata(
    tmp_path: Path, monkeypatch
) -> None:
    prebuilt = tmp_path / "prebuilt"
    prebuilt.mkdir()
    elf = prebuilt / "zephyr.elf"
    elf.write_bytes(b"portable firmware placeholder")
    (prebuilt / "firmware.json").write_text(
        json.dumps({"rtt_address": "0x20001100"}), encoding="utf-8"
    )
    monkeypatch.setattr(collect, "ZEPHYR_SDK_NM", tmp_path / "missing-nm")

    assert collect.find_rtt_address(elf) == 0x20001100


def test_command_stream_keeps_partial_lines() -> None:
    lines, pending = collect._split_complete_lines("SET fsr=2", "")
    assert lines == []
    lines, pending = collect._split_complete_lines(" off=4\n", pending)
    assert lines == ["SET fsr=2 off=4"]
    assert pending == ""
