import tempfile
from datetime import UTC, datetime

import pytest

from src.storage.database import DatabaseManager
from src.storage.models import MessageModel
from src.storage.repositories import MessageRepository


@pytest.fixture
async def repo():
    db = DatabaseManager(tempfile.mktemp(suffix=".db"))
    await db.initialize()
    return MessageRepository(db)


async def _seed_parents(db, user_id=7, session_id="s1"):
    """messages has FKs to users + sessions; seed them so inserts are valid."""
    async with db.get_connection() as conn:
        await conn.execute("INSERT INTO users (user_id) VALUES (?)", (user_id,))
        await conn.execute(
            "INSERT INTO sessions (session_id, user_id, project_path) "
            "VALUES (?, ?, '/p')",
            (session_id, user_id),
        )
        await conn.commit()


async def test_save_message_persists_window_id(repo):
    await _seed_parents(repo.db)
    mid = await repo.save_message(
        MessageModel(
            message_id=None,
            session_id="s1",
            user_id=7,
            timestamp=datetime.now(UTC),
            prompt="hi",
            response="yo",
            window_id=3,
        )
    )
    rows = await repo.get_session_messages("s1")
    assert rows[0].window_id == 3
    assert mid > 0
