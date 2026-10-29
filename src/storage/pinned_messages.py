"""Pinned status messages (schema v22, dv-ebde).

``NotificationService`` keeps one live, edited-in-place status message pinned
per (chat, pin_as). The map has to survive a restart, or the current pin is
orphaned (never edited or unpinned again) and the next job pins a second.
This replaces ``data/pinned_messages.json``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Dict, Tuple

if TYPE_CHECKING:
    from .database import DatabaseManager

PinKey = Tuple[int, str]


class PinnedMessageRepository:
    """CRUD over ``pinned_messages``."""

    def __init__(self, db: "DatabaseManager") -> None:
        self._db = db

    async def load_all(self) -> Dict[PinKey, int]:
        async with self._db.get_connection() as conn:
            cur = await conn.execute(
                "SELECT chat_id, pin_as, message_id FROM pinned_messages"
            )
            rows = await cur.fetchall()
        return {(int(r[0]), str(r[1])): int(r[2]) for r in rows}

    async def load_dates(self) -> Dict[PinKey, str]:
        """UTC creation date (YYYY-MM-DD) of each pin message (dv-6646):
        ``updated_at`` only moves when ``set`` swaps in a new message id."""
        async with self._db.get_connection() as conn:
            cur = await conn.execute(
                "SELECT chat_id, pin_as, updated_at FROM pinned_messages"
            )
            rows = await cur.fetchall()
        return {(int(r[0]), str(r[1])): str(r[2])[:10] for r in rows if r[2]}

    async def set(self, chat_id: int, pin_as: str, message_id: int) -> None:
        async with self._db.get_connection() as conn:
            await conn.execute(
                """
                INSERT INTO pinned_messages (chat_id, pin_as, message_id)
                VALUES (?, ?, ?)
                ON CONFLICT(chat_id, pin_as) DO UPDATE SET
                    message_id = excluded.message_id,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (chat_id, pin_as, message_id),
            )
            await conn.commit()

    async def delete(self, chat_id: int, pin_as: str) -> None:
        async with self._db.get_connection() as conn:
            await conn.execute(
                "DELETE FROM pinned_messages WHERE chat_id = ? AND pin_as = ?",
                (chat_id, pin_as),
            )
            await conn.commit()
