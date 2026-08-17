#!/usr/bin/env python3
"""从共用 SensUs Logo 生成白底 Windows 图标 (.ico)。

需要 Pillow: ``pip install Pillow``
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
LOGO_SOURCE = ROOT / "branding" / "sensus-logo-source.png"


def create_icon(size: int = 256) -> Image.Image:
    """保留用户提供的图形，居中合成到不透明白色画布。"""
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
    output = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("SensUs-Workstation.ico")
    sizes = [256, 128, 64, 48, 32, 16]
    images = [create_image(size) for size in sizes]
    output.parent.mkdir(parents=True, exist_ok=True)
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
