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
appears in the DECISIONS file, or the referenced card file exists. A later
decision on a task that already has an entry also passes; the full text stays
in the row, so recall() still returns it (architect note, 31.10).
"""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath
from typing import Dict, Mapping, Optional, Tuple

import structlog

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


class DecisionRefChecker:
    """Verifies decision refs against the repos of each repo agent."""

    def __init__(self, agent_repos: Mapping[str, Tuple[Path, ...]]) -> None:
        self._repos: Dict[str, Tuple[Path, ...]] = dict(agent_repos)

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
        for repo in repos:
            if card:
                path = _inside(repo, f".deviz/tasks/{card.group(1)}.md")
                if path is not None and path.is_file():
                    return True
                continue
            path = _inside(repo, ref)
            if path is None or not path.is_file():
                continue
            if _CARD_PATH.match(ref):
                return True
            ids = set(_CONTENT_ID.findall(content))
            if not ids or path.stat().st_size > _MAX_READ_BYTES:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            if any(re.search(rf"\b{re.escape(i)}\b", text) for i in ids):
                return True
        return False
