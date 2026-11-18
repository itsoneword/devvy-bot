"""Media messages kept until their file is downloaded (dv-c977).

A voice / photo / document writes a row at receipt, before the first getFile:
the file_id and the raw update JSON. Telegram's getFile can hang for many
minutes (11.11.26); with the row the download is retried in the background
and after a restart, and the turn runs when the file arrives.

Status: ``pending`` (waiting for a retry), ``running`` (claimed by one
worker: the inline handler or the background retrier), then closed as
``done``, ``stopped`` (/stop) or ``failed`` (window over; kept, retryable).
``claim`` is one conditional UPDATE so two workers never run the same turn.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, Dict, List, Optional

import structlog

if TYPE_CHECKING:
    from .database import DatabaseManager

logger = structlog.get_logger()

OPEN_STATUSES = ("pending", "running")
CLOSED_RETENTION_DAYS = 7


class PendingMediaRepository:
    """CRUD over ``pending_media``."""

    def __init__(self, db: "DatabaseManager") -> None:
        self._db = db

    async def add(
        self,
        *,
        chat_id: int,
        thread_id: Optional[int],
        message_id: int,
        user_id: int,
        profile_slug: Optional[str],
        kind: str,
        file_id: str,
        update_json: str,
        sent_at: Optional[float] = None,
        status: str = "pending",
    ) -> int:
        now = time.time()
        async with self._db.get_connection() as conn:
            cur = await conn.execute(
                "INSERT INTO pending_media (chat_id, thread_id, message_id, "
                "user_id, profile_slug, kind, file_id, update_json, status, "
                "sent_at, received_at, window_start) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    chat_id,
                    thread_id,
                    message_id,
                    user_id,
                    profile_slug,
                    kind,
                    file_id,
                    update_json,
                    status,
                    sent_at,
                    now,
                    now,
                ),
            )
            await conn.commit()
            return int(cur.lastrowid or 0)

    async def get(self, row_id: int) -> Optional[Dict[str, Any]]:
        async with self._db.get_connection() as conn:
            cur = await conn.execute(
                "SELECT * FROM pending_media WHERE id = ?", (row_id,)
            )
            row = await cur.fetchone()
            return dict(row) if row is not None else None

    async def _transition(
        self, row_id: int, from_statuses: tuple, sql_set: str, args: tuple = ()
    ) -> bool:
        marks = ",".join("?" for _ in from_statuses)
        async with self._db.get_connection() as conn:
            cur = await conn.execute(
                f"UPDATE pending_media SET {sql_set} "
                f"WHERE id = ? AND status IN ({marks})",
                (*args, row_id, *from_statuses),
            )
            await conn.commit()
            return bool(cur.rowcount and cur.rowcount > 0)

    async def claim(self, row_id: int) -> bool:
        """pending -> running; True only for the one caller that won it."""
        return await self._transition(row_id, ("pending",), "status = 'running'")

    async def release(self, row_id: int) -> bool:
        """running -> pending: the inline worker hands the row to the
        background retrier."""
        return await self._transition(row_id, ("running",), "status = 'pending'")

    async def close(
        self, row_id: int, status: str, error: Optional[str] = None
    ) -> bool:
        """Close an open row as done / stopped / failed."""
        return await self._transition(
            row_id,
            OPEN_STATUSES,
            "status = ?, closed_at = ?, last_error = COALESCE(?, last_error)",
            (status, time.time(), error),
        )

    async def reopen_failed(self, row_id: int) -> bool:
        """failed -> pending with a fresh retry window."""
        return await self._transition(
            row_id,
            ("failed",),
            "status = 'pending', closed_at = NULL, window_start = ?",
            (time.time(),),
        )

    async def note_attempt(self, row_id: int, error: str) -> None:
        async with self._db.get_connection() as conn:
            await conn.execute(
                "UPDATE pending_media SET attempts = attempts + 1, last_error = ? "
                "WHERE id = ?",
                (error[:500], row_id),
            )
            await conn.commit()

    async def set_status_message(self, row_id: int, message_id: int) -> None:
        async with self._db.get_connection() as conn:
            await conn.execute(
                "UPDATE pending_media SET status_message_id = ? WHERE id = ?",
                (message_id, row_id),
            )
            await conn.commit()

    async def list_open(self) -> List[Dict[str, Any]]:
        async with self._db.get_connection() as conn:
            cur = await conn.execute(
                "SELECT * FROM pending_media WHERE status IN ('pending', 'running') "
                "ORDER BY id"
            )
            return [dict(r) for r in await cur.fetchall()]

    async def requeue_running(self) -> int:
        """At start nothing is live: a ``running`` row was cut by the restart."""
        async with self._db.get_connection() as conn:
            cur = await conn.execute(
                "UPDATE pending_media SET status = 'pending' WHERE status = 'running'"
            )
            await conn.commit()
            return int(cur.rowcount or 0)

    async def prune(self, days: int = CLOSED_RETENTION_DAYS) -> int:
        """Delete done / stopped rows older than ``days`` (they hold update
        JSON with captions). ``failed`` rows stay: they are retryable."""
        cutoff = time.time() - days * 86400
        async with self._db.get_connection() as conn:
            cur = await conn.execute(
                "DELETE FROM pending_media WHERE status IN ('done', 'stopped') "
                "AND closed_at IS NOT NULL AND closed_at < ?",
                (cutoff,),
            )
            await conn.commit()
            return int(cur.rowcount or 0)
