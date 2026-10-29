"""TraceCollector edge cases (dv-2260); the end-to-end scenarios live in
test_model_router_trace.py."""

from __future__ import annotations

import json
from typing import Any, Dict, List
from unittest.mock import AsyncMock

import pytest

from src.model_router import ModelRouter
from src.model_router.trace import TraceCollector, current_trace


class _Sink:
    def __init__(self, fail: bool = False) -> None:
        self.rows: Dict[str, Dict[str, Any]] = {}
        self.fail = fail

    async def upsert(self, trace_id: str, rows: List[Dict[str, Any]]) -> None:
        if self.fail:
            raise RuntimeError("db locked")
        for r in rows:
            self.rows[r["tool_use_id"]] = r


async def test_parallel_calls_pair_by_id_and_errors_are_scrubbed() -> None:
    sink = _Sink()
    t = TraceCollector(sink)
    t.start("a", "Read", {"file_path": "/x"})
    t.start("b", "Bash", {"command": "curl -H 'Bearer abcdefghijklmnop' x"})
    t.finish("b", "Authorization: Bearer abcdefghijklmnop rejected", is_error=True)
    t.finish("a", "ok text")
    t.finish("a", "late duplicate")  # a second result never overwrites
    await t.flush()

    assert sink.rows["a"]["status"] == "ok"
    assert sink.rows["a"]["result_preview"] == "ok text"
    assert sink.rows["b"]["status"] == "error"
    assert "abcdefghijklmnop" not in sink.rows["b"]["error"]
    assert "abcdefghijklmnop" not in sink.rows["b"]["input"]
    assert [sink.rows[k]["seq"] for k in ("a", "b")] == [1, 2]


def test_input_stays_json_with_nested_values_capped() -> None:
    t = TraceCollector()
    t.start(
        "a",
        "Write",
        {"file_path": "/f", "content": "z" * 5000, "x": [{"y": "q" * 900}]},
    )
    data = json.loads(t.rows()[0]["input"])
    assert data["file_path"] == "/f"
    assert len(data["content"]) <= 300
    assert len(data["x"][0]["y"]) <= 300


def test_failed_turn_leaves_running_subagent_unfinished_not_detached() -> None:
    t = TraceCollector()
    t.start("a", "Agent", {"subagent_type": "Explore"})
    t.start("b", "Read", {})
    t.close(completed=False)
    rows = {r["tool_use_id"]: r for r in t.rows()}
    assert rows["a"]["status"] == "unfinished"
    assert rows["b"]["status"] == "unfinished"
    assert rows["a"]["duration_ms"] is not None


async def test_sink_failure_never_breaks_the_turn() -> None:
    t = TraceCollector(_Sink(fail=True))
    t.start("a", "Read", {})
    t.finish("a", "x")
    await t.flush()  # logged, not raised
    assert t.rows()[0]["status"] == "ok"


async def test_router_without_trace_store_still_exposes_the_trace() -> None:
    """No tool_calls repo wired (unit-test routers): the turn runs, the
    backend still sees a collector, nothing is written."""
    seen: List[Any] = []

    async def run_command(**kw: Any) -> Any:
        seen.append(current_trace())
        resp = AsyncMock()
        resp.is_error = False
        return resp

    backend = AsyncMock()
    backend.backend_id = "claude"
    backend.run_command = run_command
    router = ModelRouter(backends={"claude": backend})
    await router.run_turn("claude", purpose="chat", prompt="p", model="haiku")
    assert isinstance(seen[0], TraceCollector)
    assert current_trace() is None


async def test_router_failure_path_without_store_reraises() -> None:
    backend = AsyncMock()
    backend.backend_id = "claude"
    backend.run_command = AsyncMock(side_effect=RuntimeError("boom"))
    router = ModelRouter(backends={"claude": backend})
    with pytest.raises(RuntimeError):
        await router.run_turn("claude", purpose="chat", prompt="p", model="haiku")
