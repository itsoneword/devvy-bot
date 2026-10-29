"""Trace renderer and read helpers (dv-2260 phase 3), beside the protected
``test_trace_command.py``."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from src.model_router.trace_view import duration, input_summary, render_trace
from src.storage.facade import Storage
from src.storage.model_calls import ModelCall, ModelCallRepository


def _row(seq, tool, status="ok", parent=None, **kw):
    return {
        "id": seq,
        "seq": seq,
        "tool_use_id": f"t{seq}",
        "parent_tool_use_id": parent,
        "tool": tool,
        "status": status,
        "duration_ms": kw.pop("ms", 5),
        **kw,
    }


def test_duration_format() -> None:
    assert duration(7) == "7ms"
    assert duration(1234) == "1.2s"
    assert duration(None) == "?"


def test_input_summary_picks_the_telling_field() -> None:
    assert input_summary('{"command": "ls -la", "timeout": 5}') == "ls -la"
    assert (
        input_summary('{"subagent_type": "Explore", "description": "dig"}')
        == "Explore: dig"
    )
    # A migrated tool_usage input cut mid-JSON still reads.
    assert input_summary('{"command": "git log --one') == "git log --one"
    assert input_summary(None) == ""


def test_tree_nests_subagent_calls_and_marks_every_status() -> None:
    rows = [
        _row(
            1,
            "Agent",
            input='{"subagent_type": "x", "prompt": "p"}',
            result_preview="final words",
        ),
        _row(2, "Grep", parent="t1"),
        _row(3, "Bash", status="error", error="boom\nsecond line"),
        _row(4, "Agent", status="detached"),
        _row(5, "Read", status="unfinished"),
        _row(6, "Glob", parent="gone"),  # parent not in the trace: top level
    ]
    call = {
        "id": 9,
        "purpose": "background_job",
        "harness": "claude",
        "outcome": "ok",
        "duration_ms": 2500,
    }
    text = render_trace({"call": call, "message": None, "rows": rows}).text
    lines = text.splitlines()
    assert lines[0].startswith("Trace #9 · background_job")
    assert "no chat message" in text
    tree = lines[lines.index("Tool calls: 6") + 1 :]
    assert tree == [
        "✓ Agent x: p · 5ms",
        "  ✓ Grep · 5ms",
        "  ↳ final words",
        "✗ Bash · 5ms — boom second line",
        "⏳ Agent · detached",
        "⋯ Read · unfinished",
        "✓ Glob · 5ms",
    ]


def test_render_limits_cut_question_and_subagent_result() -> None:
    rows = [_row(1, "Agent", result_preview="r" * 1000)]
    msg = {"prompt": "q" * 1000, "response": ""}
    out = render_trace(
        {"call": {"id": 1}, "message": msg, "rows": rows},
        text_limit=50,
        result_limit=40,
    ).text
    assert "Q: " + "q" * 49 + "…" in out
    assert "A: (empty answer)" in out
    assert "↳ " + "r" * 39 + "…" in out


@pytest.fixture
async def storage(tmp_path: Path):
    st = Storage(f"sqlite:///{tmp_path / 'bot.db'}")
    await st.initialize()
    yield st
    await st.close()


def _call(user_id, session_id) -> ModelCall:
    return ModelCall(
        purpose="chat",
        kind="turn",
        harness="claude",
        harness_version="1",
        provider="anthropic",
        model="haiku",
        attended=True,
        profile=None,
        user_id=user_id,
        session_id=session_id,
        input_tokens=0,
        output_tokens=0,
        cache_read_tokens=0,
        cache_creation_tokens=0,
        cost_usd=None,
        duration_ms=1,
        outcome="ok",
        error=None,
    )


async def test_trace_of_a_turn_without_tools_finds_its_message(storage) -> None:
    """No tool rows = no message link; the session's message saved right
    after the call is the turn's."""
    await storage.users.create_user(_user(5))
    await storage.sessions.create_session(_session("s1", 5))
    call_id = await ModelCallRepository(storage.db_manager).record(_call(5, "s1"))
    await storage.messages.save_message(_message("s1", 5, "hello", "hi"))
    trace = await storage.tool_calls.trace(call_id)
    assert trace["rows"] == []
    assert trace["message"]["prompt"] == "hello"
    assert await storage.tool_calls.latest_turn_id(5) == call_id
    assert await storage.tool_calls.latest_turn_id(6) is None
    assert await storage.tool_calls.trace(call_id + 1) is None
    [recent] = await storage.tool_calls.recent_turns(5)
    assert recent["id"] == call_id and recent["tool_count"] == 0


def _user(uid):
    from src.storage.models import UserModel

    return UserModel(user_id=uid, telegram_username="u", is_allowed=True)


def _session(sid, uid):
    from src.storage.models import SessionModel

    now = datetime.now(UTC)
    return SessionModel(
        session_id=sid,
        user_id=uid,
        project_path="/p",
        created_at=now,
        last_used=now,
    )


def _message(sid, uid, prompt, response):
    from src.storage.models import MessageModel

    return MessageModel(
        message_id=None,
        session_id=sid,
        user_id=uid,
        timestamp=datetime.now(UTC),
        prompt=prompt,
        response=response,
    )
