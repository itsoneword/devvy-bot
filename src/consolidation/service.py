"""Stale-fact sweep: close facts a newer fact on the same subject replaced.

The per-message evaluator stays conservative (when unsure, keep both facts),
and the in-session ``remember`` tool has no reconciler at all, so status
updates pile up next to the facts they replaced ("concurrency 3" next to
"raised to 5"). This pass is the pressure valve (dv-4c6f, Letta sleep-time
pattern; reshaped by dv-cf07):

- **when:** after every window rotation (``after_rotation``: the outgoing
  window's final extraction, then the sweep; the new window's bundle waits
  for it, bounded), on the bundle budget valve (``request_run``) and weekly
  over every scope (``run``).
- **how:** one ``complete_text(purpose="consolidation")`` call per scope
  proposes ``[{old_id, newer_id, reason}]``; every pair is validated in code
  before anything is closed (general tier only, never a ``conflict_group``
  row, both ids active in the scope, newer really newer, a per-run fuse).
- **writes:** soft and reversible. ``MemoryRepository.close_superseded``
  sets ``superseded_by`` + ``invalidated_by='sweep'``, content untouched;
  one ``memory_sweep_closed`` log line per row and a ``- [ ] UNDO`` line in
  the day's report under ``DataLayout.consolidation_dir``. Ticking it and
  running ``scripts/consolidation_apply.py`` reopens the row.

The old similarity merges (lexical + semantic clusters) and the EXPIRE list
are retired: similarity is not supersession (banana vs peach, two different
mozzarellas), and EXPIRE only re-labelled rows that were already out of
context (dv-cf07, owner 27.10.26).
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    Awaitable,
    Callable,
    Dict,
    List,
    Optional,
    Tuple,
)

import structlog

from src.model_router import complete_text

if TYPE_CHECKING:
    from src.storage.database import DatabaseManager
    from src.storage.deviz_board import DevizBoard
    from src.storage.memory_repository import MemoryFact, MemoryRepository

logger = structlog.get_logger()

# At most one triggered (valve) run per scope per this window (plan
# decision: request_run debounce).
DEBOUNCE_HOURS = 24

# Fuse (dv-cf07 decision 5, owner 2.1): at most max(FUSE_FLOOR, FUSE_SHARE of
# the scope's active general rows) closures per sweep. The rest waits for
# the next sweep; a model gone wrong cannot empty a scope in one run.
FUSE_FLOOR = 5
FUSE_SHARE = 0.25

# Per-fact text shown to the model; pointers carry whole result notes.
SWEEP_FACT_CHARS = 300

# One call reads ~100+ facts; the router's 120 s default timed out on the
# devops replay under load (27.10.26). Off the turn path: a new window waits
# at most MEMORY_SWEEP_WAIT_SECONDS for it anyway.
SWEEP_TIMEOUT_SECONDS = 300.0

SWEEP_SYSTEM_PROMPT = (
    "You find stale memory facts. Reply with JSON only, exactly in the "
    "format requested."
)

SWEEP_PROMPT = """\
Below are the active memory facts of one agent, one per line:
`id | kind | tier | recorded | content`.

List the facts that a NEWER fact on the SAME subject makes stale:
- a status or value changed ("concurrency is 3" -> "raised to 5");
- a version moved on ("repo is at 1.3" -> "repo is at 1.5");
- a plan, a pending step or a follow-up is now done ("release goes out after
  X is merged" -> "released 1.5");
- a problem a finding describes is fixed, and the newer fact says so.
The newer fact must say so itself; never pair two facts only because they
look alike. One newer fact can make several older ones stale: list each
pair. Different items are not stale ("likes banana" vs "likes peach"),
and scope difference is not staleness ("no nuts in desserts" vs "allergic to
nuts"). Keep a fact that still teaches something after the change: an owner
rule, a lesson, a root cause, the reason behind a decision. When unsure,
leave both.

Return ONLY a JSON array, e.g.
[{{"old_id": 12, "newer_id": 40, "reason": "version 1.3 -> 1.5"}}].
Return [] if nothing is stale.

## Facts
{facts}
"""


@dataclass
class SweepResult:
    """What one sweep of one scope did."""

    scope: str
    closed: List[Tuple[int, int]] = field(default_factory=list)
    # Per closure, for the report: (old fact, newer fact, model's reason).
    details: List[Tuple["MemoryFact", "MemoryFact", str]] = field(default_factory=list)
    proposed: int = 0
    rejected: int = 0
    over_fuse: int = 0
    failed: bool = False


def _as_utc(value: Any) -> datetime:
    """recorded_at as an aware datetime (rows carry str or datetime)."""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return datetime.min.replace(tzinfo=UTC)
    if not isinstance(value, datetime):
        return datetime.min.replace(tzinfo=UTC)
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _order_key(fact: "MemoryFact") -> Tuple[datetime, int]:
    return (_as_utc(fact.recorded_at), fact.id)


def _parse_proposals(raw: str) -> List[Tuple[int, int, str]]:
    """Model reply -> [(old_id, newer_id, reason)]; raises on a reply that is
    not a JSON array (the caller closes nothing then). Malformed entries are
    dropped one by one."""
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    data = json.loads(cleaned)
    if not isinstance(data, list):
        raise ValueError("sweep reply is not a JSON array")
    out: List[Tuple[int, int, str]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        old, new = item.get("old_id"), item.get("newer_id")
        if not isinstance(old, int) or not isinstance(new, int):
            continue
        if isinstance(old, bool) or isinstance(new, bool):
            continue
        out.append((old, new, str(item.get("reason") or "")[:200]))
    return out


class ConsolidationService:
    """Stale-fact sweep per scope: after rotations, on the valve, weekly."""

    def __init__(
        self,
        memory_repo: "MemoryRepository",
        db: "DatabaseManager",
        notify_callback: Optional[Callable[[str], Awaitable[None]]] = None,
        model: Optional[str] = None,
        report_dir: Path = Path("data/consolidation"),
        board: Optional["DevizBoard"] = None,
    ) -> None:
        self._repo = memory_repo
        self._db = db
        self._notify = notify_callback  # async fn(message)
        self._board = board
        self._model = model
        self._report_dir = report_dir
        # scope -> last triggered-run time (in-memory: a restart resetting
        # the debounce at worst produces one extra sweep).
        self._last_run: Dict[str, datetime] = {}
        # One sweep per scope at a time (rotation, valve and weekly overlap).
        self._locks: Dict[str, asyncio.Lock] = {}
        # scope -> the latest rotation's eval+sweep task (wait_rotation).
        self._rotation_tasks: Dict[str, "asyncio.Task[None]"] = {}

    # --- rotation -------------------------------------------------------

    def after_rotation(self, scope: str, final_eval: Optional[Awaitable[Any]]) -> None:
        """A window rotated: await its final extraction, then sweep ``scope``.

        Fire-and-forget like ``request_run``; the new window's bundle waits on
        it through ``wait_rotation``. The sweep runs even when the final eval
        fails (the facts already stored still deserve it).
        """
        task = asyncio.create_task(self._rotation_sweep(scope, final_eval))
        self._rotation_tasks[scope] = task

        def _forget(done: "asyncio.Task[None]") -> None:
            if self._rotation_tasks.get(scope) is done:
                del self._rotation_tasks[scope]

        task.add_done_callback(_forget)

    async def wait_rotation(self, scope: str, timeout: float) -> bool:
        """Wait (bounded) for the scope's pending rotation sweep. True when
        none is pending or it finished; False on timeout. Never cancels it:
        a late sweep still lands, for the next window."""
        task = self._rotation_tasks.get(scope)
        if task is None or task.done():
            return True
        done, _ = await asyncio.wait({task}, timeout=timeout)
        return bool(done)

    async def _rotation_sweep(
        self, scope: str, final_eval: Optional[Awaitable[Any]]
    ) -> None:
        if final_eval is not None:
            try:
                await final_eval
            except Exception as e:
                logger.warning(
                    "Final evaluation before sweep failed", scope=scope, error=str(e)
                )
        try:
            await self.sweep(scope)
        except Exception as e:
            logger.error("Rotation sweep failed", scope=scope, error=str(e))

    # --- valve ----------------------------------------------------------

    def request_run(
        self, scope: str, reason: str, snapshot_chars: Optional[int] = None
    ) -> None:
        """Budget-pressure trigger (dv-5e2a calls this when a composed
        snapshot exceeds its budget; consolidate-never-truncate is their side
        of the contract). Fire-and-forget, debounced per scope per 24h.

        Also leaves an owner-visible trail per review R4: a
        ``consolidation_needed`` log line and an open board task.
        """
        now = datetime.now(UTC)
        last = self._last_run.get(scope)
        if last and now - last < timedelta(hours=DEBOUNCE_HOURS):
            logger.debug("Consolidation request debounced", scope=scope, reason=reason)
            return
        self._last_run[scope] = now
        logger.warning(
            "consolidation_needed",
            scope=scope,
            reason=reason,
            snapshot_chars=snapshot_chars,
        )
        asyncio.create_task(self._triggered_run(scope, reason, snapshot_chars))

    async def _triggered_run(
        self, scope: str, reason: str, snapshot_chars: Optional[int]
    ) -> None:
        try:
            await self._record_agent_task(scope, reason, snapshot_chars)
            await self.run(scope)
        except Exception as e:
            logger.error("Triggered consolidation failed", scope=scope, error=str(e))

    async def _record_agent_task(
        self, scope: str, reason: str, snapshot_chars: Optional[int]
    ) -> None:
        """One open board task per scope, updated in place.

        The debounce is 24h and in-memory, so a scope that stays over budget
        trips this every day. Inserting each time buried the board: 8 of the
        21 rows on the retired SQLite board were this (dv-1327). A repeat is
        the *same* problem observed again -- it appends evidence to the open
        task instead of filing another one.
        """
        if self._board is None:
            logger.warning(
                "No task board attached; consolidation not filed", scope=scope
            )
            return
        title = f"Consolidation needed: {scope}"
        detail = f"reason: {reason}"
        if snapshot_chars is not None:
            detail += f"; snapshot_chars: {snapshot_chars}"
        existing = await self._board.find_open_by_title(title)
        if existing is not None:
            await self._board.append_comment(
                existing.id,
                f"{datetime.now(UTC).strftime('%Y-%m-%d')}: still over budget — {detail}",
            )
            logger.info("Consolidation task updated", task_id=existing.id, scope=scope)
            return
        body = detail + (
            "\n\nA stale-fact sweep ran for this scope; what it closed is in "
            f"{self._report_dir}/ (UNDO lines). Still over budget after it "
            "means the scope holds that many live facts: review them "
            "(memory_forget / recall) or raise BUNDLE_FACTS_BUDGET_CHARS."
        )
        task_id = await self._board.add_task(
            title=title,
            body=body,
            created_by="consolidation",
            assignee="user",
            labels=["memory", "consolidation"],
        )
        logger.info("Consolidation task created", task_id=task_id, scope=scope)

    # --- weekly / manual ------------------------------------------------

    async def run(self, scope: Optional[str] = None) -> Path:
        """Sweep one scope (every active scope when None), then one owner
        summary. Returns the day's report path."""
        scopes = [scope] if scope else await self._active_scopes()
        results = [await self.sweep(sc, report_empty=True) for sc in scopes]
        path = self._report_path()
        self._ensure_report(path)
        closed = sum(len(r.closed) for r in results)
        failed = [r.scope for r in results if r.failed]
        logger.info(
            "Memory sweep run done",
            path=str(path),
            scopes=len(scopes),
            closed=closed,
            failed_scopes=failed,
        )
        if self._notify:
            message = (
                f"Memory sweep: {closed} stale fact(s) closed across "
                f"{len(scopes)} scope(s) → {path} (UNDO lines inside)"
            )
            if failed:
                message += f"; sweep failed for: {', '.join(failed)}"
            try:
                await self._notify(message)
            except Exception:
                pass  # notification failure is not critical
        return path

    async def _active_scopes(self) -> List[str]:
        return await self._repo.active_scopes()

    # --- the sweep ------------------------------------------------------

    async def sweep(self, scope: str, report_empty: bool = False) -> SweepResult:
        """One model call over the scope's active facts, validated closures.

        Bad or failed model output closes nothing (never a partial guess
        from a reply that did not parse).
        """
        lock = self._locks.setdefault(scope, asyncio.Lock())
        async with lock:
            result = await self._sweep(scope)
        if result.closed or report_empty:
            self._append_report(result)
        return result

    async def _sweep(self, scope: str) -> SweepResult:
        result = SweepResult(scope=scope)
        facts = await self._repo.get_facts(scope)
        if len(facts) < 2:
            return result
        by_id = {f.id: f for f in facts}
        general_count = sum(1 for f in facts if f.tier == "general")
        fuse = max(FUSE_FLOOR, int(general_count * FUSE_SHARE))

        listing = "\n".join(self._fact_line(f) for f in sorted(facts, key=_order_key))
        try:
            raw = await complete_text(
                SWEEP_PROMPT.format(facts=listing),
                purpose="consolidation",
                model=self._model,
                system_prompt=SWEEP_SYSTEM_PROMPT,
                timeout=SWEEP_TIMEOUT_SECONDS,
            )
            proposals = _parse_proposals(raw)
        except Exception as e:
            logger.warning("Memory sweep closed nothing", scope=scope, error=str(e))
            result.failed = True
            return result

        result.proposed = len(proposals)
        closed_ids: set[int] = set()
        for old_id, newer_id, reason in proposals:
            old, newer = by_id.get(old_id), by_id.get(newer_id)
            if (
                old is None
                or newer is None
                or old_id == newer_id
                or old.tier != "general"
                or old.conflict_group is not None
                or old_id in closed_ids
                or newer_id in closed_ids
                or _order_key(newer) <= _order_key(old)
            ):
                result.rejected += 1
                continue
            if len(result.closed) >= fuse:
                result.over_fuse += 1
                continue
            if not await self._repo.close_superseded(
                old_id, newer_id, scope, source="sweep"
            ):
                result.rejected += 1
                continue
            closed_ids.add(old_id)
            result.closed.append((old_id, newer_id))
            result.details.append((old, newer, reason))
            logger.info(
                "memory_sweep_closed",
                scope=scope,
                old_id=old_id,
                newer_id=newer_id,
                reason=reason,
            )
        if result.over_fuse:
            logger.warning(
                "Memory sweep fuse hit; rest left for the next sweep",
                scope=scope,
                fuse=fuse,
                over_fuse=result.over_fuse,
            )
        logger.info(
            "Memory sweep done",
            scope=scope,
            facts=len(facts),
            proposed=result.proposed,
            closed=len(result.closed),
            rejected=result.rejected,
        )
        return result

    @staticmethod
    def _fact_line(fact: "MemoryFact") -> str:
        text = " ".join(fact.content.split())[:SWEEP_FACT_CHARS]
        recorded = _as_utc(fact.recorded_at).strftime("%Y-%m-%d %H:%M")
        return f"{fact.id} | {fact.kind} | {fact.tier} | {recorded} | {text}"

    # --- report ---------------------------------------------------------

    def _report_path(self) -> Path:
        date = datetime.now(UTC).strftime("%Y-%m-%d")
        return self._report_dir / f"report-{date}.md"

    def _ensure_report(self, path: Path) -> None:
        if path.exists():
            return
        self._report_dir.mkdir(parents=True, exist_ok=True)
        date = path.stem.removeprefix("report-")
        path.write_text(
            "\n".join(
                [
                    f"# Memory sweep — {date}",
                    "",
                    "Every closure below is already applied (soft: the row is",
                    "kept, superseded_by points at the newer fact). To undo one,",
                    "flip its `- [ ]` to `- [x]` and run:",
                    "",
                    "    python3 scripts/consolidation_apply.py --report <this file>",
                    "",
                ]
            ),
            encoding="utf-8",
        )

    def _append_report(self, result: SweepResult) -> None:
        path = self._report_path()
        try:
            self._ensure_report(path)
            stamp = datetime.now(UTC).strftime("%H:%M")
            lines = [
                "",
                f"## Scope `{result.scope}` — {stamp} UTC, "
                f"{len(result.closed)} closed",
                "",
            ]
            if result.failed:
                lines.append("Sweep failed (model error or bad reply); nothing closed.")
            elif not result.closed:
                lines.append("Nothing stale.")
            for old, newer, reason in result.details:
                lines.append(f"- [ ] UNDO scope={result.scope} id={old.id}")
                lines.append(f"  - closed `{old.id}`: {old.content[:200]}")
                lines.append(f"  - by `{newer.id}`: {newer.content[:200]}")
                if reason:
                    lines.append(f"  - why: {reason}")
            with path.open("a", encoding="utf-8") as fh:
                fh.write("\n".join(lines) + "\n")
        except OSError as e:
            # The closures stand (logged per row); only the UNDO list is lost.
            logger.error(
                "Memory sweep report not written", path=str(path), error=str(e)
            )
