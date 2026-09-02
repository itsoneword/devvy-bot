"""Records profile/session lifecycle events to SQLite; surfaced via /diag.

Built against DatabaseManager.get_connection() (async context manager). Never
raises — observability must never break a response.
"""

import json
from datetime import UTC, datetime
from typing import Any, Dict, List

import structlog

logger = structlog.get_logger()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS lifecycle_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    profile TEXT NOT NULL,
    event TEXT NOT NULL,
    detail TEXT NOT NULL
)
"""


class LifecycleLogger:
    """Append-only lifecycle event recorder. Never raises."""

    def __init__(self, db: Any) -> None:
        self._db = db

    async def ensure_schema(self) -> None:
        try:
            async with self._db.get_connection() as conn:
                await conn.execute(_SCHEMA)
                await conn.commit()
        except Exception as e:  # noqa: BLE001
            logger.warning("lifecycle schema init failed", error=str(e))

    async def record(self, profile: str, event: str, detail: Dict[str, Any]) -> None:
        try:
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
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "lifecycle record failed", lifecycle_event=event, error=str(e)
            )

    async def recent(self, profile: str, limit: int = 50) -> List[Dict[str, Any]]:
        try:
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
        except Exception as e:  # noqa: BLE001
            logger.warning("lifecycle recent failed", error=str(e))
            return []
