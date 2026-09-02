"""Deterministic today-state digest (dv-df45).

The model forgets in-day state — what was eaten today, that today is a
training day — because that state lives only in scrollback. This package
computes it from the logs at turn start and injects it as a small block on
the user message, so it cannot drift or go stale and survives a window roll.

Owner principle (2026-08-14): anything capturable deterministically must
never depend on the model remembering to do it.
"""

from .digest import render_digest
from .source import TodayState, TodayStateSource

__all__ = ["TodayState", "TodayStateSource", "render_digest"]
