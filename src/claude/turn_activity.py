"""Per-turn activity ledger: names what a Claude turn was waiting on (dv-586f).

Turns have stalled for 10-30 minutes after the last tool call / final answer
with nothing in any log. The CLI can be blocked on three things: the Anthropic
API, a control request it sent to us (PreToolUse hook, ``can_use_tool``, an
in-process MCP tool), or us not reading its stream. This ledger records, per
turn, the last event of each kind with its age, the control requests currently
open (with how long they have been open), and the longest silent gap — so the
next stall log names the culprit instead of "no events".

Pure bookkeeping plus a best-effort ``/proc`` + ``ss`` probe of the CLI. No
SDK imports; the stream loop, the hook wrappers and the MCP tool shim feed it.
"""

from __future__ import annotations

import asyncio
import functools
import itertools
import os
import re
import time
from contextvars import ContextVar
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

import structlog

logger = structlog.get_logger()

# A control request (hook / permission / MCP tool) slower than this is logged
# when it returns; faster ones only update the ledger.
SLOW_CALL_LOG_SECONDS = 5.0
# A quiet CLI is described in the log after this long, then every interval.
STALL_LOG_AFTER_SECONDS = 30.0
STALL_LOG_INTERVAL_SECONDS = 60.0

_ids = itertools.count(1)


class TurnActivity:
    """Last-event-per-kind ledger for one turn."""

    def __init__(self, label: str = "") -> None:
        self.label = label
        self.started = time.monotonic()
        self.last: Dict[str, Tuple[float, str]] = {}
        self.counts: Dict[str, int] = {}
        self.open: Dict[int, Tuple[str, str, float]] = {}
        self._last_any = self.started
        self._last_any_kind = "start"
        self.max_gap = 0.0
        self.max_gap_between = ("", "")
        self.last_stall_log = 0.0

    # -- recording ---------------------------------------------------------
    def note(self, kind: str, detail: str = "") -> None:
        now = time.monotonic()
        gap = now - self._last_any
        if gap > self.max_gap:
            self.max_gap = gap
            self.max_gap_between = (self._last_any_kind, kind)
        self._last_any = now
        self._last_any_kind = kind
        self.last[kind] = (now, detail)
        self.counts[kind] = self.counts.get(kind, 0) + 1

    def begin(self, kind: str, detail: str = "") -> int:
        token = next(_ids)
        self.open[token] = (kind, detail, time.monotonic())
        self.note(f"{kind}:in", detail)
        return token

    def end(self, token: int, outcome: str = "ok", owner_wait: float = 0.0) -> float:
        """Close a control call. ``owner_wait`` is the part of it spent waiting
        for the owner's Approve/Deny tap (dv-d760): it is reported, but only
        the self-time (total minus that wait) makes a "Slow control call"."""
        entry = self.open.pop(token, None)
        if entry is None:
            return 0.0
        kind, detail, t0 = entry
        took = time.monotonic() - t0
        self_time = max(0.0, took - owner_wait)
        out = f"{detail} {outcome} {took:.1f}s"
        if owner_wait > 0:
            out += f" (owner {owner_wait:.1f}s)"
        self.note(f"{kind}:out", out.strip())
        if self_time >= SLOW_CALL_LOG_SECONDS:
            logger.warning(
                "Slow control call",
                kind=kind,
                detail=detail,
                outcome=outcome,
                seconds=round(took, 1),
                owner_wait_s=round(owner_wait, 1),
                label=self.label,
            )
        return took

    def note_raw(self, raw: Dict[str, Any]) -> None:
        """Record one raw CLI stdout message by kind (before SDK parsing)."""
        kind = str(raw.get("type"))
        sub = raw.get("subtype")
        if kind == "stream_event":
            kind = "stream:" + str((raw.get("event") or {}).get("type"))
        elif sub:
            kind = f"{kind}:{sub}"
        self.note(f"cli:{kind}")
        content = (raw.get("message") or {}).get("content")
        if not isinstance(content, list):
            return
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "tool_use":
                self.note("cli:tool_use", str(block.get("name")))
            elif btype == "tool_result":
                self.note("cli:tool_result", str(block.get("tool_use_id"))[:12])

    # -- reporting ---------------------------------------------------------
    def snapshot(self) -> Dict[str, Any]:
        now = time.monotonic()
        return {
            "turn_age_s": round(now - self.started, 1),
            "silent_s": round(now - self._last_any, 1),
            "last_event": self._last_any_kind,
            "last_by_kind": {
                k: f"{now - t:.0f}s ago" + (f" [{d}]" if d else "")
                for k, (t, d) in sorted(self.last.items(), key=lambda kv: -kv[1][0])
            },
            "open_calls": [
                f"{k} {d} open {now - t0:.0f}s" for k, d, t0 in self.open.values()
            ],
            "max_gap_s": round(self.max_gap, 1),
            "max_gap_between": " -> ".join(self.max_gap_between),
        }

    def stall_due(self) -> bool:
        now = time.monotonic()
        return (
            now - self._last_any >= STALL_LOG_AFTER_SECONDS
            and now - self.last_stall_log >= STALL_LOG_INTERVAL_SECONDS
        )

    async def log_stall(self, pid: Optional[int]) -> None:
        """One 'turn is quiet' line: ledger + what the CLI process is doing."""
        self.last_stall_log = time.monotonic()
        try:
            cli = await asyncio.wait_for(describe_process(pid), 5.0)
        except Exception as exc:  # probe is best-effort
            cli = {"probe_error": repr(exc)}
        logger.warning("Turn quiet", label=self.label, cli=cli, **self.snapshot())

    def log_summary(self, *, reason: str, min_age: float = 60.0) -> None:
        """End-of-turn ledger; only for long turns or ones with a long gap."""
        snap = self.snapshot()
        if snap["turn_age_s"] < min_age and self.max_gap < STALL_LOG_AFTER_SECONDS:
            return
        logger.info("Turn activity summary", label=self.label, reason=reason, **snap)

    # -- wrappers ----------------------------------------------------------
    def wrap(
        self, kind: str, fn: Callable[..., Awaitable[Any]], detail_arg: int = 0
    ) -> Callable[..., Awaitable[Any]]:
        """Wrap an async hook/callback; ``detail_arg`` names the tool for the log."""

        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            detail = ""
            try:
                first = args[detail_arg]
                detail = str(
                    first.get("tool_name") if isinstance(first, dict) else first
                )
            except Exception:
                pass
            token = self.begin(kind, detail)
            outcome = "ok"
            # Per call, not per turn: parallel tool calls each run in their
            # own task/context, so one call's ask is never charged to another.
            wait = [0.0]
            wait_token = _owner_wait.set(wait)
            try:
                return await fn(*args, **kwargs)
            except BaseException as exc:
                outcome = type(exc).__name__
                raise
            finally:
                _owner_wait.reset(wait_token)
                self.end(token, outcome, owner_wait=wait[0])

        return wrapper


_current: ContextVar[Optional[TurnActivity]] = ContextVar("turn_activity", default=None)


def set_current(activity: Optional[TurnActivity]) -> None:
    _current.set(activity)


def current_activity() -> Optional[TurnActivity]:
    return _current.get()


# Owner-wait accumulator of the wrapped control call running in this context
# (set by TurnActivity.wrap). None outside a wrapped call, e.g. a job's ask.
_owner_wait: ContextVar[Optional[List[float]]] = ContextVar(
    "turn_activity_owner_wait", default=None
)


def note_owner_wait(seconds: float) -> None:
    """Charge ``seconds`` of waiting for the owner to the current control call.

    Called by ConfirmationManager.request; a no-op outside a wrapped call.
    """
    holder = _owner_wait.get()
    if holder is not None:
        holder[0] += seconds


# -- CLI process probe ------------------------------------------------------
_TICKS = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
_LAST_RE = re.compile(r"(lastsnd|lastrcv|lastack):(\d+)")


def _read(path: str) -> str:
    try:
        with open(path) as fh:
            return fh.read().strip()
    except OSError as exc:
        return f"<{exc.__class__.__name__}>"


def _parse_ss(text: str, pid: int) -> List[Dict[str, Any]]:
    """Sockets of ``pid`` from ``ss -tnpi``: peer, state, ms since send/recv.

    A socket line carries ``users:(("claude",pid=N,...))``; its TCP detail
    (``lastsnd:`` ``lastrcv:``) is on the following indented line.
    """
    out: List[Dict[str, Any]] = []
    cur: Optional[Dict[str, Any]] = None
    for line in text.splitlines():
        if line[:1] not in (" ", "\t") and line.strip():
            cur = None
            if f"pid={pid}," in line:
                parts = line.split()
                # ``state established`` drops the State column.
                idx = 3 if parts[0].isdigit() else 4
                cur = {"peer": parts[idx] if len(parts) > idx else "?"}
                out.append(cur)
        elif cur is not None:
            cur.update({k: int(v) for k, v in _LAST_RE.findall(line)})
    return out


async def describe_process(pid: Optional[int]) -> Dict[str, Any]:
    """State of the CLI: scheduler state, CPU, children, TCP sockets and idle ms.

    The sockets matter: ``ss -p`` shows a process's sockets only to its
    ancestors (Yama ptrace_scope=1). From a sibling (a devops shell) a CLI with
    a live API connection looks like it has "0 TCP sockets" — the 22.10
    evidence was read that way. The bot is the CLI's parent, so it can see them.
    """
    if not pid:
        return {"pid": None}
    base = f"/proc/{pid}"
    stat = _read(f"{base}/stat")
    info: Dict[str, Any] = {"pid": pid}
    try:
        fields = stat.rsplit(")", 1)[1].split()
        info["state"] = fields[0]
        info["cpu_s"] = round((int(fields[11]) + int(fields[12])) / _TICKS, 1)
    except Exception:
        info["stat"] = stat[:80]
    info["wchan"] = _read(f"{base}/wchan")
    try:
        tasks = os.listdir(f"{base}/task")
        info["threads"] = len(tasks)
        kids: List[str] = []
        for t in tasks:
            kids += _read(f"{base}/task/{t}/children").split()
        info["children"] = len(set(kids))
    except OSError:
        pass
    try:
        proc = await asyncio.create_subprocess_exec(
            "ss",
            "-tnpi",
            "state",
            "established",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), 3.0)
        info["sockets"] = _parse_ss(stdout.decode(errors="replace"), pid)
    except Exception as exc:
        info["sockets_error"] = repr(exc)
    return info
