"""``pump2 design run <board:id>``: the designloop runner's card I/O (moved
here from the old pump).

A card labelled ``designloop`` is never a pump2 stage card (``pump2 start``
refuses it): this runner generates its variants, gates and critiques them
(``src/designloop/loop.py``) and sends one collage to the board's chat. The
card then waits in ``design_state: awaiting_pick``; the owner's number comes
back through the one answer channel, ``pump2 pending`` / ``pump2 answer``.

Card keys: ``design_state`` running → awaiting_pick → picked (status done),
rejected (the owner's «none, because …»; waits for a person to run it again)
or failed; ``design_variants`` = the offered branches in the owner's
numbering; ``design_pick`` / ``design_branch`` = his choice. Every accepted
answer is also appended to the board's verdict log
(``src/designloop/references.py``), which the next run's brief reads.

Mockup mode: on a board with an approved ``design.style_spec`` and
a ``design.mockups`` block, step 0 draws 3-4 mockups (``design_mockups``) and
the critic picks one (``design_mockup``); on a card labelled ``ownermockup``
the run stops in ``awaiting_mockup`` and the owner's number, through the same
``pump2 answer``, starts the run again with his mockup (``mockup_picked``).

Locking: pump2's tick holds no card lock and never writes a card without a
``pump2:`` stage, which a designloop card never has; card writes here are
atomic (``state.set_keys``) instead.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Sequence

from .state import (
    HOME_REPO,
    WORKTREES,
    Board,
    Card,
    add_comment,
    add_owner_answer,
    find_card,
    read_card,
    set_keys,
)

DESIGNLOOP_LABEL = "designloop"  # not `design`: a topic label on live cards
DESIGN_AWAITING = "awaiting_pick"
DESIGN_RUNNING = "running"
DESIGN_REJECTED = "rejected"
DESIGN_AWAITING_MOCKUP = "awaiting_mockup"
DESIGN_MOCKUP_PICKED = "mockup_picked"
PAID_MOCKUP_LABEL = "paidmockup"
OWNER_MOCKUP_LABEL = "ownermockup"
BGJOB = HOME_REPO / "scripts" / "bgjob"
PUMP2 = HOME_REPO / "scripts" / "pump2"
REJECTED_COMMENT = "Design rejected"
DESIGN_TEMPLATE = HOME_REPO / "config" / "taskpump" / "design_generate.md"
AGENTJOB = HOME_REPO / "scripts" / "agentjob"
AGENTCTL = HOME_REPO / "scripts" / "agentctl"
TG_LIMIT = 4000

Notify = Callable[[list[str]], None]


class DesignError(Exception):
    """A refusal: printed as one line, the card is left as it was."""


def send(argv: list[str]) -> None:
    """Run one ``agentctl notify``; raise on failure (bot down)."""
    r = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    if r.returncode:
        raise RuntimeError(f"agentctl notify rc={r.returncode}: {r.stderr.strip()}")


# --- card predicates ------------------------------------------------------


def _labels(card: Card) -> list[str]:
    raw = card.fm.get("labels") or []
    if isinstance(raw, str):
        raw = [x for x in re.split(r"[\[\],\s]+", raw) if x]
    return [str(x) for x in raw]


def is_designloop(card: Card) -> bool:
    return DESIGNLOOP_LABEL in _labels(card)


def _fm_str(card: Card, key: str) -> str:
    v = card.fm.get(key)
    return "" if v is None else str(v).strip().strip("\"'")


def design_variants(card: Card, key: str = "design_variants") -> list[str]:
    raw = card.fm.get(key)
    if isinstance(raw, list):
        return [str(x).strip().strip("\"'") for x in raw if str(x).strip()]
    text = "" if raw is None else str(raw)
    return [x.strip().strip("\"'") for x in text.strip("[]").split(",") if x.strip()]


def awaiting_pick(card: Card) -> bool:
    return (
        is_designloop(card)
        and card.fm.get("status") != "done"
        and _fm_str(card, "design_state") == DESIGN_AWAITING
    )


def awaiting_mockup(card: Card) -> bool:
    return (
        is_designloop(card)
        and card.fm.get("status") != "done"
        and _fm_str(card, "design_state") == DESIGN_AWAITING_MOCKUP
    )


def drop_task_ids(text: str) -> str:
    return re.sub(r"\b(?:[\w-]+:)?[a-z]+-[0-9a-z]{4}\b:?", " ", text, flags=re.I)


# "none, because …" / «ни один: …» rejects every variant.
_REJECT = re.compile(
    r"\s*(?:none|no|0|ни\s+один|ни\s+одного|никакой|ничего)(?![\w])(?:\W.*)?",
    re.IGNORECASE | re.DOTALL,
)


def pick_of_reply(text: str, n: int | None = None) -> int | None:
    """The variant number of a chat reply ("2", "app:dv-d001: 2", "v2", "2,
    calmer labels"); with ``n``, only 1..n counts. Anything else is None:
    asked again, never guessed."""
    m = re.fullmatch(
        r"\s*(?:v|#|variant\s*|вариант\s*)?(\d+)\s*(?:[.!]?\s*|[,;:.!—–-]\s*\S.*)",
        drop_task_ids(text),
        re.IGNORECASE | re.DOTALL,
    )
    if not m:
        return None
    k = int(m.group(1))
    if k < 1 or (n is not None and k > n):
        return None
    return k


def rejects_all(text: str) -> bool:
    """The owner's «none, because …»: no variant fits."""
    return bool(_REJECT.fullmatch(drop_task_ids(text)))


def design_change_request(card: Card) -> str:
    """The card's Description section: the problem the designer solves."""
    m = re.search(
        r"^## Description\s*\n(.*?)(?=^## |^### @|\Z)", card.body, re.M | re.S
    )
    text = (m.group(1) if m else "").strip()
    return text or _fm_str(card, "title")


# --- answer / pending -----------------------------------------------------


def dependents(card: Card, cards: Sequence[Card]) -> list[Card]:
    """The cards with ``card`` in their ``deps:`` (its build cards)."""
    return [c for c in cards if card.id in [str(d) for d in (c.fm.get("deps") or [])]]


def _sibling_cards(card: Card) -> list[Card]:
    """Every readable card in the card's own tasks directory."""
    out = []
    for p in sorted(card.path.parent.glob("*.md")):
        try:
            out.append(read_card(p))
        except (OSError, ValueError):
            continue  # an unparseable card is reported by the tick
    return out


def answer_pick(
    card: Card, text: str, full: str = "", board: Board | None = None
) -> int:
    """``pump2 answer`` on a designloop card. A variant number (plus an
    optional line) closes it; «none, because …» rejects every variant and
    leaves it ``rejected`` for a person to run again; anything else is
    refused (rc 2) and changes nothing. An accepted answer is appended to the
    board's verdict log."""
    full = full or card.id
    if awaiting_mockup(card):
        return answer_mockup(card, text, full, board)
    if not awaiting_pick(card):
        print(
            f"{full}: a designloop card not waiting for a pick "
            f"(design_state={_fm_str(card, 'design_state') or 'none'}, "
            f"status={card.fm.get('status')})",
            file=sys.stderr,
        )
        return 2
    variants = design_variants(card)
    k = pick_of_reply(text, len(variants))
    if not k and rejects_all(text):
        return _reject_all(card, text, full, board, len(variants))
    if not k:
        choices = ", ".join(str(i) for i in range(1, len(variants) + 1))
        print(
            f"{full}: not a variant number, nothing changed. Ask the owner "
            f"again: which design to build, {choices or '(none offered)'} "
            "(or «none, because …»)?",
            file=sys.stderr,
        )
        return 2
    branch = variants[k - 1]
    pick = branch.rsplit("/", 1)[-1]
    add_owner_answer(card.path, text)
    add_comment(
        card.path,
        f"Design picked: {k} = `{branch}`. Cards with this one in `deps:` "
        "start now and build on that branch.",
    )
    set_keys(
        card.path,
        status="done",
        design_state="picked",
        design_pick=pick,
        design_branch=branch,
    )
    print(f"{full}: design {pick} picked ({branch}); its build cards start")
    _log_verdict(board, card, text, f"picked {k} of {len(variants)}", full)
    if not dependents(card, _sibling_cards(card)):
        # a pick with no build card built nothing, unnoticed
        warn = (
            f"no build card depends on {card.id}: nothing will be built; "
            f"create one with deps: [{card.id}]"
        )
        add_comment(card.path, warn)
        print(f"{full}: {warn}")
    return 0


def answer_mockup(card: Card, text: str, full: str, board: Board | None = None) -> int:
    """The owner's mockup number (card label ``ownermockup``): the card goes
    to ``mockup_picked`` with ``design_mockup`` set and the run starts again
    detached (``scripts/bgjob``), building to that mockup."""
    mockups = design_variants(card, "design_mockups")
    k = pick_of_reply(text, len(mockups))
    if not k:
        choices = ", ".join(str(i) for i in range(1, len(mockups) + 1))
        print(
            f"{full}: not a mockup number, nothing changed. Ask the owner again: "
            f"which mockup to build to, {choices or '(none drawn)'}?",
            file=sys.stderr,
        )
        return 2
    chosen = mockups[k - 1]
    add_owner_answer(card.path, text)
    add_comment(card.path, f"Mockup picked by the owner: {k} = `{chosen}`.")
    set_keys(card.path, design_state=DESIGN_MOCKUP_PICKED, design_mockup=chosen)
    err = resume_run(board, full)
    if err:
        print(
            f"{full}: mockup {k} picked; the run did not start ({err}): start it "
            f"with job_start(task_id={full!r})"
        )
    else:
        print(f"{full}: mockup {k} picked; the design run starts on it")
    return 0


def use_owner_mockup(boards: dict[str, Board], full: str, mockup: str) -> int:
    """``pump2 design run <card> --mockup FILE`` (owner 13.11.26): the run
    builds to a mockup the owner chose outside it (a sprint sheet), as if he
    had picked it from the run's own mockups (``mockup_picked``). The file is
    copied next to the run's mockups so the board's job can read it."""
    import shutil

    src = Path(mockup).expanduser()
    name, _, cid = full.partition(":")
    if not src.is_file() or name not in boards:
        print(f"{full}: no such mockup file or board: {mockup}", file=sys.stderr)
        return 2
    board = boards[name]
    try:
        card = find_card(board, cid)
    except FileNotFoundError as e:
        print(f"{full}: {e}", file=sys.stderr)
        return 2
    if card.fm.get("status") == "done" or not is_designloop(card):
        print(f"{full}: done or not a designloop card", file=sys.stderr)
        return 2
    dest_dir = board.repo / ".screens" / "design" / card.id / "owner-mockup"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / src.name
    shutil.copyfile(src, dest)
    add_comment(card.path, f"Mockup given by the owner: `{dest}` (from `{src}`).")
    set_keys(card.path, design_state=DESIGN_MOCKUP_PICKED, design_mockup=str(dest))
    return 0


def resume_run(board: Board | None, full: str) -> str:
    """Start ``pump2 design run`` detached, as job_start does; "" = started."""
    if board is None:
        return "no board"
    cmd = " ".join([sys.executable, str(PUMP2), "design", "run", full])
    try:
        r = subprocess.run(
            [
                str(BGJOB),
                "run",
                "--profile",
                board.profile,
                "--cwd",
                str(HOME_REPO),
                "--name",
                f"design-{full.partition(':')[2]}",
                "--queue",
                "--cmd",
                cmd,
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as e:
        return str(e)
    return (r.stderr or r.stdout).strip()[-300:] if r.returncode else ""


def _reject_all(card: Card, text: str, full: str, board: Board | None, n: int) -> int:
    """The owner's rule: the card waits in ``rejected``; nothing runs
    until a person starts it again."""
    add_owner_answer(card.path, text)
    add_comment(
        card.path,
        f"{REJECTED_COMMENT}: none of the {n} variants fit (the owner's words "
        f"above; logged for the next run). Run again: `pump2 design run {full}`.",
    )
    set_keys(card.path, design_state=DESIGN_REJECTED)
    print(
        f"{full}: all {n} designs rejected; the card waits. A person starts it "
        f"again: `pump2 design run {full}` (job_start)"
    )
    _log_verdict(board, card, text, f"rejected all {n}", full)
    return 0


def _log_verdict(
    board: Board | None, card: Card, words: str, outcome: str, full: str
) -> None:
    """Append the owner's answer to the board's verdict log and commit it
    there; a failure is one printed line, the answer stands."""
    if board is None:
        return
    from src.designloop.references import append_verdict, verdicts_path
    from src.designloop.steps import parse_config

    config = parse_config(board.design)
    log = (
        verdicts_path(board.repo, config.references, config.verdicts)
        if config
        else None
    )
    if log is None:
        return
    try:
        append_verdict(
            log,
            words,
            card=card.id,
            screen=_fm_str(card, "design_screen"),
            outcome=outcome,
        )
    except OSError as e:
        print(f"{full}: verdict not logged: {e}")
        return
    err = _commit_log(board, log, f"design verdict {card.id}: {outcome}")
    if err:
        print(f"{full}: verdict log {log} not committed: {err}")


def _commit_log(board: Board, log: Path, msg: str) -> str:
    """Commit the log path-only when the board repo's checkout is on its
    integration branch (an uncommitted tracked file blocks later merges).
    '' = committed or nothing to do, else the reason."""
    from .cards import git_commit_paths

    r = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=board.repo,
        capture_output=True,
        text=True,
    )
    if r.returncode:
        return ""  # not a git checkout: nothing to commit into
    if r.stdout.strip() != board.branch:
        return f"checkout is on {r.stdout.strip()}, not {board.branch}"
    try:
        rel = str(log.resolve().relative_to(board.repo.resolve()))
    except ValueError:
        return f"{log} is outside {board.repo}"
    rc, out = git_commit_paths(board.repo, [rel], msg)
    return out.strip()[:300] if rc else ""


def add_owner_note(board: Board, words: str) -> int:
    """``pump2 design note <board> "<words>"``: the owner's remark on a
    design made outside a run ("I like this one"), logged with no card."""
    from src.designloop.references import append_verdict, verdicts_path
    from src.designloop.steps import parse_config

    config = parse_config(board.design)
    log = (
        verdicts_path(board.repo, config.references, config.verdicts)
        if config
        else None
    )
    if log is None or not words.strip():
        print(
            f"error: board {board.name} has no design `references` (or the "
            "remark is empty): nothing logged",
            file=sys.stderr,
        )
        return 2
    append_verdict(log, words)
    err = _commit_log(board, log, "design remark from the owner")
    print(
        f"{board.name}: remark logged in {log}"
        + (f" (not committed: {err})" if err else "")
    )
    return 0


def design_stats(cards: Sequence[Card]) -> tuple[int, int, int] | None:
    """(picked, rejected-all, runs) of a board's designloop cards, from the
    card text (no counters); None when the board has none."""
    loop = [c for c in cards if is_designloop(c)]
    if not loop:
        return None
    picked = sum(1 for c in loop if _fm_str(c, "design_state") == "picked")
    rejected = sum(len(_RUN_REJECTED.findall(c.body)) for c in loop)
    runs = sum(len(_RUN_STARTED.findall(c.body)) for c in loop)
    return picked, rejected, runs


def _machine_comment(text: str) -> re.Pattern[str]:
    return re.compile(
        r"^### @(?:\S+/)?(?:pump2|taskpump)\b[^\n]*\n+" + re.escape(text), re.M
    )


_RUN_STARTED = _machine_comment("Design run started")
_RUN_REJECTED = _machine_comment(REJECTED_COMMENT)


def pending_records(
    boards: dict[str, Board], only: str | None = None
) -> list[dict[str, Any]]:
    """``pump2 pending`` records for designloop cards waiting for a pick."""
    from .state import list_cards

    recs: list[dict[str, Any]] = []
    for board in boards.values():
        if only and board.name != only:
            continue
        cards, _bad = list_cards(board)
        for c in cards:
            if awaiting_mockup(c):
                recs.append(_mockup_record(board, c))
                continue
            if not awaiting_pick(c):
                continue
            variants = design_variants(c)
            nums = ", ".join(str(i) for i in range(1, len(variants) + 1))
            block = "\n".join(
                [
                    f"Which design to build? Reply a number, {nums} (a line why "
                    "is welcome), or «none, because …» to reject all."
                ]
                + [f"{i}. `{b}`" for i, b in enumerate(variants, 1)]
            )
            recs.append(
                {
                    "id": f"{board.name}:{c.id}",
                    "title": _fm_str(c, "title"),
                    "board": board.name,
                    "pump": "asked",
                    "kind": "pick",
                    "phase": "",
                    "open": "",
                    "questions": [
                        {"num": str(i), "text": f"`{b}`", "default": None}
                        for i, b in enumerate(variants, 1)
                    ],
                    "block": block,
                    "tags": [
                        f"DESIGN PICK — reply a number, {nums}, or «none, because …»"
                    ],
                }
            )
    return recs


def _mockup_record(board: Board, c: Card) -> dict[str, Any]:
    mockups = design_variants(c, "design_mockups")
    nums = ", ".join(str(i) for i in range(1, len(mockups) + 1))
    block = "\n".join(
        [f"Which mockup to build to? Reply a number, {nums}."]
        + [f"{i}. `{Path(m).name}`" for i, m in enumerate(mockups, 1)]
    )
    return {
        "id": f"{board.name}:{c.id}",
        "title": _fm_str(c, "title"),
        "board": board.name,
        "pump": "asked",
        "kind": "pick",
        "phase": "",
        "open": "",
        "questions": [
            {"num": str(i), "text": f"`{Path(m).name}`", "default": None}
            for i, m in enumerate(mockups, 1)
        ],
        "block": block,
        "tags": [f"MOCKUP PICK — reply a number, {nums}"],
    }


# --- the run --------------------------------------------------------------


def design_card_state(path: Path, comment_text: str, **keys: str) -> Card:
    """A comment plus the card's design keys; the card must still exist."""
    if not path.exists():
        raise DesignError(f"{path.stem} vanished during the design run")
    add_comment(path, comment_text)
    set_keys(path, **keys)
    return read_card(path)


CARD_NOTES = 12


def variant_lines(variants: Sequence[Any]) -> list[str]:
    lines = []
    for v in variants:
        score = f" {v.critique.verdict} {v.critique.score}/10" if v.critique else ""
        lines.append(
            f"- `{v.branch}`{score}" + (f" — {v.tradeoff}" if v.tradeoff else "")
        )
        # Block remarks (dv-696e) make 40-50 notes: past the cap a count and
        # the round's critic.json, which holds them all.
        lines += [f"  - {note}" for note in v.notes[:CARD_NOTES]]
        if len(v.notes) > CARD_NOTES:
            more = f"  - +{len(v.notes) - CARD_NOTES} more"
            if v.renders:
                more += f" in `{Path(v.renders[0]).parent / 'critic.json'}`"
            lines.append(more)
        if v.renders:
            lines.append(f"  - renders: `{Path(v.renders[0]).parent}`")
    return lines


def _notify_argv(
    board: Board, full: str, text: str, images: Sequence[str]
) -> list[str]:
    if len(text) > TG_LIMIT:
        text = text[: TG_LIMIT - 1] + "…"
    return [
        str(AGENTCTL),
        "notify",
        text,
        "--profile",
        board.profile,
        "--deliver",
        "--ref",
        f"pump:{full}",
        *(a for p in images for a in ("--image", str(p))),
    ]


def _deliver(
    board: Board, full: str, text: str, images: Sequence[str], notify: Notify
) -> None:
    """Send; a failure is printed, the card already holds its state."""
    try:
        notify(_notify_argv(board, full, text, images))
    except Exception as e:  # noqa: BLE001 - the card is the record
        print(f"{full}: message not sent: {e}", file=sys.stderr)


def _prepare(
    boards: dict[str, Board], full: str, read_only: bool = False
) -> tuple[Board, Card, Any, str]:
    name, _, cid = full.partition(":")
    if not cid or name not in boards:
        raise DesignError(f"{full}: not a <board>:<id> of a pump2 board")
    board = boards[name]
    try:
        card = find_card(board, cid)
    except FileNotFoundError as e:
        raise DesignError(f"{full}: {e}") from e
    if not is_designloop(card):
        raise DesignError(f"{full} has no `{DESIGNLOOP_LABEL}` label")
    state = _fm_str(card, "design_state")
    if not read_only and (
        card.fm.get("status") == "done"
        or state
        in (
            DESIGN_AWAITING,
            DESIGN_AWAITING_MOCKUP,
            "picked",
        )
    ):
        raise DesignError(
            f"{full} is past its run (design_state={state or 'none'}, "
            f"status={card.fm.get('status')})"
        )
    from src.designloop.steps import missing_scripts, parse_config

    config = parse_config(board.design)
    if not config:
        raise DesignError(
            f"board {board.name} has no valid `design` block in boards.json "
            "(render / gate argv, python)"
        )
    missing = missing_scripts(config, board.repo)
    if missing:
        raise DesignError(
            f"{board.repo} lacks the design scripts {', '.join(missing)} "
            "(the board's render / gate: a card on that board delivers them)"
        )
    from src.designloop.references import references_path

    refs = references_path(board.repo, config.references)
    if refs is not None and not refs.is_file():
        raise DesignError(
            f"board {board.name}: design.references {config.references} is "
            f"missing in {board.repo} (fix the path in boards.json or restore it)"
        )
    from src.designloop.steps import missing_kit

    kit = missing_kit(config, board.repo)
    if kit:
        raise DesignError(
            f"board {board.name}: design.kit file(s) {', '.join(kit)} missing in "
            f"{board.repo} (fix the path in boards.json or commit the image)"
        )
    from src.designloop.mockups import spec_problem

    problem = spec_problem(board.repo, config.style_spec)
    if ": invalid:" in problem or ": unreadable:" in problem:
        # A missing or draft spec is not used yet (seed -> approve is the
        # owner's step; the run goes on the kit flow); an invalid one is a
        # config mistake.
        raise DesignError(f"board {board.name}: design.{problem}")
    screen = _fm_str(card, "design_screen")
    if not screen:
        raise DesignError(f"{full} has no `design_screen:` (a features.yaml screen id)")
    return board, card, config, screen


def _sheet(steps: Any, mockups: Sequence[Path], labels: Sequence[str]) -> Path | None:
    """The mockups as one numbered grid PDF (the review renderer, run by the
    board python); returns the PDF, its cover PNG sits next to it, else None."""
    from src.designloop.review_pdf import item, render_with

    out = Path(mockups[0]).parent / "mockups-review.pdf"
    spec = {
        "title": "Mockups to choose from",
        "sections": [
            {
                "heading": "Mockups",
                "layout": "grid",
                "columns": 3,
                "note": "Answer with the mockup number.",
                "items": [item(m, label) for m, label in zip(mockups, labels)],
            }
        ],
    }
    try:
        render_with(steps.config.python, spec, out)
    except Exception:  # noqa: BLE001 - the card holds the mockups anyway
        return None
    return out


async def _step0(
    board: Board, card: Card, config: Any, brief: Any, steps: Any, full: str
) -> tuple[str, list[str], Path | None, list[Path]]:
    """Design step 0. Returns (what next, notes, chosen mockup, all mockups):
    ``off`` (no mockup mode / it failed: the kit flow), ``build`` (build to
    the chosen mockup) or ``owner`` (the owner picks)."""
    from src.designloop.mockups import (
        MockupError,
        make_mockups,
        mockup_step,
        pick_mockup,
    )
    from src.model_router import ModelCallError

    step = mockup_step(config=config, repo=board.repo)
    if step is None:
        return "off", [], None, []
    picked = _fm_str(card, "design_mockup")
    resumed = _fm_str(card, "design_state") == DESIGN_MOCKUP_PICKED
    if resumed and picked and Path(picked).is_file():
        return "build", [], Path(picked), [Path(picked)]
    saved = owner_pick(board, config, _fm_str(card, "design_screen"))
    if saved is not None:
        return (
            "build",
            [f"the owner's saved mockup for this screen: {saved}"],
            saved,
            [saved],
        )
    labels = _labels(card)
    try:
        mockups = await make_mockups(
            spec=step.spec,
            brief_text=brief.text,
            out_dir=steps.out / "mockups",
            count=step.count,
            paid=PAID_MOCKUP_LABEL in labels or step.paid,
            preset=step.preset,
            element=_fm_str(card, "design_element"),
            key_ref=step.key_ref,
        )
    except (MockupError, ModelCallError) as exc:
        return "off", [f"mockups skipped, kit flow instead: {str(exc)[:300]}"], None, []
    if OWNER_MOCKUP_LABEL in labels:
        return "owner", [], None, mockups
    try:
        index, reasons = await pick_mockup(brief.text, mockups, step.spec)
    except ModelCallError as exc:
        return (
            "build",
            [f"mockup pick failed ({exc}); took mockup 1"],
            mockups[0],
            mockups,
        )
    why = reasons[index] if index < len(reasons) else ""
    note = f"critic picked mockup {index + 1} of {len(mockups)}" + (
        f": {why}" if why else ""
    )
    return "build", [note], mockups[index], mockups


def owner_pick(board: Board, config: Any, screen: str) -> Path | None:
    """The mockup the owner already chose for this screen, kept in the board
    repo as ``owner-mockups/<screen>.png`` next to the references file. A run
    builds to it and draws no new mockups (owner 14.11.26: a run without
    ``--mockup`` drew four new ones although his pick existed)."""
    from src.designloop.references import references_path

    refs = references_path(board.repo, getattr(config, "references", None))
    if refs is None or not screen:
        return None
    path = refs.parent / "owner-mockups" / f"{screen}.png"
    return path if path.is_file() else None


_MACHINE_AUTHORS = ("pump2", "taskpump")


def card_instructions(card: Card) -> tuple[tuple[str, str], ...]:
    """The card's comments not written by the pump, oldest first, as
    (label, text): the owner's / devops' instructions for the builder."""
    from .series import comments

    out = []
    for c in comments(card):
        if c.author in _MACHINE_AUTHORS or not c.body.strip():
            continue
        when = c.when.strftime("%Y-%m-%d %H:%M") if c.when else ""
        out.append((f"{c.author} {when}".strip(), c.body.strip()))
    return tuple(out)


def design_brief(board: Board, card: Card, config: Any, screen: str, full: str) -> Any:
    """The run's brief: the board docs, the card's instructions and, when the
    board's style spec is approved, the spec as its only token section."""
    from src.designloop.brief import build_brief
    from src.designloop.mockups import spec_path, spec_problem
    from src.designloop.stylespec import StyleSpecError, load_spec

    try:
        brief = build_brief(board.repo, screen, design_change_request(card))
    except (KeyError, ValueError, OSError) as exc:
        raise DesignError(f"{full}: no brief: {exc}") from exc
    spec = ""
    if config.style_spec and not spec_problem(board.repo, config.style_spec):
        try:
            spec = load_spec(spec_path(board.repo, config.style_spec)).text()
        except (OSError, StyleSpecError):
            spec = ""
    return dataclasses.replace(
        brief,
        card_instructions=card_instructions(card),
        spec=spec,
        spec_path=config.style_spec if spec else "",
    )


def _board_steps(board: Board, card: Card, config: Any, screen: str, full: str) -> Any:
    import src.designloop.steps as steps_mod

    return steps_mod.BoardSteps(
        repo=board.repo,
        card=card.id,
        screen=screen,
        worktrees=board.repo / WORKTREES,
        out=board.repo / ".screens" / "design" / card.id,
        config=config,
        profile=board.profile,
        board=board.name,
        agentjob=AGENTJOB,
        template=DESIGN_TEMPLATE,
        ask_context=json.dumps(
            {"task_id": full, "task_title": _fm_str(card, "title"), "stage": "design"}
        ),
        restart=_fm_str(card, "design_state") == DESIGN_REJECTED,
    )


def cmd_prompt(boards: dict[str, Board], full: str) -> int:
    """``pump2 design prompt <board:id>``: print variant 1's first-draft
    builder prompt. Read-only: no model call, no card or git write; a card
    past its run is shown too. With a picked mockup on the card, the prompt
    is the mockup build's."""
    try:
        board, card, config, screen = _prepare(boards, full, read_only=True)
        brief = design_brief(board, card, config, screen, full)
        steps = _board_steps(board, card, config, screen, full)
        from src.designloop.mockups import mockup_step

        picked = _fm_str(card, "design_mockup")
        step = mockup_step(config=config, repo=board.repo)
        if step is not None and picked and Path(picked).is_file():
            steps.use_mockup(Path(picked), step.spec_path, step.spec.text())
        branches = [steps.branch(n) for n in range(1, steps.variant_count + 1)]
        text = steps.prompt(brief, 1, branches, ())
    except DesignError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except (OSError, ValueError) as e:
        print(f"error: {full}: no prompt: {e}", file=sys.stderr)
        return 2
    print(text)
    return 0


def cmd_design(
    boards: dict[str, Board],
    full: str,
    by: str = "owner",
    notify: Notify | None = None,
) -> int:
    """Run a designloop card's loop to a stop. Outside the tick: started by
    ``job_start(command=…)`` / ``scripts/bgjob run``. Refusals: rc 2, one
    line, card unchanged."""
    try:
        board, card, config, screen = _prepare(boards, full)
        brief = design_brief(board, card, config, screen, full)
    except DesignError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    notify = notify or send

    from src.designloop.loop import run_loop
    from src.model_router.job import install_job_router

    steps = _board_steps(board, card, config, screen, full)
    design_card_state(
        card.path, f"Design run started by {by}.", design_state=DESIGN_RUNNING
    )
    sent: list[tuple[str, list[str]]] = []

    async def deliver(text: str, images: Sequence[Path]) -> None:
        # Held until the card says awaiting_pick: a fast reply must find it.
        sent.append((text, [str(p) for p in images]))

    step_notes: list[str] = []
    try:
        install_job_router(HOME_REPO)
        mode, step_notes, mockup, mockups = asyncio.run(
            # The owner's verdicts and references go into the mockups too
            # (once the feed mockup ignored his «none, because …»).
            _step0(board, card, config, steps.with_owner(brief), steps, full)
        )
        if mode == "owner":
            return _ask_mockup(board, card, steps, mockups, full, notify)
        if mockup is not None:
            from src.designloop.mockups import mockup_step

            step = mockup_step(config=config, repo=board.repo)
            assert step is not None
            steps.use_mockup(mockup, step.spec_path, step.spec.text())
            set_keys(
                card.path,
                design_mockup=str(mockup),
                design_mockups="[" + ", ".join(str(m) for m in mockups) + "]",
            )
        steps.prepare()
        outcome = asyncio.run(
            run_loop(
                brief,
                deliver=deliver,
                title=f"{full} {screen}",
                mockup=mockup,
                **steps.kwargs(),
            )
        )
    except Exception as exc:  # noqa: BLE001 - every failure reaches the card
        design_card_state(card.path, f"Design run failed: {exc}", design_state="failed")
        _deliver(
            board, full, f"⚠️ design run {full} failed: {str(exc)[:400]}", [], notify
        )
        print(f"{full}: design run failed: {exc}")
        return 1
    head = (
        f"{outcome.message}\n\nStop: {outcome.stop}, {outcome.rounds} critic round(s)."
    )
    fresh = step_notes + list(getattr(steps, "fresh", []))
    body = "\n".join([head, "", *variant_lines(outcome.variants), *fresh])
    if fresh:
        note = "\n".join(fresh)
        if sent:
            sent[0] = (f"{sent[0][0]}\n\n{note}", sent[0][1])
        else:
            sent.append((note, []))
    if outcome.offered:
        design_card_state(
            card.path,
            body + f"\n\nReview PDF: `{outcome.collage}`",
            design_state=DESIGN_AWAITING,
            design_variants="[" + ", ".join(v.branch for v in outcome.offered) + "]",
        )
    else:
        design_card_state(card.path, body, design_state="failed")
    for text, images in sent:
        _deliver(board, full, text, images, notify)
    print(f"{full}: design run {outcome.stop} after {outcome.rounds} round(s)")
    return 0


def _ask_mockup(
    board: Board,
    card: Card,
    steps: Any,
    mockups: Sequence[Path],
    full: str,
    notify: Notify,
) -> int:
    """``ownermockup``: the card waits in ``awaiting_mockup``; the sheet goes
    to the owner; his number comes back through ``pump2 answer``."""
    from src.designloop.loop import review_files
    from src.designloop.mockups import mockup_offer_text

    labels = [Path(m).name for m in mockups]
    sheet = _sheet(steps, mockups, labels)
    text = mockup_offer_text(full, mockups, [])
    design_card_state(
        card.path,
        text + (f"\n\nSheet: `{sheet}`" if sheet else ""),
        design_state=DESIGN_AWAITING_MOCKUP,
        design_mockups="[" + ", ".join(str(m) for m in mockups) + "]",
    )
    images = (
        [str(p) for p in review_files(sheet)] if sheet else [str(m) for m in mockups]
    )
    _deliver(board, full, text, images, notify)
    print(f"{full}: {len(mockups)} mockups drawn; waiting for the owner's pick")
    return 0


# --- the style spec: seed and approve -----------------------------

SEED_SYSTEM = """\
You are a senior product designer writing a style spec for a mobile app in
web-design terms, from the attached images (the owner's picks / UI kit).
Answer YAML only, no prose, exactly these top-level keys: status (draft),
palette (role -> {hex: "#RRGGBB", role: "<where it is used>"}), typography
(family, roles -> {weight, size} in px), radii (element -> px), borders
(element -> {width, color hex}), elevation (element -> {offset_y, blur,
opacity 0..1}), glass (blur px, tint hex with alpha, highlight hex with
alpha), spacing (list of px), components (button_primary, button_ghost, chip,
segmented, card, tab_bar, input, toggle, sheet, badge, avatar -> {description,
states: {default, pressed, disabled, ...}}). Measure, do not guess wildly."""


def _spec_target(board: Board) -> tuple[Any, Path]:
    from src.designloop.steps import parse_config

    config = parse_config(board.design)
    if config is None or not config.style_spec:
        raise DesignError(
            f"board {board.name} has no `design.style_spec` in boards.json"
        )
    from src.designloop.mockups import spec_path

    return config, spec_path(board.repo, config.style_spec)


def spec_seed(board: Board, notify: Notify | None = None) -> int:
    """``pump2 design spec seed <board>``: a draft spec from the board's kit
    and owner reference images (one ``design_spec`` call through the router),
    written as ``status: draft`` and sent to the owner."""
    from src.designloop.stylespec import StyleSpecError, parse_spec, set_status
    from src.model_router import CallRequest, ModelCallError, get_router
    from src.model_router.job import install_job_router

    try:
        config, path = _spec_target(board)
        from src.designloop.steps import owner_inputs

        owner = owner_inputs(config, board.repo)
    except (DesignError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    images = tuple(str(Path(p).resolve()) for p in (*owner.kit, *owner.images))
    if not images:
        print(
            f"error: board {board.name} has no kit / reference images to seed from",
            file=sys.stderr,
        )
        return 2
    install_job_router(HOME_REPO)
    prompt = "Write the style spec of these images.\n\n# The owner's references\n" + (
        owner.references or "(none)"
    )
    try:
        result = asyncio.run(
            get_router().complete(
                CallRequest(
                    purpose="design_spec",
                    prompt=prompt,
                    system_prompt=SEED_SYSTEM,
                    images=images,
                    timeout_seconds=600.0,
                )
            )
        )
        text = re.sub(r"^```(?:yaml)?\s*|```\s*$", "", result.text.strip(), flags=re.M)
        text = set_status(text.strip() + "\n", "draft")
        parse_spec(text)
    except (ModelCallError, StyleSpecError) as e:
        print(f"error: spec not drafted: {e}", file=sys.stderr)
        return 1
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    msg = (
        f"🎨 {board.name}: a draft style spec is written to {path.name} "
        f"({len(images)} images). Read it; `pump2 design spec approve "
        f"{board.name}` makes it steer the design runs."
    )
    _deliver(
        board,
        board.name,
        msg + "\n\n" + text[: TG_LIMIT - len(msg) - 4],
        [],
        notify or send,
    )
    print(f"{board.name}: draft spec written to {path}")
    return 0


def spec_approve(board: Board) -> int:
    """``pump2 design spec approve <board>``: draft -> approved, committed
    path-only in the board repo."""
    from src.designloop.stylespec import StyleSpecError, parse_spec, set_status

    try:
        _config, path = _spec_target(board)
        text = path.read_text(encoding="utf-8")
        parse_spec(text)
    except (DesignError, OSError, StyleSpecError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    path.write_text(set_status(text, "approved"), encoding="utf-8")
    err = _commit_log(board, path, "design: style spec approved by the owner")
    print(
        f"{board.name}: style spec {path} approved"
        + (f" (not committed: {err})" if err else "")
    )
    return 0
