"""A detached job reports back to the owner whose workspace launched it."""

import importlib.util
import json
import subprocess
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from fastapi.testclient import TestClient

from src.api.server import create_api_app
from src.events.bus import EventBus
from src.events.handlers import AgentHandler
from src.events.types import AgentResponseEvent
from src.profiles.registry import ProfileDefinition, ProfileRegistry
from src.utils.job_scope import cwd_in_profile
from tests.unit.test_bgjob_codex_box import CLAUDE_ROUTE, _launch

REPO = Path(__file__).parents[2]


def test_extra_dir_scope_is_only_profile_root_or_owner_workspace(tmp_path):
    root = tmp_path / "neuro"
    assert cwd_in_profile(root, root)
    assert cwd_in_profile(root / "users" / "1001", root)
    assert not cwd_in_profile(root / "guests" / "1001", root)
    assert not cwd_in_profile(tmp_path / "neuro-other" / "users" / "1001", root)


def _bgjob(tmp_path: Path):
    loader = SourceFileLoader("bgjob_owner_under_test", str(REPO / "scripts/bgjob"))
    spec = importlib.util.spec_from_loader("bgjob_owner_under_test", loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    module.JOBS_ROOT = tmp_path
    return module


def test_nondefault_owner_digest_carries_user_chat_and_workspace(tmp_path, monkeypatch):
    bgjob = _bgjob(tmp_path)
    root = tmp_path / "neuro"
    cwd = root / "users" / "1001"
    cwd.mkdir(parents=True)
    job = tmp_path / "job-1"
    job.mkdir()
    (job / "meta.json").write_text(
        json.dumps(
            {
                "id": "job-1",
                "name": "research",
                "profile": "neuro",
                "cwd": str(cwd),
                "profile_root": str(root),
                "user_id": 1001,
                "chat_id": -100123,
                "batch": "batch-1",
            }
        )
    )
    (job / "exit_code").write_text("0")
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(bgjob.subprocess, "run", fake_run)
    assert bgjob.digest_batch("batch-1", now=1) == "sent"
    (argv,) = calls
    assert argv[argv.index("--user-id") + 1] == "1001"
    assert argv[argv.index("--chat-id") + 1] == "-100123"
    assert argv[argv.index("--cwd") + 1] == str(cwd)


async def test_internal_prompt_preserves_nondefault_owner_workspace(tmp_path):
    root = tmp_path / "neuro"
    cwd = root / "users" / "1001"
    other = root / "users" / "111"
    cwd.mkdir(parents=True)
    other.mkdir(parents=True)
    registry = ProfileRegistry(
        [
            ProfileDefinition(
                slug="neuro",
                name="Neuro",
                working_dir=root,
                memory_dir=root / "memory",
                persona=root / "agent.md",
                personal_memory=True,
                default=True,
            )
        ]
    )
    bus = EventBus()
    bus.publish = AsyncMock()
    settings = MagicMock()
    settings.webhook_api_secret = "secret"
    app = create_api_app(bus, settings, profile_registry=registry)
    client = TestClient(app)
    body = {
        "prompt": "report",
        "profile": "neuro",
        "cwd": str(cwd),
        "user_id": 1001,
        "chat_id": -100123,
    }
    response = client.post(
        "/internal/prompt", json=body, headers={"Authorization": "Bearer secret"}
    )
    assert response.status_code == 200
    event = bus.publish.call_args.args[0]
    assert event.working_directory == cwd
    assert event.created_by == 1001
    assert event.target_chat_ids == [-100123]

    handler = AgentHandler(
        bus, MagicMock(), root, default_user_id=111, profile_registry=registry
    )
    assert handler._resolve_job_owner(event) == 1001
    assert handler._per_user_workspace_for(cwd, 1001, "owner") == cwd

    response = MagicMock(content="Research complete", tools_used=[])
    handler.claude.run_command = AsyncMock(return_value=response)
    await handler.handle_scheduled(event)
    reply = bus.publish.call_args.args[0]
    assert isinstance(reply, AgentResponseEvent)
    assert reply.chat_id == -100123
    assert handler.claude.run_command.call_args.kwargs["user_id"] == 1001
    assert handler.claude.run_command.call_args.kwargs["working_directory"] == cwd

    body["cwd"] = str(other)
    assert (
        client.post(
            "/internal/prompt", json=body, headers={"Authorization": "Bearer secret"}
        ).status_code
        == 400
    )


def test_completion_ping_targets_nondefault_owner(tmp_path, monkeypatch):
    bgjob = _bgjob(tmp_path)
    real_main = bgjob.main

    def launch_with_notifications():
        sys.argv.remove("--quiet")
        return real_main()

    bgjob.main = launch_with_notifications
    line, job_dir, calls = _launch(
        bgjob,
        tmp_path,
        monkeypatch,
        CLAUDE_ROUTE,
        "--user-id",
        "1001",
        "--chat-id",
        "-100123",
    )
    assert "--prompt-file" in line
    runner = (job_dir / "run.sh").read_text()
    assert "notify --source bgjob" in runner
    assert "--chat-id -100123" in runner
    start = next(c for c in calls if isinstance(c, list) and "notify" in c)
    assert start[start.index("--chat-id") + 1] == "-100123"
    meta = json.loads((job_dir / "meta.json").read_text())
    assert meta["user_id"] == 1001
    assert meta["chat_id"] == -100123


def test_command_job_notices_target_initiating_chat(tmp_path, monkeypatch):
    bgjob = _bgjob(tmp_path)
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    monkeypatch.setattr(bgjob, "JOBS_ROOT", jobs)
    monkeypatch.setattr(bgjob, "load_profiles", lambda: {"neuro": str(tmp_path)})
    monkeypatch.setattr(bgjob, "live_jobs", lambda: [])
    monkeypatch.setattr(bgjob, "render_jobs_status", lambda: "status")
    monkeypatch.setattr(bgjob.subprocess, "Popen", lambda *a, **k: None)
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(bgjob.subprocess, "run", fake_run)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "bgjob",
            "run",
            "--name",
            "research",
            "--profile",
            "neuro",
            "--cmd",
            "echo done",
            "--user-id",
            "1001",
            "--chat-id",
            "-100123",
        ],
    )
    assert bgjob.main() == 0
    runner = next(jobs.glob("*/run.sh")).read_text()
    assert "notify --source bgjob" in runner
    assert "--chat-id -100123" in runner
    start = next(c for c in calls if isinstance(c, list) and "notify" in c)
    assert start[start.index("--chat-id") + 1] == "-100123"


REPORTS = -1004290210285
OWNER = 1001


def _neuro_registry(root: Path) -> ProfileRegistry:
    return ProfileRegistry(
        [
            ProfileDefinition(
                slug="neuro",
                name="Neuro",
                working_dir=root,
                memory_dir=root / "memory",
                persona=root / "agent.md",
                personal_memory=True,
                default=True,
            )
        ]
    )


async def _job_start_argv(monkeypatch, tmp_path, requester_chat_id):
    from src.claude import agent_tools
    from src.claude.agent_tools import AgentServices, build_agent_mcp_server

    root = tmp_path / "neuro"
    cwd = root / "users" / str(OWNER)
    cwd.mkdir(parents=True, exist_ok=True)
    captured = {}

    def fake_create(name, version, tools):
        captured.update({t.name: t for t in tools})
        return {"name": name}

    class Proc:
        returncode = 0

        async def communicate(self):
            return b"started job 1", b""

    spawned = []

    async def fake_exec(*argv, **kw):
        spawned.append(argv)
        return Proc()

    monkeypatch.setattr(agent_tools, "create_sdk_mcp_server", fake_create)
    monkeypatch.setattr(agent_tools.asyncio, "create_subprocess_exec", fake_exec)
    services = AgentServices(
        event_bus=MagicMock(),
        task_repo=MagicMock(),
        profile_registry=_neuro_registry(root),
        reports_chat_id=REPORTS,
    )
    build_agent_mcp_server(
        services,
        "neuro",
        cwd,
        user_id=OWNER,
        requester_chat_id=requester_chat_id,
        role="owner",
    )
    result = await captured["job_start"].handler({"prompt": "simulate", "name": "x"})
    assert result.get("isError") is not True, result
    (argv,) = spawned
    return list(argv)


async def test_job_start_in_reports_topic_targets_owner_chat(monkeypatch, tmp_path):
    argv = await _job_start_argv(monkeypatch, tmp_path, REPORTS)
    assert "--chat-id" not in argv
    assert argv[argv.index("--user-id") + 1] == str(OWNER)
    other = await _job_start_argv(monkeypatch, tmp_path, -100123)
    assert other[other.index("--chat-id") + 1] == "-100123"


def _write_job(tmp_path: Path, cwd: Path, root: Path, chat_id=None) -> None:
    job = tmp_path / "job-1"
    job.mkdir()
    (job / "meta.json").write_text(
        json.dumps(
            {
                "id": "job-1",
                "name": "research",
                "profile": "neuro",
                "cwd": str(cwd),
                "profile_root": str(root),
                "user_id": OWNER,
                "chat_id": chat_id,
                "batch": "batch-1",
            }
        )
    )
    (job / "exit_code").write_text("0")


def _digest_argv(bgjob, monkeypatch) -> list:
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(bgjob.subprocess, "run", fake_run)
    assert bgjob.digest_batch("batch-1", now=1) == "sent"
    (argv,) = calls
    return argv


def _api(root: Path, bus: EventBus) -> TestClient:
    settings = MagicMock()
    settings.webhook_api_secret = "secret"
    return TestClient(
        create_api_app(bus, settings, profile_registry=_neuro_registry(root))
    )


def _prompt_body(argv: list) -> dict:
    """The /internal/prompt body agentctl builds from the digest argv."""
    body = {
        "prompt": argv[2],
        "profile": argv[argv.index("--profile") + 1],
        "name": argv[argv.index("--name") + 1],
    }
    for flag, key in (("--user-id", "user_id"), ("--chat-id", "chat_id")):
        if flag in argv:
            body[key] = int(argv[argv.index(flag) + 1])
    if "--cwd" in argv:
        body["cwd"] = argv[argv.index("--cwd") + 1]
    return body


async def test_reports_topic_launch_digest_lands_in_profile_topic(
    tmp_path, monkeypatch
):
    from src.notifications.routing import ReportsRouter
    from src.notifications.service import NotificationService
    from src.utils.constants import DEFAULT_REPORTS_ROUTES
    from tests.unit.test_notifications.test_reports_routing import FakeRepo

    bgjob = _bgjob(tmp_path)
    root = tmp_path / "neuro"
    cwd = root / "users" / str(OWNER)
    cwd.mkdir(parents=True)
    _write_job(tmp_path, cwd, root, chat_id=None)  # job_start dropped REPORTS
    argv = _digest_argv(bgjob, monkeypatch)
    assert argv[argv.index("--chat-id") + 1] == str(OWNER)

    bus = EventBus()
    bus.publish = AsyncMock()
    response = _api(root, bus).post(
        "/internal/prompt",
        json=_prompt_body(argv),
        headers={"Authorization": "Bearer secret"},
    )
    assert response.status_code == 200
    event = bus.publish.call_args.args[0]
    handler = AgentHandler(
        bus,
        MagicMock(),
        root,
        default_user_id=111,
        profile_registry=_neuro_registry(root),
    )
    handler.claude.run_command = AsyncMock(
        return_value=MagicMock(content="Research complete", tools_used=[])
    )
    await handler.handle_scheduled(event)
    reply = bus.publish.call_args.args[0]
    assert isinstance(reply, AgentResponseEvent)
    assert reply.deliver_profile == "neuro"

    tg = AsyncMock()
    tg.create_forum_topic = AsyncMock(
        side_effect=lambda **kw: MagicMock(
            message_thread_id={"Neuro Game": 77, "Jobs": 88}[kw["name"]]
        )
    )
    router = ReportsRouter(
        REPORTS,
        dict(DEFAULT_REPORTS_ROUTES),
        FakeRepo(),
        [OWNER],
        bindings={"Neuro Game": "neuro", "Jobs": "devops"},
        reply_only=["Jobs"],
    )
    service = NotificationService(EventBus(), tg, [OWNER], reports_router=router)
    sent = AsyncMock()
    monkeypatch.setattr(service, "_rate_limited_send", sent)
    for chat_id in service._resolve_chat_ids(reply):
        await service._deliver(chat_id, reply)
    assert sent.call_args.args[0] == REPORTS
    assert sent.call_args.kwargs["thread_id"] == 77

    # The 🚀/✅ pings of the same launch: notify --chat-id OWNER, source bgjob.
    ping = AgentResponseEvent(chat_id=OWNER, text="✅ research", source="bgjob")
    await service._deliver(OWNER, ping)
    assert sent.call_args.args[0] == REPORTS
    assert sent.call_args.kwargs["thread_id"] == 88


async def test_project_subdir_job_digest_is_still_published(tmp_path, monkeypatch):
    bgjob = _bgjob(tmp_path)
    root = tmp_path / "neuro"
    project = root / "users" / str(OWNER) / "game-repo"
    project.mkdir(parents=True)
    _write_job(tmp_path, project, root, chat_id=-100123)
    argv = _digest_argv(bgjob, monkeypatch)
    assert "--cwd" not in argv
    assert argv[argv.index("--user-id") + 1] == str(OWNER)

    bus = EventBus()
    bus.publish = AsyncMock()
    response = _api(root, bus).post(
        "/internal/prompt",
        json=_prompt_body(argv),
        headers={"Authorization": "Bearer secret"},
    )
    assert response.status_code == 200
    event = bus.publish.call_args.args[0]
    assert event.working_directory == root
    assert event.created_by == OWNER
    assert event.target_chat_ids == [-100123]
