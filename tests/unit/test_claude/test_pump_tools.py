"""pump_status / pump_pending / pump_answer agent tools (dv-a9b0).

A sandboxed board agent (app, site) cannot run scripts/pump2 in its bash
box, so it gets three owner-only tools next to job_start. As with
job_start(task_id), the board is pinned to the CALLER's profile server-side
(`boards.board_of_profile`) and never a tool parameter. Every subprocess is
faked; boards.json is a tmp file mapping profile -> board.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from src.claude import agent_tools
from src.utils import boards as boards_mod
from tests.unit.test_claude import test_agent_tools as base

# fixtures
captured_tools, event_bus, scheduler = (
    base.captured_tools,
    base.event_bus,
    base.scheduler,
)
services = base.services
_tools = base.TestJobStartBoardTask._tools


@pytest.fixture(autouse=True)
def _boards_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    bf = tmp_path / "boards.json"
    bf.write_text(
        json.dumps(
            {
                "pump2_home": {"profile": "devops"},
                "boards": [
                    {"name": "app", "repo": "/x/gym", "profile": "app"},
                    {"name": "site", "repo": "/x/site", "profile": "site"},
                ],
            }
        )
    )
    monkeypatch.setattr(boards_mod, "BOARDS_FILE_DEFAULT", bf)


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
        return FakeProc(0, b"ok\n")

    monkeypatch.setattr(agent_tools.asyncio, "create_subprocess_exec", fake_exec)
    return spawned


def _calls(spawned: list, verb: str) -> list:
    return [a for a in spawned if Path(a[0]).name == "pump2" and verb in a]


# --------------------------------------------------------------------------
# approved scenario 2: app agent answers via pump_answer
# --------------------------------------------------------------------------


async def test_gymbro_pump_answer_passes_owner_words_verbatim(
    services, captured_tools, monkeypatch
):
    spawned = _fake(monkeypatch)
    tools = _tools(services, captured_tools, "app")
    assert "pump_answer" in tools
    words = '1 default, 2 да — "указатель" было бы круто; --board site'
    r = await tools["pump_answer"].handler({"task_id": "dv-0a1b", "text": words})
    assert r.get("isError") is not True, r
    answers = _calls(spawned, "answer")
    assert len(answers) == 1
    argv = answers[0]
    assert "app:dv-0a1b" in argv
    # The owner's text is one argv element, unedited, after the id.
    assert argv[-1] == words
    assert argv.index("app:dv-0a1b") < len(argv) - 1


# --------------------------------------------------------------------------
# approved scenario 3: site agent sees only site; a app id is refused
# --------------------------------------------------------------------------


async def test_pl_pump_pending_is_pinned_to_the_pl_board(
    services, captured_tools, monkeypatch
):
    spawned = _fake(monkeypatch)
    tools = _tools(services, captured_tools, "site")
    assert "pump_pending" in tools
    r = await tools["pump_pending"].handler({})
    assert r.get("isError") is not True, r
    (argv,) = _calls(spawned, "pending")
    assert argv[-1] == "site"


async def test_pl_pump_answer_on_a_gymbro_id_is_refused(
    services, captured_tools, monkeypatch
):
    spawned = _fake(monkeypatch)
    tools = _tools(services, captured_tools, "site")
    assert "pump_answer" in tools
    r = await tools["pump_answer"].handler({"task_id": "app:dv-0a1b", "text": "go"})
    assert r.get("isError") is True
    assert "not on this agent's board" in r["content"][0]["text"]
    assert _calls(spawned, "answer") == []


async def test_pl_pump_status_is_pinned_to_the_pl_board(
    services, captured_tools, monkeypatch
):
    spawned = _fake(monkeypatch)
    tools = _tools(services, captured_tools, "site")
    assert "pump_status" in tools
    r = await tools["pump_status"].handler({})
    assert r.get("isError") is not True, r
    (argv,) = _calls(spawned, "status")
    assert argv[-1] == "site"
