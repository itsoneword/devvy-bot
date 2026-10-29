"""Daily repin of the live status post (dv-6646)."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import TimedOut

from src.events.types import AgentResponseEvent
from src.notifications.service import NotificationService
from src.storage.facade import Storage

CHAT = 100
KEY = (CHAT, "pump")


def ev(text="status", repin=True):
    return AgentResponseEvent(chat_id=CHAT, text=text, pin_as="pump", repin=repin)


@pytest.fixture
async def pins(tmp_path):
    storage = Storage(f"sqlite:///{tmp_path / 'bot.db'}")
    await storage.initialize()
    yield storage.pinned_messages
    await storage.close()


@pytest.fixture
def bot():
    b = AsyncMock()
    ids = iter(range(10, 100))
    b.send_message.side_effect = lambda **kw: SimpleNamespace(message_id=next(ids))
    return b


async def make(bot, pins):
    from src.events.bus import EventBus

    svc = NotificationService(event_bus=EventBus(), bot=bot, pin_store=pins)
    await svc._load_pins()
    return svc


async def backdate(pins):
    async with pins._db.get_connection() as conn:
        await conn.execute(
            "UPDATE pinned_messages SET updated_at='2020-01-01 07:00:00'"
        )
        await conn.commit()


async def test_first_pin_is_created_then_same_day_edits(bot, pins):
    svc = await make(bot, pins)
    await svc._send_or_edit_pinned(CHAT, ev(), 5)
    await svc._send_or_edit_pinned(CHAT, ev("again"), 5)
    assert bot.send_message.await_count == 1
    bot.edit_message_text.assert_awaited_once()


async def test_repins_once_per_day_and_edits_go_to_new(bot, pins):
    svc = await make(bot, pins)
    await svc._send_or_edit_pinned(CHAT, ev(), 5)  # id 10
    await backdate(pins)
    svc = await make(bot, pins)  # restart: dates come from the db
    await svc._send_or_edit_pinned(CHAT, ev("new day"), 5)
    assert bot.send_message.await_count == 2
    assert svc._pinned_messages[KEY] == 11
    bot.unpin_chat_message.assert_awaited_once_with(chat_id=CHAT, message_id=10)
    assert bot.pin_chat_message.await_args.kwargs["message_id"] == 11

    # same tick again, and after a restart: no second repin
    await svc._send_or_edit_pinned(CHAT, ev("later"), 5)
    svc2 = await make(bot, pins)
    await svc2._send_or_edit_pinned(CHAT, ev("later 2"), 5)
    assert bot.send_message.await_count == 2
    assert [c.kwargs["message_id"] for c in bot.edit_message_text.await_args_list] == [
        11,
        11,
    ]
    assert await pins.load_all() == {KEY: 11}


async def test_send_failure_keeps_old_pin_and_edits_it(bot, pins):
    svc = await make(bot, pins)
    await svc._send_or_edit_pinned(CHAT, ev(), 5)  # id 10
    await backdate(pins)
    svc = await make(bot, pins)
    bot.send_message.side_effect = TimedOut()
    await svc._send_or_edit_pinned(CHAT, ev("x"), 5)
    assert svc._pinned_messages[KEY] == 10
    bot.unpin_chat_message.assert_not_called()
    assert bot.edit_message_text.await_args.kwargs["message_id"] == 10
    assert await pins.load_all() == {KEY: 10}


async def test_no_repin_without_flag(bot, pins):
    svc = await make(bot, pins)
    await svc._send_or_edit_pinned(CHAT, ev(), 5)
    await backdate(pins)
    svc = await make(bot, pins)
    await svc._send_or_edit_pinned(CHAT, ev(repin=False), 5)
    assert bot.send_message.await_count == 1
