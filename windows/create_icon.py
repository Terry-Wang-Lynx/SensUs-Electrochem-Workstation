#!/usr/bin/env python3
"""生成 SensUs 工作站的 Windows 图标 (.ico)。

需要 Pillow: ``pip install Pillow``
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

from PIL import Image, ImageDraw


def create_icon(size: int = 256) -> Image.Image:
    """绘制 SensUs 电化学工作站图标。"""
    img = Image.new("RGBA", (size, size), "#eef2f3")
    draw = ImageDraw.Draw(img)

    margin = int(size * 0.068)
    radius_outer = int(size * 0.200)
    radius_inner = int(size * 0.145)

    draw.rounded_rectangle(
        (margin, margin, size - margin, size - margin),
        radius=radius_outer, fill="#ffffff",
    )
    draw.rounded_rectangle(
        (int(size * 0.129), int(size * 0.129),
         int(size * 0.871), int(size * 0.871)),
        radius=radius_inner, fill="#f7faf9",
    )

    axis = "#26373d"
    teal = "#147d78"
    coral = "#d7654f"
    grid = "#d9e3e3"

    origin_x = int(size * 0.225)
    origin_y = int(size * 0.244)
    end_x = int(size * 0.781)
    end_y = int(size * 0.732)
    width = end_x - origin_x
    height = end_y - origin_y

    for fraction in (0.25, 0.5, 0.75):
        x = origin_x + int(width * fraction)
        y = origin_y + int(height * fraction)
        draw.line((x, origin_y, x, end_y), fill=grid, width=max(1, size // 205))
        draw.line((origin_x, y, end_x, y), fill=grid, width=max(1, size // 205))

    line_w = max(1, size // 57)
    draw.line((origin_x, origin_y, origin_x, end_y, end_x, end_y),
              fill=axis, width=line_w, joint="curve")

    center_x = (origin_x + end_x) // 2
    center_y = (origin_y + end_y) // 2

    forward: list[tuple[float, float]] = []
    reverse: list[tuple[float, float]] = []
    for idx in range(241):
        value = -1.0 + 2.0 * idx / 240.0
        x = center_x + value * width * 0.448
        fwd = 0.53 * math.tanh(3.4 * (value - 0.08)) + 0.20 * math.sin(
            math.pi * (value + 0.18)
        )
        rev = 0.49 * math.tanh(3.1 * (value + 0.11)) - 0.21 * math.sin(
            math.pi * (value - 0.10)
        )
        forward.append((x, center_y - height * 0.52 * fwd))
        reverse.append((x, center_y - height * 0.52 * rev))

    cv_w = max(1, size // 47)
    draw.line(forward, fill=teal, width=cv_w, joint="curve")
    draw.line(list(reversed(reverse)), fill=coral, width=cv_w, joint="curve")

    pt_x, pt_y = forward[173]
    dot_r = max(1, size // 45)
    draw.ellipse((pt_x - dot_r, pt_y - dot_r, pt_x + dot_r, pt_y + dot_r), fill=axis)
    inner_r = max(1, size // 79)
    draw.ellipse((pt_x - inner_r, pt_y - inner_r, pt_x + inner_r, pt_y + inner_r),
                 fill="#ffffff")

    bar_x = int(size * 0.303)
    bars = [
        (bar_x, teal, int(size * 0.080)),
        (int(size * 0.381), coral, int(size * 0.109)),
        (int(size * 0.459), axis, int(size * 0.064)),
    ]
    bar_w = max(1, size // 27)
    for x, color, h in bars:
        draw.rounded_rectangle(
            (x, int(size * 0.156), x + bar_w, int(size * 0.156) + h),
            radius=max(1, size // 54), fill=color,
        )

    return img


def main() -> int:
    output = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("SensUs-Workstation.ico")
    sizes = [256, 128, 64, 48, 32, 16]
    images = [create_image(size) for size in sizes]
    images[0].save(
        output, format="ICO", sizes=[(s, s) for s in sizes],
        append_images=images[1:],
    )
    print(f"图标已生成: {output} ({len(sizes)} 种尺寸)")
    return 0


def create_image(size: int) -> Image.Image:
    return create_icon(size)


if __name__ == "__main__":
    raise SystemExit(main())
