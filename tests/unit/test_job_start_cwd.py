"""dv-be37: job_start cwd / allow_dirs and the job confinement notice."""

import asyncio
import importlib.machinery
import importlib.util
from pathlib import Path
from typing import Any, List, Tuple
from unittest.mock import AsyncMock

import pytest

from src.claude import agent_tools
from src.claude.tool_context import AgentServices, TurnContext
from src.profiles.registry import ProfileDefinition, ProfileRegistry

REPO = Path(__file__).resolve().parents[2]


class _Proc:
    returncode = 0

    async def communicate(self) -> Tuple[bytes, bytes]:
        return b"started", b""


@pytest.fixture
def spawned(monkeypatch: Any) -> List[Tuple[str, ...]]:
    calls: List[Tuple[str, ...]] = []

    async def _fake(*argv: str, **kw: Any) -> _Proc:
        calls.append(tuple(argv))
        return _Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake)
    return calls


def _setup(tmp_path: Path, sandbox: bool) -> Tuple[AgentServices, TurnContext]:
    ws = tmp_path / "ws"
    ws.mkdir()
    reg = ProfileRegistry(
        [
            ProfileDefinition(
                slug="dev",
                name="dev",
                working_dir=ws,
                memory_dir=ws / "memory",
                persona=ws / "persona.md",
                default=True,
                sandbox=sandbox,
            )
        ]
    )
    services = AgentServices(
        event_bus=AsyncMock(), task_repo=AsyncMock(), profile_registry=reg
    )
    ctx = TurnContext(
        profile_slug="dev",
        working_dir=ws,
        requester_chat_id=900001,
        memory_agent_id="dev#u1",
        user_id=1,
        role="owner",
    )
    return services, ctx


@pytest.fixture
def full(tmp_path: Path) -> Tuple[AgentServices, TurnContext]:
    return _setup(tmp_path, sandbox=False)


@pytest.fixture
def jailed(tmp_path: Path) -> Tuple[AgentServices, TurnContext]:
    return _setup(tmp_path, sandbox=True)


def _outside(tmp_path: Path, name: str = "repo") -> Path:
    d = tmp_path / name
    d.mkdir()
    return d


def _flag_values(argv: Tuple[str, ...], flag: str) -> List[str]:
    return [argv[i + 1] for i, a in enumerate(argv) if a == flag]


async def test_C1_cwd_passed_to_bgjob(full, spawned, tmp_path):
    services, ctx = full
    repo = _outside(tmp_path)
    out = await agent_tools.job_start(
        services, ctx, {"prompt": "do it", "cwd": str(repo)}
    )
    assert not out.get("isError")
    argv = spawned[0]
    assert _flag_values(argv, "--cwd") == [str(repo.resolve())]


async def test_C1_no_cwd_unchanged(full, spawned):
    services, ctx = full
    await agent_tools.job_start(services, ctx, {"prompt": "do it"})
    argv = spawned[0]
    assert _flag_values(argv, "--cwd") == [str(ctx.working_dir)]
    assert "--allow-dir" not in argv


async def test_C2_allow_dirs_before_separator(full, spawned, tmp_path):
    services, ctx = full
    d1, d2 = _outside(tmp_path, "a"), _outside(tmp_path, "b")
    await agent_tools.job_start(
        services, ctx, {"prompt": "do it", "allow_dirs": [str(d1), str(d2)]}
    )
    argv = spawned[0]
    assert _flag_values(argv, "--allow-dir") == [str(d1.resolve()), str(d2.resolve())]
    sep = argv.index("--")
    assert all(i < sep for i, a in enumerate(argv) if a == "--allow-dir")
    assert argv[sep + 1] == "do it"


async def test_C3_sandboxed_profile_refused(jailed, spawned, tmp_path):
    services, ctx = jailed
    repo = _outside(tmp_path)
    for extra in ({"cwd": str(repo)}, {"allow_dirs": [str(repo)]}):
        out = await agent_tools.job_start(services, ctx, {"prompt": "x", **extra})
        assert out["isError"] is True
    assert spawned == []


async def test_C3_sandboxed_inside_workspace_ok(jailed, spawned):
    services, ctx = jailed
    sub = Path(ctx.working_dir) / "sub"
    sub.mkdir()
    out = await agent_tools.job_start(
        services, ctx, {"prompt": "x", "cwd": str(sub), "allow_dirs": [str(sub)]}
    )
    assert not out.get("isError")
    assert spawned


@pytest.mark.parametrize(
    "extra",
    [
        {"cwd": "/nonexistent-dv-be37"},
        {"cwd": "/"},
        {"cwd": "relative/dir"},
        {"allow_dirs": ["/nonexistent-dv-be37"]},
        {"allow_dirs": ["/"]},
        {"allow_dirs": ["relative/dir"]},
        {"allow_dirs": "/tmp"},
        {"allow_dirs": [1]},
    ],
)
async def test_C5_invalid_values_refused(full, spawned, extra):
    services, ctx = full
    out = await agent_tools.job_start(services, ctx, {"prompt": "x", **extra})
    assert out["isError"] is True
    assert spawned == []


async def test_C5_with_task_id_refused(full, spawned, tmp_path):
    services, ctx = full
    repo = _outside(tmp_path)
    for extra in ({"cwd": str(repo)}, {"allow_dirs": [str(repo)]}):
        out = await agent_tools.job_start(
            services, ctx, {"task_id": "dv-abcd", **extra}
        )
        assert out["isError"] is True
    assert spawned == []


async def test_C7_command_job_cwd(full, spawned, tmp_path):
    services, ctx = full
    repo = _outside(tmp_path)
    out = await agent_tools.job_start(
        services, ctx, {"command": "echo hi", "cwd": str(repo)}
    )
    assert not out.get("isError")
    assert _flag_values(spawned[0], "--cwd") == [str(repo.resolve())]


async def test_C7_command_job_allow_dirs_refused(full, spawned, tmp_path):
    services, ctx = full
    repo = _outside(tmp_path)
    out = await agent_tools.job_start(
        services, ctx, {"command": "echo hi", "allow_dirs": [str(repo)]}
    )
    assert out["isError"] is True
    assert spawned == []


def test_C6_description_documents_params():
    spec = next(s for s in agent_tools.AGENT_TOOL_SPECS if s.name == "job_start")
    assert "can only write there" not in spec.description
    assert "cwd" in spec.description and "allow_dirs" in spec.description
    assert "cwd" in spec.schema["properties"]
    assert "allow_dirs" in spec.schema["properties"]
    assert spec.schema["properties"]["allow_dirs"]["type"] == "array"


@pytest.fixture
def bgjob(monkeypatch):
    path = str(REPO / "scripts" / "bgjob")
    loader = importlib.machinery.SourceFileLoader("bgjob_be37", path)
    spec = importlib.util.spec_from_loader("bgjob_be37", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    monkeypatch.setattr(mod, "job_harness", lambda *a: "claude")
    return mod


def test_C4_prompt_job_gets_confinement_notice(bgjob, tmp_path, monkeypatch):
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    ws = tmp_path / "ws"
    ws.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    extra = tmp_path / "extra"
    extra.mkdir()
    monkeypatch.setattr(bgjob, "JOBS_ROOT", jobs)
    monkeypatch.setattr(bgjob, "load_profiles", lambda: {"general": str(ws)})
    monkeypatch.setattr(bgjob, "live_jobs", lambda: [])
    monkeypatch.setattr(bgjob.shutil, "which", lambda name: "/bin/sh")

    class R:
        returncode = 0
        stderr = ""
        stdout = "{}"

    monkeypatch.setattr(bgjob.subprocess, "run", lambda cmd, **kw: R())
    args = type(
        "A",
        (),
        {
            "prompt": "do it",
            "profile": "general",
            "name": "t",
            "allow_dir": [str(extra)],
            "cwd": str(repo),
            "model": "sonnet",
        },
    )()
    bgjob.cmd_run(args)
    prompt = next(jobs.glob("*/prompt.txt")).read_text()
    low = prompt.lower()
    assert "read-only" in low
    assert "not a host fault" in low or "not a fault" in low
    assert "remount" in low
    assert str(repo.resolve()) in prompt
    assert str(extra.resolve()) in prompt
    assert prompt.rstrip().endswith("do it")


def test_C8_prompt_budgets():
    assert (REPO / "templates" / "bgjob_preamble.md").stat().st_size < 2048
    assert (REPO / "data" / "shared-capabilities.md").stat().st_size <= 10000
