"""CRUD over webhook_events (dv-5e4f Phase 0).

Previously a module-level function in src/api/server.py.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Dict

if TYPE_CHECKING:
    from .database import DatabaseManager


class WebhookEventRepository:
    """Repository for the ``webhook_events`` table."""

    def __init__(self, db: "DatabaseManager") -> None:
        self._db = db

    async def record_if_new(
        self,
        event_id: str,
        provider: str,
        event_type: str,
        delivery_id: str,
        payload: Dict[str, Any],
    ) -> bool:
        """Atomically insert a webhook event, returning whether it was new.

        INSERT OR IGNORE on the unique ``delivery_id`` column; if the row
        already exists the insert is a no-op and ``changes() == 0``.
        """
        async with self._db.get_connection() as conn:
            await conn.execute(
                "INSERT OR IGNORE INTO webhook_events "
                "(event_id, provider, event_type, delivery_id, payload, "
                "processed) VALUES (?, ?, ?, ?, ?, 1)",
                (event_id, provider, event_type, delivery_id, json.dumps(payload)),
            )
            cursor = await conn.execute("SELECT changes()")
            row = await cursor.fetchone()
            inserted = row[0] > 0 if row else False
            await conn.commit()
            return inserted
