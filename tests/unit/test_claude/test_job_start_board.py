"""job_start(task_id) edges around the board pipeline (dv-a438 phase 1).

The binding scenarios are TestJobStartBoardTask in test_agent_tools.py; this
reuses its fake and fixtures.
"""

from pathlib import Path

from src.claude import agent_tools
from src.claude.agent_tools import build_agent_mcp_server
from src.claude.turn_setup import resolve_turn_context
from tests.unit.test_claude import test_agent_tools as base
from tests.unit.test_claude.test_turn_setup_parity import _registry

# fixtures
captured_tools, event_bus, scheduler = (
    base.captured_tools,
    base.event_bus,
    base.scheduler,
)
services = base.services
_boards_file = base._boards_file
_fake = base.TestJobStartBoardTask._fake
_tools = base.TestJobStartBoardTask._tools


async def test_task_id_needs_no_prompt(services, captured_tools, monkeypatch):
    spawned = _fake(monkeypatch)
    # dv-0a1b is no real card: skip the designloop lookup (dv-06e6).
    monkeypatch.setattr(agent_tools, "load_boards", lambda *a, **k: {})
    tools = _tools(services, captured_tools, "devops")
    r = await tools["job_start"].handler({"task_id": "dv-0a1b"})
    assert r.get("isError") is not True, r
    start = next(a for a in spawned if "start" in a)
    assert "devvy:dv-0a1b" in start
    assert start[start.index("--by") + 1] == "devops"


async def test_another_boards_id_is_refused(services, captured_tools, monkeypatch):
    spawned = _fake(monkeypatch)
    tools = _tools(services, captured_tools, "coach")
    r = await tools["job_start"].handler({"task_id": "app:dv-0a1b"})
    assert r.get("isError") is True
    assert "not on this agent's board" in r["content"][0]["text"]
    assert not any("start" in a for a in spawned)


async def test_task_id_with_command_is_refused(services, captured_tools, monkeypatch):
    spawned = _fake(monkeypatch)
    tools = _tools(services, captured_tools, "coach")
    r = await tools["job_start"].handler({"command": "make test", "task_id": "dv-0a1b"})
    assert r.get("isError") is True
    assert not any(Path(a[0]).name in ("bgjob", "pump2") for a in spawned)


async def test_coach_owner_user_workspace_starts_task_on_coach_board(
    services, captured_tools, monkeypatch, tmp_path
):
    registry = _registry(tmp_path)
    cwd = tmp_path / "coach" / "users" / "111"
    cwd.mkdir(parents=True)
    ctx = resolve_turn_context(registry, cwd, user_id=111, role="owner")
    spawned = []

    class Proc:
        returncode = 0

        def __init__(self, out):
            self.out = out

        async def communicate(self):
            return self.out, b""

    async def fake_exec(*argv, **kw):
        spawned.append(argv)
        return Proc(b"started\n")

    monkeypatch.setattr(base.agent_tools.asyncio, "create_subprocess_exec", fake_exec)
    build_agent_mcp_server(services, ctx.profile_slug, cwd, user_id=111, role="owner")
    result = await captured_tools["job_start"].handler({"task_id": "dv-0a1b"})
    assert result.get("isError") is not True, result
    assert any("coach:dv-0a1b" in a for a in spawned)


async def test_neuro_owner_prompt_job_uses_bare_profile_and_user_cwd(
    services, captured_tools, monkeypatch, tmp_path
):
    from src.profiles.registry import ProfileDefinition, ProfileRegistry

    root = tmp_path / "web"
    cwd = root / "users" / "1001"
    cwd.mkdir(parents=True)
    registry = ProfileRegistry(
        [
            ProfileDefinition(
                slug="web",
                name="Web",
                working_dir=root,
                memory_dir=root / "memory",
                persona=root / "agent.md",
                personal_memory=True,
                default=True,
            )
        ]
    )
    ctx = resolve_turn_context(registry, cwd, user_id=1001, role="owner")
    spawned = []

    class Proc:
        returncode = 0

        async def communicate(self):
            return b"started job 123", b""

    async def fake_exec(*argv, **kw):
        spawned.append(argv)
        return Proc()

    monkeypatch.setattr(base.agent_tools.asyncio, "create_subprocess_exec", fake_exec)
    build_agent_mcp_server(
        services,
        ctx.profile_slug,
        cwd,
        user_id=1001,
        requester_chat_id=555,
        role="owner",
    )
    result = await captured_tools["job_start"].handler(
        {"prompt": "simulate balance", "name": "balance"}
    )
    assert result.get("isError") is not True, result
    (argv,) = spawned
    assert argv[argv.index("--profile") + 1] == "web"
    assert argv[argv.index("--cwd") + 1] == str(cwd)


def test_devops_guest_cannot_mount_owner_job_or_task_status_tools(
    services, captured_tools, tmp_path
):
    registry = _registry(tmp_path)
    cwd = tmp_path / "devops" / "guests" / "111"
    cwd.mkdir(parents=True)
    ctx = resolve_turn_context(registry, cwd, user_id=111, role="guest")
    build_agent_mcp_server(services, ctx.profile_slug, cwd, user_id=111, role="guest")
    assert ctx.profile_slug == "devops#u111"
    assert {"job_start", "pump_answer", "pump_status", "pump_pending"}.isdisjoint(
        captured_tools
    )
