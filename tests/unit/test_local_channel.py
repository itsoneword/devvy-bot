"""dv-fe4d phase 2: the local channel -- a transport behind NotificationService.

Names the build must provide (analysis R3 + the architect's second RETURN):

* ``src.notifications.transport``: ``LOCAL_CHAT_ID == 1``, ``LocalTransport(db)``
  (rows in ``local_messages``), ``NotificationService(..., transport=...)``.
* ``GET /internal/inbox?since=<id>`` -> ``{"messages": [{id, chat_id, text,
  buttons, pinned, ...}]}`` and ``POST /internal/callback {"data": "confirm:
  <token>:y|n"}``. Both exist only when ``settings.channel == "local"`` and
  authenticate with the TUI-only credential ``settings.local_tui_secret``
  (never the shared WEBHOOK_API_SECRET a job can read).
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from src.api.server import create_api_app
from src.claude.confirmation import CALLBACK_PREFIX, ConfirmationManager
from src.events.bus import EventBus
from src.storage.database import DatabaseManager

INTERNAL = "internal-secret"
TUI = "tui-only-secret"
AUTH = {"Authorization": f"Bearer {INTERNAL}"}
TUI_AUTH = {"Authorization": f"Bearer {TUI}"}


def make_settings(channel: str = "local") -> MagicMock:
    s = MagicMock()
    s.development_mode = True
    s.github_webhook_secret = "gh-secret"
    s.webhook_api_secret = INTERNAL
    s.local_tui_secret = TUI
    s.channel = channel
    s.api_server_port = 8080
    s.debug = False
    return s


@pytest.fixture
async def db(tmp_path):
    db = DatabaseManager(str(tmp_path / "local.db"))
    await db.initialize()
    yield db
    await db.close()


def _transport(db):  # type: ignore[no-untyped-def]
    from src.notifications.transport import LocalTransport

    return LocalTransport(db)


async def _service(db, bus):  # type: ignore[no-untyped-def]
    from src.notifications.service import NotificationService
    from src.notifications.transport import LOCAL_CHAT_ID

    svc = NotificationService(
        event_bus=bus,
        transport=_transport(db),
        default_chat_ids=[LOCAL_CHAT_ID],
    )
    svc.register()
    await svc.start()
    return svc


def _inbox(client: TestClient, since: int = 0) -> list[dict]:
    r = client.get(f"/internal/inbox?since={since}", headers=TUI_AUTH)
    assert r.status_code == 200, r.text
    return r.json()["messages"]


async def _until(pred, timeout: float = 5.0):  # type: ignore[no-untyped-def]
    end = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < end:
        got = pred()
        if got:
            return got
        await asyncio.sleep(0.05)
    raise AssertionError("condition not met in time")


def test_local_chat_id_is_one() -> None:
    from src.notifications.transport import LOCAL_CHAT_ID

    assert LOCAL_CHAT_ID == 1


async def test_notify_lands_in_inbox(db) -> None:  # type: ignore[no-untyped-def]
    bus = EventBus()
    svc = await _service(db, bus)
    client = TestClient(create_api_app(bus, make_settings(), db_manager=db))
    try:
        r = client.post("/internal/notify", json={"text": "hello owner"}, headers=AUTH)
        assert r.status_code == 200
        rows = await _until(lambda: _inbox(client))
        assert [m["text"] for m in rows] == ["hello owner"]
    finally:
        await svc.stop()


async def test_pin_as_pump_twice_leaves_one_pin_with_latest_text(db) -> None:  # type: ignore[no-untyped-def]
    bus = EventBus()
    svc = await _service(db, bus)
    client = TestClient(create_api_app(bus, make_settings(), db_manager=db))
    try:
        for text in ("pump: 1 running", "pump: 2 running"):
            client.post(
                "/internal/notify",
                json={"text": text, "pin_as": "pump"},
                headers=AUTH,
            )
            await asyncio.sleep(0.3)
        await _until(lambda: [m for m in _inbox(client) if "2 running" in m["text"]])
        pinned = [m for m in _inbox(client) if m["pinned"]]
        assert len(pinned) == 1  # edited in place, not a second pin
        assert pinned[0]["text"] == "pump: 2 running"
        assert len(_inbox(client)) == 1
    finally:
        await svc.stop()


async def test_inbox_since_returns_only_newer_rows(db) -> None:  # type: ignore[no-untyped-def]
    bus = EventBus()
    svc = await _service(db, bus)
    client = TestClient(create_api_app(bus, make_settings(), db_manager=db))
    try:
        client.post("/internal/notify", json={"text": "one"}, headers=AUTH)
        first = await _until(lambda: _inbox(client))
        client.post("/internal/notify", json={"text": "two"}, headers=AUTH)
        newer = await _until(lambda: _inbox(client, since=first[-1]["id"]))
        assert [m["text"] for m in newer] == ["two"]
    finally:
        await svc.stop()


def _manager(db, timeout: float = 600.0):  # type: ignore[no-untyped-def]
    from src.notifications.transport import LOCAL_CHAT_ID

    tr = _transport(db)

    def buttons(token: str):  # type: ignore[no-untyped-def]
        return [
            ("✅ Approve", f"{CALLBACK_PREFIX}:{token}:y"),
            ("⛔ Deny", f"{CALLBACK_PREFIX}:{token}:n"),
        ]

    async def send(chat_id, text, token):  # type: ignore[no-untyped-def]
        return await tr.send(chat_id, text, parse_mode="HTML", buttons=buttons(token))

    async def edit(chat_id, message_id, text):  # type: ignore[no-untyped-def]
        await tr.edit(chat_id, message_id, text, parse_mode="HTML", buttons=None)

    return ConfirmationManager(
        send_prompt=send,
        chat_ids=[LOCAL_CHAT_ID],
        edit_prompt=edit,
        timeout_seconds=timeout,
    )


def _ask_app(db, cm, channel: str = "local"):  # type: ignore[no-untyped-def]
    return TestClient(
        create_api_app(
            EventBus(),
            make_settings(channel),
            db_manager=db,
            confirmation_manager=cm,
        )
    )


def _token_from(rows: list[dict]) -> str:
    for m in rows:
        for b in m.get("buttons") or []:
            data = b[1] if isinstance(b, (list, tuple)) else b.get("data", "")
            if str(data).endswith(":y"):
                return str(data).split(":")[1]
    return ""


async def test_job_ask_shows_in_inbox_and_callback_approve_runs_it(db) -> None:  # type: ignore[no-untyped-def]
    client = _ask_app(db, _manager(db))
    result: dict = {}

    def ask() -> None:
        r = client.post(
            "/internal/policy/ask",
            json={"reason": "rm -rf", "preview": "rm -rf x", "source": "job"},
            headers=AUTH,
        )
        result["body"] = r.json()

    task = asyncio.get_event_loop().run_in_executor(None, ask)
    token = await _until(lambda: _token_from(_inbox(client)))
    r = client.post(
        "/internal/callback",
        json={"data": f"{CALLBACK_PREFIX}:{token}:y"},
        headers=TUI_AUTH,
    )
    assert r.status_code == 200
    await asyncio.wait_for(task, 5)
    assert result["body"] == {"approved": True}


async def test_callback_deny_blocks_it(db) -> None:  # type: ignore[no-untyped-def]
    client = _ask_app(db, _manager(db))
    result: dict = {}

    def ask() -> None:
        result["body"] = client.post(
            "/internal/policy/ask",
            json={"reason": "r", "preview": "p"},
            headers=AUTH,
        ).json()

    task = asyncio.get_event_loop().run_in_executor(None, ask)
    token = await _until(lambda: _token_from(_inbox(client)))
    client.post(
        "/internal/callback",
        json={"data": f"{CALLBACK_PREFIX}:{token}:n"},
        headers=TUI_AUTH,
    )
    await asyncio.wait_for(task, 5)
    assert result["body"] == {"approved": False}


async def test_unanswered_ask_is_denied_after_timeout_and_card_edited(
    db, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr("src.model_router.job.ASK_TIMEOUT_SECONDS", 0.5)
    client = _ask_app(db, _manager(db))
    body = await asyncio.get_event_loop().run_in_executor(
        None,
        lambda: client.post(
            "/internal/policy/ask",
            json={"reason": "r", "preview": "p"},
            headers=AUTH,
        ).json(),
    )
    assert body == {"approved": False}
    rows = _inbox(client)
    assert len(rows) == 1
    assert rows[0]["edited_at"]  # the card was edited in place, not left pending
    assert not rows[0].get("buttons")  # no live Approve/Deny on a dead ask


# --- the ask gate must not be approvable by the asking job -----------------


async def test_callback_rejects_the_internal_bearer(db) -> None:  # type: ignore[no-untyped-def]
    client = _ask_app(db, _manager(db))
    for headers in (AUTH, {}):
        r = client.post(
            "/internal/callback",
            json={"data": f"{CALLBACK_PREFIX}:x:y"},
            headers=headers,
        )
        assert r.status_code in (401, 403)


async def test_inbox_rejects_the_internal_bearer(db) -> None:  # type: ignore[no-untyped-def]
    client = _ask_app(db, _manager(db))
    r = client.get("/internal/inbox", headers=AUTH)
    assert r.status_code in (401, 403)


async def test_callback_and_inbox_do_not_exist_in_telegram_mode(db) -> None:  # type: ignore[no-untyped-def]
    client = _ask_app(db, MagicMock(), channel="telegram")
    for headers in (AUTH, TUI_AUTH):
        assert client.get("/internal/inbox", headers=headers).status_code == 404
        r = client.post(
            "/internal/callback",
            json={"data": f"{CALLBACK_PREFIX}:x:y"},
            headers=headers,
        )
        assert r.status_code == 404


def test_telegram_import_confined_to_telegram_transport() -> None:
    """Importing the service, routing and local transport must not pull in
    ``telegram`` (routing.py had module-level imports)."""
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    code = (
        "import sys\n"
        "import src.notifications.service, src.notifications.transport\n"
        "from src.notifications.transport import LocalTransport\n"
        "assert 'telegram' not in sys.modules, "
        "[m for m in sys.modules if m.startswith('telegram')][:3]\n"
    )
    r = subprocess.run(
        [sys.executable, "-c", code], cwd=root, capture_output=True, text=True
    )
    assert r.returncode == 0, r.stderr[-800:]


_ = SimpleNamespace  # keep import used for editors
