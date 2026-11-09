"""dv-fe4d phase 3: what the Pump TUI needs from the inbox beyond phase 2.

Two pins live in the local inbox (the pump's "pump" and bgjob's "jobs"), so
an inbox row says which pin it is; the client keeps the inbox across polls,
so an ask edited after its first poll drops out of the open asks.
"""

import subprocess

import pytest

from src.notifications.transport import LOCAL_CHAT_ID, LocalTransport, inbox_since
from src.storage.database import DatabaseManager
from src.storage.pinned_messages import PinnedMessageRepository
from src.tui.client import PumpClient


@pytest.fixture
async def db(tmp_path):  # type: ignore[no-untyped-def]
    db = DatabaseManager(str(tmp_path / "local.db"))
    await db.initialize()
    yield db
    await db.close()


async def test_inbox_rows_name_their_pin(db) -> None:  # type: ignore[no-untyped-def]
    tr, pins = LocalTransport(db), PinnedMessageRepository(db)
    for key, text in (("pump", "pump: idle"), ("jobs", "jobs: 1 running")):
        mid = await tr.send(LOCAL_CHAT_ID, text)
        await tr.pin(LOCAL_CHAT_ID, mid)
        await pins.set(LOCAL_CHAT_ID, key, mid)
    plain = await tr.send(LOCAL_CHAT_ID, "hello")
    rows = {r["id"]: r for r in await inbox_since(db, 0)}
    assert sorted(r["pin_as"] for r in rows.values() if r["pinned"]) == ["jobs", "pump"]
    assert rows[plain]["pin_as"] is None


class SeqApi:
    """Returns one inbox reply per poll, in order."""

    def __init__(self, *replies):  # type: ignore[no-untyped-def]
        self.replies = list(replies)
        self.sinces: list = []

    def get(self, path, **params):  # type: ignore[no-untyped-def]
        self.sinces.append(params.get("since"))
        return self.replies.pop(0) if self.replies else {"messages": [], "cursor": 0}

    def post(self, path, json):  # type: ignore[no-untyped-def]
        return {}


def _client(api) -> PumpClient:  # type: ignore[no-untyped-def]
    return PumpClient(
        run=lambda argv: subprocess.CompletedProcess(argv, 0, "", ""), api=api
    )


def test_pump_pin_is_the_pin_keyed_pump_not_the_jobs_pin() -> None:
    msgs = [
        {
            "id": 1,
            "text": "pump: 2 running",
            "pinned": True,
            "pin_as": "pump",
            "buttons": [],
        },
        {"id": 2, "text": "jobs: 3", "pinned": True, "pin_as": "jobs", "buttons": []},
    ]
    c = _client(SeqApi({"messages": msgs, "cursor": 2}))
    assert c.pump_pin() == "pump: 2 running"


def test_an_ask_edited_after_the_first_poll_is_no_longer_open() -> None:
    ask = {
        "id": 3,
        "text": "rm?",
        "pinned": False,
        "buttons": [["✅", "confirm:t:y"], ["⛔", "confirm:t:n"]],
        "seq": 5,
    }
    expired = {**ask, "text": "rm? — timed out", "buttons": [], "seq": 9}
    api = SeqApi({"messages": [ask], "cursor": 5}, {"messages": [expired], "cursor": 9})
    c = _client(api)
    assert [a["id"] for a in c.asks()] == [3]
    assert c.asks() == []
    assert api.sinces == [0, 5]  # the second poll asked only for changes
