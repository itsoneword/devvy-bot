"""A board's custom stage (dv-3b99): the board's prompt file, filled with
``fill_template``, plus a fixed report contract; ``.pump/<report>`` carries a
review-style verdict (``review.parse``). pass -> the next pipeline entry;
return -> ``return_to`` on the card's shared return budget. A ``writes: false``
stage may change nothing outside ``.pump/``; a ``writes: true`` one gets its
changes committed as ``<card-id>: <stage>``."""

from __future__ import annotations

from pathlib import Path

from .. import criteria as cr
from ..state import Board, Card, Outcome, StageSpec, card_digest
from ..template import fill_template
from . import review
from .common import changed_files, commit, stopped

CONTRACT = """
=== report contract (pump2) ===
Repository (your working directory): {wt}
{writes}
Write `.pump/{report}` with exactly these level-2 sections:

## Verdict
Exactly one word: `pass` or `return`.
## Remarks
A numbered list, one remark per item: `N. [Cn|D1..D4] <text> | evidence:
<file:line, command or input>`. Cn = a criterion of the plan; D1 red tests,
D2 security, D3 regression (with a reproduction), D4 change outside the plan.
Only a BLOCK Cn or a D-class with evidence returns the work (to `{back}`);
any other remark is advice. A `return` needs at least one remark.
## Scores
One line per criterion of the plan: `Cn: <1-10> | <one line of evidence>`.
A record only: it never changes the verdict.
"""
WRITES = "You may change files in the repository; pump2 commits them. No git writes."
READ_ONLY = (
    "Write NOTHING except `.pump/{report}`: no other file, no git writes "
    "(a changed file stops the card)."
)


def prev_reports(board: Board, wt: Path, upto: str | None = None) -> str:
    """The reports of the board's custom stages present in ``.pump/`` (those
    listed before ``upto`` when given), one block each; '' when none."""
    out = []
    for name in board.stage_names():
        if name == upto:
            break
        spec = board.custom(name)
        f = wt / ".pump" / spec.report if spec else None
        if spec and f and f.exists():
            out.append(
                f"=== {name} report (.pump/{spec.report}) ===\n"
                f"{f.read_text().rstrip()}\n=== end {name} report ==="
            )
    return "\n\n".join(out)


class CustomStage:
    """The stage-module interface (``prompt`` / ``finish``) bound to a spec."""

    def __init__(self, spec: StageSpec) -> None:
        self.spec = spec

    def prompt(self, board: Board, card: Card, wt: Path) -> str:
        s = self.spec
        plan = wt / ".pump" / "plan.md"
        values = {
            "CARD": f"=== card {card.id} ({card.path.name}) ===\n"
            f"{card_digest(card)}\n=== end card ===",
            "PLAN": plan.read_text().rstrip() if plan.exists() else "(no plan.md)",
            "DIFF": review._diff(board, wt),
            "WORKTREE": str(wt),
            "BRANCH": str(card.pump2.get("branch") or f"pump2/{card.id}"),
            "PREV": prev_reports(board, wt, s.stage) or "(no earlier reports)",
        }
        rule = WRITES if s.writes else READ_ONLY.format(report=s.report)
        return (
            fill_template(s.prompt.read_text(), values).rstrip()
            + "\n"
            + (CONTRACT.format(wt=wt, writes=rule, report=s.report, back=s.return_to))
        )

    def finish(self, board: Board, card: Card, wt: Path) -> Outcome:
        s = self.spec
        f = wt / ".pump" / s.report
        if not f.exists():
            return stopped(f"{s.stage}: {f} was not written (job exit 0)")
        raw = f.read_text()
        try:
            verdict, remarks = review.parse(raw)
        except review.ReviewError as e:
            return stopped(
                f"{s.stage}: {s.report} rejected: {e}\n```\n{raw.rstrip()}\n```"
            )
        changed = changed_files(wt)
        sha = ""
        if changed and not s.writes:
            return stopped(f"{s.stage}: a writes: false stage changed files: {changed}")
        if changed:
            sha, err = commit(wt, f"{card.id}: {s.stage}")
            if err:
                return stopped(f"{s.stage}: {err}")
        done = f", committed {sha}" if sha else ""
        warn = cr.record_scores(board.name, card.id, s.stage, card.pump2, raw)
        res = self._judged(board, card, wt, verdict, remarks, done)
        if warn:
            res.comment += "\n" + warn
        return res

    def _judged(
        self, board: Board, card: Card, wt: Path, verdict: str, remarks: str, done: str
    ) -> Outcome:
        """Remarks always go through ``criteria.judge``: custom stages are new
        (dv-3b99), no pre-dv-d5aa card reaches one, so review's legacy
        "any return returns" path does not apply."""
        s = self.spec
        f = wt / ".pump" / s.report
        names = board.stage_names()
        nxt = names[names.index(s.stage) + 1]
        tail, listing, ids = "", f"Remarks:\n{remarks}", ""
        if verdict == "return":
            blocking, _, tail = cr.judge(remarks, cr.plan_criteria(wt))
            if not blocking:
                return Outcome(
                    nxt,
                    f"{s.stage}: pass (the return had no blocking remark){done}\n"
                    f"{s.stage}: {f}{tail}",
                )
            ids = "; blocking: " + ", ".join(dict.fromkeys(r.tag for r in blocking))
            listing = "Remarks:\n" + "\n".join(f"- {r.text}" for r in blocking)
        else:
            tail = cr.judge(remarks, cr.plan_criteria(wt))[2]
        if verdict == "pass":
            return Outcome(nxt, f"{s.stage}: pass{done}\n{s.stage}: {f}{tail}")
        n, over = cr.take_return(card.pump2)
        if over:
            return Outcome(
                "stopped",
                f"{s.stage}: return budget spent (return {n}), stopped for the "
                f"owner{done}{ids}\n{listing}{tail}",
                {"returns": n},
            )
        return Outcome(
            s.return_to,
            f"{s.stage}: returned to {s.return_to} (return {n}){done}{ids}\n"
            f"{listing}{tail}",
            {"returns": n},
        )
