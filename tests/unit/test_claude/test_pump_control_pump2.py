"""pump_control routes to scripts/pump2 (dv-44b1); the old pump is off."""

import pytest

from tests.unit.test_claude import test_agent_tools as base
from tests.unit.test_claude import test_pump_tools as pt
from tests.unit.test_claude.test_pump_control_tool import _taskpump

captured_tools, event_bus, scheduler = (
    base.captured_tools,
    base.event_bus,
    base.scheduler,
)
services = base.services
_tools = pt._tools


def _pump2(spawned: list) -> list:
    from pathlib import Path

    return [a for a in spawned if Path(a[0]).name == "pump2"]


@pytest.mark.parametrize("action", ["freeze", "thaw"])
async def test_freeze_thaw_run_pump2(services, captured_tools, monkeypatch, action):
    spawned = pt._fake(monkeypatch)
    tools = _tools(services, captured_tools, "devops")
    r = await tools["pump_control"].handler({"action": action})
    assert r.get("isError") is not True, r
    (argv,) = _pump2(spawned)
    assert list(argv[1:]) == [action, "--by", "devops"]
    assert _taskpump(spawned) == []


async def test_urgent_runs_pump2_with_board_id(services, captured_tools, monkeypatch):
    spawned = pt._fake(monkeypatch)
    tools = _tools(services, captured_tools, "app")
    r = await tools["pump_control"].handler(
        {"action": "urgent", "task_id": "app:dv-0a1b"}
    )
    assert r.get("isError") is not True, r
    (argv,) = _pump2(spawned)
    assert list(argv[1:]) == ["urgent", "--by", "app", "--", "app:dv-0a1b"]
    assert _taskpump(spawned) == []


async def test_description_matches_new_behaviour(services, captured_tools):
    tools = _tools(services, captured_tools, "devops")
    desc = tools["pump_control"].description.lower()
    assert "taskpump" not in desc
