"""CRUD over session_tracking (dv-5e4f Phase 0).

Previously raw SQL in src/token/tracker.py (4 of its 5 blocks -- the 5th,
``get_last_extraction``, reads ``memory_extractions`` and moved to
``src/storage/extraction_repository.py`` instead since that table has its
own owner shared with src/evaluator/service.py).
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Dict, Optional

if TYPE_CHECKING:
    from .database import DatabaseManager


class TokenTrackingRepository:
    """Repository for the ``session_tracking`` table."""

    def __init__(self, db: "DatabaseManager") -> None:
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

    async def get_session_totals(self, session_id: str) -> Dict[str, int]:
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
        async with self._db.get_connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT cumulative_tokens FROM session_tracking "
                    "WHERE session_id = ? ORDER BY id DESC LIMIT 1",
                    (session_id,),
                )
            ).fetchone()
        return row[0] if row else 0

    async def get_usage_by_project(
        self, user_id: int, since: Optional[datetime] = None
    ) -> Dict[str, Dict[str, float]]:
        """Aggregate token/cost usage per project path (profile working_dir).
        See src/token/tracker.py for the join rationale (unchanged)."""
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
            row["project_path"]: {
                "tokens": row["tokens"] or 0,
                "cost": row["cost"] or 0.0,
                "turns": row["turns"] or 0,
            }
            for row in rows
        }
