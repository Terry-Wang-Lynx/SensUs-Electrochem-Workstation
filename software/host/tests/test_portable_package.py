import json
import hashlib
import os
import runpy
from pathlib import Path

import pytest

from pa_host import collect, gui_server, runtime


def test_bundled_runtime_firmware_metadata_matches_artifacts() -> None:
    root = Path(__file__).parents[3]
    v40_dir = root / "software" / "firmware" / "prebuilt"
    v51_dir = root / "packaging" / "resources" / "v51"
    v40 = json.loads((v40_dir / "firmware.json").read_text(encoding="utf-8"))
    v51 = json.loads((v51_dir / "firmware.json").read_text(encoding="utf-8"))

    for metadata in (v40, v51):
        assert metadata["runtime_configurable"] is True
        assert metadata["runtime_protocol"] == {"name": "MEAS", "version": 1}
    for name, expected in v40["sha256"].items():
        assert hashlib.sha256((v40_dir / name).read_bytes()).hexdigest() == expected
    for name, expected in v51["artifacts_sha256"].items():
        assert hashlib.sha256(
            (v51_dir / "images" / name).read_bytes()
        ).hexdigest() == expected
    assert v51["sha256"] == v51["artifacts_sha256"]["app.signed.bin"]


def test_windows_portable_falls_back_to_system_browser(monkeypatch) -> None:
    entry = runpy.run_path(
        str(Path(__file__).parents[3] / "packaging" / "portable_entry.py"),
        run_name="portable_entry",
    )
    opened: list[str] = []
    monkeypatch.setattr(entry["webbrowser"], "open", opened.append)

    class Child:
        def wait(self) -> int:
            return 17

    fallback = entry["_fallback_to_system_browser"]
    assert fallback("http://127.0.0.1:8765/", Child(), RuntimeError("WebView2")) == 17
    assert opened == ["http://127.0.0.1:8765/"]


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
    executable = resources / "tools" / "openocd" / "bin" / (
        "openocd.exe" if os.name == "nt" else "openocd"
    )
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


def test_windows_frozen_build_does_not_use_host_jlink_install(monkeypatch) -> None:
    monkeypatch.setattr(collect, "_IS_WIN", True)
    monkeypatch.setattr(collect.runtime, "is_frozen", lambda: True)
    monkeypatch.delenv("SENSUS_JLINK_EXE", raising=False)

    assert collect._resolve_jlink_exe() == Path(
        "/__sensus_portable_no_jlink_exe__/JLink.exe"
    )


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


@pytest.mark.parametrize("transport", ["rtt", "serial"])
def test_frozen_custom_conditions_never_invoke_toolchain(
    tmp_path: Path, monkeypatch, transport: str
) -> None:
    v40 = tmp_path / "prebuilt"
    v51 = tmp_path / "v51" / "images"
    v40.mkdir()
    v51.mkdir(parents=True)
    (v40 / "zephyr.hex").write_bytes(b"runtime V4")
    (tmp_path / "v51" / "firmware.json").write_text(
        json.dumps({"settings": gui_server.SettingsController.DEFAULTS}),
        encoding="utf-8",
    )
    image = v51 / "app.signed.bin"
    image.write_bytes(b"runtime V5.1")
    (v40 / "firmware.json").write_text(
        json.dumps({"settings": gui_server.SettingsController.DEFAULTS}),
        encoding="utf-8",
    )
    settings_path = tmp_path / "gui_settings.json"
    actions: list[tuple[str, Path | None]] = []

    monkeypatch.setattr(gui_server.runtime, "is_frozen", lambda: True)
    monkeypatch.setattr(gui_server, "SETTINGS_PATH", settings_path)
    monkeypatch.setattr(gui_server, "FIRMWARE_PREBUILT_DIR", v40)
    monkeypatch.setattr(gui_server, "V51_PREBUILT_IMAGE", image)
    monkeypatch.setattr(gui_server, "PROJECT_DIR", tmp_path)
    monkeypatch.setattr(gui_server, "HARDWARE_TRANSPORT", transport)
    monkeypatch.setattr(gui_server, "SERIAL_SMP_PORT", "COM9")
    monkeypatch.setattr(gui_server, "_refresh_usb_transport", lambda: None)
    monkeypatch.setattr(gui_server, "_release_stale_measurement_bridge", lambda: None)
    monkeypatch.setattr(
        gui_server.SettingsController, "_run_build",
        staticmethod(lambda *_args, **_kwargs: pytest.fail("toolchain was invoked")),
    )
    monkeypatch.setattr(
        gui_server.SettingsController, "_flash_firmware",
        staticmethod(lambda path=None: actions.append(("flash", path))),
    )
    monkeypatch.setattr(
        gui_server.SettingsController, "_upgrade_v51_firmware",
        staticmethod(lambda path=None: actions.append(("upgrade", path))),
    )

    controller = gui_server.SettingsController()
    result = controller.apply({
        "potential_v": -0.2,
        "initial_potential_v": 0.1,
        "prestep_s": 45,
        "duration_s": 600,
        "target_rate_hz": 5,
        "fsr_nA": 40000,
        "offset_mode": "80nA",
    })

    assert result["applied"] is True
    assert result["settings"]["potential_v"] == -0.2
    assert actions == (
        [("upgrade", image)] if transport == "serial"
        else [("flash", v40 / "zephyr.hex")]
    )
    saved = json.loads(settings_path.read_text(encoding="utf-8"))
    assert saved["firmware_source"] == "prebuilt"
    assert saved["transport"] == transport
