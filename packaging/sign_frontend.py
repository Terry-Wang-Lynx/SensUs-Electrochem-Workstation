#!/usr/bin/env python3
"""Create an Ed25519-signed stable frontend ZIP and manifest."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import zipfile
from pathlib import Path

from cryptography.hazmat.primitives import serialization

from pa_host.frontend_update import BACKEND_API_MAJOR, REQUIRED_FILES, _canonical_manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("frontend", type=Path)
    parser.add_argument("--version", required=True)
    parser.add_argument("--zip-url", required=True)
    parser.add_argument("--private-key", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    missing = [name for name in REQUIRED_FILES if not (args.frontend / name).is_file()]
    if missing:
        raise SystemExit(f"frontend is incomplete: {', '.join(missing)}")
    args.output.mkdir(parents=True, exist_ok=True)
    archive = args.output / f"frontend-{args.version}.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for path in sorted(args.frontend.iterdir()):
            if path.is_file():
                bundle.write(path, path.name)
    manifest = {
        "schema_version": 1,
        "channel": "stable",
        "version": args.version,
        "api_major": BACKEND_API_MAJOR,
        "zip_url": args.zip_url,
        "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
    }
    private_key = serialization.load_pem_private_key(
        args.private_key.read_bytes(), password=None
    )
    manifest["signature"] = base64.b64encode(
        private_key.sign(_canonical_manifest(manifest))
    ).decode("ascii")
    (args.output / "stable.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(archive)
    print(args.output / "stable.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
