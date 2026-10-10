"""Memory consolidation pass: report duplicate clusters + expiry candidates.

The per-message evaluator stays conservative (when unsure, keep both facts);
this offline pass is the pressure valve that later merges duplicates and
expires long-invalid rows (dv-4c6f, Letta sleep-time pattern).

**Zero writes to memory_facts.** The pass emits a markdown report under
``DataLayout.consolidation_dir`` (``<root>/workspaces/devops/reports/
consolidation/``; legacy layout ``data/consolidation/``) and notifies the owner; merges/expiries are executed
only by ``scripts/consolidation_apply.py`` from an owner-annotated report —
a wrong merge is unrecoverable and invisible, so nothing is automatic.

Semantic comparison via one haiku CLI call per scope is sanctioned here
(off the write path — see dv-3c8f): cross-language restatements
("любит острое" / "likes spicy food") are invisible to the lexical pass.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path
from typing import TYPE_CHECKING, Awaitable, Callable, Dict, List, Optional

import structlog

from src.evaluator.writer import normalize_fact
from src.model_router import complete_text

if TYPE_CHECKING:
    from src.storage.database import DatabaseManager
    from src.storage.deviz_board import DevizBoard
    from src.storage.memory_repository import MemoryFact, MemoryRepository

logger = structlog.get_logger()

# Looser than the write path's 0.92 duplicate gate: these are *candidates*
# for owner review, not automatic rejections, so recall beats precision.
CANDIDATE_RATIO = 0.80

# Rows invalid for this long are proposed for expiry (owner default D15;
# age-only — per-fact recall hits are not measurable today).
EXPIRY_DAYS = 60

# At most one triggered run per scope per this window (plan decision:
# request_run debounce).
DEBOUNCE_HOURS = 24

SEMANTIC_SYSTEM_PROMPT = (
    "You group near-duplicate memory facts. Reply with JSON only, exactly in "
    "the format requested."
)

SEMANTIC_PROMPT = """\
Below are memory facts for one agent, as `id | content` lines. Identify groups
of facts that say the SAME thing (cross-language restatements count: a Russian
and an English sentence with the same meaning are duplicates). Scope
difference is NOT duplication ("no nuts in desserts" vs "allergic to nuts"
are different facts). When unsure, do NOT group.

Return ONLY a JSON array of arrays of ids, e.g. [[3,17],[5,9,12]].
Return [] if nothing is duplicated.

## Facts
{facts}
"""


class ConsolidationService:
    """Builds consolidation reports; never writes to memory_facts."""

    def __init__(
        self,
        memory_repo: "MemoryRepository",
        db: "DatabaseManager",
        notify_callback: Optional[Callable[[str], Awaitable[None]]] = None,
        model: str = "haiku",
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
        # the debounce at worst produces one extra report).
        self._last_run: Dict[str, datetime] = {}

    def request_run(
        self, scope: str, reason: str, snapshot_chars: Optional[int] = None
    ) -> None:
        """Budget-pressure trigger (dv-5e2a calls this when a composed
        snapshot exceeds its budget; consolidate-never-truncate is their side
        of the contract). Fire-and-forget, debounced per scope per 24h.

        Also leaves an owner-visible trail per review R4: a
        ``consolidation_needed`` log line and an open ``agent_tasks`` row.
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
            f"\n\nReview the consolidation report in {self._report_dir}/ and "
            "apply approved actions with scripts/consolidation_apply.py."
        )
        task_id = await self._board.add_task(
            title=title,
            body=body,
            created_by="consolidation",
            assignee="user",
            labels=["memory", "consolidation"],
        )
        logger.info("Consolidation task created", task_id=task_id, scope=scope)

    async def run(self, scope: Optional[str] = None) -> Path:
        """Build the report for one scope (all scopes when None). Returns the
        report path. Read-only against memory_facts."""
        scopes = [scope] if scope else await self._active_scopes()
        date = datetime.now(UTC).strftime("%Y-%m-%d")
        sections: List[str] = [
            f"# Consolidation report — {date}",
            "",
            "Read-only proposal. Nothing below has been applied. Annotate by",
            "flipping `- [ ]` to `- [x]` on approved actions, then run:",
            "",
            "    python3 scripts/consolidation_apply.py --report <this file>",
            "",
        ]
        total_groups = 0
        total_expiry = 0
        for sc in scopes:
            sec, n_groups, n_expiry = await self._scope_section(sc)
            sections.append(sec)
            total_groups += n_groups
            total_expiry += n_expiry

        self._report_dir.mkdir(parents=True, exist_ok=True)
        path = self._report_dir / f"report-{date}.md"
        path.write_text("\n".join(sections), encoding="utf-8")
        logger.info(
            "Consolidation report written",
            path=str(path),
            scopes=len(scopes),
            merge_groups=total_groups,
            expiry_candidates=total_expiry,
        )
        if self._notify:
            try:
                await self._notify(
                    f"Consolidation report: {total_groups} merge group(s), "
                    f"{total_expiry} expiry candidate(s) across {len(scopes)} "
                    f"scope(s) → {path}"
                )
            except Exception:
                pass  # notification failure is not critical
        return path

    async def _active_scopes(self) -> List[str]:
        return await self._repo.active_scopes()

    async def _scope_section(self, scope: str) -> tuple[str, int, int]:
        facts = await self._repo.get_facts(scope)
        groups = self._lexical_clusters(facts)
        semantic = await self._semantic_clusters(scope, facts)
        # Union, deduped on frozenset of ids; lexical evidence first.
        seen_groups = {frozenset(g) for g in groups}
        for g in semantic:
            if frozenset(g) not in seen_groups and len(g) >= 2:
                groups.append(g)
                seen_groups.add(frozenset(g))

        expiry = await self._expiry_candidates(scope)

        by_id = {f.id: f for f in facts}
        lines = [f"## Scope `{scope}` — {len(facts)} active fact(s)", ""]
        if groups:
            lines.append("### Proposed merge groups (full verbatim texts)")
            lines.append("")
            for group in groups:
                members = [by_id[i] for i in group if i in by_id]
                if len(members) < 2:
                    continue
                survivor = max(members, key=lambda f: f.id)
                closing = ",".join(str(f.id) for f in members if f.id != survivor.id)
                lines.append(
                    f"- [ ] MERGE scope={scope} survivor={survivor.id} "
                    f"close={closing}"
                )
                for f in members:
                    lines.append(f"  - `{f.id}` ({f.tier}): {f.content}")
                lines.append("")
        else:
            lines.append("No merge candidates.")
            lines.append("")
        if expiry:
            lines.append(
                f"### Expiry candidates (invalid > {EXPIRY_DAYS} days, age-only)"
            )
            lines.append("")
            for f in expiry:
                lines.append(f"- [ ] EXPIRE scope={scope} id={f.id}")
                lines.append(
                    f"  - `{f.id}` invalid since {f.invalid_at}: " f"{f.content[:120]}"
                )
            lines.append("")
        return "\n".join(lines), len(groups), len(expiry)

    def _lexical_clusters(self, facts: List["MemoryFact"]) -> List[List[int]]:
        """Greedy single-link clustering at the looser candidate ratio."""
        normalized = [(f.id, normalize_fact(f.content)) for f in facts]
        clusters: List[List[int]] = []
        assigned: Dict[int, int] = {}
        for i, (id_a, norm_a) in enumerate(normalized):
            for id_b, norm_b in normalized[i + 1 :]:
                if not norm_a or not norm_b:
                    continue
                shorter, longer = sorted((len(norm_a), len(norm_b)))
                if longer and shorter / longer < CANDIDATE_RATIO:
                    continue
                if SequenceMatcher(None, norm_a, norm_b).ratio() < CANDIDATE_RATIO:
                    continue
                if id_a in assigned:
                    clusters[assigned[id_a]].append(id_b)
                    assigned[id_b] = assigned[id_a]
                elif id_b in assigned:
                    clusters[assigned[id_b]].append(id_a)
                    assigned[id_a] = assigned[id_b]
                else:
                    clusters.append([id_a, id_b])
                    assigned[id_a] = assigned[id_b] = len(clusters) - 1
        return [sorted(set(c)) for c in clusters]

    async def _semantic_clusters(
        self, scope: str, facts: List["MemoryFact"]
    ) -> List[List[int]]:
        """One haiku call per scope for cross-language restatements.

        Failure returns [] — the lexical pass still stands, and the report
        notes nothing (a missing semantic pass is visible in the logs)."""
        if len(facts) < 2:
            return []
        listing = "\n".join(f"{f.id} | {f.content}" for f in facts)
        try:
            raw = await complete_text(
                SEMANTIC_PROMPT.format(facts=listing),
                purpose="consolidation",
                model=self._model,
                system_prompt=SEMANTIC_SYSTEM_PROMPT,
            )
            cleaned = raw.strip()
            if cleaned.startswith("```"):
                cleaned = cleaned.split("\n", 1)[1].rsplit("```", 1)[0].strip()
            data = json.loads(cleaned)
            valid_ids = {f.id for f in facts}
            groups: List[List[int]] = []
            if isinstance(data, list):
                for group in data:
                    if (
                        isinstance(group, list)
                        and len(group) >= 2
                        and all(isinstance(i, int) and i in valid_ids for i in group)
                    ):
                        groups.append(sorted(set(group)))
            return groups
        except Exception as e:
            logger.warning(
                "Semantic consolidation pass failed; lexical only",
                scope=scope,
                error=str(e),
            )
            return []

    async def _expiry_candidates(self, scope: str) -> List["MemoryFact"]:
        cutoff = datetime.now(UTC) - timedelta(days=EXPIRY_DAYS)
        return await self._repo.expiry_candidates(scope, cutoff)
