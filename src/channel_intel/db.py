"""Channel KB database: own SQLite file, separate from the bot DB."""

import sqlite3
from datetime import datetime
from pathlib import Path

import aiosqlite

from src.storage.schema.migrate import migrate_connection

sqlite3.register_adapter(datetime, lambda v: v.isoformat())
sqlite3.register_converter("TIMESTAMP", lambda b: datetime.fromisoformat(b.decode()))


async def connect(path: str | Path) -> aiosqlite.Connection:
    """Open a channel-KB connection with WAL mode and FK enforcement."""
    conn = await aiosqlite.connect(str(path), detect_types=sqlite3.PARSE_DECLTYPES)
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = aiosqlite.Row
    return conn


async def init_db(conn: aiosqlite.Connection) -> None:
    """Bring the channel-KB schema to head on this connection (Alembic; a
    pre-Alembic DB is bridged and stamped). Idempotent, so the daemon
    survives restarts. Schema: ``src/channel_intel/schema.py`` +
    ``migrations/channel_intel/`` (dv-5e4f Phase 1).

    The runner is sync sqlite3, so it runs ON aiosqlite's own worker thread
    against the underlying connection (works for ``:memory:`` too, where a
    second connection would see a different, empty DB).
    """
    await conn._execute(migrate_connection, "channel_intel", conn._conn)
