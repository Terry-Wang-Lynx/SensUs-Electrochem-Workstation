#!/usr/bin/env python3
"""Fail a portable macOS build whose native runtime exceeds its declared OS."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path


MACHO_MAGICS = {
    b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf",
    b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca",
    b"\xca\xfe\xba\xbf", b"\xbf\xba\xfe\xca",
}


def version_tuple(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in value.split("."))


def command(*args: object) -> str:
    completed = subprocess.run(
        [str(arg) for arg in args], check=True, capture_output=True,
        text=True, encoding="utf-8", errors="replace",
    )
    return f"{completed.stdout}\n{completed.stderr}"


def macho_files(app: Path) -> list[Path]:
    result: list[Path] = []
    for path in app.rglob("*"):
        if not path.is_file():
            continue
        try:
            with path.open("rb") as handle:
                magic = handle.read(4)
        except OSError:
            continue
        if magic in MACHO_MAGICS:
            result.append(path)
    return result


def linked_libraries(output: str) -> list[str]:
    """Return dependency paths from ``otool -L``, excluding its header."""
    libraries: list[str] = []
    for line in output.splitlines()[1:]:
        stripped = line.strip()
        if stripped:
            libraries.append(stripped.split(maxsplit=1)[0])
    return libraries


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify(app: Path, minimum_version: str) -> None:
    openocd_root = app / "Contents/Resources/tools/openocd"
    required = (
        app / "Contents/Resources/backend/SensUsBackend/SensUsBackend",
        app / "Contents/Resources/workstation/PORTABLE_RESOURCES.txt",
        app / "Contents/Resources/tools/openocd/bin/openocd",
        app / "Contents/Resources/tools/openocd/lib/libusb-1.0.0.dylib",
        app / "Contents/Resources/tools/openocd/COMPONENTS.json",
        app / "Contents/Resources/tools/openocd/BINARY_DEPENDENCIES.txt",
        app / "Contents/Resources/tools/openocd/licenses/OpenOCD-COPYING",
        app / "Contents/Resources/tools/openocd/licenses/libusb-COPYING",
        app / "Contents/Resources/tools/openocd/source/openocd-0.12.0.tar.bz2",
        app / "Contents/Resources/tools/openocd/source/libusb-1.0.29.tar.bz2",
        app / "Contents/Resources/tools/openocd/source/build_macos_openocd.sh",
        app / "Contents/Resources/tools/openocd/share/openocd/scripts/interface/jlink.cfg",
        app / "Contents/Resources/tools/openocd/share/openocd/scripts/target/nrf52.cfg",
        app / "Contents/Resources/THIRD_PARTY_LICENSES/PYTHON_PACKAGES.json",
        app / "Contents/Resources/THIRD_PARTY_LICENSES/SBOM.spdx.json",
    )
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise SystemExit("missing portable resources:\n" + "\n".join(missing))

    declared = version_tuple(minimum_version)
    failures: list[str] = []
    openocd_libraries = {
        path.name for path in (openocd_root / "lib").iterdir() if path.is_file()
    }
    if openocd_libraries != {"libusb-1.0.0.dylib"}:
        failures.append(
            "OpenOCD must contain only the reviewed libusb runtime; found: "
            + ", ".join(sorted(openocd_libraries))
        )
    files = macho_files(app)
    if not files:
        failures.append("bundle contains no Mach-O files")
    for path in files:
        try:
            architectures = command("lipo", "-archs", path).split()
            if "arm64" not in architectures:
                failures.append(f"{path}: missing arm64 architecture")
            build = command("xcrun", "vtool", "-show-build", path)
            versions = re.findall(r"^\s*minos\s+([0-9.]+)\s*$", build, re.MULTILINE)
            if versions and any(version_tuple(item) > declared for item in versions):
                failures.append(
                    f"{path}: requires macOS {max(versions, key=version_tuple)} "
                    f"but bundle declares {minimum_version}"
                )
            links = linked_libraries(command("otool", "-L", path))
            for prefix in ("/opt/homebrew/", "/usr/local/", "/Users/"):
                if any(link.startswith(prefix) for link in links):
                    failures.append(f"{path}: host dependency remains: {prefix}")
        except subprocess.CalledProcessError as exc:
            failures.append(f"{path}: native inspection failed: {exc.stderr.strip()}")

    openocd = required[2]
    try:
        output = command(openocd, "--version")
        if "Open On-Chip Debugger 0.12.0" not in output:
            failures.append(f"{openocd}: unexpected --version output")
        adapters = command(
            openocd, "-c", "echo [adapter list]; shutdown",
        )
        adapter_names = re.findall(r"^\s*\d+:\s+(\S+)\s*$", adapters, re.MULTILINE)
        if adapter_names != ["jlink"]:
            failures.append(
                f"{openocd}: expected only the jlink adapter, got {adapter_names}"
            )
    except subprocess.CalledProcessError as exc:
        failures.append(f"{openocd}: cannot execute: {exc.stderr.strip()}")

    try:
        component_path = openocd_root / "COMPONENTS.json"
        manifest = json.loads(component_path.read_text(encoding="utf-8"))
        if manifest.get("schema") != 1:
            raise ValueError("unexpected component schema")
        if manifest.get("platform") != "macos-arm64":
            raise ValueError("unexpected component platform")
        if manifest.get("minimum_macos") != minimum_version:
            raise ValueError("component minimum macOS does not match Info.plist")
        components = {
            str(item["name"]): item for item in manifest.get("components", [])
        }
        expected_components = {
            "OpenOCD": {
                "binary": "bin/openocd",
                "source": "source/openocd-0.12.0.tar.bz2",
                "license": "licenses/OpenOCD-COPYING",
            },
            "libusb": {
                "binary": "lib/libusb-1.0.0.dylib",
                "source": "source/libusb-1.0.29.tar.bz2",
                "license": "licenses/libusb-COPYING",
            },
        }
        if set(components) != set(expected_components):
            raise ValueError(f"unexpected component set: {sorted(components)}")
        for name, expected_paths in expected_components.items():
            component = components[name]
            for field in ("binary", "source"):
                if component.get(field) != expected_paths[field]:
                    raise ValueError(f"{name} has unexpected {field} path")
            binary = openocd_root / expected_paths["binary"]
            source = openocd_root / expected_paths["source"]
            license_path = openocd_root / expected_paths["license"]
            if not license_path.is_file():
                raise ValueError(f"{name} license file is missing")
            expected = str(component["binary_sha256"])
            actual = sha256(binary)
            if actual != expected:
                failures.append(
                    f"{binary}: component hash mismatch {actual} != {expected}"
                )
            expected_source = str(component["source_sha256"])
            actual_source = sha256(source)
            if actual_source != expected_source:
                failures.append(
                    f"{source}: source hash mismatch "
                    f"{actual_source} != {expected_source}"
                )
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        failures.append(f"OpenOCD component manifest is invalid: {exc}")

    if failures:
        raise SystemExit("macOS portable compatibility check failed:\n" + "\n".join(failures))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("app", type=Path)
    parser.add_argument("--minimum-version", required=True)
    args = parser.parse_args()
    verify(args.app.resolve(), args.minimum_version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
