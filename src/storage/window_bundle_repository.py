"""Persistence for frozen per-window context bundles (dv-5e2a).

One row per (user_id, working_dir, window_id): the exact system prompt every
turn of that window injects, plus composition metadata for instrumentation.
The row is written once at materialization -- the key IS the freeze; a roll or
/new bumps window_id and the next lookup misses.

One exception (dv-ef59, amends dv-5e2a): a pinned or core fact change marks
the current row of every affected window stale (:meth:`mark_stale`). A stale
row is a miss; the next turn re-materializes and :meth:`put` replaces the row
in place (``revision`` + 1). General facts never mark a row stale.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Iterable, Optional, Union

import structlog

if TYPE_CHECKING:
    from src.storage.database import DatabaseManager

logger = structlog.get_logger()

PathLike = Union[str, Path]

_COLUMNS = (
    "user_id, working_dir, window_id, agent_id, bundle_text, bundle_sha256, "
    "persona_chars, facts_count, pointers_count, family_count, total_chars, "
    "created_at, family_agent_id, stale_at, revision"
)


@dataclass
class WindowBundle:
    user_id: int
    working_dir: str
    window_id: int
    agent_id: str
    bundle_text: str
    bundle_sha256: str
    persona_chars: int
    facts_count: int
    pointers_count: int
    family_count: int
    total_chars: int
    created_at: Optional[datetime] = None
    # Family scope whose facts this bundle includes (dv-ef59); None = none.
    family_agent_id: Optional[str] = None
    # Set when a pinned/core fact behind the bundle changed (dv-ef59).
    stale_at: Optional[datetime] = None
    revision: int = 0


class WindowBundleRepository:
    """Create + read over window_bundles; the only update is the dv-ef59
    stale mark and the in-place refresh of a stale row."""

    def __init__(self, db: "DatabaseManager") -> None:
        self._db = db

    @staticmethod
    def _key(working_dir: PathLike) -> str:
        # Same normalization as WindowRepository._key -- the two tables must
        # agree on the working_dir string or bundles never match their window.
        return str(Path(working_dir))

    async def get(
        self, user_id: int, working_dir: PathLike, window_id: int
    ) -> Optional[WindowBundle]:
        wd = self._key(working_dir)
        async with self._db.get_connection() as conn:
            cur = await conn.execute(
                f"SELECT {_COLUMNS} FROM window_bundles "
                "WHERE user_id=? AND working_dir=? AND window_id=?",
                (user_id, wd, window_id),
            )
            row = await cur.fetchone()
            return WindowBundle(*row) if row else None

    async def put(self, bundle: WindowBundle) -> WindowBundle:
        """Insert the bundle; first write wins.

        If a concurrent turn already materialized this window (no-op), the
        persisted row is returned so every caller converges on one frozen
        text. A row marked stale (dv-ef59) is the one exception: the first
        write after the mark replaces it and clears the mark, later
        concurrent writes are no-ops again.
        """
        wd = self._key(bundle.working_dir)
        async with self._db.get_connection() as conn:
            await conn.execute(
                "INSERT INTO window_bundles "
                "(user_id, working_dir, window_id, agent_id, bundle_text, "
                "bundle_sha256, persona_chars, facts_count, pointers_count, "
                "family_count, total_chars, created_at, family_agent_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(user_id, working_dir, window_id) DO UPDATE SET "
                "agent_id=excluded.agent_id, bundle_text=excluded.bundle_text, "
                "bundle_sha256=excluded.bundle_sha256, "
                "persona_chars=excluded.persona_chars, "
                "facts_count=excluded.facts_count, "
                "pointers_count=excluded.pointers_count, "
                "family_count=excluded.family_count, "
                "total_chars=excluded.total_chars, "
                "created_at=excluded.created_at, "
                "family_agent_id=excluded.family_agent_id, "
                "stale_at=NULL, revision=window_bundles.revision + 1 "
                "WHERE window_bundles.stale_at IS NOT NULL",
                (
                    bundle.user_id,
                    wd,
                    bundle.window_id,
                    bundle.agent_id,
                    bundle.bundle_text,
                    bundle.bundle_sha256,
                    bundle.persona_chars,
                    bundle.facts_count,
                    bundle.pointers_count,
                    bundle.family_count,
                    bundle.total_chars,
                    datetime.now(UTC),
                    bundle.family_agent_id,
                ),
            )
            await conn.commit()
        stored = await self.get(bundle.user_id, wd, bundle.window_id)
        if stored is None:  # pragma: no cover - insert just committed
            return bundle
        if stored.bundle_sha256 != bundle.bundle_sha256:
            logger.warning(
                "Concurrent bundle materialization; keeping first write",
                user_id=bundle.user_id,
                working_dir=wd,
                window_id=bundle.window_id,
            )
        return stored

    async def mark_stale(self, scopes: Iterable[str]) -> int:
        """Mark stale the current-window bundle of every key whose bundle
        reads facts from one of ``scopes`` -- its private ``agent_id`` or its
        ``family_agent_id`` (dv-ef59). Older windows' rows are audit only and
        stay untouched. Returns the number of rows marked."""
        wanted = sorted({s for s in scopes if s})
        if not wanted:
            return 0
        marks = ",".join("?" for _ in wanted)
        async with self._db.get_connection() as conn:
            cur = await conn.execute(
                "UPDATE window_bundles SET stale_at=? "
                "WHERE stale_at IS NULL "
                f"AND (agent_id IN ({marks}) OR family_agent_id IN ({marks})) "
                "AND window_id = (SELECT MAX(w2.window_id) FROM window_bundles w2 "
                "WHERE w2.user_id = window_bundles.user_id "
                "AND w2.working_dir = window_bundles.working_dir)",
                (datetime.now(UTC), *wanted, *wanted),
            )
            await conn.commit()
            count = int(cur.rowcount or 0)
        if count:
            logger.info("bundle_marked_stale", scopes=wanted, rows=count)
        return count
