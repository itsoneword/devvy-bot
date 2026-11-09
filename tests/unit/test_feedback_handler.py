"""dv-a42c: the Feedback topic answers: isolation, voice, extraction."""

import json
import sqlite3
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram.ext import ApplicationHandlerStop

from src.bot.core import ClaudeCodeBot
from src.bot.features import feedback as fbh
from src.notifications.routing import ReportsRouter
from src.pump2 import feedback as survey
from src.storage.facade import Storage
from tests.unit.test_notifications.test_reports_routing import FakeRepo

GROUP = -1004290210285
OWNER = 100
FEEDBACK_THREAD = 41
FID = "abc123def456"


class FailingBackend:
    def __getattr__(self, name):
        raise AssertionError(f"backend touched: {name}")


@pytest.fixture
async def storage():
    with tempfile.TemporaryDirectory() as d:
        st = Storage(f"sqlite:///{Path(d) / 'bot.db'}")
        await st.initialize()
        st._p = Path(d) / "bot.db"
        yield st
        await st.close()


@pytest.fixture
def state(tmp_path, monkeypatch):
    path = tmp_path / "feedback.json"
    survey.save_state(
        path,
        {
            "open": {"feature_id": FID, "asked_at": "2026-11-07T09:00:00Z"},
            "last_feature": FID,
            "asked": {FID: {"n": 1}},
            "features": {
                FID: {
                    "board": "devvy",
                    "links": [
                        {
                            "board": "devvy",
                            "card_id": "dv-1",
                            "exec_mode": "pump2",
                            "stages": ["analyze", "build"],
                            "returns": 1,
                        },
                        {
                            "board": "devvy",
                            "card_id": "dv-2",
                            "exec_mode": "job",
                            "stages": [],
                            "returns": 0,
                        },
                    ],
                }
            },
        },
    )
    monkeypatch.setattr(survey, "STATE", path)
    return path


def _router():
    repo = FakeRepo()
    repo.rows[(GROUP, "reports:feedback")] = SimpleNamespace(
        message_thread_id=FEEDBACK_THREAD, is_active=True
    )
    repo.rows[(GROUP, "reports:coach")] = SimpleNamespace(
        message_thread_id=21, is_active=True
    )
    return ReportsRouter(
        GROUP,
        {"pump2": "Pump", "pump2:feedback": "Feedback"},
        repo,
        [OWNER],
        bindings={"Coach": "coach"},
    )


def _gate(router, role="owner", **deps):
    core = ClaudeCodeBot.__new__(ClaudeCodeBot)
    core.settings = SimpleNamespace(reports_chat_id=GROUP, allowed_users=[OWNER])
    core.deps = {
        "reports_router": router,
        "auth_manager": SimpleNamespace(get_user_role=lambda uid: role),
        **deps,
    }
    core.app = MagicMock()
    core._add_reports_chat_handlers()
    handlers = [c.args[0] for c in core.app.add_handler.call_args_list]
    return next(h for h in handlers if hasattr(h, "filters")).callback


def _msg(text=None, voice=None, thread=FEEDBACK_THREAD, mid=900):
    return SimpleNamespace(
        chat_id=GROUP,
        message_id=mid,
        text=text,
        voice=voice,
        photo=None,
        document=None,
        message_thread_id=thread,
        is_topic_message=True,
        reply_to_message=None,
        reply_text=AsyncMock(),
    )


def _ctx(storage, transcript="голос: всё супер, оценка 8"):
    vh = SimpleNamespace(
        process_voice_message=AsyncMock(
            return_value=SimpleNamespace(transcription=transcript)
        )
    )
    return SimpleNamespace(
        bot_data={
            "storage": storage,
            "features": SimpleNamespace(get_voice_handler=lambda: vh),
            "backends": {"claude": FailingBackend(), "codex": FailingBackend()},
        },
        user_data={"agent_backend": "claude"},
    )


def _upd(m, uid=OWNER):
    return SimpleNamespace(effective_message=m, effective_user=SimpleNamespace(id=uid))


def _rows(st, sql):
    conn = sqlite3.connect(st._p)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def _fake_complete(payload):
    async def complete(prompt, **kw):
        assert kw["purpose"] == "feedback_extract"
        assert "JSON" in kw["system_prompt"]
        if isinstance(payload, Exception):
            raise payload
        return payload

    return complete


async def test_feedback_topic_text_and_voice_never_reach_a_turn(
    storage, state, monkeypatch
):
    monkeypatch.setattr(
        fbh,
        "_default_complete",
        _fake_complete(json.dumps({"score": 8, "pros": "fast", "aspects": ["ui"]})),
    )
    gate = _gate(_router())
    ctx = _ctx(storage)
    for m in (
        _msg(text="есть баги: кнопка не жмётся", mid=900),
        _msg(voice=SimpleNamespace(duration=3), mid=901),
    ):
        with pytest.raises(ApplicationHandlerStop):
            await gate(_upd(m), ctx)
        m.reply_text.assert_awaited()
    assert _rows(storage, "SELECT count(*) FROM sessions")[0][0] == 0
    assert _rows(storage, "SELECT count(*) FROM messages")[0][0] == 0
    (row,) = _rows(
        storage, "SELECT * FROM message_reactions WHERE kind='feature_feedback'"
    )
    assert row["feature_id"] == FID
    assert "кнопка" in row["comment"] and "всё супер" in row["comment"]
    assert row["score"] == 8 and row["score_source"] == "extracted"
    assert row["has_bugs"] == 1
    links = _rows(storage, "SELECT card_id, exec_mode FROM feedback_feature_cards")
    assert {(r[0], r[1]) for r in links} == {("dv-1", "pump2"), ("dv-2", "job")}
    assert survey.load_state(state)["open"] is None


async def test_feedback_first_update_after_restart_empty_bot_data(
    storage, state, monkeypatch
):
    """The gate runs at group -4, before the middleware fills bot_data."""
    monkeypatch.setattr(fbh, "_default_complete", _fake_complete("not json"))
    full = _ctx(storage).bot_data
    gate = _gate(_router(), storage=storage, features=full["features"])
    ctx = SimpleNamespace(bot_data={}, user_data={})
    m = _msg(text="есть баги: кнопка не жмётся")
    with pytest.raises(ApplicationHandlerStop):
        await gate(_upd(m), ctx)
    (row,) = _rows(
        storage, "SELECT * FROM message_reactions WHERE kind='feature_feedback'"
    )
    assert row["feature_id"] == FID and row["has_bugs"] == 1
    assert ctx.bot_data["storage"] is storage


async def test_feedback_without_storage_replies(state):
    m = _msg(text="8")
    await fbh.handle_feedback_message(
        _upd(m), SimpleNamespace(bot_data={}, user_data={})
    )
    m.reply_text.assert_awaited()


async def test_feedback_topic_non_owner_dropped(storage, state):
    gate = _gate(_router(), role="guest")
    m = _msg(text="10/10")
    with pytest.raises(ApplicationHandlerStop):
        await gate(_upd(m, uid=5), _ctx(storage))
    m.reply_text.assert_not_called()
    assert not _rows(storage, "SELECT * FROM message_reactions")


async def test_bound_topic_still_passes(storage, state):
    m = _msg(text="hi", thread=21)
    assert await _gate(_router())(_upd(m), _ctx(storage)) is None


async def test_extract_good_broken_and_raising():
    good = await fbh.extract("x", _fake_complete('ok {"score": 6, "tested": 1}'))
    assert good == {"score": 6, "tested": 1}
    assert await fbh.extract("x", _fake_complete("{not json")) is None
    assert await fbh.extract("x", _fake_complete(RuntimeError("down"))) is None


async def test_broken_json_keeps_raw_text_and_keyword_rule(storage, state):
    m = _msg(text="bugs: падает на старте")
    await fbh.handle_feedback_message(
        _upd(m), _ctx(storage), complete=_fake_complete("nope")
    )
    (row,) = _rows(storage, "SELECT * FROM message_reactions")
    assert row["comment"] == "bugs: падает на старте"
    assert row["has_bugs"] == 1 and "падает" in row["bugs_text"]
    assert row["score"] is None


async def test_button_then_voice_keeps_button_score(storage, state):
    q = SimpleNamespace(
        data=f"fb:{FID}:9",
        message=SimpleNamespace(chat_id=GROUP, message_id=800, reply_text=AsyncMock()),
        from_user=SimpleNamespace(id=OWNER),
        answer=AsyncMock(),
    )
    await fbh.handle_feedback_callback(SimpleNamespace(callback_query=q), _ctx(storage))
    assert survey.load_state(state)["open"] is None
    m = _msg(voice=SimpleNamespace(duration=2))
    await fbh.handle_feedback_message(
        _upd(m),
        _ctx(storage),
        complete=_fake_complete('{"score": 3, "cons": "slow", "aspects": "speed"}'),
    )
    (row,) = _rows(storage, "SELECT * FROM message_reactions")
    assert row["score"] == 9 and row["score_source"] == "button"
    assert row["cons"] == "slow" and row["aspects"] == "speed"
    assert row["feature_id"] == FID  # attached to the last asked feature


async def test_not_tested_reasks_later(storage, state):
    q = SimpleNamespace(
        data=f"fb:{FID}:nt",
        message=SimpleNamespace(chat_id=GROUP, message_id=800, reply_text=AsyncMock()),
        from_user=SimpleNamespace(id=OWNER),
        answer=AsyncMock(),
    )
    await fbh.handle_feedback_callback(SimpleNamespace(callback_query=q), _ctx(storage))
    assert FID not in await storage.reactions.answered_features()
    st = survey.load_state(state)
    assert st["asked"][FID]["retry_at"]
    now = datetime.now(UTC)
    assert not survey._askable(st, FID, now)
    later = survey.parse_time(st["asked"][FID]["retry_at"])
    assert survey._askable(st, FID, later)


def test_fb_callback_registered_owner_only():
    from src.bot import orchestrator

    src = Path(orchestrator.__file__).read_text()
    assert 'self._owner_only(handle_feedback_callback, "feedback")' in src
    assert 'pattern=r"^fb:"' in src
