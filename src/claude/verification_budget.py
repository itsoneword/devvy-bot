"""Per-turn verification budget + the confirm/escalate ladder (dv-9f41).

Measured failure, both poles on the same coach profile: dv-7a3c wrote a full
leg programme with zero data reads; turn 1817 (2026-09-22) answered "write me
a chest session, check it isn't bullshit" with one research subagent, 17
WebSearch and 2 WebFetch calls — ~5x the month's mean cost, 129 s of silence.
Nothing in the system represented *how much* verification a turn deserves,
and persona text has already lost this fight three times (dv-1106, dv-7a3c,
dv-c388). So the dial is mechanical and lives in code:

1. **Draft** (every turn): search-class calls are counted per turn by the
   ``PreToolUse`` hook. Over ``draft_max`` the turn is logged
   (``verification_budget_exceeded``); with ``enforce`` on, further calls are
   denied with a message that names the ladder.
2. **Confirm** (follow-up turn, only when a load-bearing claim went out
   unchecked — ``needs_confirm_check``): ``WebSearch`` only, ``confirm_max``
   calls, no ``WebFetch`` page dumps, no research subagents.
3. **Escalate** (one more follow-up, only when the confirm reply reports a
   contradiction via ``CONTRADICTION_MARKER``): uncapped, scoped to the
   contradicted claim. Never a fourth turn.

The counter lives in the ``PreToolUse`` hook, not ``can_use_tool``: tools in
``allowed_tools`` (WebSearch is) never reach ``can_use_tool`` at all
(docs/FINDINGS.md 2026-08-02 / 2026-08-04).

The phase reaches the hook through a ``ContextVar`` rather than a new
``run_command`` parameter: the follow-up turns are awaited inside the
orchestrator's task, so a value set there is visible when the SDK manager
builds that turn's hook, without threading a kwarg through the facade, the
backend Protocol and the Codex backend that would ignore it.

This module is PURE apart from that ContextVar: no I/O, no storage, no SDK,
so the live hook and any offline replay call the same functions (same
discipline as ``recipe_retry.needs_search_retry``). The tool classification
(``tool_weight`` / ``is_grounded``) lives in ``llm_analytics.quality`` so the
daily counters and the live budget can never disagree on what counts.
"""

from __future__ import annotations

import contextlib
import re
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Iterator, Optional, Sequence

from ..llm_analytics.quality import (
    SUBAGENT_WEIGHT,
    is_grounded,
    looks_like_training_program,
    tool_weight,
)

PHASE_DRAFT = "draft"
PHASE_CONFIRM = "confirm"
PHASE_ESCALATE = "escalate"

_phase: ContextVar[str] = ContextVar("verification_phase", default=PHASE_DRAFT)


def current_phase() -> str:
    return _phase.get()


@contextlib.contextmanager
def verification_phase(phase: str) -> Iterator[None]:
    """Run the enclosed ``run_command`` under ``phase``'s budget."""
    token = _phase.set(phase)
    try:
        yield
    finally:
        _phase.reset(token)


@dataclass
class VerificationBudget:
    """Mutable per-turn counter. Build one per turn; never share across turns.

    ``limit`` None = uncapped (escalate phase). ``enforce`` False = count-only
    telemetry (the owner's 2026-10-13 answer: count first, deny once the
    counters show capping doesn't cost quality).
    """

    phase: str
    limit: Optional[int]
    enforce: bool = False
    used: int = 0
    exceeded_logged: bool = False

    def charge(self, tool_name: str) -> Optional[str]:
        """Account one tool call. Returns a deny reason, or None to allow.

        Count-only mode always returns None; the caller logs ``over`` itself.
        A denied call is not charged — it never ran.
        """
        weight = tool_weight(tool_name)
        if weight == 0:
            return None
        if self.phase == PHASE_CONFIRM and tool_name != "WebSearch":
            # The confirm step is snippets-only by definition; a WebFetch or a
            # subagent is how a cheap check turns into a literature review.
            if self.enforce:
                return (
                    f"{tool_name} is not available in the confirm step: use "
                    f"WebSearch result snippets only (at most {self.limit} "
                    f"searches) and compare them against your numbers."
                )
        if self.enforce and self.limit is not None and self.used + weight > self.limit:
            return (
                f"Verification budget for this turn is spent ({self.used} of "
                f"{self.limit}; a subagent costs {SUBAGENT_WEIGHT}). Answer "
                f"from what you have: name what you checked and what you did "
                f"not. Do not run a literature review — a follow-up check runs "
                f"automatically when a claim goes out unverified, and a deeper "
                f"one only if a source contradicts it."
            )
        self.used += weight
        return None

    @property
    def over(self) -> bool:
        return self.limit is not None and self.used > self.limit


def make_budget(
    phase: str,
    *,
    draft_max: int,
    confirm_max: int,
    enforce: bool,
) -> VerificationBudget:
    if phase == PHASE_CONFIRM:
        return VerificationBudget(phase, confirm_max, enforce)
    if phase == PHASE_ESCALATE:
        return VerificationBudget(phase, None, enforce)
    return VerificationBudget(PHASE_DRAFT, draft_max, enforce)


def needs_confirm_check(response: str, tool_names: Sequence[str]) -> bool:
    """True iff ``response`` prescribes a training programme (sets x reps,
    weights, %1RM, RPE) and the turn grounded it in nothing at all — the
    dv-7a3c shape. Recipes have their own path (``recipe_retry``)."""
    if not looks_like_training_program(response):
        return False
    return not is_grounded(tool_names)


# The confirm reply flags a contradiction by starting a line with this marker.
# Structured on purpose: the escalate trigger stays mechanical (a line match),
# not a judge's reading of free text.
CONTRADICTION_MARKER = "ПРОТИВОРЕЧИЕ:"
_CONTRADICTION_RE = re.compile(
    rf"(?m)^\W*{re.escape(CONTRADICTION_MARKER)}\s*(?P<claim>.+)$"
)


def contradicted_claim(confirm_response: str) -> Optional[str]:
    """The claim the confirm step found contradicted, or None (ship as is)."""
    match = _CONTRADICTION_RE.search(confirm_response)
    if match is None:
        return None
    return match.group("claim").strip()[:300] or None


def confirm_prompt(confirm_max: int) -> str:
    """Follow-up turn: a cheap check of the previous answer's numbers."""
    return (
        "Твой предыдущий ответ — программа тренировки с конкретными цифрами "
        "(подходы, повторы, веса, RPE), но в том ходе ты не проверил ни данные "
        "пользователя, ни один источник. Сделай короткую проверку: сначала "
        "посмотри историю пользователя (lift_history), затем, если цифры "
        f"нечем подтвердить, сделай не больше {confirm_max} запросов WebSearch "
        "и сравни сниппеты с цифрами программы. Без WebFetch, без субагентов, "
        "без обзора литературы — проверяются только цифры этой программы. "
        "Если всё сходится — скажи это одной фразой и назови, чем проверил. "
        "Если источник или история прямо противоречат цифре — начни отдельную "
        f"строку с «{CONTRADICTION_MARKER}» и назови эту цифру и источник, "
        "затем дай исправление."
    )


def escalate_prompt(claim: str) -> str:
    """Follow-up turn: deep check scoped to the one contradicted claim."""
    return (
        "Проверка нашла противоречие: "
        f"«{claim}». Разберись глубже только с этим утверждением — можно "
        "WebFetch и несколько поисков, но не пересматривай остальную "
        "программу. Итог: что верно, на каком источнике, и исправленная "
        "цифра. Коротко."
    )


CONFIRM_LABEL = "🔎 Проверка цифр:"
ESCALATE_LABEL = "🔬 Углублённая проверка спорного пункта:"
