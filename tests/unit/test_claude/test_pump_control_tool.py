"""pump_control agent tool (dv-b17e): freeze / thaw / urgent from chat.

One owner-only tool beside pump_status. It is global (every board), so it
does not go through `taskpump board-of`; urgent takes any board's id. Codex
gets the same spec over the agent bridge (JOB_TOOLS in the bridge tests).
"""

from pathlib import Path

import pytest

from src.claude import agent_tools
from tests.unit.test_claude import test_agent_tools as base
from tests.unit.test_claude import test_pump_tools as pt

captured_tools, event_bus, scheduler = (
    base.captured_tools,
    base.event_bus,
    base.scheduler,
)
services = base.services
_tools = pt._tools


def _taskpump(spawned: list) -> list:
    return [a for a in spawned if Path(a[0]).name == "taskpump"]


@pytest.mark.parametrize("action", ["freeze", "thaw"])
async def test_freeze_and_thaw_run_taskpump_globally(
    services, captured_tools, monkeypatch, action
):
    spawned = pt._fake(monkeypatch)
    tools = _tools(services, captured_tools, "devops")
    r = await tools["pump_control"].handler({"action": action})
    assert r.get("isError") is not True, r
    (argv,) = _taskpump(spawned)  # no board-of lookup: every board
    assert list(argv[1:]) == [action, "--by", "devops"]


async def test_urgent_passes_any_boards_id_after_double_dash(
    services, captured_tools, monkeypatch
):
    spawned = pt._fake(monkeypatch)
    tools = _tools(services, captured_tools, "gymbro")
    r = await tools["pump_control"].handler(
        {"action": "urgent", "task_id": "pl:dv-0a1b"}
    )
    assert r.get("isError") is not True, r
    (argv,) = _taskpump(spawned)
    assert list(argv[1:]) == ["urgent", "--by", "gymbro", "--", "pl:dv-0a1b"]


@pytest.mark.parametrize(
    "args",
    [
        {"action": "stop"},
        {"action": ""},
        {"action": "urgent"},
        {"action": "urgent", "task_id": "--by x"},
    ],
)
async def test_bad_action_or_id_never_spawns(
    services, captured_tools, monkeypatch, args
):
    spawned = pt._fake(monkeypatch)
    tools = _tools(services, captured_tools, "devops")
    r = await tools["pump_control"].handler(args)
    assert r.get("isError") is True
    assert spawned == []


async def test_non_owner_is_refused_even_if_mounted(
    services, captured_tools, monkeypatch
):
    spawned = pt._fake(monkeypatch)
    monkeypatch.setattr(agent_tools, "_job_tools_allowed", lambda role: True)
    agent_tools.build_agent_mcp_server(
        services, "devops", Path("/tmp/ws"), requester_chat_id=555, role="guest"
    )
    r = await captured_tools["pump_control"].handler({"action": "freeze"})
    assert r.get("isError") is True
    assert spawned == []


async def test_guest_does_not_get_the_tool(services, captured_tools):
    agent_tools.build_agent_mcp_server(
        services, "devops", Path("/tmp/ws"), requester_chat_id=555, role="guest"
    )
    assert "pump_control" not in captured_tools
