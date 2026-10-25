"""The harness trace (dv-2260): every tool call of a model turn, who made it
(main agent or a subagent), how it ended, with a short preview of the result.

One mechanism for bot turns, background jobs and the task pump: the router
(bot) or ``run_job`` (job process) opens a :class:`TraceCollector`, a backend
feeds it the stream it already reads, and rows go to a :class:`TraceSink`
*as they happen*, so a turn that is interrupted, times out or crashes keeps
what it did. Rows are keyed ``(trace_id, tool_use_id)``; ``parent_tool_use_id``
(null = main agent) links a subagent's calls to the ``Agent`` call that started
them. The router later links the trace to its ``model_calls`` row, the storage
facade to the ``messages`` row.

A backend reaches the collector through :func:`current_trace` (a contextvar),
so no ``run_command`` signature changes.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
import uuid
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any, Dict, Iterator, List, Optional, Protocol

import structlog

from src.utils.log_redaction import redact_secrets

logger = structlog.get_logger()

INPUT_MAX = 300
RESULT_PREVIEW_MAX = 200
SUBAGENT_RESULT_MAX = 4000
# Claude Code calls the subagent tool `Agent`; older builds `Task`.
SUBAGENT_TOOLS = frozenset({"Agent", "Task"})
_DETACHED_MARK = "Async agent launched"


class TraceSink(Protocol):
    async def upsert(self, trace_id: str, rows: List[Dict[str, Any]]) -> None: ...


def _clip(text: str, limit: int) -> str:
    text = redact_secrets(text.strip())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def content_text(content: Any) -> str:
    """A tool result's content (str | list of blocks | None) as plain text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(parts)
    return str(content)


def short_input(tool_input: Any) -> str:
    if tool_input in (None, {}, ""):
        return ""
    try:
        text = (
            tool_input
            if isinstance(tool_input, str)
            else json.dumps(tool_input, ensure_ascii=False, default=str)
        )
    except (TypeError, ValueError):
        text = str(tool_input)
    return _clip(text, INPUT_MAX)


class TraceCollector:
    """Rows of one turn, written to ``sink`` as they change."""

    def __init__(self, sink: Optional[TraceSink] = None) -> None:
        self.trace_id = uuid.uuid4().hex
        self._sink = sink
        self._rows: Dict[str, Dict[str, Any]] = {}
        self._clock: Dict[str, float] = {}
        self._dirty: Dict[str, None] = {}
        self._writer: Optional["asyncio.Task[None]"] = None
        self._seq = 0

    # -- recording ---------------------------------------------------------

    def start(
        self,
        tool_use_id: str,
        name: str,
        tool_input: Any,
        parent: Optional[str] = None,
    ) -> None:
        if tool_use_id in self._rows:
            return
        self._seq += 1
        subagent_type = None
        detached = 0
        if name in SUBAGENT_TOOLS and isinstance(tool_input, dict):
            subagent_type = tool_input.get("subagent_type") or None
            detached = 1 if tool_input.get("run_in_background") else 0
        self._rows[tool_use_id] = {
            "tool_use_id": tool_use_id,
            "parent_tool_use_id": parent or None,
            "seq": self._seq,
            "tool_name": name or "unknown",
            "subagent_type": subagent_type,
            "input_short": short_input(tool_input),
            "ok": None,
            "error": None,
            "started_at": datetime.now(UTC).isoformat(),
            "duration_ms": None,
            "result_bytes": None,
            "result_preview": None,
            "detached": detached,
        }
        self._clock[tool_use_id] = time.monotonic()
        self._mark(tool_use_id)

    def finish(
        self,
        tool_use_id: str,
        content: Any = None,
        is_error: bool = False,
        error: Optional[str] = None,
    ) -> None:
        row = self._rows.get(tool_use_id)
        if row is None or row["ok"] is not None:
            return
        text = content_text(content)
        limit = (
            SUBAGENT_RESULT_MAX
            if row["tool_name"] in SUBAGENT_TOOLS
            else RESULT_PREVIEW_MAX
        )
        failed = bool(is_error or error)
        row["ok"] = 0 if failed else 1
        row["duration_ms"] = int((time.monotonic() - self._clock[tool_use_id]) * 1000)
        row["result_bytes"] = len(text.encode("utf-8", "replace"))
        row["result_preview"] = _clip(text, limit) if text.strip() else None
        if failed:
            row["error"] = _clip(
                error or text or "tool call failed", RESULT_PREVIEW_MAX
            )
        if row["tool_name"] in SUBAGENT_TOOLS and text.startswith(_DETACHED_MARK):
            row["detached"] = 1
        self._mark(tool_use_id)

    def close(self, reason: str = "turn ended") -> None:
        """The stream is over: whatever has no result yet gets its final
        state. A subagent still running was launched detached; anything else
        was cut off (interrupt, timeout, crash)."""
        for use_id, row in self._rows.items():
            if row["ok"] is not None:
                continue
            if row["tool_name"] in SUBAGENT_TOOLS:
                row["detached"] = 1
            else:
                row["error"] = f"no result ({reason})"
            row["duration_ms"] = int((time.monotonic() - self._clock[use_id]) * 1000)
            self._mark(use_id)

    # -- feeding -----------------------------------------------------------

    def feed_sdk(self, message: Any) -> None:
        """One Claude Agent SDK stream message."""
        try:
            content = getattr(message, "content", None)
            if not isinstance(content, list):
                return
            parent = getattr(message, "parent_tool_use_id", None)
            for block in content:
                kind = type(block).__name__
                if kind == "ToolUseBlock":
                    self.start(
                        str(getattr(block, "id", "") or ""),
                        str(getattr(block, "name", "") or ""),
                        getattr(block, "input", None),
                        parent,
                    )
                elif kind == "ToolResultBlock":
                    self.finish(
                        str(getattr(block, "tool_use_id", "") or ""),
                        getattr(block, "content", None),
                        bool(getattr(block, "is_error", False)),
                    )
        except Exception as exc:  # tracing must never break a turn
            logger.warning("trace feed failed", error=str(exc))

    # -- output ------------------------------------------------------------

    def rows(self) -> List[Dict[str, Any]]:
        return [dict(r) for r in self._rows.values()]

    def _mark(self, tool_use_id: str) -> None:
        if self._sink is None:
            return
        self._dirty[tool_use_id] = None
        if self._writer is None or self._writer.done():
            try:
                self._writer = asyncio.get_running_loop().create_task(self._drain())
            except RuntimeError:
                self._writer = None  # no loop: stays in memory, rows() has it

    async def _drain(self) -> None:
        while self._dirty and self._sink is not None:
            ids = list(self._dirty)
            self._dirty.clear()
            batch = [dict(self._rows[i]) for i in ids]
            try:
                await self._sink.upsert(self.trace_id, batch)
            except Exception as exc:
                logger.warning("trace rows not written", error=str(exc))

    async def flush(self) -> None:
        """Wait until every row so far is written (a failed write is logged)."""
        while self._sink is not None and (
            self._dirty or (self._writer is not None and not self._writer.done())
        ):
            if self._writer is None or self._writer.done():
                self._writer = asyncio.get_running_loop().create_task(self._drain())
            await asyncio.shield(self._writer)


_current: ContextVar[Optional[TraceCollector]] = ContextVar("trace", default=None)


def current_trace() -> Optional[TraceCollector]:
    return _current.get()


@contextlib.contextmanager
def activate(collector: TraceCollector) -> Iterator[TraceCollector]:
    token = _current.set(collector)
    try:
        yield collector
    finally:
        _current.reset(token)
