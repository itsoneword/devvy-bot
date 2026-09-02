"""Token usage tracking service.

Records per-turn token usage in SQLite and provides query methods
for cumulative session tracking.
"""

from datetime import datetime
from typing import Dict, Optional

import aiosqlite
import structlog

from ..storage.database import DatabaseManager
from ..storage.models import MemoryExtractionModel

logger = structlog.get_logger()


class TokenTracker:
    """Track token usage per session turn."""

    def __init__(self, db: DatabaseManager) -> None:
        self._db = db

    async def record_turn(
        self,
        session_id: str,
        user_id: int,
        input_tokens: int,
        output_tokens: int,
        cumulative_tokens: int,
        cost: float,
    ) -> None:
        """Insert a row into session_tracking."""
        async with self._db.get_connection() as conn:
            await conn.execute(
                "INSERT INTO session_tracking "
                "(session_id, user_id, input_tokens, output_tokens, "
                "cumulative_tokens, cost) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    session_id,
                    user_id,
                    input_tokens,
                    output_tokens,
                    cumulative_tokens,
                    cost,
                ),
            )
            await conn.commit()

        logger.debug(
            "Recorded token turn",
            session_id=session_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cumulative_tokens=cumulative_tokens,
        )

    async def get_session_totals(self, session_id: str) -> Dict[str, int]:
        """Return {input_tokens, output_tokens, total_tokens} for a session."""
        async with self._db.get_connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT COALESCE(SUM(input_tokens), 0) as input_tokens, "
                    "COALESCE(SUM(output_tokens), 0) as output_tokens "
                    "FROM session_tracking WHERE session_id = ?",
                    (session_id,),
                )
            ).fetchone()
            if row:
                return {
                    "input_tokens": row[0],
                    "output_tokens": row[1],
                    "total_tokens": row[0] + row[1],
                }
            return {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}

    async def get_cumulative_for_session(self, session_id: str) -> int:
        """Return the latest cumulative_tokens value for a session."""
        async with self._db.get_connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT cumulative_tokens FROM session_tracking "
                    "WHERE session_id = ? ORDER BY id DESC LIMIT 1",
                    (session_id,),
                )
            ).fetchone()
            return row[0] if row else 0

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

    async def get_usage_by_project(
        self, user_id: int, since: Optional[datetime] = None
    ) -> Dict[str, Dict[str, float]]:
        """Aggregate token/cost usage per project path (profile working_dir).

        Joins the per-turn ``session_tracking`` rows to ``sessions`` (which
        maps a Claude session_id to the working directory it ran in) and
        sums input+output tokens, cost, and turn count per project_path.
        ``since`` filters to turns at/after that UTC timestamp; ``None``
        means all-time. Codex-backend turns aren't recorded in
        session_tracking today, so this only covers Claude usage.
        """
        query = (
            "SELECT s.project_path, "
            "SUM(st.input_tokens + st.output_tokens) as tokens, "
            "SUM(st.cost) as cost, COUNT(*) as turns "
            "FROM session_tracking st "
            "JOIN sessions s ON s.session_id = st.session_id "
            "WHERE st.user_id = ?"
        )
        params: list = [user_id]
        if since is not None:
            query += " AND st.timestamp >= ?"
            params.append(since.strftime("%Y-%m-%d %H:%M:%S"))
        query += " GROUP BY s.project_path"

        async with self._db.get_connection() as conn:
            cur = await conn.execute(query, tuple(params))
            rows = await cur.fetchall()

        return {
            row[0]: {
                "tokens": row[1] or 0,
                "cost": row[2] or 0.0,
                "turns": row[3] or 0,
            }
            for row in rows
        }
