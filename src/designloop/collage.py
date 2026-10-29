#!/usr/bin/env python3
"""One collage image of the design variants (dv-9133 phase 3).

Run by the board's python, which has Pillow (the bot venv does not):

    python collage.py --out collage.png \\
        --tile v1/a1/main-dark.png --label "1. one list, filters as chips" \\
        --tile v2/a1/main-dark.png --label "2. map first, list in a sheet"

Tiles side by side at one height, a number and the trade-off line under each.
Stdlib + Pillow only: this file is not imported by the bot.
"""

from __future__ import annotations

import argparse
import sys
import textwrap
from pathlib import Path
from typing import List, Sequence, Tuple

TILE_HEIGHT = 1400
GAP = 40
LABEL_HEIGHT = 170
BACKGROUND = (24, 24, 27)
TEXT = (240, 240, 240)
FONT_SIZE = 40
WRAP = 34


def layout(
    sizes: Sequence[Tuple[int, int]], height: int = TILE_HEIGHT
) -> List[Tuple[int, int, int, int]]:
    """(x, y, w, h) of each tile scaled to ``height``, left to right."""
    boxes = []
    x = GAP
    for w, h in sizes:
        scaled = max(1, round(w * height / h))
        boxes.append((x, GAP, scaled, height))
        x += scaled + GAP
    return boxes


def _font(size: int):  # type: ignore[no-untyped-def]
    from PIL import ImageFont  # type: ignore[import-not-found]

    for name in ("DejaVuSans-Bold.ttf", "DejaVuSans.ttf", "Arial.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def build(tiles: Sequence[Path], labels: Sequence[str], out: Path) -> None:
    from PIL import Image, ImageDraw  # type: ignore[import-not-found]

    images = [Image.open(p).convert("RGB") for p in tiles]
    boxes = layout([im.size for im in images])
    width = boxes[-1][0] + boxes[-1][2] + GAP
    sheet = Image.new("RGB", (width, TILE_HEIGHT + LABEL_HEIGHT + 2 * GAP), BACKGROUND)
    draw = ImageDraw.Draw(sheet)
    font = _font(FONT_SIZE)
    for image, (x, y, w, h), label in zip(images, boxes, labels):
        sheet.paste(image.resize((w, h)), (x, y))
        lines = textwrap.wrap(label, WRAP)[:3]
        draw.multiline_text(
            (x, y + h + 16), "\n".join(lines), fill=TEXT, font=font, spacing=8
        )
    out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out)


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--tile", type=Path, action="append", default=[])
    ap.add_argument("--label", action="append", default=[])
    args = ap.parse_args(argv)
    if not args.tile or len(args.tile) != len(args.label):
        ap.error("one --label per --tile, at least one tile")
    build(args.tile, args.label, args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
