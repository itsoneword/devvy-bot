"""Draw a bot screen as the phone shows it, to a PNG (dv-4cd8 phase 2).

The checks say *what* is wrong; review (phase 3) also has to *see* the
screen, with the ticket's goal next to it, and answer "would a first-time
user understand this?". This module draws one ``Screen`` at the phone's
width, offline and deterministically:

* ``layout(screen)`` is pure (no Pillow): every keyboard button with the
  text the phone shows (``metrics.visible_text``: "Clau…able"), where it
  sits, and whether ``checks.check`` flagged it;
* ``render(screen)`` draws that layout at 2x (390 pt → 780 px): the message
  bubble, the keyboard, a red outline round every flagged button and the
  violations listed in red under it. A passing screen has no ``RED`` pixel.

Geometry comes from ``metrics`` only, so what is drawn and what is checked
cannot drift apart. Fonts are the host's Liberation Sans (Arial metrics,
the same table the checks measure with), DejaVu Sans for symbols Liberation
lacks and Noto Color Emoji for emoji. Pillow is imported only inside
``render()``: the bot venv runs the layout and the checks without it.
"""

from dataclasses import dataclass
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import metrics
from .checks import Violation, check
from .model import Screen

RGB = Tuple[int, int, int]

#: Violation marks. Nothing else in a render uses this exact colour, so a
#: passing screen has none of it.
RED: RGB = (230, 40, 40)
CHAT_BG: RGB = (214, 226, 233)
BUBBLE_BG: RGB = (255, 255, 255)
TEXT: RGB = (0, 0, 0)
CAPTION: RGB = (96, 110, 120)
BUTTON_BG: RGB = (118, 140, 155)
BUTTON_TEXT: RGB = (255, 255, 255)

SCALE = 2  # px per pt: a 390 pt phone is a 780 px PNG
MARGIN = (metrics.PHONE_W - metrics.KEYBOARD_W) // 2  # bubble and keyboard left
CAPTION_TOP = 9  # pt above the caption (screen id and goal)
CAPTION_PX = 12
NOTE_PX = 13
RADIUS = 8
FLAG_W = 2  # pt, red outline of a flagged button

FONT_DIR = Path("/usr/share/fonts/truetype")
FONTS = {
    "bold": FONT_DIR / "liberation" / "LiberationSans-Bold.ttf",
    "regular": FONT_DIR / "liberation" / "LiberationSans-Regular.ttf",
    "symbol_bold": FONT_DIR / "dejavu" / "DejaVuSans-Bold.ttf",
    "symbol_regular": FONT_DIR / "dejavu" / "DejaVuSans.ttf",
    "emoji": FONT_DIR / "noto" / "NotoColorEmoji.ttf",
}
#: Noto Color Emoji is a bitmap font with one strike: 109 px.
EMOJI_STRIKE = 109


@dataclass(frozen=True)
class DrawnButton:
    label: str  # the text the phone shows, ellipsised if it does not fit
    text: str  # the button's full label
    flagged: bool
    outline: RGB
    codes: Tuple[str, ...]  # violation codes on this button
    x: float  # pt, from the phone's left edge
    y: float  # pt, from the top of the keyboard
    w: float
    h: float


def _button_codes(violations: List[Violation]) -> Dict[Tuple[int, int], List[str]]:
    """``(row, col)`` → codes, from the ``where`` the checks write
    (``row R button C 'label'``, or ``row R`` for the whole row)."""
    out: Dict[Tuple[int, int], List[str]] = {}
    for v in violations:
        parts = v.where.split(" ")
        if len(parts) >= 4 and parts[0] == "row" and parts[2] == "button":
            out.setdefault((int(parts[1]) - 1, int(parts[3]) - 1), []).append(v.code)
        elif len(parts) == 2 and parts[0] == "row":
            out.setdefault((int(parts[1]) - 1, -1), []).append(v.code)
    return out


def layout(
    screen: Screen, violations: Optional[List[Violation]] = None
) -> List[List[DrawnButton]]:
    """The keyboard as the phone lays it out, one ``DrawnButton`` per button
    in ``screen.rows`` order. ``violations`` defaults to ``check(screen)``."""
    found = _button_codes(check(screen) if violations is None else violations)
    rows: List[List[DrawnButton]] = []
    for r, row in enumerate(screen.rows):
        n = len(row)
        w = (metrics.KEYBOARD_W - (n - 1) * metrics.GAP) / max(1, n)
        y = r * (metrics.BUTTON_H + metrics.GAP)
        drawn = []
        for c, button in enumerate(row):
            codes = tuple(found.get((r, c), []) + found.get((r, -1), []))
            drawn.append(
                DrawnButton(
                    label=metrics.visible_text(button.text, n),
                    text=button.text,
                    flagged=bool(codes),
                    outline=RED if codes else BUTTON_BG,
                    codes=codes,
                    x=MARGIN + c * (w + metrics.GAP),
                    y=y,
                    w=w,
                    h=metrics.BUTTON_H,
                )
            )
        rows.append(drawn)
    return rows


# --- drawing (Pillow) -----------------------------------------------------


def _px(pt: float) -> int:
    return int(round(pt * SCALE))


def _is_emoji(cp: int) -> bool:
    return cp >= 0x1F000 or 0x2600 <= cp <= 0x27BF or 0x2B00 <= cp <= 0x2BFF


class _Fonts:
    """Fonts at one size, and which of them draws each character."""

    def __init__(self, pil: Any, px: int, bold: bool) -> None:
        self.ImageFont = pil.ImageFont
        weight = "bold" if bold else "regular"
        self.main = pil.ImageFont.truetype(str(FONTS[weight]), px)
        self.symbol = pil.ImageFont.truetype(str(FONTS[f"symbol_{weight}"]), px)
        self.px = px
        self.has_main = set(metrics._table()[1][weight])
        self._tofu = self._mask(self.symbol, "\ue000")  # private use: no glyph

    @staticmethod
    def _mask(font: Any, ch: str) -> Tuple[float, bytes]:
        return font.getlength(ch), bytes(font.getmask(ch))

    def font_for(self, ch: str) -> str:
        cp = ord(ch)
        if cp in self.has_main or cp < 0x2190:
            return "main"
        if self._mask(self.symbol, ch) != self._tofu:
            return "symbol"
        return "emoji" if _is_emoji(cp) else "symbol"


def _runs(fonts: _Fonts, s: str) -> List[Tuple[str, str]]:
    """``s`` split into ``(font, text)`` runs; zero-width characters drop."""
    out: List[Tuple[str, str]] = []
    joined = False
    for ch in s:
        cp = ord(ch)
        if joined or any(lo <= cp <= hi for lo, hi in metrics._ZERO_WIDTH):
            joined = cp == metrics._ZWJ
            continue
        which = fonts.font_for(ch)
        if out and out[-1][0] == which and which != "emoji":
            out[-1] = (which, out[-1][1] + ch)
        else:
            out.append((which, ch))
    return out


@lru_cache(maxsize=256)
def _emoji_tile(ch: str, px: int) -> Any:
    from PIL import Image, ImageDraw, ImageFont

    font = ImageFont.truetype(str(FONTS["emoji"]), EMOJI_STRIKE)
    # The glyph's full em box, not its ink: a ➖ stays a bar, not a square.
    width = max(1, int(round(font.getlength(ch))))
    tile = Image.new("RGBA", (width, int(EMOJI_STRIKE * 1.2)), (0, 0, 0, 0))
    ImageDraw.Draw(tile).text((0, 0), ch, font=font, embedded_color=True)
    scale = px * 1.15 / tile.height
    size = (max(1, int(round(tile.width * scale))), int(round(px * 1.15)))
    return tile.resize(size, Image.LANCZOS)


def _width(fonts: _Fonts, s: str) -> float:
    w = 0.0
    for which, run in _runs(fonts, s):
        if which == "emoji":
            w += round(fonts.px * metrics.EMOJI_EM)
        else:
            w += getattr(fonts, which).getlength(run)
    return w


def _draw_text(
    img: Any, draw: Any, fonts: _Fonts, xy: Tuple[float, float], s: str, fill: RGB
) -> None:
    """``s`` with its top-left at ``xy`` (px), font by font."""
    x, y = xy
    for which, run in _runs(fonts, s):
        if which == "emoji":
            tile = _emoji_tile(run, fonts.px)
            ty = int(round(y + (fonts.px * 1.15 - tile.height) / 2))
            img.paste(tile, (int(round(x)), ty), tile)
            x += round(fonts.px * metrics.EMOJI_EM)
            continue
        font = getattr(fonts, which)
        draw.text((int(round(x)), int(round(y))), run, font=font, fill=fill)
        x += font.getlength(run)


def _wrap(fonts: _Fonts, s: str, width: float) -> List[str]:
    lines: List[str] = []
    line = ""
    for word in s.split(" "):
        candidate = f"{line} {word}" if line else word
        if line and _width(fonts, candidate) > width:
            lines.append(line)
            line = word
        else:
            line = candidate
    lines.append(line)
    return lines


def render(screen: Screen, violations: Optional[List[Violation]] = None) -> bytes:
    """``screen`` as a PNG, 780 px wide (390 pt at 2x). Same input, same
    bytes: no clock, no network, fixed fonts."""
    import PIL.Image
    import PIL.ImageDraw
    import PIL.ImageFont

    if violations is None:
        violations = check(screen)
    rows = layout(screen, violations)

    text_fonts = _Fonts(PIL, _px(metrics.TEXT_FONT_PX), bold=False)
    button_fonts = _Fonts(PIL, _px(metrics.BUTTON_FONT_PX), bold=True)
    caption_fonts = _Fonts(PIL, _px(CAPTION_PX), bold=False)
    note_fonts = _Fonts(PIL, _px(NOTE_PX), bold=False)

    caption = screen.id + (f" — goal: {screen.goal}" if screen.goal else "")
    captions = _wrap(caption_fonts, caption, _px(metrics.PHONE_W - 2 * 20))
    top = CAPTION_TOP + len(captions) * (CAPTION_PX + 4) + 6

    lines = metrics.wrapped_lines(screen.text, screen.parse_mode)
    bubble_h = 2 * metrics.BUBBLE_PAD + len(lines) * metrics.LINE_H
    keyboard_top = top + bubble_h + metrics.GAP
    keyboard_h = len(rows) * (metrics.BUTTON_H + metrics.GAP)

    notes: List[str] = []
    for v in violations:
        notes.extend(_wrap(note_fonts, f"✗ {v.code}: {v.where} — {v.detail}", _px(350)))
    notes_top = keyboard_top + keyboard_h + 6
    height = notes_top + len(notes) * (NOTE_PX + 5) + 14

    width_px = _px(metrics.PHONE_W)
    img = PIL.Image.new("RGB", (width_px, _px(height)), CHAT_BG)
    draw = PIL.ImageDraw.Draw(img)

    for i, line in enumerate(captions):
        xy = (_px(20), _px(CAPTION_TOP + i * (CAPTION_PX + 4)))
        _draw_text(img, draw, caption_fonts, xy, line, CAPTION)

    bubble = (
        _px(MARGIN),
        _px(top),
        _px(MARGIN + metrics.KEYBOARD_W) - 1,
        _px(top + bubble_h) - 1,
    )
    bubble_bad = any(v.where == "message" for v in violations)
    draw.rounded_rectangle(
        bubble,
        radius=_px(RADIUS * 1.5),
        fill=BUBBLE_BG,
        outline=RED if bubble_bad else BUBBLE_BG,
        width=_px(FLAG_W),
    )
    for i, line in enumerate(lines):
        y = top + metrics.BUBBLE_PAD + i * metrics.LINE_H + 2
        xy = (_px(MARGIN + metrics.BUBBLE_PAD), _px(y))
        _draw_text(img, draw, text_fonts, xy, line, TEXT)

    for row in rows:
        for b in row:
            box = (
                _px(b.x),
                _px(keyboard_top + b.y),
                _px(b.x + b.w) - 1,
                _px(keyboard_top + b.y + b.h) - 1,
            )
            draw.rounded_rectangle(
                box,
                radius=_px(RADIUS),
                fill=BUTTON_BG,
                outline=b.outline,
                width=_px(FLAG_W) if b.flagged else 1,
            )
            label_w = _width(button_fonts, b.label)
            tx = _px(b.x + b.w / 2) - label_w / 2
            ty = _px(keyboard_top + b.y + (b.h - metrics.BUTTON_FONT_PX * 1.15) / 2)
            _draw_text(img, draw, button_fonts, (tx, ty), b.label, BUTTON_TEXT)

    for i, note in enumerate(notes):
        xy = (_px(20), _px(notes_top + i * (NOTE_PX + 5)))
        _draw_text(img, draw, note_fonts, xy, note, RED)

    out = BytesIO()
    img.save(out, format="PNG", optimize=False)
    return out.getvalue()
