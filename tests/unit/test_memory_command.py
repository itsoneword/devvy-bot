"""Tests for /memory command handler."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.bot.orchestrator import MessageOrchestrator
from src.config.settings import Settings


@pytest.fixture
def mock_composer() -> MagicMock:
    """Mock PromptComposer: only agent.md is file-backed now (facts live in DB)."""
    composer = MagicMock()
    composer.get_section_sizes.return_value = {"agent.md": 4800}
    return composer


def _fact(content: str, tier: str = "pinned", label=None) -> MagicMock:
    f = MagicMock()
    f.content = content
    f.tier = tier
    f.label = label
    return f


@pytest.fixture
def mock_storage() -> MagicMock:
    """Mock Storage with a memory_repository returning agent-scoped facts."""
    storage = MagicMock()
    repo = MagicMock()

    async def get_facts(agent_id, tier=None, status="active"):
        if tier == "pinned":
            return [_fact("allergic to peanuts", "pinned", "allergies")]
        return [_fact("loves beef stew", "general")]

    repo.get_facts = AsyncMock(side_effect=get_facts)
    storage.memory_repository = repo
    return storage


@pytest.fixture
def mock_update() -> MagicMock:
    update = MagicMock()
    update.message = AsyncMock()
    update.message.reply_text = AsyncMock()
    update.effective_user = MagicMock()
    update.effective_user.id = 42
    return update


def _build_orchestrator() -> MessageOrchestrator:
    """Build a minimal orchestrator for testing individual handlers."""
    settings = MagicMock(spec=Settings)
    settings.agentic_mode = True
    deps: dict = {}
    return MessageOrchestrator(settings=settings, deps=deps)


def _make_mock_session(cumulative_tokens: int = 12500) -> MagicMock:
    """Create a mock ClaudeSession with token tracking fields."""
    session = MagicMock()
    session.session_id = "sess-test"
    session.cumulative_tokens = cumulative_tokens
    session.last_eval_checkpoint = 0
    return session


class TestAgenticMemory:
    """Test /memory command output."""

    async def test_shows_total_token_estimate(
        self, mock_update: MagicMock, mock_composer: MagicMock
    ) -> None:
        """Total tokens should be sum of all sections / 4."""
        context = MagicMock()
        context.bot_data = {"prompt_composer": mock_composer}
        context.user_data = {}

        orchestrator = _build_orchestrator()
        await orchestrator.agentic_memory(mock_update, context)

        call_text = mock_update.message.reply_text.call_args[0][0]
        # agent.md only now: 4800 // 4 = 1200 -> 1.2k
        assert "1.2k tokens" in call_text

    async def test_shows_section_sizes(
        self, mock_update: MagicMock, mock_composer: MagicMock
    ) -> None:
        context = MagicMock()
        context.bot_data = {"prompt_composer": mock_composer}
        context.user_data = {}

        orchestrator = _build_orchestrator()
        await orchestrator.agentic_memory(mock_update, context)

        call_text = mock_update.message.reply_text.call_args[0][0]
        assert "agent.md" in call_text
        # file-backed user.md/focus.md are gone; facts live in the DB now
        assert "user.md" not in call_text
        assert "focus.md" not in call_text

    async def test_shows_db_facts_summary(
        self, mock_update: MagicMock, mock_composer: MagicMock, mock_storage: MagicMock
    ) -> None:
        """When storage is present, /memory lists DB pinned/general facts."""
        context = MagicMock()
        context.bot_data = {
            "prompt_composer": mock_composer,
            "storage": mock_storage,
        }
        context.user_data = {}

        orchestrator = _build_orchestrator()
        await orchestrator.agentic_memory(mock_update, context)

        call_text = mock_update.message.reply_text.call_args[0][0]
        assert "Memory for devops" in call_text
        assert "Pinned: 1" in call_text
        assert "General: 1" in call_text
        assert "allergic to peanuts" in call_text

    async def test_shows_conversation_window_size(
        self, mock_update: MagicMock, mock_composer: MagicMock
    ) -> None:
        """Show the window's REAL context level and the next eval threshold.

        Reads the conversation window, not ClaudeSession.cumulative_tokens —
        that counter sums uncached input only and reads ~2.5x low wherever
        prompt caching is working (dv-9f31).
        """
        session = _make_mock_session(cumulative_tokens=12500)
        claude_integration = MagicMock()
        claude_integration.session_manager.active_sessions = {"sess-test": session}

        window_repo = MagicMock()
        window_repo.eval_interval_tokens = 120_000
        window_repo.refresh_threshold_tokens = 200_000
        window_state = SimpleNamespace(
            window_id=4, last_context_tokens=137_400, last_eval_checkpoint=1
        )

        async def _get_or_create(*_a, **_kw):
            return window_state

        window_repo.get_or_create = _get_or_create

        context = MagicMock()
        context.bot_data = {
            "prompt_composer": mock_composer,
            "claude_integration": claude_integration,
            "window_repo": window_repo,
        }
        context.user_data = {
            "claude_session_id": "sess-test",
            "current_directory": "/p",
        }

        orchestrator = _build_orchestrator()
        await orchestrator.agentic_memory(mock_update, context)

        call_text = mock_update.message.reply_text.call_args[0][0]
        assert "Conversation window 4: 137.4k / 200k" in call_text
        assert "Next evaluator: ~240k tokens" in call_text

    async def test_token_tracker_none_shows_not_available(
        self, mock_update: MagicMock, mock_composer: MagicMock
    ) -> None:
        """When token_tracker is absent from bot_data, show 'not available'."""
        context = MagicMock()
        context.bot_data = {"prompt_composer": mock_composer}
        context.user_data = {}

        orchestrator = _build_orchestrator()
        await orchestrator.agentic_memory(mock_update, context)

        call_text = mock_update.message.reply_text.call_args[0][0]
        assert "Last evaluator: not available" in call_text

    async def test_token_tracker_with_extraction(
        self, mock_update: MagicMock, mock_composer: MagicMock
    ) -> None:
        """When token_tracker is available, show extraction timestamp."""
        session = _make_mock_session(cumulative_tokens=55000)
        claude_integration = MagicMock()
        claude_integration.session_manager.active_sessions = {"sess-test": session}

        mock_extraction = MagicMock()
        mock_extraction.timestamp = datetime(2026, 4, 12, 10, 0, 0, tzinfo=UTC)

        token_tracker = AsyncMock()
        token_tracker.get_last_extraction = AsyncMock(return_value=mock_extraction)

        context = MagicMock()
        context.bot_data = {
            "prompt_composer": mock_composer,
            "claude_integration": claude_integration,
            "token_tracker": token_tracker,
        }
        context.user_data = {"claude_session_id": "sess-test"}

        orchestrator = _build_orchestrator()
        await orchestrator.agentic_memory(mock_update, context)

        call_text = mock_update.message.reply_text.call_args[0][0]
        assert "Last evaluator: 2026-04-12" in call_text

    async def test_no_composer_returns_unavailable(
        self, mock_update: MagicMock
    ) -> None:
        context = MagicMock()
        context.bot_data = {}

        orchestrator = _build_orchestrator()
        await orchestrator.agentic_memory(mock_update, context)

        call_text = mock_update.message.reply_text.call_args[0][0]
        assert "not available" in call_text.lower()
