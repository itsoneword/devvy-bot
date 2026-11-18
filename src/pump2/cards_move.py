"""``pump2 cards-move <board> [--apply]``: move a board's cards out
of its project repo into the install's private boards repo.

``<repo>/.deviz/tasks`` (live cards, ``archive/``) and ``.deviz/config.json``
are copied to ``<boards repo>/<board>/.deviz/``, committed there in one commit
naming the source repo and its HEAD sha, removed from the project repo in one
commit that touches only card paths, and the board's ``cards_dir`` is written
to boards.json (a backup is kept). Dry run by default; a second run is a
no-op. Nothing is pushed and no history is rewritten: the old cards stay
readable in the project repo's git history.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.utils.boards import TASKS_REL, boards_repo, default_tasks_dir

from . import cards, runner
from .state import HOME_BOARD, Board, list_cards

HISTORY_NOTE = (
    "Old cards stay readable in {repo}'s git history; nothing is purged "
    "(no history rewrite)."
)


def _git(repo: Path, *args: str, env: dict[str, str] | None = None) -> tuple[int, str]:
    p = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        env={**os.environ, **env} if env else None,
    )
    return p.returncode, (p.stdout if p.returncode == 0 else p.stdout + p.stderr)


def _files(d: Path) -> dict[str, bytes]:
    """Every file under ``d`` (relative posix path -> bytes)."""
    if not d.is_dir():
        return {}
    return {
        p.relative_to(d).as_posix(): p.read_bytes()
        for p in sorted(d.rglob("*"))
        if p.is_file()
    }


def _tracked(repo: Path) -> list[str]:
    rc, out = _git(repo, "ls-files", "--", TASKS_REL.as_posix())
    return out.split() if rc == 0 else []


def _running(board: Board) -> list[str]:
    """Cards of ``board`` whose pump job is still running."""
    out = []
    for card in list_cards(board)[0]:
        job = card.pump2.get("job")
        if job and runner.bgjob_status(str(job))[0]:
            out.append(f"{card.id} ({card.pump2.get('stage')}, job {job})")
    return out


def _remove_commit(repo: Path, paths: list[str], msg: str) -> str:
    """One commit on HEAD removing ``paths`` and nothing else, built in a
    temporary index (other sessions' staged work is never swept in).
    '' = done, else the error."""
    with tempfile.TemporaryDirectory() as tmp:
        env = {"GIT_INDEX_FILE": str(Path(tmp) / "index")}
        steps: list[list[str]] = [
            ["read-tree", "HEAD"],
            ["rm", "-r", "-q", "--cached", "--", *paths],
        ]
        for argv in steps:
            rc, out = _git(repo, *argv, env=env)
            if rc:
                return out
        rc, tree = _git(repo, "write-tree", env=env)
        if rc:
            return tree
    rc, head = _git(repo, "rev-parse", "HEAD")
    if rc:
        return head
    ident = cards._identity(repo)
    p = subprocess.run(
        ["git", *ident, "commit-tree", tree.strip(), "-p", head.strip(), "-m", msg],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    if p.returncode:
        return p.stdout + p.stderr
    rc, out = _git(repo, "update-ref", "HEAD", p.stdout.strip(), head.strip())
    if rc:
        return out
    # the live index follows: the removed paths are no longer staged
    _git(repo, "rm", "-r", "-q", "-f", "--cached", "--ignore-unmatch", "--", *paths)
    return ""


def _write_config(path: Path, name: str, dest: Path) -> Path:
    """Set the board's ``cards_dir``; returns the backup file."""
    data: dict[str, Any] = json.loads(path.read_text())
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup = path.with_name(f"{path.name}.bak-{stamp}")
    shutil.copy2(path, backup)
    if name == HOME_BOARD and isinstance(data.get("pump2_home"), dict):
        data["pump2_home"]["cards_dir"] = str(dest)
    else:
        entries = data.get("boards")
        if isinstance(entries, dict):
            entries[name]["cards_dir"] = str(dest)
        else:
            for e in entries or []:
                if isinstance(e, dict) and e.get("name") == name:
                    e["cards_dir"] = str(dest)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    tmp.replace(path)
    return backup


def run(path: Path, boards: dict[str, Board], name: str, *, apply: bool) -> int:
    board = boards.get(name)
    if board is None:
        print(f"cards-move: no board {name!r} in {path}", file=sys.stderr)
        return 2
    repo = board.repo
    old = default_tasks_dir(repo)
    folder = boards_repo(path) / name
    dest = folder / TASKS_REL
    cfg_src, cfg_dst = old.parent / "config.json", dest.parent / "config.json"
    tracked = _tracked(repo)
    have = _files(old)
    extra = f"extra_dirs line for profile {board.profile!r}: - {folder}"

    if board.tasks == dest and not tracked and not have:
        print(f"cards-move: {name} already moved to {dest}; nothing to do")
        return 0
    if board.tasks != old and board.tasks != dest:
        print(
            f"cards-move: {name}'s cards_dir is {board.tasks}, neither {old} nor "
            f"{dest}; fix boards.json by hand",
            file=sys.stderr,
        )
        return 2
    rc, head = _git(repo, "rev-parse", "HEAD")
    head = head.strip() if rc == 0 else "(no commits)"
    plan = [
        f"cards-move {name}: {len(have)} file(s), {sum(map(len, have.values()))} "
        f"bytes from {old} -> {dest}",
        f"  1. commit in {folder.parent}: '{name}: cards from {repo} @ {head}'",
        f"  2. commit in {repo}: remove {len(tracked)} tracked card file(s) "
        + ("(none: no removal commit)" if not tracked else ""),
        f"  3. boards.json: {name}.cards_dir = {dest} (backup kept)",
    ]
    if not apply:
        print("\n".join(plan + ["dry run: nothing written (--apply to move)"]))
        return 0
    busy = _running(board)
    if busy:
        print(
            "cards-move: refused, pump jobs still running on "
            + ", ".join(busy)
            + " -- wait for them (or freeze the board) and rerun",
            file=sys.stderr,
        )
        return 1
    there = _files(dest)
    clash = [f for f, b in there.items() if f in have and have[f] != b]
    clash += [f for f in there if f not in have]
    if clash:
        print(
            f"cards-move: {dest} already holds other files ({clash[:5]}); "
            "refusing to merge two boards",
            file=sys.stderr,
        )
        return 1

    # 1. copy and verify (count + bytes)
    br = folder.parent
    br.mkdir(parents=True, exist_ok=True)
    if _git(br, "rev-parse", "--show-toplevel")[0]:
        rc, out = _git(br, "init", "-q", "-b", "main")
        if rc:
            print(f"cards-move: git init {br} failed: {out}", file=sys.stderr)
            return 1
    for rel, data in have.items():
        p = dest / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    dest.mkdir(parents=True, exist_ok=True)
    if cfg_src.is_file() and not cfg_dst.exists():
        shutil.copy2(cfg_src, cfg_dst)
    if _files(dest) != have:
        print(
            f"cards-move: copy to {dest} does not match; nothing removed",
            file=sys.stderr,
        )
        return 1
    msg = f"{name}: cards from {repo} @ {head}\n\npump2 cards-move"
    rc, out = cards.git_commit_paths(br, [name], msg)
    if rc and _git(br, "status", "--porcelain", "--", name)[1].strip():
        print(f"cards-move: commit in {br} failed: {out}", file=sys.stderr)
        return 1

    # 2. the project repo: one commit removing the tracked card files
    if tracked:
        err = _remove_commit(
            repo,
            [TASKS_REL.as_posix()],
            f"{name}: cards moved to the boards repo ({br})\n\n"
            f"pump2 cards-move; history is kept, nothing purged",
        )
        if err:
            print(
                f"cards-move: removal commit in {repo} failed: {err}", file=sys.stderr
            )
            return 1
    shutil.rmtree(old, ignore_errors=True)

    # 3. switch the config
    backup = _write_config(path, name, dest)
    print("\n".join(plan))
    print(f"moved: {len(have)} file(s) now in {dest}; boards.json backup {backup}")
    print(
        f"add to config/profiles.yaml ({extra}), then run "
        "scripts/check_install.py --preflight and restart_bot"
    )
    print(HISTORY_NOTE.format(repo=repo))
    return 0
