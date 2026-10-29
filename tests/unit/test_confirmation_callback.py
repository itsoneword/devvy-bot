"""Approve/deny button must give visible feedback (owner report 2026-10-11)."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock

from telegram import Message

from src.bot.orchestration.commands.admin import AdminCommandsMixin


def _update(data: str) -> Any:
    # A real prompt: HTML-rendered, preview contains "<" and "&" once decoded.
    text = "Confirm action\nfile deletion (rm)\n\nrm -rf x 2>&1 && echo <ok>"
    msg = MagicMock(spec=Message)
    msg.text = text
    msg.text_html = (
        "Confirm action\n<b>file deletion (rm)</b>\n\n"
        "<pre>rm -rf x 2&gt;&amp;1 &amp;&amp; echo &lt;ok&gt;</pre>"
    )
    query = MagicMock()
    query.data = data
    query.message = msg
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    update = MagicMock()
    update.callback_query = query
    return update


def _context(matched: bool) -> Any:
    cm = MagicMock()
    cm.resolve.return_value = matched
    ctx = MagicMock()
    ctx.bot_data = {"confirmation_manager": cm}
    return ctx


async def test_approve_shows_toast_and_edits_with_escaped_html() -> None:
    update = _update("confirm:abc:y")
    await AdminCommandsMixin._confirmation_callback(MagicMock(), update, _context(True))
    q = update.callback_query
    q.answer.assert_awaited_once_with("✅ Approved")
    sent = q.edit_message_text.await_args.args[0]
    assert "&lt;ok&gt;" in sent and "<ok>" not in sent
    assert sent.endswith("<b>✅ Approved</b>")


async def test_deny_toast() -> None:
    update = _update("confirm:abc:n")
    await AdminCommandsMixin._confirmation_callback(MagicMock(), update, _context(True))
    update.callback_query.answer.assert_awaited_once_with("⛔ Denied")


async def test_stale_tap_alerts_and_does_not_edit() -> None:
    update = _update("confirm:abc:y")
    await AdminCommandsMixin._confirmation_callback(
        MagicMock(), update, _context(False)
    )
    q = update.callback_query
    assert q.answer.await_args.kwargs.get("show_alert") is True
    q.edit_message_text.assert_not_awaited()
