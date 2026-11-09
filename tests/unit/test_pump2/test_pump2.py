"""Pump v2 core (docs/pump-v2.md): state, hold, runner, merge, slots."""

import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from src.pump2 import render, runner, slots
from src.pump2.merge import dirty_overlap, merge
from src.pump2.state import (
    Board,
    add_comment,
    find_card,
    list_cards,
    load_boards,
    set_pump2,
)

TASKS = Path(".deviz", "tasks")
CARD = """---
id: {id}
title: t
status: {status}
deps: {deps}
start_at: {start}
---

## Description

keep me

## Comments
- old comment
"""


def git(repo: Path, *a: str) -> str:
    r = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *a],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return r.stdout.strip()


def card(
    repo: Path, cid: str, status: str = "in_progress", deps: str = "[]", start: str = ""
) -> Path:
    p = repo / TASKS / f"{cid}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(CARD.format(id=cid, status=status, deps=deps, start=start))
    return p


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    (r / "a.txt").write_text("a\n")
    card(r, "dv-0001")
    git(r, "add", "-A")
    git(r, "commit", "-qm", "init")
    return r


def board(repo: Path, test: str) -> Board:
    return Board(name="t", repo=repo, test=test)


def branch_with(repo: Path, name: str, fname: str, text: str) -> None:
    git(repo, "checkout", "-q", "-b", name)
    (repo / fname).write_text(text)
    git(repo, "add", fname)
    git(repo, "commit", "-qm", name)
    git(repo, "checkout", "-q", "main")


def test_set_pump2_touches_only_its_block(repo: Path) -> None:
    p = repo / TASKS / "dv-0001.md"
    before = p.read_text()
    set_pump2(p, stage="merge", branch="b1")
    set_pump2(p, job="j1", stage="merge")
    c = find_card(board(repo, "true"), "dv-0001")
    assert c.pump2["stage"] == "merge" and c.pump2["job"] == "j1"
    assert c.pump2["since"]
    set_pump2(p, job=None)
    assert "job" not in find_card(board(repo, "true"), "dv-0001").pump2
    stripped = p.read_text().replace(
        "\n".join(
            ln for ln in p.read_text().split("\n") if ln.startswith(("pump2:", "  "))
        ),
        "",
    )
    assert "keep me" in stripped and before.split("---")[2] in p.read_text()


def test_comment_is_appended(repo: Path) -> None:
    p = repo / TASKS / "dv-0001.md"
    before = p.read_text()
    add_comment(p, "hello")
    after = p.read_text()
    assert after.startswith(before) and "### @pump2 — " in after
    assert after.rstrip().endswith("hello")


def test_load_boards_pump2_keys(tmp_path: Path) -> None:
    f = tmp_path / "b.json"
    f.write_text(
        '{"boards":[{"name":"x","repo":"/r","test":"old","pump2_test":"new",'
        '"pump2_test_core":"core"}]}'
    )
    b = load_boards(f)["x"]
    assert (b.test, b.test_core, b.branch) == ("new", "core", "main")


def test_hold_on_deps_and_start_at(repo: Path) -> None:
    b = board(repo, "true")
    from src.pump2.state import hold_reason

    card(repo, "dv-0002", deps="[dv-0001]")
    assert "dv-0001(in_progress)" in hold_reason(find_card(b, "dv-0002"), b)
    card(repo, "dv-0001", status="done")
    assert hold_reason(find_card(b, "dv-0002"), b) == ""
    card(repo, "dv-0003", start="2099-01-01T00:00:00Z")
    now = datetime(2026, 1, 1, tzinfo=UTC)
    assert "start_at" in hold_reason(find_card(b, "dv-0003"), b, now)
    assert (
        hold_reason(find_card(b, "dv-0003"), b, datetime(2100, 1, 1, tzinfo=UTC)) == ""
    )


def test_merge_green_fast_forwards(repo: Path, tmp_path: Path) -> None:
    branch_with(repo, "pump2/x", "b.txt", "b\n")
    p = repo / TASKS / "dv-0001.md"
    set_pump2(p, stage="merge", branch="pump2/x")
    b = board(repo, "test -f b.txt")
    res = merge(b, find_card(b, "dv-0001"), locks=tmp_path / "l")
    assert res.outcome == "merged"
    assert git(repo, "rev-parse", "HEAD") == res.detail
    assert (repo / "b.txt").exists()
    c = find_card(b, "dv-0001")
    assert c.pump2["stage"] == "done" and c.fm["status"] == "done"
    assert f"merged {res.detail}" in c.body
    assert (repo / ".pump-worktrees/dv-0001/.pump/merge.log").exists()
    assert ".pump-worktrees/" in (repo / ".gitignore").read_text()


def test_merge_green_but_dirty_live_checkout_goes_to_mergefix(
    repo: Path, tmp_path: Path
) -> None:
    """Tests green, but the live checkout holds someone's uncommitted edit the
    fast-forward would overwrite (gb-1ac7, gb-1ae0 05.11). dv-2c5c: no longer
    held; the card goes to the mergefix stage, the file is never touched."""
    branch_with(repo, "pump2/x", "b.txt", "b\n")
    (repo / "b.txt").write_text("someone's uncommitted work\n")
    p = repo / TASKS / "dv-0001.md"
    set_pump2(p, stage="merge", branch="pump2/x")
    runs = tmp_path / "runs"
    b = board(repo, f"echo x >> {runs}")
    res = merge(b, find_card(b, "dv-0001"), locks=tmp_path / "l")
    assert res.outcome == "mergefix"
    c = find_card(b, "dv-0001")
    assert c.pump2["stage"] == "mergefix"
    assert (repo / "b.txt").read_text() == "someone's uncommitted work\n"
    assert dirty_overlap(b, "pump2/x") == ["b.txt"]
    assert runs.read_text().count("x") == 1
    facts = (repo / ".pump-worktrees/dv-0001/.pump/merge-facts.md").read_text()
    assert "b.txt" in facts


def test_merge_green_result_is_dropped_when_main_moves(
    repo: Path, tmp_path: Path
) -> None:
    """A green result is reused only while the branch tip still holds the
    current integration branch: main moved meanwhile = full merge + tests."""
    branch_with(repo, "pump2/x", "b.txt", "b\n")
    p = repo / TASKS / "dv-0001.md"
    set_pump2(
        p, stage="merge", branch="pump2/x", green=git(repo, "rev-parse", "pump2/x")
    )
    (repo / "c.txt").write_text("c\n")
    git(repo, "add", "c.txt")
    git(repo, "commit", "-qm", "main moves")
    runs = tmp_path / "runs"
    b = board(repo, f"echo x >> {runs}")
    res = merge(b, find_card(b, "dv-0001"), locks=tmp_path / "l")
    assert res.outcome == "merged"
    assert runs.read_text().count("x") == 1


def test_merge_dirty_file_the_branch_does_not_touch_merges(
    repo: Path, tmp_path: Path
) -> None:
    branch_with(repo, "pump2/x", "b.txt", "b\n")
    (repo / "notes.txt").write_text("untracked, not in the branch\n")
    set_pump2(repo / TASKS / "dv-0001.md", stage="merge", branch="pump2/x")
    b = board(repo, "true")
    assert merge(b, find_card(b, "dv-0001"), locks=tmp_path / "l").outcome == "merged"
    assert (repo / "notes.txt").exists()


def test_merge_red_goes_to_build_then_stops(repo: Path, tmp_path: Path) -> None:
    branch_with(repo, "pump2/x", "b.txt", "b\n")
    p = repo / TASKS / "dv-0001.md"
    set_pump2(p, stage="merge", branch="pump2/x")
    head = git(repo, "rev-parse", "main")
    b = board(repo, "echo 'FAILED tests/x.py::test_boom - assert'; exit 1")
    res = merge(b, find_card(b, "dv-0001"), locks=tmp_path / "l")
    assert res.outcome == "red"
    c = find_card(b, "dv-0001")
    assert c.pump2["stage"] == "build" and c.pump2["attempt"] == 1
    assert "tests/x.py::test_boom" in c.body and "exit code: 1" in c.body
    assert git(repo, "rev-parse", "main") == head
    set_pump2(p, stage="merge")
    merge(b, find_card(b, "dv-0001"), locks=tmp_path / "l")
    assert find_card(b, "dv-0001").pump2["stage"] == "stopped"


def test_merge_conflict_stops_loud(repo: Path, tmp_path: Path) -> None:
    branch_with(repo, "pump2/x", "a.txt", "branch\n")
    (repo / "a.txt").write_text("main\n")
    git(repo, "commit", "-qam", "main moves")
    set_pump2(repo / TASKS / "dv-0001.md", stage="merge", branch="pump2/x")
    b = board(repo, "true")
    res = merge(b, find_card(b, "dv-0001"), locks=tmp_path / "l")
    # dv-2c5c: a CONFLICT no longer stops the card; it hands off to mergefix.
    assert res.outcome == "mergefix"
    c = find_card(b, "dv-0001")
    assert c.pump2["stage"] == "mergefix" and "CONFLICT" in c.body
    assert "merge-facts.md" in c.body  # the git output now lives in the facts file


def test_slot_limits(tmp_path: Path) -> None:
    d = tmp_path / "l"
    with slots.heavy_slot("a", d) as a:
        assert a
        with slots.heavy_slot("a", d) as again:
            assert not again  # 1 per board
        with slots.heavy_slot("b", d) as b, slots.heavy_slot("c", d) as c:
            assert b and c
            with slots.heavy_slot("d", d) as dd:
                assert not dd  # 3 per host
    assert slots.free("d", d)


def test_runner_launches_merge_and_reaps(repo: Path, tmp_path: Path) -> None:
    b = board(repo, "true")
    p = repo / TASKS / "dv-0001.md"
    set_pump2(p, stage="merge", branch="pump2/x")
    launched: list[str] = []

    def launch(bd: Board, c, f) -> str:  # type: ignore[no-untyped-def]
        launched.append(c.id)
        return "job-1"

    kw = dict(launch=launch, locks=tmp_path / "l")
    out = runner.tick({"t": b}, status=lambda j: (True, None), **kw)  # type: ignore[arg-type]
    assert launched == ["dv-0001"] and "launched job-1" in out[0]
    out = runner.tick({"t": b}, status=lambda j: (True, None), **kw)  # type: ignore[arg-type]
    assert launched == ["dv-0001"] and "running" in out[0]
    # job died with rc 1 and no result: one rerun, then stop, loud
    out = runner.tick({"t": b}, status=lambda j: (False, 1), **kw)  # type: ignore[arg-type]
    c = find_card(b, "dv-0001")
    assert c.pump2["stage"] == "merge" and c.pump2["attempt"] == 1
    assert "exit code: 1" in c.body
    set_pump2(p, job="job-2")
    runner.tick({"t": b}, status=lambda j: (False, 1), **kw)  # type: ignore[arg-type]
    assert find_card(b, "dv-0001").pump2["stage"] == "stopped"


def test_runner_job_cap_is_a_wait_not_a_stop(repo: Path, tmp_path: Path) -> None:
    """bgjob refusing at BGJOB_MAX_CONCURRENT: the card keeps its stage and the
    launch is retried next tick (gb-1ac7 was stopped by it, 05.11)."""
    b = board(repo, "true")
    p = repo / TASKS / "dv-0001.md"
    set_pump2(p, stage="merge", branch="pump2/x")
    cap = RuntimeError("bgjob run rc=1\nerror: 2 job(s) already running (cap 2)")

    def refuse(*a):  # type: ignore[no-untyped-def]
        raise cap

    kw = dict(locks=tmp_path / "l", status=lambda j: (True, None))
    out = runner.tick({"t": b}, launch=refuse, **kw)  # type: ignore[arg-type]
    assert out == ["t:dv-0001 merge waits: job cap"]
    c = find_card(b, "dv-0001")
    assert c.pump2["stage"] == "merge" and "launch failed" not in c.body
    # a model stage hits the same cap: same wait
    set_pump2(p, stage="review")
    out = runner.tick({"t": b}, launch_stage=refuse, **kw)  # type: ignore[arg-type]
    assert out == ["t:dv-0001 review waits: job cap"]
    assert find_card(b, "dv-0001").pump2["stage"] == "review"
    # any other launch error still stops the card, loud
    set_pump2(p, stage="merge")

    def boom(*a):  # type: ignore[no-untyped-def]
        raise RuntimeError("bgjob run rc=1\nno such profile")

    runner.tick({"t": b}, launch=boom, **kw)  # type: ignore[arg-type]
    c = find_card(b, "dv-0001")
    assert c.pump2["stage"] == "stopped" and "no such profile" in c.body


def test_set_pump2_writes_no_old_pump_key(repo: Path) -> None:
    """dv-81a7: the old pump is gone; set_pump2 adds no `pump:` marker and
    leaves any old key a card still carries to `scripts/pump2 migrate`."""
    p = repo / TASKS / "dv-0001.md"
    set_pump2(p, stage="analyze")
    set_pump2(p, stage="tests")
    fm = p.read_text().split("---")[1]
    assert not re.search(r"^pump(_\w+)?\s*:", fm, re.M)
    p2 = repo / TASKS / "dv-0002.md"
    p2.write_text("---\nid: dv-0002\nstatus: in_progress\npump: error\n---\n")
    set_pump2(p2, stage="review")
    fm2 = p2.read_text().split("---")[1]
    assert fm2.count("pump: error") == 1 and "pump: skip" not in fm2


def test_every_model_stage_has_a_route_row() -> None:
    """Each model stage runs on its models.yaml `jobs:` row (pilot 05.11: all
    stages took the default job model)."""
    from src.model_router.routes import DEFAULT_JOB_ROUTES
    from src.pump2.stages import MODULES

    assert set(runner.STAGE_ROUTE) == set(MODULES)
    assert set(runner.STAGE_ROUTE.values()) <= set(DEFAULT_JOB_ROUTES)


def test_runner_holds_card_with_open_deps(repo: Path, tmp_path: Path) -> None:
    b = board(repo, "true")
    card(repo, "dv-0001", status="todo")  # open, and not picked up (dv-79f0)
    card(repo, "dv-0002", deps="[dv-0001]")
    set_pump2(repo / TASKS / "dv-0002.md", stage="merge", branch="x")
    out = runner.tick(
        {"t": b},
        launch=lambda *a: pytest.fail("launched a held card"),
        status=lambda j: (False, 0),
        locks=tmp_path / "l",
    )
    assert out == ["t:dv-0002 held: deps dv-0001(todo)"]


def test_status_line(repo: Path) -> None:
    b = board(repo, "true")
    set_pump2(repo / TASKS / "dv-0001.md", stage="merge", job="j9")
    wt = repo / ".pump-worktrees/dv-0001/.pump"
    wt.mkdir(parents=True)
    (wt / "merge.log").write_text("one\ntwo\n")
    (line,) = render.status_lines({"t": b})
    assert line.startswith("t:dv-0001  merge  j9  0m  two")
    assert len(list_cards(b)[0]) == 1


def test_merge_commit_without_git_identity(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    branch_with(repo, "pump2/x", "b.txt", "b\n")
    (repo / "c.txt").write_text("c\n")
    git(repo, "add", "c.txt")
    git(repo, "commit", "-qm", "main moves")  # no conflict: a real merge commit
    set_pump2(repo / TASKS / "dv-0001.md", stage="merge", branch="pump2/x")
    for k in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME", "EMAIL"):
        monkeypatch.delenv(k, raising=False)
    empty = tmp_path / "empty.gitconfig"
    empty.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(empty))
    b = board(repo, "test -f b.txt -a -f c.txt")
    assert merge(b, find_card(b, "dv-0001"), locks=tmp_path / "l").outcome == "merged"


# ---- step 2: analyze + the generic model stage -----------------------------
from src.pump2.cli import main as cli_main  # noqa: E402
from src.pump2.stages import analyze  # noqa: E402

PLAN = """## Goal
Do the thing.
## Acceptance criteria
C1 [BLOCK] it prints | because: the card asks | check: model
C2 [ADVICE] a test covers it | because: safety | check: model
## Touches
{touches}
## Size
M
## Kind
bug
## Assumptions
none
## Jobs
## Questions
{questions}
## Notes
n
"""


Q_DELETE = "Q: Delete the old table? | default: keep it | matters: data | found: f"


class Fake:
    """Fake bgjob: records launches, status comes from ``code``."""

    def __init__(self) -> None:
        self.launched: list[tuple[str, str, Path, str]] = []
        self.code: int | None = 0
        self.running = False

    def launch(self, bd, c, stage, wt, prompt):  # type: ignore[no-untyped-def]
        self.launched.append((c.id, stage, wt, prompt))
        return f"job-{len(self.launched)}"

    def tick(self, b: Board, tmp: Path) -> list[str]:
        return runner.tick(
            {"t": b},
            launch_stage=self.launch,
            status=lambda j: (self.running, self.code),
            output=lambda j, n: "line1\nline2 of output",
            locks=tmp / "l",
        )


def plan(repo: Path, touches: str = "code", questions: str = "") -> None:
    d = repo / ".pump-worktrees/dv-0001/.pump"
    d.mkdir(parents=True, exist_ok=True)
    (d / "plan.md").write_text(PLAN.format(touches=touches, questions=questions))


def start(repo: Path) -> None:
    set_pump2(repo / TASKS / "dv-0001.md", stage="analyze")


def test_parse_good_missing_and_empty_touches() -> None:
    ok = analyze.parse(PLAN.format(touches="- code\ndocs", questions="none"))
    assert ok["Touches"] == "code\ndocs"
    with pytest.raises(analyze.PlanError, match="missing section.*Notes"):
        analyze.parse(PLAN.format(touches="code", questions="").split("## Notes")[0])
    with pytest.raises(analyze.PlanError, match="Touches is empty"):
        analyze.parse(PLAN.format(touches="", questions=""))
    with pytest.raises(analyze.PlanError, match="unknown"):
        analyze.parse(PLAN.format(touches="code\nweb", questions=""))


def test_first_tick_creates_branch_worktree_and_launches(
    repo: Path, tmp_path: Path
) -> None:
    b, f = board(repo, "true"), Fake()
    start(repo)
    f.running = True
    (line,) = f.tick(b, tmp_path)
    assert "analyze launched job-1" in line and "branch pump2/dv-0001 created" in line
    assert git(repo, "rev-parse", "pump2/dv-0001~1") == git(repo, "rev-parse", "main")
    cid, stage, wt, prompt = f.launched[0]
    assert wt == repo / ".pump-worktrees/dv-0001" and wt.exists()
    assert "keep me" in prompt and "## Acceptance criteria" in prompt
    assert str(wt) in prompt
    c = find_card(b, "dv-0001")
    assert c.pump2["job"] == "job-1" and c.pump2["branch"] == "pump2/dv-0001"
    # board repo stays clean; .gitignore line is committed on the card branch
    assert git(repo, "status", "--porcelain").split() == [
        "M",
        str(TASKS / "dv-0001.md"),
    ]
    assert ".pump/" in git(repo, "show", "pump2/dv-0001:.gitignore")
    assert "running job-1" in f.tick(b, tmp_path)[0]


def test_branch_is_cut_from_the_integration_branch(repo: Path, tmp_path: Path) -> None:
    git(repo, "checkout", "-q", "-b", "integ")
    (repo / "i.txt").write_text("i\n")
    git(repo, "add", "i.txt")
    git(repo, "commit", "-qm", "integ")
    git(repo, "checkout", "-q", "main")
    b = Board(name="t", repo=repo, test="true", branch="integ")
    f = Fake()
    start(repo)
    f.running = True
    f.tick(b, tmp_path)
    assert git(repo, "merge-base", "--is-ancestor", "integ", "pump2/dv-0001") == ""
    assert (repo / ".pump-worktrees/dv-0001/i.txt").exists()


def test_no_questions_routing(repo: Path, tmp_path: Path) -> None:
    b, f = board(repo, "true"), Fake()
    start(repo)
    f.tick(b, tmp_path)  # launch (job reported finished next)
    plan(repo, "code\ndocs")
    assert "analyze done -> tests" in f.tick(b, tmp_path)[0]
    c = find_card(b, "dv-0001")
    assert c.pump2["stage"] == "tests" and "job" not in c.pump2
    assert "Touches: code, docs" in c.body and "Acceptance criteria: 2" in c.body
    assert "plan.md" in c.body and c.fm["status"] == "in_progress"
    assert "tests launched" in f.tick(b, tmp_path)[0]


def test_docs_only_skips_tests_and_builds(repo: Path, tmp_path: Path) -> None:
    b, f = board(repo, "true"), Fake()
    start(repo)
    f.tick(b, tmp_path)
    plan(repo, "docs")
    assert "-> build" in f.tick(b, tmp_path)[0]
    assert find_card(b, "dv-0001").pump2["stage"] == "build"


def test_bad_plan_stops_loud(repo: Path, tmp_path: Path) -> None:
    b, f = board(repo, "true"), Fake()
    start(repo)
    f.tick(b, tmp_path)
    plan(repo, touches="")
    f.tick(b, tmp_path)  # first rejection: one rerun with the error (dv-d5aa)
    assert find_card(b, "dv-0001").pump2["format_retry"] == 1
    f.tick(b, tmp_path)  # relaunch
    plan(repo, touches="")
    f.tick(b, tmp_path)
    c = find_card(b, "dv-0001")
    assert c.pump2["stage"] == "stopped"
    assert "Touches is empty" in c.body and "## Goal" in c.body


def test_question_round_trip_and_second_round_stop(repo: Path, tmp_path: Path) -> None:
    b, f = board(repo, "true"), Fake()
    start(repo)
    f.tick(b, tmp_path)
    plan(repo, "code", Q_DELETE)
    f.tick(b, tmp_path)
    c = find_card(b, "dv-0001")
    assert c.fm["status"] == "waiting_user" and c.pump2["stage"] == "analyze"
    assert c.pump2["round"] == 1 and "1. Q: Delete the old table?" in c.body
    assert "[waiting_user]" in render.status_lines({"t": b})[0]
    assert f.tick(b, tmp_path) == []  # parked: nothing happens
    bf = repo / "boards.json"
    bf.write_text(f'{{"boards":[{{"name":"t","repo":"{repo}"}}]}}')
    assert cli_main(["--boards", str(bf), "answer", "t:dv-0001", "Yes, delete."]) == 0
    c = find_card(b, "dv-0001")
    assert c.fm["status"] == "in_progress"
    assert c.body.rstrip().endswith("Yes, delete.") and "### @owner — " in c.body
    assert cli_main(["--boards", str(bf), "answer", "t:dv-0001", "again"]) == 2
    f.tick(b, tmp_path)  # relaunch, with the earlier plan and the answer
    assert len(f.launched) == 2
    p2 = f.launched[1][3]
    assert "Q: Delete the old table?" in p2 and "Yes, delete." in p2
    plan(repo, "code", "Q: And the backups? | default: keep | matters: m | found: f")
    f.tick(b, tmp_path)
    c = find_card(b, "dv-0001")
    assert c.pump2["stage"] == "stopped" and "asked twice" in c.body
    assert "1. Q: And the backups?" in c.body


def test_second_pass_without_questions_proceeds(repo: Path, tmp_path: Path) -> None:
    b, f = board(repo, "true"), Fake()
    start(repo)
    f.tick(b, tmp_path)
    plan(repo, "code", "Q: q? | default: d | matters: m | found: f")
    f.tick(b, tmp_path)
    bf = repo / "boards.json"
    bf.write_text(f'{{"boards":[{{"name":"t","repo":"{repo}"}}]}}')
    cli_main(["--boards", str(bf), "answer", "t:dv-0001", "ok"])
    f.tick(b, tmp_path)
    plan(repo, "code", "")
    f.tick(b, tmp_path)
    c = find_card(b, "dv-0001")
    assert c.pump2["stage"] == "tests" and "round" not in c.pump2


def test_model_job_rc_nonzero_reruns_once_then_stops(
    repo: Path, tmp_path: Path
) -> None:
    b, f = board(repo, "true"), Fake()
    start(repo)
    f.tick(b, tmp_path)
    f.code = 3
    f.tick(b, tmp_path)
    c = find_card(b, "dv-0001")
    assert c.pump2["stage"] == "analyze" and c.pump2["retry"] == 1
    assert "exit code: 3" in c.body and "line2 of output" in c.body
    assert "rerunning once" in c.body
    f.tick(b, tmp_path)  # the rerun launches
    assert len(f.launched) == 2
    f.tick(b, tmp_path)
    c = find_card(b, "dv-0001")
    assert c.pump2["stage"] == "stopped" and "stopped for the owner" in c.body


def test_launch_oserror_stops_only_that_card(repo: Path, tmp_path: Path) -> None:
    """06.11.26: E2BIG from one card's launch killed every tick for 20 min."""
    b, f = board(repo, "true"), Fake()
    start(repo)

    def boom(bd, c, stage, wt, prompt):  # type: ignore[no-untyped-def]
        raise OSError(7, "Argument list too long")

    f.launch = boom  # type: ignore[method-assign]
    (line,) = f.tick(b, tmp_path)
    assert "launch failed" in line
    c = find_card(b, "dv-0001")
    assert c.pump2["stage"] == "stopped" and "Argument list too long" in c.body


def test_big_prompt_goes_through_a_file(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[list[str]] = []

    def run(argv, **kw):  # type: ignore[no-untyped-def]
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, "started job-9\n", "")

    monkeypatch.setattr(runner.subprocess, "run", run)
    monkeypatch.setattr(runner, "_STARTED", __import__("re").compile(r"(job-\d+)"))
    b = board(repo, "true")
    start(repo)
    c = find_card(b, "dv-0001")
    wt = tmp_path / "wt"
    big = "x" * (runner.PROMPT_ARG_MAX + 1)
    assert runner.bgjob_launch_stage(b, c, "mergefix", wt, big) == "job-9"
    arg = seen[0][-1]
    assert len(arg) < 1000 and "prompt-mergefix.md" in arg
    assert (wt / ".pump" / "prompt-mergefix.md").read_text() == big
    seen.clear()
    runner.bgjob_launch_stage(b, c, "build", wt, "small")
    assert seen[0][-1] == "small"


def test_merge_killed_tests_retry_merge_then_stop(repo: Path, tmp_path: Path) -> None:
    # gb-1aed (06.11): a host-wide OOM killed the merge run (137); it went back
    # to build as RED, and the build had nothing to change
    branch_with(repo, "pump2/x", "b.txt", "b\n")
    p = repo / TASKS / "dv-0001.md"
    set_pump2(p, stage="merge", branch="pump2/x")
    b = board(repo, "exit 137")
    for k in (1, 2):
        assert merge(b, find_card(b, "dv-0001"), locks=tmp_path / "l").outcome == (
            "busy"
        )
        c = find_card(b, "dv-0001")
        assert c.pump2["stage"] == "merge" and c.pump2["killed"] == k
        assert f"retry {k}/2" in c.body and "back to build" not in c.body
    merge(b, find_card(b, "dv-0001"), locks=tmp_path / "l")
    c = find_card(b, "dv-0001")
    assert c.pump2["stage"] == "stopped" and "killed" not in c.pump2
