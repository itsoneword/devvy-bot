"""Regression test: MessageOrchestrator._window_conversation_text must scope
by working_dir (via sessions.project_path), not just window_id.

Root cause: every profile's window_id counter independently starts at 1
(WindowRepository.get_or_create), so two different profiles for the same
user both have a window_id=1. Before this fix, the conversation-text query
filtered only on (user_id, window_id), so one profile's messages could leak
into another profile's rotation seed / memory-evaluator input — e.g. devops
answering a stale coach conversation about weather/HRV charts instead of the
user's actual current message.
"""

import tempfile
import types

import pytest

from src.bot.orchestrator import MessageOrchestrator
from src.storage.database import DatabaseManager
from src.storage.window_repository import WindowState


@pytest.fixture
async def db():
    d = DatabaseManager(tempfile.mktemp(suffix=".db"))
    await d.initialize()
    return d


async def _seed_session(db, session_id, project_path, user_id=7):
    async with db.get_connection() as conn:
        await conn.execute(
            "INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,)
        )
        await conn.execute(
            "INSERT INTO sessions (session_id, user_id, project_path) "
            "VALUES (?, ?, ?)",
            (session_id, user_id, project_path),
        )
        await conn.commit()


async def test_window_conversation_text_scopes_by_working_dir(db):
    await _seed_session(db, "s-devops", "/devops")
    await _seed_session(db, "s-coach", "/coach")
    async with db.get_connection() as conn:
        await conn.execute(
            "INSERT INTO messages (session_id,user_id,prompt,response,window_id) "
            "VALUES ('s-devops',7,'restart button question','approve/deny',1)"
        )
        await conn.execute(
            "INSERT INTO messages (session_id,user_id,prompt,response,window_id) "
            "VALUES ('s-coach',7,'weather in costa rica','san jose coords',1)"
        )
        await conn.commit()

    class FakeStorage:
        db_manager = db

    context = types.SimpleNamespace(bot_data={"storage": FakeStorage()})
    devops_state = WindowState(7, "/devops", 1, "claude", 0, 0, 0)
    coach_state = WindowState(7, "/coach", 1, "claude", 0, 0, 0)

    devops_text = await MessageOrchestrator._window_conversation_text(
        types.SimpleNamespace(), context, 7, "/devops", devops_state
    )
    assert "restart button question" in devops_text
    assert "costa rica" not in devops_text

    coach_text = await MessageOrchestrator._window_conversation_text(
        types.SimpleNamespace(), context, 7, "/coach", coach_state
    )
    assert "weather in costa rica" in coach_text
    assert "restart button" not in coach_text
