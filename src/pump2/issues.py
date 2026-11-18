"""GitHub issue status sweep.

A card filed from a GitHub issue (``github_issue_file``) carries the
frontmatter key ``issue: OWNER/NAME#N``. Once that card is merged (pump2 stage
``done``) or closed without a merge, the tick posts exactly one comment on the
issue through ``scripts/gh_intake status`` and writes ``issue_posted: <ts>``.
The comment is built from typed fields only (board:id, a hex sha, a PR URL
under the configured repo): no card title, body or comment text leaves the
board. A gh failure leaves the key unset; the next tick retries. Nothing here
reads or files issues.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable
from pathlib import Path

from .state import HOME_REPO, Board, Card, list_cards, now_iso, set_keys

GH_INTAKE = HOME_REPO / "scripts" / "gh_intake"
ISSUE_KEY = "issue"
POSTED_KEY = "issue_posted"
CLOSED = {"done", "closed", "cancelled", "canceled", "wontfix", "archived"}
_ISSUE = re.compile(r"([A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9._-]+)#([1-9][0-9]*)")
# the merge stage's own comment line (src/pump2/merge.py); line-anchored so a
# description quoting "merge: merged <hex>" mid-line is not taken
_MERGED = re.compile(r"^merge: merged ([0-9a-f]{7,40})\b", re.M)
_SHA = re.compile(r"[0-9a-f]{7,40}")
_TASK = re.compile(r"[a-z0-9][a-z0-9_-]*:[a-z][a-z0-9]*-[0-9a-z]+")
TIMEOUT = 90

Run = Callable[[list[str]], "tuple[int, str]"]


def issue_ref(card: Card) -> tuple[str, int] | None:
    m = _ISSUE.fullmatch(str(card.fm.get(ISSUE_KEY) or "").strip())
    return (m.group(1), int(m.group(2))) if m else None


def merged_sha(card: Card) -> str:
    """The merge commit: pump2 ``merged`` key, else the last merge line."""
    sha = str(card.pump2.get("merged") or "")
    if not _SHA.fullmatch(sha):
        found = _MERGED.findall(card.body)
        sha = found[-1] if found else ""
    return sha[:7]


def pr_url(card: Card, slug: str) -> str:
    v = str(card.fm.get("pr") or "").strip()
    ok = re.fullmatch(rf"https://github\.com/{re.escape(slug)}/pull/[1-9][0-9]*", v)
    return v if ok else ""


def outcome(card: Card) -> str:
    """'merged', 'closed' or '' (still open)."""
    if card.pump2.get("stage") == "done":
        return "merged"
    if card.fm.get("status") in CLOSED:
        return "closed"
    return ""


def status_argv(board: Board, card: Card, number: int, state: str) -> list[str]:
    task = f"{board.name}:{card.id}"
    if not _TASK.fullmatch(task):
        raise ValueError(f"card id {task!r} is not a board:id")
    argv = [
        str(GH_INTAKE),
        "--repo",
        board.intake_github,
        "status",
        str(number),
        "--task",
        task,
        "--state",
        state,
    ]
    if state == "merged":
        sha, pr = merged_sha(card), pr_url(card, board.intake_github)
        argv += ["--sha", sha] if sha else []
        argv += ["--pr", pr] if pr else []
    return argv


def _run(argv: list[str]) -> tuple[int, str]:
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=TIMEOUT)
    except (OSError, subprocess.SubprocessError) as e:
        return 1, str(e)
    return r.returncode, (r.stderr or r.stdout).strip()


def sweep(
    board: Board, ours: Callable[[object], bool] = lambda _a: True, run: Run = _run
) -> list[str]:
    """One status post per finished ``issue:`` card of ``board``; log lines."""
    if not board.intake_github:
        return []
    out: list[str] = []
    for card in list_cards(board)[0]:
        if POSTED_KEY in card.fm or not ours(card.fm.get("assignee")):
            continue
        ref = issue_ref(card)
        state = outcome(card) if ref else ""
        if not ref or not state:
            continue
        tag = f"{board.name}:{card.id}"
        slug, number = ref
        if slug != board.intake_github:
            out.append(f"{tag} issue {slug}#{number}: not this board's repo, skipped")
            continue
        try:
            argv = status_argv(board, card, number, state)
        except ValueError as e:
            out.append(f"{tag} issue status not posted: {e}")
            continue
        rc, err = run(argv)
        if rc:
            out.append(f"{tag} issue #{number} status not posted (retry): {err}")
            continue
        set_keys(Path(card.path), **{POSTED_KEY: now_iso()})
        out.append(f"{tag} issue #{number} status posted: {state}")
        if board.close_issue:
            close = [str(GH_INTAKE), "--repo", slug, "close", str(number)]
            rc, err = run(close)
            if rc:
                out.append(f"{tag} issue #{number} not closed: {err}")
    return out
