#!/usr/bin/env python3
"""Create white-background workstation icons from the shared SensUs logo."""

from __future__ import annotations

import struct
import sys
from io import BytesIO
from pathlib import Path

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
LOGO_SOURCE = ROOT / "branding" / "sensus-logo-source.png"


_ICNS_PNG_TYPES = (
    (16, b"icp4"),
    (32, b"icp5"),
    (64, b"icp6"),
    (128, b"ic07"),
    (256, b"ic08"),
    (512, b"ic09"),
    (1024, b"ic10"),
)


def _write_icns(image: Image.Image, output: Path) -> None:
    """Write a PNG-backed ICNS without Apple's xattr-sensitive iconutil."""
    entries: list[bytes] = []
    for size, icon_type in _ICNS_PNG_TYPES:
        resized = image.resize((size, size), Image.Resampling.LANCZOS)
        payload = BytesIO()
        resized.save(payload, format="PNG")
        data = payload.getvalue()
        entries.append(icon_type + struct.pack(">I", len(data) + 8) + data)
    body = b"".join(entries)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(b"icns" + struct.pack(">I", len(body) + 8) + body)


def render_logo(size: int) -> Image.Image:
    """Preserve the supplied mark and center it on an opaque white canvas."""
    with Image.open(LOGO_SOURCE) as source:
        mark = source.convert("RGBA")
    available = round(size * 0.90)
    scale = min(available / mark.width, available / mark.height)
    mark = mark.resize(
        (round(mark.width * scale), round(mark.height * scale)),
        Image.Resampling.LANCZOS,
    )
    image = Image.new("RGBA", (size, size), "#ffffff")
    position = ((size - mark.width) // 2, (size - mark.height) // 2)
    image.alpha_composite(mark, position)
    return image


def main() -> int:
    output = Path(sys.argv[1])
    image = render_logo(1024)
    output.parent.mkdir(parents=True, exist_ok=True)
    image.save(output)
    if len(sys.argv) > 2:
        _write_icns(image, Path(sys.argv[2]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
