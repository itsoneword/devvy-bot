"""DB-backed agent memory: facts scoped by agent_id, with tiers + lifecycle.

Mirrors src/storage/window_repository.py. A fact is an atomic, verbatim,
dated statement of one of two kinds (dv-8254):

- ``user_fact`` — stated by or about the user; injected at window start.
- ``conversation_result`` — produced by the agent (research, diagnosis,
  decision); never injected as content, only as a dated one-line pointer.

Invalidation is metadata-only: the old row is closed (``invalid_at`` +
``superseded_by``), never rewritten — the supersede chain is the history.
``status`` stays maintained ('active' <=> ``invalid_at IS NULL``) so readers
written against either column agree.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, List, Optional, Tuple

import structlog

if TYPE_CHECKING:
    from .database import DatabaseManager

logger = structlog.get_logger()

GENERAL_INJECTION_CAP = 20
# Family-shared facts get their own (small) budget so they can never starve
# the member's private facts out of the prompt.
FAMILY_INJECTION_CAP = 5
# Newest conversation_result pointers surfaced per scope (owner default D7).
POINTER_CAP = 20
# Cap on general user_facts in the window-bundle snapshot (dv-7517 step 4,
# owner decision 2026-08-15: 20, may drop to 10 after observation). Injected
# facts are so the agent knows who it is speaking with, not a history dump —
# everything older is reachable via recall(). A backstop, not a target: the
# 12k-char consolidation valve in window_bundle still fires first for healthy
# scopes. Pinned stays uncapped.
SNAPSHOT_GENERAL_CAP = 20


def _fts_query(text: str) -> str:
    """Turn free text into a forgiving FTS5 OR query of quoted terms."""
    terms = re.findall(r"[\w]+", text.lower())
    if not terms:
        return '""'
    return " OR ".join(f'"{t}"' for t in terms)


@dataclass
class MemoryFact:
    id: int
    agent_id: str
    tier: str
    label: Optional[str]
    content: str
    status: str
    source: Optional[str]
    kind: str = "user_fact"
    valid_from: Optional[datetime] = None
    invalid_at: Optional[datetime] = None
    recorded_at: Optional[datetime] = None
    superseded_by: Optional[int] = None
    source_ref: Optional[str] = None
    conflict_group: Optional[str] = None
    invalidated_by: Optional[str] = None


@dataclass
class ResultPointer:
    """One-line stand-in for a conversation_result fact in injected context."""

    id: int
    recorded_at: Optional[datetime]
    label: Optional[str]
    source_ref: Optional[str]
    summary: str  # first line of content, <=100 ch


@dataclass
class FactsSnapshot:
    """Everything the window bundle injects, fetched in one call (dv-5e2a)."""

    pinned: List[MemoryFact]
    general: List[MemoryFact]
    conflicts: List[Tuple[MemoryFact, MemoryFact]]
    pointers: List[ResultPointer]


def _fact_columns(prefix: str = "") -> str:
    cols = (
        "id",
        "agent_id",
        "tier",
        "label",
        "content",
        "status",
        "source",
        "kind",
        "valid_from",
        "invalid_at",
        "recorded_at",
        "superseded_by",
        "source_ref",
        "conflict_group",
        "invalidated_by",
    )
    return ", ".join(f"{prefix}{c}" for c in cols)


def format_fact_date(dt: Optional[datetime]) -> str:
    """Owner decision D2: DD.MM.YY, always with year."""
    return dt.strftime("%d.%m.%y") if dt else "??.??.??"


def render_fact_line(fact: MemoryFact) -> str:
    """The one shared renderer for injected user_fact lines (review R7/D2)."""
    return f"[{format_fact_date(fact.valid_from)}] {fact.content}"


def render_pointer_line(pointer: ResultPointer) -> str:
    """Dated one-line pointer to a conversation_result; never its content."""
    date = format_fact_date(pointer.recorded_at)
    target = f" → {pointer.source_ref}" if pointer.source_ref else ""
    return (
        f"[{date}] {pointer.summary}{target}"
        " — retrieve via recall()/Read before relying on it"
    )


def truncate_at_word(text: str, limit: int = 100) -> str:
    """Cut at the last word boundary within ``limit``, appending … only when
    something was actually cut. A single word longer than the limit is
    hard-cut (still with …) rather than returned overlong."""
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]  # reserve room for the ellipsis
    boundary = cut.rfind(" ")
    if boundary > 0:
        cut = cut[:boundary]
    return cut.rstrip() + "…"


def _pointer_from_fact(fact: MemoryFact) -> ResultPointer:
    # Title = the label when one exists; only unlabeled rows fall back to a
    # first-line excerpt. Pointers must identify the work, not preview it
    # (dv-5e2a criterion 3: conversation_result content appears nowhere).
    if fact.label:
        summary = truncate_at_word(fact.label.strip())
    elif fact.content:
        summary = truncate_at_word(fact.content.strip().splitlines()[0])
    else:
        summary = ""
    return ResultPointer(
        id=fact.id,
        recorded_at=fact.recorded_at or fact.valid_from,
        label=fact.label,
        source_ref=fact.source_ref,
        summary=summary,
    )


class MemoryRepository:
    """CRUD over memory_facts, scoped by agent_id."""

    def __init__(self, db: "DatabaseManager") -> None:
        self._db = db

    async def add_fact(
        self,
        agent_id: str,
        tier: str,
        content: str,
        label: Optional[str] = None,
        source: str = "manual",
        kind: str = "user_fact",
        source_ref: Optional[str] = None,
        valid_from: Optional[datetime] = None,
    ) -> int:
        now = datetime.now(UTC).isoformat()
        async with self._db.get_connection() as conn:
            cur = await conn.execute(
                "INSERT INTO memory_facts "
                "(agent_id, tier, label, content, source, kind, source_ref, "
                "valid_from, recorded_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    agent_id,
                    tier,
                    label,
                    content,
                    source,
                    kind,
                    source_ref,
                    valid_from.isoformat() if valid_from else now,
                    now,
                ),
            )
            await conn.commit()
            return int(cur.lastrowid or 0)

    async def get_facts(
        self,
        agent_id: str,
        tier: Optional[str] = None,
        status: str = "active",
        kind: Optional[str] = None,
    ) -> List[MemoryFact]:
        sql = (
            f"SELECT {_fact_columns()} "
            "FROM memory_facts WHERE agent_id=? AND status=?"
        )
        params: list = [agent_id, status]
        if tier is not None:
            sql += " AND tier=?"
            params.append(tier)
        if kind is not None:
            sql += " AND kind=?"
            params.append(kind)
        sql += " ORDER BY updated_at DESC, id DESC"
        async with self._db.get_connection() as conn:
            cur = await conn.execute(sql, tuple(params))
            return [MemoryFact(*row) for row in await cur.fetchall()]

    async def get_agent_tool_facts(
        self, agent_id: str, since: Optional[datetime] = None
    ) -> List[MemoryFact]:
        """Active facts the in-session ``remember`` tool saved, optionally
        bounded to rows created after ``since`` (dv-c5d9 step 1: what was
        already saved during the evaluator's slice)."""
        sql = (
            f"SELECT {_fact_columns()} FROM memory_facts "
            "WHERE agent_id=? AND source='agent_tool' AND invalid_at IS NULL"
        )
        params: list = [agent_id]
        if since is not None:
            # created_at comes from CURRENT_TIMESTAMP: space-separated naive
            # UTC. The bound must match that lexical format or same-day rows
            # compare wrong ('T' sorts after ' ').
            if since.tzinfo is not None:
                since = since.astimezone(UTC).replace(tzinfo=None)
            sql += " AND created_at > ?"
            params.append(since.isoformat(sep=" "))
        async with self._db.get_connection() as conn:
            cur = await conn.execute(sql, tuple(params))
            return [MemoryFact(*row) for row in await cur.fetchall()]

    async def get_fact(self, fact_id: int, agent_id: str) -> Optional[MemoryFact]:
        """One fact by id, scoped — the audit tools' server-side ownership
        check (dv-a9e1): an id that exists but belongs to another scope
        returns None, indistinguishable from not existing."""
        async with self._db.get_connection() as conn:
            cur = await conn.execute(
                f"SELECT {_fact_columns()} FROM memory_facts "
                "WHERE id=? AND agent_id=?",
                (fact_id, agent_id),
            )
            row = await cur.fetchone()
            return MemoryFact(*row) if row else None

    async def get_fact_history(self, fact_id: int, agent_id: str) -> List[MemoryFact]:
        """The full supersede chain containing ``fact_id``, oldest first.

        Walks ``superseded_by`` links in both directions from the given row
        ("you said X until 04.08, then Y" — dv-a9e1). Empty list when the id
        is not in this scope. Cycle-guarded: a corrupt link terminates the
        walk instead of hanging it.
        """
        fact = await self.get_fact(fact_id, agent_id)
        if fact is None:
            return []
        chain = [fact]
        seen = {fact.id}
        async with self._db.get_connection() as conn:
            # Backwards: predecessors point AT the chain head via superseded_by.
            head = fact.id
            while True:
                cur = await conn.execute(
                    f"SELECT {_fact_columns()} FROM memory_facts "
                    "WHERE superseded_by=? AND agent_id=?",
                    (head, agent_id),
                )
                row = await cur.fetchone()
                if row is None:
                    break
                prev = MemoryFact(*row)
                if prev.id in seen:
                    break
                chain.insert(0, prev)
                seen.add(prev.id)
                head = prev.id
            # Forwards: follow the tail's own superseded_by link.
            next_id = fact.superseded_by
            while next_id and next_id not in seen:
                cur = await conn.execute(
                    f"SELECT {_fact_columns()} FROM memory_facts "
                    "WHERE id=? AND agent_id=?",
                    (next_id, agent_id),
                )
                row = await cur.fetchone()
                if row is None:
                    break
                nxt = MemoryFact(*row)
                chain.append(nxt)
                seen.add(nxt.id)
                next_id = nxt.superseded_by
        return chain

    async def supersede_fact(
        self,
        old_id: int,
        agent_id: str,
        tier: str,
        content: str,
        label: Optional[str] = None,
        source: str = "evaluator",
        kind: str = "user_fact",
        source_ref: Optional[str] = None,
    ) -> int:
        """Metadata-only supersede: insert the new statement verbatim as its
        own row, close the old one (``invalid_at`` + ``superseded_by`` link).
        The old row's content is never touched — no code path merges text.
        """
        now = datetime.now(UTC).isoformat()
        async with self._db.get_connection() as conn:
            cur = await conn.execute(
                "INSERT INTO memory_facts "
                "(agent_id, tier, label, content, source, kind, source_ref, "
                "valid_from, recorded_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (agent_id, tier, label, content, source, kind, source_ref, now, now),
            )
            new_id = int(cur.lastrowid or 0)
            await conn.execute(
                "UPDATE memory_facts SET status='superseded', invalid_at=?, "
                "superseded_by=?, invalidated_by=?, updated_at=? "
                "WHERE id=? AND agent_id=?",
                (now, new_id, source, now, old_id, agent_id),
            )
            await conn.commit()
            return new_id

    async def invalidate_fact(
        self, fact_id: int, agent_id: str, source: str = "evaluator"
    ) -> bool:
        """Close a fact with no replacement ("no longer true, nothing new").

        Also the primitive behind the audit path's "forget that" (dv-a9e1,
        ``source='user_request'``). Returns False when no row matched.
        """
        now = datetime.now(UTC).isoformat()
        async with self._db.get_connection() as conn:
            cur = await conn.execute(
                "UPDATE memory_facts SET status='superseded', invalid_at=?, "
                "invalidated_by=?, updated_at=? "
                "WHERE id=? AND agent_id=? AND invalid_at IS NULL",
                (now, source, now, fact_id, agent_id),
            )
            await conn.commit()
            return cur.rowcount > 0

    async def flag_conflict(self, id_a: int, id_b: int, agent_id: str) -> None:
        """Mark two facts as a safety conflict; BOTH stay active (review R1).

        Same ``conflict_group`` value (lowest id of the pair, as text) on both
        rows, so ``conflict_group IS NOT NULL`` fetches all conflicted rows
        and GROUP BY pairs them.
        """
        group = str(min(id_a, id_b))
        now = datetime.now(UTC).isoformat()
        async with self._db.get_connection() as conn:
            await conn.execute(
                "UPDATE memory_facts SET conflict_group=?, updated_at=? "
                "WHERE id IN (?, ?) AND agent_id=?",
                (group, now, id_a, id_b, agent_id),
            )
            await conn.commit()

    async def search(
        self,
        agent_id: str,
        query: str,
        limit: int,
        include_invalid: bool = False,
    ) -> List[MemoryFact]:
        """FTS keyword match over ALL facts (any tier, any kind), no recency
        fill.

        For explicit agent-driven retrieval (the ``recall`` tool), where a
        query that matches nothing should return nothing -- unlike
        :meth:`search_general`, used for automatic per-turn injection, which
        pads with recent facts so a small store is never left empty in the
        prompt.

        ``include_invalid=True`` returns closed rows too, with their validity
        window on the dataclass — "when did this change" becomes answerable.
        """
        q = query.strip()
        if not q:
            return []
        sql = (
            f"SELECT {_fact_columns('f.')} "
            "FROM memory_facts_fts fts "
            "JOIN memory_facts f ON f.id = fts.rowid "
            "WHERE memory_facts_fts MATCH ? AND f.agent_id=? "
        )
        if not include_invalid:
            sql += "AND f.invalid_at IS NULL "
        sql += "ORDER BY rank LIMIT ?"
        async with self._db.get_connection() as conn:
            cur = await conn.execute(sql, (_fts_query(q), agent_id, limit))
            return [MemoryFact(*row) for row in await cur.fetchall()]

    async def search_general(
        self, agent_id: str, query: str, limit: int = GENERAL_INJECTION_CAP
    ) -> List[MemoryFact]:
        """FTS keyword match over active general user_facts, topped up with
        most-recent facts to ``limit``.

        The recency fill is not an optimization: facts are often stored in a
        different language than the user's message (e.g. English facts,
        Russian chat), where keyword FTS matches nothing. Filling to the cap
        keeps small fact sets fully injected regardless of query language.

        ``conversation_result`` rows are excluded by construction — they are
        never injected as content, only as pointers (dv-8254).
        """
        q = query.strip()
        async with self._db.get_connection() as conn:
            matched: List[MemoryFact] = []
            if q:
                cur = await conn.execute(
                    f"SELECT {_fact_columns('f.')} "
                    "FROM memory_facts_fts fts "
                    "JOIN memory_facts f ON f.id = fts.rowid "
                    "WHERE memory_facts_fts MATCH ? AND f.agent_id=? "
                    "AND f.tier='general' AND f.invalid_at IS NULL "
                    "AND f.kind='user_fact' "
                    "ORDER BY rank LIMIT ?",
                    (_fts_query(q), agent_id, limit),
                )
                matched = [MemoryFact(*row) for row in await cur.fetchall()]
            if len(matched) < limit:
                seen = {f.id for f in matched}
                cur = await conn.execute(
                    f"SELECT {_fact_columns()} "
                    "FROM memory_facts WHERE agent_id=? AND tier='general' "
                    "AND invalid_at IS NULL AND kind='user_fact' "
                    "ORDER BY updated_at DESC, id DESC LIMIT ?",
                    (agent_id, limit),
                )
                for row in await cur.fetchall():
                    fact = MemoryFact(*row)
                    if fact.id not in seen and len(matched) < limit:
                        matched.append(fact)
            return matched

    async def get_pointers(
        self, agent_id: str, cap: int = POINTER_CAP
    ) -> List[ResultPointer]:
        """Newest active conversation_result rows as injectable pointers."""
        async with self._db.get_connection() as conn:
            cur = await conn.execute(
                f"SELECT {_fact_columns()} "
                "FROM memory_facts WHERE agent_id=? "
                "AND kind='conversation_result' AND invalid_at IS NULL "
                "ORDER BY recorded_at DESC, id DESC LIMIT ?",
                (agent_id, cap),
            )
            return [
                _pointer_from_fact(MemoryFact(*row)) for row in await cur.fetchall()
            ]

    async def facts_snapshot(self, agent_id: str) -> FactsSnapshot:
        """One-call read of everything injectable for a scope (dv-5e2a's
        frozen window bundle consumes this; dv-c5d9's reconciler reads the
        same snapshot)."""
        async with self._db.get_connection() as conn:
            cur = await conn.execute(
                f"SELECT {_fact_columns()} "
                "FROM memory_facts WHERE agent_id=? AND kind='user_fact' "
                "AND invalid_at IS NULL AND tier='pinned' "
                "ORDER BY updated_at DESC, id DESC",
                (agent_id,),
            )
            pinned = [MemoryFact(*row) for row in await cur.fetchall()]
            # Core = hard identity facts (age, goals, current plan). Never
            # capped and rendered ahead of general: recency is the right
            # ordering for status, the wrong one for identity — without this
            # split, every new status row eventually pushes an identity fact
            # out of the prompt (dv-7517 follow-up, owner 2026-08-15).
            cur = await conn.execute(
                f"SELECT {_fact_columns()} "
                "FROM memory_facts WHERE agent_id=? AND kind='user_fact' "
                "AND invalid_at IS NULL AND tier='core' "
                "ORDER BY recorded_at DESC, id DESC",
                (agent_id,),
            )
            core = [MemoryFact(*row) for row in await cur.fetchall()]
            cur = await conn.execute(
                f"SELECT {_fact_columns()} "
                "FROM memory_facts WHERE agent_id=? AND kind='user_fact' "
                "AND invalid_at IS NULL AND tier='general' "
                "ORDER BY recorded_at DESC, id DESC LIMIT ?",
                (agent_id, SNAPSHOT_GENERAL_CAP),
            )
            general = core + [MemoryFact(*row) for row in await cur.fetchall()]
            cur = await conn.execute(
                f"SELECT {_fact_columns()} "
                "FROM memory_facts WHERE agent_id=? AND invalid_at IS NULL "
                "AND conflict_group IS NOT NULL "
                "ORDER BY conflict_group, id",
                (agent_id,),
            )
            conflicted = [MemoryFact(*row) for row in await cur.fetchall()]

        conflicts: List[Tuple[MemoryFact, MemoryFact]] = []
        by_group: dict[str, List[MemoryFact]] = {}
        for fact in conflicted:
            by_group.setdefault(fact.conflict_group or "", []).append(fact)
        for group in by_group.values():
            # Adjacent pairs; a group larger than 2 still renders pairwise.
            for a, b in zip(group[::2], group[1::2]):
                conflicts.append((a, b))

        pointers = await self.get_pointers(agent_id)
        return FactsSnapshot(
            pinned=pinned, general=general, conflicts=conflicts, pointers=pointers
        )


class MemoryRetriever:
    """Selects facts to inject each turn: pinned always, general by FTS + cap.

    v1 policy. Swap in semantic retrieval later without touching the composer
    or schema -- only this class changes.

    Injected lines are date-prefixed (``[04.08.26] no nuts``, owner decision
    D2) and filtered to ``kind='user_fact'``; conversation_result rows
    surface only as dated pointers (third element of :meth:`select`).
    """

    def __init__(
        self,
        repo: MemoryRepository,
        general_cap: int = GENERAL_INJECTION_CAP,
        family_cap: int = FAMILY_INJECTION_CAP,
    ) -> None:
        self._repo = repo
        self._cap = general_cap
        self._family_cap = family_cap

    async def select(
        self, agent_id: str, query: str
    ) -> tuple[list[str], list[str], list[str]]:
        """Return (pinned_lines, general_lines, pointer_lines)."""
        pinned = await self._repo.get_facts(agent_id, tier="pinned", kind="user_fact")
        # Core identity facts are always injected, ahead of the FTS/recency
        # selection and outside its cap (mirrors facts_snapshot).
        core = await self._repo.get_facts(agent_id, tier="core", kind="user_fact")
        general = await self._repo.search_general(agent_id, query, limit=self._cap)
        pointers = await self._repo.get_pointers(agent_id)
        return (
            [render_fact_line(f) for f in pinned],
            [render_fact_line(f) for f in core + general],
            [render_pointer_line(p) for p in pointers],
        )

    async def select_family(self, agent_id: str, query: str) -> list[str]:
        """Family-shared facts under their own budget (split from private).

        ``agent_id`` is the family scope (``f{family_id}:{slug}``). Pinned
        family facts go first, then FTS/recency general facts fill the
        remaining budget — capped at ``family_cap`` total so family facts can
        never crowd out the member's private facts.
        """
        pinned = await self._repo.get_facts(agent_id, tier="pinned", kind="user_fact")
        lines = [render_fact_line(f) for f in pinned[: self._family_cap]]
        remaining = self._family_cap - len(lines)
        if remaining > 0:
            general = await self._repo.search_general(agent_id, query, limit=remaining)
            lines.extend(render_fact_line(f) for f in general)
        return lines
