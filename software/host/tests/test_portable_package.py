import json
import hashlib
from pathlib import Path

from pa_host import collect, gui_server, runtime


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


def test_source_subprocess_command_keeps_module_invocation() -> None:
    command = runtime.module_command("pa_host.collect", "--out", "run.csv")
    assert command[1:4] == ["-m", "pa_host.collect", "--out"]


def test_frozen_subprocess_command_reuses_backend(monkeypatch) -> None:
    monkeypatch.setattr(runtime, "is_frozen", lambda: True)
    monkeypatch.setattr(runtime.sys, "executable", "/portable/SensUsBackend")
    assert runtime.module_command("pa_host.it_tool", "measure") == [
        "/portable/SensUsBackend", "it-tool", "measure"
    ]
    assert runtime.module_command("smpmgr", "--help") == [
        "/portable/SensUsBackend", "smpmgr", "--help"
    ]


def test_saved_firmware_is_restored_only_for_matching_transport(
    tmp_path: Path, monkeypatch
) -> None:
    image = tmp_path / "app.signed.bin"
    image.write_bytes(b"v51 firmware")
    settings = tmp_path / "gui_settings.json"
    settings.write_text(json.dumps({
        "settings": gui_server.SettingsController.DEFAULTS,
        "firmware_source": "prebuilt",
        "firmware_sha256": hashlib.sha256(image.read_bytes()).hexdigest(),
        "transport": "serial",
    }), encoding="utf-8")
    monkeypatch.setattr(gui_server, "SETTINGS_PATH", settings)
    monkeypatch.setattr(gui_server, "V51_PREBUILT_IMAGE", image)
    monkeypatch.setattr(gui_server, "HARDWARE_TRANSPORT", "auto")

    controller = gui_server.SettingsController()
    controller.restore_for_transport("rtt")
    assert controller.snapshot()["applied"] is False
    controller.restore_for_transport("serial")
    assert controller.snapshot()["applied"] is True
