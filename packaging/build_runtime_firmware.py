#!/usr/bin/env python3
"""Build and stage the runtime-configurable V4 and V5.1 firmware images."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path


PROTOCOL = {"name": "MEAS", "version": 1}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def require(path: Path, description: str) -> Path:
    if not path.exists():
        raise SystemExit(f"missing {description}: {path}")
    return path


def build(
    *, root: Path, west: Path, build_dir: Path, board: str,
    environment: dict[str, str], mcuboot: bool,
) -> None:
    firmware = root / "software" / "firmware"
    command = [
        str(west), "build", "-p", "always", "-b", board,
        "-d", str(build_dir), str(firmware), "--",
        f"-DBOARD_ROOT={firmware}", f"-DDTS_ROOT={firmware}",
        "-Dfirmware_CONFIG_SENSUS_USE_DEFAULT_MEASUREMENT_CONFIG=y",
    ]
    if mcuboot:
        command.append("-DSB_CONFIG_BOOTLOADER_MCUBOOT=y")
    subprocess.run(command, cwd=root, env=environment, check=True)

    config = require(
        build_dir / "firmware" / "zephyr" / ".config", "application Kconfig"
    ).read_text(encoding="utf-8", errors="replace")
    if "CONFIG_SENSUS_USE_DEFAULT_MEASUREMENT_CONFIG=y" not in config:
        raise SystemExit(
            f"runtime default configuration did not reach child image: {build_dir}"
        )


def rtt_address(elf: Path, nm: Path) -> str:
    output = subprocess.run(
        [str(nm), "-g", str(elf)], check=True, capture_output=True, text=True
    ).stdout
    match = re.search(
        r"^([0-9a-fA-F]+)\s+\w\s+_SEGGER_RTT$", output, re.MULTILINE
    )
    if match is None:
        raise SystemExit(f"_SEGGER_RTT not found in {elf}")
    return f"0x{int(match.group(1), 16):08x}"


def copy(source: Path, destination: Path) -> None:
    require(source, "firmware artifact")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.suffix.lower() == ".hex":
        destination.write_bytes(source.read_bytes().replace(b"\r\n", b"\n"))
    else:
        shutil.copy2(source, destination)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--root", type=Path, default=Path(__file__).resolve().parent.parent
    )
    parser.add_argument("--ncs-dir", type=Path, default=Path.home() / "ncs")
    parser.add_argument(
        "--sdk-dir", type=Path, default=Path.home() / "zephyr-sdk-1.0.1"
    )
    parser.add_argument("--build-root", type=Path)
    args = parser.parse_args()

    root = args.root.resolve()
    ncs = args.ncs_dir.expanduser().resolve()
    sdk = args.sdk_dir.expanduser().resolve()
    build_root = (
        args.build_root.resolve()
        if args.build_root else root / "artifacts" / "runtime-firmware"
    )
    west = require(ncs / ".venv" / "bin" / "west", "NCS west")
    nm = require(
        sdk / "gnu" / "arm-zephyr-eabi" / "bin" / "arm-zephyr-eabi-nm",
        "Zephyr SDK nm",
    )
    environment = {
        **os.environ,
        "ZEPHYR_BASE": str(require(ncs / "zephyr", "Zephyr source")),
        "ZEPHYR_TOOLCHAIN_VARIANT": "zephyr",
        "ZEPHYR_SDK_INSTALL_DIR": str(sdk),
        "PATH": os.pathsep.join((
            str(ncs / ".venv" / "bin"), str(ncs / "zephyr" / "scripts"),
            os.environ.get("PATH", ""),
        )),
    }

    v40_build = build_root / "v40"
    v51_build = build_root / "v51"
    build(
        root=root, west=west, build_dir=v40_build,
        board="pa_converter_v40", environment=environment, mcuboot=False,
    )
    build(
        root=root, west=west, build_dir=v51_build,
        board="pa_converter_v51", environment=environment, mcuboot=True,
    )

    v40_source = v40_build / "firmware" / "zephyr"
    v40_destination = root / "software" / "firmware" / "prebuilt"
    copy(v40_source / "zephyr.hex", v40_destination / "zephyr.hex")
    copy(v40_source / "zephyr.elf", v40_destination / "zephyr.elf")
    v40_metadata_path = v40_destination / "firmware.json"
    v40_metadata = json.loads(v40_metadata_path.read_text(encoding="utf-8"))
    v40_metadata.update({
        "name": "SensUs pA-Converter V4 runtime-configurable firmware",
        "runtime_configurable": True,
        "runtime_protocol": PROTOCOL,
        "rtt_address": rtt_address(v40_destination / "zephyr.elf", nm),
        "sha256": {
            "zephyr.hex": sha256(v40_destination / "zephyr.hex"),
            "zephyr.elf": sha256(v40_destination / "zephyr.elf"),
        },
    })
    v40_metadata_path.write_text(
        json.dumps(v40_metadata, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    v51_zephyr = v51_build / "firmware" / "zephyr"
    v51_destination = root / "packaging" / "resources" / "v51"
    v51_images = v51_destination / "images"
    copies = {
        v51_zephyr / "zephyr.signed.bin": v51_images / "app.signed.bin",
        v51_zephyr / "zephyr.signed.hex": v51_images / "app.signed.hex",
        v51_build / "dfu_application.zip": v51_images / "dfu_application.zip",
        v51_build / "mcuboot" / "zephyr" / "zephyr.hex": v51_images / "mcuboot.hex",
    }
    for source, destination in copies.items():
        copy(source, destination)
    v51_metadata_path = v51_destination / "firmware.json"
    v51_metadata = json.loads(v51_metadata_path.read_text(encoding="utf-8"))
    v51_metadata.update({
        "name": "SensUs pA-Converter V5.1 runtime-configurable firmware",
        "board": "pa_converter_v51/nrf52833",
        "ncs_version": "v3.4.0",
        "runtime_configurable": True,
        "runtime_protocol": PROTOCOL,
        "image": "images/app.signed.bin",
        "sha256": sha256(v51_images / "app.signed.bin"),
        "artifacts_sha256": {
            path.name: sha256(path) for path in copies.values()
        },
    })
    v51_metadata_path.write_text(
        json.dumps(v51_metadata, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"staged V4 firmware in {v40_destination}")
    print(f"staged V5.1 firmware in {v51_destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
