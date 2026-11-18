"""SQL for the devlog channel (dv-fc9d): ``channel_posts`` drafts in bot.db and
the two ``model_calls`` reads the devlog tools need. Sync ``sqlite3`` on a
short connection the caller owns (``src/devlog`` holds no SQL).

The table itself is Alembic's (``migrations/bot`` 0014); ``ensure_schema`` is
the same DDL for a DB without Alembic (tests, scripts).
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

DDL = """
CREATE TABLE IF NOT EXISTS channel_posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile TEXT NOT NULL,
    status TEXT NOT NULL,
    text TEXT NOT NULL,
    headline TEXT,
    image_path TEXT NOT NULL,
    image_sha256 TEXT NOT NULL,
    image_kind TEXT NOT NULL,
    leak_hits TEXT NOT NULL DEFAULT '[]',
    superseded_by INTEGER,
    preview_chat_id INTEGER,
    preview_message_id INTEGER,
    channel_message_id INTEGER,
    created_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP NOT NULL,
    published_at TIMESTAMP
)
"""

_INSERT_COLS = (
    "profile",
    "status",
    "text",
    "headline",
    "image_path",
    "image_sha256",
    "image_kind",
    "leak_hits",
)
_UPDATE_COLS = frozenset(
    {
        "status",
        "leak_hits",
        "superseded_by",
        "preview_chat_id",
        "preview_message_id",
        "channel_message_id",
        "published_at",
    }
)


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(DDL)
    conn.commit()


def get_post(conn: sqlite3.Connection, post_id: int) -> Optional[Dict[str, Any]]:
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM channel_posts WHERE id = ?", (post_id,)
    ).fetchone()
    return dict(row) if row is not None else None


def insert_post(conn: sqlite3.Connection, **values: Any) -> int:
    """One new row; ``values`` = every ``_INSERT_COLS`` column."""
    now = now_iso()
    cols = list(_INSERT_COLS) + ["created_at", "updated_at"]
    params = [values[c] for c in _INSERT_COLS] + [now, now]
    cur = conn.execute(
        f"INSERT INTO channel_posts ({', '.join(cols)})"
        f" VALUES ({', '.join('?' for _ in cols)})",
        params,
    )
    conn.commit()
    return int(cur.lastrowid or 0)


def update_post(
    conn: sqlite3.Connection,
    post_id: int,
    *,
    only_if: Sequence[str] = (),
    **values: Any,
) -> int:
    """Set ``values`` (whitelisted columns) and ``updated_at``; with
    ``only_if``, only while the status is one of those. Returns rowcount."""
    bad = set(values) - _UPDATE_COLS
    if bad:
        raise ValueError(f"not an updatable channel_posts column: {sorted(bad)}")
    sets = [f"{c} = ?" for c in values] + ["updated_at = ?"]
    params: List[Any] = list(values.values()) + [now_iso(), post_id]
    where = "id = ?"
    if only_if:
        where += f" AND status IN ({', '.join('?' for _ in only_if)})"
        params += list(only_if)
    cur = conn.execute(
        f"UPDATE channel_posts SET {', '.join(sets)} WHERE {where}", params
    )
    conn.commit()
    return int(cur.rowcount)


def _ro(db_path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)


def spend_by_profile_job(db_path: Path, since: datetime) -> List[Tuple[Any, ...]]:
    """``(profile, job or purpose, calls, usd, tokens)`` since ``since``, the
    40 costliest groups. Empty when the DB cannot be read."""
    try:
        conn = _ro(db_path)
    except sqlite3.Error:
        return []
    try:
        return list(
            conn.execute(
                "SELECT COALESCE(profile, '-'), COALESCE(job_name, purpose),"
                " COUNT(*), COALESCE(SUM(cost_usd), 0),"
                " COALESCE(SUM(input_tokens + output_tokens), 0)"
                " FROM model_calls WHERE ts >= ?"
                " GROUP BY 1, 2 ORDER BY 4 DESC LIMIT 40",
                (since.strftime("%Y-%m-%d %H:%M:%S"),),
            ).fetchall()
        )
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def purpose_cost_since(
    db_path: Path, purpose: str, since: datetime, *, unknown_cost_usd: float = 0.0
) -> float:
    """Sum of ``cost_usd`` of one purpose's ``model_calls`` rows since ``since``.

    A row with no cost (no reported cost, no pricing rule) counts as
    ``unknown_cost_usd``, so a cap cannot be bypassed by unpriced calls.
    """
    conn = _ro(db_path)
    try:
        row = conn.execute(
            "SELECT COALESCE(SUM(COALESCE(cost_usd, ?)), 0) FROM model_calls"
            " WHERE purpose = ? AND ts >= ?",
            (unknown_cost_usd, purpose, since.strftime("%Y-%m-%d %H:%M:%S")),
        ).fetchone()
    finally:
        conn.close()
    return float(row[0] or 0)
