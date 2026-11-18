"""``pump2 doctor``: will the pump pick this board up?

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

from src.utils.boards import default_tasks_dir

from . import identity
from .state import Board, list_cards, load_boards

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
    out += _check_cards(board)
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


def _check_cards(board: Board) -> list[str]:
    """The cards dir; on a moved board also: inside a git repo, and
    no card files left in the project repo (a half-done ``cards-move``)."""
    d = board.tasks
    if not d.is_dir():
        return [f"no cards dir {d} -- run `mkdir -p {d}` (or fix cards_dir)"]
    if not board.moved:
        return []
    out = []
    if _top(d) is None:
        out.append(
            f"cards dir {d} is not inside a git repo -- `git init` the boards repo"
        )
    old = default_tasks_dir(board.repo)
    left = sorted(old.rglob("*.md")) if old.is_dir() else []
    tracked = subprocess.run(
        ["git", "ls-files", "--", str(old)],
        cwd=board.repo,
        capture_output=True,
        text=True,
    ).stdout.split()
    if left or tracked:
        out.append(
            f"{len(set(map(str, left)) | set(tracked))} card file(s) left in "
            f"{old} of {board.repo} -- rerun `pump2 cards-move {board.name} --apply`"
        )
    return out


def _executable(first: str) -> bool:
    if os.sep in first:
        return os.path.isfile(first) and os.access(first, os.X_OK)
    return shutil.which(first) is not None


def _check_mockups(config: Any, repo: Path) -> list[str]:
    """a configured style spec that is missing / invalid / draft,
    and a ``design.mockups`` block with no FLUX key or URL in service flux."""
    from src.designloop.mockups import spec_problem

    out = []
    problem = spec_problem(repo, config.style_spec)
    if problem:
        out.append(
            f"design.{problem} -- mockup mode stays off until it is fixed "
            "(`pump2 design spec seed|approve`)"
        )
    if config.mockups is not None:
        if not config.style_spec:
            out.append(
                "design.mockups is set without design.style_spec -- mockups stay "
                "off; add the spec path"
            )
        from src.model_router.routes import flux_credentials

        if flux_credentials() is None:
            out.append(
                "design.mockups is set but FLUX_API_TOKEN / FLUX_API_URL are not "
                "in key-store service flux -- mockups fall back to the paid "
                "image route; /setkey flux ..."
            )
    return out


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
    from src.designloop.references import references_path

    refs = references_path(repo, config.references)
    if refs is not None and not refs.is_file():
        out.append(
            f"design.references {config.references} does not exist in {repo} -- "
            "a design run refuses; fix the path in boards.json or restore the file"
        )
    from src.designloop.steps import missing_kit

    for name in missing_kit(config, repo):
        out.append(
            f"design.kit {name} does not exist in {repo} -- a design run "
            "refuses; fix the path in boards.json or commit the image"
        )
    from src.designloop.inventory import missing as missing_components

    if config.components is None:
        out.append(
            "design.components is malformed (a list of at most 12 paths / globs) "
            "-- the builder gets no component inventory; fix it in boards.json"
        )
    for name in missing_components(repo, config.components or ()):
        out.append(
            f"design.components {name} matches no file in {repo} -- the "
            "inventory skips it; fix the path in boards.json"
        )
    out += _check_mockups(config, repo)
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
        argv = shlex.split(board.test)
    except ValueError as e:
        return [f"test command does not parse ({e}) -- fix pump2_test"]
    first = argv[0] if argv else ""
    if not first:
        return ["no test command -- set pump2_test in boards.json"]
    if not _executable(first):
        return [
            f"test command {first!r} is not an executable -- "
            "install it or fix pump2_test"
        ]
    if argv[1:3] == ["-m", "pytest"] and not _run([first, "-c", "import pytest"]):
        # dv-184c: a venv whose python symlink now resolves to an upgraded
        # system interpreter is still executable but sees no site-packages.
        return [
            f"test command python {first} cannot `import pytest` -- "
            "install pytest into it or point pump2_test at a venv that has it"
        ]
    return []


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


def identity_lines(boards: dict[str, Board]) -> list[str]:
    """This install's identity: ``me``, the names that count as
    ours, a clash warning, and open cards per foreign assignee per board.
    Notes only: an unset ``me`` is single-install mode, never a failure."""
    ident = identity.current()
    extra = {b.profile for b in boards.values()}
    if not ident.me:
        return [
            "me not set: single-install mode, every in_progress card is taken "
            '(set top-level "me" in boards.json)'
        ]
    local = identity.local_profiles(ident, extra)
    out = [f"me: {ident.me}"]
    out.append("ours: " + ", ".join(sorted(local | ident.aliases) or ["-"]))
    if ident.me in local:
        out.append(
            f"warning: me {ident.me!r} is also a profile slug -- give the "
            "install its own name"
        )
    ours = identity.ours_check(ident, extra)
    for b in boards.values():
        counts: dict[str, int] = {}
        for c in list_cards(b)[0]:
            who = identity.owner_of(c.fm)
            if c.fm.get("status") in ("done", "cancelled", "archived") or ours(who):
                continue
            counts[who] = counts.get(who, 0) + 1
        if counts:
            names = ", ".join(f"{k} {v}" for k, v in sorted(counts.items()))
            out.append(f"{b.name}: open cards of other installs: {names}")
    return out


def cmd_doctor(boards: dict[str, Board], only: str | None) -> int:
    """The CLI: 0 green, 1 a problem, 2 an unknown board name."""
    if only and only not in boards:
        known = ", ".join(boards) or "none"
        print(f"doctor: no board {only!r} (known: {known})", file=sys.stderr)
        return 2
    for line in identity_lines(boards):
        print(line)
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
