"""The harness trace (dv-2260): collector, router wiring, interrupted turns,
jobs over the internal API, the viewer, Codex feed."""

import asyncio
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from fastapi.testclient import TestClient

from src.agent_backends.codex_cli import _feed_codex_trace, map_codex_event
from src.api.server import create_api_app
from src.claude.sdk_integration import ClaudeResponse
from src.events.bus import EventBus
from src.model_router import ModelRouter
from src.model_router.job import RemoteTraceSink
from src.model_router.trace import TraceCollector, current_trace
from src.model_router.trace_view import render_tree, trace_report
from src.storage.database import DatabaseManager
from src.storage.model_calls import ModelCallRepository
from src.storage.tool_calls import ToolCallRepository
from tests.unit.test_api.test_internal_endpoints import AUTH, make_settings


@pytest.fixture
async def db(tmp_path):
    manager = DatabaseManager(f"sqlite:///{tmp_path / 'test.db'}")
    await manager.initialize()
    yield manager
    await manager.close()


def _asst(*blocks: Any, parent: Any = None) -> AssistantMessage:
    return AssistantMessage(content=list(blocks), model="m", parent_tool_use_id=parent)


def _user(*blocks: Any, parent: Any = None) -> UserMessage:
    return UserMessage(content=list(blocks), parent_tool_use_id=parent)


def _subagent_stream() -> List[Any]:
    """main -> Agent(a1) -> subagent Read(r1) ; main Bash(b1) fails."""
    return [
        _asst(ToolUseBlock(id="a1", name="Agent", input={"subagent_type": "Explore"})),
        _asst(
            ToolUseBlock(id="r1", name="Read", input={"file_path": "/x"}), parent="a1"
        ),
        _user(ToolResultBlock(tool_use_id="r1", content="file body"), parent="a1"),
        _user(ToolResultBlock(tool_use_id="a1", content="found it: /x" * 3)),
        _asst(ToolUseBlock(id="b1", name="Bash", input={"command": "ls"})),
        _user(ToolResultBlock(tool_use_id="b1", content="denied", is_error=True)),
    ]


def test_collector_builds_the_subagent_tree() -> None:
    trace = TraceCollector()
    for m in _subagent_stream():
        trace.feed_sdk(m)
    rows = {r["tool_use_id"]: r for r in trace.rows()}
    assert rows["a1"]["parent_tool_use_id"] is None
    assert rows["a1"]["subagent_type"] == "Explore"
    assert rows["a1"]["result_preview"].startswith("found it")
    assert rows["r1"]["parent_tool_use_id"] == "a1" and rows["r1"]["ok"] == 1
    assert rows["b1"]["ok"] == 0 and rows["b1"]["error"] == "denied"
    assert rows["r1"]["duration_ms"] is not None
    text = render_tree(trace.rows())
    assert "  ✓ Read" in text and "✗ Bash" in text and "Agent[Explore]" in text


def test_previews_are_clipped_and_scrubbed() -> None:
    trace = TraceCollector()
    secret = "TOKEN=abcdefghijkl123456"
    trace.feed_sdk(_asst(ToolUseBlock(id="t", name="Bash", input={"command": secret})))
    trace.feed_sdk(_user(ToolResultBlock(tool_use_id="t", content="x" * 5000)))
    trace.feed_sdk(_asst(ToolUseBlock(id="g", name="Agent", input={})))
    trace.feed_sdk(_user(ToolResultBlock(tool_use_id="g", content="y" * 9000)))
    rows = {r["tool_use_id"]: r for r in trace.rows()}
    assert "abcdefghijkl" not in rows["t"]["input_short"]
    assert len(rows["t"]["result_preview"]) <= 200 and rows["t"]["result_bytes"] == 5000
    assert len(rows["g"]["result_preview"]) <= 4000


def test_detached_subagent_is_marked() -> None:
    trace = TraceCollector()
    trace.feed_sdk(
        _asst(ToolUseBlock(id="bg", name="Agent", input={"run_in_background": True}))
    )
    trace.feed_sdk(_asst(ToolUseBlock(id="still", name="Agent", input={})))
    trace.feed_sdk(_asst(ToolUseBlock(id="cut", name="Bash", input={})))
    trace.feed_sdk(
        _user(ToolResultBlock(tool_use_id="bg", content="Async agent launched x"))
    )
    trace.close("turn ended")
    rows = {r["tool_use_id"]: r for r in trace.rows()}
    assert rows["bg"]["detached"] == 1 and rows["bg"]["ok"] == 1
    assert rows["still"]["detached"] == 1 and rows["still"]["ok"] is None
    assert rows["cut"]["detached"] == 0 and "no result" in rows["cut"]["error"]


async def test_rows_are_written_while_the_turn_runs(db) -> None:
    repo = ToolCallRepository(db)
    trace = TraceCollector(repo)
    trace.feed_sdk(_asst(ToolUseBlock(id="t1", name="Read", input={})))
    await trace.flush()  # as the stream goes on, with no close()
    await repo.link_message(trace.trace_id, 1, "s")
    (row,) = await repo.for_message(1)
    assert row["tool_name"] == "Read" and row["ok"] is None
    trace.feed_sdk(_user(ToolResultBlock(tool_use_id="t1", content="ok")))
    await trace.flush()
    (row,) = await repo.for_message(1)
    assert row["ok"] == 1 and row["result_preview"] == "ok"


def _router(db: DatabaseManager, backend: Any) -> ModelRouter:
    return ModelRouter(
        backends={"claude": backend},
        recorder=ModelCallRepository(db),
        tool_calls=ToolCallRepository(db),
    )


def _backend(effect: Any) -> MagicMock:
    backend = MagicMock()
    backend.backend_id = "claude"
    backend.default_model.return_value = "sonnet"
    backend.run_command = AsyncMock(side_effect=effect)
    return backend


async def test_router_links_trace_to_model_call_and_response(db) -> None:
    async def run(**_: Any) -> ClaudeResponse:
        trace = current_trace()
        assert trace is not None
        for m in _subagent_stream():
            trace.feed_sdk(m)
        return ClaudeResponse(
            content="hi", session_id="s1", cost=0.1, duration_ms=1, num_turns=1
        )

    router = _router(db, _backend(run))
    response = await router.run_turn("claude", purpose="chat", prompt="p")
    repo = ToolCallRepository(db)
    assert response.trace_id
    (turn,) = await repo.last_turns(1)
    rows = await repo.for_model_call(turn["id"])
    assert len(rows) == 3 and {r["session_id"] for r in rows} == {"s1"}


async def test_interrupted_turn_keeps_its_rows(db) -> None:
    async def run(**_: Any) -> ClaudeResponse:
        trace = current_trace()
        assert trace is not None
        trace.feed_sdk(_asst(ToolUseBlock(id="t1", name="Read", input={})))
        trace.feed_sdk(_user(ToolResultBlock(tool_use_id="t1", content="done")))
        trace.feed_sdk(_asst(ToolUseBlock(id="t2", name="Bash", input={})))
        raise asyncio.CancelledError()

    router = _router(db, _backend(run))
    with pytest.raises(asyncio.CancelledError):
        await router.run_turn("claude", purpose="chat", prompt="p", session_id="s9")
    repo = ToolCallRepository(db)
    (turn,) = await repo.last_turns(1)
    assert turn["outcome"] == "error"
    rows = await repo.for_model_call(turn["id"])
    assert [(r["tool_name"], r["ok"]) for r in rows] == [("Read", 1), ("Bash", None)]
    assert "no result" in rows[1]["error"]


async def test_no_tool_calls_means_no_trace_link(db) -> None:
    async def run(**_: Any) -> ClaudeResponse:
        return ClaudeResponse(
            content="hi", session_id="s1", cost=0.1, duration_ms=1, num_turns=1
        )

    response = await _router(db, _backend(run)).run_turn(
        "claude", purpose="chat", prompt="p"
    )
    assert response.trace_id is None


class TestJobsOverTheInternalApi:
    async def test_rows_and_link_arrive_through_the_bot(self, db) -> None:
        app = create_api_app(
            EventBus(),
            make_settings(),
            db_manager=db,
            model_calls=ModelCallRepository(db),
        )
        client = TestClient(app)
        trace = TraceCollector()
        for m in _subagent_stream():
            trace.feed_sdk(m)
        rows = trace.rows()
        r = client.post(
            "/internal/tool_calls",
            json={"trace_id": trace.trace_id, "rows": rows},
            headers=AUTH,
        )
        assert r.status_code == 200, r.text
        r = client.post(
            "/internal/model_calls",
            json={
                "purpose": "background_job",
                "kind": "turn",
                "harness": "claude",
                "outcome": "ok",
                "session_id": "js",
                "trace_id": trace.trace_id,
            },
            headers=AUTH,
        )
        assert r.status_code == 200, r.text
        repo = ToolCallRepository(db)
        (turn,) = await repo.last_turns(1)
        got = await repo.for_model_call(turn["id"])
        assert len(got) == 3 and got[0]["session_id"] == "js"

    def test_tool_calls_need_auth(self, db) -> None:
        app = create_api_app(EventBus(), make_settings(), db_manager=db)
        r = TestClient(app).post(
            "/internal/tool_calls", json={"trace_id": "t", "rows": []}
        )
        assert r.status_code == 401

    async def test_remote_sink_posts_batches(self) -> None:
        link = MagicMock()
        sent: List[Any] = []
        link.post = lambda path, body, timeout: sent.append((path, body)) or {}
        await RemoteTraceSink(link).upsert("tid", [{"tool_use_id": "a"}])
        assert sent == [
            (
                "/internal/tool_calls",
                {"trace_id": "tid", "rows": [{"tool_use_id": "a"}]},
            )
        ]

    async def test_remote_sink_survives_a_dead_bot(self) -> None:
        link = MagicMock()
        link.post = MagicMock(side_effect=OSError("down"))
        await RemoteTraceSink(link).upsert("tid", [{"tool_use_id": "a"}])


async def test_report_shows_question_answer_and_tree(db) -> None:
    async def run(**_: Any) -> ClaudeResponse:
        trace = current_trace()
        assert trace is not None
        for m in _subagent_stream():
            trace.feed_sdk(m)
        return ClaudeResponse(
            content="hi", session_id="s1", cost=0.1, duration_ms=1, num_turns=1
        )

    response = await _router(db, _backend(run)).run_turn(
        "claude", purpose="chat", prompt="p"
    )
    async with db.get_connection() as conn:
        await conn.execute("INSERT INTO users (user_id) VALUES (1)")
        await conn.execute(
            "INSERT INTO sessions (session_id, user_id, project_path) "
            "VALUES ('s1', 1, '/p')"
        )
        await conn.execute(
            "INSERT INTO messages (session_id, user_id, prompt, response) "
            "VALUES ('s1', 1, 'where is x?', 'in /x')"
        )
        await conn.commit()
    await ToolCallRepository(db).link_message(response.trace_id, 1, "s1")

    for arg in ("", "1", "call:1"):
        out = await trace_report(db, arg)
        assert "Q: where is x?" in out and "A: in /x" in out
        assert "Agent[Explore]" in out and "  ✓ Read" in out
    assert "call:1" in await trace_report(db, "last")
    assert "trace" in await trace_report(db, "nonsense")


def test_codex_items_are_fed_and_paired() -> None:
    trace = TraceCollector()
    item = {"id": "i1", "type": "command_execution", "command": "ls"}
    events: List[Dict[str, Any]] = [
        {"type": "item.started", "item": {**item, "status": "in_progress"}},
        {
            "type": "item.completed",
            "item": {
                **item,
                "exit_code": 1,
                "aggregated_output": "boom",
                "status": "failed",
            },
        },
    ]
    for e in events:
        _feed_codex_trace(trace, e, map_codex_event(e))
    (row,) = trace.rows()
    assert row["tool_name"] == "Bash" and row["parent_tool_use_id"] is None
    assert row["ok"] == 0 and row["result_preview"] == "boom"


def test_no_active_trace_outside_the_router() -> None:
    assert current_trace() is None


async def test_trace_command_replies_with_the_report(db) -> None:
    from src.bot.orchestration.commands.trace import trace_command

    storage = MagicMock()
    storage.db_manager = db
    update = MagicMock()
    update.message.reply_text = AsyncMock()
    context = MagicMock()
    context.bot_data = {"storage": storage}
    context.args = ["last"]
    await trace_command(update, context)
    text = update.message.reply_text.call_args.args[0]
    assert text.startswith("<pre>") and "no traced turns" in text
