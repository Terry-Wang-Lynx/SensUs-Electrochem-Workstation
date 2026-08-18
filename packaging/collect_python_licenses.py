#!/usr/bin/env python3
"""Collect licenses and an SPDX inventory from the exact portable venv."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import re
import shutil
import sys
from pathlib import Path


PROJECT_NAME = "sensus-electrochem-workstation"
LICENSE_PREFIXES = ("license", "copying", "notice", "authors", "copyright")
LICENSE_DIR = Path(__file__).resolve().with_name("licenses")
SUPPLEMENTAL_LICENSES = {
    ("pyserial", "3.5"): {
        "path": LICENSE_DIR / "pyserial-3.5-LICENSE.txt",
        "sha256": "f91cb9813de6a5b142b8f7f2dede630b5134160aedaeaf55f4d6a7e2593ca3f3",
        "source": "pyserial-3.5.tar.gz/LICENSE.txt",
        "source_url": (
            "https://files.pythonhosted.org/packages/source/p/pyserial/"
            "pyserial-3.5.tar.gz"
        ),
        "source_sha256": (
            "3c77e014170dfffbd816e6ffc205e9842efb10be9f58ec16d3e8675b4925cddb"
        ),
    },
    ("proxy-tools", "0.1.0"): {
        "path": LICENSE_DIR / "proxy_tools-0.1.0-LICENSE.txt",
        "sha256": "e91e13d5d5e3782c7f11006c2f6585079bede422f63589a1b6bcd1afcf24e1fd",
        "source": "proxy_tools commit 70b751ef LICENSE.txt",
        "source_url": (
            "https://raw.githubusercontent.com/jtushman/proxy_tools/"
            "70b751ef5e0647d974506fd5871903711b5e1811/LICENSE.txt"
        ),
        "source_sha256": (
            "a428fb8a2e762af3eb0a6edbbb88e9b42ccfee80fd9b423958bcacf9b9abbfe4"
        ),
        "license_expression": "BSD-3-Clause",
    },
}
PYWINRT_LICENSE = {
    "path": LICENSE_DIR / "pywinrt-3.2.1-LICENSE.txt",
    "sha256": "6e898069e8b3c6d8d23dc70ac7067cc2b7c9db14c36df873026758db82c0891d",
    "source": "PyWinRT v3.2.1 LICENSE",
    "source_url": (
        "https://raw.githubusercontent.com/pywinrt/pywinrt/v3.2.1/LICENSE"
    ),
    "source_sha256": (
        "6e898069e8b3c6d8d23dc70ac7067cc2b7c9db14c36df873026758db82c0891d"
    ),
    "license_expression": "MIT",
}


def canonical_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def safe_component_name(name: str, version: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", f"{name}-{version}").strip("-")


def license_expression(metadata: importlib.metadata.PackageMetadata) -> str:
    expression = str(metadata.get("License-Expression") or "").strip()
    return expression or "NOASSERTION"


def declared_license(metadata: importlib.metadata.PackageMetadata) -> str:
    expression = str(metadata.get("License-Expression") or "").strip()
    legacy = str(metadata.get("License") or "").strip()
    classifiers = [
        value.removeprefix("License :: ")
        for value in metadata.get_all("Classifier", [])
        if value.startswith("License :: ")
    ]
    return expression or legacy or "; ".join(classifiers) or "NOASSERTION"


def license_files(distribution: importlib.metadata.Distribution) -> list[Path]:
    selected: list[Path] = []
    for relative in distribution.files or ():
        relative_path = Path(str(relative))
        name = relative_path.name.lower()
        if not name.startswith(LICENSE_PREFIXES):
            continue
        if any(part.lower().endswith(".dsym") for part in relative_path.parts):
            continue
        located = Path(distribution.locate_file(relative))
        if not located.is_file() or located.stat().st_size > 2 * 1024 * 1024:
            continue
        try:
            if b"\0" in located.read_bytes()[:8192]:
                continue
        except OSError:
            continue
        selected.append(located)
    return sorted(set(selected), key=lambda path: str(path).lower())


def metadata_license_text(
    metadata: importlib.metadata.PackageMetadata,
) -> str | None:
    value = str(metadata.get("License") or "").strip()
    if len(value) < 120 or "\n" not in value:
        return None
    return value + ("" if value.endswith("\n") else "\n")


def supplemental_license(name: str, version: str) -> dict[str, object] | None:
    canonical = canonical_name(name)
    entry = SUPPLEMENTAL_LICENSES.get((canonical, version))
    if (
        entry is None
        and version == "3.2.1"
        and (canonical == "winrt-runtime" or canonical.startswith("winrt-windows-"))
    ):
        entry = PYWINRT_LICENSE
    if entry is None:
        return None
    path = Path(entry["path"])
    if not path.is_file():
        raise RuntimeError(f"Supplemental license file is missing: {path}")
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != entry["sha256"]:
        raise RuntimeError(
            f"Supplemental license hash mismatch for {name}=={version}: "
            f"{actual} != {entry['sha256']}"
        )
    return {**entry, "path": path}


def shared_pyobjc_license(
    distributions: list[importlib.metadata.Distribution],
    name: str,
    version: str,
) -> tuple[Path, str] | None:
    canonical = canonical_name(name)
    if canonical != "pyobjc-core" and not canonical.startswith("pyobjc-framework-"):
        return None
    candidates = sorted(
        distributions,
        key=lambda item: canonical_name(str(item.metadata.get("Name") or "")),
    )
    for candidate in candidates:
        candidate_name = str(candidate.metadata.get("Name") or "").strip()
        candidate_canonical = canonical_name(candidate_name)
        if candidate.version != version or (
            candidate_canonical != "pyobjc-core"
            and not candidate_canonical.startswith("pyobjc-framework-")
        ):
            continue
        files = license_files(candidate)
        if files:
            return files[0], f"shared PyObjC {version} license from {candidate_name}"
    return None


def python_license() -> Path:
    version = f"python{sys.version_info.major}.{sys.version_info.minor}"
    candidates = (
        Path(sys.base_prefix) / "LICENSE.txt",
        Path(sys.base_prefix) / "LICENSE",
        Path(sys.base_prefix) / "lib" / version / "LICENSE.txt",
    )
    for path in candidates:
        if path.is_file():
            return path
    raise RuntimeError(f"Python {sys.version.split()[0]} license file was not found")


def collect(destination: Path) -> dict[str, object]:
    destination = destination.resolve()
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)

    distributions = sorted(
        importlib.metadata.distributions(),
        key=lambda item: canonical_name(str(item.metadata.get("Name") or "")),
    )
    components: list[dict[str, object]] = []
    missing: list[str] = []
    seen: set[str] = set()
    for distribution in distributions:
        name = str(distribution.metadata.get("Name") or "").strip()
        if not name or canonical_name(name) == PROJECT_NAME or canonical_name(name) in seen:
            continue
        seen.add(canonical_name(name))
        version = str(distribution.version)
        component_dir = destination / safe_component_name(name, version)
        files = license_files(distribution)
        copied: list[str] = []
        license_source = "installed distribution"
        metadata_text = metadata_license_text(distribution.metadata)
        supplement = supplemental_license(name, version)
        if not files and metadata_text is None and supplement is None:
            shared = shared_pyobjc_license(distributions, name, version)
            if shared is not None:
                files = [shared[0]]
                license_source = shared[1]
        if files:
            component_dir.mkdir(parents=True)
            used_names: set[str] = set()
            for index, source in enumerate(files, start=1):
                output_name = source.name
                if output_name.lower() in used_names:
                    output_name = f"{index}-{output_name}"
                used_names.add(output_name.lower())
                target = component_dir / output_name
                shutil.copy2(source, target)
                copied.append(str(target.relative_to(destination)))
        elif metadata_text is not None:
            component_dir.mkdir(parents=True)
            target = component_dir / "LICENSE-from-package-metadata.txt"
            target.write_text(metadata_text, encoding="utf-8")
            copied.append(str(target.relative_to(destination)))
            license_source = "installed package metadata License field"
        elif supplement is not None:
            component_dir.mkdir(parents=True)
            source = Path(supplement["path"])
            target = component_dir / source.name
            shutil.copy2(source, target)
            copied.append(str(target.relative_to(destination)))
            license_source = str(supplement["source"])
        else:
            missing.append(f"{name}=={version}")
        component: dict[str, object] = {
            "name": name,
            "version": version,
            "declared_license": (
                str(supplement["license_expression"])
                if supplement is not None and supplement.get("license_expression")
                else declared_license(distribution.metadata)
            ),
            "license_expression": (
                str(supplement["license_expression"])
                if supplement is not None and supplement.get("license_expression")
                else license_expression(distribution.metadata)
            ),
            "homepage": str(
                distribution.metadata.get("Home-page")
                or distribution.metadata.get("Project-URL")
                or ""
            ),
            "license_files": copied,
            "license_source": license_source,
        }
        if supplement is not None:
            component["license_source_url"] = supplement["source_url"]
            component["license_source_sha256"] = supplement["source_sha256"]
        components.append(component)

    python_version = sys.version.split()[0]
    python_dir = destination / f"Python-{python_version}"
    python_dir.mkdir()
    python_target = python_dir / "LICENSE.txt"
    shutil.copy2(python_license(), python_target)
    components.insert(0, {
        "name": "Python",
        "version": python_version,
        "declared_license": "Python-2.0",
        "license_expression": "Python-2.0",
        "homepage": "https://www.python.org/",
        "license_files": [str(python_target.relative_to(destination))],
    })

    if missing:
        raise RuntimeError(
            "Portable dependencies without distributable license files: "
            + ", ".join(missing)
        )

    inventory = {
        "schema": 1,
        "python": python_version,
        "platform": sys.platform,
        "components": components,
    }
    (destination / "PYTHON_PACKAGES.json").write_text(
        json.dumps(inventory, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    digest = hashlib.sha256(
        json.dumps(components, sort_keys=True).encode("utf-8")
    ).hexdigest()
    packages = []
    relationships = []
    for index, component in enumerate(components, start=1):
        package_id = f"SPDXRef-Package-{index}"
        packages.append({
            "SPDXID": package_id,
            "name": component["name"],
            "versionInfo": component["version"],
            "downloadLocation": "NOASSERTION",
            "filesAnalyzed": False,
            "licenseConcluded": "NOASSERTION",
            "licenseDeclared": component["license_expression"],
            "copyrightText": "NOASSERTION",
        })
        relationships.append({
            "spdxElementId": "SPDXRef-DOCUMENT",
            "relationshipType": "DESCRIBES",
            "relatedSpdxElement": package_id,
        })
    spdx = {
        "spdxVersion": "SPDX-2.3",
        "dataLicense": "CC0-1.0",
        "SPDXID": "SPDXRef-DOCUMENT",
        "name": "SensUs portable Python runtime",
        "documentNamespace": f"https://sensus.local/sbom/{digest}",
        "creationInfo": {
            "creators": ["Tool: packaging/collect_python_licenses.py"],
            "created": "1970-01-01T00:00:00Z",
        },
        "packages": packages,
        "relationships": relationships,
    }
    (destination / "SBOM.spdx.json").write_text(
        json.dumps(spdx, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return inventory


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    inventory = collect(args.destination)
    print(
        f"Collected {len(inventory['components'])} Python runtime components "
        f"into {args.destination}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
