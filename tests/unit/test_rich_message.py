"""Rich Messages (dv-1c98): sendRichMessage with a text-path fallback."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram.error import BadRequest

from src.bot.orchestration.turns import _appended_text
from src.bot.rich_message import MAX_CHARS, fits_rich, try_send_rich
from src.bot.utils.formatting import FormattedMessage
from src.events.bus import EventBus
from src.events.types import AgentResponseEvent
from src.notifications.service import NotificationService
from src.notifications.transport import TelegramTransport

TABLE = "# T\n\n| a | b |\n|---|---|\n| 1 | 2 |\n"


def _bot(result=None, error=None):  # type: ignore[no-untyped-def]
    bot = MagicMock()
    bot.do_api_request = AsyncMock(
        return_value=(
            result if result is not None else {"message_id": 7, "chat": {"id": 5}}
        ),
        side_effect=error,
    )
    bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=9))
    return bot


async def test_rich_ok_sends_markdown_raw() -> None:
    bot = _bot()
    sent = await try_send_rich(
        bot, 5, TABLE, message_thread_id=3, reply_to_message_id=11
    )
    assert (sent.chat_id, sent.message_id) == (5, 7)
    method = bot.do_api_request.call_args.args[0]
    kw = bot.do_api_request.call_args.kwargs["api_kwargs"]
    assert method == "sendRichMessage"
    assert kw["rich_message"] == {"markdown": TABLE} and kw["message_thread_id"] == 3
    assert kw["reply_parameters"]["message_id"] == 11


@pytest.mark.parametrize(
    "error", [BadRequest("method not found"), TimeoutError(), ValueError("x")]
)
async def test_rich_error_returns_none(error: Exception) -> None:
    assert await try_send_rich(_bot(error=error), 5, TABLE) is None


async def test_too_long_falls_back_without_calling() -> None:
    bot = _bot()
    assert await try_send_rich(bot, 5, "x" * (MAX_CHARS + 1)) is None
    assert await try_send_rich(bot, 5, "\n\n".join(["p"] * 600)) is None
    bot.do_api_request.assert_not_called()
    assert fits_rich(TABLE) and not fits_rich("  ")


async def test_result_without_message_id_falls_back() -> None:
    assert await try_send_rich(_bot(result={"ok": 1}), 5, TABLE) is None


def _service(bot, rich: bool) -> NotificationService:  # type: ignore[no-untyped-def]
    return NotificationService(
        EventBus(), transport=TelegramTransport(bot, rich_messages=rich)
    )


def _event(**kw):  # type: ignore[no-untyped-def]
    return AgentResponseEvent(chat_id=5, text=TABLE, parse_mode="markdown", **kw)


async def test_notification_rich_ok_skips_text_path() -> None:
    bot = _bot()
    await _service(bot, True)._rate_limited_send(5, _event())
    bot.do_api_request.assert_awaited_once()
    bot.send_message.assert_not_called()


async def test_notification_rich_error_falls_back_to_html() -> None:
    bot = _bot(error=BadRequest("Method not found"))
    await _service(bot, True)._rate_limited_send(5, _event())
    bot.send_message.assert_awaited_once()
    assert bot.send_message.call_args.kwargs["parse_mode"] == "HTML"


async def test_notification_too_long_falls_back_to_chunks() -> None:
    bot = _bot()
    text = "word " * 8000  # 40000 chars
    ev = AgentResponseEvent(chat_id=5, text=text, parse_mode="markdown")
    await _service(bot, True)._rate_limited_send(5, ev)
    bot.do_api_request.assert_not_called()
    assert bot.send_message.await_count > 1


async def test_notification_flag_off_and_buttons_use_text() -> None:
    bot = _bot()
    await _service(bot, False)._rate_limited_send(5, _event())
    bot.do_api_request.assert_not_called()
    bot.send_message.assert_awaited_once()
    bot2 = _bot()
    ev = _event(buttons=[[{"text": "ok", "callback_data": "x"}]])
    await _service(bot2, True)._rate_limited_send(5, ev)
    bot2.do_api_request.assert_not_called()


def test_appended_text_suffix_and_new_message() -> None:
    before = ["body"]
    assert _appended_text(before, [FormattedMessage("body\n\n---\nm")]) == "\n\n---\nm"
    assert (
        _appended_text(before, [FormattedMessage("body"), FormattedMessage("f")])
        == "\n\nf"
    )
    assert _appended_text(before, [FormattedMessage("body")]) == ""


def test_setting_defaults_on() -> None:
    from src.config.settings import Settings

    assert Settings.model_fields["enable_rich_messages"].default is True
