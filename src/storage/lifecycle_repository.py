"""CRUD over lifecycle_events (dv-5e4f Phase 0).

Previously src/observability/lifecycle.py owned both the DDL (``_SCHEMA`` /
``ensure_schema()``, an idempotent CREATE TABLE IF NOT EXISTS run outside
the bot.db migration chain) and the raw SQL. The table now has one owner:
this file for access, bot.db migration v26 (src/storage/database.py) for
the DDL.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Dict, List

if TYPE_CHECKING:
    from .database import DatabaseManager


_SCHEMA = """
CREATE TABLE IF NOT EXISTS lifecycle_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    profile TEXT NOT NULL,
    event TEXT NOT NULL,
    detail TEXT NOT NULL
)
"""


class LifecycleRepository:
    """Repository for the ``lifecycle_events`` table."""

    def __init__(self, db: "DatabaseManager") -> None:
        self._db = db

    async def ensure_schema(self) -> None:
        """Idempotent CREATE TABLE IF NOT EXISTS -- a no-op against a real
        bot.db (migration v26 already created the table) but still needed
        for callers that hand in a bare db-like stub of their own (e.g.
        tests) that was never migrated."""
        async with self._db.get_connection() as conn:
            await conn.execute(_SCHEMA)
            await conn.commit()

    async def record(self, profile: str, event: str, detail: Dict[str, Any]) -> None:
        async with self._db.get_connection() as conn:
            await conn.execute(
                "INSERT INTO lifecycle_events (ts, profile, event, detail) "
                "VALUES (?, ?, ?, ?)",
                (
                    datetime.now(UTC).isoformat(),
                    profile,
                    event,
                    json.dumps(detail),
                ),
            )
            await conn.commit()

    async def recent(self, profile: str, limit: int = 50) -> List[Dict[str, Any]]:
        async with self._db.get_connection() as conn:
            cursor = await conn.execute(
                "SELECT ts, profile, event, detail FROM lifecycle_events "
                "WHERE profile = ? ORDER BY id DESC LIMIT ?",
                (profile, limit),
            )
            rows = await cursor.fetchall()
        return [
            {
                "ts": r[0],
                "profile": r[1],
                "event": r[2],
                "detail": json.loads(r[3]),
            }
            for r in rows
        ]
