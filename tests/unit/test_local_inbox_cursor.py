"""dv-fe4d phase 2 (review R2): an edited card reaches a polling TUI.

``/internal/inbox?since=`` is a change cursor (``seq``), not a row id: an ask
card is edited in place when it times out or is answered, and a client whose
cursor is already past the card must get it again, with its buttons gone.
"""

import asyncio
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from src.api.server import create_api_app
from src.claude.confirmation import CALLBACK_PREFIX, ConfirmationManager
from src.events.bus import EventBus
from src.notifications.transport import LOCAL_CHAT_ID, LocalTransport, inbox_since
from src.storage.database import DatabaseManager

INTERNAL = "internal-secret"
TUI = "tui-only-secret"
AUTH = {"Authorization": f"Bearer {INTERNAL}"}
TUI_AUTH = {"Authorization": f"Bearer {TUI}"}


@pytest.fixture
async def db(tmp_path):  # type: ignore[no-untyped-def]
    db = DatabaseManager(str(tmp_path / "local.db"))
    await db.initialize()
    yield db
    await db.close()


def _settings() -> MagicMock:
    s = MagicMock()
    s.development_mode = True
    s.github_webhook_secret = "gh-secret"
    s.webhook_api_secret = INTERNAL
    s.local_tui_secret = TUI
    s.channel = "local"
    s.api_server_port = 8080
    s.debug = False
    return s


def _client(db, timeout: float = 600.0) -> TestClient:  # type: ignore[no-untyped-def]
    tr = LocalTransport(db)

    def buttons(token: str):  # type: ignore[no-untyped-def]
        return [
            ("✅ Approve", f"{CALLBACK_PREFIX}:{token}:y"),
            ("⛔ Deny", f"{CALLBACK_PREFIX}:{token}:n"),
        ]

    async def send(chat_id, text, token):  # type: ignore[no-untyped-def]
        return await tr.send(chat_id, text, parse_mode="HTML", buttons=buttons(token))

    async def edit(chat_id, message_id, text):  # type: ignore[no-untyped-def]
        await tr.edit(chat_id, message_id, text, parse_mode="HTML", buttons=None)

    cm = ConfirmationManager(
        send_prompt=send,
        chat_ids=[LOCAL_CHAT_ID],
        edit_prompt=edit,
        timeout_seconds=timeout,
    )
    return TestClient(
        create_api_app(EventBus(), _settings(), db_manager=db, confirmation_manager=cm)
    )


def _poll(client: TestClient, since: int) -> dict:
    r = client.get(f"/internal/inbox?since={since}", headers=TUI_AUTH)
    assert r.status_code == 200, r.text
    return r.json()


async def _until(pred, timeout: float = 5.0):  # type: ignore[no-untyped-def]
    end = asyncio.get_event_loop().time() + timeout
    while True:
        value = await asyncio.get_event_loop().run_in_executor(None, pred)
        if value or asyncio.get_event_loop().time() > end:
            return value
        await asyncio.sleep(0.05)


def _ask(client: TestClient):  # type: ignore[no-untyped-def]
    return asyncio.get_event_loop().run_in_executor(
        None,
        lambda: client.post(
            "/internal/policy/ask",
            json={"reason": "r", "preview": "p"},
            headers=AUTH,
        ).json(),
    )


async def test_timed_out_ask_comes_back_to_a_cursor_past_it(db, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr("src.model_router.job.ASK_TIMEOUT_SECONDS", 1.0)
    client = _client(db)
    task = _ask(client)
    first = await _until(lambda: _poll(client, 0)["messages"])
    assert len(first) == 1 and first[0]["buttons"]
    ask_id = first[0]["id"]
    cursor = _poll(client, 0)["cursor"]
    assert cursor == first[0]["seq"]

    # Nothing changed yet: the cursor past the ask returns nothing.
    assert _poll(client, cursor)["messages"] == []
    assert await asyncio.wait_for(task, 5) == {"approved": False}

    # Same cursor after the timeout: the edited card, buttons gone.
    again = _poll(client, cursor)
    assert [m["id"] for m in again["messages"]] == [ask_id]
    card = again["messages"][0]
    assert card["edited_at"] and not card["buttons"]
    assert again["cursor"] > cursor
    # ... and once only: the next cursor is past the edit.
    assert _poll(client, again["cursor"])["messages"] == []


async def test_answered_ask_comes_back_without_buttons(db) -> None:  # type: ignore[no-untyped-def]
    client = _client(db)
    task = _ask(client)
    first = await _until(lambda: _poll(client, 0)["messages"])
    cursor = _poll(client, 0)["cursor"]
    data = next(b[1] for b in first[0]["buttons"] if b[1].endswith(":y"))
    r = client.post("/internal/callback", json={"data": data}, headers=TUI_AUTH)
    assert r.json() == {"matched": True}
    assert await asyncio.wait_for(task, 5) == {"approved": True}
    rows = _poll(client, cursor)["messages"]
    assert [m["id"] for m in rows] == [first[0]["id"]]
    assert not rows[0]["buttons"] and "Approved" in rows[0]["text"]


async def test_every_write_moves_the_row_past_all_others(db) -> None:  # type: ignore[no-untyped-def]
    tr = LocalTransport(db)
    a = await tr.send(LOCAL_CHAT_ID, "a")
    b = await tr.send(LOCAL_CHAT_ID, "b")
    rows = await inbox_since(db, 0)
    top = max(m["seq"] for m in rows)

    await tr.edit(LOCAL_CHAT_ID, a, "a2")
    assert [m["id"] for m in await inbox_since(db, top)] == [a]
    top = max(m["seq"] for m in await inbox_since(db, 0))

    await tr.pin(LOCAL_CHAT_ID, b)
    await tr.unpin(LOCAL_CHAT_ID, b)
    assert [m["id"] for m in await inbox_since(db, top)] == [b]
