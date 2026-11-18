"""dv-b8bb (was dv-2c5c's mergefix): when the mechanical merge does not land,
the resolve stage (the board's agent, here a fake) gets the facts; code checks
override it. A moved integration branch is re-merged without a model."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from src.pump2 import runner
from src.pump2.merge import MOVED_MAX, merge
from src.pump2.state import Board, find_card, set_pump2, worktree_path

TASKS = Path(".deviz", "tasks")
CID = "dv-0001"
STRAY_MSG = "pump2: commit stray uncommitted edit ({}) left by another session"
Agent = Callable[[Path, str], None]


def git(repo: Path, *a: str) -> str:
    r = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *a],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return r.stdout.strip()


def report(verdict: str, files: list[str] | None = None) -> str:
    out = f"## Verdict\n{verdict}\n\n## Reasoning\nbecause of facts\n"
    if verdict == "owner":
        out += "\n## Question\nwhich?\n\n## Owner reason\nreal product choice\n"
    if files:
        out += "\n## Stray files\n" + "".join(f"- {f}\n" for f in files)
    return out


def commit_file(repo: Path, name: str, text: str, msg: str = "c") -> None:
    f = repo / name
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text)
    git(repo, "add", name)
    git(repo, "commit", "-qm", msg)


def lines(n: int = 20) -> str:
    return "".join(f"line {i}\n" for i in range(1, n + 1))


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    (r / "a.txt").write_text("a\n")
    (r / "docs").mkdir()
    (r / "docs/DECISIONS.md").write_text(lines())
    (r / "docs/c.md").write_text("one\n")
    (r / "mod.py").write_text("x = 1\n")
    (r / TASKS).mkdir(parents=True)
    (r / TASKS / f"{CID}.md").write_text(
        f"---\nid: {CID}\ntitle: t\nstatus: in_progress\ndeps: []\n---\n\n"
        "## Description\n\nd\n\n## Comments\n"
    )
    git(r, "add", "-A")
    git(r, "commit", "-qm", "init")
    return r


def card_branch(repo: Path, fname: str, text: str) -> None:
    git(repo, "checkout", "-q", "-b", "pump2/x")
    commit_file(repo, fname, text, "card")
    git(repo, "checkout", "-q", "main")


class Rig:
    def __init__(self, repo: Path, tmp: Path, agent: Agent, test: str = "") -> None:
        self.repo, self.tmp, self.agent = repo, tmp, agent
        self.counter = tmp / "runs"
        self.board = Board(
            name="t", repo=repo, test=test or f"echo x >> {self.counter}"
        )
        self.prompts: list[str] = []
        self.other_launches: list[str] = []
        self.facts: list[str] = []
        set_pump2(repo / TASKS / f"{CID}.md", stage="merge", branch="pump2/x")

    @property
    def runs(self) -> int:
        return self.counter.read_text().count("x") if self.counter.exists() else 0

    def launch_merge(self, bd, c, f) -> str:  # type: ignore[no-untyped-def]
        merge(bd, find_card(bd, c.id), locks=self.tmp / "l")
        return "job-merge"

    def launch_stage(self, bd, c, stage, wt, prompt) -> str:  # type: ignore[no-untyped-def]
        if stage != "resolve":
            self.other_launches.append(stage)
            return "job-other"
        self.prompts.append(prompt)
        ff = wt / ".pump" / "merge-facts.md"
        self.facts.append(ff.read_text() if ff.exists() else "")
        self.agent(wt, prompt)
        return f"job-r{len(self.prompts)}"

    def run(self, ticks: int = 12) -> str:
        for _ in range(ticks):
            c = find_card(self.board, CID)
            if (
                c.pump2["stage"] in ("done", "stopped")
                or c.fm["status"] != "in_progress"
            ):
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
    def body(self) -> str:
        return find_card(self.board, CID).body

    def log_msgs(self) -> str:
        return git(self.repo, "log", "--format=%s", "main")


def writes(verdict: str, files: list[str] | None = None) -> Agent:
    def agent(wt: Path, prompt: str) -> None:
        (wt / ".pump" / "resolve.md").write_text(report(verdict, files))

    return agent


def dirty_setup(repo: Path, path: str = "docs/DECISIONS.md") -> None:
    """Card edits line 1 of ``path``; another session left line 20 edited."""
    text = (repo / path).read_text() if (repo / path).exists() else lines()
    ls = text.splitlines(keepends=True)
    card = list(ls)
    card[0] = "card edit\n"
    card_branch(repo, path, "".join(card))
    dirty = list(ls)
    dirty[-1] = "stray edit\n"
    (repo / path).write_text("".join(dirty))


# ---- (a) dirty docs --------------------------------------------------------
def test_dirty_docs_stray_lands_with_one_test_run(repo: Path, tmp_path: Path) -> None:
    dirty_setup(repo)
    rig = Rig(repo, tmp_path, writes("continue merge", ["docs/DECISIONS.md"]))
    assert rig.run() == "done"
    assert len(rig.prompts) == 1
    assert "docs/DECISIONS.md" in rig.facts[0] and "stray edit" in rig.facts[0]
    assert rig.runs == 1
    assert rig.log_msgs().count(STRAY_MSG.format("docs/DECISIONS.md")) == 1
    final = (repo / "docs/DECISIONS.md").read_text()
    assert "card edit" in final and "stray edit" in final
    assert "resolve: continue merge" in rig.body


# ---- (b) dirty code --------------------------------------------------------
def test_dirty_code_stops_naming_the_file(repo: Path, tmp_path: Path) -> None:
    dirty_setup(repo, "mod.py")
    head = git(repo, "rev-parse", "main")
    rig = Rig(repo, tmp_path, writes("continue merge", ["mod.py"]))
    assert rig.run() == "stopped"
    assert "uncommitted code" in rig.body and "mod.py" in rig.body
    assert git(repo, "rev-parse", "main") == head
    assert "stray edit" in (repo / "mod.py").read_text()


# ---- (c)/(d) integration branch moved after green: no model ----------------
def mover(repo: Path, tmp: Path, fname: str, text: str, times: int = 1) -> str:
    """A test command that moves main (``times`` times) while the tests run."""
    script = tmp / "t.sh"
    script.write_text(
        f"echo x >> {tmp / 'runs'}\n"
        f"if [ $(cat {tmp / 'flag'} 2>/dev/null | wc -c) -lt {times} ]; then\n"
        f"  printf x >> {tmp / 'flag'}\n"
        f"  printf '%s' \"$(cat {tmp / 'flag'}){text}\" > {repo / fname}\n"
        f"  git -C {repo} add {fname}\n"
        f"  git -C {repo} -c user.name=t -c user.email=t@t commit -qm moved\n"
        "fi\n"
    )
    return f"bash {script}"


def no_model(wt: Path, prompt: str) -> None:
    pytest.fail("resolve called on a mechanical case")


def test_main_moved_by_docs_lands_without_retest(repo: Path, tmp_path: Path) -> None:
    card_branch(repo, "b.txt", "b\n")
    rig = Rig(repo, tmp_path, no_model, mover(repo, tmp_path, "docs/c.md", "two\n"))
    assert rig.run() == "done"
    assert rig.runs == 1 and rig.prompts == []
    assert (repo / "docs/c.md").read_text() == "xtwo\n"
    assert (repo / "b.txt").exists() and "non-code: still green" in rig.body


def test_main_moved_by_deviz_card_lands_without_retest(
    repo: Path, tmp_path: Path
) -> None:
    card_branch(repo, "b.txt", "b\n")
    test = mover(repo, tmp_path, ".deviz/tasks/dv-0009.md", "card\n")
    rig = Rig(repo, tmp_path, no_model, test)
    assert rig.run() == "done" and rig.runs == 1


def test_main_moved_by_code_retests_once(repo: Path, tmp_path: Path) -> None:
    card_branch(repo, "b.txt", "b\n")
    rig = Rig(repo, tmp_path, no_model, mover(repo, tmp_path, "new.py", "y = 2\n"))
    assert rig.run() == "done"
    assert rig.runs == 2 and rig.prompts == []
    assert (repo / "new.py").exists() and (repo / "b.txt").exists()


def test_main_moving_every_time_goes_to_resolve(repo: Path, tmp_path: Path) -> None:
    card_branch(repo, "b.txt", "b\n")
    test = mover(repo, tmp_path, "new.py", "y = 2\n", times=9)
    rig = Rig(repo, tmp_path, writes("owner"), test)
    rig.run()
    assert len(rig.prompts) == 1 and f"({MOVED_MAX} re-merges)" in rig.body


# ---- (e) conflicts ---------------------------------------------------------
def conflict(repo: Path, fname: str, card_text: str, main_text: str) -> None:
    card_branch(repo, fname, card_text)
    commit_file(repo, fname, main_text, "main moves")


def resolver(fname: str, verdict: str, text: str = "resolved\n") -> Agent:
    def agent(wt: Path, prompt: str) -> None:
        (wt / fname).write_text(text)
        (wt / ".pump" / "resolve.md").write_text(report(verdict))

    return agent


def test_markdown_conflict_resolved_and_landed(repo: Path, tmp_path: Path) -> None:
    conflict(repo, "docs/c.md", "card\n", "main\n")
    rig = Rig(repo, tmp_path, resolver("docs/c.md", "continue merge"))
    assert rig.run() == "done"
    assert rig.runs <= 1
    assert (repo / "docs/c.md").read_text() == "resolved\n"
    assert "docs/c.md" in rig.facts[0]


def test_code_conflict_resolved_then_one_test_run(repo: Path, tmp_path: Path) -> None:
    conflict(repo, "mod.py", "x = 2\n", "x = 3\n")
    rig = Rig(repo, tmp_path, resolver("mod.py", "continue merge", "x = 4\n"))
    assert rig.run() == "done"
    assert rig.runs == 1 and (repo / "mod.py").read_text() == "x = 4\n"


def test_red_after_two_resolves_stops_with_history(repo: Path, tmp_path: Path) -> None:
    conflict(repo, "mod.py", "x = 2\n", "x = 3\n")
    rig = Rig(repo, tmp_path, resolver("mod.py", "continue merge", "x = 4\n"), "exit 1")
    assert rig.run() == "stopped"
    assert len(rig.prompts) == 2
    assert "resolve history:" in rig.body and "> resolve: continue merge" in rig.body


# ---- (f) bad outcomes ------------------------------------------------------
def test_conflict_markers_left_behind_stop(repo: Path, tmp_path: Path) -> None:
    conflict(repo, "docs/c.md", "card\n", "main\n")

    def agent(wt: Path, prompt: str) -> None:
        (wt / ".pump" / "resolve.md").write_text(report("continue merge"))

    rig = Rig(repo, tmp_path, agent)
    assert rig.run() == "stopped"
    assert len(rig.prompts) == 1
    assert "docs/c.md" in rig.body and "marker" in rig.body.lower()
    wt = repo / ".pump-worktrees" / CID  # the merge in progress was ended
    assert not git(wt, "ls-files", "--unmerged").strip()


@pytest.mark.parametrize("content", [None, "no sections at all", "## Verdict\nmaybe\n"])
def test_missing_or_unparseable_report_stops(
    repo: Path, tmp_path: Path, content: str | None
) -> None:
    dirty_setup(repo)

    def agent(wt: Path, prompt: str) -> None:
        if content is not None:
            (wt / ".pump" / "resolve.md").write_text(content)

    rig = Rig(repo, tmp_path, agent)
    assert rig.run() == "stopped"
    assert "resolve: " in rig.body


def test_owner_verdict_carries_the_reason(repo: Path, tmp_path: Path) -> None:
    dirty_setup(repo)
    rig = Rig(repo, tmp_path, writes("owner"))
    rig.run()
    c = find_card(rig.board, CID)
    assert c.fm["status"] == "waiting_user" and c.pump2["stage"] == "resolve"
    assert "because of facts" in rig.body and "which?" in rig.body


def test_failure_again_after_resolve_resolves_again(repo: Path, tmp_path: Path) -> None:
    dirty_setup(repo)
    # the agent says continue but leaves the live file dirty: the ff fails again
    rig = Rig(repo, tmp_path, writes("continue merge"))
    assert rig.run() == "stopped"
    assert len(rig.prompts) == 2
    assert "docs/DECISIONS.md" in rig.body


def test_resolve_job_failing_twice_ends_the_merge(repo: Path, tmp_path: Path) -> None:
    conflict(repo, "mod.py", "x = 2\n", "x = 3\n")
    rig = Rig(repo, tmp_path, resolver("mod.py", "continue merge", "x = 4\n"))
    for _ in range(8):
        if find_card(rig.board, CID).pump2["stage"] in ("done", "stopped"):
            break
        runner.tick(
            {"t": rig.board},
            launch=rig.launch_merge,
            launch_stage=rig.launch_stage,
            status=lambda j: (False, 1 if j.startswith("job-r") else 0),
            output=lambda j, n: "boom",
            locks=tmp_path / "l",
        )
    card = find_card(rig.board, CID)
    assert card.pump2["stage"] == "stopped" and card.pump2["stopped_at"] == "merge"
    assert len(rig.prompts) == 2  # the job ran, failed, reran once
    wt = worktree_path(rig.board, CID)
    assert not git(wt, "ls-files", "--unmerged").strip()


def test_mergefix_card_in_flight_moves_to_resolve(repo: Path, tmp_path: Path) -> None:
    """C10: a card left at stage mergefix by the old code goes to resolve."""
    conflict(repo, "mod.py", "x = 2\n", "x = 3\n")
    rig = Rig(repo, tmp_path, resolver("mod.py", "continue merge", "x = 4\n"))
    merge(rig.board, find_card(rig.board, CID), locks=tmp_path / "l")
    set_pump2(repo / TASKS / f"{CID}.md", stage="mergefix", resolves=None, job=None)
    assert rig.run() == "done" and len(rig.prompts) == 1


# ---- (2) mechanical happy path --------------------------------------------
def test_mechanical_merge_never_launches_a_stage(repo: Path, tmp_path: Path) -> None:
    card_branch(repo, "b.txt", "b\n")
    rig = Rig(repo, tmp_path, no_model)
    assert rig.run() == "done"
    assert rig.prompts == [] and rig.other_launches == []
    assert rig.runs == 1


# ---- (3) route -------------------------------------------------------------
def test_resolve_has_a_route_row() -> None:
    from src.model_router.routes import load_call_routes

    assert load_call_routes(None).resolve_job("pump_resolve").model == "opus"
    assert runner.STAGE_ROUTE["resolve"] == "pump_resolve"


# ---- (4) test edits: staged ones count, clean incoming ones do not ---------
def tick_until(rig: Rig, stage: str, ticks: int = 8) -> None:
    for _ in range(ticks):
        if find_card(rig.board, CID).pump2["stage"] in (stage, "stopped"):
            return
        runner.tick(
            {"t": rig.board},
            launch=rig.launch_merge,
            launch_stage=rig.launch_stage,
            status=lambda j: (False, 0),
            output=lambda j, n: "",
            locks=rig.tmp / "l",
        )


def test_staged_test_edit_is_listed_and_goes_to_review(
    repo: Path, tmp_path: Path
) -> None:
    commit_file(repo, "tests/test_x.py", "assert 1\n")
    conflict(repo, "mod.py", "x = 2\n", "x = 3\n")
    commit_file(repo, "tests/test_y.py", "assert 2\n", "main adds a test")

    def agent(wt: Path, prompt: str) -> None:
        (wt / "mod.py").write_text("x = 4\n")
        (wt / "tests/test_x.py").write_text("assert True\n")
        git(wt, "add", "tests/test_x.py")  # staged by the agent: still counted
        (wt / ".pump" / "resolve.md").write_text(report("continue merge"))

    rig = Rig(repo, tmp_path, agent)
    tick_until(rig, "review")
    card = find_card(rig.board, CID)
    assert card.pump2["stage"] == "review"
    assert card.pump2["test_edits"] == ["tests/test_x.py"]
    assert "rerouted to review" in rig.body


def test_rerun_discards_test_edits(repo: Path, tmp_path: Path) -> None:
    dirty_setup(repo)

    def agent(wt: Path, prompt: str) -> None:
        (wt / "tests").mkdir(exist_ok=True)
        (wt / "tests/test_new.py").write_text("assert 0\n")
        git(wt, "add", "tests/test_new.py")
        (wt / "keep.py").write_text("k = 1\n")
        (wt / ".pump" / "resolve.md").write_text(report("rerun build"))

    rig = Rig(repo, tmp_path, agent)
    tick_until(rig, "build")
    assert find_card(rig.board, CID).pump2["stage"] == "build"
    wt = worktree_path(rig.board, CID)
    assert not (wt / "tests/test_new.py").exists()
    assert (wt / "keep.py").exists()


def test_committed_test_edit_is_still_listed(repo: Path, tmp_path: Path) -> None:
    # review return 2026-11-12: an agent that runs `git commit` itself moved
    # HEAD past its test edit; the diff is taken from the HEAD at the stop
    commit_file(repo, "tests/test_x.py", "assert 1\n")
    conflict(repo, "mod.py", "x = 2\n", "x = 3\n")
    commit_file(repo, "tests/test_y.py", "assert 2\n", "main adds a test")

    def agent(wt: Path, prompt: str) -> None:
        (wt / "mod.py").write_text("x = 4\n")
        (wt / "tests/test_x.py").write_text("assert True\n")
        git(wt, "add", "-A")
        git(wt, "commit", "-qm", "agent commits the merge itself")
        (wt / ".pump" / "resolve.md").write_text(report("continue merge"))

    rig = Rig(repo, tmp_path, agent)
    tick_until(rig, "review")
    card = find_card(rig.board, CID)
    assert card.pump2["stage"] == "review"
    # a self-committed merge hides which side a path came from: the incoming
    # test is listed too (review names it; errs safe, the agent broke a rule)
    assert "tests/test_x.py" in card.pump2["test_edits"]
    assert card.pump2.get("green") is None


# ---- C8: build RED on a wrong test -> resolve -> review -> merge ------------
def test_wrong_test_build_red_resolved_reviewed_and_landed(
    tmp_path: Path,
) -> None:
    import sys

    from .test_pump2 import TASKS as T2
    from .test_pump2 import Fake, plan
    from .test_pump2_step3 import put, setup, write_tests_json

    r = tmp_path / "r"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    (r / T2).mkdir(parents=True)
    (r / T2 / "dv-0001.md").write_text(
        "---\nid: dv-0001\ntitle: t\nstatus: in_progress\ndeps: []\n---\n\n"
        "## Description\n\nd\n\n## Comments\n"
    )
    (r / ".gitignore").write_text(".pump-worktrees/\n__pycache__/\n")
    git(r, "add", "-A")
    git(r, "commit", "-qm", "init")
    b = Board(name="t", repo=r, test="true", python=sys.executable)
    f = Fake()
    wt = setup(r, tmp_path, "tests", b, f)
    wrong = (
        "from pathlib import Path\n\ndef test_v():\n"
        "    assert (Path(__file__).parents[1] / 'v.txt').read_text() == '2'\n"
    )
    put(wt, "tests/test_v.py", wrong)
    write_tests_json(wt, ["tests/test_v.py"])
    f.tick(b, tmp_path)  # tests -> build
    f.tick(b, tmp_path)  # build launched
    put(wt, "v.txt", "1")  # the code is right, the test is wrong
    f.tick(b, tmp_path)
    st = find_card(b, "dv-0001").pump2
    assert st["stage"] == "resolve" and st["resolve_from"] == "build"
    f.tick(b, tmp_path)  # resolve launched
    assert f.launched[-1][1] == "resolve"
    put(wt, "tests/test_v.py", wrong.replace("'2'", "'1'"))
    put(wt, ".pump/resolve.md", report("continue merge"))
    f.tick(b, tmp_path)
    st = find_card(b, "dv-0001").pump2
    assert st["stage"] == "review", find_card(b, "dv-0001").body
    assert st["test_edits"] == ["tests/test_v.py"]
    f.tick(b, tmp_path)  # review launched
    assert f.launched[-1][1] == "review" and "weakened" in f.launched[-1][3]
    plan(r)  # review reads the plan
    put(
        wt,
        ".pump/review.md",
        "## Verdict\npass\n\n## Remarks\n"
        "tests/test_v.py: expected value fixed to the spec, not weakened\n",
    )
    f.tick(b, tmp_path)
    assert find_card(b, "dv-0001").pump2["stage"] == "merge"

    def launch(bd, c, fl):  # type: ignore[no-untyped-def]
        merge(bd, find_card(bd, c.id), locks=tmp_path / "l")
        return "job-merge"

    for _ in range(3):
        if find_card(b, "dv-0001").pump2["stage"] == "done":
            break
        runner.tick(
            {"t": b},
            launch=launch,
            launch_stage=f.launch,
            status=lambda j: (False, 0),
            output=lambda j, n: "",
            locks=tmp_path / "l",
        )
    assert find_card(b, "dv-0001").pump2["stage"] == "done"
    assert (r / "tests/test_v.py").read_text() == wrong.replace("'2'", "'1'")
