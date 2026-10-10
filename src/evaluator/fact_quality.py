"""Daily fact-quality check: what got saved in the last 24h, and is it junk?

dv-404f, follow-up to dv-7517. The episodic guard and the extraction prompt's
durability rules stop most junk *before* it is written; neither is a guarantee,
so this pass keeps the residue visible instead of letting it accumulate
silently until the next manual audit.

**Zero writes to memory_facts.** Report-only, by construction: the whole module
issues one SELECT and one CLI call per scope. Closing, editing or flipping a
fact stays an owner-approved action via ``scripts/cleanup_episodic.py`` — a
wrong automatic close is invisible and unrecoverable (same rule as the
consolidation pass, dv-4c6f).

Two independent opinions per new fact, so a disagreement is itself signal:

1. :func:`src.evaluator.episodic_guard.classify` — deterministic, free, the
   exact function that runs on the write path. A fact it flags here is one it
   did NOT flag at write time (different tier, or written by the ``remember``
   tool which the guard does not gate).
2. One cheap Claude CLI call per scope (haiku by default, the reconciler's
   model) judging each fact durable / episodic / malformed. A judge outage
   degrades to guard-only — it never fails the run.

The journal lines this emits (``fact_quality_verdict``) are the labeled corpus
for the next round of extraction-prompt and guard tuning.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Dict, List, Optional

import structlog

from src.model_router import complete_text

from ..storage.memory_repository import MemoryRepository
from .episodic_guard import classify as classify_episodic

if TYPE_CHECKING:
    from ..storage.database import DatabaseManager
    from ..storage.memory_repository import MemoryFact

logger = structlog.get_logger()

# The reporting window. A daily job with a 24h window double-reports nothing
# and misses nothing as long as the cron fires once a day; a missed run leaves
# a gap rather than duplicating, which is the safe direction for a report.
WINDOW_HOURS = 24

# Per-scope ceiling on the judge prompt. A scope that saved more than this in
# one day has a bigger problem than fact quality, and the excess is still
# guard-classified and counted — only the LLM opinion is skipped.
MAX_JUDGED_FACTS = 60

# Per-fact content ceiling inside the judge prompt, matching
# MAX_INJECTABLE_FACT_CHARS (dv-7517 step 5) so an ordinary user_fact is never
# cut — only long conversation_result bodies are. Measured 2026-08-15 on the
# live scope: truncating at 400 with no marker made haiku call two perfectly
# good facts "malformed — truncated mid-sentence". Anything cut is now marked
# and the prompt says what the marker means.
MAX_JUDGE_CHARS = 600
TRUNCATION_MARKER = " …[shortened for this review]"

# How many offending facts to spell out in the Telegram message per scope.
# The rest are in the journal; a report nobody reads is not a report.
MAX_DETAIL_LINES = 5

VALID_VERDICTS = ("durable", "episodic", "malformed")

JUDGE_SYSTEM_PROMPT = (
    "You judge whether saved memory facts are durable. Reply with JSON only, "
    "exactly in the format requested."
)

JUDGE_PROMPT = """\
Below are memory facts saved for one agent in the last day, as
`id | kind | content` lines. Judge each one independently:

- "durable" — still true and useful weeks from now: preferences, allergies,
  injuries and medical conditions, goals and targets, standing protocols or
  programs, biographical facts, product references. A repeated pattern stated
  AS a pattern ("usually skips breakfast") is durable.
- "episodic" — a dated one-off event: one meal (with or without kcal), one
  night's sleep, one session's actual sets and reps, one weigh-in, one day's
  summary. These belong in logs, never in memory.
- "malformed" — unusable as a fact: an instruction to the assistant rather
  than a fact about the user, a raw data dump, or empty of meaning.

Two rules about the text itself, both learned from real misjudgements:

- A line ending in "{marker}" was shortened for this review only. Judge it on
  what is shown; never call it malformed for stopping mid-sentence.
- kind `conversation_result` is a stored piece of work (research, an analysis)
  that is injected only as a one-line pointer, so length is expected and not a
  defect. Judge it episodic only when it records a one-off event.

When a fact is genuinely borderline, answer "durable" — a wrongly kept fact
costs a line of context, a wrongly flagged one costs the owner's trust in this
report.

Return ONLY a JSON array, one entry per fact:
[{{"id": 12, "verdict": "episodic", "why": "single meal with kcal"}}]
`why` is at most 12 words. Include every id exactly once.

## Facts
{facts}
"""


@dataclass
class FactVerdict:
    """Both opinions about one newly-saved fact."""

    fact_id: int
    scope: str
    tier: str
    content: str
    kind: str = "user_fact"
    guard_reason: Optional[str] = None
    judge_verdict: Optional[str] = None
    judge_why: str = ""

    @property
    def is_suspect(self) -> bool:
        """Either opinion says junk. Deliberately OR, not AND: this report
        exists to surface candidates for the owner, not to auto-close them."""
        return self.guard_reason is not None or self.judge_verdict in (
            "episodic",
            "malformed",
        )

    @property
    def reason(self) -> str:
        parts = []
        if self.guard_reason:
            parts.append(f"guard: {self.guard_reason}")
        if self.judge_verdict in ("episodic", "malformed"):
            why = f" ({self.judge_why})" if self.judge_why else ""
            parts.append(f"judge: {self.judge_verdict}{why}")
        return "; ".join(parts)


@dataclass
class ScopeReport:
    """One agent scope's slice of the daily check."""

    scope: str
    verdicts: List[FactVerdict] = field(default_factory=list)
    # 'ok' | 'skipped' (nothing to judge) | 'failed: ...' (guard-only run)
    judge_status: str = "skipped"

    @property
    def total(self) -> int:
        return len(self.verdicts)

    @property
    def suspects(self) -> List[FactVerdict]:
        return [v for v in self.verdicts if v.is_suspect]


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _judge_excerpt(content: str) -> str:
    """One listing line per fact: newlines collapsed (they would break the
    `id | kind | content` framing) and long bodies cut with a marker the
    prompt explains, so a cut is never read as a truncated fact."""
    flat = " ".join(content.split())
    if len(flat) <= MAX_JUDGE_CHARS:
        return flat
    return flat[:MAX_JUDGE_CHARS].rstrip() + TRUNCATION_MARKER


def format_report(reports: List[ScopeReport]) -> str:
    """The owner-facing message. Summary lines first, then the offenders."""
    lines = ["Fact check (last 24h):"]
    detail: List[str] = []
    for report in reports:
        suspects = report.suspects
        if suspects:
            ids = ", ".join(str(v.fact_id) for v in suspects)
            lines.append(
                f"{report.scope}: {_plural(report.total, 'new fact')}, "
                f"{len(suspects)} look like trash (ids {ids})"
            )
        else:
            lines.append(
                f"{report.scope}: {_plural(report.total, 'new fact')}, "
                "all look durable"
            )
        if report.judge_status.startswith("failed"):
            lines.append(f"  (judge unavailable — guard only: {report.judge_status})")
        for verdict in suspects[:MAX_DETAIL_LINES]:
            excerpt = verdict.content.strip().replace("\n", " ")
            if len(excerpt) > 120:
                excerpt = excerpt[:119] + "…"
            detail.append(f"  {verdict.fact_id} [{verdict.reason}] {excerpt}")
        if len(suspects) > MAX_DETAIL_LINES:
            detail.append(f"  … {len(suspects) - MAX_DETAIL_LINES} more in the journal")
    if detail:
        lines.append("")
        lines.extend(detail)
        lines.append("")
        lines.append("Report only — nothing was closed or changed.")
    return "\n".join(lines)


class FactQualityCheck:
    """Daily guard+judge review of newly-saved facts. Never writes."""

    def __init__(
        self,
        db: "DatabaseManager",
        notify_callback: Optional[Callable[[str], Awaitable[None]]] = None,
        model: str = "haiku",
        window_hours: int = WINDOW_HOURS,
    ) -> None:
        self._db = db
        self._memory_repo = MemoryRepository(db)
        self._notify = notify_callback  # async fn(message)
        self._model = model
        self._window_hours = window_hours

    async def run(self, scope: Optional[str] = None) -> List[ScopeReport]:
        """Check facts saved in the window (one scope, or all of them).

        Returns the per-scope reports. Nothing new anywhere = no Telegram
        message: a daily job that pings "0 new facts" every morning trains the
        owner to ignore it.
        """
        since = datetime.now(UTC) - timedelta(hours=self._window_hours)
        facts = await self._new_facts(since, scope)
        if not facts:
            logger.info(
                "Fact quality check: no new facts",
                window_hours=self._window_hours,
                scope=scope,
            )
            return []

        by_scope: Dict[str, List["MemoryFact"]] = {}
        for fact in facts:
            by_scope.setdefault(fact.agent_id, []).append(fact)

        reports = [
            await self._scope_report(sc, rows) for sc, rows in sorted(by_scope.items())
        ]

        total_suspect = sum(len(r.suspects) for r in reports)
        logger.info(
            "Fact quality check complete",
            scopes=len(reports),
            new_facts=len(facts),
            suspect=total_suspect,
        )
        if self._notify:
            try:
                await self._notify(format_report(reports))
            except Exception as e:  # notification failure is not critical
                logger.warning("Fact quality report notification failed", error=str(e))
        return reports

    async def _scope_report(self, scope: str, facts: List["MemoryFact"]) -> ScopeReport:
        report = ScopeReport(scope=scope)
        for fact in facts:
            report.verdicts.append(
                FactVerdict(
                    fact_id=fact.id,
                    scope=scope,
                    tier=fact.tier,
                    content=fact.content,
                    kind=fact.kind,
                    guard_reason=classify_episodic(fact.content, tier=fact.tier),
                )
            )

        judged, report.judge_status = await self._judge(scope, report.verdicts)
        for verdict in report.verdicts:
            judgement = judged.get(verdict.fact_id)
            if judgement:
                verdict.judge_verdict, verdict.judge_why = judgement
            if verdict.is_suspect:
                # The labeled corpus for the next prompt/guard iteration
                # (dv-404f): full content, both opinions, one line per fact.
                logger.warning(
                    "fact_quality_verdict",
                    scope=scope,
                    fact_id=verdict.fact_id,
                    tier=verdict.tier,
                    guard_reason=verdict.guard_reason,
                    judge_verdict=verdict.judge_verdict,
                    judge_why=verdict.judge_why,
                    content=verdict.content,
                )
        return report

    async def _new_facts(
        self, since: datetime, scope: Optional[str]
    ) -> List["MemoryFact"]:
        """Rows created in the window. Read-only, every scope by default.

        ``created_at`` is CURRENT_TIMESTAMP: space-separated naive UTC. The
        bound must be formatted the same way or same-day rows compare wrong
        ('T' sorts after ' ') — the trap already documented in
        ``get_agent_tool_facts``.
        """
        bound = since.astimezone(UTC).replace(tzinfo=None).isoformat(sep=" ")
        return await self._memory_repo.new_facts_since(bound, agent_id=scope)

    async def _judge(
        self, scope: str, verdicts: List[FactVerdict]
    ) -> tuple[Dict[int, tuple[str, str]], str]:
        """One cheap CLI call per scope. Never raises.

        Returns ``(judgements by fact id, status)``. Any failure — transport,
        timeout, unparseable JSON — returns an empty mapping with a 'failed:'
        status: the guard's verdicts still stand and the report still goes out
        (with the outage stated, so a silent judge is not mistaken for a clean
        day).
        """
        batch = verdicts[:MAX_JUDGED_FACTS]
        if not batch:
            return {}, "skipped"
        listing = "\n".join(
            f"{v.fact_id} | {v.kind} | {_judge_excerpt(v.content)}" for v in batch
        )
        try:
            raw = await complete_text(
                JUDGE_PROMPT.format(facts=listing, marker=TRUNCATION_MARKER.strip()),
                purpose="fact_quality",
                model=self._model,
                system_prompt=JUDGE_SYSTEM_PROMPT,
            )
            return self._parse_judgements(raw, {v.fact_id for v in batch}), "ok"
        except Exception as e:
            logger.warning(
                "Fact quality judge failed; guard-only report",
                scope=scope,
                error=str(e),
            )
            return {}, f"failed: {str(e)[:120]}"

    @staticmethod
    def _parse_judgements(raw: str, known_ids: set[int]) -> Dict[int, tuple[str, str]]:
        """Parse the judge's JSON. Unknown ids and unknown verdicts are
        dropped rather than guessed — an unparsed entry means "no LLM opinion",
        which reads as durable and errs toward keeping the fact."""
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        data = json.loads(cleaned)
        if isinstance(data, dict) and "verdicts" in data:
            data = data["verdicts"]
        if not isinstance(data, list):
            raise ValueError("Expected JSON array of judgements")

        judged: Dict[int, tuple[str, str]] = {}
        for entry in data:
            if not isinstance(entry, dict):
                continue
            fact_id = entry.get("id")
            verdict = entry.get("verdict")
            if not isinstance(fact_id, int) or fact_id not in known_ids:
                logger.warning("Fact quality judgement with bad id", entry=entry)
                continue
            if verdict not in VALID_VERDICTS:
                logger.warning("Fact quality judgement unknown verdict", entry=entry)
                continue
            why = entry.get("why")
            judged[fact_id] = (str(verdict), str(why or "")[:120])
        return judged


def register_daily_check(
    scheduler: Any,
    db: "DatabaseManager",
    enabled: bool,
    cron_expression: str,
    notify_callback: Optional[Callable[[str], Awaitable[None]]] = None,
    model: str = "haiku",
) -> Optional[FactQualityCheck]:
    """Register the daily check as a native scheduler job when enabled.

    Same mechanism as the weekly consolidation pass (``add_native_job``: a
    plain async callable, re-registered every boot, not persisted). The flag
    check lives here rather than in ``main.py`` so "off means never
    registered" is a testable property and not a line of boot code.

    Returns the service (so callers can trigger a run by hand) or None when
    the flag is off.
    """
    if not enabled:
        logger.info("Daily fact-quality check disabled", cron=cron_expression)
        return None

    service = FactQualityCheck(db=db, notify_callback=notify_callback, model=model)

    async def _daily_fact_quality() -> None:
        try:
            await service.run()
        except Exception as e:
            logger.error("Daily fact-quality check failed", error=str(e))

    scheduler.add_native_job("fact-quality-check", cron_expression, _daily_fact_quality)
    return service
