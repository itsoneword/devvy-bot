"""Phone geometry and Telegram's button truncation (dv-4cd8).

Calibrated on what the owner's phone actually showed (analysis dv-4cd8 §4),
measured in Liberation Sans (Arial metrics) Bold 15 px:

* three to a row, "Claude Fable" (93 px) truncated and "✓ Sonnet" fit, so
  the label space is between 66 and 93 px;
* two to a row, "Codex GPT-5.6-Luna" (147 px) truncated and "✓ GPT 5.6
  Terra" (112 px) fit, so it is between 112 and 147 px.

Both hold for a keyboard 270-340 px wide; ``KEYBOARD_W`` = 310 gives 79 px
at three and 132 px at two. Move it inside that window only if a fixture
disagrees, and keep the three acceptance fixtures right.

Telegram shortens a label in the *middle* ("Clau…able"), so two labels that
differ only in the middle look the same on the phone: ``visible_text`` is
what the duplicate check compares.
"""

import html
import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Optional, Tuple

PHONE_W = 390
KEYBOARD_W = 310
GAP = 6  # between buttons in a row, and between rows
PAD = 10  # left and right inside a button
BUTTON_FONT_PX = 15
BUTTON_H = 42
TEXT_FONT_PX = 16
TEXT_W = 290  # text width inside the message bubble
LINE_H = 21
BUBBLE_PAD = 10
#: The chat area of a 390×844 phone under the header and above the input.
SCREEN_BUDGET_H = 600
MAX_ROW = 4
SLOW_MS = 2000.0

ELLIPSIS = "…"
#: A symbol or emoji the width table lacks (✓ ➖ 💸 …) is drawn from an emoji
#: font, wider than a letter.
EMOJI_EM = 1.25
OTHER_EM = 0.6
_ZERO_WIDTH = (
    (0x200B, 0x200F),  # zero-width space/joiners, marks
    (0xFE00, 0xFE0F),  # variation selectors
    (0x1F3FB, 0x1F3FF),  # skin tones
    (0xE0020, 0xE007F),  # tag sequences
)
_ZWJ = 0x200D
_WIDTHS = Path(__file__).with_name("widths.json")


@lru_cache(maxsize=1)
def _table() -> Tuple[int, Dict[str, Dict[int, int]]]:
    data = json.loads(_WIDTHS.read_text(encoding="utf-8"))
    fonts = {
        name: {int(cp): int(w) for cp, w in widths.items()}
        for name, widths in data["fonts"].items()
    }
    return int(data["units_per_em"]), fonts


def text_width(s: str, px: float = BUTTON_FONT_PX, bold: bool = True) -> float:
    """Width of ``s`` in px on one line (no kerning)."""
    upm, fonts = _table()
    widths = fonts["bold" if bold else "regular"]
    em = 0.0
    joined = False
    for ch in s:
        cp = ord(ch)
        if joined or any(lo <= cp <= hi for lo, hi in _ZERO_WIDTH):
            # The emoji after a ZWJ draws into the one before it.
            joined = cp == _ZWJ
            continue
        w = widths.get(cp)
        if w is not None:
            em += w / upm
        else:
            em += EMOJI_EM if cp >= 0x2190 else OTHER_EM
    return em * px


def label_space(n_in_row: int) -> float:
    """Px a label may use in a row of ``n_in_row`` buttons."""
    n = max(1, n_in_row)
    return (KEYBOARD_W - (n - 1) * GAP) / n - 2 * PAD


def is_truncated(label: str, n_in_row: int) -> bool:
    return text_width(label) > label_space(n_in_row)


def visible_text(label: str, n_in_row: int) -> str:
    """``label`` as Telegram shows it ``n_in_row`` to a row: whole, or head
    + "…" + tail sized to fit (middle ellipsis)."""
    space = label_space(n_in_row)
    if text_width(label) <= space:
        return label
    budget = space - text_width(ELLIPSIS)
    # Grow whichever side is narrower, a character at a time, until the next
    # character fits on neither: head and tail end up about even.
    i, j = 0, len(label)
    while i < j:
        head_w, tail_w = text_width(label[:i]), text_width(label[j:])
        grow_head = head_w <= tail_w
        nxt = text_width(label[i]) if grow_head else text_width(label[j - 1])
        if head_w + tail_w + nxt > budget:
            other = text_width(label[j - 1]) if grow_head else text_width(label[i])
            if head_w + tail_w + other > budget:
                break
            grow_head = not grow_head
        if grow_head:
            i += 1
        else:
            j -= 1
    head, tail = label[:i], label[j:]
    return f"{head.rstrip()}{ELLIPSIS}{tail.lstrip()}"


def plain_text(text: str, parse_mode: Optional[str]) -> str:
    """The message text as shown: HTML tags dropped, entities unescaped."""
    if (parse_mode or "").upper() == "HTML":
        text = re.sub(r"<br\s*/?>", "\n", text)
        text = html.unescape(re.sub(r"<[^>]+>", "", text))
    return text


def wrapped_lines(text: str, parse_mode: Optional[str] = None) -> List[str]:
    """The message as the bubble wraps it at ``TEXT_W`` (greedy, by word;
    a word wider than a line breaks by character)."""
    lines: List[str] = []
    for para in plain_text(text, parse_mode).split("\n"):
        line = ""
        for word in para.split(" "):
            candidate = f"{line} {word}" if line else word
            if text_width(candidate, TEXT_FONT_PX, bold=False) <= TEXT_W:
                line = candidate
                continue
            if line:
                lines.append(line)
            line = ""
            for ch in word:
                if text_width(line + ch, TEXT_FONT_PX, bold=False) > TEXT_W and line:
                    lines.append(line)
                    line = ""
                line += ch
        lines.append(line)
    return lines


def screen_height(n_lines: int, n_rows: int) -> int:
    """Px from the top of the bubble to the bottom of the keyboard."""
    return 2 * BUBBLE_PAD + n_lines * LINE_H + n_rows * (BUTTON_H + GAP)
