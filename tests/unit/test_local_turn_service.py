"""The local adapter uses the durable window lifecycle and interaction store."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from src.chat.local_turns import LocalChatTurns
from src.storage.repositories import MessageRepository
from src.utils.turn_locks import TurnLocks
from tests.unit.window_harness import (
    SESSION_ID,
    USER_ID,
    _make_repo,
    _mock_response,
    _real_message_saver,
    _seed_conversation,
)


async def test_local_turn_rolls_and_seeds_next_turn(tmp_path: Path) -> None:
    repo = await _make_repo(eval_interval=0, refresh=2_000)
    await _seed_conversation(repo, USER_ID, str(tmp_path))
    contexts = iter((2_100, 400))
    seen = []
    locks = TurnLocks()

    async def run_turn(*args, **kwargs):
        assert locks.get(USER_ID, "devops").locked()
        seen.append(kwargs)
        response = _mock_response(context_tokens=next(contexts))
        response.session_id = SESSION_ID
        return response

    backend = MagicMock()
    backend.resumable_session_for = AsyncMock(return_value=SESSION_ID)
    storage = MagicMock()
    storage.db_manager = repo._db
    storage.memory_repository = None
    storage.messages = MessageRepository(repo._db)
    storage.save_claude_interaction = _real_message_saver(repo)
    turns = LocalChatTurns(
        SimpleNamespace(agent_backend="claude", approved_directory=tmp_path),
        {
            "profile_registry": None,
            "backends": {"claude": backend},
            "model_router": SimpleNamespace(run_turn=run_turn),
            "window_repo": repo,
            "storage": storage,
            "turn_locks": locks,
        },
    )
    first = [
        item
        async for item in turns.stream(user_id=USER_ID, profile="devops", text="one")
    ]
    assert any(item["type"] == "window_rolled" for item in first)
    second = [
        item
        async for item in turns.stream(user_id=USER_ID, profile="devops", text="two")
    ]
    assert any(item["type"] == "seeded" for item in second)
    assert seen[1]["force_new"] is True
    assert "<conversation_context>" in seen[1]["prompt"]
    assert seen[1]["prompt"].endswith("two")
    assert storage.save_claude_interaction.await_count == 2


async def test_local_codex_turn_rolls_at_the_model_aware_threshold(
    tmp_path: Path, monkeypatch
) -> None:
    """Review dv-fe4d p5: the TUI uses the same model-aware roll threshold as
    Telegram, so a Codex model with a small window rolls on time."""
    from src.agent_backends.codex_catalog import CODEX_CATALOG, CodexModel

    monkeypatch.setattr(
        CODEX_CATALOG,
        "cached",
        lambda: [
            CodexModel(slug="gpt-5.6-sol", display_name="x", context_window=30_000)
        ],
    )
    # configured 100k; model-safe = 0.8 * 30k = 24k; a 25k turn must roll.
    repo = await _make_repo(eval_interval=0, refresh=100_000)
    await _seed_conversation(repo, USER_ID, str(tmp_path))
    seen = []

    async def run_turn(*args, **kwargs):
        seen.append(kwargs)
        response = _mock_response(context_tokens=25_000)
        response.session_id = SESSION_ID
        return response

    backend = MagicMock()
    backend.resumable_session_for = AsyncMock(return_value=SESSION_ID)
    storage = MagicMock()
    storage.db_manager = repo._db
    storage.memory_repository = None
    storage.messages = MessageRepository(repo._db)
    storage.save_claude_interaction = _real_message_saver(repo)
    turns = LocalChatTurns(
        SimpleNamespace(
            agent_backend="openai_codex", approved_directory=tmp_path, agent_model=None
        ),
        {
            "profile_registry": None,
            "backends": {"openai_codex": backend},
            "model_router": SimpleNamespace(run_turn=run_turn),
            "window_repo": repo,
            "storage": storage,
        },
    )
    events = [
        item
        async for item in turns.stream(user_id=USER_ID, profile="devops", text="one")
    ]
    assert any(item["type"] == "window_rolled" for item in events), events


def test_default_model_ref_matches_telegrams_fallback() -> None:
    from src.chat.turn_service import ChatTurnService

    settings = SimpleNamespace(agent_model=None, claude_model=None)
    assert ChatTurnService.default_model_ref("openai_codex", settings) == (
        "openai-codex/gpt-5.6-sol"
    )
    assert ChatTurnService.default_model_ref("claude", settings) == "CLI default"
