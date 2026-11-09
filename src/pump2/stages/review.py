"""REVIEW: plan + diff + build report -> ``.pump/review.md`` (pass / return).

pass -> merge; return -> build once more with the remarks; a second return
stops the card for the owner. A re-review sees its earlier remarks
(``review.prev.md``, moved by the runner before relaunch).
"""

from __future__ import annotations

import re
from pathlib import Path

from .. import criteria as cr
from ..merge import is_noncode, sh
from ..state import Board, Card, Outcome, card_digest

DIFF_CAP_BYTES = 60_000  # bytes of diff given inline; over it, code first


class ReviewError(ValueError):
    pass


def _section(text: str, name: str) -> str | None:
    m = re.search(rf"^##\s+{name}\s*$(.*?)(?=^##\s|\Z)", text, re.M | re.S)
    return m.group(1).strip() if m else None


def parse(text: str, changed: list[str] | None = None) -> tuple[str, str]:
    """(verdict, remarks); strict: Verdict is exactly ``pass`` or ``return``,
    a return carries at least one numbered remark. ``changed`` = the diff's
    file list when the prompt left a code file out (over DIFF_CAP_BYTES): then
    ``## Files read`` must name at least one of them, else the reviewer judged
    code it never opened (once: 74 files passed in 18 s)."""
    verdict = _section(text, "Verdict")
    if verdict is None:
        raise ReviewError("missing section: Verdict")
    v = verdict.strip().strip("`*").lower()
    if v not in ("pass", "return"):
        raise ReviewError(f"Verdict must be exactly pass or return, got {verdict!r}")
    remarks = _section(text, "Remarks")
    if remarks is None:
        raise ReviewError("missing section: Remarks")
    if v == "return" and not re.search(r"^\s*\d+[.)]\s+\S", remarks, re.M):
        raise ReviewError("a return needs numbered remarks")
    if changed is not None:
        read = _section(text, "Files read") or ""
        names = {ln.strip().lstrip("-* ").strip("`") for ln in read.splitlines()}
        if not names & set(changed):
            raise ReviewError(
                "the diff left code files out and `## Files read` names "
                "none of the changed files: the review did not read the code"
            )
    return v, remarks


def returned_remarks(wt: Path) -> str:
    """Remarks of the current review.md when its verdict is return, else ''."""
    f = wt / ".pump" / "review.md"
    if not f.exists():
        return ""
    try:
        v, remarks = parse(f.read_text())
    except ReviewError:
        return ""
    return remarks if v == "return" else ""


def _plan(board: Board, wt: Path) -> tuple[str, list[str], list[str]] | None:
    """How the diff goes into the prompt, the one source for both
    ``_diff`` and ``capped_names``: None = the whole diff fits under
    DIFF_CAP_BYTES (or git failed / empty). Else (inlined diff text, code
    files left out, docs files left out): code-file diffs are taken in git
    order while they fit the budget; docs (``is_noncode``) are never inlined."""
    rng = f"{board.branch}...HEAD"
    rc, full = sh(["git", "diff", rng], wt)
    if rc or len(full.encode()) <= DIFF_CAP_BYTES:
        return None
    _, names = sh(["git", "diff", "--name-only", rng], wt)
    budget = DIFF_CAP_BYTES
    parts: list[str] = []
    code_out: list[str] = []
    docs_out: list[str] = []
    for f in names.splitlines():
        if not f.strip():
            continue
        if is_noncode(f):
            docs_out.append(f)
            continue
        _, d = sh(["git", "diff", rng, "--", f], wt)
        size = len(d.encode())
        if size <= budget:
            parts.append(d.rstrip())
            budget -= size
        else:
            code_out.append(f)
    return "\n".join(parts), code_out, docs_out


def capped_names(board: Board, wt: Path) -> list[str] | None:
    """The changed files when a changed code file was left out of the
    prompt's diff (over DIFF_CAP_BYTES), else None (the code was inlined)."""
    plan = _plan(board, wt)
    if plan is None or not plan[1]:
        return None
    _, names = sh(["git", "diff", "--name-only", f"{board.branch}...HEAD"], wt)
    return names.split()


def _stat_lines(board: Board, wt: Path, files: list[str]) -> list[str]:
    _, stat = sh(["git", "diff", "--stat=1000,900", f"{board.branch}...HEAD"], wt)
    by = {ln.split("|", 1)[0].strip(): ln.strip() for ln in stat.splitlines()}
    return [by.get(f, f) for f in files]


def _diff(board: Board, wt: Path) -> str:
    rng = f"{board.branch}...HEAD"
    rc, full = sh(["git", "diff", rng], wt)
    if rc:
        return f"(git diff {rng} failed: {full})"
    if not full.strip():
        return "(empty diff)"
    plan = _plan(board, wt)
    if plan is None:
        return full.rstrip()
    inlined, code_out, docs_out = plan
    out = [
        f"(diff is over {DIFF_CAP_BYTES} bytes: code-file diffs are inlined while "
        "they fit; the files left out are listed by name with their stat line)"
    ]
    if code_out:
        out.append(
            f"Code files left out: read them yourself (`git diff {rng} -- <file>`) "
            "and list every changed file you read under `## Files read`; a "
            "review.md naming none of the changed files is rejected."
        )
        out += _stat_lines(board, wt, code_out)
    if docs_out:
        out.append("Docs/cards left out (read them if the plan needs it):")
        out += _stat_lines(board, wt, docs_out)
    if inlined:
        out += ["", inlined]
    return "\n".join(out)


def _test_result(wt: Path) -> str:
    log = wt / ".pump" / "build-tests.log"
    if not log.exists():
        return "(no build test run: nothing to run for this card)"
    lines = [
        ln
        for ln in log.read_text().splitlines()
        if ln.strip() and not ln.startswith("testrun:")
    ]
    return lines[-1] if lines else "(no output)"


def prompt(board: Board, card: Card, wt: Path) -> str:
    pump = wt / ".pump"

    def read(name: str, absent: str) -> str:
        f = pump / name
        return f.read_text().rstrip() if f.exists() else absent

    prev = pump / "review.prev.md"
    again = ""
    if prev.exists():
        again = (
            "\nThis is a RE-REVIEW: the build was returned once. Your earlier "
            "review is below; check each remark was addressed, and do not raise "
            "new nits you did not raise before.\n\n=== earlier review.md ===\n"
            f"{prev.read_text().rstrip()}\n=== end earlier review ===\n"
        )
    return f"""You are the REVIEW stage of a task pipeline. Review the work on this branch.

Repository (your working directory): {wt}

=== card {card.id} ({card.path.name}) ===
{card_digest(card)}
=== end card ===

=== plan (.pump/plan.md) ===
{read("plan.md", "(missing)")}
=== end plan ===

=== diff {board.branch}...HEAD ===
{_diff(board, wt)}
=== end diff ===

=== build notes (.pump/build.md) ===
{read("build.md", "(no build stage ran)")}
=== end build notes ===

Build test result: {_test_result(wt)}
{again}
A `docs`-only card has no tests stage: build wrote the docs without a test
run. Review the diff against the plan's criteria.

Review against: (1) the acceptance criteria in the plan, one by one; (2)
regressions in code the diff touches; (3) scope: nothing outside the plan;
(4) the files listed in tests.json must not be edited (already enforced by the
pipeline, do not re-check it).

Write NOTHING except `.pump/review.md`. No git writes, no other files.

Output contract: `.pump/review.md` has exactly these level-2 sections:

## Verdict
Exactly one word: `pass` or `return`.
## Remarks
A numbered list, one remark per item: `N. [Cn|D1..D4] <text> | evidence:
<file:line, command or input>`. Cn = a criterion of the plan; D1 red tests,
D2 security, D3 regression (with a reproduction), D4 change outside the plan.
Only a BLOCK Cn or a D-class with evidence returns the work; any other remark
is advice and does not block. Empty on pass.
## Scores
One line per criterion of the plan: `Cn: <1-10> | <one line of evidence>`.
A record for comparison only: it never changes the verdict.
## Files read
One path per line: the changed files you opened. Required when the diff above
leaves code files out (then review.md naming none of them is rejected); otherwise
optional.
"""


def finish(board: Board, card: Card, wt: Path) -> Outcome:
    f = wt / ".pump" / "review.md"
    if not f.exists():
        return Outcome("stopped", f"review: {f} was not written (job exit 0)")
    raw = f.read_text()
    try:
        verdict, remarks = parse(raw, changed=capped_names(board, wt))
    except ReviewError as e:
        return Outcome(
            "stopped", f"review: review.md rejected: {e}\n```\n{raw.rstrip()}\n```"
        )
    warn = cr.record_scores(board.name, card.id, "review", card.pump2, raw)
    res = _judged(board, card, wt, f, verdict, remarks)
    if warn:
        res.comment += "\n" + warn
    return res


def _judged(
    board: Board, card: Card, wt: Path, f: Path, verdict: str, remarks: str
) -> Outcome:
    if not cr.frozen(card.pump2):  # a pre-dv-d5aa plan: the old rules
        return _legacy(card, f, verdict, remarks)
    plan = wt / ".pump" / "plan.md"
    sha = cr.criteria_sha(plan.read_text()) if plan.exists() else ""
    if sha != card.pump2["criteria_sha"]:
        return Outcome(
            "stopped",
            "review: criteria changed after analyze (criteria_sha "
            f"{card.pump2['criteria_sha'][:12]} != {sha[:12] or 'no plan.md'}); "
            "only the owner's answer may change them",
        )
    blocking, advice, tail = cr.judge(remarks, cr.plan_criteria(wt))
    if verdict == "pass" or not blocking:
        why = "" if verdict == "pass" else " (the return had no blocking remark)"
        return Outcome("merge", f"review: pass{why}\nreview: {f}{tail}")
    ids = ", ".join(dict.fromkeys(r.tag for r in blocking))
    listing = "\n".join(f"- {r.text}" for r in blocking)
    n, over = cr.take_return(card.pump2)
    if over:
        return Outcome(
            "stopped",
            f"review: return budget spent (return {n}), stopped for the owner; "
            f"blocking: {ids}\nRemarks:\n{listing}{tail}\n\n"
            + cr.summary(wt, "stopped: return budget", [r.text for r in advice]),
            {"returns": n},
        )
    return Outcome(
        "build",
        f"review: returned to build (return {n}); blocking: {ids}\n"
        f"Remarks:\n{listing}{tail}",
        {"returns": n},
    )


def _legacy(card: Card, f: Path, verdict: str, remarks: str) -> Outcome:
    if verdict == "pass":
        return Outcome("merge", f"review: pass\nreview: {f}")
    n, over = cr.take_return(card.pump2)
    if over:
        return Outcome(
            "stopped",
            f"review: two returns, stopped for the owner\nRemarks:\n{remarks}",
            {"returns": n},
        )
    return Outcome(
        "build",
        f"review: returned to build (return {n})\nRemarks:\n{remarks}",
        {"returns": n},
    )
