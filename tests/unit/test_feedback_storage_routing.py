"""dv-a42c: feedback rows in message_reactions, routing, buttons, export."""

import sqlite3
import tempfile
from pathlib import Path

import pytest

from src.bot.card_buttons import clean_buttons
from src.llm_analytics.grafana_export import export_usage
from src.notifications.routing import source_matches
from src.storage.facade import Storage
from src.storage.schema.migrate import migrate_connection
from src.utils.constants import DEFAULT_REPORTS_ROUTES


def _topic(source):
    best = ""
    for k in DEFAULT_REPORTS_ROUTES:
        if source_matches(source, k) and len(k) > len(best):
            best = k
    return DEFAULT_REPORTS_ROUTES[best]


def test_routing_feedback_topic():
    assert _topic("pump2:feedback") == "Feedback"
    assert _topic("pump2:news") == "Pump"


def test_fb_buttons_allowed():
    rows = [[{"text": "7", "callback_data": "fb:abc123:7"}]]
    assert clean_buttons(rows) == rows


def test_migration_adds_feedback_schema():
    conn = sqlite3.connect(":memory:")
    migrate_connection("bot", conn)
    cols = {r[1]: r for r in conn.execute("PRAGMA table_info(message_reactions)")}
    for c in ("kind", "feature_id", "comment", "has_bugs", "bugs_text"):
        assert c in cols
    assert cols["messages_message_id"][3] == 0  # nullable
    link = {r[1] for r in conn.execute("PRAGMA table_info(feedback_feature_cards)")}
    assert {
        "feature_id",
        "board",
        "card_id",
        "merge_sha",
        "exec_mode",
        "stages",
        "returns",
    } <= link


@pytest.fixture
async def storage():
    with tempfile.TemporaryDirectory() as d:
        st = Storage(f"sqlite:///{Path(d) / 'bot.db'}")
        await st.initialize()
        st._p = Path(d) / "bot.db"
        yield st
        await st.close()


async def test_record_feedback_comment_bugs_and_answered(storage):
    r = storage.reactions
    await r.record_feedback("f1", score=8, chat_id=1, message_id=5, user_id=2)
    assert "f1" in await r.answered_features()
    await r.set_comment("f1", "есть баги: кнопка не жмётся")
    conn = sqlite3.connect(storage._p)
    row = conn.execute(
        "SELECT kind, score, comment, has_bugs, bugs_text FROM message_reactions"
        " WHERE feature_id='f1'"
    ).fetchone()
    assert row[0] == "feature_feedback" and row[1] == 8
    assert row[3] == 1 and "кнопка" in row[4]
    await r.record_feedback(
        "f2", score=None, skipped=True, chat_id=1, message_id=6, user_id=2
    )
    assert "f2" in await r.answered_features()


def test_grafana_export_feedback(tmp_path):
    db = tmp_path / "db" / "bot.db"
    db.parent.mkdir()
    conn = sqlite3.connect(db)
    migrate_connection("bot", conn)
    conn.execute(
        "INSERT INTO message_reactions (telegram_chat_id, telegram_message_id,"
        " user_id, profile_slug, emoji, score, reacted_at, kind, feature_id,"
        " has_bugs) VALUES (386000111, 1, 386000111, NULL, '', 9,"
        " '2026-11-07T09:00:00+00:00', 'feature_feedback', 'f1', 1)"
    )
    conn.execute(
        "INSERT INTO feedback_feature_cards (feature_id, board, card_id,"
        " merge_sha, exec_mode, stages, returns) VALUES"
        " ('f1','devvy','dv-1','abc','pump2','analyze,tests',1)"
    )
    conn.commit()
    conn.close()
    out = tmp_path / "g" / "devvy.db"
    out.parent.mkdir()
    assert export_usage(db, out) is not False
    o = sqlite3.connect(out)
    cols = {r[1] for r in o.execute("PRAGMA table_info(message_reactions)")}
    assert {"kind", "has_bugs", "feature_id"} <= cols
    assert "comment" not in cols
    lc = {r[1] for r in o.execute("PRAGMA table_info(feedback_feature_cards)")}
    assert {"feature_id", "board", "card_id", "exec_mode", "stages", "returns"} <= lc
    assert o.execute("SELECT count(*) FROM feedback_feature_cards").fetchone()[0] == 1
    assert b"386000111" not in out.read_bytes()
