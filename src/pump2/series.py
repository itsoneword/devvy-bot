"""The per-board "clean in a row" series (dv-8c35), from card files + git.

A card counts when pump2 merged it (``merge: merged <sha> into``) at or after
``SERIES_START``. It is clean when every comment between its first pump2
start and that merge is ``@pump2`` or ``@owner`` (``pump2 answer``) and no
pump2 comment there says ``restarted by``. Anything else is a break and resets
the count, as does a pump2 card whose branch reached the integration branch
without a pump2 merge ("landed by hand"). Never raises: a card that cannot be
read (bad time, git error) is skipped.
"""

from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime

from .state import Board, Card

SERIES_START = datetime(2026, 11, 8, 10, 0, tzinfo=UTC)
NORMAL_AUTHORS = ("pump2", "owner")

# `### @builder — 2026-11-07T18:49Z`, `### @devops merge — 2026-...`
_HEAD = re.compile(r"^### @(\S+)(?: [^\n—]*)? — (\S+).*$", re.M)
_START = re.compile(r"\bstarted by\b|picked up from in_progress")
_RESTART = re.compile(r"restarted by \S+(?: at \S+)?")
_MERGED = re.compile(r"^merge: merged ([0-9a-f]{7,40}) into", re.M)


@dataclass
class Comment:
    author: str
    when: datetime | None
    body: str


@dataclass
class Event:
    when: datetime
    card: str
    reason: str  # "" = a clean merge


def _log(msg: str) -> None:
    print(f"pump2 series: {msg}", file=sys.stderr)


def comments(card: Card) -> list[Comment]:
    from .pin import parse_time

    heads = list(_HEAD.finditer(card.body))
    out = []
    for i, h in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(card.body)
        out.append(
            Comment(h.group(1), parse_time(h.group(2)), card.body[h.end() : end])
        )
    return out


def _first_start(cs: list[Comment]) -> int | None:
    return next(
        (
            i
            for i, c in enumerate(cs)
            if c.author == "pump2"
            and _START.search(c.body)
            and not _RESTART.search(c.body)
        ),
        None,
    )


def _merge_event(card: Card, cs: list[Comment]) -> Event | None:
    """The merge event of a pump2-merged card, or None (not merged / unreadable)."""
    idx = next(
        (
            i
            for i in range(len(cs) - 1, -1, -1)
            if cs[i].author == "pump2" and _MERGED.search(cs[i].body)
        ),
        None,
    )
    if idx is None:
        if not card.pump2.get("merged"):
            return None
        from .pin import parse_time

        # Only the key, no merge comment: `since` is when the card's last
        # stage changed, not the merge itself; close enough for ordering.
        when = parse_time(str(card.pump2.get("since") or ""))
        idx = len(cs)
    else:
        when = cs[idx].when
    if when is None:
        _log(f"{card.id}: merge time unreadable, skipped")
        return None
    start = _first_start(cs[:idx])
    if start is None:
        return Event(when, card.id, "entered mid-pipeline")
    for c in cs[start + 1 : idx]:
        if c.author not in NORMAL_AUTHORS:
            return Event(when, card.id, f"hand fix by @{c.author}")
        if c.author == "pump2":
            m = _RESTART.search(c.body)
            if m:
                return Event(when, card.id, m.group(0))
    return Event(when, card.id, "")


def _hand_landed(board: Board, card: Card, cs: list[Comment]) -> Event | None:
    """A pump2 card, not merged by pump2, whose branch has commits of its own
    and is in the integration branch now but was not at its first pump2 start.

    ``ensure_branch`` creates the branch from the integration branch at the
    first stage launch, which can be after main moved past the start: a branch
    with no commits beyond its creation point landed nothing."""
    from .pin import _git, branch_at, parse_time

    if not card.pump2:
        return None
    start = _first_start(cs)
    started = cs[start].when if start is not None else None
    if started is None:
        return None
    branch = str(card.pump2.get("branch") or f"pump2/{card.id}")
    tip = _git(board.repo, "rev-parse", "--verify", "-q", f"refs/heads/{branch}")
    if not tip:
        return None

    def ancestor(of: str) -> bool:
        return bool(of) and _is_ancestor(board, tip, of)

    if not ancestor(board.branch):
        return None
    if not _own_commits(board, branch, tip):
        return None
    if ancestor(branch_at(board.repo, board.branch, started)):
        return None
    times = [c.when for c in cs if c.when is not None]
    when = max(times) if times else parse_time(str(card.fm.get("updated") or ""))
    if when is None:
        return None
    return Event(when, card.id, "landed by hand")


def _own_commits(board: Board, branch: str, tip: str) -> bool:
    """True when the branch has commits beyond its creation point (the oldest
    entry of its reflog). No reflog: unknown, so no break (logged)."""
    from .pin import _git

    log = _git(board.repo, "reflog", "show", "--format=%H", f"refs/heads/{branch}")
    if not log:
        _log(f"{board.name}: no reflog for {branch}, landed-by-hand check skipped")
        return False
    created = log.splitlines()[-1].strip()
    return bool(_git(board.repo, "rev-list", "--max-count=1", f"{created}..{tip}"))


def _is_ancestor(board: Board, a: str, b: str) -> bool:
    r = subprocess.run(
        ["git", "merge-base", "--is-ancestor", a, b],
        cwd=board.repo,
        capture_output=True,
        timeout=30,
    )
    return r.returncode == 0


def events(board: Board, cards: list[Card]) -> list[Event]:
    out = []
    for card in cards:
        try:
            cs = comments(card)
            ev = _merge_event(card, cs) or _hand_landed(board, card, cs)
        except Exception as e:  # one bad card must not stop the pin
            _log(f"{board.name}:{card.id} skipped: {e}")
            continue
        if ev is not None and ev.when >= SERIES_START:
            out.append(ev)
    return sorted(out, key=lambda e: e.when)


def board_series(
    board: Board, cards: list[Card], now: datetime | None = None
) -> tuple[int, Event | None]:
    """(clean merges since the last break, the last break or None)."""
    n, last = 0, None
    for ev in events(board, cards):
        if ev.reason:
            n, last = 0, ev
        else:
            n += 1
    return n, last


def series_line(board: Board, cards: list[Card], now: datetime | None = None) -> str:
    try:
        n, last = board_series(board, cards, now)
    except Exception as e:
        _log(f"{board.name}: {e}")
        n, last = 0, None
    brk = f"{last.card} {last.reason}" if last else "none"
    since = SERIES_START.strftime("%d.%m %H:%MZ")
    return f"series: {n} clean in a row (since {since}); last break: {brk}"
