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


def test_frozen_build_prefers_openocd_shipped_next_to_workstation(
    tmp_path: Path, monkeypatch
) -> None:
    resources = tmp_path / "Resources"
    workstation = resources / "workstation"
    executable = resources / "tools" / "openocd" / "bin" / "openocd"
    scripts = resources / "tools" / "openocd" / "share" / "openocd" / "scripts"
    workstation.mkdir(parents=True)
    executable.parent.mkdir(parents=True)
    scripts.joinpath("interface").mkdir(parents=True)
    executable.write_text("portable openocd", encoding="ascii")
    scripts.joinpath("interface", "jlink.cfg").write_text(
        "adapter driver jlink\n", encoding="ascii"
    )

    monkeypatch.setattr(collect.runtime, "is_frozen", lambda: True)
    monkeypatch.setattr(collect.runtime, "project_dir", lambda: workstation)
    monkeypatch.setattr(collect.sys, "executable", str(resources / "backend" / "SensUsBackend"))
    monkeypatch.delenv("SENSUS_OPENOCD_EXE", raising=False)
    monkeypatch.delenv("SENSUS_OPENOCD_SCRIPTS", raising=False)

    resolved_executable, resolved_scripts = collect._resolve_openocd()

    assert resolved_executable == executable
    assert resolved_scripts == scripts


def test_frozen_project_dir_resolves_app_resources_without_source_checkout(
    tmp_path: Path, monkeypatch
) -> None:
    resources = tmp_path / "Resources"
    backend = resources / "backend" / "SensUsBackend"
    workstation = resources / "workstation"
    backend.mkdir(parents=True)
    workstation.mkdir()

    monkeypatch.setattr(runtime, "is_frozen", lambda: True)
    monkeypatch.setattr(runtime.sys, "executable", str(backend / "SensUsBackend"))
    monkeypatch.setattr(runtime.sys, "_MEIPASS", str(backend / "_internal"), raising=False)
    monkeypatch.delenv("SENSUS_RESOURCE_DIR", raising=False)
    monkeypatch.delenv("SENSUS_PROJECT_DIR", raising=False)

    assert runtime.project_dir() == workstation.resolve()


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
