"""A model switch leaves a marker in the conversation (dv-168e).

Without it the new model reads earlier replies -- resumed session or the
rebuilt window -- as its own. The marker rides the
durable ``pending_notes`` queue the next turn consumes.
"""

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.bot.orchestrator import MessageOrchestrator
from src.bot.window_seed import model_switch_note
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


def _context(storage, workdir: Path, *, backend: str, model=None):
    context = MagicMock()
    context.user_data = {
        "current_directory": workdir,
        "agent_backend": backend,
        "role": "owner",
    }
    if model is not None:
        context.user_data["agent_model"] = model
    context.bot_data = {
        "window_repo": WindowRepository(storage.db_manager),
        "storage": storage,
        "evaluator_service": None,
        "profile_registry": None,
    }
    return context


def _query():
    query = MagicMock()
    query.from_user.id = USER_ID
    query.edit_message_text = AsyncMock()
    return query


async def _switch(storage, tmp_path, data, *, backend, model=None):
    workdir = tmp_path / "ws"
    workdir.mkdir(exist_ok=True)
    orch = MessageOrchestrator(create_test_config(approved_directory=str(workdir)), {})
    context = _context(storage, workdir, backend=backend, model=model)
    await orch._handle_model_callback(_query(), context, data)
    return workdir, context


def test_note_names_both_models():
    note = model_switch_note(("claude", "opus"), ("openai_codex", "openai-codex/x"))
    assert "Claude (opus) -> OpenAI Codex (openai-codex/x)" in note
    assert "were written by Claude (opus), not by you" in note
    assert note.startswith("<model_switch>") and note.endswith("</model_switch>")


def test_default_sentinel_is_labelled_default():
    note = model_switch_note(("claude", None), ("claude", "claude"))
    assert "Claude (default model) -> Claude (default model)" in note


async def test_same_backend_tier_change_queues_note_without_roll(storage, tmp_path):
    workdir, _ = await _switch(
        storage, tmp_path, "model:claude:sonnet", backend="claude", model="opus"
    )
    notes = await storage.pending_notes.consume(USER_ID, str(workdir))
    assert len(notes) == 1
    assert "Claude (opus) -> Claude (sonnet)" in notes[0]
    # No backend change: the window is not rolled, the session resumes.
    repo = WindowRepository(storage.db_manager)
    state = await repo.get_or_create(USER_ID, workdir)
    assert state.pending_seed_window is None


async def test_backend_switch_queues_note_without_roll(storage, tmp_path):
    repo = WindowRepository(storage.db_manager)
    workdir = tmp_path / "ws"
    workdir.mkdir()
    before = await repo.get_or_create(USER_ID, workdir)
    await _switch(
        storage, tmp_path, "model:codex:gpt-5.5", backend="claude", model="opus"
    )
    notes = await storage.pending_notes.consume(USER_ID, str(workdir))
    assert len(notes) == 1
    assert "Claude (opus) -> OpenAI Codex (" in notes[0]
    after = await repo.get_or_create(USER_ID, workdir)
    # dv-168e: no roll -- the next turn rebuilds this same window in full.
    assert after.window_id == before.window_id
    assert after.pending_seed_window == before.window_id
    assert after.force_new_session is True


async def test_reselecting_same_model_queues_nothing(storage, tmp_path):
    workdir, _ = await _switch(
        storage, tmp_path, "model:claude:opus", backend="claude", model="opus"
    )
    assert await storage.pending_notes.consume(USER_ID, str(workdir)) == []


async def test_note_is_consumed_once(storage, tmp_path):
    workdir, _ = await _switch(
        storage, tmp_path, "model:claude:sonnet", backend="claude", model="opus"
    )
    assert len(await storage.pending_notes.consume(USER_ID, str(workdir))) == 1
    assert await storage.pending_notes.consume(USER_ID, str(workdir)) == []
