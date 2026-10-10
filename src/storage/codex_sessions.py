"""Codex thread bookkeeping on the ``sessions`` row (dv-ebde / dv-2a61).

Codex used to keep a second store, ``data/codex_sessions.json``: one entry per
(user, working_dir) pointing at the thread to resume, plus the running token
totals ``turn.completed`` reported, so the next turn could take its delta.
Every Codex thread already has a ``sessions`` row (dv-0c01), so the JSON is
now columns on that row (schema v22):

- ``backend = 'openai_codex'`` marks a Codex thread. Rows from before v22 have
  ``backend`` NULL until :func:`import_codex_sessions_json` tags them.
- ``thread_head = 1`` is the JSON entry's pointer: at most one Codex row per
  (user, project_path) carries it, and that is the thread ``thread_for``
  resumes. It is deliberately independent of ``is_active`` / ``last_used``
  (the JSON pointer ignored both), so the lookup returns exactly the thread
  the JSON did.
- ``thread_total_input`` / ``thread_total_output``: running totals at the end
  of the previous turn on that thread.
- ``cumulative_tokens``: summed per-turn flow on that thread.

``CodexSessionStore`` is the bot's store; ``InMemoryCodexSessionStore`` has
the same async surface for tests and for a Codex backend built without a DB.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

import structlog

if TYPE_CHECKING:
    from .database import DatabaseManager

logger = structlog.get_logger()

CODEX_BACKEND = "openai_codex"
# Rows the import has to CREATE (threads older than dv-0c01, so no row yet)
# go in inactive: the Codex lookup ignores is_active (it follows
# thread_head), but the Claude facade's _find_resumable_session picks the
# newest *active* row in a dir regardless of backend, and must not start
# resuming a months-old Codex thread id in a dir it never saw one in.
IMPORTED_INACTIVE_REASON = "codex_json_import"


def _parse_ts(value: Any) -> datetime:
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value))
        except (TypeError, ValueError):
            return datetime.fromtimestamp(0, tz=UTC)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _entry(row: Any) -> Dict[str, Any]:
    """A row in the dict shape the JSON store's entries had."""
    return {
        "session_id": row["session_id"],
        "user_id": row["user_id"],
        "working_dir": row["project_path"],
        "cumulative_tokens": int(row["cumulative_tokens"] or 0),
        "thread_total_input": int(row["thread_total_input"] or 0),
        "thread_total_output": int(row["thread_total_output"] or 0),
        "updated_at": _parse_ts(row["last_used"]).isoformat(),
    }


_COLUMNS = (
    "session_id, user_id, project_path, last_used, cumulative_tokens, "
    "thread_total_input, thread_total_output"
)
_HEAD = "backend = 'openai_codex' AND thread_head = 1"


class CodexSessionStore:
    """Codex thread index over bot.db ``sessions`` rows."""

    def __init__(self, db: "DatabaseManager") -> None:
        self._db = db

    async def thread_for(self, user_id: int, working_dir: Path) -> Optional[str]:
        """Thread to resume for (user, dir): the row carrying the head flag."""
        info = await self.info_for(user_id, working_dir)
        return info["session_id"] if info else None

    async def info_for(
        self, user_id: int, working_dir: Path
    ) -> Optional[Dict[str, Any]]:
        async with self._db.get_connection() as conn:
            cur = await conn.execute(
                f"SELECT {_COLUMNS} FROM sessions "
                f"WHERE user_id = ? AND project_path = ? AND {_HEAD} "
                "ORDER BY last_used DESC LIMIT 1",
                (user_id, str(Path(working_dir))),
            )
            row = await cur.fetchone()
        return _entry(row) if row else None

    async def thread_totals(
        self, user_id: int, working_dir: Path, thread_id: str
    ) -> Tuple[int, int]:
        """(input, output) running totals recorded at the end of the previous
        turn on ``thread_id``; (0, 0) for a thread never recorded."""
        async with self._db.get_connection() as conn:
            cur = await conn.execute(
                "SELECT thread_total_input, thread_total_output FROM sessions "
                "WHERE session_id = ? AND user_id = ? AND backend = ?",
                (thread_id, user_id, CODEX_BACKEND),
            )
            row = await cur.fetchone()
        if not row:
            return 0, 0
        return int(row[0] or 0), int(row[1] or 0)

    async def record(
        self,
        user_id: int,
        working_dir: Path,
        thread_id: str,
        input_tokens: int,
        output_tokens: int,
        thread_total_input: Optional[int] = None,
        thread_total_output: Optional[int] = None,
    ) -> None:
        """Add one turn's flow (PER TURN) to the thread's counter, remember
        the running totals the CLI reported, and make it the (user, dir) head.

        Every turn that ends with a thread id lands here, whichever path
        started it (resume, fresh, window roll, resume-failure retry, backend
        switch). Upserts: the row normally exists already
        (``_persist_session`` runs first), but a missing one is created
        rather than dropped. Needs the ``users`` row (FK), which
        ``_persist_session`` guarantees.
        """
        now = datetime.now(UTC)
        wd = str(Path(working_dir))
        flow = int(input_tokens) + int(output_tokens)
        async with self._db.get_connection() as conn:
            # Move the pointer: one head per (user, dir), as the JSON had.
            await conn.execute(
                f"UPDATE sessions SET thread_head = 0 WHERE user_id = ? "
                f"AND project_path = ? AND {_HEAD} AND session_id != ?",
                (user_id, wd, thread_id),
            )
            await conn.execute(
                """
                INSERT INTO sessions
                    (session_id, user_id, project_path, created_at, last_used,
                     backend, thread_head, thread_total_input,
                     thread_total_output, cumulative_tokens)
                VALUES (?, ?, ?, ?, ?, ?, 1, COALESCE(?, 0), COALESCE(?, 0), ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    backend = excluded.backend,
                    thread_head = 1,
                    last_used = excluded.last_used,
                    thread_total_input = COALESCE(?, thread_total_input),
                    thread_total_output = COALESCE(?, thread_total_output),
                    cumulative_tokens = cumulative_tokens + ?
                """,
                (
                    thread_id,
                    user_id,
                    wd,
                    now,
                    now,
                    CODEX_BACKEND,
                    thread_total_input,
                    thread_total_output,
                    flow,
                    thread_total_input,
                    thread_total_output,
                    flow,
                ),
            )
            await conn.commit()

    async def clear(self, user_id: int, working_dir: Path) -> None:
        """Drop the resume pointer for (user, dir). Only the flag: the row
        stays active and intact as the FK parent of the thread's messages."""
        async with self._db.get_connection() as conn:
            cur = await conn.execute(
                f"UPDATE sessions SET thread_head = 0 WHERE user_id = ? "
                f"AND project_path = ? AND {_HEAD}",
                (user_id, str(Path(working_dir))),
            )
            await conn.commit()
        logger.info(
            "Codex resume pointer dropped",
            user_id=user_id,
            working_dir=str(working_dir),
            rows=cur.rowcount,
        )

    async def sessions_for_user(self, user_id: int) -> List[Dict[str, Any]]:
        """The (user, dir) heads for ``user_id``, newest first: one per dir,
        like the JSON store's entries."""
        async with self._db.get_connection() as conn:
            cur = await conn.execute(
                f"SELECT {_COLUMNS} FROM sessions WHERE user_id = ? AND {_HEAD} "
                "ORDER BY last_used DESC",
                (user_id,),
            )
            rows = await cur.fetchall()
        return [_entry(r) for r in rows]


class InMemoryCodexSessionStore:
    """Same surface as :class:`CodexSessionStore`, no persistence."""

    def __init__(self) -> None:
        self._threads: Dict[str, Dict[str, Any]] = {}  # thread_id -> entry
        self._heads: Dict[Tuple[int, str], str] = {}  # (user, dir) -> thread

    def _key(self, user_id: int, working_dir: Path) -> Tuple[int, str]:
        return user_id, str(Path(working_dir))

    async def thread_for(self, user_id: int, working_dir: Path) -> Optional[str]:
        return self._heads.get(self._key(user_id, working_dir))

    async def info_for(
        self, user_id: int, working_dir: Path
    ) -> Optional[Dict[str, Any]]:
        tid = self._heads.get(self._key(user_id, working_dir))
        return dict(self._threads[tid]) if tid else None

    async def thread_totals(
        self, user_id: int, working_dir: Path, thread_id: str
    ) -> Tuple[int, int]:
        e = self._threads.get(thread_id)
        if not e or e["user_id"] != user_id:
            return 0, 0
        return e["thread_total_input"], e["thread_total_output"]

    async def record(
        self,
        user_id: int,
        working_dir: Path,
        thread_id: str,
        input_tokens: int,
        output_tokens: int,
        thread_total_input: Optional[int] = None,
        thread_total_output: Optional[int] = None,
    ) -> None:
        key = self._key(user_id, working_dir)
        e = self._threads.setdefault(
            thread_id,
            {
                "session_id": thread_id,
                "user_id": user_id,
                "working_dir": key[1],
                "cumulative_tokens": 0,
                "thread_total_input": 0,
                "thread_total_output": 0,
            },
        )
        e["cumulative_tokens"] += int(input_tokens) + int(output_tokens)
        if thread_total_input is not None:
            e["thread_total_input"] = int(thread_total_input)
        if thread_total_output is not None:
            e["thread_total_output"] = int(thread_total_output)
        e["updated_at"] = datetime.now(UTC).isoformat()
        self._heads[key] = thread_id

    async def clear(self, user_id: int, working_dir: Path) -> None:
        self._heads.pop(self._key(user_id, working_dir), None)

    async def sessions_for_user(self, user_id: int) -> List[Dict[str, Any]]:
        heads = [
            dict(self._threads[tid])
            for (uid, _), tid in self._heads.items()
            if uid == user_id
        ]
        return sorted(heads, key=lambda e: e["updated_at"], reverse=True)


def _scope_from_key(key: str) -> Optional[Tuple[int, str]]:
    """``"<user_id>::<dir>"`` -> (user_id, dir), as ``CodexSessionStore._key``
    in the JSON era built it."""
    uid, sep, wd = str(key).partition("::")
    if not sep or not wd:
        return None
    try:
        return int(uid), str(Path(wd))
    except ValueError:
        return None


def _scope_from_fields(e: Dict[str, Any]) -> Optional[Tuple[int, str]]:
    try:
        uid = int(e["user_id"])
    except (KeyError, TypeError, ValueError):
        return None
    wd = e.get("working_dir")
    return (uid, str(Path(wd))) if wd else None


async def import_codex_sessions_json(
    db: "DatabaseManager", path: Path
) -> Dict[str, int]:
    """Fold the legacy ``codex_sessions.json`` into ``sessions`` rows.

    Read-only on the file (never written, moved or deleted: it is the
    rollback copy for one release). Idempotent: only rows whose ``backend``
    is still NULL are touched, and tagging them is what marks them done, so a
    rerun (every startup) is a no-op, and a thread the new code has since
    recorded or dropped is never overwritten by the stale file.

    Per JSON entry ``"<user>::<dir>"`` (one per (user, dir), the resume head):
    - row exists, untagged: set backend + thread totals + the head flag
      (unless the scope already has a head: newer state wins), and
      cumulative_tokens = max(row, json).
    - row missing: insert it, inactive (see IMPORTED_INACTIVE_REASON; the
      ``users`` row must exist, else skipped).
    - row belongs to another user: skipped.
    - row exists under a different project_path than the key's dir
      (``moved``): tagged, but NOT made a head, because ``thread_for`` keys
      on project_path and a head there would change another scope's lookup.
      That entry loses its resume (a fresh, seeded thread next turn).
    The scope is the JSON *key*, which is what the old lookup matched on.
    ``scripts/check_codex_session_import.py`` proves the lookup parity
    against the live files without writing anything.
    """
    counts = {"tagged": 0, "inserted": 0, "already": 0, "skipped": 0, "moved": 0}
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return counts
    except (OSError, ValueError) as exc:
        logger.warning(
            "Codex session JSON unreadable; import skipped",
            path=str(path),
            error=str(exc),
        )
        return counts
    if not isinstance(raw, dict):
        return counts

    async with db.get_connection() as conn:
        for key, e in raw.items():
            if not isinstance(e, dict):
                counts["skipped"] += 1
                continue
            sid = e.get("session_id")
            # The KEY is what the JSON lookup matched on, so it is the scope.
            # The entry's own working_dir can be stale: the 2026-06 workspace
            # migration rewrote keys but not that field (live: key
            # .../workspaces/channel, working_dir ~/channel-workspace).
            # Fall back to the fields only if the key is odd.
            scope = _scope_from_key(key)
            if scope is None:
                scope = _scope_from_fields(e)
            if not sid or scope is None:
                counts["skipped"] += 1
                continue
            uid, wd = scope
            tin = int(e.get("thread_total_input", 0) or 0)
            tout = int(e.get("thread_total_output", 0) or 0)
            cum = int(e.get("cumulative_tokens", 0) or 0)

            cur = await conn.execute(
                f"SELECT 1 FROM sessions WHERE user_id = ? AND project_path = ? "
                f"AND {_HEAD}",
                (uid, wd),
            )
            has_head = await cur.fetchone() is not None

            cur = await conn.execute(
                "SELECT backend, user_id, project_path FROM sessions "
                "WHERE session_id = ?",
                (sid,),
            )
            row = await cur.fetchone()
            if row is not None:
                if row["backend"] is not None:
                    counts["already"] += 1
                    continue
                if int(row["user_id"]) != uid:
                    logger.warning(
                        "Codex JSON entry owned by another user; skipped",
                        key=key,
                        session_id=sid,
                    )
                    counts["skipped"] += 1
                    continue
                if row["project_path"] != wd:
                    logger.warning(
                        "Codex JSON dir differs from the session row",
                        session_id=sid,
                        json_dir=wd,
                        row_dir=row["project_path"],
                    )
                    counts["moved"] += 1
                head = 0 if has_head or row["project_path"] != wd else 1
                await conn.execute(
                    "UPDATE sessions SET backend = ?, thread_head = ?, "
                    "thread_total_input = ?, thread_total_output = ?, "
                    "cumulative_tokens = MAX(cumulative_tokens, ?) "
                    "WHERE session_id = ? AND backend IS NULL",
                    (CODEX_BACKEND, head, tin, tout, cum, sid),
                )
                counts["tagged"] += 1
                continue

            cur = await conn.execute("SELECT 1 FROM users WHERE user_id = ?", (uid,))
            if await cur.fetchone() is None:
                logger.warning(
                    "Codex JSON entry for unknown user; skipped",
                    session_id=sid,
                    user_id=uid,
                )
                counts["skipped"] += 1
                continue
            ts = _parse_ts(e.get("updated_at"))
            await conn.execute(
                "INSERT INTO sessions (session_id, user_id, project_path, "
                "created_at, last_used, backend, thread_head, "
                "thread_total_input, thread_total_output, cumulative_tokens, "
                "is_active, deactivated_at, deactivated_reason) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, FALSE, ?, ?)",
                (
                    sid,
                    uid,
                    wd,
                    ts,
                    ts,
                    CODEX_BACKEND,
                    0 if has_head else 1,
                    tin,
                    tout,
                    cum,
                    datetime.now(UTC).isoformat(),
                    IMPORTED_INACTIVE_REASON,
                ),
            )
            counts["inserted"] += 1
        await conn.commit()

    if any(counts[k] for k in ("tagged", "inserted", "skipped", "moved")):
        logger.info("Codex session JSON imported", path=str(path), **counts)
    return counts
