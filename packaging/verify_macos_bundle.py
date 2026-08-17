#!/usr/bin/env python3
"""Fail a portable macOS build whose native runtime exceeds its declared OS."""

from __future__ import annotations

import argparse
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


def verify(app: Path, minimum_version: str) -> None:
    required = (
        app / "Contents/Resources/backend/SensUsBackend/SensUsBackend",
        app / "Contents/Resources/workstation/PORTABLE_RESOURCES.txt",
        app / "Contents/Resources/tools/openocd/bin/openocd",
        app / "Contents/Resources/tools/openocd/share/openocd/scripts/interface/jlink.cfg",
        app / "Contents/Resources/tools/openocd/share/openocd/scripts/target/nrf52.cfg",
    )
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise SystemExit("missing portable resources:\n" + "\n".join(missing))

    declared = version_tuple(minimum_version)
    failures: list[str] = []
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
        if "Open On-Chip Debugger" not in output:
            failures.append(f"{openocd}: unexpected --version output")
    except subprocess.CalledProcessError as exc:
        failures.append(f"{openocd}: cannot execute: {exc.stderr.strip()}")

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
