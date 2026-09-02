"""One-shot notes injected into the next agent turn in a workspace.

Written when something user-visible happened outside the agent's transcript
(currently: a verbatim scheduled reminder delivered straight to Telegram).
The next turn in the same (user_id, working_dir) scope prepends the note so
the agent knows what the user is replying to; consumption deletes the rows.
Scoped by working_dir so a coach note can never leak into a chef turn.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, List

import structlog

if TYPE_CHECKING:
    from .database import DatabaseManager

logger = structlog.get_logger()


class PendingNoteRepository:
    """Add/consume over the pending_notes table. Never raises — a note
    failure must not break reminder delivery or the next turn."""

    def __init__(self, db: "DatabaseManager") -> None:
        self._db = db

    async def add(self, user_id: int, working_dir: str, note: str) -> None:
        try:
            async with self._db.get_connection() as conn:
                await conn.execute(
                    "INSERT INTO pending_notes (user_id, working_dir, note) "
                    "VALUES (?, ?, ?)",
                    (user_id, working_dir, note),
                )
                await conn.commit()
        except Exception as e:
            logger.warning(
                "pending note write failed",
                user_id=user_id,
                working_dir=working_dir,
                error=str(e),
            )

    async def consume(self, user_id: int, working_dir: str) -> List[str]:
        """Return all notes for the scope, oldest first, deleting them."""
        try:
            async with self._db.get_connection() as conn:
                cur = await conn.execute(
                    "SELECT id, note FROM pending_notes "
                    "WHERE user_id = ? AND working_dir = ? ORDER BY id",
                    (user_id, working_dir),
                )
                rows = await cur.fetchall()
                if not rows:
                    return []
                await conn.execute(
                    "DELETE FROM pending_notes "
                    "WHERE user_id = ? AND working_dir = ?",
                    (user_id, working_dir),
                )
                await conn.commit()
                return [row[1] for row in rows]
        except Exception as e:
            logger.warning(
                "pending note consume failed",
                user_id=user_id,
                working_dir=working_dir,
                error=str(e),
            )
            return []
