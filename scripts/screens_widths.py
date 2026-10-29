#!/usr/bin/env python3
"""Regenerate ``src/bot/screens/widths.json`` from the Liberation Sans TTFs.

The screen checks (dv-4cd8) measure button labels the way the phone does,
but the bot venv has no Pillow or fontTools. So the advance widths are read
once, here, with a small TrueType reader (``cmap`` format 4 + ``hmtx``,
``struct`` only), and checked in as JSON. Liberation Sans has Arial's
metrics, which is close enough to the Telegram client's UI font to calibrate
truncation (analysis dv-4cd8 §4).

Usage (from the repo root):
    python scripts/screens_widths.py            # rewrite widths.json
    python scripts/screens_widths.py --check    # exit 1 if it would change
"""

import argparse
import json
import struct
import sys
from pathlib import Path
from typing import Dict, Tuple

FONT_DIR = Path("/usr/share/fonts/truetype/liberation")
FONTS = {
    "bold": FONT_DIR / "LiberationSans-Bold.ttf",
    "regular": FONT_DIR / "LiberationSans-Regular.ttf",
}
# Basic Latin, Latin-1, Cyrillic, General Punctuation, arrows, math,
# geometric shapes, misc symbols, dingbats (✓ ➖ ➕ live here if the font has
# them; a codepoint the font lacks is left out and measured as a fallback).
RANGES = (
    (0x20, 0x7E),
    (0xA0, 0xFF),
    (0x400, 0x4FF),
    (0x2000, 0x206F),
    (0x20A0, 0x20BF),
    (0x2100, 0x214F),
    (0x2190, 0x21FF),
    (0x2200, 0x22FF),
    (0x2300, 0x23FF),
    (0x2500, 0x25FF),
    (0x2600, 0x27BF),
)
OUT = Path(__file__).resolve().parent.parent / "src/bot/screens/widths.json"


def _tables(data: bytes) -> Dict[str, Tuple[int, int]]:
    (num,) = struct.unpack_from(">H", data, 4)
    tables = {}
    for i in range(num):
        tag, _, offset, length = struct.unpack_from(">4sIII", data, 12 + 16 * i)
        tables[tag.decode("latin-1")] = (offset, length)
    return tables


def _cmap4(data: bytes, cmap: int) -> Dict[int, int]:
    """codepoint → glyph id from the Windows Unicode BMP subtable."""
    (count,) = struct.unpack_from(">H", data, cmap + 2)
    sub = None
    for i in range(count):
        platform, encoding, offset = struct.unpack_from(">HHI", data, cmap + 4 + 8 * i)
        if (platform, encoding) == (3, 1):
            sub = cmap + offset
    if sub is None or struct.unpack_from(">H", data, sub)[0] != 4:
        raise ValueError("no (3,1) format 4 cmap subtable")
    (seg_x2,) = struct.unpack_from(">H", data, sub + 6)
    segs = seg_x2 // 2
    ends = sub + 14
    starts = ends + seg_x2 + 2
    deltas = starts + seg_x2
    range_offs = deltas + seg_x2
    glyphs: Dict[int, int] = {}
    for s in range(segs):
        (end,) = struct.unpack_from(">H", data, ends + 2 * s)
        (start,) = struct.unpack_from(">H", data, starts + 2 * s)
        (delta,) = struct.unpack_from(">h", data, deltas + 2 * s)
        ro_at = range_offs + 2 * s
        (ro,) = struct.unpack_from(">H", data, ro_at)
        for cp in range(start, end + 1):
            if cp == 0xFFFF:
                continue
            if ro == 0:
                gid = (cp + delta) & 0xFFFF
            else:
                (gid,) = struct.unpack_from(">H", data, ro_at + ro + 2 * (cp - start))
                gid = (gid + delta) & 0xFFFF if gid else 0
            if gid:
                glyphs[cp] = gid
    return glyphs


def font_widths(path: Path) -> Tuple[int, Dict[int, int]]:
    """(unitsPerEm, codepoint → advance width in font units) for RANGES."""
    data = path.read_bytes()
    t = _tables(data)
    (units_per_em,) = struct.unpack_from(">H", data, t["head"][0] + 18)
    (n_metrics,) = struct.unpack_from(">H", data, t["hhea"][0] + 34)
    hmtx = t["hmtx"][0]

    def advance(gid: int) -> int:
        i = min(gid, n_metrics - 1)
        return int(struct.unpack_from(">H", data, hmtx + 4 * i)[0])

    glyphs = _cmap4(data, t["cmap"][0])
    widths = {
        cp: advance(glyphs[cp])
        for lo, hi in RANGES
        for cp in range(lo, hi + 1)
        if cp in glyphs
    }
    return units_per_em, widths


def build() -> str:
    fonts: Dict[str, Dict[str, int]] = {}
    units = set()
    for name, path in FONTS.items():
        upm, by_cp = font_widths(path)
        units.add(upm)
        fonts[name] = {str(cp): w for cp, w in sorted(by_cp.items())}
    if len(units) != 1:
        raise ValueError(f"fonts disagree on unitsPerEm: {units}")
    lines = [
        "{",
        f'"source": {json.dumps({k: p.name for k, p in FONTS.items()})},',
        f'"units_per_em": {units.pop()},',
        '"fonts": {',
    ]
    for i, (name, widths) in enumerate(fonts.items()):
        comma = "," if i < len(fonts) - 1 else ""
        lines.append(f"{json.dumps(name)}: {json.dumps(widths)}{comma}")
    lines += ["}", "}"]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    text = build()
    if args.check:
        current = OUT.read_text(encoding="utf-8") if OUT.exists() else ""
        if current != text:
            print(f"{OUT} is stale: run scripts/screens_widths.py", file=sys.stderr)
            return 1
        return 0
    OUT.write_text(text, encoding="utf-8")
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
