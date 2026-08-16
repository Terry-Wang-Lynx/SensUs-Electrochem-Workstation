#!/usr/bin/env python3
"""Create the source raster for the macOS workstation icon."""

from __future__ import annotations

import math
import struct
import sys
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw


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


def main() -> int:
    output = Path(sys.argv[1])
    size = 1024
    image = Image.new("RGBA", (size, size), "#eef2f3")
    draw = ImageDraw.Draw(image)

    draw.rounded_rectangle((70, 70, 954, 954), radius=205, fill="#ffffff")
    draw.rounded_rectangle((132, 132, 892, 892), radius=148, fill="#f7faf9")

    axis = "#26373d"
    teal = "#147d78"
    coral = "#d7654f"
    grid = "#d9e3e3"

    for fraction in (0.25, 0.5, 0.75):
        x = 230 + int(570 * fraction)
        y = 250 + int(500 * fraction)
        draw.line((x, 250, x, 750), fill=grid, width=5)
        draw.line((230, y, 800, y), fill=grid, width=5)

    draw.line((230, 250, 230, 750, 800, 750), fill=axis, width=18, joint="curve")

    forward: list[tuple[float, float]] = []
    reverse: list[tuple[float, float]] = []
    for index in range(241):
        value = -1.0 + 2.0 * index / 240.0
        x = 515 + value * 255
        forward_current = 0.53 * math.tanh(3.4 * (value - 0.08)) + 0.20 * math.sin(
            math.pi * (value + 0.18)
        )
        reverse_current = 0.49 * math.tanh(3.1 * (value + 0.11)) - 0.21 * math.sin(
            math.pi * (value - 0.10)
        )
        forward.append((x, 510 - 260 * forward_current))
        reverse.append((x, 510 - 260 * reverse_current))

    draw.line(forward, fill=teal, width=22, joint="curve")
    draw.line(list(reversed(reverse)), fill=coral, width=22, joint="curve")
    point_x, point_y = forward[173]
    draw.ellipse((point_x - 23, point_y - 23, point_x + 23, point_y + 23), fill=axis)
    draw.ellipse((point_x - 13, point_y - 13, point_x + 13, point_y + 13), fill="#ffffff")

    for x, color, height in ((310, teal, 82), (390, coral, 112), (470, axis, 66)):
        draw.rounded_rectangle((x, 160, x + 38, 160 + height), radius=19, fill=color)

    output.parent.mkdir(parents=True, exist_ok=True)
    image.save(output)
    if len(sys.argv) > 2:
        _write_icns(image, Path(sys.argv[2]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
