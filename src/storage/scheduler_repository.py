"""CRUD over scheduled_jobs (dv-5e4f Phase 0).

Previously raw SQL duplicated in src/scheduler/scheduler.py (5 blocks) and
src/bot/orchestration/commands/info.py (1 read, a column-subset duplicate of
``list_active_jobs``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, List

if TYPE_CHECKING:
    from .database import DatabaseManager


class SchedulerRepository:
    """Repository for the ``scheduled_jobs`` table."""

    def __init__(self, db: "DatabaseManager") -> None:
        self._db = db

    async def list_active_jobs(self) -> List[Dict[str, Any]]:
        """Every active job, oldest first -- both the in-process scheduler's
        own startup load and any read-only caller (e.g. the /schedules
        command) use this same query."""
        async with self._db.get_connection() as conn:
            cursor = await conn.execute(
                "SELECT * FROM scheduled_jobs WHERE is_active = 1 ORDER BY created_at"
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def active_job_ids_for_item(self, item_id: int) -> List[str]:
        async with self._db.get_connection() as conn:
            cursor = await conn.execute(
                "SELECT job_id FROM scheduled_jobs WHERE item_id = ? "
                "AND is_active = 1",
                (item_id,),
            )
            return [row[0] for row in await cursor.fetchall()]

    async def save_job(
        self,
        job_id: str,
        job_name: str,
        cron_expression: str,
        prompt: str,
        target_chat_ids_csv: str,
        working_directory: str,
        skill_name: Any,
        created_by: int,
        delivery: str,
        item_id: Any,
        timezone: Any,
        ends_at_iso: Any,
    ) -> None:
        """Insert (or replace) a job definition, active by default. Callers
        own formatting ``target_chat_ids`` to a CSV string and ``ends_at`` to
        an ISO string first -- this just binds already-SQL-ready values."""
        async with self._db.get_connection() as conn:
            await conn.execute(
                "INSERT OR REPLACE INTO scheduled_jobs "
                "(job_id, job_name, cron_expression, prompt, target_chat_ids, "
                "working_directory, skill_name, created_by, is_active, "
                "delivery, item_id, timezone, ends_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)",
                (
                    job_id,
                    job_name,
                    cron_expression,
                    prompt,
                    target_chat_ids_csv,
                    working_directory,
                    skill_name,
                    created_by,
                    delivery,
                    item_id,
                    timezone,
                    ends_at_iso,
                ),
            )
            await conn.commit()

    async def deactivate_job(self, job_id: str) -> None:
        async with self._db.get_connection() as conn:
            await conn.execute(
                "UPDATE scheduled_jobs SET is_active=0 WHERE job_id=?", (job_id,)
            )
            await conn.commit()
