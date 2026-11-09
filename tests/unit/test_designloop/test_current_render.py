"""dv-a8d6: the current app, rendered once per run, is the reference.

Same fakes as test_steps.py; only the processes are faked, git is real.
"""

import subprocess
from pathlib import Path
from typing import Any, List

import pytest

import src.designloop.steps as steps_mod
from src.designloop.brief import build_brief
from src.designloop.critic import CRITERIA, Critique
from src.designloop.loop import Variant
from tests.unit.test_designloop.test_steps import (  # noqa: F401
    CARD,
    CONFIG,
    Runner,
    _git,
    repo,
    steps,
)


class CurrentRunner(Runner):
    """Records every render call (argv + cwd); the current render can fail."""

    def __init__(self, current_rc: int = 0, current_pngs: bool = True, **kw: Any):
        super().__init__(**kw)
        self.renders: List[Any] = []
        self.current_rc = current_rc
        self.current_pngs = current_pngs

    def __call__(self, cmd, **kw):
        cmd = [str(c) for c in cmd]
        if cmd[0] == "render":
            out = Path(cmd[cmd.index("--out") + 1])
            self.renders.append((cmd, out, Path(str(kw.get("cwd")))))
            if out.name == "current":
                if self.current_rc:
                    return subprocess.CompletedProcess(cmd, self.current_rc, "", "boom")
                if not self.current_pngs:
                    return subprocess.CompletedProcess(cmd, 0, "", "")
        return super().__call__(cmd, **kw)

    def current_calls(self) -> List[Any]:
        return [r for r in self.renders if r[1].name == "current"]


async def _run_one(repo: Path, tmp_path: Path, runner: CurrentRunner):
    s = steps(repo, tmp_path, runner)
    await s.generate(build_brief(repo, "feed", "x"), {})
    renders = await s.render(f"design/{CARD}/v1")
    return s, renders


async def test_current_app_is_rendered_once_at_base_in_a_branchless_worktree(
    repo, tmp_path
):
    runner = CurrentRunner()
    s, _ = await _run_one(repo, tmp_path, runner)
    await s.render(f"design/{CARD}/v2")
    await s.render(f"design/{CARD}/v1")

    calls = runner.current_calls()
    assert len(calls) == 1  # once per run, not per variant or round
    argv, out, cwd = calls[0]
    assert out == s.out / "current"
    assert argv[argv.index("--screen") + 1] == "feed"
    assert cwd not in [s.worktree(f"design/{CARD}/v{n}") for n in (1, 2, 3)]
    assert _git(cwd, "rev-parse", "HEAD") == _git(repo, "rev-parse", "main")
    detached = subprocess.run(
        ["git", "symbolic-ref", "-q", "HEAD"], cwd=cwd, capture_output=True
    )
    assert detached.returncode != 0  # no branch
    assert sorted(p.name for p in out.glob("*.png")) == [
        "main-dark.png",
        "main-light.png",
    ]


async def test_a_failed_current_render_raises(repo, tmp_path):
    for runner in (CurrentRunner(current_rc=1), CurrentRunner(current_pngs=False)):
        with pytest.raises(RuntimeError):
            await _run_one(repo, tmp_path / f"{runner.current_rc}", runner)


async def test_gate_refs_are_the_current_render_dir(repo, tmp_path):
    runner = CurrentRunner()
    s, renders = await _run_one(repo, tmp_path, runner)
    await s.gate(f"design/{CARD}/v1", renders)
    assert runner.gates[-1][2] == str(s.out / "current")


async def test_critic_gets_the_current_render_as_reference(repo, tmp_path, monkeypatch):
    runner = CurrentRunner()
    s, renders = await _run_one(repo, tmp_path, runner)
    brief = build_brief(repo, "feed", "x")
    seen: List[Any] = []

    async def fake(b, r, *a, **kw):
        seen.append((b, list(r)))
        return Critique(verdict="ready", criteria={k: "ok" for k in CRITERIA}, score=8)

    monkeypatch.setattr(steps_mod, "critique", fake)
    await s.critic(brief, renders)
    got_brief, got_renders = seen[0]
    assert got_renders == list(renders)
    assert sorted(p.name for p in got_brief.refs) == ["main-dark.png", "main-light.png"]
    assert all(Path(p).parent == s.out / "current" for p in got_brief.refs)


async def test_critic_falls_back_to_docs_refs_without_a_current_png(
    repo, tmp_path, monkeypatch
):
    runner = CurrentRunner()
    s, renders = await _run_one(repo, tmp_path, runner)
    for p in (s.out / "current").glob("*.png"):
        p.unlink()
    brief = build_brief(repo, "feed", "x")
    seen: List[Any] = []

    async def fake(b, r, *a, **kw):
        seen.append(b)
        return Critique(verdict="ready", criteria={k: "ok" for k in CRITERIA}, score=8)

    monkeypatch.setattr(steps_mod, "critique", fake)
    await s.critic(brief, renders)
    assert tuple(seen[0].refs) == tuple(brief.refs)


async def test_collage_starts_with_the_current_dark_tile(repo, tmp_path):
    runner = CurrentRunner()
    s, renders = await _run_one(repo, tmp_path, runner)
    cmds: List[List[str]] = []
    inner = runner.__call__

    def spy(cmd, **kw):
        if str(cmd[0]) == "py":
            cmds.append([str(c) for c in cmd])
        return inner(cmd, **kw)

    s.run = spy
    variants = [
        Variant(branch=f"design/{CARD}/v{n}", renders=tuple(renders), tradeoff=f"t{n}")
        for n in (1, 2)
    ]
    await s.collage(variants)
    cmd = cmds[0]
    tiles = [cmd[i + 1] for i, a in enumerate(cmd) if a == "--tile"]
    labels = [cmd[i + 1] for i, a in enumerate(cmd) if a == "--label"]
    assert tiles[0] == str(s.out / "current" / "main-dark.png")
    assert labels[:3] == ["0. current", "1. t1", "2. t2"]
    assert len(tiles) == 3
