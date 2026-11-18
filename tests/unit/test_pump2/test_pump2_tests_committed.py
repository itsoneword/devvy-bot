"""dv-47e7: a tests stage whose listed files are already committed on the card
branch (vs the integration branch) moves on to build."""

from pathlib import Path

from src.pump2.state import find_card  # noqa: F401

from .test_pump2 import Fake, board, git
from .test_pump2 import repo as _repo
from .test_pump2_step3 import body, put, setup, stage_of, write_tests_json

repo = _repo

TEST = "tests/test_new.py"
SRC = "def test_C1_a():\n    assert False\n"


def test_C1_tests_already_committed_on_branch_go_to_build(
    repo: Path, tmp_path: Path
) -> None:
    b, f = board(repo, "true"), Fake()
    wt = setup(repo, tmp_path, "tests", b, f)
    put(wt, TEST, SRC)
    git(wt, "add", TEST)
    git(wt, "commit", "-qm", "dv-0001: tests")
    write_tests_json(wt, [TEST])
    head = git(wt, "rev-parse", "HEAD").strip()
    f.tick(b, tmp_path)
    assert stage_of(b) == "build", body(b)
    assert "(kept, unchanged)" in body(b)
    assert git(wt, "rev-parse", "HEAD").strip() == head  # no new commit


def test_C2_listed_file_identical_to_integration_branch_still_stops(
    repo: Path, tmp_path: Path
) -> None:
    put(repo, TEST, SRC)
    git(repo, "add", TEST)
    git(repo, "commit", "-qm", "test on main")
    b, f = board(repo, "true"), Fake()
    wt = setup(repo, tmp_path, "tests", b, f)
    write_tests_json(wt, [TEST])  # unchanged vs HEAD and vs main
    f.tick(b, tmp_path)
    assert stage_of(b) == "resolve", body(b)  # every stop goes to resolve (dv-b8bb)
    assert "not new or changed" in body(b)


def test_C2_changed_file_not_listed_still_stops(repo: Path, tmp_path: Path) -> None:
    b, f = board(repo, "true"), Fake()
    wt = setup(repo, tmp_path, "tests", b, f)
    put(wt, TEST, SRC)
    git(wt, "add", TEST)
    git(wt, "commit", "-qm", "dv-0001: tests")
    put(wt, "tests/test_other.py", SRC)
    write_tests_json(wt, [TEST])
    f.tick(b, tmp_path)
    assert stage_of(b) == "resolve", body(b)  # every stop goes to resolve (dv-b8bb)
    assert "does not list" in body(b)
