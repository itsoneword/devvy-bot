"""The harness trace (dv-2260): every tool call of a model turn, who made it
(main agent or a subagent), how it ended, with a short preview of the result.

The router opens a :class:`TraceCollector` per turn, the backend feeds it the
stream it already reads, and rows go to a :class:`TraceSink` *as they happen*,
so a turn that is interrupted, times out or crashes keeps what it did. Rows are
keyed ``(trace_id, tool_use_id)``; ``parent_tool_use_id`` (NULL = main agent)
links a subagent's calls to the ``Agent`` call that started them. The router
links the trace to its ``model_calls`` row, the storage facade to its
``messages`` row.

A backend reaches the collector through :func:`current_trace` (a contextvar),
so no ``run_command`` signature changes.

Row ``status``: ``ok`` / ``error`` once the result arrived; ``detached`` for a
subagent still running when the turn ended (backgrounded: its result arrives
after ``ResultMessage``, which nobody reads); ``unfinished`` for a call that
never got a result (interrupt, timeout, crash) -- also the state of a call
still running while the turn streams.
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

from src.utils.log_redaction import redact, redact_secrets

logger = structlog.get_logger()

INPUT_VALUE_MAX = 300
RESULT_PREVIEW_MAX = 200
SUBAGENT_RESULT_MAX = 4000
ERROR_MAX = 500
# Claude Code calls the subagent tool `Agent`; older builds `Task`.
SUBAGENT_TOOLS = frozenset({"Agent", "Task"})
_DETACHED_MARK = "Async agent launched"

OK, ERROR, DETACHED, UNFINISHED = "ok", "error", "detached", "unfinished"


class TraceSink(Protocol):
    async def upsert(self, trace_id: str, rows: List[Dict[str, Any]]) -> None: ...


def scrub(text: str) -> str:
    return redact_secrets(redact(text))


def _clip(text: str, limit: int) -> str:
    text = scrub(text.strip())
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


def _cap_values(value: Any) -> Any:
    if isinstance(value, str):
        return _clip(value, INPUT_VALUE_MAX)
    if isinstance(value, dict):
        return {str(k): _cap_values(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_cap_values(v) for v in value]
    return value


def short_input(tool_input: Any) -> Optional[str]:
    """The call's input as JSON, every string value capped and scrubbed.
    Stays parseable, so readers can still pick out ``file_path`` etc."""
    if tool_input in (None, {}, ""):
        return None
    try:
        return json.dumps(_cap_values(tool_input), ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return json.dumps(_clip(str(tool_input), INPUT_VALUE_MAX))


class TraceCollector:
    """Rows of one turn, written to ``sink`` as they change."""

    def __init__(self, sink: Optional[TraceSink] = None) -> None:
        self.trace_id = uuid.uuid4().hex
        self._sink = sink
        self._rows: Dict[str, Dict[str, Any]] = {}
        self._done: set = set()
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
        if not tool_use_id or tool_use_id in self._rows:
            return
        self._seq += 1
        subagent_type = None
        if name in SUBAGENT_TOOLS and isinstance(tool_input, dict):
            subagent_type = tool_input.get("subagent_type") or None
        self._rows[tool_use_id] = {
            "tool_use_id": tool_use_id,
            "parent_tool_use_id": parent or None,
            "seq": self._seq,
            "tool": name or "unknown",
            "subagent_type": subagent_type,
            "input": short_input(tool_input),
            "status": UNFINISHED,
            "error": None,
            "started_at": datetime.now(UTC),
            "duration_ms": None,
            "result_bytes": None,
            "result_preview": None,
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
        if row is None or tool_use_id in self._done:
            return
        self._done.add(tool_use_id)
        text = content_text(content)
        subagent = row["tool"] in SUBAGENT_TOOLS
        limit = SUBAGENT_RESULT_MAX if subagent else RESULT_PREVIEW_MAX
        failed = bool(is_error or error)
        row["duration_ms"] = self._elapsed(tool_use_id)
        row["result_bytes"] = len(text.encode("utf-8", "replace"))
        row["result_preview"] = _clip(text, limit) if text.strip() else None
        if failed:
            row["status"] = ERROR
            row["error"] = _clip(error or text or "tool call failed", ERROR_MAX)
        elif subagent and text.startswith(_DETACHED_MARK):
            row["status"] = DETACHED
        else:
            row["status"] = OK
        self._mark(tool_use_id)

    def close(self, completed: bool = True) -> None:
        """The stream is over: whatever has no result yet gets its final
        state. After a turn that completed, a subagent still running was
        launched detached; anything else was cut off."""
        for use_id, row in self._rows.items():
            if use_id in self._done:
                continue
            self._done.add(use_id)
            if completed and row["tool"] in SUBAGENT_TOOLS:
                row["status"] = DETACHED
            else:
                row["status"] = UNFINISHED
            row["duration_ms"] = self._elapsed(use_id)
            self._mark(use_id)

    def _elapsed(self, tool_use_id: str) -> int:
        return int((time.monotonic() - self._clock[tool_use_id]) * 1000)

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
