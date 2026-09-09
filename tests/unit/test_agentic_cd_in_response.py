"""dv-b4f1 regression: a reply containing `cd ` must not become an error.

The agentic text path used to call classic mode's
``_update_working_directory_from_claude_response``, which ended in
``new_path.is_relative_to(settings.approved_directory)``. In denylist mode
(``APPROVED_DIRECTORY`` unset, the live config) that raises ``TypeError``,
which the helper's ``except (ValueError, OSError)`` did not catch, so the
turn's broad handler replaced the model's answer with an error message -
after the tokens had already been paid for.

Re-verified 2026-09-02, after dv-c2a4 deleted ``src/bot/handlers/`` outright:
no call site scans the reply for ``cd`` any more, and ``turns.py`` carries a
NOTE forbidding its return. These tests pin the behaviour from the outside
(orchestrator in, Telegram replies out), so they keep holding no matter which
module such a scan might reappear in.
"""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.bot.orchestrator import MessageOrchestrator
from src.config.settings import Settings

_ANSWER = "sure, run cd /tmp then ls"

# Every reply the dead classic regex `(?:^|\n).*?cd\s+([^\s\n]+)` matched
# against the lowercased response. The third has no word boundary before
# `cd`, so an ordinary English sentence was enough to trigger the crash.
_CD_REPLIES = [
    _ANSWER,
    "let me cd /etc and look",
    "the abcd values are fine",
]


@pytest.fixture(autouse=True)
def _denylist_env(monkeypatch):
    """Denylist mode is the live config: APPROVED_DIRECTORY must be unset."""
    monkeypatch.delenv("APPROVED_DIRECTORY", raising=False)


def _denylist_settings() -> Settings:
    settings = Settings(
        telegram_bot_token="test:token",
        telegram_bot_username="testbot",
        approved_directory=None,
    )
    # The crash needed `approved_directory is None`; assert we reproduce it.
    assert settings.approved_directory is None
    return settings


def _mock_response(answer: str = _ANSWER) -> MagicMock:
    r = MagicMock()
    r.session_id = "sess-1"
    r.content = answer
    r.tools_used = []
    r.input_tokens = 10
    r.output_tokens = 20
    r.context_tokens = 100
    r.cost = 0.0
    r.error_type = None
    return r


def _harness(settings: Settings, tmp_path: Path, answer: str = _ANSWER):
    claude_integration = MagicMock()
    claude_integration.run_command = AsyncMock(return_value=_mock_response(answer))

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


@pytest.mark.parametrize("answer", _CD_REPLIES)
async def test_cd_in_response_returns_the_answer_in_denylist_mode(answer, tmp_path):
    """A `cd`-shaped reply must not turn the answer into an error."""
    settings = _denylist_settings()
    orchestrator = MessageOrchestrator(settings, {})
    update, context = _harness(settings, tmp_path, answer)

    await orchestrator.agentic_text(update, context)

    sent = [c.args[0] for c in update.message.reply_text.call_args_list if c.args]
    assert any(answer in text for text in sent), sent
    assert not any("Claude Process Error" in text for text in sent), sent
    assert not any(text.startswith("❌") for text in sent), sent


@pytest.mark.parametrize("answer", _CD_REPLIES)
async def test_cd_in_response_does_not_move_the_working_directory(answer, tmp_path):
    """Model prose must never relocate the user's working directory."""
    settings = _denylist_settings()
    orchestrator = MessageOrchestrator(settings, {})
    update, context = _harness(settings, tmp_path, answer)

    await orchestrator.agentic_text(update, context)

    assert context.user_data["current_directory"] == tmp_path
