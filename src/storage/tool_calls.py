"""``tool_calls``: the harness trace's store (dv-2260; replaces ``tool_usage``).

Written by :class:`src.model_router.trace.TraceCollector` through
:meth:`ToolCallRepository.upsert` while a turn runs; linked to the turn's
``model_calls`` row by the router and to its ``messages`` row by the storage
facade. Read by ``/trace``, ``scripts/trace`` and the old ``tool_usage``
readers.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from .database import DatabaseManager

RETENTION_DAYS = 90

_COLUMNS = (
    "tool_use_id",
    "parent_tool_use_id",
    "seq",
    "tool_name",
    "subagent_type",
    "input_short",
    "ok",
    "error",
    "started_at",
    "duration_ms",
    "result_bytes",
    "result_preview",
    "detached",
)


class ToolCallRepository:
    def __init__(self, db: "DatabaseManager") -> None:
        self._db = db

    async def upsert(self, trace_id: str, rows: List[Dict[str, Any]]) -> None:
        """Insert or update rows of one trace, keyed (trace_id, tool_use_id).
        Never touches the links (model_call_id, message_id, session_id)."""
        if not rows:
            return
        cols = ", ".join(("trace_id",) + _COLUMNS)
        marks = ", ".join("?" * (len(_COLUMNS) + 1))
        updates = ", ".join(
            f"{c} = excluded.{c}" for c in _COLUMNS if c not in ("tool_use_id",)
        )
        async with self._db.get_connection() as conn:
            for row in rows:
                await conn.execute(
                    f"INSERT INTO tool_calls ({cols}) VALUES ({marks}) "
                    f"ON CONFLICT (trace_id, tool_use_id) DO UPDATE SET {updates}",
                    (trace_id,) + tuple(row.get(c) for c in _COLUMNS),
                )
            await conn.commit()

    async def link_model_call(
        self, trace_id: str, model_call_id: int, session_id: Optional[str] = None
    ) -> None:
        async with self._db.get_connection() as conn:
            await conn.execute(
                "UPDATE tool_calls SET model_call_id = ?, "
                "session_id = COALESCE(?, session_id) WHERE trace_id = ?",
                (model_call_id, session_id, trace_id),
            )
            await conn.commit()

    async def link_message(
        self, trace_id: str, message_id: int, session_id: str
    ) -> None:
        async with self._db.get_connection() as conn:
            await conn.execute(
                "UPDATE tool_calls SET message_id = ?, session_id = ? "
                "WHERE trace_id = ?",
                (message_id, session_id, trace_id),
            )
            await conn.commit()

    async def for_message(self, message_id: int) -> List[Dict[str, Any]]:
        return await self._rows("message_id = ?", (message_id,))

    async def for_model_call(self, model_call_id: int) -> List[Dict[str, Any]]:
        return await self._rows("model_call_id = ?", (model_call_id,))

    async def for_session(self, session_id: str) -> List[Dict[str, Any]]:
        return await self._rows("session_id = ?", (session_id,))

    async def _rows(self, where: str, args: tuple) -> List[Dict[str, Any]]:
        async with self._db.get_connection() as conn:
            cur = await conn.execute(
                f"SELECT * FROM tool_calls WHERE {where} ORDER BY seq, id", args
            )
            return [dict(r) for r in await cur.fetchall()]

    async def last_turns(self, n: int = 5) -> List[Dict[str, Any]]:
        """The ``n`` newest model calls that have a trace, newest first."""
        async with self._db.get_connection() as conn:
            cur = await conn.execute(
                "SELECT mc.id, mc.ts, mc.purpose, mc.profile, mc.outcome, "
                "COUNT(t.id) AS calls "
                "FROM model_calls mc JOIN tool_calls t ON t.model_call_id = mc.id "
                "GROUP BY mc.id ORDER BY mc.id DESC LIMIT ?",
                (n,),
            )
            return [dict(r) for r in await cur.fetchall()]

    async def model_call_of_message(self, message_id: int) -> Optional[int]:
        async with self._db.get_connection() as conn:
            cur = await conn.execute(
                "SELECT model_call_id FROM tool_calls WHERE message_id = ? "
                "AND model_call_id IS NOT NULL LIMIT 1",
                (message_id,),
            )
            row = await cur.fetchone()
            return int(row[0]) if row else None

    async def prune(self, days: int = RETENTION_DAYS) -> int:
        """Delete rows older than ``days``; returns how many."""
        async with self._db.get_connection() as conn:
            cur = await conn.execute(
                "DELETE FROM tool_calls WHERE started_at < datetime('now', ?)",
                (f"-{int(days)} days",),
            )
            await conn.commit()
            return cur.rowcount or 0

    async def get_tool_stats(self) -> List[Dict[str, Any]]:
        """Per tool: calls, sessions, successes and failures (all callers)."""
        async with self._db.get_connection() as conn:
            cur = await conn.execute(
                "SELECT tool_name, COUNT(*) AS usage_count, "
                "COUNT(DISTINCT session_id) AS sessions_used, "
                "SUM(CASE WHEN ok = 1 THEN 1 ELSE 0 END) AS success_count, "
                "SUM(CASE WHEN ok = 0 THEN 1 ELSE 0 END) AS error_count "
                "FROM tool_calls GROUP BY tool_name ORDER BY usage_count DESC"
            )
            return [dict(r) for r in await cur.fetchall()]

    async def model_call(self, model_call_id: int) -> Optional[Dict[str, Any]]:
        """The ``model_calls`` row a trace belongs to."""
        async with self._db.get_connection() as conn:
            cur = await conn.execute(
                "SELECT * FROM model_calls WHERE id = ?", (model_call_id,)
            )
            row = await cur.fetchone()
            return dict(row) if row else None

    async def message(self, message_id: int) -> Optional[Dict[str, Any]]:
        """Prompt and response of the ``messages`` row a trace belongs to."""
        async with self._db.get_connection() as conn:
            cur = await conn.execute(
                "SELECT prompt, response FROM messages WHERE message_id = ?",
                (message_id,),
            )
            row = await cur.fetchone()
            return dict(row) if row else None
