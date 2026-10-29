"""The design loop's real steps on a board repo (dv-9133 phase 3).

A real git repo (the trimmed gymbro docs), a real ``run_loop``; only the
processes the steps start are faked: the agentjob generator, the board's
render and gate argv, and the collage python. The critic is a fake coroutine.
"""

import subprocess
from pathlib import Path
from typing import Any, Dict, List

import pytest

from src.designloop.brief import build_brief
from src.designloop.critic import CRITERIA, Critique
from src.designloop.loop import run_loop
from src.designloop.steps import (
    JOB_ROUTE,
    BoardSteps,
    DesignConfig,
    parse_config,
    parse_gate_output,
    tradeoff_of,
)

CARD = "gb-d001"
AGENTJOB = Path("/fake/agentjob")
TEMPLATE = (
    Path(__file__).resolve().parents[3] / "config" / "taskpump" / "design_generate.md"
)
CONFIG = DesignConfig(
    render=("render", "--out", "{out}", "--screen", "{screen}"),
    gate=("gate", "{renders}", "{refs}"),
    python="py",
)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repo(gymbro_repo: Path) -> Path:
    for args in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "t@t"],
        ["config", "user.name", "t"],
        ["add", "-A"],
        ["commit", "-qm", "init"],
    ):
        _git(gymbro_repo, *args)
    return gymbro_repo


class Runner:
    """Fakes agentjob / render / gate / collage python; git runs for real.

    ``idle`` = variants whose generator leaves the worktree untouched on its
    first call."""

    def __init__(self, idle=(), gate_out: str = "[]"):
        self.idle = set(idle)
        self.gate_out = gate_out
        self.jobs: List[Dict[str, Any]] = []
        self.gates: List[List[str]] = []

    def __call__(self, cmd, **kw):
        cmd = [str(c) for c in cmd]
        if cmd[0] == str(AGENTJOB):
            cwd = Path(cmd[cmd.index("--cwd") + 1])
            prompt = Path(cmd[cmd.index("--prompt-file") + 1]).read_text()
            name = cwd.name.rsplit("-", 1)[-1]
            self.jobs.append({"cmd": cmd, "cwd": cwd, "prompt": prompt, "v": name})
            if name in self.idle:
                self.idle.discard(name)
            else:
                n = sum(1 for j in self.jobs if j["v"] == name)
                (cwd / "Discover.tsx").write_text(f"{name} draft {n}\n")
                (cwd / "DESIGN_NOTES.md").write_text(
                    f"# Notes\n\nTrade-off: {name} groups the filters\n"
                )
            return subprocess.CompletedProcess(cmd, 0, "done", "")
        if cmd[0] == "render":
            out = Path(cmd[cmd.index("--out") + 1])
            for theme in ("dark", "light"):
                (out / f"main-{theme}.png").write_bytes(b"\x89PNG\r\n\x1a\n")
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if cmd[0] == "gate":
            self.gates.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, self.gate_out, "")
        if cmd[0] == "py":
            out = Path(cmd[cmd.index("--out") + 1])
            out.write_bytes(b"\x89PNG\r\n\x1a\ncollage")
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return subprocess.run(cmd, **kw)


def steps(repo: Path, tmp_path: Path, runner: Runner) -> BoardSteps:
    s = BoardSteps(
        repo=repo,
        card=CARD,
        screen="feed",
        worktrees=tmp_path / "wt" / "gymbro",
        out=repo / ".screens" / "design" / CARD,
        config=CONFIG,
        profile="gymbro",
        agentjob=AGENTJOB,
        template=TEMPLATE,
        run=runner,
    )
    s.prepare()
    return s


def ready() -> Critique:
    return Critique(verdict="ready", criteria={k: "ok" for k in CRITERIA}, score=8)


async def test_a_ready_run_commits_three_real_app_variants_and_offers_them(
    repo, tmp_path
):
    runner = Runner()
    s = steps(repo, tmp_path, runner)
    seen: List[Any] = []

    async def critic(brief, renders):
        seen.append(list(renders))
        return ready()

    async def deliver(text, images):
        seen.append((text, list(images)))

    kwargs = {**s.kwargs(), "critic": critic}
    brief = build_brief(repo, "feed", "Too many filters on top; reduce them")
    outcome = await run_loop(brief, deliver=deliver, **kwargs)

    assert outcome.stop == "ready"
    # One generator job per variant, on the design_generate job route, in
    # that variant's worktree, as the board's profile.
    assert len(runner.jobs) == 3
    for n, job in enumerate(runner.jobs, 1):
        cmd = job["cmd"]
        assert cmd[cmd.index("--route") + 1] == JOB_ROUTE
        assert cmd[cmd.index("--profile") + 1] == "gymbro"
        assert job["cwd"] == tmp_path / "wt" / "gymbro" / f"{CARD}-v{n}"
        assert "Too many filters on top" in job["prompt"]
        # Each branch carries one commit on the real app.
        branch = f"design/{CARD}/v{n}"
        assert _git(repo, "rev-list", "--count", f"main..{branch}") == "1"
        assert f"v{n} draft 1" in _git(repo, "show", f"{branch}:Discover.tsx")
    # The trade-off line comes from DESIGN_NOTES.md.
    assert [v.tradeoff for v in outcome.offered] == [
        f"v{n} groups the filters" for n in (1, 2, 3)
    ]
    assert outcome.collage == repo / ".screens" / "design" / CARD / "collage.png"
    text, images = seen[-1]
    assert images == [outcome.collage]
    assert "v2 groups the filters" in text
    # The board gate got the variant's renders and the current refs.
    assert runner.gates[0][2] == str(repo / "docs" / "screens" / "feed")


async def test_a_variant_with_no_commit_fails_the_gate_and_goes_back(repo, tmp_path):
    runner = Runner(idle={"v2"})
    s = steps(repo, tmp_path, runner)

    async def critic(brief, renders):
        return ready()

    async def deliver(text, images):
        return None

    kwargs = {**s.kwargs(), "critic": critic}
    brief = build_brief(repo, "feed", "Too many filters on top; reduce them")
    outcome = await run_loop(brief, deliver=deliver, **kwargs)

    assert (outcome.stop, outcome.rounds) == ("ready", 1)
    # v1..v3 once, then v2 again with the gate's finding: not a round.
    assert [j["v"] for j in runner.jobs] == ["v1", "v2", "v3", "v2"]
    assert "no commit against the current app" in runner.jobs[-1]["prompt"]
    # The board's own gate never ran on the empty attempt: v1 + v3, then all 3.
    assert len(runner.gates) == 2 + 3


def test_parse_config_takes_argv_lists_and_refuses_the_rest():
    good = {"render": ["r", "{out}"], "gate": ["g"], "python": "/py"}
    assert parse_config(good) == DesignConfig(("r", "{out}"), ("g",), "/py")
    assert parse_config({**good, "variants": 2}).variants == 2
    for bad in (
        None,
        "x",
        {**good, "render": "r {out}"},
        {**good, "gate": []},
        {**good, "python": ""},
        {**good, "variants": True},
    ):
        assert parse_config(bad) is None, bad


def test_gate_output_is_a_list_or_failures_object():
    assert parse_gate_output("[]") == []
    assert parse_gate_output('["EN: clipped"]') == ["EN: clipped"]
    assert parse_gate_output('{"failures": ["tap 32 px"]}') == ["tap 32 px"]
    with pytest.raises(ValueError):
        parse_gate_output('"pass"')


async def test_a_gate_that_prints_no_json_stops_the_run(repo, tmp_path):
    s = steps(repo, tmp_path, Runner(gate_out="Traceback ..."))
    await s.generate(build_brief(repo, "feed", "x"), {})
    renders = await s.render(f"design/{CARD}/v1")
    with pytest.raises(RuntimeError, match="gate of design/gb-d001/v1"):
        await s.gate(f"design/{CARD}/v1", renders)


def test_tradeoff_line():
    assert tradeoff_of("# N\n\nTrade-off: map first, list in a sheet\n") == (
        "map first, list in a sheet"
    )
    assert tradeoff_of("- one list\n") == "one list"
    assert tradeoff_of("") == ""
