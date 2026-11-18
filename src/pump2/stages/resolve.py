"""RESOLVE (dv-b8bb): every stop comes here first (state.stop). The board's
agent writes ``.pump/resolve.md``: ``continue <stage>`` (pump2 commits),
``rerun <stage>`` (with a note) or ``owner``. Code keeps the hard limits.

A question stop of analyze (dv-f707, ``state.QUESTION_STOP``) is triaged here
before the owner sees it: ``## Answers`` answers questions by number (one
card comment by the board's profile); ``rerun analyze`` needs every number,
``owner`` passes the unanswered ones on unchanged, taken from ``plan.md``.
"""

from __future__ import annotations

import re
from pathlib import Path

from .. import criteria as cr
from ..merge import MARKERS, dirty_overlap, is_noncode, merging, sh
from ..render import ASK_HEAD, DECIDED_HEAD
from ..state import (
    BUILTIN_PIPELINE,
    QUESTION_STOP,
    RESOLVE_MAX,
    STOP_FILE,
    Board,
    Card,
    Outcome,
    card_digest,
    resolve_history,
    write_comment,
)
from . import build
from .common import changed_files, commit, read_tests_json, stopped
from .review import _section
from .tests import TESTS_DIR

NOTE_FILE = "resolve-note.md"  # a rerun's note, shown in that stage's prompt
OWNER_REASONS = ("money", "keys", "prod", "deletion outside", "product choice")
_ANSWER = re.compile(r"^\s*\**(\d+)[.)]\**\s+(.+?)\s*$")


class ResolveError(ValueError):
    pass


def _order(board: Board) -> list[str]:
    return board.stage_names() or list(BUILTIN_PIPELINE)


def parse(text: str, stages: list[str], questions: int = 0) -> dict[str, str]:
    """verb, target, note, reasoning, question, reason, stray, answers; strict.
    ``questions`` > 0: a question stop with that many questions."""
    verdict = _section(text, "Verdict")
    if not verdict:
        raise ResolveError("missing or empty section: Verdict")
    first, _, note = verdict.partition("\n")
    words = first.strip().strip("`*").lower().split()
    verb, target = (words + ["", ""])[:2]
    if verb not in ("continue", "rerun", "owner") or (verb == "owner") != (not target):
        raise ResolveError(
            "Verdict must be `continue <stage>`, `rerun <stage>` or `owner`, "
            f"got {first!r}"
        )
    if target and target not in stages:
        raise ResolveError(f"stage {target!r} is not one of {stages}")
    out = {"verb": verb, "target": target, "note": note.strip()}
    out["reasoning"] = _section(text, "Reasoning") or ""
    if not out["reasoning"]:
        raise ResolveError("missing or empty section: Reasoning")
    out["question"] = _section(text, "Question") or ""
    out["reason"] = (_section(text, "Owner reason") or "").lower()
    out["answers"] = _section(text, "Answers") or ""
    if questions:
        _check_answers(out, verb, target, questions)
    elif verb == "owner" and not (
        out["question"] and any(r in out["reason"] for r in OWNER_REASONS)
    ):
        raise ResolveError(
            f"owner needs ## Question and ## Owner reason, one of {OWNER_REASONS}"
        )
    out["stray"] = _section(text, "Stray files") or ""
    return out


def answers(text: str) -> dict[int, str]:
    """{question number: the agent's decision} of an ``## Answers`` section."""
    out: dict[int, str] = {}
    for ln in text.splitlines():
        m = _ANSWER.match(ln)
        if m:
            out[int(m.group(1))] = m.group(2)
        elif ln.strip():
            raise ResolveError(f"Answers line is not `<n>. <decision>`: {ln!r}")
    return out


def _check_answers(v: dict[str, str], verb: str, target: str, n: int) -> None:
    if not ((verb, target) == ("rerun", "analyze") or verb == "owner"):
        raise ResolveError(
            "a question stop takes `rerun analyze` (all answered) or `owner`"
        )
    got = answers(v["answers"])
    bad = sorted(k for k in got if not 1 <= k <= n)
    if bad:
        raise ResolveError(f"Answers names unknown question(s) {bad}; 1..{n} exist")
    left = [k for k in range(1, n + 1) if k not in got]
    if verb == "rerun" and left:
        raise ResolveError(f"rerun analyze needs an answer to every question: {left}")
    if verb == "owner":
        if not left:
            raise ResolveError("owner with every question answered: rerun analyze")
        if not any(r in v["reason"] for r in OWNER_REASONS):
            raise ResolveError(f"owner needs ## Owner reason, one of {OWNER_REASONS}")


def plan_questions(wt: Path) -> list[str]:
    """The ``Q:`` lines of ``.pump/plan.md``, as analyze kept them."""
    f = wt / ".pump" / "plan.md"
    if not f.exists():
        return []
    try:
        return cr.parse_questions(cr.sections(f.read_text()).get("Questions", ""))
    except cr.FormatError:
        return []


def question_stop(card: Card, wt: Path) -> list[str]:
    """The questions of an analyze question stop being triaged; [] otherwise."""
    stop = wt / ".pump" / STOP_FILE
    if card.pump2.get("resolve_from") != "analyze" or not stop.exists():
        return []
    if not stop.read_text().startswith(f"stopped in: analyze\n\n{QUESTION_STOP}"):
        return []
    return plan_questions(wt)


QUESTION_RULES = """This is a QUESTION stop: analysis asked the numbered questions above
before any code was written. You answer what you may decide; only the rest
reaches the owner. The work lands on staging for the owner to try, not
straight in prod, so take the default (or your own choice) of a question about
look, wording, defaults, limits, a flag or scope, and say how to flip it back.
Pass to the owner, even when its default is to do nothing, every question
about: money (billing, payments, plans on sale); keys; prod: an action on the
prod server or prod data (deploy, restart, migration, deletion), not a look or
behaviour change that ships by the normal deploy and is easy to flip back;
deletion outside the job tree; what happens to people's sign-ups, bookings or
payments; a product choice hard to undo.

Pick ONE verdict:
- `rerun analyze`: you answered every question; analyze reruns with them.
- `owner`: the questions you do not answer go to the owner unchanged.

Output contract: `.pump/resolve.md` has these level-2 sections:
## Verdict
`rerun analyze` or `owner`.
## Reasoning
One line per question: why you decided it or passed it on.
## Answers
`<n>. <decision>; flip back: <how>`, one line per question you decide.
## Owner reason
(owner only) One of: {reasons}.
"""


def prompt(board: Board, card: Card, wt: Path) -> str:
    pump = wt / ".pump"
    stop = pump / STOP_FILE
    reason = stop.read_text().rstrip() if stop.exists() else "(stop.md missing)"
    facts = pump / "merge-facts.md"
    if facts.exists():
        reason += "\n\n" + facts.read_text().rstrip()
    at = card.pump2.get("resolve_from") or "?"
    qs = question_stop(card, wt)
    if qs:
        return f"""You are the RESOLVE stage of a task pipeline, run as the board's agent:
analysis of card {card.id} asked questions and you triage them first.

Repository (the card's worktree, your working directory): {wt}

=== card {card.id} ({card.path.name}) ===
{card_digest(card)}
=== end card ===

=== questions (.pump/plan.md, the plan is in .pump/plan.md) ===
{chr(10).join(f"{i}. {q}" for i, q in enumerate(qs, 1))}
=== end questions ===

{QUESTION_RULES.format(reasons=", ".join(OWNER_REASONS))}"""
    return f"""You are the RESOLVE stage of a task pipeline: card {card.id} stopped in
stage `{at}` and you decide what happens next.

Repository (the card's worktree, your working directory): {wt}

=== card {card.id} ({card.path.name}) ===
{card_digest(card)}
=== end card ===

=== stop reason (.pump/{STOP_FILE}) ===
{reason}
=== end stop reason ==={resolve_history(card.body)}

Plan: .pump/plan.md; the stages' reports and logs are in .pump/. If conflicted
files are listed, the merge is in progress here: edit them, no markers left.

Pick ONE verdict:
- `continue <stage>`: you fixed it here (code, a conflict, a wrong test); the
  pipeline commits the worktree and the card goes on at <stage>.
- `rerun <stage>`: that stage runs again; lines under the Verdict line are a
  note shown in its prompt. Test-file edits are discarded, the rest stays.
- `owner`: only for money, keys, prod (an action on the prod server or prod
  data), deletion outside the job tree or a real product choice: one question.
Stages: {", ".join(_order(board))}.

Hard limits, checked in code: no git writes (no add, commit, merge, abort,
push); never land the card on `{board.branch}`: only the merge gate does, on
green tests; every test file you edit goes to review, which checks it was not
weakened (list them in Reasoning); at most {RESOLVE_MAX} resolves per card.

Output contract: `.pump/resolve.md` has these level-2 sections:
## Verdict
`continue <stage>`, `rerun <stage>` or `owner`; for rerun, the note below.
## Reasoning
Two to six lines: the cause and what you changed.
## Question
## Owner reason
(owner only) The question; one of the five reasons above.
## Stray files
(optional, `continue merge`) Another session's uncommitted docs edits in the
live checkout that block the fast-forward, one path per line: pump2 commits them.
"""


def _edited(wt: Path, base: str = "HEAD") -> list[str]:
    """Changed vs ``base`` (HEAD at the stop), untracked too, minus merge input"""
    _, out = sh(["git", "diff", "--name-only", "-z", base or "HEAD"], wt)
    _, new = sh(["git", "ls-files", "--others", "--exclude-standard", "-z"], wt)
    paths = {p for p in (out + "\0" + new).split("\0") if p}
    if merging(wt):
        paths = {p for p in paths if _blob(wt, p) != _obj(wt, f"MERGE_HEAD:{p}")}
    return sorted(paths)


def _obj(wt: Path, spec: str) -> str:
    rc, out = sh(["git", "rev-parse", "-q", "--verify", spec], wt)
    return out.strip() if rc == 0 else ""


def _blob(wt: Path, path: str) -> str:  # worktree blob id, "" if absent
    f = wt / path
    return sh(["git", "hash-object", "--", path], wt)[1].strip() if f.is_file() else ""


def _tests(wt: Path, paths: list[str]) -> list[str]:
    listed, _ = read_tests_json(wt)
    return [p for p in paths if p in listed or p.startswith(f"{TESTS_DIR}/")]


def _save_leftovers(wt: Path) -> str:
    """`rerun analyze`: uncommitted changes go to ``.pump/leftovers-<n>.patch``
    and the worktree back to HEAD; returns the patch name, '' if clean."""
    _, new = sh(["git", "ls-files", "--others", "--exclude-standard", "-z"], wt)
    _, diff = sh(["git", "diff", "--binary", "HEAD"], wt)
    for p in [p for p in new.split("\0") if p]:
        diff += sh(["git", "diff", "--no-index", "--binary", "/dev/null", p], wt)[1]
    if not diff.strip():
        return ""
    pump = wt / ".pump"
    out = pump / f"leftovers-{len(list(pump.glob('leftovers-*.patch'))) + 1}.patch"
    out.write_text(diff)
    sh(["git", "reset", "-q", "--hard", "HEAD"], wt)
    sh(["git", "clean", "-fdq"], wt)  # untracked, never ignored (.pump stays)
    return out.name


def finish(board: Board, card: Card, wt: Path) -> Outcome:
    f = wt / ".pump" / "resolve.md"
    if not f.exists():
        return stopped(f"resolve: {f} was not written (job exit 0)")
    raw = f.read_text()
    order = _order(board)
    qs = question_stop(card, wt)
    try:
        v = parse(raw, [s for s in order if s != "resolve"], len(qs))
    except ResolveError as e:
        return stopped(f"resolve: resolve.md rejected: {e}\n```\n{raw.rstrip()}\n```")
    head = f"resolve: {v['verb']} {v['target']}".rstrip() + f"\n{v['reasoning']}"
    st = card.pump2
    base = str(st.get("stop_head") or "")
    if base and not st.get("merged"):
        rc, _ = sh(["git", "merge-base", "--is-ancestor", base, board.branch], wt)
        if rc == 0:
            return stopped(
                f"{head}\noverride: stop: the card branch was landed by hand on "
                f"{board.branch}; only the merge gate may land it"
            )
    if qs:
        return _triaged(board, card, v, head, qs)
    if v["verb"] == "owner":
        return Outcome(
            "waiting_user",
            f"{head}\nquestion for the owner ({v['reason']}): {v['question']}\n"
            f'answer with `pump2 answer {board.name}:{card.id} "<text>"`',
        )
    (wt / ".pump" / "merge-facts.md").unlink(missing_ok=True)
    if v["verb"] == "rerun":
        if merging(wt):
            sh(["git", "merge", "--abort"], wt)
        base = str(card.pump2.get("resolve_head") or "HEAD")
        for p in _tests(wt, _edited(wt, base)):  # a test edit only lands via review
            if _obj(wt, f"{base}:{p}"):
                sh(["git", "restore", "-SW", "-s", base, "--", p], wt)
            else:
                sh(["git", "rm", "-q", "--cached", "--ignore-unmatch", "--", p], wt)
                (wt / p).unlink(missing_ok=True)
        (wt / ".pump" / NOTE_FILE).write_text(v["note"] or v["reasoning"])
        if v["target"] != "analyze":
            return Outcome(v["target"], head)
        saved = _save_leftovers(wt)  # rethink: the plan changes, code is redone
        if saved:
            head += f"\nuncommitted leftovers saved to .pump/{saved}"
        return Outcome("analyze", head, {"rethink": True})
    return _continue(board, card, wt, v, head)


def _triaged(
    board: Board, card: Card, v: dict[str, str], head: str, qs: list[str]
) -> Outcome:
    """A question stop: the agent's answers become one card comment by the
    board's profile; the rest go to the owner byte-identical from plan.md."""
    got = answers(v["answers"])
    if got:
        rows = [
            f"{n}. {qs[n - 1].removeprefix('Q:').split(' | ')[0].strip()} -> {a}"
            for n, a in sorted(got.items())
        ]
        write_comment(card.path, "\n".join([DECIDED_HEAD, *rows]), board.profile)
    if v["verb"] == "rerun":
        return Outcome("analyze", head, {"rethink": None})
    ask = "\n".join(f"{n}. {q}" for n, q in enumerate(qs, 1) if n not in got)
    why = "\n".join("> " + ln for ln in head.splitlines())
    return Outcome(
        "waiting_user",
        f"{ASK_HEAD} (answer with "
        f'`pump2 answer {board.name}:{card.id} "<text>"`)\n{ask}\n\n'
        f"{why}\n> owner reason: {v['reason']}",
        {"stage": "analyze"},
    )


def _continue(
    board: Board, card: Card, wt: Path, v: dict[str, str], head: str
) -> Outcome:
    target, order = v["target"], _order(board)
    base = str(card.pump2.get("resolve_head") or "") or _obj(wt, "HEAD")
    edits = _edited(wt, base)
    marked = [
        p
        for p in edits
        if (wt / p).is_file() and MARKERS.search((wt / p).read_text(errors="replace"))
    ]
    if marked:
        return stopped(f"{head}\noverride: stop: conflict markers left in {marked}")
    listed, _ = read_tests_json(wt)
    tests = _tests(wt, edits)
    dirty = dirty_overlap(board, str(card.pump2.get("branch")))
    code = [d for d in dirty if not is_noncode(d)]
    if target == "merge" and code:
        return stopped(
            f"{head}\noverride: stop: uncommitted code in the live checkout "
            f"{board.repo}: {', '.join(code)} (never committed by pump2)"
        )
    state: dict[str, object] = {}
    if changed_files(wt) or merging(wt):
        sha, err = commit(wt, f"{card.id}: resolve")
        if err:
            return stopped(f"{head}\nresolve: {err}")
        head += f"\ncommitted {sha}"
    sha = _obj(wt, "HEAD")
    if sha and sha != base:  # changed by our commit or the agent's own
        green = str(card.pump2.get("green") or "")
        _, d = sh(["git", "diff", "--name-only", green, sha], wt) if green else (1, "?")
        state["green"] = sha if all(is_noncode(p) for p in d.split()) else None
    past = order.index(target) > order.index("build") if "build" in order else False
    files = [p for p in listed if (wt / p).exists()]
    if past and files:
        res, red = build.gate(board, wt, files, [])
        if red:
            return stopped(f"{head}\n{red}")
        head += f"\ngate: {res[0]}"
    if tests:
        state["test_edits"] = sorted(
            {*map(str, card.pump2.get("test_edits") or []), *tests}
        )
        head += "\ntest files edited (review checks them): " + ", ".join(tests)
        if "review" in order and order.index(target) > order.index("review"):
            target = "review"
            head += "\nrerouted to review: a test file was edited"
    stray = [ln.strip().lstrip("-* ").strip("`") for ln in v["stray"].splitlines()]
    if target == "merge" and any(stray):
        state["stray"] = [s for s in stray if s]
    return Outcome(target, head, state)
