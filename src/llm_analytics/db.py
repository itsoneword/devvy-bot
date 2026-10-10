"""Index DB: own SQLite file, WAL, sync sqlite3 (indexer runs off-thread).

The read API (milestone 3) will add an aiosqlite connection; the indexer's
parse/write path is deliberately sync per the plan.
"""

import sqlite3
from pathlib import Path

from src.storage.schema.migrate import migrate_connection


def connect(path: str | Path) -> sqlite3.Connection:
    """Open the index DB with WAL mode; rows come back as sqlite3.Row."""
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.row_factory = sqlite3.Row
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Bring the index DB schema to head on this connection (Alembic; a
    pre-Alembic DB is bridged and stamped). Idempotent: a no-op when
    current. Schema: ``src/llm_analytics/schema.py`` + ``migrations/
    llm_analytics/`` (dv-5e4f Phase 1)."""
    migrate_connection("llm_analytics", conn)
