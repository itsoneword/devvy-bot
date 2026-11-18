"""Draw a short headline on a generated picture (dv-fc9d).

The image model draws no text (owner 12.11.26: cheap models draw Russian
badly); this code puts the headline on: a dark band at the bottom for
contrast, lines wrapped by measured width. Fonts are the bot screens' ones
(``src/bot/screens/render.py`` ``FONTS``): Liberation Sans Bold, DejaVu Sans
Bold as fallback; both have Cyrillic.
"""

from __future__ import annotations

import io
from typing import Any, List

MAX_HEADLINE_CHARS = 80
_TEXT = (255, 255, 255)
_BAND = (0, 0, 0, 165)


def _font(px: int) -> Any:
    from PIL import ImageFont

    from ..bot.screens.render import FONTS

    for key in ("bold", "symbol_bold"):
        path = FONTS[key]
        if path.is_file():
            return ImageFont.truetype(str(path), px)
    raise RuntimeError("no headline font (Liberation Sans / DejaVu Sans) on this host")


def _wrap(draw: Any, text: str, font: Any, width: int) -> List[str]:
    lines: List[str] = []
    line = ""
    for word in text.split():
        trial = f"{line} {word}".strip()
        if line and draw.textlength(trial, font=font) > width:
            lines.append(line)
            line = word
        else:
            line = trial
    if line:
        lines.append(line)
    return lines


def draw_headline(image: bytes, headline: str) -> bytes:
    """``image`` with ``headline`` drawn in a bottom band, as PNG bytes; same
    size as the input. Raises ``ValueError`` for an empty or too long one."""
    from PIL import Image, ImageDraw

    headline = " ".join((headline or "").split())
    if not headline:
        raise ValueError("headline is empty")
    if len(headline) > MAX_HEADLINE_CHARS:
        raise ValueError(
            f"headline is {len(headline)} characters; the limit is {MAX_HEADLINE_CHARS}"
        )
    base = Image.open(io.BytesIO(image)).convert("RGBA")
    w, h = base.size
    margin = max(12, w // 20)
    px = max(14, w // 16)
    probe = ImageDraw.Draw(base)
    font = _font(px)
    lines = _wrap(probe, headline, font, w - 2 * margin)
    # Shrink until the text takes at most 40% of the picture's height.
    while px > 14 and len(lines) * px * 1.25 > h * 0.4:
        px = int(px * 0.85)
        font = _font(px)
        lines = _wrap(probe, headline, font, w - 2 * margin)
    line_h = int(px * 1.25)
    band_h = line_h * len(lines) + margin
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    draw.rectangle([(0, h - band_h), (w, h)], fill=_BAND)
    y = h - band_h + margin // 2
    for line in lines:
        draw.text((margin, y), line, font=font, fill=_TEXT)
        y += line_h
    out = Image.alpha_composite(base, overlay).convert("RGB")
    buf = io.BytesIO()
    out.save(buf, format="PNG")
    return buf.getvalue()
