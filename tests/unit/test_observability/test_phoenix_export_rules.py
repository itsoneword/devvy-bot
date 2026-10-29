"""dv-87d8: rules the Phoenix exporter relies on, beyond the pinned contract.

* Completeness: a call is exported once, with the tool rows linked at that
  moment. That is only complete while no tool row is written after the link;
  the invariant tests below fail the day that changes (then the exporter
  needs per-tool export tracking).
* Edge handling of the REST answer and the mapping.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import httpx
import pytest

from src.claude.sdk_integration import ClaudeResponse
from src.model_router import ModelRouter
from src.model_router.harness import HarnessVersions
from src.model_router.trace import current_trace
from src.observability.phoenix import (
    export_pending,
    pump_job,
    register_phoenix_export,
    span_id_of,
    spans_for,
)
from src.storage.database import DatabaseManager
from src.storage.model_calls import ModelCall, ModelCallRepository
from src.storage.tool_calls import ToolCallRepository


@pytest.fixture
async def db(tmp_path):
    manager = DatabaseManager(f"sqlite:///{tmp_path / 'test.db'}")
    await manager.initialize()
    yield manager
    await manager.close()


class _Versions(HarnessVersions):
    async def get(self, harness: str) -> str:
        return f"{harness}-9.9"


class _OrderSink:
    """A ToolCallRepository stand-in that logs the order of writes."""

    def __init__(self) -> None:
        self.ops: List[str] = []
        self.rows: Dict[str, Dict[str, Any]] = {}

    async def upsert(self, trace_id: str, rows: List[Dict[str, Any]]) -> None:
        self.ops.append("upsert")
        for r in rows:
            self.rows[r["tool_use_id"]] = dict(r)

    async def link_model_call(
        self, trace_id: str, model_call_id: int, session_id: Optional[str] = None
    ) -> None:
        self.ops.append("link")


def _backend(fail: bool) -> MagicMock:
    backend = MagicMock()
    backend.backend_id = "claude"
    backend.default_model.return_value = "sonnet"

    async def run_command(**_: Any) -> ClaudeResponse:
        trace = current_trace()
        assert trace is not None
        trace.start("b1", "Bash", {"command": "ls"})
        trace.finish("b1", "ok")
        trace.start("a1", "Agent", {"subagent_type": "Explore"})  # never ends
        if fail:
            raise RuntimeError("boom")
        return ClaudeResponse(
            content="hi", session_id="s1", cost=0.1, duration_ms=5, num_turns=1
        )

    backend.run_command = run_command
    return backend


@pytest.mark.parametrize("fail", [False, True])
async def test_no_tool_row_is_written_after_the_link(db, fail: bool) -> None:
    sink = _OrderSink()
    router = ModelRouter(
        recorder=ModelCallRepository(db),
        versions=_Versions(),
        tool_calls=sink,  # type: ignore[arg-type]
        backends={"claude": _backend(fail)},
    )
    try:
        await router.run_turn(
            "claude", purpose="chat", prompt="p", working_directory=Path("/w")
        )
    except RuntimeError:
        assert fail
    assert sink.ops[-1] == "link"
    assert "upsert" not in sink.ops[sink.ops.index("link") :]
    # The open subagent is final at link time (detached; unfinished if the turn failed).
    assert sink.rows["a1"]["status"] == ("unfinished" if fail else "detached")
    assert sink.rows["a1"]["duration_ms"] is not None


# --- mapping edges -----------------------------------------------------------


def _row(**over: Any) -> Dict[str, Any]:
    row: Dict[str, Any] = {
        "id": 7,
        "ts": "2026-10-25 10:00:00",
        "purpose": "chat",
        "kind": "plain",
        "profile": None,
        "outcome": "timeout",
        "error": None,
        "duration_ms": 2000,
        "user_id": None,
        "session_id": None,
        "cost_usd": None,
    }
    row.update(over)
    return row


def test_plain_call_maps_to_llm_and_timeout_is_error() -> None:
    (root,) = spans_for(_row(), [])
    assert root["span_kind"] == "LLM"
    assert root["name"] == "chat"
    assert root["status_code"] == "ERROR" and root["status_message"] == "timeout"
    assert root["start_time"] == "2026-10-25T09:59:58+00:00"
    assert root["end_time"] == "2026-10-25T10:00:00+00:00"
    assert "llm.cost.total" not in root["attributes"]
    assert "user.id" not in root["attributes"]


def test_ids_are_deterministic_and_orphan_parent_falls_back_to_root() -> None:
    tools = [
        {
            "tool_use_id": "x",
            "parent_tool_use_id": "gone",
            "tool": "Read",
            "status": "ok",
            "started_at": "2026-10-25T09:59:59+00:00",
            "duration_ms": 10,
        },
    ]
    one, two = spans_for(_row(), tools), spans_for(_row(), tools)
    assert one == two
    assert one[1]["parent_id"] == span_id_of(7, "root")
    assert one[1]["span_kind"] == "TOOL"


def test_pump_job_names() -> None:
    assert pump_job("pump-dv-87d8-implement-p2") == {
        "task": "dv-87d8",
        "stage": "implement",
    }
    assert pump_job("pump-pl:dv-1234-review") == {
        "task": "pl:dv-1234",
        "stage": "review",
    }
    assert pump_job("nightly") is None and pump_job(None) is None


# --- sending edges -----------------------------------------------------------


async def _seed(db: DatabaseManager) -> int:
    mc = await ModelCallRepository(db).record(
        ModelCall(
            purpose="chat",
            kind="turn",
            harness="claude",
            harness_version=None,
            provider="anthropic",
            model="m",
            attended=True,
            profile="devops",
            user_id=1,
            session_id="s",
            input_tokens=1,
            output_tokens=1,
            cache_read_tokens=0,
            cache_creation_tokens=0,
            cost_usd=None,
            duration_ms=10,
            outcome="ok",
            error=None,
        )
    )
    assert mc is not None
    async with db.get_connection() as conn:
        await conn.execute(
            "UPDATE model_calls SET ts = datetime('now', '-300 seconds') WHERE id = ?",
            (mc,),
        )
        await conn.commit()
    return mc


async def _tick(db: DatabaseManager, handler: Any) -> List[httpx.Request]:
    seen: List[httpx.Request] = []

    def record(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return handler(req)

    async with httpx.AsyncClient(transport=httpx.MockTransport(record)) as client:
        await export_pending(
            model_calls=ModelCallRepository(db),
            tool_calls=ToolCallRepository(db),
            client=client,
            base_url="http://p.test/",
            project="devvy",
        )
    return seen


async def _marked(db: DatabaseManager, mc: int) -> bool:
    async with db.get_connection() as conn:
        cur = await conn.execute(
            "SELECT exported_at FROM model_calls WHERE id = ?", (mc,)
        )
        row = await cur.fetchone()
    return row[0] is not None


async def test_404_or_401_leaves_rows_for_the_next_tick(db) -> None:
    mc = await _seed(db)
    for status in (404, 401, 500):
        seen = await _tick(db, lambda r, s=status: httpx.Response(s))
        assert len(seen) == 1
        assert str(seen[0].url) == "http://p.test/v1/projects/devvy/spans"
        assert not await _marked(db, mc)


async def test_invalid_span_is_dropped_and_its_call_marked(db) -> None:
    mc = await _seed(db)
    root = span_id_of(mc, "root")
    replies = [
        httpx.Response(
            400,
            json={"duplicate_spans": [], "invalid_spans": [{"span_id": root}]},
        )
    ]
    seen = await _tick(db, lambda r: replies.pop(0))
    assert len(seen) == 1  # nothing left to re-send
    assert await _marked(db, mc)


async def test_registered_tick_posts_to_phoenix(db, monkeypatch) -> None:
    mc = await _seed(db)
    seen: List[httpx.Request] = []
    real = httpx.AsyncClient

    def client(**kw: Any) -> httpx.AsyncClient:
        def handler(req: httpx.Request) -> httpx.Response:
            seen.append(req)
            return httpx.Response(202, json={})

        return real(transport=httpx.MockTransport(handler), **kw)

    monkeypatch.setattr("src.observability.phoenix.httpx.AsyncClient", client)

    class Sched:
        jobs: List[Any] = []

        def add_native_job(self, name: str, cron: str, coro: Any) -> None:
            self.jobs.append(coro)

    sched = Sched()
    tick = register_phoenix_export(sched, db, "http://p.test", project="devvy-x")
    assert tick is not None and sched.jobs == [tick]
    await tick()
    assert [str(r.url) for r in seen] == ["http://p.test/v1/projects/devvy-x/spans"]
    assert json.loads(seen[0].content)["data"][0]["name"] == "chat · devops"
    assert await _marked(db, mc)
