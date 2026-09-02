"""Render a TodayState as the <today_state> block.

Hard-capped at ``MAX_CHARS``: this block rides on every turn, so an
unbounded digest would be an unbounded per-turn cost. Values stay in the
user's own language (Russian) — the digest is read by the coach persona,
which replies in that language.
"""

from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from .source import TodayState

MAX_CHARS = 1400

_WEEKDAY_RU = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]

HEADER_NOTE = (
    "Deterministic snapshot computed from the logs at turn start. Supersedes "
    "any earlier today_state block and anything remembered in-conversation "
    "about today. For anything beyond these numbers, call the data tools."
)


def _num(value: Optional[float]) -> str:
    """Render a number without a pointless trailing .0."""
    if value is None:
        return "—"
    if float(value).is_integer():
        return str(int(value))
    return f"{float(value):.1f}"


def _plural_ru(count: int, one: str, few: str, many: str) -> str:
    """Russian count agreement: 1 приём, 2-4 приёма, 5+ приёмов (11-14 take
    the 'many' form despite ending in 1-4)."""
    if 11 <= count % 100 <= 14:
        return many
    remainder = count % 10
    if remainder == 1:
        return one
    if 2 <= remainder <= 4:
        return few
    return many


def _food_line(state: TodayState) -> str:
    if not state.food_meals:
        return "- Еда сегодня: ничего не записано"
    meals = state.food_meals
    parts = [
        f"{meals} " + _plural_ru(meals, "приём", "приёма", "приёмов"),
        f"{_num(state.food_kcal)} ккал",
        f"{_num(state.food_protein)} г белка",
    ]
    line = "- Еда сегодня: " + ", ".join(parts)
    if state.last_meal:
        when = f" {state.last_meal_time}" if state.last_meal_time else ""
        line += f" (последний: {state.last_meal}{when})"
    return line


def _sleep_line(state: TodayState) -> str:
    sleep = state.sleep or {}
    fields = [
        ("sleep_min", "мин"),
        ("score", "score"),
        ("hrv", "HRV"),
        ("rhr", "RHR"),
    ]
    shown: List[str] = []
    for key, label in fields:
        value = sleep.get(key)
        if value is None:
            continue
        shown.append(
            f"{_num(value)} {label}" if label == "мин" else f"{label} {_num(value)}"
        )
    if not shown:
        return "- Сон (ночь на сегодня): не записан"
    return "- Сон (ночь на сегодня): " + ", ".join(shown)


def _train_line(state: TodayState) -> str:
    if state.train_sets_today:
        session = f" ({state.train_session_today})" if state.train_session_today else ""
        today_part = f"{state.train_sets_today} подходов{session}"
    else:
        today_part = "не записана"
    line = f"- Тренировка сегодня: {today_part}"
    if state.last_train_date:
        session = f" {state.last_train_session}" if state.last_train_session else ""
        line += f"  |  последняя до сегодня: {state.last_train_date}{session}"
    return line


def _activity_line(state: TodayState) -> str:
    if not state.activity_count_today:
        return "- Активность сегодня: не записана"
    types = ", ".join(state.activity_types_today)
    line = f"- Активность сегодня: {state.activity_count_today}"
    if types:
        line += f" ({types})"
    if state.activity_kcal_today is not None:
        line += f", {_num(state.activity_kcal_today)} ккал актив."
    return line


def render_digest(state: Optional[TodayState], now_local: datetime) -> str:
    """Build the block. Returns "" when there is no state to describe."""
    if state is None:
        return ""

    stamp = now_local.strftime("%Y-%m-%d %H:%M")
    zone = now_local.tzname() or ""
    weekday = _WEEKDAY_RU[now_local.weekday()]
    lines = [
        f'<today_state as_of="{stamp} {zone} ({weekday})">'.replace("  ", " "),
        HEADER_NOTE,
        _food_line(state),
        _sleep_line(state),
    ]
    if state.weight_kg is not None:
        date_part = f" ({state.weight_date})" if state.weight_date else ""
        lines.append(f"- Вес: {_num(state.weight_kg)} кг{date_part}")
    lines.append(_train_line(state))
    lines.append(_activity_line(state))

    planned: List[str] = []
    if state.planned_today:
        planned.append(f"сегодня {state.planned_today}")
    if state.planned_tomorrow:
        planned.append(f"завтра {state.planned_tomorrow}")
    if planned:
        lines.append("- По программе: " + "; ".join(planned))

    if state.active_restrictions:
        lines.append("- Активные ограничения: " + ", ".join(state.active_restrictions))

    lines.append("</today_state>")
    block = "\n".join(lines)
    if len(block) > MAX_CHARS:
        # Truncate the body, never the closing tag — an unclosed block would
        # bleed into the user's actual message.
        keep = MAX_CHARS - len("\n…\n</today_state>")
        block = block[:keep] + "\n…\n</today_state>"
    return block
