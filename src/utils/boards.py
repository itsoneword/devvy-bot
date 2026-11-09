"""The one reader of ``config/taskpump/boards.json`` (dv-32fe).

Three callers need the file: the pump (``scripts/pump2``: full validation), conversation attribution (board names
only) and the memory writer (which profile owns which repo, so a per-task
decision can be checked against that repo's DECISIONS.md). Each keeps its own
error policy; only the ``json.loads`` lives here.
"""

from __future__ import annotations

import json
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
