"""dv-586f: the per-turn ledger that names what a stalled CLI waits on."""

import asyncio
import os
from unittest.mock import AsyncMock

import pytest
from structlog.testing import capture_logs

from src.claude import turn_activity as ta
from src.claude.confirmation import ConfirmationManager

SS = """Recv-Q Send-Q Local Address:Port Peer Address:Port Process
0      0      10.0.0.1:46258 160.79.104.10:443 users:(("claude",pid=42,fd=12))
\t ts sack cubic lastsnd:157684 lastrcv:150792 lastack:26768
0      0      10.0.0.1:1 1.2.3.4:443 users:(("other",pid=7,fd=3))
\t ts lastsnd:1 lastrcv:2 lastack:3
"""


def test_parse_ss_picks_only_the_pid_and_reads_idle_times() -> None:
    socks = ta._parse_ss(SS, 42)
    assert socks == [
        {
            "peer": "160.79.104.10:443",
            "lastsnd": 157684,
            "lastrcv": 150792,
            "lastack": 26768,
        }
    ]
    assert ta._parse_ss(SS, 99) == []


def test_note_raw_records_kinds_tools_and_max_gap() -> None:
    a = ta.TurnActivity("t")
    a.note_raw({"type": "stream_event", "event": {"type": "message_start"}})
    a.note_raw(
        {
            "type": "assistant",
            "message": {"content": [{"type": "tool_use", "name": "Bash"}]},
        }
    )
    a.note_raw({"type": "system", "subtype": "init"})
    snap = a.snapshot()
    assert "cli:stream:message_start" in snap["last_by_kind"]
    assert "[Bash]" in snap["last_by_kind"]["cli:tool_use"]
    assert "cli:system:init" in snap["last_by_kind"]
    assert snap["last_event"] == "cli:system:init"


async def test_wrap_times_the_call_and_tracks_it_while_open() -> None:
    a = ta.TurnActivity("t")
    seen = {}

    async def hook(inp: dict, tool_use_id: str, ctx: object) -> dict:
        seen["open"] = list(a.snapshot()["open_calls"])
        return {}

    assert await a.wrap("hook:PreToolUse", hook)({"tool_name": "Bash"}, "x", None) == {}
    assert seen["open"] and "Bash" in seen["open"][0]
    assert a.open == {}
    assert "hook:PreToolUse:out" in a.last


async def test_wrap_records_exception_and_releases() -> None:
    a = ta.TurnActivity("t")

    async def boom(name: str) -> None:
        raise ValueError("x")

    with pytest.raises(ValueError):
        await a.wrap("mcp", boom)("remember")
    assert a.open == {}
    assert "ValueError" in a.last["mcp:out"][1]


def test_stall_due_waits_for_silence(monkeypatch: pytest.MonkeyPatch) -> None:
    a = ta.TurnActivity("t")
    assert not a.stall_due()
    a._last_any -= ta.STALL_LOG_AFTER_SECONDS + 1
    assert a.stall_due()
    a.last_stall_log = a._last_any + ta.STALL_LOG_AFTER_SECONDS + 1
    a.last_stall_log = __import__("time").monotonic()
    assert not a.stall_due()


async def test_describe_process_reads_own_proc() -> None:
    info = await ta.describe_process(os.getpid())
    assert info["pid"] == os.getpid()
    assert info["state"] in "RSDZ"
    assert "threads" in info
    assert (await ta.describe_process(None)) == {"pid": None}


async def test_log_stall_never_raises_on_dead_pid() -> None:
    a = ta.TurnActivity("t")
    await asyncio.wait_for(a.log_stall(2**22 + 1), 10)


# -- dv-d760: owner confirmation wait is not hook self-time --------------------
#
# The real thresholds are 5 s; these tests lower SLOW_CALL_LOG_SECONDS so a
# "6 s" wait is 0.6 s of real time against a 0.3 s threshold (same ratio).

_THRESHOLD = 0.3
_OWNER_WAIT = 0.6


def _slow_lines(logs: list, detail: str = "") -> list:
    return [
        e
        for e in logs
        if e.get("event") == "Slow control call"
        and (not detail or e.get("detail") == detail)
    ]


def _manager_that_owner_taps_after(delay: float, approved: bool = True) -> tuple:
    """A real ConfirmationManager whose owner taps the card ``delay`` s later."""
    send = AsyncMock(return_value=11)
    manager = ConfirmationManager(send, chat_ids=[7], timeout_seconds=60)
    tappers: list = []

    async def _send(chat_id: int, text: str, token: str) -> int:
        async def tap() -> None:
            await asyncio.sleep(delay)
            manager.resolve(token, approved)

        tappers.append(asyncio.ensure_future(tap()))
        return 11

    send.side_effect = _send
    return manager, tappers


async def test_owner_tap_wait_is_not_a_slow_control_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scenario: the hook waits '6 s' on the owner's tap -> no Slow control
    call; the wait is recorded as owner wait (ledger + Confirmation line)."""
    monkeypatch.setattr(ta, "SLOW_CALL_LOG_SECONDS", _THRESHOLD)
    a = ta.TurnActivity("devops:test")
    manager, _ = _manager_that_owner_taps_after(_OWNER_WAIT)

    async def hook(inp: dict, tool_use_id: str, ctx: object) -> dict:
        approved = await manager.request(
            "recursive/forced file deletion (rm)", "rm -f x.out", source="chat"
        )
        return {} if approved else {"deny": True}

    with capture_logs() as logs:
        out = await a.wrap("hook:PreToolUse", hook)({"tool_name": "Bash"}, "t1", None)

    assert out == {}
    assert _slow_lines(logs) == [], _slow_lines(logs)
    resolved = [e for e in logs if e.get("event") == "Confirmation resolved"]
    assert len(resolved) == 1
    assert resolved[0]["waited_s"] >= _OWNER_WAIT - 0.1
    assert resolved[0]["source"] == "chat"
    assert "rm -f x.out" in resolved[0]["preview"]
    # The ledger's out-detail names the owner wait, so /stall logs show it.
    assert "owner" in a.last["hook:PreToolUse:out"][1]


async def test_slow_hook_without_ask_still_logs_slow_control_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scenario: the hook itself is slow ('6 s', no ask) -> still logged."""
    monkeypatch.setattr(ta, "SLOW_CALL_LOG_SECONDS", _THRESHOLD)
    a = ta.TurnActivity("devops:test")

    async def hook(inp: dict, tool_use_id: str, ctx: object) -> dict:
        await asyncio.sleep(_OWNER_WAIT)
        return {}

    with capture_logs() as logs:
        await a.wrap("hook:PreToolUse", hook)({"tool_name": "Bash"}, "t1", None)

    slow = _slow_lines(logs, "Bash")
    assert len(slow) == 1
    assert slow[0]["kind"] == "hook:PreToolUse"
    assert slow[0]["seconds"] >= _OWNER_WAIT - 0.1


async def test_parallel_calls_owner_wait_is_per_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scenario: two parallel tool calls in one turn, only one asks -> the
    other shows zero owner wait (and is still reported as slow self-time)."""
    monkeypatch.setattr(ta, "SLOW_CALL_LOG_SECONDS", _THRESHOLD)
    a = ta.TurnActivity("devops:test")
    manager, _ = _manager_that_owner_taps_after(_OWNER_WAIT)

    async def asking(inp: dict, tool_use_id: str, ctx: object) -> dict:
        await manager.request("rm", "rm -f x.out", source="chat")
        return {}

    async def busy(inp: dict, tool_use_id: str, ctx: object) -> dict:
        await asyncio.sleep(_OWNER_WAIT)
        return {}

    with capture_logs() as logs:
        await asyncio.gather(
            a.wrap("hook:PreToolUse", asking)({"tool_name": "Bash"}, "t1", None),
            a.wrap("hook:PreToolUse", busy)({"tool_name": "Read"}, "t2", None),
        )

    assert _slow_lines(logs, "Bash") == []
    busy_lines = _slow_lines(logs, "Read")
    assert len(busy_lines) == 1
    assert busy_lines[0]["owner_wait_s"] == 0
    assert a.open == {}


async def test_job_path_confirmation_outside_any_wrapped_call() -> None:
    """added: a job ask (/internal/policy/ask) has no TurnActivity -- the
    request must not crash, and the timeout line names the source and wait."""
    send = AsyncMock(return_value=11)
    manager = ConfirmationManager(send, chat_ids=[7], timeout_seconds=0.05)

    with capture_logs() as logs:
        approved = await manager.request(
            "git push", "git push origin main", source="pump-dv-1234-build"
        )

    assert approved is False
    timed_out = [
        e for e in logs if str(e.get("event", "")).startswith("Confirmation timed out")
    ]
    assert len(timed_out) == 1
    assert timed_out[0]["source"] == "pump-dv-1234-build"
    assert timed_out[0]["waited_s"] >= 0.04
    assert "git push origin main" in timed_out[0]["preview"]
