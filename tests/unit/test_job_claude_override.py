"""dv-a8e4 phase 2: an explicit, one-job Claude override.

Contract pinned here:
- ``CallRoutes.resolve_job(name, *, model=None, harness=None)``: an explicit
  ``harness="claude"`` beats the owner's ``job_switch`` for that call only;
  any other value raises ``ValueError``.
- ``scripts/agentjob --harness claude`` (also with ``--print-route``) and
  ``scripts/bgjob run --harness claude`` carry it; bgjob forwards it to the
  route probe and the generated agentjob line.
- ``job_start(prompt, harness="claude")`` passes ``--harness claude`` to
  bgjob; ``command`` and ``task_id`` jobs reject it.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from src.model_router.routes import CallRoutes
from tests.unit import test_bgjob_codex_box as box
from tests.unit.test_claude import test_agent_tools as base

_REPO = Path(__file__).parents[2]

# fixtures reused from the agent-tools tests
captured_tools, event_bus, scheduler = (
    base.captured_tools,
    base.event_bus,
    base.scheduler,
)
services = base.services
bgjob = box.bgjob


def _codex_table() -> CallRoutes:
    return replace(CallRoutes(), job_switch="openai_codex")


# -- routes ---------------------------------------------------------------


def test_explicit_claude_beats_the_global_codex_switch() -> None:
    routes = _codex_table()
    assert routes.resolve_job("background_job").provider == "openai"
    r = routes.resolve_job("background_job", harness="claude")
    assert r.provider == "anthropic_sub"
    assert r.model == "sonnet"


def test_override_does_not_stick_to_the_next_job() -> None:
    routes = _codex_table()
    before = routes.resolve_job("background_job")
    routes.resolve_job("background_job", harness="claude")
    assert routes.resolve_job("background_job") == before
    assert routes.job_switch == "openai_codex"
    assert before.provider == "openai"


@pytest.mark.parametrize("bad", ["openrouter", "openai_codex", "gpt", "CLAUDE "])
def test_invalid_override_values_fail_clearly(bad: str) -> None:
    with pytest.raises(ValueError, match="harness"):
        _codex_table().resolve_job("background_job", harness=bad)


def test_explicit_claude_honours_an_explicit_model() -> None:
    r = _codex_table().resolve_job("background_job", model="opus", harness="claude")
    assert r.model == "opus"


# -- agentjob -------------------------------------------------------------


def _print_route(*argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(_REPO / "scripts" / "agentjob"), "--print-route", *argv],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=_REPO,
        env={**__import__("os").environ, "PYTHONPATH": str(_REPO)},
    )


def test_agentjob_print_route_accepts_claude_override() -> None:
    r = _print_route("--harness", "claude")
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["harness"] == "claude"


def test_agentjob_rejects_invalid_override_value() -> None:
    r = _print_route("--harness", "openrouter")
    assert r.returncode != 0
    assert "harness" in r.stderr.lower()


# -- bgjob ----------------------------------------------------------------


def _load(name: str) -> Any:
    loader = box.SourceFileLoader(name, str(box._BGJOB_PATH))
    spec = box.importlib.util.spec_from_loader(name, loader)
    mod = box.importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def test_bgjob_claude_override_runs_a_claude_box_under_codex_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The probe gets ``--harness claude`` (and answers Claude); the launch is
    the Claude outer-bwrap one and the agentjob line repeats the override."""
    line, _jobdir, calls = box._launch(
        _load("bgjob_ov1"),
        tmp_path,
        monkeypatch,
        box.CLAUDE_ROUTE,
        "--harness",
        "claude",
    )
    flat = [c if isinstance(c, str) else " ".join(map(str, c)) for c in calls]
    probe = next(c for c in flat if "--print-route" in c)
    assert "--harness claude" in probe
    assert "--harness claude" in line
    assert "bwrap" in line
    assert "--native-preflight" not in line
    assert str(Path.home() / ".codex") not in box._binds(line)


def test_bgjob_without_override_keeps_codex_native_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = _load("bgjob_ov2")
    line, _j, calls = box._launch(mod, tmp_path, monkeypatch, box.CODEX_ROUTE)
    assert "--harness" not in line
    assert "--native-preflight" in line


def test_bgjob_rejects_invalid_harness_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = _load("bgjob_ov3")
    with pytest.raises(SystemExit) as e:
        box._launch(
            mod, tmp_path, monkeypatch, box.CODEX_ROUTE, "--harness", "openrouter"
        )
    assert e.value.code != 0


def test_failed_codex_job_has_no_claude_retry_path() -> None:
    """No implicit fallback: neither launcher mentions retrying on Claude."""
    for name in ("agentjob", "bgjob"):
        text = (_REPO / "scripts" / name).read_text().lower()
        assert "fall back to claude" not in text
        assert "retry on claude" not in text
    # The only Claude switch is the explicit flag.
    assert "--harness" in (_REPO / "scripts" / "agentjob").read_text()


# -- job_start tool -------------------------------------------------------

_fake = base.TestJobStartBoardTask._fake
_tools = base.TestJobStartBoardTask._tools


async def test_job_start_passes_claude_override_to_bgjob(
    services, captured_tools, monkeypatch
) -> None:
    spawned = _fake(monkeypatch)
    tools = _tools(services, captured_tools, "devops")
    r = await tools["job_start"].handler({"prompt": "retry it", "harness": "claude"})
    assert r.get("isError") is not True, r
    run = next(a for a in spawned if Path(a[0]).name == "bgjob")
    assert run[run.index("--harness") + 1] == "claude"
    assert run.index("--harness") < run.index("--")


async def test_job_start_without_override_sends_no_flag(
    services, captured_tools, monkeypatch
) -> None:
    spawned = _fake(monkeypatch)
    tools = _tools(services, captured_tools, "devops")
    r = await tools["job_start"].handler({"prompt": "plain"})
    assert r.get("isError") is not True, r
    run = next(a for a in spawned if Path(a[0]).name == "bgjob")
    assert "--harness" not in run


@pytest.mark.parametrize(
    "args",
    [
        {"command": "make test", "harness": "claude"},
        {"task_id": "dv-0a1b", "harness": "claude"},
        {"prompt": "x", "harness": "openrouter"},
        {"prompt": "x", "harness": "openai_codex"},
    ],
)
async def test_job_start_rejects_command_task_and_invalid_overrides(
    services, captured_tools, monkeypatch, args
) -> None:
    spawned = _fake(monkeypatch)
    tools = _tools(services, captured_tools, "devops")
    r = await tools["job_start"].handler(args)
    assert r.get("isError") is True
    assert "harness" in r["content"][0]["text"].lower()
    assert not any(Path(a[0]).name in ("bgjob",) for a in spawned)
    assert not any("start" in a for a in spawned)
