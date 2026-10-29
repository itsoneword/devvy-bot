"""The mechanical UI gate: what a phone shows wrong, without a human looking.

``check(screen)`` returns one ``Violation`` per problem; an empty list is a
pass. Codes:

* ``truncated``  — a label wider than its button (Telegram shows "Clau…able");
* ``duplicate``  — two buttons whose *visible* text is the same (a bare
  −/+ stepper is exempt: the label row above it says what it steps,
  dv-f6af Настройки "Параллельно задач: 5" then [−] [+]);
* ``exclusive``  — two actions that cannot both apply shown together
  (Login + Logout): show the one the state allows (df11ac2's /model);
* ``row_width``  — more than ``MAX_ROW`` buttons in one row;
* ``too_long``   — text plus keyboard taller than one phone screen;
* ``slow``       — the handler took over ``SLOW_MS`` (only measured screens).
"""

import re
from dataclasses import dataclass
from typing import Dict, List, Tuple

from . import metrics
from .model import Button, Screen

#: Actions where showing both means one of them is wrong for the state.
#: "Switch to X" pairs and −/+ steppers are not here on purpose.
EXCLUSIVE_PAIRS = frozenset(
    frozenset(p)
    for p in (
        ("login", "logout"),
        ("signin", "signout"),
        ("freeze", "thaw"),
        ("on", "off"),
        ("enable", "disable"),
        ("start", "stop"),
        ("show", "hide"),
        ("pause", "resume"),
        ("lock", "unlock"),
        ("mute", "unmute"),
        ("subscribe", "unsubscribe"),
    )
)

#: Bare step glyphs: repeated on purpose, one pair per labelled setting.
STEPPERS = frozenset(("−", "-", "+", "➖", "➕"))


@dataclass(frozen=True)
class Violation:
    code: str
    where: str
    detail: str


def _where(r: int, c: int, button: Button) -> str:
    return f"row {r + 1} button {c + 1} {button.text!r}"


def _tokens(button: Button) -> Tuple[str, ...]:
    """What the button does, as words: its callback_data, else its label."""
    source = button.callback_data or button.text
    return tuple(t for t in re.split(r"[^0-9a-zа-яё]+", source.lower()) if t)


def _exclusive(a: Button, b: Button) -> bool:
    ta, tb = _tokens(a), _tokens(b)
    if len(ta) != len(tb):
        return False
    diff = [(x, y) for x, y in zip(ta, tb) if x != y]
    return len(diff) == 1 and frozenset(diff[0]) in EXCLUSIVE_PAIRS


def check(screen: Screen) -> List[Violation]:
    out: List[Violation] = []
    placed = [
        (r, c, b, len(row))
        for r, row in enumerate(screen.rows)
        for c, b in enumerate(row)
    ]

    for r, row in enumerate(screen.rows):
        if len(row) > metrics.MAX_ROW:
            out.append(
                Violation(
                    "row_width",
                    f"row {r + 1}",
                    f"{len(row)} buttons, at most {metrics.MAX_ROW}",
                )
            )

    seen: Dict[str, Tuple[int, int, Button]] = {}
    for r, c, b, n in placed:
        shown = metrics.visible_text(b.text, n)
        if shown != b.text:
            out.append(
                Violation(
                    "truncated",
                    _where(r, c, b),
                    f"shown as {shown!r} ({metrics.text_width(b.text):.0f} px "
                    f"> {metrics.label_space(n):.0f} px at {n} per row)",
                )
            )
        if shown in STEPPERS:
            continue
        first = seen.get(shown)
        if first is not None:
            out.append(
                Violation(
                    "duplicate",
                    _where(r, c, b),
                    f"shows {shown!r}, like {_where(*first)}",
                )
            )
        else:
            seen[shown] = (r, c, b)

    for i, (r1, c1, a, _) in enumerate(placed):
        for r2, c2, b, _ in placed[i + 1 :]:
            if _exclusive(a, b):
                out.append(
                    Violation(
                        "exclusive",
                        _where(r2, c2, b),
                        f"shown with {_where(r1, c1, a)}: only one applies",
                    )
                )

    lines = metrics.wrapped_lines(screen.text, screen.parse_mode)
    height = metrics.screen_height(len(lines), len(screen.rows))
    if height > metrics.SCREEN_BUDGET_H:
        out.append(
            Violation(
                "too_long",
                "message",
                f"{len(lines)} lines + {len(screen.rows)} rows = {height} px "
                f"> {metrics.SCREEN_BUDGET_H} px",
            )
        )

    if screen.render_ms is not None and screen.render_ms > metrics.SLOW_MS:
        out.append(
            Violation(
                "slow",
                "handler",
                f"{screen.render_ms:.0f} ms > {metrics.SLOW_MS:.0f} ms",
            )
        )
    return out
