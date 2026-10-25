"""Render a turn's harness trace (dv-2260) as text: the question, the answer
and the tree of tool calls (main agent -> subagents -> their tools).

Shared by ``scripts/trace`` and the owner-only ``/trace`` command.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from src.storage.database import DatabaseManager

USAGE = (
    "trace <message_id> | trace call:<model_call_id> | trace last [N] "
    "(no argument = newest turn)"
)


def _line(text: Optional[str], limit: int) -> str:
    flat = " ".join((text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def render_tree(rows: List[Dict[str, Any]]) -> str:
    """Indented tree; ✓ ok, ✗ failed, … no result, ⇢ detached subagent."""
    by_parent: Dict[Optional[str], List[Dict[str, Any]]] = {}
    ids = {r["tool_use_id"] for r in rows if r.get("tool_use_id")}
    for r in rows:
        parent = r.get("parent_tool_use_id")
        # A parent we never saw (older rows, cut stream) hangs off the root.
        by_parent.setdefault(parent if parent in ids else None, []).append(r)
    out: List[str] = []

    def walk(parent: Optional[str], depth: int) -> None:
        for r in by_parent.get(parent, []):
            ok = r.get("ok")
            mark = "…" if ok is None else ("✓" if ok else "✗")
            label = r["tool_name"]
            if r.get("subagent_type"):
                label += f"[{r['subagent_type']}]"
            dur = f" {r['duration_ms']}ms" if r.get("duration_ms") is not None else ""
            det = " ⇢detached" if r.get("detached") else ""
            pad = "  " * depth
            out.append(
                f"{pad}{mark} {label}{dur}{det}  {_line(r.get('input_short'), 100)}"
            )
            if r.get("error"):
                out.append(f"{pad}    error: {_line(r['error'], 160)}")
            elif r.get("result_preview"):
                out.append(f"{pad}    → {_line(r['result_preview'], 200)}")
            if r.get("tool_use_id"):
                walk(r["tool_use_id"], depth + 1)

    walk(None, 0)
    return "\n".join(out) if out else "(no tool calls)"


async def trace_report(db: "DatabaseManager", arg: str = "") -> str:
    from src.storage.tool_calls import ToolCallRepository

    repo = ToolCallRepository(db)
    arg = arg.strip()
    if arg.startswith("last"):
        n = (
            int(arg[4:].strip() or 5)
            if arg[4:].strip().isdigit() or not arg[4:].strip()
            else 5
        )
        turns = await repo.last_turns(min(n, 20))
        return (
            "\n".join(
                f"call:{t['id']} {t['ts']} {t['purpose']} {t['profile'] or '-'} "
                f"{t['outcome']} {t['calls']} tools"
                for t in turns
            )
            or "(no traced turns yet)"
        )

    message_id: Optional[int] = None
    call_id: Optional[int] = None
    if not arg:
        turns = await repo.last_turns(1)
        call_id = turns[0]["id"] if turns else None
    elif arg.startswith("call:") and arg[5:].isdigit():
        call_id = int(arg[5:])
    elif arg.isdigit():
        message_id = int(arg)
        call_id = await repo.model_call_of_message(message_id)
    else:
        return USAGE
    if call_id is None and message_id is None:
        return "(no traced turns yet)"

    rows = (
        await repo.for_model_call(call_id)
        if call_id is not None
        else await repo.for_message(message_id or 0)
    )
    head: List[str] = []
    mc = await repo.model_call(call_id) if call_id is not None else None
    if mc is not None:
        head.append(
            f"call:{mc['id']} {mc['ts']} {mc['purpose']} "
            f"profile={mc['profile'] or '-'} {mc['harness']}/{mc['model']} "
            f"{mc['outcome']} {mc['duration_ms']}ms cost={mc['cost_usd']}"
        )
    if message_id is None and rows:
        message_id = next((r["message_id"] for r in rows if r["message_id"]), None)
    msg = await repo.message(message_id) if message_id is not None else None
    if msg is not None:
        head.append(f"message:{message_id}")
        head.append(f"Q: {_line(msg['prompt'], 300)}")
        head.append(f"A: {_line(msg['response'], 300) or '(empty)'}")
    return "\n".join(head + [render_tree(rows)])
