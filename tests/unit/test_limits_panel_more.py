"""/limits outage panel details beyond the protected scenarios (dv-7ad9)."""

from unittest.mock import AsyncMock, MagicMock, patch

from src.bot.orchestration.commands.info import InfoCommandsMixin
from src.bot.orchestrator import MessageOrchestrator
from src.config import create_test_config


def test_quota_stop_notice_points_at_limits():
    notice = InfoCommandsMixin._stop_notice(object(), "quota")  # type: ignore[arg-type]
    assert notice and "/limits" in notice


async def test_failed_taskpump_freeze_is_reported_not_hidden(tmp_path):
    orch = MessageOrchestrator(create_test_config(approved_directory=str(tmp_path)), {})
    context = MagicMock()
    context.user_data = {"role": "owner"}
    context.bot_data = {"auth_manager": None}
    query = MagicMock()
    query.from_user.id = 1
    query.edit_message_text = AsyncMock()
    with patch(
        "src.claude.agent_tools.run_repo_script",
        AsyncMock(return_value=(1, "pump is locked")),
    ) as run:
        await orch._handle_limits_callback(query, context, "lim:freeze")
    run.assert_awaited_once_with("taskpump", "freeze", "--by", "owner")
    text = query.edit_message_text.await_args.args[0]
    assert "failed (exit 1)" in text and "pump is locked" in text
