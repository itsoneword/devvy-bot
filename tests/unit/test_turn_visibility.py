"""dv-19a6: queued messages say what they wait behind; background turns show
in the owner's DM; shutdown reports dropped queued messages."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

from src.events.handlers import AgentHandler
from src.utils.turn_locks import TurnLocks, report_dropped_messages


async def test_queue_note_names_holder():
    locks = TurnLocks()
    lock = locks.get(1)
    async with locks.hold(1, "job job-finish"):
        assert "job job-finish" in lock.queue_note()
    async with lock:
        assert "your previous message" in lock.queue_note()


async def test_waiter_cleared_on_acquire_and_pending_while_blocked():
    locks = TurnLocks()
    lock = locks.get(1)
    await lock.acquire()

    async def waiter():
        locks.enqueue(1, 99, "voice")
        async with lock:
            pass

    t = asyncio.create_task(waiter())
    await asyncio.sleep(0.01)
    assert [(w.chat_id, w.kind) for w in locks.pending()] == [(99, "voice")]
    lock.release()
    await t
    assert locks.pending() == []


async def test_shutdown_reports_dropped():
    locks = TurnLocks()
    lock = locks.get(1)
    await lock.acquire()
    locks.enqueue(1, 99, "voice")
    bot = MagicMock()
    bot.send_message = AsyncMock()
    assert await report_dropped_messages(locks, bot) == 1
    kw = bot.send_message.call_args.kwargs
    assert kw["chat_id"] == 99 and "voice" in kw["text"]


async def test_busy_notice_sent_and_removed():
    h = AgentHandler(
        event_bus=MagicMock(),
        claude_integration=MagicMock(),
        default_working_directory=".",
        default_user_id=1,
    )
    sent = MagicMock()
    sent.delete = AsyncMock()
    h.bot = MagicMock()
    h.bot.send_message = AsyncMock(return_value=sent)
    async with h._busy_notice(1, "job x"):
        assert "job x" in h.bot.send_message.call_args.kwargs["text"]
        sent.delete.assert_not_called()
    sent.delete.assert_awaited_once()
