"""The one way to create a card on any board.

An id is ``<prefix>-xxxx`` (4 random hex). It is free when no card file uses
it in the board's live ``.deviz/tasks/`` (incl. ``archive/``), on the
integration branch, on HEAD or on any ``pump2/*`` branch of the board repo.
The file is created with ``O_CREAT|O_EXCL`` in the live tasks dir, so two
writers never get the same id and an existing card is never overwritten:
the live directory is the allocator, the ref scan covers ids committed on
branches. ``DevizBoard.add_task``, ``pump2 new``, the ``task_add`` own-board
switch, the build stage's follow-ups and the merge repair all come here.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import secrets
import subprocess
from collections import Counter
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import NamedTuple

from src.utils.boards import TASKS_REL

from .state import Board

DEFAULT_PREFIX = "dv"
TRIES = 200
_ID_FILE = re.compile(r"^([a-z][a-z0-9]*)-([0-9a-f]{4})\.md$")

# Frontmatter keys in the order the deviz CLI writes them; others follow.
KNOWN_ORDER = (
    "id",
    "title",
    "status",
    "priority",
    "assignee",
    "labels",
    "deps",
    "parent",
    "created",
    "updated",
    "created_by",
)


class Created(NamedTuple):
    id: str
    path: Path


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _rand() -> str:
    return secrets.token_hex(2)


def render(fm: dict[str, str], body: str) -> str:
    """A card file: raw ``key: value`` frontmatter (the deviz format), body."""
    ordered = [k for k in KNOWN_ORDER if k in fm]
    ordered += [k for k in fm if k not in KNOWN_ORDER]
    lines = "\n".join(f"{k}: {fm[k]}" for k in ordered)
    return f"---\n{lines}\n---\n{body}"


def sections(body: str) -> str:
    """The section skeleton every new card gets."""
    out = f"\n## Description\n\n{body.strip()}\n\n"
    return out + "## Acceptance Criteria\n\n## Notes\n\n## Comments\n"


def _git(repo: Path, *args: str) -> tuple[int, str]:
    try:
        p = subprocess.run(
            ["git", *args], cwd=repo, capture_output=True, text=True, timeout=30
        )
    except (OSError, subprocess.TimeoutExpired):
        return 1, ""
    return p.returncode, p.stdout


def _ids_in(names: Iterable[str]) -> set[str]:
    out = set()
    for n in names:
        m = _ID_FILE.match(Path(n).name)
        if m:
            out.add(f"{m.group(1)}-{m.group(2)}")
    return out


def _live_ids(tasks: Path) -> set[str]:
    names = [p.name for p in tasks.glob("*.md")]
    names += [p.name for p in (tasks / "archive").glob("*.md")]
    return _ids_in(names)


def ref_ids(tasks: Path, branches: Iterable[str] = ()) -> set[str]:
    """Card ids committed on HEAD, ``branches`` and every ``pump2/*`` branch
    of the repo holding ``tasks``. {} when it is not in a git repo."""
    rc, top = _git(tasks, "rev-parse", "--show-toplevel")
    if rc:
        return set()
    repo = Path(top.strip())
    try:
        rel = tasks.resolve().relative_to(repo.resolve()).as_posix()
    except ValueError:
        return set()
    return ref_ids_at(repo, rel, branches)


def ref_ids_at(repo: Path, rel: str, branches: Iterable[str] = ()) -> set[str]:
    """Card ids under ``rel`` on HEAD, ``branches`` and every ``pump2/*``
    branch of ``repo``. {} when it is not a git repo."""
    if not repo.is_dir():
        return set()
    _, heads = _git(repo, "for-each-ref", "--format=%(refname)", "refs/heads/pump2/")
    refs = ["HEAD", *branches, *heads.split()]
    out: set[str] = set()
    for ref in dict.fromkeys(refs):
        rc, names = _git(repo, "ls-tree", "-r", "--name-only", ref, "--", rel)
        if rc == 0:
            out |= _ids_in(names.split())
    return out


def board_prefix(tasks: Path) -> str:
    """``.deviz/config.json`` idPrefix, else the most common prefix of the
    existing cards, else ``dv``."""
    cfg = tasks.parent / "config.json"
    try:
        p = json.loads(cfg.read_text()).get("idPrefix")
        if isinstance(p, str) and p:
            return p
    except (OSError, ValueError, AttributeError):
        pass
    seen = Counter(i.rsplit("-", 1)[0] for i in _live_ids(tasks))
    return seen.most_common(1)[0][0] if seen else DEFAULT_PREFIX


def new_card_file(
    tasks: Path,
    text: Callable[[str], str],
    *,
    prefix: str = "",
    branches: Iterable[str] = (),
    rand: Callable[[], str] | None = None,
    taken: Iterable[str] = (),
) -> Created:
    """Pick a free id and create ``tasks/<id>.md`` = ``text(id)`` exclusively.
    The id/exclusive-create primitive: never overwrites a file. ``taken``:
    ids known elsewhere (a moved board's project repo branches)."""
    tasks.mkdir(parents=True, exist_ok=True)
    prefix = prefix or board_prefix(tasks)
    rand = rand or _rand
    taken = ref_ids(tasks, branches) | set(taken)
    for _ in range(TRIES):
        cid = f"{prefix}-{rand()}"
        if cid in taken or cid in _live_ids(tasks):
            continue
        path = tasks / f"{cid}.md"
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text(cid))
        return Created(cid, path)
    raise RuntimeError(f"could not allocate a free card id in {tasks}")


def card_text(
    cid: str,
    title: str,
    body: str = "",
    *,
    status: str = "todo",
    priority: str = "medium",
    assignee: str = "",
    labels: Iterable[str] = (),
    deps: Iterable[str] = (),
    parent: str = "",
    created_by: str = "",
    extra: dict[str, str] | None = None,
) -> str:
    now = _now_iso()
    fm = {
        "id": cid,
        "title": " ".join(title.split()),
        "status": status,
        "priority": priority,
        "assignee": assignee,
        "labels": f"[{', '.join(labels)}]",
        "deps": f"[{', '.join(deps)}]",
        "parent": parent,
        "created": now,
        "updated": now,
    }
    if created_by:
        fm["created_by"] = created_by
    fm.update(extra or {})
    return render(fm, sections(body or title))


def create_card(
    board: Board,
    title: str,
    body: str = "",
    *,
    status: str = "todo",
    labels: Iterable[str] = (),
    deps: Iterable[str] = (),
    parent: str = "",
    created_by: str = "",
    assignee: str = "",
    extra: dict[str, str] | None = None,
    rand: Callable[[], str] | None = None,
) -> Created:
    """Create a card on ``board``; returns its id and file. ``created_by``:
    an ``identity.author(...)`` string; '' = ``<install>/pump2``. ``extra``:
    more one-line frontmatter keys (e.g. ``issue``)."""
    from .identity import author

    labels, deps = list(labels), list(deps)
    created_by = created_by or author("pump2")
    made = new_card_file(
        board.tasks,
        lambda cid: card_text(
            cid,
            title,
            body,
            status=status,
            labels=labels,
            deps=deps,
            parent=parent,
            assignee=assignee,
            created_by=created_by,
            extra=extra,
        ),
        branches=[board.branch],
        rand=rand,
        taken=_project_ids(board),
    )
    commit_cards(board.tasks, f"{board.name}: create {made.id}")
    return made


def _project_ids(board: Board) -> set[str]:
    """A moved board's ids still committed in its project repo (pre-move
    integration and ``pump2/*`` branches): never reused."""
    if not board.moved:
        return set()
    return ref_ids_at(board.repo, TASKS_REL.as_posix(), [board.branch])


_ID_LINE = re.compile(r"^id:.*$", re.M)


def refile_text(text: str) -> Callable[[str], str]:
    """``text`` (a card file) with its ``id:`` line set to the new id."""

    def make(cid: str) -> str:
        if not text.startswith("---\n"):
            return text
        end = text.find("\n---", 4)
        head, rest = text[:end], text[end:]
        if _ID_LINE.search(head):
            head = _ID_LINE.sub(f"id: {cid}", head, count=1)
        else:
            head = head.replace("---\n", f"---\nid: {cid}\n", 1)
        return head + rest

    return make


def refile(board: Board, text: str) -> Created:
    """File ``text`` (a card whose id is taken) under a fresh id."""
    made = new_card_file(
        board.tasks,
        refile_text(text),
        branches=[board.branch],
        taken=_project_ids(board),
    )
    commit_cards(board.tasks, f"{board.name}: refile {made.id}")
    return made


def _identity(repo: Path) -> list[str]:
    """``-c user.*`` for a repo with no committer configured (a fresh boards
    repo on a host without a global git identity)."""
    rc, email = _git(repo, "config", "user.email")
    if rc == 0 and email.strip():
        return []
    return ["-c", "user.name=devvy pump2", "-c", "user.email=pump2@localhost"]


def git_commit_paths(repo: Path, paths: list[str], msg: str) -> tuple[int, str]:
    """``git add -A`` + commit of ``paths`` only (other staged or dirty files
    of ``repo`` stay out of the commit)."""
    rc, out = _git(repo, "add", "-A", "--", *paths)
    if rc:
        return rc, out
    try:
        p = subprocess.run(
            ["git", *_identity(repo), "commit", "-q", "--no-verify", "-m", msg]
            + ["--", *paths],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)
    return p.returncode, p.stdout + p.stderr


def commit_cards(tasks: Path, msg: str) -> str:
    """Commit the board folder holding ``tasks`` in the boards repo when it
    is dirty: ``<boards repo>/<board>/.deviz/tasks``. A cards dir at
    a repo's top-level ``.deviz/tasks`` (a project repo, not moved) is never
    committed by code. '' = committed or nothing to do, else the error."""
    folder = tasks.parent.parent
    if not folder.is_dir():
        return ""
    rc, top = _git(folder, "rev-parse", "--show-toplevel")
    if rc:
        return ""
    repo = Path(top.strip()).resolve()
    if folder.resolve() == repo:
        return ""
    rel = folder.resolve().relative_to(repo).as_posix()
    _, gd = _git(repo, "rev-parse", "--absolute-git-dir")
    lock = Path(gd.strip() or repo / ".git") / "devvy-cards.lock"
    try:
        fh = open(lock, "a")
    except OSError:
        fh = None
    try:
        if fh is not None:
            fcntl.flock(fh, fcntl.LOCK_EX)
        _, st = _git(repo, "status", "--porcelain", "-uall", "--", rel)
        if not st.strip():
            return ""
        rc, out = git_commit_paths(repo, [rel], msg)
        return "" if rc == 0 else (out.strip() or f"git commit rc={rc}")
    finally:
        if fh is not None:
            fh.close()
