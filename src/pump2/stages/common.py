"""Shared by the tests and build stages: git status of the card worktree,
tests.json, the one commit this code makes on the card branch."""

from __future__ import annotations

import json
from pathlib import Path

from ..merge import sh
from ..state import Outcome


def changed_files(wt: Path) -> list[str]:
    """Paths new or changed vs HEAD (staged, unstaged, untracked); .pump/ and
    other ignored paths do not show."""
    rc, out = sh(
        ["git", "-c", "status.renames=false", "status", "--porcelain", "-z", "-uall"],
        wt,
    )
    if rc:
        raise RuntimeError(f"git status failed: {out}")
    return sorted(e[3:] for e in out.split("\0") if len(e) > 3)


def read_tests_json(wt: Path) -> tuple[list[str], str]:
    """(files, error); error is '' when tests.json is valid."""
    f = wt / ".pump" / "tests.json"
    if not f.exists():
        return [], f"{f} was not written"
    raw = f.read_text()
    try:
        files = json.loads(raw)["files"]
        if not isinstance(files, list) or not all(isinstance(x, str) for x in files):
            raise TypeError("files must be a list of strings")
    except (ValueError, KeyError, TypeError) as e:
        return [], f"tests.json rejected ({e!r}); content:\n{raw.rstrip()}"
    return files, ""


def commit(wt: Path, message: str, files: list[str] | None = None) -> tuple[str, str]:
    """Commit ``files`` (all changes when None) on the card branch.
    Returns (sha, error)."""
    rc, out = sh(["git", "add", "--", *files] if files else ["git", "add", "-A"], wt)
    if rc:
        return "", f"git add failed: {out}"
    cmd = ["git", "commit", "-q", "-m", message] + (["--", *files] if files else [])
    rc, out = sh(cmd, wt)
    if rc:
        return "", f"git commit failed: {out}"
    _, sha = sh(["git", "rev-parse", "HEAD"], wt)
    return sha.strip(), ""


def stopped(msg: str) -> Outcome:
    return Outcome("stopped", msg)
