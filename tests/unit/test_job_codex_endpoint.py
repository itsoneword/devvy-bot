"""dv-d63d phase 3: the Codex job's loopback policy endpoint and bgjob's route
probe, beyond the end-to-end tests in test_job_codex.py."""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import subprocess
import urllib.error
import urllib.request
from importlib.machinery import SourceFileLoader
from pathlib import Path
from typing import Any, Dict, Tuple
from unittest.mock import AsyncMock

import pytest

from src.agent_backends.codex_cli import policy_hook_args
from src.model_router.job import BotLink, JobSpec
from src.model_router.job_codex import JobPolicyEndpoint, codex_job_argv
from src.model_router.routes import Route

_REPO = Path(__file__).parents[2]


def _spec(cwd: Path) -> JobSpec:
    return JobSpec(
        purpose="background_job",
        name="pump-dv-x-review",
        prompt="x",
        model="openai-codex/gpt-6-sol",
        route=Route("openai_codex", "openai", "openai-codex/gpt-6-sol"),
        working_directory=cwd,
        full_access=True,
        unrestricted=True,
        profile="devops",
    )


def _post(url: str, token: str, body: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {token}"},
        method="POST",
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


async def test_endpoint_refuses_a_wrong_token_without_deciding(tmp_path: Path) -> None:
    link = BotLink("http://x:1", None)
    link.ask = AsyncMock(return_value=True)  # type: ignore[method-assign]
    endpoint = JobPolicyEndpoint(_spec(tmp_path), link)
    await endpoint.start()
    try:
        status, answer = await asyncio.to_thread(
            _post,
            endpoint.url,
            "not-the-token",
            {"tool_name": "Bash", "tool_input": {"command": "git push"}},
        )
    finally:
        await endpoint.close()
    assert status == 401 and answer["decision"] == "deny"
    link.ask.assert_not_awaited()


async def test_a_hook_fired_twice_for_one_call_asks_the_owner_once(
    tmp_path: Path,
) -> None:
    link = BotLink("http://x:1", None)
    link.ask = AsyncMock(return_value=False)  # type: ignore[method-assign]
    endpoint = JobPolicyEndpoint(_spec(tmp_path), link)
    await endpoint.start()
    body = {
        "tool_name": "Bash",
        "tool_input": {"command": "git push origin main"},
        "tool_use_id": "call_1",
    }
    try:
        first = await asyncio.to_thread(_post, endpoint.url, endpoint.token, body)
        second = await asyncio.to_thread(_post, endpoint.url, endpoint.token, body)
    finally:
        await endpoint.close()
    assert first == second
    assert first[1]["decision"] == "deny"
    assert "Denied by owner" in first[1]["reason"]
    link.ask.assert_awaited_once()
    # The ask carries the job's name and profile, as on Claude jobs.
    assert link.ask.await_args.args[2] == "pump-dv-x-review"
    assert link.ask.await_args.kwargs["context"]["profile"] == "devops"


def test_codex_job_mounts_the_same_hook_builder_as_chat(tmp_path: Path) -> None:
    argv = codex_job_argv(_spec(tmp_path), "codex", tmp_path / "o.txt", None)
    hook = policy_hook_args(3720)
    start = argv.index(hook[0])
    assert argv[start : start + len(hook)] == hook
    assert argv[-1] == "-" and "resume" not in argv


async def test_missing_codex_fails_loud_and_runs_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.model_router import job_codex
    from src.model_router.job import run_job

    monkeypatch.setattr(job_codex, "codex_program", lambda *a: None)
    link = BotLink("http://x:1", None)
    link.record = AsyncMock()  # type: ignore[method-assign]
    assert await run_job(_spec(tmp_path), link) == 2
    link.record.assert_not_awaited()


@pytest.fixture
def bgjob() -> Any:
    loader = SourceFileLoader("bgjob_probe_under_test", str(_REPO / "scripts/bgjob"))
    spec = importlib.util.spec_from_loader("bgjob_probe_under_test", loader)
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


@pytest.mark.parametrize(
    "rc,stdout,expected",
    [
        (0, '{"harness": "openai_codex"}', "openai_codex"),
        (1, "", ""),  # route table error: agentjob reports it itself
        (0, "not json", ""),
    ],
)
def test_bgjob_route_probe(
    bgjob: Any,
    monkeypatch: pytest.MonkeyPatch,
    rc: int,
    stdout: str,
    expected: str,
) -> None:
    seen = []

    def fake_run(cmd: Any, **kw: Any) -> Any:
        seen.append(cmd)
        return subprocess.CompletedProcess(cmd, rc, stdout, "")

    monkeypatch.setattr(bgjob.subprocess, "run", fake_run)
    args = argparse.Namespace(route="pump_review", model="sonnet")
    assert bgjob.job_harness("py", Path("agentjob"), args) == expected
    assert seen == [
        [
            "py",
            "agentjob",
            "--print-route",
            "--route",
            "pump_review",
            "--model",
            "sonnet",
        ]
    ]
