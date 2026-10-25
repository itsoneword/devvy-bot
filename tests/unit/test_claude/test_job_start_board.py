"""job_start(task_id) edges around the board pipeline (dv-a438 phase 1).

The binding scenarios are TestJobStartBoardTask in test_agent_tools.py; this
reuses its fake and fixtures.
"""

from pathlib import Path

from tests.unit.test_claude import test_agent_tools as base

# fixtures
captured_tools, event_bus, scheduler = (
    base.captured_tools,
    base.event_bus,
    base.scheduler,
)
services = base.services
_fake = base.TestJobStartBoardTask._fake
_tools = base.TestJobStartBoardTask._tools


async def test_task_id_needs_no_prompt(services, captured_tools, monkeypatch):
    spawned = _fake(monkeypatch)
    tools = _tools(services, captured_tools, "devops")
    r = await tools["job_start"].handler({"task_id": "dv-0a1b"})
    assert r.get("isError") is not True, r
    start = next(a for a in spawned if "start" in a)
    assert "devvy:dv-0a1b" in start
    assert start[start.index("--by") + 1] == "devops"


async def test_another_boards_id_is_refused(services, captured_tools, monkeypatch):
    spawned = _fake(monkeypatch)
    tools = _tools(services, captured_tools, "pl")
    r = await tools["job_start"].handler({"task_id": "gymbro:dv-0a1b"})
    assert r.get("isError") is True
    assert "not on this agent's board" in r["content"][0]["text"]
    assert not any("start" in a for a in spawned)


async def test_task_id_with_command_is_refused(services, captured_tools, monkeypatch):
    spawned = _fake(monkeypatch)
    tools = _tools(services, captured_tools, "pl")
    r = await tools["job_start"].handler({"command": "make test", "task_id": "dv-0a1b"})
    assert r.get("isError") is True
    assert not any(Path(a[0]).name in ("bgjob", "taskpump") for a in spawned)
