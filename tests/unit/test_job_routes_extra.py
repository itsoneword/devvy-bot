"""Job routes (dv-d63d phase 2): edges beyond tests/unit/test_job_routes.py."""

import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

from src.model_router.job import BotLink, JobSpec, run_job
from src.model_router.routes import (
    PROVIDER_OPENROUTER,
    CallRoutes,
    Route,
    describe_call_routes,
    load_call_routes,
)
from src.model_router.types import JOB_ROUTE_NAMES

REPO = Path(__file__).resolve().parents[2]
SLUG = "z-ai/glm-5.3"


def _routes(tmp_path: Path, body: str) -> CallRoutes:
    path = tmp_path / "models.yaml"
    path.write_text(body, encoding="utf-8")
    return load_call_routes(path)


def test_jobs_default_on_openrouter_moves_every_job(tmp_path: Path) -> None:
    """The example file's 'every job on OpenRouter' preset: one row."""
    routes = _routes(
        tmp_path,
        f"jobs:\n  default: {{harness: openrouter, models: {{openrouter: {SLUG}}}}}\n",
    )
    for name in JOB_ROUTE_NAMES:
        job = routes.resolve_job(name)
        assert (job.harness, job.provider, job.model) == (
            "claude",
            PROVIDER_OPENROUTER,
            SLUG,
        )


def test_top_level_default_does_not_move_jobs(tmp_path: Path) -> None:
    """The plain-call default row is not the jobs' default."""
    routes = _routes(tmp_path, "default:\n  harness: openai_codex\n")
    assert routes.resolve_job("background_job").provider == "anthropic_sub"


def test_job_row_with_a_fallback_is_rejected(tmp_path: Path) -> None:
    """Owner 2026-10-26: a job never falls back to another harness."""
    with pytest.raises(ValueError, match="never fall back"):
        _routes(tmp_path, "jobs:\n  pump_review: {fallback: {harness: claude}}\n")


def test_unknown_job_route_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="pump_nope"):
        _routes(tmp_path, "jobs:\n  pump_nope: {}\n")
    with pytest.raises(ValueError, match="unknown job route"):
        CallRoutes().resolve_job("pump_nope")


def test_openrouter_job_without_a_model_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="no model"):
        _routes(tmp_path, "jobs:\n  pump_review: {harness: openrouter}\n")


def test_show_config_lists_the_job_routes() -> None:
    lines = describe_call_routes(CallRoutes())
    assert "job pump_implement: claude / anthropic_sub / opus" in lines
    assert "job background_job: claude / anthropic_sub / sonnet" in lines


def test_agentjob_prints_the_resolved_route(tmp_path: Path) -> None:
    path = tmp_path / "models.yaml"
    path.write_text(
        f"jobs:\n  pump_review: {{harness: openrouter, models: {{openrouter: {SLUG}}}}}\n"
    )
    env = {"PATH": "/usr/bin:/bin", "MODEL_ROUTES_PATH": str(path), "HOME": "/tmp"}
    out = subprocess.run(
        [
            sys.executable,
            str(REPO / "scripts" / "agentjob"),
            "--print-route",
            "--route",
            "pump_review",
            "--model",
            "sonnet",
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) == {
        "harness": "claude",
        "provider": "openrouter",
        "model": SLUG,
    }
    assert "ignored" in out.stderr  # the dropped --model sonnet is logged


def _spec(tmp_path: Path, route: Route) -> JobSpec:
    return JobSpec(
        purpose="background_job",
        name="j",
        prompt="x",
        model=route.model or "",
        route=route,
        working_directory=tmp_path,
        full_access=True,
        unrestricted=True,
    )


async def test_a_codex_job_route_fails_loud_without_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex jobs come in phase 3: until then a codex route never runs the
    job on Claude instead."""
    monkeypatch.setattr(
        "src.model_router.job.ClaudeSDKClient",
        lambda *a, **k: pytest.fail("no client may be built"),
    )
    route = Route("openai_codex", "openai", "openai-codex/gpt-6-sol")
    assert await run_job(_spec(tmp_path, route), BotLink("http://x:1", None)) == 2


async def test_an_openrouter_job_with_no_key_fails_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setattr("src.model_router.job.openrouter_key", lambda: None)
    monkeypatch.setattr(
        "src.model_router.job.ClaudeSDKClient",
        lambda *a, **k: pytest.fail("no client may be built"),
    )
    route = Route("claude", PROVIDER_OPENROUTER, SLUG)
    code = await run_job(
        _spec(tmp_path, route), BotLink("http://x:1", None), out=io.StringIO()
    )
    assert code == 1
    assert "/setkey openrouter" in capsys.readouterr().err
