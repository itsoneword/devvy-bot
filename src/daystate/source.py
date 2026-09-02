"""TodayState — the shape the digest renders, and the seam that fills it."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol


@dataclass
class TodayState:
    """Everything the digest may show, all optional.

    Every field is nullable on purpose: a missing log is a normal state of
    the world, never an error. The renderer says "не записано" rather than
    omitting the line, so the model can tell "nothing logged" apart from
    "this digest does not cover that".
    """

    date: str
    food_meals: int = 0
    food_kcal: Optional[float] = None
    food_protein: Optional[float] = None
    last_meal: Optional[str] = None
    last_meal_time: Optional[str] = None
    sleep: Optional[Dict[str, Any]] = None
    weight_kg: Optional[float] = None
    weight_date: Optional[str] = None
    train_sets_today: int = 0
    train_session_today: Optional[str] = None
    last_train_date: Optional[str] = None
    last_train_session: Optional[str] = None
    activity_count_today: int = 0
    activity_types_today: List[str] = field(default_factory=list)
    activity_kcal_today: Optional[float] = None
    planned_today: Optional[str] = None
    planned_tomorrow: Optional[str] = None
    active_restrictions: List[str] = field(default_factory=list)


class TodayStateSource(Protocol):
    """The seam between the digest and whatever stores the logs.

    Kept as a protocol so the renderer never learns about SQL, and so tests
    can supply a plain object instead of a database.
    """

    async def get_today_state(self, user_id: int, today: str) -> Optional[TodayState]:
        """State for ``today``, or None when this user has no data at all."""

    async def has_entries_today(self, user_id: int, today: str) -> bool:
        """True when any log holds a row for ``today`` (gap-ping criterion)."""
