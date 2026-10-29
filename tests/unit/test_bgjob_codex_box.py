"""dv-d63d phase 3: the bgjob box of a Codex job.

bgjob resolves the job route before it builds the bwrap box (``agentjob
--print-route``, analysis "Codex jobs"): a Codex job also needs ``~/.codex``
writable (sessions, rollouts, auth refresh); a Claude job does not get it.
A pump stage (``--cwd <worktree>``) writes only its job dir, its worktree
and the CLI homes -- never the live checkout.
"""

from __future__ import annotations

import importlib.util
import re
import shutil
import subprocess
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path
from typing import Any, List, Tuple

import pytest

_REPO = Path(__file__).parents[2]
_BGJOB_PATH = _REPO / "scripts" / "bgjob"

CODEX_ROUTE = (
    '{"harness": "openai_codex", "provider": "openai", '
    '"model": "openai-codex/gpt-6-sol"}'
)
CLAUDE_ROUTE = '{"harness": "claude", "provider": "anthropic_sub", "model": "sonnet"}'


@pytest.fixture
def bgjob():
    loader = SourceFileLoader("bgjob_codex_under_test", str(_BGJOB_PATH))
    spec = importlib.util.spec_from_loader("bgjob_codex_under_test", loader)
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def _launch(
    bgjob: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    route_json: str,
    *argv: str,
) -> Tuple[str, Path, List[Any]]:
    """`bgjob run <argv>` through the real parser; nothing launched. The
    route probe answers ``route_json``. Returns (agentjob line, job dir,
    every subprocess argv)."""
    jobs, ws = tmp_path / "jobs", tmp_path / "ws"
    jobs.mkdir()
    ws.mkdir()
    real_bwrap = shutil.which("bwrap") or "/usr/bin/bwrap"
    monkeypatch.setattr(bgjob, "JOBS_ROOT", jobs)
    monkeypatch.setattr(bgjob, "load_profiles", lambda: {"general": str(ws)})
    monkeypatch.setattr(bgjob, "load_extra_dirs", lambda slug: [])
    monkeypatch.setattr(bgjob, "live_jobs", lambda: [])
    monkeypatch.setattr(bgjob, "_venv_python", lambda: sys.executable)
    monkeypatch.setattr(
        bgjob.shutil, "which", lambda n: real_bwrap if n == "bwrap" else "/bin/sh"
    )
    calls: List[Any] = []

    def _answer(cmd: Any) -> str:
        flat = cmd if isinstance(cmd, str) else " ".join(map(str, cmd))
        return route_json if "--print-route" in flat else ""

    def fake_run(cmd: Any, **kw: Any) -> Any:
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, _answer(cmd), "")

    def fake_check_output(cmd: Any, **kw: Any) -> Any:
        calls.append(cmd)
        out = _answer(cmd)
        text = kw.get("text") or kw.get("universal_newlines") or kw.get("encoding")
        return out if text else out.encode()

    monkeypatch.setattr(bgjob.subprocess, "run", fake_run)
    monkeypatch.setattr(bgjob.subprocess, "check_output", fake_check_output)
    monkeypatch.setattr(
        sys, "argv", ["bgjob", "run", "do it", "--profile", "general", "--quiet", *argv]
    )
    assert bgjob.main() == 0
    run_sh = next(jobs.glob("*/run.sh"))
    line = next(
        ln
        for ln in run_sh.read_text().splitlines()
        if "agentjob" in ln and "--prompt-file" in ln
    )
    return line, run_sh.parent, calls


def _binds(line: str) -> List[str]:
    """The rw binds of the bwrap box on the agentjob line."""
    return [m.group(1).strip("'\"") for m in re.finditer(r"--bind (\S+) \1", line)]


CODEX_HOME = str(Path.home() / ".codex")
CLAUDE_HOME = str(Path.home() / ".claude")


def test_codex_job_box_makes_the_codex_home_writable(
    bgjob: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """jobs.background_job on openai_codex: the box lets codex write its
    sessions and refresh its login, or the job cannot answer."""
    line, _jobdir, calls = _launch(bgjob, tmp_path, monkeypatch, CODEX_ROUTE)

    assert any(
        "--print-route" in (c if isinstance(c, str) else " ".join(map(str, c)))
        for c in calls
    ), "bgjob must resolve the job route before building the box"
    assert CODEX_HOME in _binds(line)


def test_claude_job_box_does_not_get_the_codex_home(
    bgjob: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Guard: a Claude job keeps today's write set (no ~/.codex)."""
    line, _jobdir, _calls = _launch(bgjob, tmp_path, monkeypatch, CLAUDE_ROUTE)

    binds = _binds(line)
    assert CLAUDE_HOME in binds
    assert CODEX_HOME not in binds


def test_codex_pump_stage_box_writes_only_its_worktree(
    bgjob: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Codex pump stage (`--cwd <worktree> --route pump_implement`) may
    write its job dir, its worktree and the CLI homes; not the live checkout
    and not the profile's workspace."""
    worktree = tmp_path / "pump-worktrees" / "dv-x"
    worktree.mkdir(parents=True)

    line, jobdir, _calls = _launch(
        bgjob,
        tmp_path,
        monkeypatch,
        CODEX_ROUTE,
        "--cwd",
        str(worktree),
        "--route",
        "pump_implement",
    )

    assert "--route pump_implement" in line
    # /tmp is the box's shared scratch for every job (job_box_prefix).
    assert sorted(b for b in _binds(line) if b != "/tmp") == sorted(
        [str(jobdir), str(worktree.resolve()), CLAUDE_HOME, CODEX_HOME]
    )
    assert str(_REPO) not in _binds(line)
    assert str(tmp_path / "ws") not in _binds(line)
