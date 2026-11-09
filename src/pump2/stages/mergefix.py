"""MERGEFIX: the mechanical merge did not land (ff blocked by a
dirty live checkout, the integration branch moved after green, or a
CONFLICT). merge.py wrote ``.pump/merge-facts.md`` and did the git merge into
the card branch itself (a conflict is left in progress). The model resolves
conflicted files and writes ``.pump/mergefix.md`` with a verdict:

land | commit-stray | retest | stop

Code checks override the model: any non-docs file that came in (or was
resolved) since green means ``retest``; a dirty code file in the live checkout
means ``stop``, never a commit. ``land`` / ``commit-stray`` go back to the
merge stage with the green result kept, so the one ff path lands it with no
test run (merge.py stays the only writer of the integration branch).
"""

from __future__ import annotations

import re
from pathlib import Path

from ..merge import MARKERS, dirty_overlap, is_noncode, merging, sh, unmerged
from ..state import Board, Card, Outcome, card_digest
from .common import commit

VERDICTS = ("land", "commit-stray", "retest", "stop")


class MergefixError(ValueError):
    pass


def _section(text: str, name: str) -> str | None:
    m = re.search(rf"^##\s+{name}\s*$(.*?)(?=^##\s|\Z)", text, re.M | re.S)
    return m.group(1).strip() if m else None


def parse(text: str) -> tuple[str, str, list[str]]:
    """(verdict, reasoning, stray files); strict, like review.parse."""
    verdict = _section(text, "Verdict")
    if verdict is None:
        raise MergefixError("missing section: Verdict")
    v = verdict.strip().strip("`*").lower()
    if v not in VERDICTS:
        raise MergefixError(
            f"Verdict must be exactly one of {', '.join(VERDICTS)}, got {verdict!r}"
        )
    reasoning = _section(text, "Reasoning")
    if not reasoning:
        raise MergefixError("missing or empty section: Reasoning")
    stray = _section(text, "Stray files") or ""
    files = [
        ln.strip().lstrip("-* ").strip("`")
        for ln in stray.splitlines()
        if ln.strip().lstrip("-* ")
    ]
    return v, reasoning, files


def prompt(board: Board, card: Card, wt: Path) -> str:
    f = wt / ".pump" / "merge-facts.md"
    facts = f.read_text().rstrip() if f.exists() else "(merge-facts.md missing)"
    return f"""You are the MERGEFIX stage of a task pipeline. The mechanical merge of
this card into `{board.branch}` did not land; judge what changed and finish it.

Repository (the card's worktree, your working directory): {wt}

=== card {card.id} ({card.path.name}) ===
{card_digest(card)}
=== end card ===

=== merge facts (.pump/merge-facts.md) ===
{facts}
=== end merge facts ===

If the facts list conflicted files, the merge of `{board.branch}` is in
progress in this worktree: edit each conflicted file so it holds both sides'
intent and no conflict markers remain. Read the files and `git log` / `git
diff` as you need.

Then pick ONE verdict:
- `land`: what changed since the green tip is non-code only (docs, *.md,
  .deviz cards) and any conflict was in such files: land with no new test run.
- `commit-stray`: the fast-forward is blocked by another session's
  uncommitted edit in the live checkout, and it is docs/markdown only: the
  pipeline commits it as-is on `{board.branch}`, then lands. List the files.
- `retest`: code changed on `{board.branch}` since green, or you resolved a
  conflict in code: the board tests run once more, then the merge lands.
- `stop`: anything you cannot judge, or an uncommitted CODE file in the live
  checkout (never commit someone's uncommitted code).
The pipeline checks your verdict: a code file among the changes turns land
into retest, a dirty code file is always stop.

No git writes (no add, commit, merge, abort): the pipeline does them. Edit
nothing except the conflicted files and `.pump/mergefix.md`.

Output contract: `.pump/mergefix.md` has these level-2 sections:

## Verdict
Exactly one word: `land`, `commit-stray`, `retest` or `stop`.
## Reasoning
Two to six lines: what changed, why the verdict.
## Stray files
Only for commit-stray: one path per line.
"""


def _abort(wt: Path) -> None:
    if merging(wt):
        sh(["git", "merge", "--abort"], wt)


CLEARED: dict[str, object] = {"mergefix": None, "stray": None}


def abandon(wt: Path) -> dict[str, object]:
    """The mergefix job died for good: end its merge, return the keys to clear."""
    _abort(wt)
    return dict(CLEARED)


def _stop(wt: Path, msg: str) -> Outcome:
    _abort(wt)  # a merge left in progress never leaks into another stage
    return Outcome("stopped", msg, dict(CLEARED))


def finish(board: Board, card: Card, wt: Path) -> Outcome:
    f = wt / ".pump" / "mergefix.md"
    if not f.exists():
        return _stop(wt, f"mergefix: {f} was not written (job exit 0)")
    raw = f.read_text()
    try:
        verdict, reasoning, _listed = parse(raw)
    except MergefixError as e:
        return _stop(
            wt, f"mergefix: mergefix.md rejected: {e}\n```\n{raw.rstrip()}\n```"
        )
    head = f"mergefix: {verdict}\n{reasoning}\nreport: {f}"
    if verdict == "stop":
        return _stop(wt, head)
    branch = str(card.pump2.get("branch") or "")
    dirty = dirty_overlap(board, branch)
    code_dirty = [d for d in dirty if not is_noncode(d)]
    if code_dirty:
        return _stop(
            wt,
            f"{head}\noverride: stop: uncommitted code in the live checkout "
            f"{board.repo}: {', '.join(code_dirty)} (never committed by pump2; "
            "commit or drop it, then move the card back to merge)",
        )
    conflicted = unmerged(wt)
    marked = [
        p
        for p in conflicted
        if (wt / p).is_file() and MARKERS.search((wt / p).read_text(errors="replace"))
    ]
    if marked:
        return _stop(
            wt,
            f"{head}\noverride: stop: conflict markers left in " + ", ".join(marked),
        )
    if merging(wt):
        sha, err = commit(wt, f"{card.id}: merge {board.branch} (mergefix)")
        if err:
            return _stop(wt, f"{head}\nmergefix: {err}")
    _, tip = sh(["git", "rev-parse", "HEAD"], wt)
    tip = tip.strip()
    green = str(card.pump2.get("green") or "")
    if green:
        _, d = sh(["git", "diff", "--name-only", green, tip], wt)
        came = sorted(set(d.split()))
    else:
        came = conflicted
    code = [p for p in came if not is_noncode(p)]
    final, note = verdict, ""
    if verdict in ("land", "commit-stray") and code:
        final = "retest"
        note = f"\noverride: {verdict} -> retest: code changed: {', '.join(code)}"
    if final == "commit-stray" and not dirty:
        final = "land"
        note += "\nnote: nothing is dirty any more: land"
    if final != "retest" and not green:
        # never tested against this integration branch: the one test run
        final = "retest"
        note += "\nnote: the board tests have not run on this merge yet: one run"
    comment = head + note
    if final == "retest":
        return Outcome("merge", comment, {"green": None, "stray": None})
    state: dict[str, object] = {"green": tip, "stray": None}
    if final == "commit-stray":
        state["stray"] = dirty
    return Outcome("merge", comment, state)
