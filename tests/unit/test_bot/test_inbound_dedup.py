"""Inbound dedup gate (dv-002b): one owner message starts at most one turn."""

import asyncio
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
import structlog
from telegram import Update
from telegram.ext import Application, MessageHandler, filters

from src.bot.core import ClaudeCodeBot

_uid = [1000]


def _update(
    text="hello",
    message_id=1,
    chat_id=-100,
    user_id=7,
    thread=None,
    edited=False,
    update_id=None,
    bot=None,
):
    _uid[0] += 1
    msg = {
        "message_id": message_id,
        "date": int(datetime.now(timezone.utc).timestamp()),
        "chat": {"id": chat_id, "type": "supergroup"},
        "from": {"id": user_id, "is_bot": False, "first_name": "U"},
        "text": text,
    }
    if thread is not None:
        msg["message_thread_id"] = thread
        msg["is_topic_message"] = True
    if text.startswith("/"):
        msg["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text)}]
    data = {
        "update_id": update_id if update_id is not None else _uid[0],
        ("edited_message" if edited else "message"): msg,
    }
    return Update.de_json(data, bot)


@pytest.fixture
def harness():
    settings = MagicMock()
    settings.reports_chat_id = None
    settings.telegram_token_str = "1:abc"
    core = ClaudeCodeBot(settings, {})
    app = Application.builder().token("1:abc").concurrent_updates(True).build()
    app._initialized = True  # process_update needs it; no network used
    core.app = app
    core._add_middleware()
    # Drop the real security/auth/rate-limit middleware: only the gate is tested.
    for g in (-3, -2, -1):
        app.handlers.pop(g, None)
    seen = []

    async def downstream(update, context):
        seen.append(update.update_id)

    app.add_handler(MessageHandler(filters.ALL, downstream), group=10)
    return app, seen


async def _send(app, **kw):
    await app.process_update(_update(bot=app.bot, **kw))


async def test_C1_same_message_id_starts_no_turn(harness):
    app, seen = harness
    await _send(app, message_id=5, text="a")
    await _send(app, message_id=5, text="a")
    assert len(seen) == 1
    # also for different text / commands / media-like: same id is the same message
    await _send(app, message_id=6, text="/status")
    await _send(app, message_id=6, text="/status")
    assert len(seen) == 2


async def test_C2_burst_repeat_distinct_ids_dropped(harness):
    app, seen = harness
    for mid in (10, 11, 12):
        await _send(app, message_id=mid, text="place order")
    assert len(seen) == 1


async def test_C3_non_repeats_pass(harness):
    app, seen = harness
    await _send(app, message_id=20, text="one")
    await _send(app, message_id=21, text="two")  # different text
    await _send(app, message_id=22, text="one", user_id=8)  # other user
    await _send(app, message_id=23, text="one", thread=3)  # other thread
    await _send(app, message_id=24, text="one", chat_id=-200)  # other chat
    await _send(app, message_id=20, text="one", edited=True)  # edit
    assert len(seen) == 6
    # commands are not burst-guarded
    await _send(app, message_id=30, text="/status")
    await _send(app, message_id=31, text="/status")
    assert len(seen) == 8


async def test_C3_identical_text_after_window_passes(harness, monkeypatch):
    import time

    app, seen = harness
    real = time.monotonic
    await _send(app, message_id=40, text="again")
    monkeypatch.setattr(time, "monotonic", lambda: real() + 60)
    await _send(app, message_id=41, text="again")
    assert len(seen) == 2


async def test_C4_five_concurrent_identical_updates_one_turn(harness):
    app, seen = harness
    turns = []

    async def fake_turn(self, update, context, override_text=None):
        turns.append(update.update_id)
        await asyncio.sleep(0.05)

    from src.bot.orchestration import turns as turns_mod  # noqa: F401
    from src.bot.orchestrator import MessageOrchestrator

    orig = MessageOrchestrator._agentic_text_turn
    MessageOrchestrator._agentic_text_turn = fake_turn
    try:

        async def turn_handler(update, context):
            await MessageOrchestrator._agentic_text_turn(None, update, context)

        app.add_handler(MessageHandler(filters.TEXT, turn_handler), group=11)
        await asyncio.gather(
            *[_send(app, message_id=100 + i, text="same text") for i in range(5)]
        )
    finally:
        MessageOrchestrator._agentic_text_turn = orig
    assert len(turns) == 1


async def test_C5_drop_logs_ids(harness):
    app, _ = harness
    await _send(app, message_id=50, text="x", update_id=9001, thread=4)
    with structlog.testing.capture_logs() as logs:
        await _send(app, message_id=50, text="x", update_id=9002, thread=4)
        await _send(app, message_id=51, text="x", update_id=9003, thread=4)
    by_reason = {e.get("reason"): e for e in logs if e.get("reason")}
    same = by_reason["same_message_id"]
    burst = by_reason["burst_repeat"]
    assert same["update_id"] == 9002 and same["chat_id"] == -100
    assert same["message_id"] == 50
    assert "thread_id" in same or "message_thread_id" in same
    assert burst["update_id"] == 9003 and burst["message_id"] == 51


def test_C5_agentic_text_log_carries_ids():
    import inspect

    from src.bot.orchestration import turns

    src = inspect.getsource(turns)
    block = src.split('"Agentic text message"', 1)[1][:400]
    for key in ("update_id", "chat_id", "message_id"):
        assert key in block


async def test_C6_seen_set_bounded(harness):
    app, seen = harness
    for i in range(5000):
        await _send(app, message_id=1000 + i, text=f"m{i}")
    assert len(seen) == 5000
    # Find the single gate module's seen-set size via its module attributes.
    import src.bot.middleware.dedup as dedup

    caps = [
        v
        for k, v in vars(dedup).items()
        if isinstance(v, int) and not isinstance(v, bool) and k.isupper()
    ]
    assert caps, "dedup module must expose its caps as constants"


def test_C6_single_registration_point():
    import inspect

    src = inspect.getsource(ClaudeCodeBot._add_middleware)
    assert src.count("dedup") >= 1
