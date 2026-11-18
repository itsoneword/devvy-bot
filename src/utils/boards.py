"""The one reader of ``config/taskpump/boards.json`` (dv-32fe).

Three callers need the file: the pump (``scripts/pump2``: full validation), conversation attribution (board names
only) and the memory writer (which profile owns which repo, so a per-task
decision can be checked against that repo's DECISIONS.md). Each keeps its own
error policy; only the ``json.loads`` lives here.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .paths import REPO_ROOT

BOARDS_FILE_DEFAULT = REPO_ROOT / "config" / "taskpump" / "boards.json"
# This repo's own board is implicit in boards.json (served by the pump with no
# entry); its jobs and chat run as this profile.
HOME_PROFILE = "devops"


def read_board_entries(path: Path) -> List[Any]:
    """The raw ``boards`` list of the file, entries unvalidated.

    Raises OSError when the file cannot be read and ValueError when it is not
    a JSON object, so each caller keeps its own policy (the pump prints and
    carries on, attribution returns nothing).
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path}: top level is not an object")
    boards = data.get("boards") or []
    if not isinstance(boards, list):
        raise ValueError(f"{path}: 'boards' is not a list")
    return boards


def agent_repos(
    path: Optional[Path] = None,
    home_profile: str = HOME_PROFILE,
    home_repo: Path = REPO_ROOT,
) -> Dict[str, Tuple[Path, ...]]:
    """Profile slug -> the repos whose boards run under it.

    The home board comes first. An entry without a name or an absolute repo
    is skipped; an entry without ``profile`` belongs to the home profile (the
    pump's default). A missing or broken file leaves only the home board.
    """
    repos: Dict[str, List[Path]] = {home_profile: [home_repo]}
    try:
        entries = read_board_entries(path or BOARDS_FILE_DEFAULT)
    except (OSError, ValueError):
        entries = []
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
            continue
        repo = Path(str(entry.get("repo") or "")).expanduser()
        if not repo.is_absolute():
            continue
        profile = str(entry.get("profile") or home_profile)
        if repo not in repos.setdefault(profile, []):
            repos[profile].append(repo)
    return {slug: tuple(paths) for slug, paths in repos.items()}


# The home board's name (pump2's HOME_BOARD).
HOME_BOARD = "devvy"


def pump2_board_names(path: Optional[Path] = None) -> set[str]:
    """Boards Pump v2 serves (dv-d474): ``devvy`` when the file has a
    ``pump2_home`` object, plus every ``boards[]`` entry with ``"pump2": true``.
    A missing or broken file gives an empty set.
    """
    try:
        data = json.loads((path or BOARDS_FILE_DEFAULT).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    if not isinstance(data, dict):
        return set()
    names: set[str] = set()
    if isinstance(data.get("pump2_home"), dict):
        names.add(HOME_BOARD)
    boards = data.get("boards")
    for entry in boards if isinstance(boards, list) else []:
        if (
            isinstance(entry, dict)
            and entry.get("pump2") is True
            and isinstance(entry.get("name"), str)
        ):
            names.add(entry["name"])
    return names


def board_of_profile(profile: str, path: Optional[Path] = None) -> Optional[str]:
    """Name of the board whose jobs run under ``profile`` (dv-81a7), or None.

    What job_start(task_id) and the pump tools resolve a chat agent's ticket
    through: two boards can both mint ``dv-`` ids, so the caller's profile
    picks the board, never the id. ``HOME_PROFILE`` always gets the home board
    (``devvy``), as the old ``taskpump board-of`` had it: with or without a
    ``pump2_home`` key or a boards file (pump2 then says plainly when the board
    is not served). Other profiles get the first ``boards[]`` entry in file
    order (``profile`` defaults to the home profile).
    """
    if profile == HOME_PROFILE:
        return HOME_BOARD
    try:
        data = json.loads((path or BOARDS_FILE_DEFAULT).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    home = data.get("pump2_home")
    if isinstance(home, dict) and str(home.get("profile") or "") == profile:
        return HOME_BOARD
    boards = data.get("boards")
    for entry in boards if isinstance(boards, list) else []:
        if (
            isinstance(entry, dict)
            and isinstance(entry.get("name"), str)
            and str(entry.get("profile") or HOME_PROFILE) == profile
        ):
            return str(entry["name"])
    return None


def design_board_of_profile(profile: str, path: Optional[Path] = None) -> Optional[str]:
    """Board whose pictures ``profile`` may generate: its own board, else the
    first board listing it in ``design.profiles`` (a second agent on the same
    product, e.g. packops on gymbro). Pictures land in that board's assets
    root, so every generator of one product shares one MAP.md."""
    own = board_of_profile(profile, path)
    if own is not None:
        return own
    try:
        data = json.loads((path or BOARDS_FILE_DEFAULT).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    boards = data.get("boards") if isinstance(data, dict) else None
    for entry in boards if isinstance(boards, list) else []:
        design = entry.get("design") if isinstance(entry, dict) else None
        names = design.get("profiles") if isinstance(design, dict) else None
        if isinstance(names, list) and profile in names:
            if isinstance(entry.get("name"), str):
                return str(entry["name"])
    return None


# ---------------------------------------------------------------- cards dirs
# dv-36b2: a board's cards live in its ``cards_dir`` (boards.json entry key,
# or inside ``pump2_home``); unset = the old in-repo location. This module is
# the only place that builds that default path.
TASKS_REL = Path(".deviz", "tasks")
CARDS_KEY = "cards_dir"
# the install's private boards repo: top-level ``boards_repo`` key
BOARDS_REPO_DEFAULT = Path.home() / "Devvybot" / "boards"


def default_tasks_dir(repo: Path) -> Path:
    """The pre-move cards dir of a project repo."""
    return Path(repo) / TASKS_REL


def tasks_dir(entry: Any, repo: Path) -> Path:
    """The cards dir of one boards.json entry (``pump2_home`` or a
    ``boards[]`` item): its ``cards_dir`` (``~`` expanded; relative = to the
    repo), else ``<repo>/.deviz/tasks``."""
    raw = entry.get(CARDS_KEY) if isinstance(entry, dict) else None
    if isinstance(raw, str) and raw.strip():
        p = Path(raw.strip()).expanduser()
        return p if p.is_absolute() else Path(repo) / p
    return default_tasks_dir(repo)


def _load(path: Optional[Path]) -> Dict[str, Any]:
    try:
        data = json.loads((path or BOARDS_FILE_DEFAULT).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def boards_repo(path: Optional[Path] = None) -> Path:
    """The install's boards repo (``boards_repo``), else ~/Devvybot/boards."""
    raw = _load(path).get("boards_repo")
    if isinstance(raw, str) and raw.strip():
        return Path(raw.strip()).expanduser()
    return BOARDS_REPO_DEFAULT


def all_tasks_dirs(
    path: Optional[Path] = None, home_repo: Path = REPO_ROOT
) -> Dict[str, Path]:
    """Board name -> cards dir: the home board first (always), then every
    ``boards[]`` entry with a name and an absolute repo."""
    return {name: cards for name, (cards, _) in board_dirs(path, home_repo).items()}


def board_dirs(
    path: Optional[Path] = None, home_repo: Path = REPO_ROOT
) -> Dict[str, Tuple[Path, Path]]:
    """Board name -> (cards dir, project repo), same order and entries as
    :func:`all_tasks_dirs` (dv-65dd: the sweep reads a done card and the
    board repo's ``docs/DECISIONS.md``)."""
    data = _load(path)
    out: Dict[str, Tuple[Path, Path]] = {
        HOME_BOARD: (tasks_dir(data.get("pump2_home"), home_repo), Path(home_repo))
    }
    entries = data.get("boards") or []
    if isinstance(entries, dict):
        entries = [{"name": k, **v} for k, v in entries.items() if isinstance(v, dict)]
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
            continue
        repo = Path(str(entry.get("repo") or "")).expanduser()
        if repo.is_absolute():
            out.setdefault(entry["name"], (tasks_dir(entry, repo), repo))
    return out


def home_tasks_dir(path: Optional[Path] = None, home_repo: Path = REPO_ROOT) -> Path:
    """This repo's own board's cards dir."""
    return tasks_dir(_load(path).get("pump2_home"), home_repo)


def repo_tasks_dir(
    repo: Path, path: Optional[Path] = None, home_repo: Path = REPO_ROOT
) -> Path:
    """The cards dir of the board whose project repo is ``repo``; a repo no
    board names keeps the old in-repo location."""
    want = Path(repo).expanduser().resolve()
    if want == Path(home_repo).resolve():
        return home_tasks_dir(path, home_repo)
    data = _load(path)
    entries = data.get("boards") or []
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        r = Path(str(entry.get("repo") or "")).expanduser()
        if r.is_absolute() and r.resolve() == want:
            return tasks_dir(entry, r)
    return default_tasks_dir(repo)


_CARD_ID = re.compile(r"\b([a-z]{2,4}-[0-9a-f]{4})\b")
_UNSAFE = re.compile(r"[\"$`\\\n]")


def card_title(text: str, path: Optional[Path] = None, limit: int = 60) -> str:
    """Title of the first card id (``gb-8a80``) found in ``text`` (a job name),
    from any board's cards dir or its ``archive/``; '' when none. Shell-safe
    (no quotes, ``$``, backticks): job pings bake it into a shell string."""
    m = _CARD_ID.search(text or "")
    if not m:
        return ""
    for d in all_tasks_dirs(path).values():
        for p in (d / f"{m.group(1)}.md", d / "archive" / f"{m.group(1)}.md"):
            try:
                with p.open(encoding="utf-8", errors="replace") as f:
                    for i, line in enumerate(f):
                        if line.startswith("title:"):
                            t = _UNSAFE.sub("", line[6:].strip().strip("\"'"))
                            return t if len(t) <= limit else t[: limit - 1] + "…"
                        if i > 30:
                            break
            except OSError:
                continue
    return ""


def job_label(name: str, path: Optional[Path] = None) -> str:
    """A job's name plus its card's title when the name holds a card id."""
    title = card_title(name, path)
    return f"{name} — {title}" if title else name
