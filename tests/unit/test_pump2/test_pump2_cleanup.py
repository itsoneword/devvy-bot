"""dv-8bf8: the tick removes the worktree of a done card (plain
`git worktree remove`, no force), keeps a dirty one with one note, and never
deletes a branch."""

import subprocess
from pathlib import Path
from unittest import mock

from src.pump2 import runner
from src.pump2.state import WORKTREES, find_card, set_keys, set_pump2

from .test_pump2 import TASKS, board, card, git
from .test_pump2 import repo as repo  # noqa: F401


def _tick(repo: Path, tmp_path: Path) -> list[str]:
    return runner.tick({"t": board(repo, "true")}, locks=tmp_path / "locks")


def _wt(repo: Path, name: str) -> Path:
    return repo / WORKTREES / name


def _ignore(repo: Path) -> None:
    (repo / ".gitignore").write_text(".pump/\n.pump-worktrees/\n__pycache__/\n")
    git(repo, "add", ".gitignore")
    git(repo, "commit", "-qm", "ignore")


def _done(repo: Path, cid: str, stage: str = "done", status: str = "done") -> Path:
    """A card whose worktree exists on branch pump2/<cid>, with a .pump dir."""
    p = card(repo, cid, status=status)
    set_pump2(p, stage=stage, branch=f"pump2/{cid}")
    git(repo, "branch", f"pump2/{cid}")
    git(repo, "worktree", "add", "-q", str(_wt(repo, cid)), f"pump2/{cid}")
    (_wt(repo, cid) / ".pump").mkdir()
    (_wt(repo, cid) / ".pump" / "plan.md").write_text("plan\n")
    return p


def _listed(repo: Path) -> str:
    return git(repo, "worktree", "list", "--porcelain")


def _notes(repo: Path, cid: str) -> int:
    return find_card(board(repo, "true"), cid).body.count("worktree kept")


def test_C1_clean_done_worktree_removed_branch_kept(
    repo: Path, tmp_path: Path  # noqa: F811
) -> None:
    _ignore(repo)
    _done(repo, "dv-0002")
    sha = git(repo, "rev-parse", "pump2/dv-0002")
    (_wt(repo, "dv-0002") / "__pycache__").mkdir()
    (_wt(repo, "dv-0002") / "__pycache__" / "x.pyc").write_text("x")
    _tick(repo, tmp_path)
    assert not _wt(repo, "dv-0002").exists()
    assert str(_wt(repo, "dv-0002")) not in _listed(repo)
    assert git(repo, "rev-parse", "pump2/dv-0002") == sha


def test_C1_untracked_pump_dir_is_deleted_first(
    repo: Path, tmp_path: Path  # noqa: F811
) -> None:
    # no .gitignore line: .pump/ shows as untracked and must not block removal
    _done(repo, "dv-0002")
    _tick(repo, tmp_path)
    assert not _wt(repo, "dv-0002").exists()
    assert git(repo, "rev-parse", "--verify", "pump2/dv-0002")


def test_C2_dirty_worktree_kept_with_one_note(
    repo: Path, tmp_path: Path  # noqa: F811
) -> None:
    _ignore(repo)
    for cid, mutate in (
        ("dv-0002", lambda w: (w / "a.txt").write_text("changed\n")),
        ("dv-0003", lambda w: (w / "new.txt").write_text("new\n")),
        (
            "dv-0004",
            lambda w: ((w / "s.txt").write_text("s\n"), git(w, "add", "s.txt")),
        ),
    ):
        _done(repo, cid)
        mutate(_wt(repo, cid))
    _tick(repo, tmp_path)
    _tick(repo, tmp_path)
    _tick(repo, tmp_path)
    for cid, fname in (
        ("dv-0002", "a.txt"),
        ("dv-0003", "new.txt"),
        ("dv-0004", "s.txt"),
    ):
        wt = _wt(repo, cid)
        assert wt.exists() and (wt / fname).exists()
        assert (wt / ".pump" / "plan.md").read_text() == "plan\n"
        assert _notes(repo, cid) == 1
        assert fname in find_card(board(repo, "true"), cid).body
    assert (_wt(repo, "dv-0002") / "a.txt").read_text() == "changed\n"


def test_C2_note_repeats_only_for_a_new_file_set(
    repo: Path, tmp_path: Path  # noqa: F811
) -> None:
    _ignore(repo)
    _done(repo, "dv-0002")
    wt = _wt(repo, "dv-0002")
    (wt / "one.txt").write_text("1")
    _tick(repo, tmp_path)
    (wt / "two.txt").write_text("2")
    _tick(repo, tmp_path)
    _tick(repo, tmp_path)
    assert _notes(repo, "dv-0002") == 2


def test_C3_never_force_never_branch_delete(
    repo: Path, tmp_path: Path  # noqa: F811
) -> None:
    _ignore(repo)
    _done(repo, "dv-0002")
    calls: list[list[str]] = []
    real = subprocess.run

    def spy(argv: object, *a: object, **kw: object) -> object:
        if isinstance(argv, (list, tuple)):
            calls.append([str(x) for x in argv])
        return real(argv, *a, **kw)  # type: ignore[call-overload]

    with mock.patch("subprocess.run", spy):
        _tick(repo, tmp_path)
    assert not _wt(repo, "dv-0002").exists()
    rm = [c for c in calls if "worktree" in c and "remove" in c]
    assert rm
    for c in rm:
        assert "--force" not in c and "-f" not in c
    assert not [c for c in calls if "branch" in c and ("-d" in c or "-D" in c)]
    assert not [c for c in calls if "prune" in c]


def test_C3_only_done_cards_and_no_foreign_dirs(
    repo: Path, tmp_path: Path  # noqa: F811
) -> None:
    _ignore(repo)
    _done(repo, "dv-0002", stage="stopped", status="in_progress")
    _done(repo, "dv-0003", stage="merge", status="in_progress")
    _done(repo, "dv-0004", stage="build", status="waiting_user")
    for name in ("_integration", "dv-0099"):  # integration checkout, orphan dir
        git(repo, "worktree", "add", "-q", "--detach", str(_wt(repo, name)), "main")
    _tick(repo, tmp_path)
    for name in ("dv-0002", "dv-0003", "dv-0004", "_integration", "dv-0099"):
        assert _wt(repo, name).exists(), name


def _design(repo: Path, picked: str = "v2") -> Path:
    p = card(repo, "dv-0010", status="done")
    set_keys(
        p,
        labels="[designloop]",
        design_state="picked",
        design_pick=picked,
        design_variants="[design/dv-0010/v1, design/dv-0010/v2, design/dv-0010/v3]",
    )
    for v in ("v1", "v2", "v3"):
        git(repo, "branch", f"design/dv-0010/{v}")
        git(
            repo,
            "worktree",
            "add",
            "-q",
            str(_wt(repo, f"dv-0010-{v}")),
            f"design/dv-0010/{v}",
        )
    git(
        repo,
        "worktree",
        "add",
        "-q",
        "--detach",
        str(_wt(repo, "dv-0010-current")),
        "main",
    )
    return p


def test_C4_design_variants_removed_pick_waits_for_dependents(
    repo: Path, tmp_path: Path  # noqa: F811
) -> None:
    _ignore(repo)
    _design(repo)
    dep = card(repo, "dv-0011", status="in_progress", deps="[dv-0010]")
    _tick(repo, tmp_path)
    assert not _wt(repo, "dv-0010-v1").exists()
    assert not _wt(repo, "dv-0010-v3").exists()
    assert not _wt(repo, "dv-0010-current").exists()
    assert _wt(repo, "dv-0010-v2").exists()
    card(repo, "dv-0011", status="done", deps="[dv-0010]")
    assert dep.exists()
    _tick(repo, tmp_path)
    assert not _wt(repo, "dv-0010-v2").exists()
    for v in ("v1", "v2", "v3"):
        assert git(repo, "rev-parse", "--verify", f"design/dv-0010/{v}")


def test_C4_pick_without_dependents_removed_and_dirty_variant_kept(
    repo: Path, tmp_path: Path  # noqa: F811
) -> None:
    _ignore(repo)
    _design(repo)
    (_wt(repo, "dv-0010-v3") / "mine.txt").write_text("keep")
    _tick(repo, tmp_path)
    assert not _wt(repo, "dv-0010-v2").exists()
    assert (_wt(repo, "dv-0010-v3") / "mine.txt").exists()


def test_C4_design_card_not_done_is_untouched(
    repo: Path, tmp_path: Path  # noqa: F811
) -> None:
    _ignore(repo)
    p = _design(repo)
    set_keys(p, status="in_progress")
    _tick(repo, tmp_path)
    for n in ("v1", "v2", "v3", "current"):
        assert _wt(repo, f"dv-0010-{n}").exists()


def test_C5_git_failure_does_not_stop_tick_or_other_cards(
    repo: Path, tmp_path: Path  # noqa: F811
) -> None:
    _ignore(repo)
    _done(repo, "dv-0002")
    _done(repo, "dv-0003")
    (_wt(repo, "dv-0002") / ".git").write_text("gitdir: /nonexistent\n")  # broken
    out = _tick(repo, tmp_path)
    assert _wt(repo, "dv-0002").exists()
    assert not _wt(repo, "dv-0003").exists()
    assert any("dv-0002" in ln for ln in out)


def test_C5_runs_inside_tick_for_every_board_card(
    repo: Path, tmp_path: Path  # noqa: F811
) -> None:
    _ignore(repo)
    for i in range(2, 6):
        _done(repo, f"dv-000{i}")
    out = _tick(repo, tmp_path)
    assert all(not _wt(repo, f"dv-000{i}").exists() for i in range(2, 6))
    assert sum("worktree removed" in ln for ln in out) == 4
    assert (repo / TASKS / "dv-0002.md").exists()


def test_C6_done_card_without_worktree_renders(
    repo: Path, tmp_path: Path  # noqa: F811
) -> None:
    from src.pump2 import render

    _ignore(repo)
    _done(repo, "dv-0002")
    _tick(repo, tmp_path)
    c = find_card(board(repo, "true"), "dv-0002")
    assert render.last_line(board(repo, "true"), c) in ("", None)
    _tick(repo, tmp_path)
