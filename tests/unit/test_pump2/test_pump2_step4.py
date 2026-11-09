"""Pump v2 step 4: lenient frontmatter, review stage, board guard."""

from pathlib import Path

import pytest

from src.pump2.state import list_cards, parse_frontmatter, read_card, set_pump2

from .test_pump2 import Board


def test_lenient_frontmatter_cases() -> None:
    fm = parse_frontmatter(
        [
            "id: dv-1",
            "title: Tool key store: /setkey, /keys, /delkey",
            "deps: [dv-a, dv-b]",
            "labels: [x]",
            'q: "quoted: yes"',
            "n: 5",
            "empty:",
            "pump2:",
            '  stage: "build"',
            "  attempt: 1",
        ]
    )
    assert fm["title"] == "Tool key store: /setkey, /keys, /delkey"
    assert fm["deps"] == ["dv-a", "dv-b"] and fm["labels"] == ["x"]
    assert fm["q"] == "quoted: yes" and fm["n"] == 5 and fm["empty"] is None
    assert fm["pump2"] == {"stage": "build", "attempt": 1}


def test_round_trip_through_set_pump2(tmp_path: Path) -> None:
    p = tmp_path / "dv-1.md"
    head = "id: dv-1\ntitle: Tool key store: /setkey, /keys\ndeps: [dv-a]\n"
    p.write_text(f"---\n{head}status: in_progress\n---\n\nbody\n")
    set_pump2(p, stage="review")
    set_pump2(p, stage="build", returns=1)
    text = p.read_text()
    assert text.startswith(f"---\n{head}status: in_progress\n")
    c = read_card(p)
    assert c.fm["title"] == "Tool key store: /setkey, /keys"
    assert c.pump2["stage"] == "build" and c.pump2["returns"] == 1
    assert text.endswith("---\n\nbody\n")


def test_truly_broken_cards_are_listed(tmp_path: Path) -> None:
    d = tmp_path / ".deviz" / "tasks"
    d.mkdir(parents=True)
    (d / "a.md").write_text("no frontmatter\n")
    (d / "b.md").write_text('---\nid: b\npump2:\n  stage: "x\n---\n')
    (d / "c.md").write_text("---\nid: c\ntitle: ok: fine\n---\n")
    cards, bad = list_cards(Board(name="t", repo=tmp_path))
    assert [c.id for c in cards] == ["c"]
    assert sorted(p.name for p, _ in bad) == ["a.md", "b.md"]


# ---- review stage ----------------------------------------------------------
from src.pump2.stages import review  # noqa: E402
from src.pump2.state import find_card  # noqa: E402

from .test_pump2 import TASKS, Fake, board, git  # noqa: E402
from .test_pump2 import repo as _repo  # noqa: E402,F401
from .test_pump2_step3 import put, setup  # noqa: E402

repo = _repo


def verdict(wt: Path, v: str, remarks: str = "") -> None:
    (wt / ".pump/review.md").write_text(f"## Verdict\n{v}\n\n## Remarks\n{remarks}\n")


def st(b: Board) -> dict:  # type: ignore[type-arg]
    return find_card(b, "dv-0001").pump2


def test_review_prompt_and_pass(repo: Path, tmp_path: Path) -> None:
    b, f = board(repo, "true"), Fake()
    wt = setup(repo, tmp_path, "review", b, f)
    p = f.launched[-1][3]
    assert "## Acceptance criteria" in p and "=== diff main...HEAD ===" in p
    assert "docs`-only" in p and "tests.json" in p
    verdict(wt, "pass")
    assert "review done -> merge" in f.tick(b, tmp_path)[0]
    assert st(b)["stage"] == "merge"


def test_review_diff_inline_and_capped(repo: Path, tmp_path: Path) -> None:
    b, f = board(repo, "true"), Fake()
    wt = setup(repo, tmp_path, "review", b, f)
    put(wt, "small.py", "x = 1\n")
    git(wt, "add", "-A")
    git(wt, "commit", "-qm", "c")
    assert "+x = 1" in review.prompt(b, find_card(b, "dv-0001"), wt)
    put(wt, "big.py", "y = 1\n" * (review.DIFF_CAP_BYTES // 3))
    git(wt, "add", "-A")
    git(wt, "commit", "-qm", "c2")
    p = review.prompt(b, find_card(b, "dv-0001"), wt)
    assert f"over {review.DIFF_CAP_BYTES} bytes" in p and "big.py" in p
    assert "+y = 1" not in p and "## Files read" in p


def _big_commit(wt: Path) -> None:
    put(wt, "big.py", "y = 1\n" * (review.DIFF_CAP_BYTES // 3))
    git(wt, "add", "-A")
    git(wt, "commit", "-qm", "big")


def test_review_capped_diff_without_files_read_is_rejected(
    repo: Path, tmp_path: Path
) -> None:
    """The prompt carried a file list only; a verdict that names no file it
    read judged code it never opened (gb-1ac7, 05.11: 74 files, pass in 18 s)."""
    b, f = board(repo, "true"), Fake()
    wt = setup(repo, tmp_path, "review", b, f)
    _big_commit(wt)
    (wt / ".pump/review.md").write_text("## Verdict\npass\n## Remarks\n")
    f.tick(b, tmp_path)
    assert st(b)["stage"] == "stopped"
    assert "did not read the code" in find_card(b, "dv-0001").body


def test_review_capped_diff_with_files_read_passes(repo: Path, tmp_path: Path) -> None:
    b, f = board(repo, "true"), Fake()
    wt = setup(repo, tmp_path, "review", b, f)
    _big_commit(wt)
    (wt / ".pump/review.md").write_text(
        "## Verdict\npass\n## Remarks\n## Files read\n- big.py\n"
    )
    assert "review done -> merge" in f.tick(b, tmp_path)[0]
    assert st(b)["stage"] == "merge"
    # a small diff never needs the section (the code was in the prompt)
    assert review.capped_names(b, wt) is not None
    assert review.parse("## Verdict\npass\n## Remarks\n") == ("pass", "")


@pytest.mark.parametrize(
    "text", ["", "## Verdict\nmaybe\n## Remarks\n", "## Verdict\nreturn\n## Remarks\n"]
)
def test_review_rejects(repo: Path, tmp_path: Path, text: str) -> None:
    b, f = board(repo, "true"), Fake()
    wt = setup(repo, tmp_path, "review", b, f)
    if text:
        (wt / ".pump/review.md").write_text(text)
    f.tick(b, tmp_path)
    assert st(b)["stage"] == "stopped"
    assert "review" in find_card(b, "dv-0001").body


def test_review_return_build_remarks_rereview_second_return(
    repo: Path, tmp_path: Path
) -> None:
    b, f = board(repo, "true"), Fake()
    wt = setup(repo, tmp_path, "review", b, f)
    verdict(wt, "return", "1. src/a.py: handle None")
    f.tick(b, tmp_path)
    assert st(b)["stage"] == "build" and st(b)["returns"] == 1
    assert "src/a.py: handle None" in find_card(b, "dv-0001").body
    f.tick(b, tmp_path)  # build relaunch
    assert "Review remarks to address" in f.launched[-1][3]
    assert "src/a.py: handle None" in f.launched[-1][3]
    # build finished; card goes back to review
    from src.pump2.state import set_pump2

    set_pump2(repo / TASKS / "dv-0001.md", stage="review", job=None)
    f.tick(b, tmp_path)
    assert "earlier review.md" in f.launched[-1][3]
    assert (wt / ".pump/review.prev.md").exists()
    verdict(wt, "return", "1. src/b.py: still wrong")
    f.tick(b, tmp_path)
    assert st(b)["stage"] == "stopped"
    assert "two returns, stopped for the owner" in find_card(b, "dv-0001").body


# ---- board guard -----------------------------------------------------------
def test_default_boards_file_is_guarded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    from src.pump2 import cli
    from src.pump2.state import load_boards

    f = tmp_path / "b.json"
    f.write_text(
        json.dumps(
            {
                "boards": [
                    {"name": "old", "repo": str(tmp_path / "a")},
                    {"name": "off", "repo": str(tmp_path / "b"), "pump2": False},
                    {"name": "on", "repo": str(tmp_path / "c"), "pump2": True},
                ]
            }
        )
    )
    assert list(load_boards(f, guard=True)) == ["on"]
    assert list(load_boards(f)) == ["old", "off", "on"]  # --boards override
    # the CLI: default file guarded (none flagged -> nothing touched), override not
    monkeypatch.setattr(cli, "DEFAULT_BOARDS", f)
    (tmp_path / "c" / ".deviz" / "tasks").mkdir(parents=True)
    assert cli.main(["status"]) == 0
    assert cli.main(["--boards", str(f), "status", "on"]) == 0


def test_home_board_comes_from_pump2_home(tmp_path: Path) -> None:
    """This repo's board is the top-level ``pump2_home`` object, never a
    ``boards`` entry (the old pump has it built in and would serve a listed
    copy twice). Present = enabled, guard or not; absent = no home board."""
    import json

    from src.pump2.state import HOME_BOARD, HOME_REPO, load_boards

    f = tmp_path / "b.json"
    f.write_text(json.dumps({"boards": [{"name": "x", "repo": str(tmp_path)}]}))
    assert HOME_BOARD not in load_boards(f, guard=True)
    f.write_text(
        json.dumps(
            {
                "pump2_home": {
                    "profile": "devops",
                    "pump2_test": "/v/bin/pytest -m core -q",
                    "pump2_branch": "main",
                },
                "boards": [{"name": "x", "repo": str(tmp_path)}],
            }
        )
    )
    boards = load_boards(f, guard=True)
    assert list(boards) == [HOME_BOARD]
    home = boards[HOME_BOARD]
    assert home.repo == HOME_REPO and home.profile == "devops"
    assert home.test_python() == "/v/bin/python" and home.branch == "main"
