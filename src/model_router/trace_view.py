"""Render one call's harness trace as text (dv-2260 phase 3).

Shared by the owner's ``/trace`` command and ``scripts/trace``; the data comes
from :meth:`src.storage.tool_calls.ToolCallRepository.trace`. One line per
tool call, a subagent's calls indented under the ``Agent`` call that started
them, its final result on a ``↳`` line below::

    ✓ Read /x/README · 1.2s
    ✓ Agent general-purpose: scan · 3.4s
      ✓ Grep ^# · 12ms
      ↳ Subagent report: README has 3 sections.
    ✗ Bash wc -l nope · 8ms — wc: nope: No such file or directory
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .trace import DETACHED, ERROR, OK, SUBAGENT_TOOLS

MARK = {OK: "✓", ERROR: "✗", DETACHED: "⏳"}
UNFINISHED_MARK = "⋯"
INDENT = "  "
INPUT_SHOWN = 100
ERROR_SHOWN = 160
# The input field that says most about a call, in order of preference.
_KEY_FIELDS = (
    "command",
    "file_path",
    "notebook_path",
    "pattern",
    "path",
    "url",
    "query",
    "skill",
    "description",
    "prompt",
)

_CUT_JSON = re.compile(r'^\{"\w+": "(.*)', re.S)


@dataclass(frozen=True)
class TraceText:
    head: str  # call line, question, answer
    tree: str  # one line per tool call

    @property
    def text(self) -> str:
        return f"{self.head}\n\n{self.tree}"


def duration(ms: Optional[int]) -> str:
    if ms is None:
        return "?"
    if ms < 1000:
        return f"{int(ms)}ms"
    return f"{ms / 1000:.1f}s"


def _one_line(text: Any, limit: Optional[int]) -> str:
    flat = " ".join(str(text or "").split())
    if limit is not None and len(flat) > limit:
        return flat[: limit - 1] + "…"
    return flat


def input_summary(raw: Optional[str]) -> str:
    """The stored JSON input, reduced to its telling field."""
    if not raw:
        return ""
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        # Rows migrated from tool_usage hold JSON cut at a fixed length.
        cut = _CUT_JSON.match(raw)
        return _one_line(cut.group(1) if cut else raw, INPUT_SHOWN)
    if isinstance(value, dict):
        if value.get("subagent_type"):
            what = value.get("description") or value.get("prompt") or ""
            return _one_line(f"{value['subagent_type']}: {what}", INPUT_SHOWN)
        for key in _KEY_FIELDS:
            if value.get(key):
                return _one_line(value[key], INPUT_SHOWN)
        value = json.dumps(value, ensure_ascii=False)
    return _one_line(value, INPUT_SHOWN)


def _tool_line(row: Dict[str, Any]) -> str:
    status = str(row.get("status") or "")
    mark = MARK.get(status, UNFINISHED_MARK)
    parts = [f"{mark} {row.get('tool') or 'unknown'}"]
    summary = input_summary(row.get("input"))
    if summary:
        parts.append(f" {summary}")
    if status == DETACHED:
        parts.append(" · detached")
    elif status in (OK, ERROR):
        parts.append(f" · {duration(row.get('duration_ms'))}")
    else:
        parts.append(" · unfinished")
    if status == ERROR and row.get("error"):
        parts.append(f" — {_one_line(row['error'], ERROR_SHOWN)}")
    return "".join(parts)


def tree_lines(
    rows: List[Dict[str, Any]], result_limit: Optional[int] = None
) -> List[str]:
    """Rows in call order, subagent calls under their ``Agent`` call. A row
    whose parent is not in the trace is shown at the top level."""
    rows = sorted(rows, key=lambda r: (r.get("seq") or 0, r.get("id") or 0))
    ids = {r.get("tool_use_id") for r in rows if r.get("tool_use_id")}
    children: Dict[Optional[str], List[Dict[str, Any]]] = {}
    for row in rows:
        parent = row.get("parent_tool_use_id")
        children.setdefault(parent if parent in ids else None, []).append(row)

    out: List[str] = []

    def walk(parent: Optional[str], depth: int) -> None:
        for row in children.get(parent, []):
            out.append(INDENT * depth + _tool_line(row))
            use_id = row.get("tool_use_id")
            if use_id and use_id != parent:
                walk(use_id, depth + 1)
            if row.get("tool") in SUBAGENT_TOOLS and row.get("result_preview"):
                if row.get("status") in (OK, ERROR):
                    result = _one_line(row["result_preview"], result_limit)
                    out.append(INDENT * (depth + 1) + f"↳ {result}")

    walk(None, 0)
    return out


def render_trace(
    trace: Dict[str, Any],
    *,
    text_limit: Optional[int] = None,
    result_limit: Optional[int] = None,
) -> TraceText:
    """``trace`` as :meth:`ToolCallRepository.trace` returns it. The limits
    cap the question/answer and the subagent results (None = in full)."""
    call = trace["call"]
    message = trace.get("message")
    rows = trace.get("rows") or []

    facts = [f"#{call['id']}", str(call.get("purpose") or "?")]
    if call.get("profile"):
        facts.append(str(call["profile"]))
    facts.append(
        " ".join(str(x) for x in (call.get("harness"), call.get("model")) if x)
    )
    facts.append(str(call.get("outcome") or "?"))
    if call.get("ts"):
        facts.append(str(call["ts"])[:19])
    facts.append(duration(call.get("duration_ms")))
    if call.get("cost_usd") is not None:
        facts.append(f"${float(call['cost_usd']):.4f}")
    head = ["Trace " + " · ".join(f for f in facts if f)]
    if message:
        head.append(f"Q: {_one_line(message.get('prompt'), text_limit)}")
        answer = message.get("response") or "(empty answer)"
        head.append(f"A: {_one_line(answer, text_limit)}")
    else:
        head.append("Q: (no chat message: job or internal call)")
    if call.get("error"):
        head.append(f"Error: {_one_line(call['error'], ERROR_SHOWN)}")

    lines = tree_lines(rows, result_limit)
    tree = f"Tool calls: {len(rows)}"
    if lines:
        tree += "\n" + "\n".join(lines)
    return TraceText(head="\n".join(head), tree=tree)
