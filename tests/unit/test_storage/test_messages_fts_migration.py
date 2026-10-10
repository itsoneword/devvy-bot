"""messages_fts (dv-r3c1; legacy migration 14, now Alembic bot 0002).

Covers the two properties that matter for an external-content FTS5 table:
existing rows get backfilled once, and re-running the migration script is a
no-op rather than double-indexing or erroring.
"""

from datetime import UTC, datetime

import pytest

from src.storage.database import DatabaseManager


@pytest.fixture
async def db(tmp_path):
    manager = DatabaseManager(f"sqlite:///{tmp_path / 'fts.db'}")
    await manager.initialize()
    yield manager
    await manager.close()


async def _insert_message(
    db: DatabaseManager, message_id_hint: str, prompt: str
) -> None:
    async with db.get_connection() as conn:
        await conn.execute(
            "INSERT OR IGNORE INTO users (user_id, telegram_username) VALUES (1, 'x')"
        )
        await conn.execute(
            "INSERT OR IGNORE INTO sessions (session_id, user_id, project_path) "
            "VALUES (?, 1, '/wd')",
            (message_id_hint,),
        )
        await conn.execute(
            "INSERT INTO messages (session_id, user_id, timestamp, prompt, response) "
            "VALUES (?, 1, ?, ?, 'resp')",
            (message_id_hint, datetime.now(UTC), prompt),
        )
        await conn.commit()


async def test_messages_fts_table_and_triggers_exist(db: DatabaseManager):
    async with db.get_connection() as conn:
        cur = await conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='messages_fts'"
        )
        assert await cur.fetchone() is not None
        cur = await conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' "
            "AND name IN ('messages_fts_ai', 'messages_fts_ad', 'messages_fts_au')"
        )
        assert {r[0] for r in await cur.fetchall()} == {
            "messages_fts_ai",
            "messages_fts_ad",
            "messages_fts_au",
        }


async def test_new_row_is_indexed_via_trigger(db: DatabaseManager):
    await _insert_message(db, "s1", "unique searchable phrase")
    async with db.get_connection() as conn:
        cur = await conn.execute(
            "SELECT prompt FROM messages_fts WHERE messages_fts MATCH 'searchable'"
        )
        rows = await cur.fetchall()
        assert [r[0] for r in rows] == ["unique searchable phrase"]


async def test_rerunning_migrations_does_not_duplicate_or_error(db: DatabaseManager):
    """Re-invoking the schema runner on a current DB (Alembic at head) must
    be inert, not a double-index or a crash."""
    await _insert_message(db, "s1", "pre-existing backfilled row")

    # Simulate a second boot against the same (already-migrated) database.
    await db.initialize()

    async with db.get_connection() as conn:
        cur = await conn.execute(
            "SELECT COUNT(*) FROM messages_fts WHERE messages_fts MATCH 'backfilled'"
        )
        row = await cur.fetchone()
        assert row[0] == 1


async def test_deleting_a_message_removes_it_from_the_index(db: DatabaseManager):
    await _insert_message(db, "s1", "message to be deleted soon")
    async with db.get_connection() as conn:
        await conn.execute("DELETE FROM messages WHERE session_id = 's1'")
        await conn.commit()
        cur = await conn.execute(
            "SELECT COUNT(*) FROM messages_fts WHERE messages_fts MATCH 'deleted'"
        )
        row = await cur.fetchone()
        assert row[0] == 0
