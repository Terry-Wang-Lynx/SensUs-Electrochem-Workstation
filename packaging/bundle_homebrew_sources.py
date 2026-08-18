#!/usr/bin/env python3
"""Bundle exact Homebrew formula source and patches for copied macOS dylibs."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import urllib.parse
import urllib.request
from pathlib import Path


URL_RE = re.compile(r'^\s*url\s+"([^"]+)"', re.MULTILINE)
SHA_RE = re.compile(r'^\s*sha256\s+"([0-9a-f]{64})"', re.MULTILINE)
PATCH_RE = re.compile(
    r"patch do\s+url\s+\"([^\"]+)\"\s+sha256\s+\"([0-9a-f]{64})\"\s+end",
    re.DOTALL,
)
LICENSE_RE = re.compile(
    r"^(?:licen[cs]e|copying|notice|copyright|authors)(?:[._-].*)?$",
    re.IGNORECASE,
)


def command(*arguments: str) -> str:
    return subprocess.run(
        arguments, check=True, capture_output=True, text=True, encoding="utf-8",
    ).stdout.strip()


def download(url: str, expected_sha: str, target: Path) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "SensUs-packager/1"})
    digest = hashlib.sha256()
    with urllib.request.urlopen(request, timeout=120) as response, target.open("wb") as out:
        while chunk := response.read(1024 * 1024):
            digest.update(chunk)
            out.write(chunk)
    actual = digest.hexdigest()
    if actual != expected_sha:
        target.unlink(missing_ok=True)
        raise RuntimeError(f"SHA-256 mismatch for {url}: {actual}")


def copy_licenses(prefix: Path, destination: Path) -> list[str]:
    copied: list[str] = []
    for source in sorted(prefix.rglob("*"), key=lambda item: str(item).lower()):
        if not source.is_file() or not LICENSE_RE.match(source.name):
            continue
        relative = source.relative_to(prefix)
        target = destination / "licenses" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        copied.append(str(target.relative_to(destination)))
    if not copied:
        raise RuntimeError(f"no installed license files found under {prefix}")
    return copied


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("destination", type=Path)
    parser.add_argument("formulas", nargs="+")
    args = parser.parse_args()
    destination = args.destination.resolve()
    destination.mkdir(parents=True, exist_ok=True)
    components: list[dict[str, object]] = []

    for formula in args.formulas:
        version_line = command("brew", "list", "--versions", formula)
        installed_version = version_line.split(maxsplit=1)[1]
        installed_prefix = Path(command("brew", "--prefix", formula)).resolve()
        formula_text = command("brew", "cat", formula) + "\n"
        source_url = URL_RE.search(formula_text)
        source_sha = SHA_RE.search(formula_text)
        if source_url is None or source_sha is None:
            raise RuntimeError(f"cannot resolve pinned source for {formula}")
        component_dir = destination / formula
        component_dir.mkdir(exist_ok=True)
        (component_dir / "Formula.rb").write_text(formula_text, encoding="utf-8")
        suffix = "".join(
            Path(urllib.parse.urlparse(source_url.group(1)).path).suffixes
        )
        source_name = f"source-{installed_version}{suffix or '.archive'}"
        download(source_url.group(1), source_sha.group(1), component_dir / source_name)
        licenses = copy_licenses(installed_prefix, component_dir)
        patches: list[dict[str, str]] = []
        for index, (url, sha) in enumerate(PATCH_RE.findall(formula_text), start=1):
            patch_name = f"patch-{index:02d}.patch"
            download(url, sha, component_dir / patch_name)
            patches.append({"url": url, "sha256": sha, "file": patch_name})
        components.append({
            "formula": formula,
            "installed_version": installed_version,
            "source_url": source_url.group(1),
            "source_sha256": source_sha.group(1),
            "source_file": source_name,
            "installed_prefix": str(installed_prefix),
            "license_files": licenses,
            "patches": patches,
        })

    (destination / "homebrew-components.json").write_text(
        json.dumps({"schema": 1, "components": components}, indent=2) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
