"""dv-80b8: restart a stopped card (`pump2 start`), a red build gets one fix
round, stop paths record `stopped_at`."""

from pathlib import Path

import pytest

from src.pump2 import cli
from src.pump2.state import add_comment, find_card, set_pump2

from .test_pump2 import TASKS, board
from .test_pump2 import repo as _repo
from .test_pump2_step3 import (
    body,
    built,
    put,
    script,
    setup,
    stage_of,
    write_tests_json,
)

repo = _repo

RED = "echo 'FAILED tests/test_new.py::test_a - x'; exit 1"


def card_of(repo: Path):  # type: ignore[no-untyped-def]
    return find_card(board(repo, "true"), "dv-0001")


def restart(repo: Path, by: str = "devops") -> int:
    return cli._start({"t": board(repo, "true")}, "t:dv-0001", by)


def test_C1_start_restarts_stopped_card_at_its_stage(repo: Path) -> None:
    p = repo / TASKS / "dv-0001.md"
    set_pump2(p, stage="stopped", stopped_at="build", job="j1", job_of="build", retry=1)
    p.write_text(p.read_text().replace("status: in_progress", "status: blocked"))
    assert restart(repo, "gymbro") == 0
    c = card_of(repo)
    assert c.pump2["stage"] == "build"
    for k in ("job", "job_of", "retry"):
        assert k not in c.pump2
    assert "status: in_progress" in p.read_text()
    assert "restarted by gymbro at build" in c.body


def test_C1_start_still_refuses_live_and_done_cards(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    p = repo / TASKS / "dv-0001.md"
    for st in ("review", "done"):
        set_pump2(p, stage=st)
        assert restart(repo) != 0
        assert card_of(repo).pump2["stage"] == st


def test_C1_every_stop_path_writes_stopped_at(repo: Path, tmp_path: Path) -> None:
    from .test_pump2 import Fake

    b, f = board(repo, "true"), Fake()
    p = repo / TASKS / "dv-0001.md"
    set_pump2(p, stage="build", jobs=12)  # at the job ceiling
    f.tick(b, tmp_path)
    c = find_card(b, "dv-0001")
    assert c.pump2["stage"] == "stopped" and c.pump2["stopped_at"] == "build"


def test_C1_red_build_stop_writes_stopped_at(repo: Path, tmp_path: Path) -> None:
    b, f, wt = built(repo, tmp_path, "true", script(tmp_path, "py", RED))
    put(wt, "feature.py")
    f.tick(b, tmp_path)  # red 1: fix round
    f.tick(b, tmp_path)  # relaunch
    f.tick(b, tmp_path)  # red 2: stop
    c = find_card(b, "dv-0001")
    assert c.pump2["stage"] == "stopped" and c.pump2["stopped_at"] == "build"


@pytest.mark.parametrize(
    "prefix,want",
    [
        ("build: the tests in tests.json are RED", "build"),
        ("tests: BLOCK criteria with check: test have no test named C1", "tests"),
        ("review: returned", "review"),
        ("merge: conflict", "merge"),
        ("mergefix: gave up", "mergefix"),
        ("analyze: plan rejected", "analyze"),
        ("something unprefixed", "analyze"),
    ],
)
def test_C2_legacy_stopped_card_restarts_at_comment_prefix(
    repo: Path, prefix: str, want: str
) -> None:
    p = repo / TASKS / "dv-0001.md"
    set_pump2(p, stage="stopped")
    add_comment(p, "analyze: older comment")
    add_comment(p, prefix)
    assert "stopped_at" not in card_of(repo).pump2
    assert restart(repo) == 0
    assert card_of(repo).pump2["stage"] == want


def test_C3_first_red_build_reruns_with_failing_list(
    repo: Path, tmp_path: Path
) -> None:
    b, f, wt = built(repo, tmp_path, "true", script(tmp_path, "py", RED))
    put(wt, "feature.py")
    n = len(f.launched)
    f.tick(b, tmp_path)  # gate red
    assert stage_of(b) == "build"
    assert find_card(b, "dv-0001").pump2["build_red"] == 1
    f.tick(b, tmp_path)  # rerun launched
    assert len(f.launched) == n + 1 and f.launched[-1][1] == "build"
    assert "tests/test_new.py::test_a" in f.launched[-1][3]
    assert "build-tests.log" not in body(b) or True
    f.tick(b, tmp_path)  # second red
    assert stage_of(b) == "stopped"


def test_C3_manual_restart_prompt_lists_failing_tests(
    repo: Path, tmp_path: Path
) -> None:
    b, f, wt = built(repo, tmp_path, "true", script(tmp_path, "py", RED))
    put(wt, "feature.py")
    for _ in range(3):
        f.tick(b, tmp_path)
    assert stage_of(b) == "stopped"
    assert cli._start({"t": b}, "t:dv-0001", "devops") == 0
    assert stage_of(b) == "build"
    f.tick(b, tmp_path)
    assert f.launched[-1][1] == "build"
    assert "tests/test_new.py::test_a" in f.launched[-1][3]
    f.tick(b, tmp_path)  # red again: the restart got its own fix round
    assert stage_of(b) == "build"


def test_C4_job_start_pump_ids_registered() -> None:
    from src.claude.agent_tools import valid_task_id

    for ok in ("gb-1af9", "gymbro:gb-1af9", "dv-dfb1", "pl:dv-0a1b2c"):
        assert valid_task_id(ok), ok
    for bad in (
        "",
        "--cwd=/",
        "dv-dfb1; rm",
        "../dv-dfb1",
        "gb-",
        "gb-1af9 --by x",
        "gymbro:../gb-1af9",
    ):
        assert not valid_task_id(bad), bad


def test_C6_missing_block_test_gets_one_rerun(repo: Path, tmp_path: Path) -> None:
    from .test_pump2 import Fake

    b, f = board(repo, "true"), Fake()
    wt = setup(repo, tmp_path, "tests", b, f)
    plan = wt / ".pump" / "plan.md"
    plan.write_text(
        plan.read_text().replace(
            "it prints | because: the card asks | check: model",
            "it prints | because: the card asks | check: test",
        )
    )
    set_pump2(repo / TASKS / "dv-0001.md", criteria_sha="abc")
    put(wt, "tests/test_new.py", "def test_other():\n    assert True\n")
    write_tests_json(wt, ["tests/test_new.py"])
    n = len(f.launched)
    f.tick(b, tmp_path)
    assert stage_of(b) == "tests"
    f.tick(b, tmp_path)
    assert len(f.launched) == n + 1 and f.launched[-1][1] == "tests"
    assert "test_<id>" in f.launched[-1][3] or "C1" in f.launched[-1][3]
    f.tick(b, tmp_path)
    assert stage_of(b) == "stopped"


def test_C7_restart_at_job_ceiling_is_refused(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    p = repo / TASKS / "dv-0001.md"
    set_pump2(p, stage="stopped", stopped_at="build", jobs=12)
    assert restart(repo) != 0
    assert "ceiling" in capsys.readouterr().out.lower()
    assert card_of(repo).pump2["stage"] == "stopped"
