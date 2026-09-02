"""Shared task board: tasks any profile (or the user) can create and see.

Mirrors src/storage/memory_repository.py. Tasks live in one common space --
there is no per-agent scoping; `created_by` / `assignee` are informational
profile slugs (or 'user').
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Dict, List, Optional

import structlog

if TYPE_CHECKING:
    from .database import DatabaseManager

logger = structlog.get_logger()


@dataclass
class AgentTask:
    id: int
    title: str
    body: Optional[str]
    created_by: str
    assignee: Optional[str]
    due_at: Optional[datetime]
    status: str
    created_at: Optional[datetime]

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        for key in ("due_at", "created_at"):
            if isinstance(d[key], datetime):
                d[key] = d[key].isoformat()
        return d


_COLUMNS = "id, title, body, created_by, assignee, due_at, status, created_at"


def _coerce_dt(val: Any) -> Optional[datetime]:
    if val is None or isinstance(val, datetime):
        return val
    if isinstance(val, str):
        try:
            return datetime.fromisoformat(val)
        except ValueError:
            return None
    return None


def _from_row(row: Any) -> AgentTask:
    return AgentTask(
        id=row[0],
        title=row[1],
        body=row[2],
        created_by=row[3],
        assignee=row[4],
        due_at=_coerce_dt(row[5]),
        status=row[6],
        created_at=_coerce_dt(row[7]),
    )


class TaskRepository:
    """CRUD over the shared agent_tasks table."""

    def __init__(self, db: "DatabaseManager") -> None:
        self._db = db

    async def add_task(
        self,
        title: str,
        body: Optional[str] = None,
        created_by: str = "user",
        assignee: Optional[str] = None,
        due_at: Optional[datetime] = None,
    ) -> int:
        async with self._db.get_connection() as conn:
            cur = await conn.execute(
                "INSERT INTO agent_tasks (title, body, created_by, assignee, due_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (title, body, created_by, assignee, due_at),
            )
            await conn.commit()
            return int(cur.lastrowid or 0)

    async def get_task(self, task_id: int) -> Optional[AgentTask]:
        async with self._db.get_connection() as conn:
            cur = await conn.execute(
                f"SELECT {_COLUMNS} FROM agent_tasks WHERE id=?", (task_id,)
            )
            row = await cur.fetchone()
            return _from_row(row) if row else None

    async def list_tasks(
        self, status: Optional[str] = "open", limit: int = 50
    ) -> List[AgentTask]:
        sql = f"SELECT {_COLUMNS} FROM agent_tasks"
        params: list = []
        if status:
            sql += " WHERE status=?"
            params.append(status)
        sql += (
            " ORDER BY CASE WHEN due_at IS NULL THEN 1 ELSE 0 END,"
            " due_at ASC, id DESC LIMIT ?"
        )
        params.append(limit)
        async with self._db.get_connection() as conn:
            cur = await conn.execute(sql, tuple(params))
            return [_from_row(row) for row in await cur.fetchall()]

    async def set_status(self, task_id: int, status: str) -> bool:
        now = datetime.now(UTC).isoformat()
        async with self._db.get_connection() as conn:
            cur = await conn.execute(
                "UPDATE agent_tasks SET status=?, updated_at=? WHERE id=?",
                (status, now, task_id),
            )
            await conn.commit()
            return cur.rowcount > 0
