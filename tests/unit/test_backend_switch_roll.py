"""A backend switch must run the same durable transition as a compaction
(dv-a826).

``_maybe_roll_on_switch`` used to ``roll_window`` and then write the seed and
``force_new_session`` into ``context.user_data`` only. That is in-memory (no
persistence configured), so switch -> restart -> next message lost both: the
turn resumed the retired session with no continuity seed, and the outgoing
window never got its final extraction. The 150k path had already been fixed
to persist the intent in ``conversation_windows`` (dv-9f31); the switch path
had not. These tests pin the switch path to ``refresh_window``.
"""

import asyncio
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.bot.orchestration.windowing import consume_pending_seed
from src.bot.orchestrator import MessageOrchestrator
from src.config import create_test_config
from src.storage.facade import Storage
from src.storage.window_repository import WindowRepository

USER_ID = 777


@pytest.fixture
async def storage():
    with tempfile.TemporaryDirectory() as temp_dir:
        st = Storage(f"sqlite:///{Path(temp_dir) / 'bot.db'}")
        await st.initialize()
        yield st
        await st.close()


async def _seed_window(storage, workdir: Path, window_id: int) -> int:
    """One turn in ``window_id`` for ``workdir``; returns its message id.

    ``window_conversation_text`` joins messages to sessions on project_path,
    so the sessions row must carry the same dir the callback resolves.
    """
    async with storage.db_manager.get_connection() as conn:
        await conn.execute("INSERT INTO users (user_id) VALUES (?)", (USER_ID,))
        await conn.execute(
            "INSERT INTO sessions (session_id, user_id, project_path) "
            "VALUES ('s', ?, ?)",
            (USER_ID, str(workdir)),
        )
        cur = await conn.execute(
            "INSERT INTO messages (session_id,user_id,prompt,response,window_id) "
            "VALUES ('s', ?, 'old-q', 'old-a', ?)",
            (USER_ID, window_id),
        )
        await conn.commit()
        return int(cur.lastrowid)


def _context(storage, window_repo, workdir: Path, evaluator, *, backend: str):
    context = MagicMock()
    context.user_data = {
        "current_directory": workdir,
        "agent_backend": backend,
        "role": "owner",
    }
    context.bot_data = {
        "window_repo": window_repo,
        "storage": storage,
        "evaluator_service": evaluator,
        "profile_registry": None,
    }
    return context


def _query():
    query = MagicMock()
    query.from_user.id = USER_ID
    query.edit_message_text = AsyncMock()
    return query


async def _drain_tasks() -> None:
    """Let the fire-and-forget evaluator task run to completion."""
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    if pending:
        await asyncio.gather(*pending)


async def test_switch_persists_transition_and_runs_final_extraction(storage, tmp_path):
    workdir = tmp_path / "ws"
    workdir.mkdir()
    settings = create_test_config(approved_directory=str(workdir))
    window_repo = WindowRepository(storage.db_manager)
    evaluator = MagicMock()
    evaluator.run = AsyncMock(return_value=True)

    before = await window_repo.get_or_create(USER_ID, workdir)
    await _seed_window(storage, workdir, before.window_id)

    orch = MessageOrchestrator(settings, {})
    context = _context(storage, window_repo, workdir, evaluator, backend="openai_codex")

    await orch._handle_model_callback(_query(), context, "model:claude:opus")
    await _drain_tasks()

    # 1. The outgoing window got its final extraction, bounded by ITS
    #    checkpoint cursor and fed the outgoing conversation.
    evaluator.run.assert_awaited_once()
    kwargs = evaluator.run.await_args.kwargs
    assert kwargs["session_id"] == f"{USER_ID}:{workdir}:refresh"
    assert kwargs["since_message_id"] == before.last_checkpoint_message_id
    assert "old-q" in kwargs["conversation_text"]
    assert "old-a" in kwargs["conversation_text"]
    assert kwargs["backend"] == "claude"

    # 2. The transition is armed in the DB, not just user_data.
    after = await window_repo.get_or_create(USER_ID, workdir)
    assert after.window_id == before.window_id + 1
    assert after.active_backend == "claude"
    assert after.pending_seed_window == before.window_id
    assert after.force_new_session is True
    # user_data is still mirrored for the in-process fast path.
    assert context.user_data["pending_seed_window"] == before.window_id
    assert context.user_data["force_new_session"] is True


async def test_switch_transition_survives_a_restart(storage, tmp_path):
    """Acceptance: switch backend, restart, next turn still gets the seed
    tail and force_new. A restart is modelled as a fresh, empty user_data."""
    workdir = tmp_path / "ws"
    workdir.mkdir()
    settings = create_test_config(approved_directory=str(workdir))
    window_repo = WindowRepository(storage.db_manager)

    before = await window_repo.get_or_create(USER_ID, workdir)
    await _seed_window(storage, workdir, before.window_id)

    orch = MessageOrchestrator(settings, {})
    context = _context(storage, window_repo, workdir, None, backend="claude")
    await orch._handle_model_callback(_query(), context, "model:codex:gpt-5.5")
    await _drain_tasks()
    assert context.user_data["agent_backend"] == "openai_codex"

    # --- restart: in-memory state is gone ---
    fresh_user_data: dict = {}

    # What the turn path reads to decide force_new (turns.py, dv-9f31).
    state = await window_repo.get_or_create(USER_ID, workdir)
    assert state.force_new_session is True
    assert state.active_backend == "openai_codex"

    prompt = await consume_pending_seed(
        window_repo,
        fresh_user_data,
        user_id=USER_ID,
        working_dir=workdir,
        user_message="new-msg",
    )
    assert "old-q" in prompt
    assert prompt.rstrip().endswith("new-msg")


async def test_same_backend_is_a_no_op(storage, tmp_path):
    """Re-picking a tier on the current backend must not roll or extract."""
    workdir = tmp_path / "ws"
    workdir.mkdir()
    settings = create_test_config(approved_directory=str(workdir))
    window_repo = WindowRepository(storage.db_manager)
    evaluator = MagicMock()
    evaluator.run = AsyncMock(return_value=True)

    before = await window_repo.get_or_create(USER_ID, workdir)
    orch = MessageOrchestrator(settings, {})
    context = _context(storage, window_repo, workdir, evaluator, backend="claude")

    await orch._handle_model_callback(_query(), context, "model:claude:opus")
    await _drain_tasks()

    after = await window_repo.get_or_create(USER_ID, workdir)
    assert after.window_id == before.window_id
    assert after.force_new_session is False
    assert after.pending_seed_window is None
    evaluator.run.assert_not_awaited()
