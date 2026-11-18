"""Is a repo agent's per-task decision actually in its repo? (dv-32fe)

Agents with their own repo (the profiles of ``config/taskpump/boards.json``
plus devops, see :func:`src.utils.boards.agent_repos`) keep per-task project
decisions in ``docs/DECISIONS.md`` or the task card. Memory then holds a
pointer (``kind='conversation_result'`` + ``source_ref``), full text in the
row, one line injected. A pointer is only honest when the target really holds
the decision, so :class:`MemoryWriter` asks this checker before it writes one;
an unverified decision stays an injected ``user_fact`` flagged
``source_ref="missing:<ref>"`` (``MISSING_REF_PREFIX``) and the consolidation
sweep promotes it once the repo catches up.

"Verified" is a heuristic, deliberately cheap: a task id from the content
appears in the DECISIONS file, or the referenced card file exists (in the
agent's own repos or, dv-65dd, on any board passed as ``card_dirs``). A later
decision on a task that already has an entry also passes; the full text stays
in the row, so recall() still returns it (architect note, 31.10).

dv-3e28: a card ref is no longer a pointer. :meth:`DecisionRefChecker.resolve_card`
finds the card on whichever board owns the id (board-qualified ref, then the
speaker's own boards, then every other board of boards.json, one match only);
the writer appends the decision to that card as a comment and stores the row
off-prompt (``kind='card_decision'``). :meth:`verify` keeps the dv-32fe
meaning for ``docs/DECISIONS.md`` refs.
"""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath
from typing import Dict, Mapping, Optional, Tuple

import structlog

from src.utils.boards import all_tasks_dirs, repo_tasks_dir

logger = structlog.get_logger()

# A card id as written in a ref: "gb-1ab3", "dv-32fe", or board-qualified
# "pl:dv-1a2b" (the pump's cross-board form).
_CARD_REF = re.compile(r"^(?:[a-z0-9-]{1,16}:)?([a-z]{2,8}-[0-9a-z]{4,})$")
# Task ids inside a fact's content. At least one digit: "re-added" or
# "pre-face" are words, not ids, and would match any DECISIONS file.
_CONTENT_ID = re.compile(r"\b[a-z]{2,8}-(?=[0-9a-f]*[0-9])[0-9a-f]{4,}\b")
_CARD_PATH = re.compile(r"^\.deviz/tasks/[a-z]{2,8}-[0-9a-z]{4,}\.md$")
# A DECISIONS file larger than this is not read (gymbro's is ~190 KB).
_MAX_READ_BYTES = 4_000_000


def is_decision_ref(ref: Optional[str]) -> bool:
    """True for a ref naming where a project decision lives: a path whose
    basename is ``DECISIONS.md``, a card id, or ``.deviz/tasks/<id>.md``.
    Other refs (research notes, logs) are not checked."""
    if not ref:
        return False
    ref = ref.strip()
    return bool(
        PurePosixPath(ref).name == "DECISIONS.md"
        or _CARD_REF.match(ref)
        or _CARD_PATH.match(ref)
    )


def _inside(repo: Path, ref: str) -> Optional[Path]:
    """``repo/ref`` when ``ref`` is repo-relative and stays inside it."""
    rel = PurePosixPath(ref)
    if rel.is_absolute() or ".." in rel.parts or ref.startswith("~"):
        return None
    root = repo.resolve()
    target = (root / rel).resolve()
    return target if target.is_relative_to(root) else None


def card_ref_id(ref: Optional[str]) -> Optional[Tuple[Optional[str], str]]:
    """(board or None, card id) when ``ref`` names a card: ``gb-1ab3``,
    ``pl:dv-1a2b`` or ``.deviz/tasks/<id>.md``; None for any other ref."""
    ref = (ref or "").strip()
    m = _CARD_REF.match(ref)
    if m:
        board = ref.split(":", 1)[0] if ":" in ref else None
        return board, m.group(1)
    if _CARD_PATH.match(ref):
        return None, PurePosixPath(ref).stem
    return None


def _card_file(tasks_dir: Path, cid: str) -> Optional[Path]:
    for p in (tasks_dir / f"{cid}.md", tasks_dir / "archive" / f"{cid}.md"):
        if p.is_file():
            return p
    return None


class DecisionRefChecker:
    """Verifies decision refs against the repos of each repo agent."""

    def __init__(
        self,
        agent_repos: Mapping[str, Tuple[Path, ...]],
        boards_path: Optional[Path] = None,
        card_dirs: Optional[Mapping[str, Path]] = None,
    ) -> None:
        self._repos: Dict[str, Tuple[Path, ...]] = dict(agent_repos)
        # boards.json for the cross-board card lookup (dv-3e28); None = the
        # install's default file.
        self._boards_path = boards_path
        # dv-65dd: board name -> cards dir of every board. A card ref
        # resolves on any board: devops records decisions about gymbro cards,
        # which its own repos never hold (25 of 28 devops flags on 14.11).
        # When given, resolve_card uses the same map instead of boards.json.
        self._card_dir_map: Optional[Dict[str, Path]] = (
            dict(card_dirs) if card_dirs is not None else None
        )
        self._card_dirs: Tuple[Path, ...] = tuple((card_dirs or {}).values())

    def resolve_card(self, agent_id: str, ref: str) -> Optional[Tuple[str, Path]]:
        """(board name, card file) of the card ``ref`` names, or None.

        Order: a board-qualified ref -> that board only; else the speaker's
        own boards; else every other board, where an id found on more than
        one board is ambiguous (None). Never raises."""
        try:
            return self._resolve_card(agent_id, ref)
        except Exception as e:
            logger.warning(
                "card ref lookup failed", agent_id=agent_id, ref=ref, error=str(e)
            )
            return None

    def _resolve_card(self, agent_id: str, ref: str) -> Optional[Tuple[str, Path]]:
        parsed = card_ref_id(ref)
        if parsed is None:
            return None
        board, cid = parsed
        dirs = (
            self._card_dir_map
            if self._card_dir_map is not None
            else all_tasks_dirs(self._boards_path)
        )
        if board is not None:
            d = dirs.get(board)
            found = _card_file(d, cid) if d is not None else None
            return (board, found) if found else None
        by_dir = {d.expanduser().resolve(): name for name, d in dirs.items()}
        own = []
        for repo in self._repos.get(agent_id.split("#", 1)[0], ()):
            d = repo_tasks_dir(repo, self._boards_path).expanduser().resolve()
            own.append((by_dir.get(d) or Path(repo).name, d))
        for name, d in own:
            found = _card_file(d, cid)
            if found:
                return name, found
        own_dirs = {d for _, d in own}
        hits = [
            (name, f)
            for name, d in dirs.items()
            if d.expanduser().resolve() not in own_dirs
            for f in [_card_file(d, cid)]
            if f is not None
        ]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            logger.info(
                "card ref ambiguous",
                agent_id=agent_id,
                ref=ref,
                boards=[h[0] for h in hits],
            )
        return None

    @property
    def agents(self) -> frozenset[str]:
        """Profile slugs that own a repo (the repo-agent set)."""
        return frozenset(self._repos)

    def handles(self, agent_id: str) -> bool:
        """True when ``agent_id``'s profile (``#u…`` suffix ignored) has a repo."""
        return agent_id.split("#", 1)[0] in self._repos

    def verify(self, agent_id: str, ref: str, content: str) -> bool:
        """Does ``ref`` in ``agent_id``'s repo hold this decision? Never raises:
        any doubt (unknown agent, escaping path, unreadable file) is False,
        which keeps the full text injected."""
        try:
            return self._verify(agent_id, ref.strip(), content)
        except Exception as e:  # fail safe: the fact stays a full fact
            logger.warning(
                "decision ref check failed", agent_id=agent_id, ref=ref, error=str(e)
            )
            return False

    def _verify(self, agent_id: str, ref: str, content: str) -> bool:
        repos = self._repos.get(agent_id.split("#", 1)[0], ())
        card = _CARD_REF.match(ref)
        # a card id, or the old ``.deviz/tasks/<id>.md`` path: looked up in
        # the board's cards dir, wherever it lives (dv-36b2)
        cid = card.group(1) if card else None
        if cid is None and _CARD_PATH.match(ref):
            cid = PurePosixPath(ref).stem
        if cid and any((d / f"{cid}.md").is_file() for d in self._card_dirs):
            return True
        for repo in repos:
            if cid:
                if (repo_tasks_dir(repo) / f"{cid}.md").is_file():
                    return True
                continue
            path = _inside(repo, ref)
            if path is None or not path.is_file():
                continue
            ids = set(_CONTENT_ID.findall(content))
            if not ids or path.stat().st_size > _MAX_READ_BYTES:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            if any(re.search(rf"\b{re.escape(i)}\b", text) for i in ids):
                return True
        return False
