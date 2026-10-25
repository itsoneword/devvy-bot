"""Pinned status edges outside a reports topic (dv-3d3d)."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

from telegram.error import TimedOut

from tests.unit.test_notifications.test_reports_routing import (
    OWNER,
    bot,
    deliver,
    make_service,
)

__all__ = ["bot"]  # fixture


async def test_pin_outside_a_topic_does_not_unpin_all(bot: AsyncMock) -> None:
    bot.send_message = AsyncMock(
        return_value=SimpleNamespace(chat=SimpleNamespace(id=OWNER), message_id=3)
    )
    svc = make_service(bot, on=False)
    await deliver(svc, "bgjob:status", pin_as="jobs")
    bot.pin_chat_message.assert_awaited_once()
    bot.unpin_all_forum_topic_messages.assert_not_called()


async def test_timeout_on_edit_keeps_the_one_pin(bot: AsyncMock) -> None:
    bot.send_message = AsyncMock(
        return_value=SimpleNamespace(chat=SimpleNamespace(id=OWNER), message_id=3)
    )
    svc = make_service(bot, on=False)
    await deliver(svc, "bgjob:status", pin_as="jobs")
    bot.edit_message_text = AsyncMock(side_effect=TimedOut())
    await deliver(svc, "bgjob:status", pin_as="jobs")
    assert bot.send_message.await_count == 1
    bot.unpin_chat_message.assert_not_called()
    assert svc._pinned_messages == {(OWNER, "jobs"): 3}
