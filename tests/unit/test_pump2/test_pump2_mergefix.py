"""dv-2c5c: when the mechanical merge does not land, the mergefix stage (a
model, here a fake agent) judges the facts; code checks override the model."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from src.pump2 import runner
from src.pump2.merge import merge
from src.pump2.state import Board, find_card, set_pump2

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
        self.mergefix_prompts: list[str] = []
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
        if stage != "mergefix":
            self.other_launches.append(stage)
            return "job-other"
        self.mergefix_prompts.append(prompt)
        ff = wt / ".pump" / "merge-facts.md"
        self.facts.append(ff.read_text() if ff.exists() else "")
        self.agent(wt, prompt)
        return f"job-mf{len(self.mergefix_prompts)}"

    def run(self, ticks: int = 12) -> str:
        for _ in range(ticks):
            c = find_card(self.board, CID)
            if c.pump2["stage"] in ("done", "stopped"):
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
        (wt / ".pump" / "mergefix.md").write_text(report(verdict, files))

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
def test_dirty_docs_commit_stray_lands_with_one_test_run(
    repo: Path, tmp_path: Path
) -> None:
    dirty_setup(repo)
    rig = Rig(repo, tmp_path, writes("commit-stray", ["docs/DECISIONS.md"]))
    assert rig.run() == "done"
    assert len(rig.mergefix_prompts) == 1
    assert "docs/DECISIONS.md" in rig.facts[0] and "stray edit" in rig.facts[0]
    assert rig.runs == 1
    assert rig.log_msgs().count(STRAY_MSG.format("docs/DECISIONS.md")) == 1
    final = (repo / "docs/DECISIONS.md").read_text()
    assert "card edit" in final and "stray edit" in final
    assert "mergefix: commit-stray" in rig.body
    assert (repo / ".pump-worktrees" / CID / ".pump" / "mergefix.md").exists()


# ---- (b) dirty code --------------------------------------------------------
@pytest.mark.parametrize("verdict", ["commit-stray", "land", "stop"])
def test_dirty_code_stops_naming_the_file(
    repo: Path, tmp_path: Path, verdict: str
) -> None:
    dirty_setup(repo, "mod.py")
    head = git(repo, "rev-parse", "main")
    rig = Rig(repo, tmp_path, writes(verdict, ["mod.py"]))
    assert rig.run() == "stopped"
    assert "mod.py" in rig.body
    assert git(repo, "rev-parse", "main") == head
    assert "stray edit" in (repo / "mod.py").read_text()


# ---- (c)/(d) integration branch moved after green --------------------------
def mover(repo: Path, tmp: Path, fname: str, text: str) -> str:
    """A test command that moves main (once) while the tests run."""
    script = tmp / "t.sh"
    script.write_text(
        f"echo x >> {tmp / 'runs'}\n"
        f"if [ ! -f {tmp / 'flag'} ]; then touch {tmp / 'flag'}\n"
        f"  printf '%s' '{text}' > {repo / fname}\n"
        f"  git -C {repo} add {fname}\n"
        f"  git -C {repo} -c user.name=t -c user.email=t@t commit -qm moved\n"
        "fi\n"
    )
    return f"bash {script}"


def test_main_moved_by_docs_lands_without_retest(repo: Path, tmp_path: Path) -> None:
    card_branch(repo, "b.txt", "b\n")
    test = mover(repo, tmp_path, "docs/c.md", "two\n")
    rig = Rig(repo, tmp_path, writes("land"), test)
    assert rig.run() == "done"
    assert rig.runs == 1 and len(rig.mergefix_prompts) == 1
    assert (repo / "docs/c.md").read_text() == "two\n"
    assert (repo / "b.txt").exists()


def test_main_moved_by_deviz_card_lands_without_retest(
    repo: Path, tmp_path: Path
) -> None:
    card_branch(repo, "b.txt", "b\n")
    test = mover(repo, tmp_path, ".deviz/tasks/dv-0009.md", "card\n")
    rig = Rig(repo, tmp_path, writes("land"), test)
    assert rig.run() == "done" and rig.runs == 1


@pytest.mark.parametrize("verdict", ["retest", "land"])
def test_main_moved_by_code_retests_once(
    repo: Path, tmp_path: Path, verdict: str
) -> None:
    card_branch(repo, "b.txt", "b\n")
    test = mover(repo, tmp_path, "new.py", "y = 2\n")
    rig = Rig(repo, tmp_path, writes(verdict), test)
    assert rig.run() == "done"
    assert rig.runs == 2 and len(rig.mergefix_prompts) == 1
    assert (repo / "new.py").exists() and (repo / "b.txt").exists()


# ---- (e) conflicts ---------------------------------------------------------
def conflict(repo: Path, fname: str, card_text: str, main_text: str) -> None:
    card_branch(repo, fname, card_text)
    commit_file(repo, fname, main_text, "main moves")


def resolver(fname: str, verdict: str, text: str = "resolved\n") -> Agent:
    def agent(wt: Path, prompt: str) -> None:
        (wt / fname).write_text(text)
        (wt / ".pump" / "mergefix.md").write_text(report(verdict))

    return agent


def test_markdown_conflict_resolved_and_landed(repo: Path, tmp_path: Path) -> None:
    conflict(repo, "docs/c.md", "card\n", "main\n")
    rig = Rig(repo, tmp_path, resolver("docs/c.md", "land"))
    assert rig.run() == "done"
    assert rig.runs <= 1
    assert (repo / "docs/c.md").read_text() == "resolved\n"
    assert "<<<<<<<" not in (repo / "docs/c.md").read_text()
    assert "docs/c.md" in rig.facts[0]


def test_code_conflict_resolved_then_one_test_run(repo: Path, tmp_path: Path) -> None:
    conflict(repo, "mod.py", "x = 2\n", "x = 3\n")
    rig = Rig(repo, tmp_path, resolver("mod.py", "retest", "x = 4\n"))
    assert rig.run() == "done"
    assert rig.runs == 1 and (repo / "mod.py").read_text() == "x = 4\n"


def test_code_conflict_land_verdict_is_overridden_to_retest(
    repo: Path, tmp_path: Path
) -> None:
    conflict(repo, "mod.py", "x = 2\n", "x = 3\n")
    rig = Rig(repo, tmp_path, resolver("mod.py", "land", "x = 4\n"))
    assert rig.run() == "done" and rig.runs == 1


def test_code_conflict_red_after_a_red_stops(repo: Path, tmp_path: Path) -> None:
    conflict(repo, "mod.py", "x = 2\n", "x = 3\n")
    rig = Rig(repo, tmp_path, resolver("mod.py", "retest", "x = 4\n"), "exit 1")
    set_pump2(repo / TASKS / f"{CID}.md", attempt=1)
    assert rig.run() == "stopped"
    assert len(rig.mergefix_prompts) == 1


# ---- (f) bad outcomes ------------------------------------------------------
def test_conflict_markers_left_behind_stop(repo: Path, tmp_path: Path) -> None:
    conflict(repo, "docs/c.md", "card\n", "main\n")

    def agent(wt: Path, prompt: str) -> None:
        (wt / ".pump" / "mergefix.md").write_text(report("land"))

    rig = Rig(repo, tmp_path, agent)
    assert rig.run() == "stopped"
    assert len(rig.mergefix_prompts) == 1
    assert "docs/c.md" in rig.body or "marker" in rig.body.lower()
    wt = repo / ".pump-worktrees" / CID  # the merge in progress was ended
    assert not git(wt, "ls-files", "--unmerged").strip()
    assert not (wt / ".git").is_dir() and "MERGE_HEAD" not in git(
        wt, "status", "--porcelain=v2", "--branch"
    )


@pytest.mark.parametrize("content", [None, "no sections at all", "## Verdict\nmaybe\n"])
def test_missing_or_unparseable_report_stops(
    repo: Path, tmp_path: Path, content: str | None
) -> None:
    dirty_setup(repo)

    def agent(wt: Path, prompt: str) -> None:
        if content is not None:
            (wt / ".pump" / "mergefix.md").write_text(content)

    rig = Rig(repo, tmp_path, agent)
    assert rig.run() == "stopped"
    assert "mergefix" in rig.body


def test_stop_verdict_carries_the_reason(repo: Path, tmp_path: Path) -> None:
    dirty_setup(repo)
    rig = Rig(repo, tmp_path, writes("stop"))
    assert rig.run() == "stopped"
    assert "because of facts" in rig.body


# ---- (g) one mergefix per merge attempt -----------------------------------
def test_second_failure_after_mergefix_stops_loud(repo: Path, tmp_path: Path) -> None:
    dirty_setup(repo)
    # the agent says land but leaves the live file dirty: the ff fails again
    rig = Rig(repo, tmp_path, writes("land"))
    assert rig.run() == "stopped"
    assert len(rig.mergefix_prompts) == 1
    assert "docs/DECISIONS.md" in rig.body


# ---- (2) mechanical happy path --------------------------------------------
def test_mechanical_merge_never_launches_a_stage(repo: Path, tmp_path: Path) -> None:
    card_branch(repo, "b.txt", "b\n")

    def agent(wt: Path, prompt: str) -> None:
        pytest.fail("model called on a clean merge")

    rig = Rig(repo, tmp_path, agent)
    assert rig.run() == "done"
    assert rig.mergefix_prompts == [] and rig.other_launches == []
    assert rig.runs == 1


# ---- (3) route -------------------------------------------------------------
def test_mergefix_has_a_route_row() -> None:
    from src.model_router.routes import load_call_routes

    assert load_call_routes(None).resolve_job("pump_mergefix").model == "opus"
    assert runner.STAGE_ROUTE["mergefix"] == "pump_mergefix"
