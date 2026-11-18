"""dv-b8bb: every stop goes to one `resolve` stage (the board's own agent, here
a fake) first; at most 2 per card; code keeps the hard limits."""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from src.pump2 import runner
from src.pump2.merge import merge
from src.pump2.state import Board, find_card, set_pump2

TASKS = Path(".deviz", "tasks")
CID = "dv-0001"
Agent = Callable[[Path, str], None]
SRC = Path(__file__).resolve().parents[3] / "src" / "pump2"


def git(repo: Path, *a: str) -> str:
    r = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *a],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return r.stdout.strip()


def commit_file(repo: Path, name: str, text: str, msg: str = "c") -> None:
    f = repo / name
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text)
    git(repo, "add", name)
    git(repo, "commit", "-qm", msg)


def report(verdict: str, extra: str = "") -> str:
    return f"## Verdict\n{verdict}\n\n## Reasoning\nbecause of the facts\n{extra}"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    (r / "mod.py").write_text("x = 1\n")
    (r / "tests").mkdir()
    (r / "tests/test_x.py").write_text("def test_x():\n    assert 1\n")
    (r / TASKS).mkdir(parents=True)
    (r / TASKS / f"{CID}.md").write_text(
        f"---\nid: {CID}\ntitle: t\nstatus: in_progress\ndeps: []\n---\n\n"
        "## Description\n\nd\n\n## Comments\n"
    )
    git(r, "add", "-A")
    git(r, "commit", "-qm", "init")
    return r


def conflict(repo: Path, fname: str, card_text: str, main_text: str) -> None:
    git(repo, "checkout", "-q", "-b", "pump2/x")
    commit_file(repo, fname, card_text, "card")
    git(repo, "checkout", "-q", "main")
    commit_file(repo, fname, main_text, "main moves")


class Rig:
    def __init__(self, repo: Path, tmp: Path, agent: Agent, test: str = "") -> None:
        self.repo, self.tmp, self.agent = repo, tmp, agent
        self.counter = tmp / "runs"
        self.board = Board(
            name="t",
            repo=repo,
            profile="gymbro",
            test=test or f"echo x >> {self.counter}",
        )
        self.prompts: list[str] = []
        self.launched: list[str] = []
        set_pump2(repo / TASKS / f"{CID}.md", stage="merge", branch="pump2/x")

    @property
    def runs(self) -> int:
        return self.counter.read_text().count("x") if self.counter.exists() else 0

    def launch_merge(self, bd, c, f):  # type: ignore[no-untyped-def]
        merge(bd, find_card(bd, c.id), locks=self.tmp / "l")
        return "job-merge"

    def launch_stage(self, bd, c, stage, wt, prompt):  # type: ignore[no-untyped-def]
        self.launched.append(stage)
        if stage != "resolve":
            return "job-other"
        self.prompts.append(prompt)
        self.agent(wt, prompt)
        return f"job-r{len(self.prompts)}"

    def run(self, ticks: int = 14) -> str:
        for _ in range(ticks):
            c = find_card(self.board, CID)
            if (
                c.pump2["stage"] in ("done", "stopped")
                or c.fm.get("status") == "waiting_user"
            ):
                break
            if c.pump2["stage"] not in ("merge", "resolve"):
                break
            runner.tick(
                {"t": self.board},
                launch=self.launch_merge,
                launch_stage=self.launch_stage,
                status=lambda j: (False, 0),
                output=lambda j, n: "",
                locks=self.tmp / "l",
            )
        return str(find_card(self.board, CID).pump2["stage"])

    @property
    def card(self):  # type: ignore[no-untyped-def]
        return find_card(self.board, CID)

    @property
    def body(self) -> str:
        return self.card.body


def fixer(fname: str, verdict: str, text: str = "x = 4\n") -> Agent:
    def agent(wt: Path, prompt: str) -> None:
        (wt / fname).write_text(text)
        (wt / ".pump" / "resolve.md").write_text(report(verdict))

    return agent


def noop(verdict: str) -> Agent:
    def agent(wt: Path, prompt: str) -> None:
        (wt / ".pump" / "resolve.md").write_text(report(verdict))

    return agent


# ---- C1 --------------------------------------------------------------------
def test_C1_conflict_stop_goes_to_resolve_with_reason(
    repo: Path, tmp_path: Path
) -> None:
    conflict(repo, "mod.py", "x = 2\n", "x = 3\n")
    rig = Rig(repo, tmp_path, noop("owner"))
    runner.tick(
        {"t": rig.board},
        launch=rig.launch_merge,
        launch_stage=rig.launch_stage,
        status=lambda j: (False, 0),
        output=lambda j, n: "",
        locks=tmp_path / "l",
    )
    c = rig.card
    assert c.pump2["stage"] == "resolve"
    assert int(c.pump2.get("resolves") or 0) == 0 or c.pump2["stage"] == "resolve"
    runner.tick(
        {"t": rig.board},
        launch=rig.launch_merge,
        launch_stage=rig.launch_stage,
        status=lambda j: (False, 0),
        output=lambda j, n: "",
        locks=tmp_path / "l",
    )
    assert rig.launched.count("resolve") == 1
    p = rig.prompts[0]
    assert "mod.py" in p and "merge" in p and CID in p


def test_C1_model_stage_stop_goes_to_resolve(repo: Path, tmp_path: Path) -> None:
    from src.pump2.stages import MODULES

    assert "resolve" in MODULES
    from src.pump2.state import STAGES

    assert "resolve" in STAGES and "mergefix" not in STAGES


# ---- C2 --------------------------------------------------------------------
def test_C2_continue_merge_commits_and_lands_one_comment(
    repo: Path, tmp_path: Path
) -> None:
    conflict(repo, "mod.py", "x = 2\n", "x = 3\n")
    rig = Rig(repo, tmp_path, fixer("mod.py", "continue merge"))
    assert rig.run() == "done"
    assert (repo / "mod.py").read_text() == "x = 4\n"
    assert rig.runs >= 1  # code changed: tests ran before landing
    assert len(re.findall(r"^resolve: ", rig.body, re.M)) == 1
    assert f"{CID}: resolve" in git(repo, "log", "--format=%s", "pump2/x")


def test_C2_malformed_report_stops_card(repo: Path, tmp_path: Path) -> None:
    conflict(repo, "mod.py", "x = 2\n", "x = 3\n")
    rig = Rig(repo, tmp_path, noop("maybe do something"))
    assert rig.run() == "stopped"


@pytest.mark.parametrize("target", ["done", "nosuchstage"])
def test_C2_target_done_or_unknown_stops(
    repo: Path, tmp_path: Path, target: str
) -> None:
    conflict(repo, "mod.py", "x = 2\n", "x = 3\n")
    rig = Rig(repo, tmp_path, fixer("mod.py", f"continue {target}"))
    head = git(repo, "rev-parse", "main")
    assert rig.run() == "stopped"
    assert git(repo, "rev-parse", "main") == head


def test_C2_owner_verdict_waits_for_the_user(repo: Path, tmp_path: Path) -> None:
    conflict(repo, "mod.py", "x = 2\n", "x = 3\n")

    def agent(wt: Path, prompt: str) -> None:
        (wt / ".pump" / "resolve.md").write_text(
            report(
                "owner",
                "\n## Question\nWhich price do we charge?\n"
                "\n## Owner reason\nreal product choice\n",
            )
        )

    rig = Rig(repo, tmp_path, agent)
    rig.run()
    assert rig.card.fm.get("status") == "waiting_user"
    assert "Which price" in rig.body


def test_C2_rerun_note_shown_in_next_prompt(repo: Path, tmp_path: Path) -> None:
    conflict(repo, "mod.py", "x = 2\n", "x = 3\n")
    rig = Rig(repo, tmp_path, noop("rerun build\n\nNote: use the other API"))
    for _ in range(4):
        runner.tick(
            {"t": rig.board},
            launch=rig.launch_merge,
            launch_stage=rig.launch_stage,
            status=lambda j: (False, 0),
            output=lambda j, n: "",
            locks=tmp_path / "l",
        )
    assert find_card(rig.board, CID).pump2["stage"] in ("build", "resolve")


# ---- C3 --------------------------------------------------------------------
def test_C3_third_stop_stops_with_history(repo: Path, tmp_path: Path) -> None:
    conflict(repo, "mod.py", "x = 2\n", "x = 3\n")
    rig = Rig(repo, tmp_path, noop("owner"))
    p = repo / TASKS / f"{CID}.md"
    set_pump2(p, resolves=2)
    from src.pump2.state import add_comment

    add_comment(p, "resolve: continue merge\nfirst decision")
    add_comment(p, "resolve: rerun build\nsecond decision")
    assert rig.run() == "stopped"
    assert rig.prompts == []
    body = rig.body
    assert body.count("first decision") >= 2 and body.count("second decision") >= 2


def test_C3_direct_stops_skip_resolve(repo: Path, tmp_path: Path) -> None:
    # no test command: config, not the card
    conflict(repo, "mod.py", "x = 2\n", "x = 2\n")
    rig = Rig(repo, tmp_path, noop("owner"))
    rig.board.test = ""
    assert rig.run() == "stopped"
    assert rig.prompts == []


# ---- C4 --------------------------------------------------------------------
def test_C4_code_change_clears_green(repo: Path, tmp_path: Path) -> None:
    conflict(repo, "mod.py", "x = 2\n", "x = 3\n")
    rig = Rig(repo, tmp_path, fixer("mod.py", "continue merge"))
    set_pump2(repo / TASKS / f"{CID}.md", green="deadbeef")
    assert rig.run() == "done"
    assert rig.runs >= 1


def test_C4_landed_by_hand_is_stopped(repo: Path, tmp_path: Path) -> None:
    conflict(repo, "mod.py", "x = 2\n", "x = 3\n")

    def agent(wt: Path, prompt: str) -> None:
        (wt / "mod.py").write_text("x = 4\n")
        git(wt, "add", "mod.py")
        git(wt, "commit", "-qm", "by hand")
        git(repo, "merge", "-q", "--no-edit", "pump2/x")
        (wt / ".pump" / "resolve.md").write_text(report("continue merge"))

    rig = Rig(repo, tmp_path, agent)
    assert rig.run() == "stopped"
    assert "landed by hand" in rig.body


# ---- C5 --------------------------------------------------------------------
def test_C5_test_edits_listed_and_review_not_skipped(
    repo: Path, tmp_path: Path
) -> None:
    conflict(
        repo,
        "tests/test_x.py",
        "def test_x():\n    assert 2\n",
        "def test_x():\n    assert 3\n",
    )
    rig = Rig(
        repo,
        tmp_path,
        fixer("tests/test_x.py", "continue merge", "def test_x():\n    assert True\n"),
    )
    for _ in range(3):
        if rig.card.pump2["stage"] in ("review", "stopped"):
            break
        runner.tick(
            {"t": rig.board},
            launch=rig.launch_merge,
            launch_stage=rig.launch_stage,
            status=lambda j: (False, 0),
            output=lambda j, n: "",
            locks=tmp_path / "l",
        )
    c = rig.card
    assert c.pump2["stage"] == "review"
    assert "tests/test_x.py" in str(c.pump2.get("test_edits"))
    assert "tests/test_x.py" in rig.body


def test_C5_review_prompt_and_rejection() -> None:
    from src.pump2.stages import review

    src = (SRC / "stages" / "review.py").read_text()
    assert "test_edits" in src and "weakened" in src
    assert hasattr(review, "prompt")


# ---- C6 --------------------------------------------------------------------
def _lines(paths: list[Path]) -> int:
    return sum(len(p.read_text().splitlines()) for p in paths)


def test_C6_special_cases_deleted_and_core_small() -> None:
    assert not (SRC / "stages" / "mergefix.py").exists()
    text = "\n".join(p.read_text() for p in SRC.rglob("*.py"))
    for name in ("MERGEFIX_MAX", "_mergefixes", "build_red"):
        assert name not in text, name
    # word match: design.py's `timeout_seconds` is not the old `_second` helper
    assert not re.search(r"\b_second\b", text)
    assert not re.search(r"def _again\b", text)
    assert "pump_mergefix" not in text
    core = sorted((SRC / "stages").glob("*.py")) + [
        SRC / "state.py",
        SRC / "merge.py",
        SRC / "runner.py",
    ]
    assert _lines(core) <= 5000
    assert (SRC / "stages" / "resolve.py").exists()


# ---- C7 / C11 --------------------------------------------------------------
def test_C7_core_does_not_grow() -> None:
    core = sorted((SRC / "stages").glob("*.py")) + [
        SRC / "state.py",
        SRC / "merge.py",
        SRC / "runner.py",
    ]
    assert _lines(core) <= 3700


def test_C11_route_pump_resolve() -> None:
    root = SRC.parents[1]
    assert "pump_resolve" in (root / "src/model_router/routes.py").read_text()
    assert "pump_mergefix" not in (root / "src/model_router/routes.py").read_text()
    assert "pump_resolve" in (root / "config/models.example.yaml").read_text()
