"""``pump2 doctor``: will the pump pick this board up? (dv-55b7).

One line per problem, each ending in a fix hint. Read-only and fast: git
probes, ``<pump2_python> -c pass`` and a PATH lookup of the test command's
first token -- never the test command itself. Also the "pump2 boards" step of
``scripts/check_install.py`` (src/boot/check.py).
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from .state import Board, load_boards

TIMEOUT = 30
_LOAD: Any = object()  # check_boards: load the registry the way boot does


def _run(argv: list[str]) -> bool:
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0


def _git(repo: Path, *args: str) -> bool:
    return _run(["git", "-C", str(repo), *args])


def _top(repo: Path) -> Path | None:
    try:
        r = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return Path(r.stdout.strip()).resolve() if r.returncode == 0 else None


def _inside(path: Path, roots: list[Path]) -> bool:
    return any(path == r or r in path.parents for r in roots)


def load_registry() -> tuple[Any, str]:
    """(registry or None, note). None = skip the profile check: single-profile
    mode, or a config that does not load (check_install reports that one)."""
    try:
        from src.config import load_config
        from src.profiles import load_profile_registry

        config = load_config()
        if not config.profiles_config_path:
            return None, "single-profile mode (PROFILES_CONFIG_PATH unset)"
        return (
            load_profile_registry(
                config.profiles_config_path,
                sandbox_default=config.sandbox_enabled,
                data_root=config.devvy_data_root,
            ),
            "",
        )
    except Exception as e:  # noqa: BLE001 - one note, never a traceback
        text = (str(e).strip().splitlines() or [type(e).__name__])[0]
        return None, f"profile check skipped: config does not load: {text}"


def check_board(board: Board, registry: Any) -> list[str]:
    """Problem lines for one board; [] = green. ``registry`` None skips the
    profile check."""
    out: list[str] = []
    repo = board.repo
    if not repo.is_dir():
        return [f"repo {repo} does not exist -- fix `repo` in boards.json"]
    repo = repo.resolve()
    top = _top(repo)
    if top is None:
        out.append(f"{repo} is not a git repo -- run `git init` there")
    elif top != repo:
        out.append(f"{repo} is not the git top level ({top}) -- point `repo` there")
    elif not _git(repo, "rev-parse", "--verify", "-q", "HEAD"):
        out.append(f"{repo} has no commits -- make a first commit")
    elif not _git(repo, "rev-parse", "--verify", "-q", f"refs/heads/{board.branch}"):
        out.append(
            f"integration branch {board.branch!r} missing in {repo} -- "
            f"create it (`git branch {board.branch}`) or set pump2_branch"
        )
    if not (repo / ".deviz" / "tasks").is_dir():
        out.append(f"no .deviz/tasks/ in {repo} -- run `mkdir -p {repo}/.deviz/tasks`")
    if registry is not None:
        p = registry.get_by_slug(board.profile)
        if p is None:
            out.append(
                f"profile {board.profile!r} is not in profiles.yaml -- "
                "add it or fix `profile` in boards.json"
            )
        else:
            roots = [Path(d).resolve() for d in [p.working_dir, *p.extra_dirs]]
            if not _inside(repo, roots):
                out.append(
                    f"{repo} is outside profile {board.profile!r} -- add it to "
                    "that profile's extra_dirs in profiles.yaml"
                )
    out += _check_test(board)
    out += _check_design(board, repo)
    if board.python and not _run([board.python, "-c", "pass"]):
        out.append(
            f"pump2_python {board.python} does not run `-c pass` -- "
            "fix the path or create the venv"
        )
    return out


def _executable(first: str) -> bool:
    if os.sep in first:
        return os.path.isfile(first) and os.access(first, os.X_OK)
    return shutil.which(first) is not None


def _check_design(board: Board, repo: Path) -> list[str]:
    """Static checks of the ``design`` block (the design loop recipe in
    docs/pump.md); never runs the render or the gate."""
    if board.design is None:
        return []
    from src.designloop.steps import missing_scripts, parse_config

    config = parse_config(board.design)
    if config is None:
        return [
            "`design` block is malformed (render / gate: non-empty lists of "
            "strings, python: a path) -- fix it in boards.json (docs/pump.md)"
        ]
    out = [
        f"design script {a} is not in {repo} -- commit it there "
        "(worktrees are cut from HEAD) or fix the path in `design`"
        for a in missing_scripts(config, repo)
    ]
    for key, argv, need in (
        ("render", config.render, "{out}"),
        ("gate", config.gate, "{renders}"),
    ):
        if not _executable(argv[0]):
            out.append(
                f"design.{key} {argv[0]!r} is not an executable -- "
                "install it or fix the first argv token"
            )
        if not any(need in a for a in argv):
            out.append(f"design.{key} has no {need} placeholder -- add it to the argv")
    if not _executable(config.python):
        out.append(
            f"design.python {config.python!r} is not an executable -- "
            "fix the path or create the venv"
        )
    elif not _run([config.python, "-c", "import PIL"]):
        out.append(
            f"design.python {config.python} cannot `import PIL` -- "
            "install Pillow into it (`pip install pillow`)"
        )
    return out


def _check_test(board: Board) -> list[str]:
    try:
        first = (shlex.split(board.test) or [""])[0]
    except ValueError as e:
        return [f"test command does not parse ({e}) -- fix pump2_test"]
    if not first:
        return ["no test command -- set pump2_test in boards.json"]
    if _executable(first):
        return []
    return [
        f"test command {first!r} is not an executable -- "
        "install it or fix pump2_test"
    ]


def check_boards(
    path: Path,
    only: str | None = None,
    *,
    guard: bool = False,
    registry: Any = _LOAD,
) -> list[str]:
    """``<board>: <problem> -- <fix>`` for every board in ``path`` (or only
    ``only``). Raises FileNotFoundError on a missing boards file."""
    boards = load_boards(path, guard=guard)
    if registry is _LOAD:
        registry, _note = load_registry()
    return [
        f"{b.name}: {line}"
        for b in boards.values()
        if not only or b.name == only
        for line in check_board(b, registry)
    ]


def cmd_doctor(boards: dict[str, Board], only: str | None) -> int:
    """The CLI: 0 green, 1 a problem, 2 an unknown board name."""
    if only and only not in boards:
        known = ", ".join(boards) or "none"
        print(f"doctor: no board {only!r} (known: {known})", file=sys.stderr)
        return 2
    registry, note = load_registry()
    if note:
        print(f"note: {note}", file=sys.stderr)
    rc = 0
    for b in boards.values():
        if only and b.name != only:
            continue
        problems = check_board(b, registry)
        rc = rc or (1 if problems else 0)
        for line in problems or ["OK"]:
            print(f"{b.name}: {line}")
    return rc
