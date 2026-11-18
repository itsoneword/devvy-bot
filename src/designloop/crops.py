#!/usr/bin/env python3
"""The critic's crops of one render (dv-696e), from the render's DOM sidecar.

Run by the board's python, which has Pillow (the bot venv does not):

    python crops.py --png a1/main-dark.png --sidecar a1/main-dark.boxes.json \\
        --out a1/critic

The sidecar ``<state>-<theme>.boxes.json`` is written by the board's render
script: ``{"scale": n, "content_height": css px, "blocks": [{"id", "x", "y",
"w", "h"}]}`` in CSS px (``getBoundingClientRect`` per ``data-testid``).
Writes copies into ``--out`` (never beside the original: render and gate glob
that folder): ``<stem>.page.png`` cut to the content height and one
``<stem>.<id>.png`` per block box. A box outside the image or with no area is
skipped with a reason, never guessed. Prints the result as JSON.
Stdlib + Pillow only (Pillow imported on use, so the bot can import this).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Union

PathLike = Union[str, Path]


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", name).strip("_") or "block"


def make_crops(png: PathLike, sidecar: PathLike, out_dir: PathLike) -> Dict[str, Any]:
    """``{"page": path, "blocks": {id: path}, "skipped": ["id: why"]}``; block
    ids keep the sidecar's (document) order."""
    from PIL import Image

    png, out = Path(png), Path(out_dir)
    data = json.loads(Path(sidecar).read_text(encoding="utf-8"))
    scale = float(data.get("scale") or 1)
    out.mkdir(parents=True, exist_ok=True)
    stem = png.stem
    blocks: Dict[str, str] = {}
    skipped: List[str] = []
    with Image.open(png) as image:
        width, height = image.size
        content = data.get("content_height")
        bottom = height
        if isinstance(content, (int, float)) and content > 0:
            bottom = max(1, min(height, round(content * scale)))
        page = out / f"{stem}.page.png"
        image.crop((0, 0, width, bottom)).save(page)
        for box in data.get("blocks") or []:
            ident = str(box.get("id") or "")
            if not ident:
                skipped.append(f"?: no id in {box!r}")
                continue
            if ident in blocks:
                continue
            try:
                x, y, w, h = (float(box[k]) * scale for k in ("x", "y", "w", "h"))
            except (KeyError, TypeError, ValueError):
                skipped.append(f"{ident}: no x/y/w/h")
                continue
            if w <= 0 or h <= 0:
                skipped.append(f"{ident}: zero area")
                continue
            left, top = max(0, round(x)), max(0, round(y))
            right, low = min(width, round(x + w)), min(bottom, round(y + h))
            if right <= left or low <= top:
                skipped.append(f"{ident}: outside the image")
                continue
            path = out / f"{stem}.{_safe(ident)}.png"
            image.crop((left, top, right, low)).save(path)
            blocks[ident] = str(path)
    return {"page": str(page), "blocks": blocks, "skipped": skipped}


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--png", required=True)
    parser.add_argument("--sidecar", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    print(json.dumps(make_crops(args.png, args.sidecar, args.out)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
