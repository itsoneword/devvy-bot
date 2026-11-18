"""dv-65aa: card ids never collide across branches and agents. One creator
(`src/pump2/cards.py::create_card`), exclusive create, stage follow-ups filed
by pump2 at land, add/add card files re-filed under a fresh id at merge."""

from __future__ import annotations

import concurrent.futures as cf
import json
import re
import secrets
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.pump2 import cards
from src.pump2.merge import ensure_worktree, merge
from src.pump2.state import Board, find_card, list_cards, read_card  # noqa: F401

from .test_pump2_merge_resolve import (  # noqa: F401
    CID,
    TASKS,
    Rig,
    commit_file,
    git,
    repo,
    writes,
)


def card_text(cid: str, title: str) -> str:
    return (
        f"---\nid: {cid}\ntitle: {title}\nstatus: todo\ndeps: []\n---\n\n"
        "## Description\n\nd\n\n## Comments\n"
    )


def forced(*ids: str):  # type: ignore[no-untyped-def]
    """A random source that yields these 4-hex suffixes, then fresh ones."""
    it = iter(ids)

    def rand() -> str:
        try:
            return next(it)
        except StopIteration:
            return secrets.token_hex(2)

    return rand


def make(board: Board, title: str, rand=None, **kw):  # type: ignore[no-untyped-def]
    return cards.create_card(board, title, rand=rand, **kw)


def bare(repo: Path) -> Board:  # noqa: F811
    return Board(name="t", repo=repo, test="true")


# ---------------------------------------------------------------- C1


def test_C1_id_avoids_live_integration_and_pump2_branches(
    repo: Path,  # noqa: F811
) -> None:  # noqa: F811
    git(repo, "checkout", "-q", "-b", "pump2/x")
    commit_file(repo, f"{TASKS}/dv-aaaa.md", card_text("dv-aaaa", "branch"))
    git(repo, "checkout", "-q", "main")
    commit_file(repo, f"{TASKS}/dv-bbbb.md", card_text("dv-bbbb", "main"))
    (repo / TASKS / "archive").mkdir()
    (repo / TASKS / "archive" / "dv-cccc.md").write_text(card_text("dv-cccc", "a"))
    # live-only (uncommitted)
    (repo / TASKS / "dv-dddd.md").write_text(card_text("dv-dddd", "live"))
    # an id that exists on the integration branch only, removed from the live tree
    commit_file(repo, f"{TASKS}/dv-eeee.md", card_text("dv-eeee", "gone"))
    (repo / TASKS / "dv-eeee.md").unlink()

    rand = forced("aaaa", "bbbb", "cccc", "dddd", "eeee", "1234")
    out = make(bare(repo), "new one", rand=rand)
    cid = out if isinstance(out, str) else getattr(out, "id", str(out))
    assert cid == "dv-1234"
    assert (repo / TASKS / "dv-1234.md").exists()
    assert "new one" in (repo / TASKS / "dv-1234.md").read_text()


def test_C1_cli_new_prints_board_and_id(repo: Path, tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    import src.pump2.cli as cli

    bj = tmp_path / "boards.json"
    bj.write_text(
        json.dumps({"boards": [{"name": "t", "repo": str(repo), "pump2": True}]})
    )
    rc = cli.main(["--boards", str(bj), "new", "t", "--title", "from cli"])
    assert rc == 0
    m = re.search(r"t:(dv-[0-9a-f]{4})", capsys.readouterr().out)
    assert m and (repo / TASKS / f"{m.group(1)}.md").exists()


# ---------------------------------------------------------------- C2


def test_C2_existing_file_is_never_overwritten(repo: Path) -> None:  # noqa: F811
    f = repo / TASKS / "dv-aaaa.md"
    f.write_text(card_text("dv-aaaa", "keep me"))
    before = f.read_bytes()
    out = make(bare(repo), "other", rand=forced("aaaa", "aaaa", "bbbb"))
    assert f.read_bytes() == before
    assert "dv-bbbb" in str(out)
    assert (repo / TASKS / "dv-bbbb.md").exists()


def test_C2_parallel_creates_are_distinct(repo: Path) -> None:  # noqa: F811
    b = bare(repo)
    with cf.ThreadPoolExecutor(8) as ex:
        res = list(ex.map(lambda i: make(b, f"card {i}"), range(20)))
    ids = {str(r if isinstance(r, str) else getattr(r, "id", r)) for r in res}
    assert len(ids) == 20
    files = [p for p in (repo / TASKS).glob("dv-*.md") if p.name != f"{CID}.md"]
    assert len(files) == 20


# ---------------------------------------------------------------- C3


def test_C3_devizboard_add_task_uses_the_same_primitive(
    repo: Path, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    import asyncio

    from src.storage.deviz_board import DevizBoard

    git(repo, "checkout", "-q", "-b", "pump2/x")
    commit_file(repo, f"{TASKS}/dv-aaaa.md", card_text("dv-aaaa", "branch only"))
    git(repo, "checkout", "-q", "main")
    seq = iter(["aaaa", "7777"])
    monkeypatch.setattr(secrets, "token_hex", lambda n=2: next(seq))
    tid = asyncio.run(DevizBoard(repo / TASKS).add_task("via devvy board"))
    assert tid == "dv-7777"


def test_C3_task_add_own_board_files_on_the_callers_board(
    repo: Path, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    import asyncio

    from src.claude import agent_tools as at

    board = bare(repo)
    monkeypatch.setattr(at.boards_mod, "board_of_profile", lambda p, *a, **k: "t")
    monkeypatch.setattr(at, "load_boards", lambda *a, **k: {"t": board})
    spec = next(s for s in at.AGENT_TOOL_SPECS if s.name == "task_add")
    assert "own_board" in json.dumps(spec.input_schema, default=str)
    ctx = SimpleNamespace(profile_slug="acme", role="owner", user_id=1)
    res = asyncio.run(
        at.task_add(
            SimpleNamespace(task_repo=None),
            ctx,  # type: ignore[arg-type]
            {"title": "own board card", "own_board": True},
        )
    )
    text = json.dumps(res, default=str)
    m = re.search(r"dv-[0-9a-f]{4}", text)
    assert m, text
    assert "own board card" in (repo / TASKS / f"{m.group(0)}.md").read_text()


# ---------------------------------------------------------------- C4


def test_C4_build_prompt_points_at_followups_json() -> None:
    src = Path("src/pump2/stages/build.py").read_text()
    assert ".pump/followups.json" in src or "followups.json" in src


def test_C4_followups_filed_once_after_fast_forward(
    repo: Path, tmp_path: Path  # noqa: F811
) -> None:  # noqa: F811
    git(repo, "checkout", "-q", "-b", "pump2/x")
    commit_file(repo, "b.txt", "b\n", "work")
    git(repo, "checkout", "-q", "main")
    rig = Rig(repo, tmp_path, writes("land"))
    wt = ensure_worktree(rig.board, CID, "pump2/x")
    (wt / ".pump").mkdir(exist_ok=True)
    (wt / ".pump" / "followups.json").write_text(
        json.dumps([{"title": "follow one", "body": "b1"}, {"title": "follow two"}])
    )
    assert rig.run() == "done"
    filed = [c for c in list_cards(rig.board)[0] if c.id != CID]
    assert sorted(c.fm.get("title", "") for c in filed) == ["follow one", "follow two"]
    for c in filed:
        assert c.fm.get("status") == "todo" and c.fm.get("parent") == CID
        assert c.id in rig.body
    n = len(list(rig.repo.joinpath(TASKS).glob("*.md")))
    # a merge retry does not file them again
    merge(rig.board, find_card(rig.board, CID), locks=tmp_path / "l")
    assert len(list(rig.repo.joinpath(TASKS).glob("*.md"))) == n


# ---------------------------------------------------------------- C5


def _gb1b3b(repo: Path, live_only: bool) -> None:  # noqa: F811
    """Master has card dv-1b3c (title MASTER); the branch adds the same path
    with another title."""
    old = f"{TASKS}/dv-1b3c.md"
    git(repo, "checkout", "-q", "-b", "pump2/x")
    commit_file(repo, old, card_text("dv-1b3c", "BRANCH design"), "branch card")
    git(repo, "checkout", "-q", "main")
    if live_only:  # on the live tree only, not committed on main
        (repo / old).write_text(card_text("dv-1b3c", "MASTER monetization"))
    else:
        commit_file(repo, old, card_text("dv-1b3c", "MASTER monetization"), "m")


@pytest.mark.parametrize("live_only", [False, True])
def test_C5_added_card_with_taken_id_is_refiled(
    repo: Path, tmp_path: Path, live_only: bool  # noqa: F811
) -> None:
    _gb1b3b(repo, live_only)
    rig = Rig(repo, tmp_path, writes("land"))
    assert rig.run() == "done"
    allc = {c.id: c for c in list_cards(rig.board)[0]}
    assert allc["dv-1b3c"].fm["title"] == "MASTER monetization"
    moved = [c for c in allc.values() if c.fm.get("title") == "BRANCH design"]
    assert len(moved) == 1 and moved[0].id != "dv-1b3c"
    assert re.fullmatch(r"dv-[0-9a-f]{4}", moved[0].id)
    assert f"id: {moved[0].id}" in moved[0].path.read_text()
    body = rig.body
    assert "dv-1b3c" in body and moved[0].id in body
    assert "/pump2 — " in body


# ---------------------------------------------------------------- C8


def test_C8_free_id_branch_card_kept_and_modified_card_integration_wins(
    repo: Path, tmp_path: Path  # noqa: F811
) -> None:
    other = f"{TASKS}/dv-0002.md"
    commit_file(repo, other, card_text("dv-0002", "same"))
    git(repo, "checkout", "-q", "-b", "pump2/x")
    (repo / other).write_text(card_text("dv-0002", "same") + "branch edit\n")
    commit_file(repo, f"{TASKS}/dv-0003.md", card_text("dv-0003", "new"), "add")
    git(repo, "checkout", "-q", "main")
    commit_file(repo, other, card_text("dv-0002", "same") + "main edit\n", "m")
    rig = Rig(repo, tmp_path, writes("land"))
    assert rig.run() == "done"
    assert "main edit" in (repo / other).read_text()
    assert "branch edit" not in (repo / other).read_text()
    assert (repo / TASKS / "dv-0003.md").exists()
    assert (
        subprocess.run(
            ["git", "cat-file", "-e", "main:" + f"{TASKS}/dv-0003.md"], cwd=repo
        ).returncode
        == 0
    )
