"""``tool_calls``: the harness trace's store (dv-2260; replaces ``tool_usage``).

Written by :class:`src.model_router.trace.TraceCollector` through
:meth:`ToolCallRepository.upsert` while a turn runs; linked to the turn's
``model_calls`` row by the router and to its ``messages`` row by the storage
facade. Read by ``/export``, the stats, the old ``tool_usage`` readers and
the trace viewer (``/trace``, ``scripts/trace``; :meth:`ToolCallRepository.trace`).

Retention: rows older than ``retention_days`` (``TRACE_RETENTION_DAYS``,
default 90) are deleted by :meth:`ToolCallRepository.prune` -- at bot startup
(``Storage.initialize``) and then at most once a day from the write path
(:meth:`ToolCallRepository.prune_if_due`). No scheduler job.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Sequence

import structlog

if TYPE_CHECKING:
    from .database import DatabaseManager

logger = structlog.get_logger()

TRACE_RETENTION_DAYS = 90
_PRUNE_EVERY = timedelta(hours=24)

# Row fields the collector owns; the links (model_call_id, message_id,
# session_id) are set by link_model_call / link_message.
_COLUMNS = (
    "tool_use_id",
    "parent_tool_use_id",
    "seq",
    "tool",
    "subagent_type",
    "input",
    "status",
    "error",
    "started_at",
    "duration_ms",
    "result_bytes",
    "result_preview",
)


class ToolCallRepository:
    def __init__(
        self, db: "DatabaseManager", retention_days: int = TRACE_RETENTION_DAYS
    ) -> None:
        self._db = db
        self.retention_days = retention_days
        self._last_prune: Optional[datetime] = None

    async def upsert(self, trace_id: str, rows: List[Dict[str, Any]]) -> None:
        """Insert or update rows of one trace, keyed (trace_id, tool_use_id).
        Never touches the links."""
        if not rows:
            return
        cols = ", ".join(("trace_id",) + _COLUMNS)
        marks = ", ".join("?" * (len(_COLUMNS) + 1))
        updates = ", ".join(
            f"{c} = excluded.{c}" for c in _COLUMNS if c != "tool_use_id"
        )
        async with self._db.get_connection() as conn:
            for row in rows:
                await conn.execute(
                    f"INSERT INTO tool_calls ({cols}) VALUES ({marks}) "
                    f"ON CONFLICT (trace_id, tool_use_id) DO UPDATE SET {updates}",
                    (trace_id,) + tuple(row.get(c) for c in _COLUMNS),
                )
            await conn.commit()
        await self.prune_if_due()

    async def prune(
        self, days: Optional[int] = None, now: Optional[datetime] = None
    ) -> int:
        """Delete rows whose ``started_at`` is older than ``days``; returns
        how many. ``julianday`` reads both the collector's ISO timestamps and
        the plain ``YYYY-MM-DD HH:MM:SS`` of rows migrated from tool_usage."""
        days = self.retention_days if days is None else days
        now = now or datetime.now(UTC)
        cutoff = (now - timedelta(days=days)).astimezone(UTC)
        async with self._db.get_connection() as conn:
            cur = await conn.execute(
                "DELETE FROM tool_calls WHERE julianday(started_at) < julianday(?)",
                (cutoff.strftime("%Y-%m-%d %H:%M:%S"),),
            )
            await conn.commit()
            deleted = cur.rowcount or 0
        self._last_prune = now
        if deleted:
            logger.info("tool_calls pruned", deleted=deleted, days=days)
        return deleted

    async def prune_if_due(self, now: Optional[datetime] = None) -> int:
        """Prune unless this repository already did in the last 24h."""
        now = now or datetime.now(UTC)
        if self._last_prune is not None and now - self._last_prune < _PRUNE_EVERY:
            return 0
        try:
            return await self.prune(now=now)
        except Exception as exc:  # retention must never break a write
            logger.warning("tool_calls prune failed", error=str(exc))
            self._last_prune = now  # try again tomorrow, not on every write
            return 0

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

    async def for_model_calls(
        self, model_call_ids: Sequence[int]
    ) -> Dict[int, List[Dict[str, Any]]]:
        """Rows of many model calls in one query, grouped by call and ordered
        by ``seq, id``; a call without rows is absent (dv-87d8 exporter)."""
        out: Dict[int, List[Dict[str, Any]]] = {}
        if not model_call_ids:
            return out
        marks = ", ".join("?" * len(model_call_ids))
        async with self._db.get_connection() as conn:
            cur = await conn.execute(
                f"SELECT * FROM tool_calls WHERE model_call_id IN ({marks}) "
                "ORDER BY model_call_id, seq, id",
                tuple(int(i) for i in model_call_ids),
            )
            for r in await cur.fetchall():
                row = dict(r)
                out.setdefault(int(row["model_call_id"]), []).append(row)
        return out

    async def for_session(self, session_id: str) -> List[Dict[str, Any]]:
        return await self._rows("session_id = ?", (session_id,))

    async def latest_turn_id(self, user_id: Optional[int] = None) -> Optional[int]:
        """Id of the newest ``model_calls`` turn (of ``user_id`` if given) --
        what ``/trace`` with no argument shows."""
        where = "kind = 'turn'"
        args: tuple[Any, ...] = ()
        if user_id is not None:
            where, args = where + " AND user_id = ?", (user_id,)
        async with self._db.get_connection() as conn:
            cur = await conn.execute(
                f"SELECT id FROM model_calls WHERE {where} ORDER BY id DESC LIMIT 1",
                args,
            )
            row = await cur.fetchone()
        return int(row[0]) if row else None

    async def recent_turns(self, limit: int = 20) -> List[Dict[str, Any]]:
        """The newest ``model_calls`` turns with their tool-call count, for
        ``scripts/trace --list``."""
        async with self._db.get_connection() as conn:
            cur = await conn.execute(
                "SELECT mc.id, mc.ts, mc.purpose, mc.profile, mc.outcome, "
                "(SELECT COUNT(*) FROM tool_calls tc WHERE tc.model_call_id = mc.id)"
                " AS tool_count FROM model_calls mc WHERE mc.kind = 'turn' "
                "ORDER BY mc.id DESC LIMIT ?",
                (limit,),
            )
            return [dict(r) for r in await cur.fetchall()]

    async def trace(self, model_call_id: int) -> Optional[Dict[str, Any]]:
        """One call's trace for ``/trace`` and ``scripts/trace``: ``{"call":
        model_calls row, "message": messages row or None, "rows": tool_calls
        rows}``; None when there is no such call. The message is the one the
        tool rows are linked to; a turn without tools has no such link, so
        then it is the session's first message saved at or after the call
        (the facade saves it right after the router records the call). A job
        or pump call (no session in ``messages``) has none."""
        async with self._db.get_connection() as conn:
            cur = await conn.execute(
                "SELECT * FROM model_calls WHERE id = ?", (model_call_id,)
            )
            call = await cur.fetchone()
            if call is None:
                return None
            cur = await conn.execute(
                "SELECT * FROM tool_calls WHERE model_call_id = ? ORDER BY seq, id",
                (model_call_id,),
            )
            rows = [dict(r) for r in await cur.fetchall()]
            message_id = next(
                (r["message_id"] for r in rows if r.get("message_id")), None
            )
            if message_id is not None:
                cur = await conn.execute(
                    "SELECT * FROM messages WHERE message_id = ?", (message_id,)
                )
            else:
                cur = await conn.execute(
                    "SELECT * FROM messages WHERE session_id = ? "
                    "AND julianday(timestamp) >= julianday("
                    "(SELECT ts FROM model_calls WHERE id = ?)) - 5.0 / 86400 "
                    "ORDER BY message_id LIMIT 1",
                    (call["session_id"], model_call_id),
                )
            message = await cur.fetchone()  # NULL session_id: no row
        return {
            "call": dict(call),
            "message": dict(message) if message else None,
            "rows": rows,
        }

    async def _rows(self, where: str, args: tuple) -> List[Dict[str, Any]]:
        async with self._db.get_connection() as conn:
            cur = await conn.execute(
                f"SELECT * FROM tool_calls WHERE {where} ORDER BY seq, id", args
            )
            return [dict(r) for r in await cur.fetchall()]

    async def get_tool_stats(self) -> List[Dict[str, Any]]:
        """Per tool: calls, sessions, successes and failures (all callers,
        as tool_usage counted)."""
        async with self._db.get_connection() as conn:
            cur = await conn.execute(
                "SELECT tool AS tool_name, COUNT(*) AS usage_count, "
                "COUNT(DISTINCT session_id) AS sessions_used, "
                "SUM(CASE WHEN status = 'ok' THEN 1 ELSE 0 END) AS success_count, "
                "SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END) AS error_count "
                "FROM tool_calls GROUP BY tool ORDER BY usage_count DESC"
            )
            return [dict(r) for r in await cur.fetchall()]
