"""Soft-deleting a session records when and why (schema v21, dv-2a5a)."""

from datetime import UTC, datetime
from pathlib import Path

import aiosqlite
import pytest
import pytest_asyncio

from src.claude.session import ClaudeSession
from src.storage.database import DatabaseManager
from src.storage.models import SessionModel
from src.storage.session_storage import SQLiteSessionStorage


@pytest_asyncio.fixture
async def db_manager(tmp_path):
    manager = DatabaseManager(f"sqlite:///{tmp_path / 'test.db'}")
    await manager.initialize()
    yield manager
    await manager.close()


async def _row(db_manager, session_id):
    async with db_manager.get_connection() as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
        )
        return SessionModel.from_row(await cur.fetchone())


@pytest.mark.asyncio
async def test_delete_session_stamps_reason_and_time(db_manager):
    storage = SQLiteSessionStorage(db_manager)
    await storage.save_session(
        ClaudeSession(
            session_id="s1",
            user_id=1,
            project_path=Path("/p"),
            created_at=datetime.now(UTC),
            last_used=datetime.now(UTC),
        )
    )
    before = await _row(db_manager, "s1")
    assert before.is_active and before.deactivated_at is None
    assert before.deactivated_reason is None

    await storage.delete_session("s1", reason="resume_failed:transcript_missing")

    after = await _row(db_manager, "s1")
    assert not after.is_active
    assert after.deactivated_reason == "resume_failed:transcript_missing"
    assert after.deactivated_at is not None
    assert (datetime.now(UTC) - after.deactivated_at).total_seconds() < 60
    # Retired rows never come back as resumable.
    assert await storage.get_user_sessions(1) == []
