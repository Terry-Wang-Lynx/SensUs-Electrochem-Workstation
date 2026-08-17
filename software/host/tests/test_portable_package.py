import json
import hashlib
import os
import plistlib
import re
import runpy
import subprocess
import sys
from pathlib import Path

import pytest

import pa_host
from pa_host import collect, gui_server, runtime


def test_release_versions_stay_in_sync() -> None:
    root = Path(__file__).parents[3]
    match = re.search(
        r'^version = "([^"]+)"$',
        (root / "pyproject.toml").read_text(encoding="utf-8"),
        flags=re.MULTILINE,
    )
    assert match is not None
    project_version = match.group(1)
    with (root / "macos" / "Info.plist").open("rb") as handle:
        app_metadata = plistlib.load(handle)

    assert pa_host.__version__ == project_version
    assert app_metadata["CFBundleShortVersionString"] == project_version
    assert app_metadata["CFBundleVersion"] == project_version
    assert app_metadata["LSMinimumSystemVersion"] == "14.0"


def test_portable_builds_pin_python_and_enforce_macos_compatibility() -> None:
    root = Path(__file__).parents[3]
    macos_build = (root / "packaging" / "build_macos_portable.sh").read_text(
        encoding="utf-8"
    )
    windows_build = (root / "windows" / "build_portable.ps1").read_text(
        encoding="utf-8"
    )
    verifier = (root / "packaging" / "verify_macos_bundle.py").read_text(
        encoding="utf-8"
    )

    assert 'SENSUS_PORTABLE_PYTHON_MINOR:-3.12' in macos_build
    assert "Portable Windows builds require Python 3.12" in windows_build
    assert "verify_macos_bundle.py" in macos_build
    assert "OpenOCD is required for a self-contained J-Link package" in macos_build
    for command_name in ('"lipo"', '"vtool"', '"otool"'):
        assert command_name in verifier


def test_portable_packages_use_the_shared_brand_logo(tmp_path: Path) -> None:
    root = Path(__file__).parents[3]
    source = root / "branding" / "sensus-logo-source.png"
    macos_icon = (root / "macos" / "create_icon.py").read_text(encoding="utf-8")
    windows_icon = (root / "windows" / "create_icon.py").read_text(encoding="utf-8")
    windows_build = (root / "windows" / "build_portable.ps1").read_text(
        encoding="utf-8"
    )
    spec = (root / "packaging" / "portable.spec").read_text(encoding="utf-8")
    manifest = (root / "MANIFEST.in").read_text(encoding="utf-8")

    assert source.is_file()
    assert '"branding" / "sensus-logo-source.png"' in macos_icon
    assert '"branding" / "sensus-logo-source.png"' in windows_icon
    assert 'Join-Path $Root "windows\\create_icon.py"' in windows_build
    assert 'icon=str(WINDOWS_ICON) if sys.platform == "win32" else None' in spec
    assert "recursive-include branding *.png" in manifest
    assert "recursive-include software/host/pa_host/gui *.html *.js *.css *.png" in manifest

    generated_icon = tmp_path / "SensUs-Workstation.ico"
    result = subprocess.run(
        [sys.executable, str(root / "windows" / "create_icon.py"), str(generated_icon)],
        check=False,
        capture_output=True,
        env={**os.environ, "PYTHONIOENCODING": "cp1252"},
    )
    assert result.returncode == 0, result.stderr.decode("ascii", errors="replace")
    assert generated_icon.is_file()


def test_windows_portable_builds_and_bundles_pinned_winusb_helper() -> None:
    root = Path(__file__).parents[3]
    helper_build = (
        root / "packaging" / "build_windows_winusb_helper.ps1"
    ).read_text(encoding="utf-8")
    windows_build = (root / "windows" / "build_portable.ps1").read_text(
        encoding="utf-8"
    )
    workflow = (root / ".github" / "workflows" / "portable-release.yml").read_text(
        encoding="utf-8"
    )
    spec = (root / "packaging" / "portable.spec").read_text(encoding="utf-8")

    commit = "9b23b82a2dd1cbffc16d46c212f92c6bf8c0c602"
    for value in (
        commit,
        "29314207814ce9d5d73695f7e9239539cf37c79e750b9d5ea5a5ef5487a583d6",
        "9950b7a226e3ea387365c046d13b68bd0b0b18c015c034363601ff601c5b5585",
        "38605d8d5a86f408a4b7bec60f6d4a096050eee72f89a63a8d5be125252d3fe7",
    ):
        assert value in helper_build
    assert commit in workflow
    assert "microsoft/setup-msbuild@v2" in workflow
    assert "build_windows_winusb_helper.ps1" in workflow
    for name in (
        "wdi-simple.exe", "COPYING-LGPL", "libwdi-1.5.1-source.zip", "SOURCE.txt"
    ):
        assert name in windows_build
    assert '"pa_host.windows_jlink"' in spec


def test_windows_first_launch_allows_defender_cold_start() -> None:
    entry = runpy.run_path(
        str(Path(__file__).parents[3] / "packaging" / "portable_entry.py"),
        run_name="portable_entry",
    )

    assert entry["WINDOWS_COLD_START_TIMEOUT_S"] >= 120


def test_portable_launchers_expose_their_installed_location_to_app_updates() -> None:
    root = Path(__file__).parents[3]
    swift = (root / "macos" / "Sources" / "main.swift").read_text(encoding="utf-8")
    windows = (root / "packaging" / "portable_entry.py").read_text(encoding="utf-8")
    spec = (root / "packaging" / "portable.spec").read_text(encoding="utf-8")

    assert 'environment["SENSUS_APP_BUNDLE"] = Bundle.main.bundleURL.path' in swift
    assert 'environment["SENSUS_APP_PID"]' in swift
    assert '"SENSUS_APP_ROOT": str(Path(sys.executable).resolve().parent)' in windows
    assert '"SENSUS_APP_PID": str(os.getpid())' in windows
    assert '"pa_host.app_update"' in spec


def test_macos_openocd_bundler_reuses_already_relocated_libraries() -> None:
    root = Path(__file__).parents[3]
    bundler = (root / "packaging" / "bundle_macos_openocd.sh").read_text(
        encoding="utf-8"
    )

    assert "@executable_path/../lib/*|@loader_path/*" in bundler
    assert 'source_lib_dir="$prefix/lib"' in bundler
    assert 'source_dependency="$source_lib_dir/${dependency:t}"' in bundler


def test_macos_bundle_verifier_ignores_otool_header_path() -> None:
    root = Path(__file__).parents[3]
    verifier = runpy.run_path(str(root / "packaging" / "verify_macos_bundle.py"))

    links = verifier["linked_libraries"](
        "/Users/student/SensUs Workstation.app/Contents/MacOS/SensUsWorkstation:\n"
        "\t@rpath/libusb-1.0.0.dylib (compatibility version 3.0.0)\n"
        "\t/System/Library/Frameworks/AppKit.framework/AppKit "
        "(compatibility version 1.0.0)\n"
    )

    assert links == [
        "@rpath/libusb-1.0.0.dylib",
        "/System/Library/Frameworks/AppKit.framework/AppKit",
    ]


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


def test_frozen_build_uses_compatible_system_jlink_when_present(
    tmp_path: Path, monkeypatch
) -> None:
    program_files = tmp_path / "Program Files"
    commander = program_files / "SEGGER" / "JLink_V999" / "JLink.exe"
    commander.parent.mkdir(parents=True)
    commander.write_text("commander", encoding="ascii")
    commander.chmod(0o755)
    monkeypatch.setattr(collect, "_IS_WIN", True)
    monkeypatch.setattr(collect.runtime, "is_frozen", lambda: True)
    monkeypatch.delenv("SENSUS_JLINK_EXE", raising=False)
    monkeypatch.setenv("ProgramFiles", str(program_files))
    monkeypatch.setenv("ProgramFiles(x86)", str(tmp_path / "Program Files (x86)"))

    assert collect._resolve_jlink_exe() == commander


def test_frozen_build_allows_explicit_jlink_service_override(
    tmp_path: Path, monkeypatch
) -> None:
    commander = tmp_path / "JLink.exe"
    commander.write_text("commander", encoding="ascii")
    monkeypatch.setattr(collect, "_IS_WIN", True)
    monkeypatch.setattr(collect.runtime, "is_frozen", lambda: True)
    monkeypatch.setenv("SENSUS_JLINK_EXE", str(commander))

    assert collect._resolve_jlink_exe() == commander


def test_jlink_target_probe_uses_explicit_connection_sequence(
    tmp_path: Path, monkeypatch
) -> None:
    commander = tmp_path / "JLinkExe"
    commander.write_text("commander", encoding="ascii")
    commander.chmod(0o755)
    captured: dict[str, str] = {}

    def fake_run(command, **kwargs):
        script_path = Path(command[command.index("-CommanderScript") + 1])
        captured["script"] = script_path.read_text(encoding="utf-8")
        return subprocess.CompletedProcess(
            command, 0,
            stdout=(
                "Found SW-DP with ID 0x2BA01477\n"
                "Cortex-M4 identified.\n10000100 = 00052833\n"
                "Script processing completed.\n"
            ),
            stderr="",
        )

    monkeypatch.setattr(collect.subprocess, "run", fake_run)

    reachable, _ = collect.probe_jlink_target(
        "29734569", executable=commander
    )

    assert reachable is True
    assert captured["script"].splitlines() == [
        "si SWD", "speed 100", "device nRF52833_xxAA", "connect",
        "mem32 0x10000100 1", "q",
    ]


def test_jlink_target_probe_retries_a_transient_first_connect(
    tmp_path: Path, monkeypatch
) -> None:
    commander = tmp_path / "JLinkExe"
    commander.write_text("commander", encoding="ascii")
    commander.chmod(0o755)
    attempts = 0

    def fake_run(command, **kwargs):
        nonlocal attempts
        attempts += 1
        output = (
            "Could not connect to the target device.\n"
            if attempts == 1
            else "10000100 = 00052833\nScript processing completed.\n"
        )
        return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")

    monkeypatch.setattr(collect.subprocess, "run", fake_run)
    monkeypatch.setattr(collect.time, "sleep", lambda _seconds: None)

    reachable, output = collect.probe_jlink_target(
        "29734569", executable=commander
    )

    assert reachable is True
    assert attempts == 2
    assert "Could not connect" in output
    assert "10000100 = 00052833" in output


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


def test_macos_portable_launcher_never_falls_back_to_a_saved_source_tree() -> None:
    root = Path(__file__).parents[3]
    swift = (root / "macos" / "Sources" / "main.swift").read_text(
        encoding="utf-8"
    )
    resolver_start = swift.index("private static func resolveProjectRoot()")
    resolver_end = swift.index(
        "private static func resolveBundledBackend()", resolver_start
    )
    resolver = swift[resolver_start:resolver_end]

    assert "if resolveBundledBackend() != nil" in resolver
    assert resolver.index("if resolveBundledBackend() != nil") < resolver.index(
        'environment["SENSUS_PROJECT_DIR"]'
    )
    assert resolver.index("if resolveBundledBackend() != nil") < resolver.index(
        'UserDefaults.standard.string(forKey: "projectRoot")'
    )


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
    metadata = {
        "settings": gui_server.SettingsController.DEFAULTS,
        "runtime_configurable": True,
        "runtime_protocol": {"name": "MEAS", "version": 1},
        "rtt_address": "0x20001000",
    }
    (tmp_path / "v51" / "firmware.json").write_text(
        json.dumps(metadata),
        encoding="utf-8",
    )
    image = v51 / "app.signed.bin"
    image.write_bytes(b"runtime V5.1")
    (v40 / "firmware.json").write_text(
        json.dumps(metadata),
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
    probe_states = iter((("missing", "legacy"), ("missing", "legacy"),
                         ("ready", "verified")))
    monkeypatch.setattr(
        gui_server.SettingsController, "_probe_runtime_firmware",
        staticmethod(lambda *_args, **_kwargs: next(probe_states)),
    )
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
