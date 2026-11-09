"""Telegram reactions as answer-quality labels (dv-e1a7).

A 👎/👍/❤/🔥 on a bot turn reply is stored against the turn in bot.db
(``message_reactions``); everything else is ignored. No model call.
"""

import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from telegram import (
    ReactionTypeCustomEmoji,
    ReactionTypeEmoji,
    ReactionTypePaid,
)

from src.storage.facade import Storage

CHAT = 555
OWNER = 111
GUEST = 222
STRANGER = 333
TURN_MSG = 900  # messages.message_id the turn was saved as


@pytest.fixture
async def storage():
    with tempfile.TemporaryDirectory() as d:
        st = Storage(f"sqlite:///{Path(d) / 'bot.db'}")
        await st.initialize()
        st._test_db_path = Path(d) / "bot.db"
        yield st
        await st.close()


def _rows(storage):
    conn = sqlite3.connect(storage._test_db_path)
    conn.row_factory = sqlite3.Row
    try:
        return [
            dict(r) for r in conn.execute("SELECT * FROM message_reactions ORDER BY id")
        ]
    finally:
        conn.close()


def _context(storage):
    roles = {OWNER: "owner", GUEST: "guest", STRANGER: "banned"}
    auth = MagicMock()
    auth.get_user_role = MagicMock(side_effect=lambda uid: roles.get(uid))
    settings = SimpleNamespace(allowed_users=[OWNER])
    return SimpleNamespace(
        bot_data={"storage": storage, "auth_manager": auth, "settings": settings},
        application=SimpleNamespace(bot_data=None),
    )


def _update(new, *, msg_id=10, user_id=OWNER, chat_id=CHAT, anonymous=False):
    mr = SimpleNamespace(
        chat=SimpleNamespace(id=chat_id),
        message_id=msg_id,
        user=None if anonymous else SimpleNamespace(id=user_id, is_bot=False),
        actor_chat=None,
        old_reaction=(),
        new_reaction=tuple(new),
        date=datetime(2026, 11, 6, 9, 0),
    )
    return SimpleNamespace(
        message_reaction=mr,
        effective_user=mr.user,
        effective_chat=mr.chat,
    )


def E(emoji):
    return ReactionTypeEmoji(emoji=emoji)


async def _handle(storage, update):
    from src.bot.features.reactions import handle_message_reaction

    await handle_message_reaction(update, _context(storage))


async def _ledger(storage, msg_id=10, kind="turn_reply", mm=TURN_MSG, profile="chef"):
    await storage.sent_messages.record(
        CHAT,
        msg_id,
        kind,
        messages_message_id=mm if kind == "turn_reply" else None,
        profile_slug=profile,
    )


@pytest.mark.parametrize(
    "emoji,score",
    [("👎", -1), ("👍", 1), ("❤", 1), ("❤️", 1), ("🔥", 2)],
)
async def test_listed_reaction_stores_one_row(storage, emoji, score):
    await _ledger(storage)
    await _handle(storage, _update([E(emoji)]))
    rows = _rows(storage)
    assert len(rows) == 1
    r = rows[0]
    assert r["telegram_chat_id"] == CHAT
    assert r["telegram_message_id"] == 10
    assert r["messages_message_id"] == TURN_MSG
    assert r["profile_slug"] == "chef"
    assert r["user_id"] == OWNER
    assert r["score"] == score
    assert r["emoji"].replace("️", "") == emoji.replace("️", "")
    assert r["reacted_at"]


async def test_changing_reaction_updates_the_same_row(storage):
    await _ledger(storage)
    await _handle(storage, _update([E("👎")]))
    await _handle(storage, _update([E("🔥")]))
    rows = _rows(storage)
    assert len(rows) == 1
    assert rows[0]["score"] == 2


async def test_removing_reaction_deletes_the_row(storage):
    await _ledger(storage)
    await _handle(storage, _update([E("👎")]))
    await _handle(storage, _update([]))
    assert _rows(storage) == []


async def test_removing_to_unlisted_emoji_deletes_the_row(storage):
    await _ledger(storage)
    await _handle(storage, _update([E("👎")]))
    await _handle(storage, _update([E("🤡")]))
    assert _rows(storage) == []


async def test_strongest_listed_emoji_wins(storage):
    await _ledger(storage)
    await _handle(storage, _update([E("👍"), E("🤡"), E("🔥")]))
    rows = _rows(storage)
    assert len(rows) == 1 and rows[0]["score"] == 2


@pytest.mark.parametrize(
    "new",
    [
        [E("🤡")],
        [E("😢")],
        [ReactionTypeCustomEmoji(custom_emoji_id="5368324170671202286")],
        [ReactionTypePaid()],
    ],
)
async def test_unlisted_reactions_store_nothing(storage, new):
    await _ledger(storage)
    await _handle(storage, _update(new))
    assert _rows(storage) == []


async def test_reaction_on_system_notify_ignored(storage):
    await _ledger(storage, kind="system_notify")
    await _handle(storage, _update([E("👎")]))
    assert _rows(storage) == []


async def test_reaction_on_unledgered_message_ignored(storage):
    await _handle(storage, _update([E("👎")], msg_id=4242))
    assert _rows(storage) == []


async def test_anonymous_reaction_ignored(storage):
    await _ledger(storage)
    await _handle(storage, _update([E("👎")], anonymous=True))
    assert _rows(storage) == []


async def test_count_update_without_message_reaction_ignored(storage):
    await _ledger(storage)
    from src.bot.features.reactions import handle_message_reaction

    upd = SimpleNamespace(message_reaction=None, effective_user=None)
    await handle_message_reaction(upd, _context(storage))
    assert _rows(storage) == []


async def test_unauthorised_user_stores_nothing(storage):
    await _ledger(storage)
    await _handle(storage, _update([E("👎")], user_id=STRANGER))
    assert _rows(storage) == []


async def test_two_users_get_separate_rows(storage):
    await _ledger(storage)
    await _handle(storage, _update([E("👎")], user_id=OWNER))
    await _handle(storage, _update([E("🔥")], user_id=GUEST))
    rows = _rows(storage)
    assert {(r["user_id"], r["score"]) for r in rows} == {(OWNER, -1), (GUEST, 2)}


async def test_reaction_on_any_chunk_labels_the_turn(storage):
    await _ledger(storage, msg_id=10)
    await _ledger(storage, msg_id=11)
    await _handle(storage, _update([E("👎")], msg_id=11))
    rows = _rows(storage)
    assert len(rows) == 1
    assert rows[0]["telegram_message_id"] == 11
    assert rows[0]["messages_message_id"] == TURN_MSG


async def test_handler_makes_no_model_call(storage, monkeypatch):
    import src.model_router as mr

    boom = MagicMock(side_effect=AssertionError("model call"))
    monkeypatch.setattr(mr, "get_router", boom, raising=False)
    await _ledger(storage)
    await _handle(storage, _update([E("👎")]))
    boom.assert_not_called()


def test_handler_is_registered():
    src = (Path(__file__).parents[2] / "src/bot/core.py").read_text() + (
        Path(__file__).parents[2] / "src/bot/orchestrator.py"
    ).read_text()
    assert "MessageReactionHandler" in src


def test_migration_and_schema_declare_table():
    root = Path(__file__).parents[2]
    mig = root / "migrations/bot/versions/0010_message_reactions.py"
    assert mig.exists()
    assert "message_reactions" in mig.read_text()
    assert "message_reactions" in (root / "src/storage/schema/bot.py").read_text()
