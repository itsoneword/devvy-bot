"""dv-fe4d phase 2: the transport seam under NotificationService.

Telegram behaviour must be the calls the service made before the seam; the
local inbox must take the same service path (pins, markdown, files).
"""

from __future__ import annotations

import asyncio
import threading
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.claude.confirmation import ConfirmationManager
from src.events.bus import EventBus
from src.events.types import AgentResponseEvent
from src.notifications.service import NotificationService
from src.notifications.transport import (
    LOCAL_CHAT_ID,
    LocalTransport,
    TelegramTransport,
    close_buttons,
    inbox_since,
)
from src.storage.database import DatabaseManager


@pytest.fixture
async def db(tmp_path):  # type: ignore[no-untyped-def]
    db = DatabaseManager(str(tmp_path / "bot.db"))
    await db.initialize()
    yield db
    await db.close()


# --- TelegramTransport: the same Bot calls as before ------------------------


async def test_telegram_send_with_buttons_builds_one_keyboard_row() -> None:
    bot = AsyncMock()
    bot.send_message.return_value = MagicMock(message_id=42)
    tr = TelegramTransport(bot)
    mid = await tr.send(
        7, "<b>ok?</b>", parse_mode="HTML", buttons=[("Yes", "c:t:y"), ("No", "c:t:n")]
    )
    assert mid == 42
    kw = bot.send_message.call_args.kwargs
    assert kw["chat_id"] == 7 and kw["parse_mode"] == "HTML"
    row = kw["reply_markup"].inline_keyboard
    assert len(row) == 1
    assert [(b.text, b.callback_data) for b in row[0]] == [
        ("Yes", "c:t:y"),
        ("No", "c:t:n"),
    ]


async def test_telegram_edit_without_buttons_sends_no_markup() -> None:
    bot = AsyncMock()
    await TelegramTransport(bot).edit(7, 9, "done", parse_mode="HTML")
    kw = bot.edit_message_text.call_args.kwargs
    assert kw == {"chat_id": 7, "message_id": 9, "text": "done", "parse_mode": "HTML"}


async def test_telegram_transport_names_telegram_errors() -> None:
    from telegram.error import BadRequest, TelegramError

    tr = TelegramTransport(AsyncMock())
    assert tr.Error is TelegramError and tr.BadRequest is BadRequest


def test_service_needs_a_bot_or_a_transport() -> None:
    with pytest.raises(ValueError):
        NotificationService(EventBus())


# --- LocalTransport through the service -------------------------------------


async def test_local_markdown_and_files_land_as_rows(db, tmp_path) -> None:  # type: ignore[no-untyped-def]
    img = tmp_path / "chart.png"
    img.write_bytes(b"\x89PNG")
    svc = NotificationService(EventBus(), transport=LocalTransport(db))
    ev = AgentResponseEvent(
        chat_id=LOCAL_CHAT_ID,
        text="**done**",
        parse_mode="markdown",
        image_paths=[str(img)],
    )
    await svc._rate_limited_send(LOCAL_CHAT_ID, ev)
    rows = await inbox_since(db)
    assert rows[0]["text"] == "<b>done</b>" and rows[0]["parse_mode"] == "HTML"
    assert rows[1]["text"] == f"📎 {img}"


async def test_local_empty_pin_text_retires_the_pin(db) -> None:  # type: ignore[no-untyped-def]
    svc = NotificationService(EventBus(), transport=LocalTransport(db))
    pin = AgentResponseEvent(chat_id=1, text="pump: 1 running", pin_as="pump")
    await svc._rate_limited_send(1, pin)
    assert [m["pinned"] for m in await inbox_since(db)] == [True]
    await svc._rate_limited_send(
        1, AgentResponseEvent(chat_id=1, text="", pin_as="pump")
    )
    assert await inbox_since(db) == []


async def test_local_edit_of_a_missing_row_raises_the_transport_error(db) -> None:  # type: ignore[no-untyped-def]
    tr = LocalTransport(db)
    with pytest.raises(tr.Error, match="not found"):
        await tr.edit(1, 999, "x")


async def test_close_buttons_strips_the_card_and_appends_the_verdict(db) -> None:  # type: ignore[no-untyped-def]
    tr = LocalTransport(db)
    await tr.send(
        1, "ask", buttons=[("Approve", "confirm:abc:y"), ("Deny", "confirm:abc:n")]
    )
    assert await close_buttons(db, "confirm:abc:y", "✅ Approved") is True
    row = (await inbox_since(db))[0]
    assert row["buttons"] == [] and row["text"].endswith("✅ Approved</b>")
    assert row["edited_at"]
    assert await close_buttons(db, "confirm:abc:y", "✅ Approved") is False


# --- the bus and the resolve, as a headless server uses them ----------------


async def test_service_starts_and_stops_a_bus_nobody_started(db) -> None:  # type: ignore[no-untyped-def]
    bus = EventBus()
    svc = NotificationService(bus, transport=LocalTransport(db))
    await svc.start()
    assert bus.running
    await svc.stop()
    assert not bus.running


async def test_service_leaves_a_running_bus_alone(db) -> None:  # type: ignore[no-untyped-def]
    bus = EventBus()
    await bus.start()
    svc = NotificationService(bus, transport=LocalTransport(db))
    await svc.start()
    await svc.stop()
    assert bus.running  # main owns it
    await bus.stop()


async def test_resolve_from_another_thread_wakes_the_waiting_ask() -> None:
    tokens: list[str] = []

    async def send(chat_id: int, text: str, token: str) -> int:
        tokens.append(token)
        return 1

    cm = ConfirmationManager(send_prompt=send, chat_ids=[1], timeout_seconds=30)
    ask = asyncio.create_task(cm.request("r", "p"))
    while not tokens:
        await asyncio.sleep(0.01)
    # As an API request served on its own loop/thread resolves it.
    t = threading.Thread(target=lambda: asyncio.run(_resolve(cm, tokens[0])))
    t.start()
    assert await asyncio.wait_for(ask, 5) is True
    t.join(5)


async def _resolve(cm: ConfirmationManager, token: str) -> None:
    assert cm.resolve(token, True) is True
