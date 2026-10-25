"""pump_status / pump_pending / pump_answer agent tools (dv-a9b0).

A sandboxed board agent (gymbro, pl) cannot run scripts/taskpump in its bash
box, so it gets three owner-only tools next to job_start. As with
job_start(task_id), the board is pinned to the CALLER's profile server-side
(`taskpump board-of <slug>`) and never a tool parameter. Every subprocess is
faked; the fake answers `board-of` by the profile slug in its argv.
"""

from pathlib import Path
from typing import Any

import pytest

from src.claude import agent_tools
from tests.unit.test_claude import test_agent_tools as base

# fixtures
captured_tools, event_bus, scheduler = (
    base.captured_tools,
    base.event_bus,
    base.scheduler,
)
services = base.services
_tools = base.TestJobStartBoardTask._tools

BOARDS = {"gymbro": "gymbro", "pl": "pl", "devops": "devvy"}


def _fake(monkeypatch: pytest.MonkeyPatch) -> list:
    spawned: list = []

    class FakeProc:
        def __init__(self, rc: int, out: bytes) -> None:
            self.returncode = rc
            self._out = out

        async def communicate(self) -> tuple[bytes, bytes]:
            return (self._out, b"")

    async def fake_exec(*argv: str, **kw: Any) -> FakeProc:
        spawned.append(argv)
        if Path(argv[0]).name == "taskpump" and "board-of" in argv:
            board = BOARDS.get(argv[-1])
            if board:
                return FakeProc(0, board.encode() + b"\n")
            return FakeProc(1, b"error: no board for this profile\n")
        return FakeProc(0, b"ok\n")

    monkeypatch.setattr(agent_tools.asyncio, "create_subprocess_exec", fake_exec)
    return spawned


def _calls(spawned: list, verb: str) -> list:
    return [a for a in spawned if Path(a[0]).name == "taskpump" and verb in a]


# --------------------------------------------------------------------------
# approved scenario 2: gymbro agent answers via pump_answer
# --------------------------------------------------------------------------


async def test_gymbro_pump_answer_passes_owner_words_verbatim(
    services, captured_tools, monkeypatch
):
    spawned = _fake(monkeypatch)
    tools = _tools(services, captured_tools, "gymbro")
    assert "pump_answer" in tools
    words = '1 default, 2 да — "указатель" было бы круто; --board pl'
    r = await tools["pump_answer"].handler({"task_id": "dv-0a1b", "text": words})
    assert r.get("isError") is not True, r
    answers = _calls(spawned, "answer")
    assert len(answers) == 1
    argv = answers[0]
    assert "gymbro:dv-0a1b" in argv
    # The owner's text is one argv element, unedited, after the id.
    assert argv[-1] == words
    assert argv.index("gymbro:dv-0a1b") < len(argv) - 1


# --------------------------------------------------------------------------
# approved scenario 3: pl agent sees only pl; a gymbro id is refused
# --------------------------------------------------------------------------


async def test_pl_pump_pending_is_pinned_to_the_pl_board(
    services, captured_tools, monkeypatch
):
    spawned = _fake(monkeypatch)
    tools = _tools(services, captured_tools, "pl")
    assert "pump_pending" in tools
    r = await tools["pump_pending"].handler({})
    assert r.get("isError") is not True, r
    (argv,) = _calls(spawned, "pending")
    assert argv[argv.index("--board") + 1] == "pl"


async def test_pl_pump_answer_on_a_gymbro_id_is_refused(
    services, captured_tools, monkeypatch
):
    spawned = _fake(monkeypatch)
    tools = _tools(services, captured_tools, "pl")
    assert "pump_answer" in tools
    r = await tools["pump_answer"].handler({"task_id": "gymbro:dv-0a1b", "text": "go"})
    assert r.get("isError") is True
    assert "not on this agent's board" in r["content"][0]["text"]
    assert _calls(spawned, "answer") == []


async def test_pl_pump_status_is_pinned_to_the_pl_board(
    services, captured_tools, monkeypatch
):
    spawned = _fake(monkeypatch)
    tools = _tools(services, captured_tools, "pl")
    assert "pump_status" in tools
    r = await tools["pump_status"].handler({})
    assert r.get("isError") is not True, r
    (argv,) = _calls(spawned, "status")
    assert argv[argv.index("--board") + 1] == "pl"
