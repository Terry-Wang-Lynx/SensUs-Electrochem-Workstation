#!/usr/bin/env python3
"""Stage read-only firmware and update configuration for portable builds."""

from __future__ import annotations

import argparse
import json
import os
import shutil
from pathlib import Path


def copy_tree(source: Path, destination: Path) -> None:
    if not source.is_dir():
        raise SystemExit(f"missing packaging resource: {source}")
    shutil.copytree(source, destination, dirs_exist_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("destination", type=Path)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    args = parser.parse_args()
    root = args.root.resolve()
    destination = args.destination.resolve()
    shutil.rmtree(destination, ignore_errors=True)
    destination.mkdir(parents=True)

    copy_tree(
        root / "software" / "firmware" / "prebuilt",
        destination / "software" / "firmware" / "prebuilt",
    )
    copy_tree(
        root / "packaging" / "resources" / "v51",
        destination / "software" / "ver5.1",
    )
    config_dir = destination / "config"
    config_dir.mkdir()
    (config_dir / "frontend-update.json").write_text(
        json.dumps(
            {
                "channel": "stable",
                "manifest_url": os.environ.get("SENSUS_FRONTEND_MANIFEST_URL", ""),
                "public_key": os.environ.get("SENSUS_FRONTEND_PUBLIC_KEY", ""),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    (destination / "PORTABLE_RESOURCES.txt").write_text(
        "Read-only resources for SensUs Workstation portable builds.\n",
        encoding="ascii",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
