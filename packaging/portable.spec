# -*- mode: python ; coding: utf-8 -*-

import sys
from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules, copy_metadata

ROOT = Path(SPECPATH).resolve().parent
WINDOWS_ICON = ROOT / "artifacts" / "build" / "windows-x64" / "SensUs-Workstation.ico"
if sys.platform == "win32" and not WINDOWS_ICON.is_file():
    raise SystemExit(f"Missing generated Windows icon: {WINDOWS_ICON}")

a = Analysis(
    [str(ROOT / "packaging" / "portable_entry.py")],
    pathex=[str(ROOT / "software" / "host")],
    datas=[
        (str(ROOT / "software" / "host" / "pa_host" / "gui"), "pa_host/gui")
    ] + copy_metadata("smpmgr", recursive=True),
    hiddenimports=[
        "pa_host.gui_server",
        "pa_host.it_tool",
        "pa_host.collect",
        "pa_host.analyze",
        "pa_host.app_update",
        "pa_host.cv",
        "pa_host.diagnostics",
        "pa_host.filtering",
        "pa_host.frontend_update",
        "pa_host.it",
        "pa_host.live_metrics",
        "pa_host.record",
        "pa_host.runtime",
        "pa_host.stability_eta",
        "pa_host.workspace_history",
        "pa_host.windows_jlink",
        "serial",
        "serial.tools.list_ports",
        "serial.tools.list_ports_common",
        "serial.tools.list_ports_posix",
        "serial.tools.list_ports_osx",
        "serial.tools.list_ports_windows",
        "webview",
    ] + collect_submodules("smpmgr") + collect_submodules("smpclient") + collect_submodules("smp"),
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "PyQt5", "PyQt6", "PySide2", "PySide6"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="SensUsBackend",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon=str(WINDOWS_ICON) if sys.platform == "win32" else None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="SensUsBackend",
)
