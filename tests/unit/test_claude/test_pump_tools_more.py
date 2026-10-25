"""More pump-tool edges (dv-a9b0), beside the protected test_pump_tools.py."""

from tests.unit.test_claude import test_pump_tools as base

captured_tools, event_bus, scheduler, services = (
    base.captured_tools,
    base.event_bus,
    base.scheduler,
    base.services,
)


async def test_bare_id_is_pinned_to_the_callers_board(
    services, captured_tools, monkeypatch
):
    spawned = base._fake(monkeypatch)
    tools = base._tools(services, captured_tools, "gymbro")
    r = await tools["pump_answer"].handler({"task_id": "gb-1a2b", "text": "go"})
    assert r.get("isError") is not True, r
    (argv,) = base._calls(spawned, "answer")
    assert list(argv[-3:]) == ["gymbro:gb-1a2b", "--", "go"]


async def test_profile_without_a_board_gets_the_no_board_error(
    services, captured_tools, monkeypatch
):
    spawned = base._fake(monkeypatch)
    tools = base._tools(services, captured_tools, "chef")
    r = await tools["pump_pending"].handler({})
    assert r.get("isError") is True
    assert "no board" in r["content"][0]["text"]
    assert base._calls(spawned, "pending") == []


async def test_empty_answer_is_refused(services, captured_tools, monkeypatch):
    spawned = base._fake(monkeypatch)
    tools = base._tools(services, captured_tools, "pl")
    r = await tools["pump_answer"].handler({"task_id": "dv-0a1b", "text": "  "})
    assert r.get("isError") is True
    assert base._calls(spawned, "answer") == []
