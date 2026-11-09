"""ANALYZE: card + repo -> ``.pump/plan.md`` (goal, Cn criteria, touches,
size, kind, assumptions, jobs, questions; format in ``..criteria``).

``.pump/`` is state, never committed. Questions park the card in
``waiting_user``; the owner's verbatim answer (``pump2 answer``) reruns this
stage once. A second round of questions stops the card. A plan that breaks
the format reruns once with the error (``format_retry``), then stops.
"""

from __future__ import annotations

from pathlib import Path

from .. import criteria as cr
from ..state import Board, Card, Outcome, card_digest

SECTIONS = (
    "Goal",
    "Acceptance criteria",
    "Touches",
    "Size",
    "Kind",
    "Assumptions",
    "Jobs",
    "Questions",
    "Notes",
)
TOUCHES = cr.TOUCHES


class PlanError(ValueError):
    pass


def prompt(board: Board, card: Card, wt: Path) -> str:
    pump = wt / ".pump"
    prev = pump / "plan.prev.md"
    again = ""
    if prev.exists():
        again = (
            "\nThis is a SECOND pass. Your earlier plan is below; the owner's "
            "answer is the last `### @owner` section of the card. Produce the "
            "final plan with that answer applied. Ask nothing the owner has "
            "already answered.\n\n=== earlier plan.md ===\n"
            f"{prev.read_text()}\n=== end earlier plan ===\n"
        )
    cur = pump / "plan.md"
    if card.pump2.get("format_retry") and cur.exists():
        raw = cur.read_text()
        try:
            parse(raw)
            err = "(none now)"
        except PlanError as e:
            err = str(e)
        again += (
            f"\nYour plan.md was REJECTED by the format check: {err}\n"
            "Rewrite it in the format below.\n=== rejected plan.md ===\n"
            f"{raw}\n=== end rejected plan ===\n"
        )
    return f"""You are the ANALYZE stage of a task pipeline. Turn the card below into a plan.

Repository (your working directory): {wt}

=== card {card.id} ({card.path.name}) ===
{card_digest(card)}
=== end card ===
{again}
Read the repository as much as you need. Write NOTHING except the file
`.pump/plan.md` (create the directory if missing). No commits, no git writes,
do not edit any other file.

Output contract: `.pump/plan.md` has exactly these sections, in this order,
with these level-2 headings:

## Goal
1-3 lines.
## Acceptance criteria
One line each: `C1 [BLOCK|ADVICE] <verifiable criterion> | because: <why> |
check: test|script|model|owner`. BLOCK = the work fails without it. Max
BLOCK/total by Size: S 3/3, M 5/8, L 6/14; at most 3 `check: owner`;
`check: test` needs code, db or ui in Touches.
## Touches
A subset of: code, db, ui, docs. One per line, nothing else on the line.
## Size
S, M or L (code raises it: ui or db -> M, both -> L).
## Kind
One of: bug, feature, new screen, docs, ops.
## Assumptions
One per line: decision | because: why | reversible: yes/no. Or `none`.
## Jobs
JTBD, required for feature / new screen / ui: who the user is, what job
they finish, the numbered steps, and data volumes (rows, items) taken from the
DB or the code. Never ask the owner for volumes. Else may be empty.
## Questions
One per line: `Q: <question> | default: <what you do if unanswered> |
matters: <what changes> | found: <what you checked>`, or empty. At most S 1,
M 3, L 5; extra ones become Assumptions. A question without a default is
rejected.
## Notes
Anything the next stages need (files, risks).

Rules for questions: do not ask about things you can decide yourself (temp
files, which file to edit, naming, formatting). Ask only about product, scope,
money, production or deletion decisions. When in doubt, decide and record the
choice in Assumptions.
"""


def parse(text: str) -> dict[str, str]:
    """Strict parse: every section present, Touches/Size/Kind valid, the
    criteria and questions in the ``..criteria`` format, Jobs where needed."""
    secs = cr.sections(text)
    missing = [s for s in SECTIONS if s not in secs]
    if missing:
        raise PlanError("missing section(s): " + ", ".join(missing))
    touches = cr.lines(secs["Touches"])
    if not touches:
        raise PlanError("Touches is empty")
    bad = [t for t in touches if t not in TOUCHES]
    if bad:
        raise PlanError(f"Touches has unknown value(s) {bad}; allowed: {TOUCHES}")
    if not secs["Goal"]:
        raise PlanError("Goal is empty")
    size = secs["Size"].strip().strip("`*")
    if size not in cr.SIZES:
        raise PlanError(f"Size must be one of {cr.SIZES}, got {secs['Size']!r}")
    kind = secs["Kind"].strip().strip("`*").lower()
    if kind not in cr.KINDS:
        raise PlanError(f"Kind must be one of {cr.KINDS}, got {secs['Kind']!r}")
    try:
        crit = cr.parse_criteria(secs["Acceptance criteria"])
        cr.check_criteria(crit, cr.effective_size(size, touches), touches)
        cr.parse_questions(secs["Questions"])
    except cr.FormatError as e:
        raise PlanError(str(e)) from None
    if (kind in ("feature", "new screen") or "ui" in touches) and secs[
        "Jobs"
    ].lower() in cr.NONE:
        raise PlanError(f"Jobs is empty: required for Kind {kind} or Touches ui")
    secs["Touches"] = "\n".join(dict.fromkeys(touches))
    secs["Size"], secs["Kind"] = size, kind
    return secs


def finish(board: Board, card: Card, wt: Path) -> Outcome:
    f = wt / ".pump" / "plan.md"
    if not f.exists():
        return Outcome("stopped", f"analyze: {f} was not written (job exit 0)")
    raw = f.read_text()
    try:
        plan = parse(raw)
    except PlanError as e:
        msg = f"analyze: plan.md rejected: {e}\n```\n{raw.rstrip()}\n```"
        if int(card.pump2.get("format_retry") or 0) >= 1:
            return Outcome("stopped", msg + "\nrejected twice, stopped")
        return Outcome(
            "analyze", msg + "\nrerunning once with the error", {"format_retry": 1}
        )
    touches = plan["Touches"].split("\n")
    size = cr.effective_size(plan["Size"], touches)
    crit = cr.parse_criteria(plan["Acceptance criteria"])
    qs = cr.parse_questions(plan["Questions"])
    keep, extra = qs[: cr.MAX_QUESTIONS[size]], qs[cr.MAX_QUESTIONS[size] :]
    if extra:
        a = plan["Assumptions"]
        a = "" if a.lower() in cr.NONE else a + "\n"
        a += "\n".join(f"{q} (moved from Questions: over Size {size})" for q in extra)
        raw = cr.replace_section(raw, "Questions", "\n".join(keep))
        raw = cr.replace_section(raw, "Assumptions", a)
        f.write_text(raw)
    sha = cr.criteria_sha(raw)
    raised = f" (raised from {plan['Size']})" if size != plan["Size"] else ""
    blocks = sum(c.level == "BLOCK" for c in crit)
    head = (
        f"analyze: plan ready\nGoal: {plan['Goal']}\nTouches: {', '.join(touches)}\n"
        f"Size: {size}{raised}\nKind: {plan['Kind']}\n"
        f"Acceptance criteria: {len(crit)} ({blocks} BLOCK)\nplan: {f}"
    )
    if extra:
        head += f"\n{len(extra)} question(s) over the Size {size} limit moved to Assumptions"
    state = {
        "round": None,
        "format_retry": None,
        "size": size,
        "kind": plan["Kind"],
        "touches": touches,
        "criteria_sha": sha,
    }
    if not keep:
        # docs only: no tests stage, but build still writes the docs
        nxt = "tests" if set(touches) & {"code", "db", "ui"} else "build"
        return Outcome(nxt, head, state)
    q = "\n".join(f"{i}. {x}" for i, x in enumerate(keep, 1))
    rnd = int(card.pump2.get("round") or 0)
    if rnd >= 1:
        return Outcome(
            "stopped",
            f"analyze: asked twice, stopped for the owner\n{head}\nQuestions:\n{q}",
        )
    return Outcome(
        "waiting_user",
        f"analyze: questions for the owner (answer with "
        f'`pump2 answer {board.name}:{card.id} "<text>"`)\n{q}\n\n{head}',
        dict(state, round=rnd + 1),
    )
