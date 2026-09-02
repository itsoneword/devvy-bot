"""data.db-backed TodayStateSource (dv-5f3a landed; the CSVs are frozen).

All SQL lives in ``UserDataRepository`` — the single access seam — so this
module only composes repository calls into a TodayState.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import structlog

from ..storage.user_data import UserDataRepository
from .program import parse_weekly_split, planned_for
from .source import TodayState

logger = structlog.get_logger()


class DbTodayStateSource:
    """Reads today's state for one user out of data.db."""

    def __init__(self, repo: UserDataRepository) -> None:
        self._repo = repo

    async def has_entries_today(self, user_id: int, today: str) -> bool:
        return await self._repo.has_entries_on(user_id, today)

    async def get_today_state(self, user_id: int, today: str) -> Optional[TodayState]:
        from datetime import date as date_cls

        summary = await self._repo.day_summary(user_id, today)
        state = TodayState(date=today)

        totals: Dict[str, Any] = summary.get("food_totals") or {}
        state.food_meals = int(totals.get("meals") or 0)
        state.food_kcal = totals.get("kcal")
        state.food_protein = totals.get("protein")
        food_rows = summary.get("food") or []
        if food_rows:
            last = food_rows[-1]
            state.last_meal = last.get("meal")
            state.last_meal_time = last.get("time")

        state.sleep = summary.get("sleep")

        train_rows = summary.get("train") or []
        state.train_sets_today = len(train_rows)
        if train_rows:
            sessions = [r.get("session") for r in train_rows if r.get("session")]
            state.train_session_today = sessions[0] if sessions else None

        previous = await self._repo.last_train_before(user_id, today)
        if previous:
            state.last_train_date = previous.get("date")
            state.last_train_session = previous.get("session")

        activity_rows = summary.get("activity") or []
        state.activity_count_today = len(activity_rows)
        state.activity_types_today = [
            str(r["type"]) for r in activity_rows if r.get("type")
        ]
        kcal_values = [
            r["kcal_active"] for r in activity_rows if r.get("kcal_active") is not None
        ]
        if kcal_values:
            state.activity_kcal_today = sum(kcal_values)

        weight = await self._repo.latest_weight(user_id)
        if weight:
            state.weight_kg = weight.get("weight_kg")
            state.weight_date = weight.get("date")

        # Owner decision 4 (2026-08-15): ACTIVE only. Chronic conditions are
        # standing constraints and belong in the window bundle, not in every
        # single turn — repeating them per turn was the pollution the owner
        # explicitly rejected.
        issues = await self._repo.issue_list(user_id, status="ACTIVE")
        state.active_restrictions = [
            str(i.get("title")) for i in issues if i.get("title")
        ]

        programs = await self._repo.doc_list(user_id, kind="program")
        if programs:
            doc = await self._repo.doc_get(user_id, int(programs[-1]["id"]))
            mapping = parse_weekly_split(doc.get("content") if doc else None)
            try:
                weekday = date_cls.fromisoformat(today).weekday()
            except ValueError:
                weekday = None  # type: ignore[assignment]
            if weekday is not None:
                state.planned_today = planned_for(mapping, weekday)
                state.planned_tomorrow = planned_for(mapping, weekday + 1)

        # A user who tracks nothing at all (a guest, or someone who has never
        # logged) gets no block: "ничего не записано" on every line, every
        # turn, is noise that teaches the model nothing. An empty day for a
        # user who DOES track is different — that absence is informative, so
        # any history at all keeps the digest on.
        tracks_anything = any(
            (
                state.food_meals,
                state.sleep,
                state.train_sets_today,
                state.last_train_date,
                state.activity_count_today,
                state.weight_kg is not None,
                state.active_restrictions,
                state.planned_today,
                state.planned_tomorrow,
            )
        )
        return state if tracks_anything else None
