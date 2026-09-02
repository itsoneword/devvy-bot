"""Persistence for frozen per-window context bundles (dv-5e2a).

One row per (user_id, working_dir, window_id): the exact system prompt every
turn of that window injects, plus composition metadata for instrumentation.
The row is written once at materialization and never updated -- the key IS the
freeze; a roll or /new bumps window_id and the next lookup misses.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Optional, Union

import structlog

if TYPE_CHECKING:
    from src.storage.database import DatabaseManager

logger = structlog.get_logger()

PathLike = Union[str, Path]

_COLUMNS = (
    "user_id, working_dir, window_id, agent_id, bundle_text, bundle_sha256, "
    "persona_chars, facts_count, pointers_count, family_count, total_chars, "
    "created_at"
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


class WindowBundleRepository:
    """CRUD (create + read only, by design) over window_bundles."""

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

        If a concurrent turn already materialized this window (INSERT OR
        IGNORE no-op), the persisted row is returned so every caller
        converges on one frozen text.
        """
        wd = self._key(bundle.working_dir)
        async with self._db.get_connection() as conn:
            await conn.execute(
                "INSERT OR IGNORE INTO window_bundles "
                "(user_id, working_dir, window_id, agent_id, bundle_text, "
                "bundle_sha256, persona_chars, facts_count, pointers_count, "
                "family_count, total_chars, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
