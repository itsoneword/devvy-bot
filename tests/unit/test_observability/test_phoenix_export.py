"""devvy -> Phoenix exporter (dv-87d8 phase 1).

Finished ``model_calls`` rows plus their linked ``tool_calls`` are projected
into OpenInference spans and POSTed to Phoenix REST
``POST {base_url}/v1/projects/{project}/spans`` with body ``{"data": [span]}``.
See ``docs/research/dv-87d8/analysis.md`` Phase 1.

Contract these tests pin (module ``src.observability.phoenix``):

* ``spans_for(call_row, tool_rows) -> list[dict]`` -- pure mapping.
* ``async export_pending(*, model_calls, tool_calls, client, base_url, project)``
  -- one tick: select unexported calls >=120 s old, their tools in one query,
  POST, mark exported. Never raises.
* ``register_phoenix_export(scheduler, db, phoenix_url, project="devvy")`` --
  adds native job ``phoenix-export`` (every minute) when ``phoenix_url`` is
  set; registers nothing and returns ``None`` when it is not.
* ``ModelCall.job_name``, ``ModelCallRepository.unexported`` /
  ``mark_exported``, ``ToolCallRepository.for_model_calls(ids)``.

Span shape (Phoenix REST): ``name, context{trace_id, span_id}, span_kind,
parent_id, start_time, end_time, status_code, status_message, attributes``.
Attributes may be flat dotted keys or nested; ``metadata`` a dict or JSON.
"""

from __future__ import annotations

import hashlib
import json
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

import httpx
import pytest

from src.storage.database import DatabaseManager
from src.storage.model_calls import ModelCall, ModelCallRepository
from src.storage.tool_calls import ToolCallRepository

BASE = "http://phoenix.test"
PROJECT = "devvy"


@pytest.fixture
async def db(tmp_path):
    manager = DatabaseManager(f"sqlite:///{tmp_path / 'test.db'}")
    await manager.initialize()
    yield manager
    await manager.close()


# --- helpers ---------------------------------------------------------------


def _call(**over: Any) -> ModelCall:
    fields: Dict[str, Any] = dict(
        purpose="chat",
        kind="turn",
        harness="claude",
        harness_version="2.1.300",
        provider="anthropic",
        model="claude-sonnet-4-6",
        attended=True,
        profile="devops",
        user_id=42,
        session_id="sess-1",
        input_tokens=100,
        output_tokens=50,
        cache_read_tokens=1000,
        cache_creation_tokens=200,
        cost_usd=0.25,
        duration_ms=30_000,
        outcome="ok",
        error=None,
    )
    fields.update(over)
    return ModelCall(**fields)


def _tool(
    tid: str,
    name: str,
    seq: int,
    parent: Optional[str] = None,
    status: str = "ok",
    error: Optional[str] = None,
    subagent_type: Optional[str] = None,
    duration_ms: Optional[int] = 1000,
) -> Dict[str, Any]:
    return {
        "tool_use_id": tid,
        "parent_tool_use_id": parent,
        "seq": seq,
        "tool": name,
        "subagent_type": subagent_type,
        "input": f"{name} input",
        "status": status,
        "error": error,
        "started_at": "2026-10-25T10:00:00+00:00",
        "duration_ms": duration_ms,
        "result_bytes": 10,
        "result_preview": f"{name} result",
    }


async def _seed(
    db: DatabaseManager,
    call: ModelCall,
    tools: List[Dict[str, Any]] = (),  # type: ignore[assignment]
    age_s: int = 300,
) -> int:
    """Record a call, age it by ``age_s`` seconds, link ``tools`` to it."""
    mc_id = await ModelCallRepository(db).record(call)
    assert mc_id is not None
    async with db.get_connection() as conn:
        await conn.execute(
            "UPDATE model_calls SET ts = datetime('now', ?) WHERE id = ?",
            (f"-{age_s} seconds", mc_id),
        )
        await conn.commit()
    if tools:
        trace = f"tr-{mc_id}"
        repo = ToolCallRepository(db)
        await repo.upsert(trace, list(tools))
        await repo.link_model_call(trace, mc_id)
    return mc_id


async def _exported(db: DatabaseManager) -> Dict[int, Any]:
    async with db.get_connection() as conn:
        cur = await conn.execute("SELECT id, exported_at FROM model_calls")
        return {int(r[0]): r[1] for r in await cur.fetchall()}


class Phoenix:
    """httpx MockTransport recording every request; replies from a script."""

    def __init__(self, *replies: Any) -> None:
        self.replies = list(replies)
        self.requests: List[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        reply = self.replies.pop(0) if self.replies else 202
        if isinstance(reply, Exception):
            raise reply
        if callable(reply):
            return reply(request)
        if isinstance(reply, tuple):
            return httpx.Response(reply[0], json=reply[1])
        return httpx.Response(reply, json={})

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))

    def spans(self, i: int = -1) -> List[Dict[str, Any]]:
        return json.loads(self.requests[i].content)["data"]


async def _tick(db: DatabaseManager, phoenix: Phoenix) -> None:
    from src.observability.phoenix import export_pending

    async with phoenix.client() as client:
        await export_pending(
            model_calls=ModelCallRepository(db),
            tool_calls=ToolCallRepository(db),
            client=client,
            base_url=BASE,
            project=PROJECT,
        )


def _attr(span: Dict[str, Any], key: str) -> Any:
    attrs = span.get("attributes") or {}
    if key in attrs:
        return attrs[key]
    node: Any = attrs
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _meta(span: Dict[str, Any]) -> Dict[str, Any]:
    meta = _attr(span, "metadata")
    if isinstance(meta, str):
        meta = json.loads(meta)
    return meta or {}


def _sid(span: Dict[str, Any]) -> str:
    return span["context"]["span_id"]


def _root(spans: List[Dict[str, Any]]) -> Dict[str, Any]:
    (root,) = [s for s in spans if not s.get("parent_id")]
    return root


def _by_name(spans: List[Dict[str, Any]], name: str) -> Dict[str, Any]:
    (span,) = [s for s in spans if s["name"] == name]
    return span


def _h(text: str, n: int) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:n]


# --- 1. chat turn lands in project "devvy" ---------------------------------


async def test_chat_turn_lands_in_devvy_with_root_tools_and_tokens(db) -> None:
    mc = await _seed(
        db,
        _call(),
        [_tool("b1", "Bash", 1), _tool("r1", "Read", 2), _tool("g1", "Grep", 3)],
    )
    phoenix = Phoenix(202)
    await _tick(db, phoenix)

    assert len(phoenix.requests) == 1
    req = phoenix.requests[0]
    assert req.method == "POST"
    assert str(req.url) == f"{BASE}/v1/projects/devvy/spans"
    spans = phoenix.spans()
    assert len(spans) == 4

    root = _root(spans)
    assert root["span_kind"] == "AGENT"
    assert root["name"] == "chat · devops"
    assert root["status_code"] == "OK"
    assert root["context"]["trace_id"] == _h(f"devvy:mc:{mc}", 32)
    assert _sid(root) == _h(f"devvy:mc:{mc}:root", 16)
    assert _attr(root, "llm.model_name") == "claude-sonnet-4-6"
    assert _attr(root, "session.id") == "sess-1"
    assert str(_attr(root, "user.id")) == "42"
    assert _attr(root, "llm.token_count.prompt") == 100 + 1000 + 200
    assert _attr(root, "llm.token_count.completion") == 50
    assert _attr(root, "llm.token_count.total") == 1350
    assert _attr(root, "llm.cost.total") == pytest.approx(0.25)
    meta = _meta(root)
    assert meta["service"] == "devvy" and meta["agent"] == "devops"
    assert meta["purpose"] == "chat" and meta["model_call_id"] == mc

    for name in ("Bash", "Read", "Grep"):
        tool = _by_name(spans, name)
        assert tool["span_kind"] == "TOOL"
        assert tool["parent_id"] == _sid(root)
        assert tool["context"]["trace_id"] == root["context"]["trace_id"]
        assert _attr(tool, "tool.name") == name
    # The owner's 1.1 answer: no chat question/answer text on the root.
    assert _attr(root, "input.value") in (None, "")
    assert _attr(root, "output.value") in (None, "")

    assert (await _exported(db))[mc] is not None
    # Exported once: the next tick has nothing to send.
    again = Phoenix(202)
    await _tick(db, again)
    assert again.requests == []


# --- 2. pump stage trace ---------------------------------------------------


async def test_pump_stage_trace_is_named_with_task_and_stage(db) -> None:
    await _seed(
        db,
        _call(
            purpose="background_job",
            attended=False,
            job_name="pump-dv-87d8-implement-p2",
        ),
    )
    phoenix = Phoenix(202)
    await _tick(db, phoenix)

    root = _root(phoenix.spans())
    assert root["name"] == "pump implement dv-87d8"
    meta = _meta(root)
    assert meta["task_id"] == "dv-87d8"
    assert meta["pump_stage"] == "implement"
    assert meta["job_name"] == "pump-dv-87d8-implement-p2"
    assert "pump" in (_attr(root, "tag.tags") or [])


async def test_non_pump_job_is_named_job(db) -> None:
    await _seed(db, _call(purpose="background_job", job_name="nightly-digest"))
    phoenix = Phoenix(202)
    await _tick(db, phoenix)
    root = _root(phoenix.spans())
    assert root["name"] == "job nightly-digest"
    assert "task_id" not in _meta(root) or _meta(root)["task_id"] is None


# --- 3. subagent nesting and ERROR status ----------------------------------


async def test_subagent_tools_nest_and_failures_show_error(db) -> None:
    await _seed(
        db,
        _call(outcome="error", error="turn blew up"),
        [
            _tool("a1", "Agent", 1, subagent_type="Explore"),
            _tool("r1", "Read", 2, parent="a1"),
            _tool("g1", "Grep", 3, parent="a1"),
            _tool("b1", "Bash", 4, status="error", error="denied"),
            _tool("a2", "Agent", 5, status="detached", duration_ms=None),
        ],
    )
    phoenix = Phoenix(202)
    await _tick(db, phoenix)
    spans = phoenix.spans()
    root = _root(spans)

    assert root["status_code"] == "ERROR"
    assert "turn blew up" in (root.get("status_message") or "")

    agent = next(
        s for s in spans if s["name"] == "Agent" and not _meta(s).get("detached")
    )
    assert agent["span_kind"] == "AGENT"
    assert agent["parent_id"] == _sid(root)
    for name in ("Read", "Grep"):
        child = _by_name(spans, name)
        assert child["parent_id"] == _sid(agent)
        assert child["span_kind"] == "TOOL"

    bash = _by_name(spans, "Bash")
    assert bash["status_code"] == "ERROR"
    assert "denied" in (bash.get("status_message") or "")

    # A background (detached) subagent is not a failure.
    detached = next(s for s in spans if _meta(s).get("detached"))
    assert detached["span_kind"] == "AGENT"
    assert detached["status_code"] != "ERROR"
    assert detached["end_time"] == root["end_time"]


# --- 4. Phoenix down / half-duplicate batch --------------------------------


async def test_phoenix_down_marks_nothing_and_catches_up_later(db) -> None:
    mc = await _seed(db, _call(), [_tool("b1", "Bash", 1)])

    down = Phoenix(httpx.ConnectError("refused"))
    await _tick(db, down)  # must not raise
    assert (await _exported(db))[mc] is None

    busy = Phoenix(503)
    await _tick(db, busy)
    assert (await _exported(db))[mc] is None

    back = Phoenix(202)
    await _tick(db, back)
    assert len(back.requests) == 1
    assert len(back.spans()) == 2
    assert (await _exported(db))[mc] is not None

    after = Phoenix(202)
    await _tick(db, after)
    assert after.requests == []


async def test_half_duplicate_batch_resends_only_new_spans_once(db) -> None:
    c1 = await _seed(db, _call(), [_tool("b1", "Bash", 1), _tool("r1", "Read", 2)])
    c2 = await _seed(db, _call(), [_tool("b2", "Bash", 1)])
    c3 = await _seed(db, _call())

    dup_root = _h(f"devvy:mc:{c1}:root", 16)
    dup_tool = _h(f"devvy:mc:{c1}:b1", 16)
    trace1 = _h(f"devvy:mc:{c1}", 32)
    body = {
        "total_duplicates": 2,
        "total_invalid": 0,
        "duplicate_spans": [
            {"span_id": dup_root, "trace_id": trace1},
            {"span_id": dup_tool, "trace_id": trace1},
        ],
        "invalid_spans": [],
    }
    phoenix = Phoenix((400, body), 202)
    await _tick(db, phoenix)

    assert len(phoenix.requests) == 2
    first = {_sid(s) for s in phoenix.spans(0)}
    second = {_sid(s) for s in phoenix.spans(1)}
    assert {dup_root, dup_tool} <= first
    assert dup_root not in second and dup_tool not in second
    assert second == first - {dup_root, dup_tool}
    assert _h(f"devvy:mc:{c1}:r1", 16) in second
    assert _h(f"devvy:mc:{c2}:root", 16) in second
    assert _h(f"devvy:mc:{c3}:root", 16) in second

    marks = await _exported(db)
    assert all(marks[c] is not None for c in (c1, c2, c3))


async def test_second_400_falls_back_per_call_and_never_raises(db) -> None:
    ids = [await _seed(db, _call()) for _ in range(3)]
    garbage = (400, {"detail": "nope"})
    phoenix = Phoenix(garbage, garbage, 202, 202, 202)
    await _tick(db, phoenix)
    # 1 batch + 1 retry at most before per-call fallback.
    assert len(phoenix.requests) <= 2 + len(ids)
    marks = await _exported(db)
    assert all(marks[c] is not None for c in ids)


# --- 5. PHOENIX_URL unset; constant queries per tick -----------------------


class FakeScheduler:
    def __init__(self) -> None:
        self.jobs: list = []

    def add_native_job(self, name, cron_expression, coroutine) -> None:
        self.jobs.append((name, cron_expression, coroutine))


def test_phoenix_url_defaults_unset() -> None:
    from src.config.settings import Settings

    assert Settings.model_fields["phoenix_url"].default is None
    assert Settings.model_fields["phoenix_project"].default == "devvy"


async def test_unset_phoenix_url_schedules_nothing(db) -> None:
    from src.observability.phoenix import register_phoenix_export

    sched = FakeScheduler()
    assert register_phoenix_export(sched, db, None) is None
    assert register_phoenix_export(sched, db, "") is None
    assert sched.jobs == []

    register_phoenix_export(sched, db, BASE, project=PROJECT)
    ((name, cron, coro),) = sched.jobs
    assert name == "phoenix-export"
    assert cron == "* * * * *"  # with the 120 s lag: lands within ~3 min


def _count_queries(db: DatabaseManager) -> List[str]:
    """Patch ``db.get_connection`` so every execute on it is recorded."""
    seen: List[str] = []
    original = db.get_connection

    class Counting:
        def __init__(self, conn: Any) -> None:
            self._conn = conn

        async def execute(self, sql: str, *a: Any, **kw: Any) -> Any:
            seen.append(sql)
            return await self._conn.execute(sql, *a, **kw)

        async def executemany(self, sql: str, *a: Any, **kw: Any) -> Any:
            seen.append(sql)
            return await self._conn.executemany(sql, *a, **kw)

        def __getattr__(self, name: str) -> Any:
            return getattr(self._conn, name)

    @asynccontextmanager
    async def counting():  # type: ignore[no-untyped-def]
        async with original() as conn:
            yield Counting(conn)

    db.get_connection = counting  # type: ignore[method-assign]
    return seen


async def _queries_for(tmp_path, n: int) -> int:
    db = DatabaseManager(f"sqlite:///{tmp_path / f'q{n}.db'}")
    await db.initialize()
    try:
        for _ in range(n):
            await _seed(db, _call(), [_tool("b", "Bash", 1), _tool("r", "Read", 2)])
        seen = _count_queries(db)
        phoenix = Phoenix(202)
        await _tick(db, phoenix)
        assert len(phoenix.requests) == 1
        del db.get_connection  # back to the class method
        assert all(v is not None for v in (await _exported(db)).values())
        return len(seen)
    finally:
        await db.close()


async def test_db_queries_per_tick_do_not_grow_with_rows(tmp_path) -> None:
    few = await _queries_for(tmp_path, 5)
    many = await _queries_for(tmp_path, 50)
    assert few == many
    assert few <= 3


# --- added -----------------------------------------------------------------


async def test_rows_younger_than_120s_wait(db) -> None:
    """added: a call still settling (cost, trace) is not exported yet."""
    fresh = await _seed(db, _call(), age_s=10)
    phoenix = Phoenix(202)
    await _tick(db, phoenix)
    assert phoenix.requests == []
    assert (await _exported(db))[fresh] is None


async def test_crash_after_post_replays_as_duplicates_without_third_post(
    db, monkeypatch
) -> None:
    """added: POST 202, then mark_exported crashes; next tick's duplicates-only
    400 marks everything with no further request."""
    mc = await _seed(db, _call(), [_tool("b1", "Bash", 1)])

    async def boom(self, ids):  # type: ignore[no-untyped-def]
        raise RuntimeError("db locked")

    with monkeypatch.context() as m:
        m.setattr(ModelCallRepository, "mark_exported", boom)
        first = Phoenix(202)
        await _tick(db, first)  # must not raise
    assert (await _exported(db))[mc] is None
    sent = first.spans()

    body = {
        "total_duplicates": len(sent),
        "total_invalid": 0,
        "duplicate_spans": [
            {"span_id": _sid(s), "trace_id": s["context"]["trace_id"]} for s in sent
        ],
        "invalid_spans": [],
    }
    replay = Phoenix((400, body))
    await _tick(db, replay)
    assert len(replay.requests) == 1
    assert {_sid(s) for s in replay.spans(0)} == {_sid(s) for s in sent}
    assert (await _exported(db))[mc] is not None


async def test_for_model_calls_groups_in_one_query(db) -> None:
    """added: batched tool lookup, ordered by seq, empty list for a miss."""
    a = await _seed(db, _call(), [_tool("x2", "Read", 2), _tool("x1", "Bash", 1)])
    b = await _seed(db, _call(), [_tool("y1", "Grep", 1)])
    seen = _count_queries(db)
    got = await ToolCallRepository(db).for_model_calls([a, b, 999_999])
    assert len(seen) == 1
    assert [r["tool_use_id"] for r in got[a]] == ["x1", "x2"]
    assert [r["tool_use_id"] for r in got[b]] == ["y1"]
    assert got.get(999_999, []) == []
