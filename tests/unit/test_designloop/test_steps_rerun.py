"""dv-af52: a rerun brings reused variant branches up to the current base."""

import subprocess
from pathlib import Path

from tests.unit.test_designloop.test_steps import (  # noqa: F401
    CARD,
    Runner,
    _git,
    repo,
    steps,
)


def _commit(cwd: Path, name: str, text: str, msg: str) -> None:
    (cwd / name).write_text(text)
    _git(cwd, "add", "-A")
    _git(cwd, "commit", "-qm", msg)


def _is_ancestor(repo: Path, a: str, b: str) -> bool:
    return (
        subprocess.run(
            ["git", "merge-base", "--is-ancestor", a, b], cwd=repo
        ).returncode
        == 0
    )


def _wt(tmp_path: Path, n: int) -> Path:
    return tmp_path / "wt" / "app" / f"{CARD}-v{n}"


def test_C1_rerun_merges_new_base_keeping_variant_work(repo, tmp_path):  # noqa: F811
    s = steps(repo, tmp_path, Runner())
    _commit(_wt(tmp_path, 1), "mine.txt", "work\n", "v1 work")
    _commit(repo, "recipe.txt", "new\n", "base moves")
    base = _git(repo, "rev-parse", "HEAD")
    s2 = steps(repo, tmp_path, Runner())
    for n in (1, 2, 3):
        assert _is_ancestor(repo, base, f"design/{CARD}/v{n}")
    assert (_wt(tmp_path, 1) / "recipe.txt").exists()
    assert (_wt(tmp_path, 1) / "mine.txt").exists()
    assert not getattr(s2, "fresh", [])
    # up to date: a further rerun adds no commit
    tip = _git(repo, "rev-parse", f"design/{CARD}/v1")
    steps(repo, tmp_path, Runner())
    assert _git(repo, "rev-parse", f"design/{CARD}/v1") == tip
    assert s is not None


def test_C1_rerun_updates_a_branch_without_a_worktree(repo, tmp_path):  # noqa: F811
    steps(repo, tmp_path, Runner())
    _commit(_wt(tmp_path, 2), "mine.txt", "work\n", "v2 work")
    _git(repo, "worktree", "remove", "--force", str(_wt(tmp_path, 2)))
    _commit(repo, "recipe.txt", "new\n", "base moves")
    base = _git(repo, "rev-parse", "HEAD")
    s = steps(repo, tmp_path, Runner())
    assert _is_ancestor(repo, base, f"design/{CARD}/v2")
    assert "mine.txt" in _git(repo, "ls-tree", "--name-only", f"design/{CARD}/v2")
    assert not getattr(s, "fresh", [])


def test_C2_conflict_restarts_variant_from_base(repo, tmp_path):  # noqa: F811
    steps(repo, tmp_path, Runner())
    _commit(_wt(tmp_path, 2), "clash.txt", "variant\n", "v2 clash")
    _commit(_wt(tmp_path, 1), "mine.txt", "work\n", "v1 work")
    _commit(repo, "clash.txt", "main\n", "base clash")
    base = _git(repo, "rev-parse", "HEAD")
    s = steps(repo, tmp_path, Runner())
    wt = _wt(tmp_path, 2)
    assert _git(repo, "rev-parse", f"design/{CARD}/v2") == base
    assert _git(wt, "status", "--porcelain") == ""
    assert (wt / "clash.txt").read_text() == "main\n"
    assert not (Path(_git(wt, "rev-parse", "--git-dir")) / "MERGE_HEAD").exists()
    assert any("v2" in line for line in s.fresh)
    assert not any("v1" in line for line in s.fresh)
    assert (_wt(tmp_path, 1) / "mine.txt").exists()
