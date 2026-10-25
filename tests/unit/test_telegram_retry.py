"""dv-2e34: Telegram network timeouts are retried and never audited."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram.error import BadRequest, NetworkError, TimedOut

from src.bot.core import ClaudeCodeBot
from src.bot.telegram_retry import is_transient_network_error, with_telegram_retry


async def test_retries_then_succeeds():
    calls = []

    async def call():
        calls.append(1)
        if len(calls) < 3:
            raise TimedOut()
        return "ok"

    assert await with_telegram_retry(call, delays=(0, 0, 0)) == "ok"
    assert len(calls) == 3


async def test_gives_up_and_raises():
    call = AsyncMock(side_effect=TimedOut())
    with pytest.raises(TimedOut):
        await with_telegram_retry(call, delays=(0, 0))
    assert call.await_count == 3


async def test_bad_request_not_retried():
    call = AsyncMock(side_effect=BadRequest("nope"))
    with pytest.raises(BadRequest):
        await with_telegram_retry(call, delays=(0, 0))
    assert call.await_count == 1


def test_classifier():
    assert is_transient_network_error(TimedOut())
    assert is_transient_network_error(NetworkError("x"))
    assert not is_transient_network_error(BadRequest("x"))
    assert not is_transient_network_error(ValueError("x"))


async def test_error_handler_resend_notice_no_audit():
    audit = MagicMock()
    audit.log_security_violation = AsyncMock()
    update = MagicMock()
    update.effective_message.reply_text = AsyncMock()
    context = MagicMock()
    context.error = TimedOut()
    context.bot_data = {"audit_logger": audit}
    bot = ClaudeCodeBot.__new__(ClaudeCodeBot)
    await bot._error_handler(update, context)
    text = update.effective_message.reply_text.await_args.args[0]
    assert "resend" in text
    audit.log_security_violation.assert_not_awaited()
