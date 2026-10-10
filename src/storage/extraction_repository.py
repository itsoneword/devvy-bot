"""CRUD over memory_extractions (dv-5e4f Phase 0).

Shared by src/token/tracker.py (read: ``get_last_extraction``) and
src/evaluator/service.py (dedupe read + write), previously each holding its
own raw SQL against the same table.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

import aiosqlite

from .models import MemoryExtractionModel

if TYPE_CHECKING:
    from .database import DatabaseManager


class MemoryExtractionRepository:
    """Repository for the ``memory_extractions`` table."""

    def __init__(self, db: "DatabaseManager") -> None:
        self._db = db

    async def get_last_extraction(
        self, session_id: str
    ) -> Optional[MemoryExtractionModel]:
        """Return the most recent memory extraction for a session."""
        async with self._db.get_connection() as conn:
            conn.row_factory = aiosqlite.Row
            row = await (
                await conn.execute(
                    "SELECT * FROM memory_extractions "
                    "WHERE session_id = ? ORDER BY id DESC LIMIT 1",
                    (session_id,),
                )
            ).fetchone()
            return MemoryExtractionModel.from_row(row) if row else None

    async def already_extracted(self, user_id: int, prompt_hash: str) -> bool:
        """True when this exact prompt has already been extracted
        successfully. Failed/unparseable runs are excluded on purpose --
        those must still be retried on the next trigger."""
        async with self._db.get_connection() as conn:
            cur = await conn.execute(
                "SELECT 1 FROM memory_extractions WHERE user_id=? AND "
                "prompt_hash=? AND validation_status='success' LIMIT 1",
                (user_id, prompt_hash),
            )
            return await cur.fetchone() is not None

    async def record_extraction(
        self,
        session_id: str,
        user_id: int,
        prompt_hash: str,
        raw_response: Optional[str],
        parsed_insights_json: Optional[str],
        validation_status: str,
        target_files_json: Optional[str],
        error_message: Optional[str],
        cumulative_tokens_at_run: int,
        op_stats_json: Optional[str],
    ) -> None:
        """Insert one extraction attempt. Callers own JSON-encoding the
        list/dict fields first (``parsed_insights``, ``target_files``,
        ``op_stats``) -- this just binds already-SQL-ready values."""
        async with self._db.get_connection() as conn:
            await conn.execute(
                "INSERT INTO memory_extractions "
                "(session_id, user_id, prompt_hash, raw_response, "
                "parsed_insights, validation_status, target_files, "
                "error_message, cumulative_tokens_at_run, op_stats) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    session_id,
                    user_id,
                    prompt_hash,
                    raw_response,
                    parsed_insights_json,
                    validation_status,
                    target_files_json,
                    error_message,
                    cumulative_tokens_at_run,
                    op_stats_json,
                ),
            )
            await conn.commit()
