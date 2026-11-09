"""Pump v2 step 3: tests + build stages, python resolution, bad cards."""

import json
import sys
from pathlib import Path

import pytest

from src.pump2 import render, runner
from src.pump2.state import Board, find_card, list_cards, load_boards, set_pump2

from .test_pump2 import TASKS, Fake, board, git, plan
from .test_pump2 import repo as _repo

repo = _repo  # the shared fixture

WT = ".pump-worktrees/dv-0001"


def script(tmp: Path, name: str, body: str) -> str:
    p = tmp / name
    p.write_text("#!/bin/sh\n" + body + "\n")
    p.chmod(0o755)
    return str(p)


def setup(repo: Path, tmp: Path, stage: str, b: Board, f: Fake) -> Path:
    """Card at ``stage``, worktree made by an analyze tick, plan.md written;
    the stage's own job is launched (f.launched[-1])."""
    set_pump2(repo / TASKS / "dv-0001.md", stage="analyze")
    f.running = True
    f.tick(b, tmp)  # creates branch + worktree
    plan(repo)
    set_pump2(repo / TASKS / "dv-0001.md", stage=stage, job=None)
    f.running = False
    f.tick(b, tmp)  # launches the stage
    return repo / WT


def put(wt: Path, rel: str, text: str = "x\n") -> None:
    (wt / rel).parent.mkdir(parents=True, exist_ok=True)
    (wt / rel).write_text(text)


def write_tests_json(wt: Path, files: object) -> None:
    (wt / ".pump/tests.json").write_text(json.dumps({"files": files}))


def stage_of(b: Board) -> str:
    return str(find_card(b, "dv-0001").pump2["stage"])


def body(b: Board) -> str:
    return find_card(b, "dv-0001").body


# ---- tests stage -----------------------------------------------------------
def test_tests_stage_prompt_and_good_finish(repo: Path, tmp_path: Path) -> None:
    b, f = board(repo, "true"), Fake()
    wt = setup(repo, tmp_path, "tests", b, f)
    prompt = f.launched[-1][3]
    assert "FAILING tests" in prompt
    assert "## Acceptance criteria" in prompt and str(wt) in prompt
    put(wt, "tests/test_new.py", "def test_a():\n    assert False\n")
    put(wt, "tests/test_two.py")
    write_tests_json(wt, ["tests/test_new.py", "tests/test_two.py"])
    assert "tests done -> build" in f.tick(b, tmp_path)[0]
    assert stage_of(b) == "build"
    sha = git(repo, "rev-parse", "pump2/dv-0001")
    assert sha in body(b) and "tests/test_two.py" in body(b)
    assert git(repo, "log", "-1", "--format=%s", "pump2/dv-0001") == "dv-0001: tests"
    files = git(repo, "show", "--name-only", "--format=", "pump2/dv-0001").split()
    assert sorted(files) == ["tests/test_new.py", "tests/test_two.py"]


@pytest.mark.parametrize(
    "case,msg",
    [
        ("missing", "tests.json was not written"),
        ("outside", "not existing files under tests/"),
        ("absent", "not existing files under tests/"),
        ("traversal", "not existing files under tests/"),
        ("empty", "lists no files"),
        ("garbage", "tests.json rejected"),
        ("unchanged", "not new or changed"),
        ("extra", "tests.json does not list"),
    ],
)
def test_tests_stage_rejects(repo: Path, tmp_path: Path, case: str, msg: str) -> None:
    b, f = board(repo, "true"), Fake()
    wt = setup(repo, tmp_path, "tests", b, f)
    put(wt, "tests/test_new.py")
    put(wt, "src_file.py")
    if case == "outside":
        write_tests_json(wt, ["src_file.py"])
    elif case == "absent":
        write_tests_json(wt, ["tests/nope.py"])
    elif case == "traversal":
        write_tests_json(wt, ["tests/../src_file.py"])
    elif case == "empty":
        write_tests_json(wt, [])
    elif case == "garbage":
        (wt / ".pump/tests.json").write_text("{nope")
    elif case == "unchanged":
        git(wt, "add", "tests/test_new.py")
        git(wt, "commit", "-qm", "pre")
        write_tests_json(wt, ["tests/test_new.py"])
    elif case == "extra":
        write_tests_json(wt, ["tests/test_new.py"])
    f.tick(b, tmp_path)
    assert stage_of(b) == "stopped" and msg in body(b)
    assert not git(repo, "log", "--format=%s", "pump2/dv-0001").startswith("dv-0001")


# ---- build stage -----------------------------------------------------------
def built(repo: Path, tmp: Path, test: str, python: str) -> tuple[Board, Fake, Path]:
    b = Board(name="t", repo=repo, test=test, python=python)
    f = Fake()
    wt = setup(repo, tmp, "tests", b, f)
    put(wt, "tests/test_new.py", "def test_a():\n    assert False\n")
    write_tests_json(wt, ["tests/test_new.py"])
    f.tick(b, tmp)  # tests done -> build
    f.tick(b, tmp)  # build launched
    return b, f, wt


def test_build_prompt_lists_tests_and_forbids_edits(repo: Path, tmp_path: Path) -> None:
    b, f, wt = built(repo, tmp_path, "pytest -m core", "python3")
    prompt = f.launched[-1][3]
    assert f.launched[-1][1] == "build"
    assert "tests/test_new.py" in prompt and "may NOT edit" in prompt
    assert ".pump/build.md" in prompt and "pytest -m core" in prompt


def test_build_edited_test_file_stops(repo: Path, tmp_path: Path) -> None:
    b, f, wt = built(repo, tmp_path, "true", script(tmp_path, "py", "exit 0"))
    put(wt, "tests/test_new.py", "def test_a():\n    pass\n")
    put(wt, "feature.py")
    f.tick(b, tmp_path)
    assert stage_of(b) == "stopped"
    assert "build edited test files: ['tests/test_new.py']" in body(b)


def test_build_changed_nothing_stops(repo: Path, tmp_path: Path) -> None:
    b, f, wt = built(repo, tmp_path, "true", script(tmp_path, "py", "exit 0"))
    f.tick(b, tmp_path)
    assert stage_of(b) == "stopped" and "build changed nothing" in body(b)


def test_docs_only_build_commits_without_tests(repo: Path, tmp_path: Path) -> None:
    b, f = board(repo, "true"), Fake()
    wt = setup(repo, tmp_path, "build", b, f)  # no tests stage ran: no tests.json
    prompt = f.launched[-1][3]
    assert "docs\nonly" in prompt and "may NOT edit" not in prompt
    put(wt, "docs/howto.md", "# how\n")
    assert "build done -> review" in f.tick(b, tmp_path)[0]
    assert stage_of(b) == "review" and "docs-only plan" in body(b)
    assert git(repo, "log", "-1", "--format=%s", "pump2/dv-0001") == "dv-0001: build"


def test_docs_only_build_changed_nothing_stops(repo: Path, tmp_path: Path) -> None:
    b, f = board(repo, "true"), Fake()
    setup(repo, tmp_path, "build", b, f)
    f.tick(b, tmp_path)
    assert stage_of(b) == "stopped" and "build changed nothing" in body(b)


def test_build_red_stops_with_node_ids(repo: Path, tmp_path: Path) -> None:
    red = script(tmp_path, "py", "echo 'FAILED tests/test_new.py::test_a - x'; exit 1")
    b, f, wt = built(repo, tmp_path, "true", red)
    put(wt, "feature.py")
    f.tick(b, tmp_path)  # first red: one fix round (dv-80b8)
    assert stage_of(b) == "build"
    f.tick(b, tmp_path)  # relaunch
    f.tick(b, tmp_path)  # second red
    assert stage_of(b) == "stopped"
    assert "RED" in body(b) and "tests/test_new.py::test_a" in body(b)
    assert not git(repo, "log", "-1", "--format=%s", "pump2/dv-0001").endswith("build")


def test_build_green_commits_and_goes_to_review(repo: Path, tmp_path: Path) -> None:
    ok = script(tmp_path, "py", "echo '1 passed in 0.01s'; exit 0")
    b, f, wt = built(repo, tmp_path, "true", ok)
    put(wt, "feature.py", "y = 1\n")
    put(wt, ".pump/build.md", "added feature.py\n")
    assert "build done -> review" in f.tick(b, tmp_path)[0]
    assert stage_of(b) == "review"
    c = body(b)
    assert "1 passed" in c and "added feature.py" in c
    sha = git(repo, "rev-parse", "pump2/dv-0001")
    assert sha in c and "1 file(s) changed" in c
    assert git(repo, "log", "-1", "--format=%s", "pump2/dv-0001") == "dv-0001: build"
    assert git(repo, "show", "--name-only", "--format=", "pump2/dv-0001") == (
        "feature.py"
    )
    assert "review launched" in f.tick(b, tmp_path)[0]  # step 4 runs it


@pytest.mark.parametrize(
    "test,has", [("pytest -m core --no-cov", True), ("pytest -m core", False)]
)
def test_build_passes_no_cov_only_when_board_uses_it(
    repo: Path, tmp_path: Path, test: str, has: bool
) -> None:
    # app has no pytest-cov: a forced --no-cov made every build exit 4 (05.11)
    red = script(tmp_path, "py", 'echo "args: $*"; exit 1')
    b, f, wt = built(repo, tmp_path, test, red)
    put(wt, "feature.py")
    f.tick(b, tmp_path)
    assert stage_of(b) == "build"  # first red: a fix round (dv-80b8)
    assert ("--no-cov" in body(b)) is has


def test_build_tests_see_new_files_in_git_ls_files(repo: Path, tmp_path: Path) -> None:
    # dv-a3d1: a test that reads `git ls-files` went red because the build's
    # new file was still untracked when the gate ran (commit comes after).
    b, f, wt = built(repo, tmp_path, "true", sys.executable)
    put(
        wt,
        "tests/test_new.py",
        "import subprocess\n\n\ndef test_a():\n"
        "    out = subprocess.run(['git', 'ls-files'], capture_output=True,"
        " text=True).stdout\n"
        "    assert 'docs/new.md' in out.split()\n",
    )
    git(wt, "add", "tests/test_new.py")
    git(wt, "commit", "-qm", "ls-files test")
    put(wt, "docs/new.md", "hi\n")
    f.tick(b, tmp_path)
    assert stage_of(b) == "review", body(b)
    assert "docs/new.md" in git(
        repo, "show", "--name-only", "--format=", "pump2/dv-0001"
    )


def test_build_with_a_real_pytest_run(repo: Path, tmp_path: Path) -> None:
    b, f, wt = built(repo, tmp_path, "true", sys.executable)
    # replace the failing test by one that needs the feature (not allowed to be
    # edited by build, so rewrite it in the committed history instead)
    put(
        wt,
        "tests/test_new.py",
        "from feature import y\n\n\ndef test_a():\n    assert y == 1\n",
    )
    git(wt, "add", "tests/test_new.py")
    git(wt, "commit", "-qm", "real test")
    put(wt, "feature.py", "y = 1\n")
    f.tick(b, tmp_path)
    assert stage_of(b) == "review"
    assert " passed" in body(b) and "peak" not in body(b).split("tests: ")[-1][:20]


# ---- python resolution -----------------------------------------------------
def test_python_resolution() -> None:
    assert Board("a", Path("/r"), test="/v/bin/pytest -m core").test_python() == (
        "/v/bin/python"
    )
    assert Board("a", Path("/r"), test="/v/bin/pytest", python="/p").test_python() == (
        "/p"
    )
    assert Board("a", Path("/r"), test="make test").test_python() == "python3"


def test_load_boards_pump2_python(tmp_path: Path) -> None:
    f = tmp_path / "b.json"
    f.write_text('{"boards":[{"name":"x","repo":"/r","pump2_python":"/q/python"}]}')
    assert load_boards(f)["x"].python == "/q/python"


# ---- unparseable cards -----------------------------------------------------
def test_unparseable_card_is_listed(repo: Path, tmp_path: Path) -> None:
    bad = repo / TASKS / "dv-bad.md"
    bad.write_text('---\nid: dv-bad\npump2:\n  stage: "x\n---\nbody\n')
    b = board(repo, "true")
    cards, errs = list_cards(b)
    assert [c.id for c in cards] == ["dv-0001"]
    assert [p.name for p, _ in errs] == ["dv-bad.md"]
    assert "\n" not in errs[0][1]
    line = [ln for ln in render.status_lines({"t": b}) if "dv-bad" in ln][0]
    assert line.startswith("t:dv-bad.md  ERROR  ") and "pump2" in line
    tick = runner.tick({"t": b}, locks=tmp_path / "l")
    assert any(ln.startswith("t:dv-bad.md ERROR") for ln in tick)


def test_build_gate_past_timeout_stops_not_rebuilds(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # dv-a833 06.11: a gate run longer than the tick got the tick killed and the
    # card rebuilt 12 times; now the run is cut and the card stops once
    from src.pump2.stages import build

    monkeypatch.setattr(build, "GATE_TIMEOUT", 1)
    slow = script(tmp_path, "py", "sleep 30; echo '1 passed'")
    b, f, wt = built(repo, tmp_path, "true", slow)
    put(wt, "feature.py")
    f.tick(b, tmp_path)
    assert stage_of(b) == "stopped"
    assert "ran past 1 s" in body(b)


def test_build_skips_a_listed_test_file_the_branch_lost(
    repo: Path, tmp_path: Path
) -> None:
    # dv-1af0 (06.11): a mergefix deleted a listed file the integration branch
    # had removed, and the build gate stopped on "file or directory not found"
    ok = script(tmp_path, "py", "echo '1 passed in 0.01s'; exit 0")
    b, f, wt = built(repo, tmp_path, "true", ok)
    write_tests_json(wt, ["tests/test_new.py", "tests/test_gone.py"])
    put(wt, "feature.py", "y = 1\n")
    assert "build done -> review" in f.tick(b, tmp_path)[0]
    assert "not run, gone from the branch: tests/test_gone.py" in body(b)


def test_C1_build_that_git_rms_a_tracked_file_goes_to_review(
    repo: Path, tmp_path: Path
) -> None:
    # dv-92ee: gb-1ada / gb-1ae2 stopped on `git add --intent-to-add` failing
    # with "pathspec did not match" for a file the build had git rm'd
    ok = script(tmp_path, "py", "echo '1 passed in 0.01s'; exit 0")
    b, f, wt = built(repo, tmp_path, "true", ok)
    put(wt, "old/legacy.txt", "old\n")
    git(wt, "add", "old/legacy.txt")
    git(wt, "commit", "-qm", "legacy file")
    git(wt, "rm", "-q", "old/legacy.txt")
    put(wt, "feature.py", "y = 1\n")
    f.tick(b, tmp_path)
    assert stage_of(b) == "review", body(b)
    assert "old/legacy.txt" not in git(
        repo, "ls-tree", "-r", "--name-only", "pump2/dv-0001"
    )
    assert "feature.py" in git(repo, "ls-tree", "-r", "--name-only", "pump2/dv-0001")


def test_C2_failed_intent_to_add_warns_and_continues(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.pump2.stages import build

    ok = script(tmp_path, "py", "echo '1 passed in 0.01s'; exit 0")
    b, f, wt = built(repo, tmp_path, "true", ok)
    put(wt, "feature.py", "y = 1\n")
    real_sh = build.sh

    def fake_sh(cmd, cwd, *a, **kw):  # type: ignore[no-untyped-def]
        if "--intent-to-add" in cmd:
            return 128, "fatal: pathspec 'x' did not match any files"
        return real_sh(cmd, cwd, *a, **kw)

    monkeypatch.setattr(build, "sh", fake_sh)
    f.tick(b, tmp_path)
    assert stage_of(b) == "review", body(b)
    assert "warning:" in body(b) and "intent-to-add" in body(b)
