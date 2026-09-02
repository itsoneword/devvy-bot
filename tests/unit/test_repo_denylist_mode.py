"""dv-a1c8: /repo must work with APPROVED_DIRECTORY unset.

`agentic_repo` and the `cd:` callback both read
``settings.approved_directory`` straight into ``base.iterdir()`` / ``base /
name``. In denylist mode — the native service, the shipped configuration —
that value is None, so the one command README documents in the walkthrough
raised AttributeError. Both now resolve the base through
``OrchestratorBase._workspace_base``, which falls back to the active profile's
working_dir.
"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from src.bot.orchestrator import MessageOrchestrator
from src.config.settings import Settings


def _denylist_settings() -> Settings:
    return Settings(
        telegram_bot_token="test:token",
        telegram_bot_username="testbot",
        approved_directory=None,
    )


def _registry(working_dir: Path) -> SimpleNamespace:
    profile = SimpleNamespace(
        slug="devops",
        name="DevOps",
        working_dir=working_dir,
        allows_role=lambda role: True,
    )
    return SimpleNamespace(
        profiles=[profile],
        get_by_slug=lambda slug: profile if slug == "devops" else None,
        get_default=lambda: profile,
    )


def _context(registry) -> MagicMock:
    context = MagicMock()
    context.user_data = {"active_profile": "devops"}
    context.bot_data = {
        "profile_registry": registry,
        "claude_integration": None,
        "audit_logger": None,
        "storage": None,
    }
    return context


async def test_repo_lists_the_active_profile_workspace(tmp_path):
    (tmp_path / "alpha").mkdir()
    (tmp_path / "beta" / ".git").mkdir(parents=True)

    settings = _denylist_settings()
    registry = _registry(tmp_path)
    orchestrator = MessageOrchestrator(settings, {"profile_registry": registry})

    update = MagicMock()
    update.effective_user.id = 1
    update.message.text = "/repo"
    update.message.reply_text = AsyncMock()

    await orchestrator.agentic_repo(update, _context(registry))

    text = update.message.reply_text.call_args[0][0]
    assert "alpha" in text
    assert "beta" in text


async def test_repo_switch_resolves_against_the_profile_workspace(tmp_path):
    (tmp_path / "alpha").mkdir()

    settings = _denylist_settings()
    registry = _registry(tmp_path)
    orchestrator = MessageOrchestrator(settings, {"profile_registry": registry})

    update = MagicMock()
    update.effective_user.id = 1
    update.message.text = "/repo alpha"
    update.message.reply_text = AsyncMock()

    context = _context(registry)
    await orchestrator.agentic_repo(update, context)

    assert context.user_data["current_directory"] == tmp_path / "alpha"


async def test_cd_callback_resolves_against_the_profile_workspace(tmp_path):
    (tmp_path / "alpha").mkdir()

    settings = _denylist_settings()
    registry = _registry(tmp_path)
    orchestrator = MessageOrchestrator(settings, {"profile_registry": registry})

    query = MagicMock()
    query.data = "cd:alpha"
    query.from_user.id = 1
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()

    update = MagicMock()
    update.callback_query = query

    context = _context(registry)
    await orchestrator._agentic_callback(update, context)

    assert context.user_data["current_directory"] == tmp_path / "alpha"


async def test_repo_falls_back_to_home_without_profiles_or_approved_dir():
    """No registry, no approved_directory: still must not raise."""
    settings = _denylist_settings()
    orchestrator = MessageOrchestrator(settings, {})

    context = MagicMock()
    context.user_data = {}
    context.bot_data = {"profile_registry": None}

    assert orchestrator._workspace_base(context) == Path.home()
