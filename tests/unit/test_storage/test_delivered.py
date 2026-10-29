"""persist_delivered_text: the one write for text sent outside a reply (dv-da50).

Shared by the memory-system notices (main.py), verbatim reminders
(AgentHandler) and the agent's ``notify`` tool.
"""

from pathlib import Path

import pytest

from src.storage.delivered import persist_delivered_text
from src.storage.facade import Storage
from src.storage.window_repository import WindowRepository
from src.utils.turn_locks import TurnLocks

WORKDIR = Path("/tmp/ws-delivered")


@pytest.fixture
async def storage(tmp_path: Path):
    st = Storage(f"sqlite:///{tmp_path / 'bot.db'}")
    await st.initialize()
    yield st
    await st.close()


async def _seed(storage: Storage, user_id: int, session_id: str) -> None:
    async with storage.db_manager.get_connection() as conn:
        await conn.execute("INSERT INTO users (user_id) VALUES (?)", (user_id,))
        await conn.execute(
            "INSERT INTO sessions (session_id, user_id, project_path) "
            "VALUES (?, ?, ?)",
            (session_id, user_id, str(WORKDIR)),
        )
        await conn.commit()


async def _messages(storage: Storage) -> list:
    async with storage.db_manager.get_connection() as conn:
        cur = await conn.execute("SELECT * FROM messages ORDER BY message_id")
        return [dict(r) for r in await cur.fetchall()]


async def test_saves_into_the_users_session_with_the_current_window(storage):
    await _seed(storage, 1, "s-other")
    await _seed(storage, 2, "s-mine")
    windows = WindowRepository(storage.db_manager)

    message_id = await persist_delivered_text(
        storage, windows, 2, WORKDIR, prompt="[p]", response="told you"
    )

    (row,) = await _messages(storage)
    assert row["message_id"] == message_id
    assert (row["session_id"], row["user_id"]) == ("s-mine", 2)
    assert (row["prompt"], row["response"]) == ("[p]", "told you")
    state = await windows.get_or_create(2, WORKDIR)
    assert row["window_id"] == state.window_id


async def test_no_session_in_that_workspace_saves_nothing(storage):
    await _seed(storage, 1, "s-other")

    assert await persist_delivered_text(storage, None, 2, str(WORKDIR), "", "x") is None
    assert await _messages(storage) == []


async def test_busy_names_unlabelled_locks_by_key():
    locks = TurnLocks()
    lock = locks.get(5, "chef")
    assert locks.busy() == []
    await lock.acquire()
    try:
        assert locks.busy() == ["user 5 / chef"]
    finally:
        lock.release()
    assert locks.busy() == []
