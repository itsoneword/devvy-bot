"""Build a fresh (empty) copy of each of the 4 SQLite DBs this repo owns,
via the SAME init/migration code the running bot uses.

Shared by:
- tests/unit/test_storage/test_legacy_schema_fixtures.py (dv-5e4f Phase 0.1,
  item 1): asserts a fresh init still matches the committed structural
  fixture under tests/fixtures/legacy_schema/.
- the golden-master seed fixture (Phase 0.1, item 2): builds on top of a
  fresh DB before inserting synthetic rows.
- scripts/gen_legacy_schema_fixtures.py: one-off generator for the committed
  fixtures themselves.

Since dv-5e4f Phase 1 every one of these goes through Alembic (the same
``ensure_schema`` / ``init_db`` calls the app makes). The frozen pre-Alembic
replay lives in ``src.storage.schema.legacy.build_legacy``.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from src.channel_intel.db import connect as channel_intel_connect
from src.channel_intel.db import init_db as channel_intel_init_db
from src.llm_analytics.db import connect as llm_analytics_connect
from src.llm_analytics.db import init_db as llm_analytics_init_db
from src.storage.database import DatabaseManager
from src.storage.user_data import UserDataManager

DB_NAMES = ("bot", "data", "llm_analytics", "channel_intel")


async def build_fresh_bot_db(path: Path) -> DatabaseManager:
    """bot.db: DatabaseManager.initialize -> Alembic (migrations/bot)."""
    manager = DatabaseManager(f"sqlite:///{path}")
    await manager.initialize()
    return manager


async def build_fresh_data_db(path: Path) -> UserDataManager:
    """data.db: UserDataManager (DatabaseManager subclass), own Alembic
    history (migrations/data), plus the py_casefold connection hook."""
    manager = UserDataManager(f"sqlite:///{path}")
    await manager.initialize()
    return manager


async def build_fresh_channel_intel_db(path: Path) -> None:
    """channel_intel.db: src.channel_intel.db.init_db -> Alembic on the
    caller's aiosqlite connection."""
    conn = await channel_intel_connect(path)
    try:
        await channel_intel_init_db(conn)
    finally:
        await conn.close()


def build_fresh_llm_analytics_db(path: Path) -> None:
    """llm_analytics.db: sync sqlite3, src.llm_analytics.db.init_db ->
    Alembic on the caller's connection."""
    conn: sqlite3.Connection = llm_analytics_connect(path)
    try:
        llm_analytics_init_db(conn)
    finally:
        conn.close()


async def build_all_fresh(root: Path) -> dict[str, Path]:
    """Build fresh copies of all 4 DBs under ``root`` and return their paths,
    keyed by name in DB_NAMES. Closes every manager/connection before
    returning so the files are safe to snapshot immediately."""
    paths = {name: root / f"{name}.db" for name in DB_NAMES}

    bot_mgr = await build_fresh_bot_db(paths["bot"])
    await bot_mgr.close()

    data_mgr = await build_fresh_data_db(paths["data"])
    await data_mgr.close()

    build_fresh_llm_analytics_db(paths["llm_analytics"])
    await build_fresh_channel_intel_db(paths["channel_intel"])

    return paths
