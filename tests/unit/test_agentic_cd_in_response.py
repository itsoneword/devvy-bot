"""dv-b4f1 regression: a reply containing `cd ` must not become an error.

The agentic text path used to call classic mode's
``_update_working_directory_from_claude_response``, which ended in
``new_path.is_relative_to(settings.approved_directory)``. In denylist mode
(``APPROVED_DIRECTORY`` unset — the live config) that raises ``TypeError``,
which the helper's ``except (ValueError, OSError)`` did not catch, so the
turn's broad handler replaced the model's answer with an error message —
after the tokens had already been paid for.
"""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from src.bot.orchestrator import MessageOrchestrator
from src.config.settings import Settings

_ANSWER = "sure, run cd /tmp then ls"


def _denylist_settings() -> Settings:
    return Settings(
        telegram_bot_token="test:token",
        telegram_bot_username="testbot",
        approved_directory=None,
    )


def _mock_response() -> MagicMock:
    r = MagicMock()
    r.session_id = "sess-1"
    r.content = _ANSWER
    r.tools_used = []
    r.input_tokens = 10
    r.output_tokens = 20
    r.context_tokens = 100
    r.cost = 0.0
    r.error_type = None
    return r


def _harness(settings: Settings, tmp_path: Path):
    claude_integration = MagicMock()
    claude_integration.run_command = AsyncMock(return_value=_mock_response())

    update = MagicMock()
    update.effective_user.id = 123
    update.message.text = "how do I get to /tmp?"
    update.message.message_id = 1
    update.message.message_thread_id = None
    update.message.reply_to_message = None
    update.message.chat.id = 999
    update.message.chat.type = "group"
    update.message.chat.send_action = AsyncMock()
    update.message.reply_text = AsyncMock(return_value=AsyncMock())

    context = MagicMock()
    context.user_data = {"current_directory": tmp_path}
    context.bot_data = {
        "settings": settings,
        "claude_integration": claude_integration,
        "storage": None,
        "rate_limiter": None,
        "audit_logger": None,
        "window_repo": None,
    }
    return update, context


async def test_cd_in_response_returns_the_answer_in_denylist_mode(tmp_path):
    """A `cd /tmp` in the reply must not turn the answer into an error."""
    settings = _denylist_settings()
    orchestrator = MessageOrchestrator(settings, {})
    update, context = _harness(settings, tmp_path)

    await orchestrator.agentic_text(update, context)

    sent = [c.args[0] for c in update.message.reply_text.call_args_list if c.args]
    assert any(_ANSWER in text for text in sent), sent
    assert not any("Claude Process Error" in text for text in sent), sent
    assert not any(text.startswith("❌") for text in sent), sent


async def test_cd_in_response_does_not_move_the_working_directory(tmp_path):
    """Model prose must never relocate the user's working directory."""
    settings = _denylist_settings()
    orchestrator = MessageOrchestrator(settings, {})
    update, context = _harness(settings, tmp_path)

    await orchestrator.agentic_text(update, context)

    assert context.user_data["current_directory"] == tmp_path
