"""Pump tools run scripts/pump2 on the caller's board (dv-d474, dv-81a7)."""

import json
from pathlib import Path

import pytest

from src.claude import agent_tools
from src.pump2.state import add_comment, set_pump2, set_status
from src.utils import boards as boards_mod
from tests.unit.test_claude import test_agent_tools as base

captured_tools, event_bus, scheduler = (
    base.captured_tools,
    base.event_bus,
    base.scheduler,
)
services = base.services
_tools = base.TestJobStartBoardTask._tools


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list:
    gym = tmp_path / "gym"
    (gym / ".deviz/tasks").mkdir(parents=True)
    bf = tmp_path / "boards.json"
    bf.write_text(
        json.dumps(
            {
                "pump2_home": {"profile": "devops"},
                "boards": [
                    {
                        "name": "app",
                        "repo": str(gym),
                        "profile": "app",
                        "pump2": True,
                    },
                    {"name": "site", "repo": str(tmp_path / "site"), "profile": "site"},
                ],
            }
        )
    )
    monkeypatch.setattr(boards_mod, "BOARDS_FILE_DEFAULT", bf)
    p = gym / ".deviz/tasks/dv-0a1b.md"
    p.write_text("---\nid: dv-0a1b\ntitle: t\nstatus: in_progress\n---\n\nbody\n")
    set_pump2(p, stage="analyze", round=1)
    set_status(p, "waiting_user")
    add_comment(p, "analyze: questions for the owner\n1. ok?")
    done: list = []

    async def fake(name: str, *argv: str) -> "tuple[int, str]":
        done.append((name, argv))
        return 0, "ok"

    monkeypatch.setattr(agent_tools, "run_repo_script", fake)
    # Routing only: the designloop card lookup (dv-06e6) sees no board.
    monkeypatch.setattr(agent_tools, "load_boards", lambda *a, **k: {})
    return done


def _of(calls: list, name: str, verb: str) -> list:
    return [a for n, a in calls if n == name and verb in a]


async def test_job_start_routes(services, captured_tools, calls) -> None:
    for profile, full in (
        ("devops", "devvy:dv-0a1b"),
        ("app", "app:dv-0a1b"),
        ("site", "site:dv-0a1b"),
    ):
        calls.clear()
        tools = _tools(services, captured_tools, profile)
        r = await tools["job_start"].handler({"prompt": "x", "task_id": "dv-0a1b"})
        assert r.get("isError") is not True, r
        (argv,) = _of(calls, "pump2", "start")
        assert full in argv
        assert argv[argv.index("--by") + 1] == profile
        assert [n for n, _ in calls] == ["pump2"]


@pytest.mark.parametrize("verb", ["status", "pending"])
async def test_status_pending_run_pump2_on_the_callers_board(
    services, captured_tools, calls, verb
) -> None:
    for profile, board in (("devops", "devvy"), ("app", "app"), ("site", "site")):
        calls.clear()
        tools = _tools(services, captured_tools, profile)
        r = await tools[f"pump_{verb}"].handler({})
        assert r.get("isError") is not True, r
        assert [(n, list(a)) for n, a in calls] == [("pump2", [verb, board])]


async def test_answer_routes(services, captured_tools, calls) -> None:
    words = '1 да -- "x"; --board site'
    for profile, full in (("app", "app:dv-0a1b"), ("site", "site:dv-0a1b")):
        calls.clear()
        tools = _tools(services, captured_tools, profile)
        r = await tools["pump_answer"].handler({"task_id": "dv-0a1b", "text": words})
        assert r.get("isError") is not True, r
        (argv,) = _of(calls, "pump2", "answer")
        assert full in argv and argv[-1] == words
        assert [n for n, _ in calls] == ["pump2"]


async def test_profile_without_board_is_refused(
    services, captured_tools, calls
) -> None:
    tools = _tools(services, captured_tools, "chef")
    for name in ("pump_status", "pump_pending"):
        r = await tools[name].handler({})
        assert r.get("isError") is True
        assert "no board" in r["content"][0]["text"]
    assert calls == []


async def test_control(services, captured_tools, calls) -> None:
    tools = _tools(services, captured_tools, "app")
    r = await tools["pump_control"].handler(
        {"action": "urgent", "task_id": "app:dv-0a1b"}
    )
    assert r.get("isError") is not True  # dv-44b1: pump2 has urgent now
    assert _of(calls, "pump2", "urgent")
    r = await tools["pump_control"].handler({"action": "freeze"})
    assert r.get("isError") is not True
    assert _of(calls, "pump2", "freeze")
