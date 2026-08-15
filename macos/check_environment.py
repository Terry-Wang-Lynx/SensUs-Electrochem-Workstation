#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PREBUILT = ROOT / "software/firmware/prebuilt"


def status(ok: bool, label: str, detail: str = "") -> None:
    marker = "OK" if ok else "--"
    suffix = f": {detail}" if detail else ""
    print(f"[{marker}] {label}{suffix}")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def find_brew_tool(name: str) -> Path | None:
    found = shutil.which(name)
    if found:
        return Path(found)
    for prefix in (Path("/opt/homebrew/bin"), Path("/usr/local/bin")):
        candidate = prefix / name
        if candidate.exists():
            return candidate
    return None


def jlink_usb_present() -> bool:
    done = subprocess.run(
        ["/usr/sbin/ioreg", "-p", "IOUSB", "-l", "-w", "0"],
        capture_output=True, text=True, check=False,
    )
    text = done.stdout.lower()
    return "j-link" in text or "segger" in text


def existing_toolchain_path(
    configured: str | None, fallbacks: tuple[str, ...], marker: str
) -> Path:
    candidates = ([configured] if configured else []) + list(fallbacks)
    paths = [Path(value).expanduser() for value in candidates if value]
    for path in paths:
        if (path / marker).exists():
            return path
    return paths[0]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--require-toolchain", action="store_true")
    args = parser.parse_args()

    print(f"macOS {platform.mac_ver()[0]} / {platform.machine()}")
    status(sys.version_info >= (3, 10), "Python", platform.python_version())

    try:
        import matplotlib
        import numpy
        detail = f"numpy {numpy.__version__}, matplotlib {matplotlib.__version__}"
        status(True, "Python 分析依赖", detail)
    except ImportError as exc:
        status(False, "Python 分析依赖", str(exc))
        return 1

    metadata_path = PREBUILT / "firmware.json"
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        bad = [name for name, expected in metadata["sha256"].items()
               if not (PREBUILT / name).exists()
               or sha256(PREBUILT / name) != expected]
        status(not bad, "随包推荐固件", "校验通过" if not bad else f"失败: {bad}")
        if bad:
            return 1
    except (OSError, KeyError, ValueError, json.JSONDecodeError) as exc:
        status(False, "随包推荐固件", str(exc))
        return 1

    openocd = find_brew_tool("openocd")
    scripts = None
    if openocd:
        for candidate in (
            openocd.parent.parent / "share/openocd/scripts",
            Path("/opt/homebrew/share/openocd/scripts"),
            Path("/usr/local/share/openocd/scripts"),
        ):
            if (candidate / "interface/jlink.cfg").exists():
                scripts = candidate
                break
    status(bool(openocd and scripts), "OpenOCD J-Link 通道",
           str(openocd) if openocd else "未安装")

    usb = jlink_usb_present()
    status(usb, "J-Link USB", "已识别" if usb else "未插入（安装仍然可完成）")

    ncs = existing_toolchain_path(
        os.environ.get("SENSUS_NCS_DIR"),
        ("~/sensus-toolchains/ncs", "~/ncs"),
        "zephyr/zephyr-env.sh",
    )
    sdk = existing_toolchain_path(
        os.environ.get("SENSUS_ZEPHYR_SDK_DIR"),
        ("~/sensus-toolchains/zephyr-sdk-1.0.1", "~/zephyr-sdk-1.0.1"),
        "gnu/arm-zephyr-eabi/bin/arm-zephyr-eabi-gcc",
    )
    toolchain_ok = (
        (ncs / ".venv/bin/west").exists()
        and (ncs / "zephyr/zephyr-env.sh").exists()
        and (sdk / "gnu/arm-zephyr-eabi/bin/arm-zephyr-eabi-gcc").exists()
    )
    status(toolchain_ok, "可选固件工具链",
           f"已安装：{ncs} / {sdk}" if toolchain_ok
           else "未安装；推荐条件可直接用，修改其他条件前需安装")

    if not openocd or not scripts:
        return 1
    if args.require_toolchain and not toolchain_ok:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
