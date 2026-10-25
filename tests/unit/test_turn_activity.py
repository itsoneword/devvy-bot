"""dv-586f: the per-turn ledger that names what a stalled CLI waits on."""

import asyncio
import os

import pytest

from src.claude import turn_activity as ta

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
