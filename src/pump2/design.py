"""``pump2 design run <board:id>``: the designloop runner's card I/O (moved
here from the old pump).

A card labelled ``designloop`` is never a pump2 stage card (``pump2 start``
refuses it): this runner generates its variants, gates and critiques them
(``src/designloop/loop.py``) and sends one collage to the board's chat. The
card then waits in ``design_state: awaiting_pick``; the owner's number comes
back through the one answer channel, ``pump2 pending`` / ``pump2 answer``.

Card keys: ``design_state`` running → awaiting_pick → picked (status done),
or failed; ``design_variants`` = the offered branches in the owner's
numbering; ``design_pick`` / ``design_branch`` = his choice.

Locking: pump2's tick holds no card lock and never writes a card without a
``pump2:`` stage, which a designloop card never has; card writes here are
atomic (``state.set_keys``) instead.
"""

from __future__ import annotations

import asyncio
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


def design_variants(card: Card) -> list[str]:
    raw = card.fm.get("design_variants")
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


def drop_task_ids(text: str) -> str:
    return re.sub(r"\b(?:[\w-]+:)?[a-z]+-[0-9a-z]{4}\b:?", " ", text, flags=re.I)


def pick_of_reply(text: str, n: int | None = None) -> int | None:
    """The variant number of a chat reply ("2", "app:dv-d001: 2", "v2"); with
    ``n``, only 1..n counts. Anything else is None: asked again, never guessed."""
    m = re.fullmatch(
        r"\s*(?:v|#|variant\s*|вариант\s*)?(\d+)\s*[.!]?\s*",
        drop_task_ids(text),
        re.IGNORECASE,
    )
    if not m:
        return None
    k = int(m.group(1))
    if k < 1 or (n is not None and k > n):
        return None
    return k


def design_change_request(card: Card) -> str:
    """The card's Description section: the problem the designer solves."""
    m = re.search(
        r"^## Description\s*\n(.*?)(?=^## |^### @|\Z)", card.body, re.M | re.S
    )
    text = (m.group(1) if m else "").strip()
    return text or _fm_str(card, "title")


# --- answer / pending -----------------------------------------------------


def answer_pick(card: Card, text: str, full: str = "") -> int:
    """``pump2 answer`` on a designloop card. A variant number closes it;
    anything else is refused (rc 2) and changes nothing."""
    full = full or card.id
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
    if not k:
        choices = ", ".join(str(i) for i in range(1, len(variants) + 1))
        print(
            f"{full}: not a variant number, nothing changed. Ask the owner "
            f"again: which design to build, {choices or '(none offered)'}?",
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
    return 0


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
            if not awaiting_pick(c):
                continue
            variants = design_variants(c)
            nums = ", ".join(str(i) for i in range(1, len(variants) + 1))
            block = "\n".join(
                [f"Which design to build? Reply a number, {nums}."]
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
                    "tags": [f"DESIGN PICK — reply a number, {nums}"],
                }
            )
    return recs


# --- the run --------------------------------------------------------------


def design_card_state(path: Path, comment_text: str, **keys: str) -> Card:
    """A comment plus the card's design keys; the card must still exist."""
    if not path.exists():
        raise DesignError(f"{path.stem} vanished during the design run")
    add_comment(path, comment_text)
    set_keys(path, **keys)
    return read_card(path)


def variant_lines(variants: Sequence[Any]) -> list[str]:
    lines = []
    for v in variants:
        score = f" {v.critique.verdict} {v.critique.score}/10" if v.critique else ""
        lines.append(
            f"- `{v.branch}`{score}" + (f" — {v.tradeoff}" if v.tradeoff else "")
        )
        lines += [f"  - {note}" for note in v.notes]
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


def _prepare(boards: dict[str, Board], full: str) -> tuple[Board, Card, Any, str]:
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
    if card.fm.get("status") == "done" or state in (DESIGN_AWAITING, "picked"):
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
    screen = _fm_str(card, "design_screen")
    if not screen:
        raise DesignError(f"{full} has no `design_screen:` (a features.yaml screen id)")
    return board, card, config, screen


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
        from src.designloop.brief import build_brief

        try:
            brief = build_brief(board.repo, screen, design_change_request(card))
        except (KeyError, ValueError, OSError) as exc:
            raise DesignError(f"{full}: no brief: {exc}") from exc
    except DesignError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    notify = notify or send

    import src.designloop.steps as steps_mod
    from src.designloop.loop import run_loop
    from src.model_router.job import install_job_router

    steps = steps_mod.BoardSteps(
        repo=board.repo,
        card=card.id,
        screen=screen,
        worktrees=board.repo / WORKTREES,
        out=board.repo / ".screens" / "design" / card.id,
        config=config,
        profile=board.profile,
        agentjob=AGENTJOB,
        template=DESIGN_TEMPLATE,
        ask_context=json.dumps(
            {"task_id": full, "task_title": _fm_str(card, "title"), "stage": "design"}
        ),
    )
    design_card_state(
        card.path, f"Design run started by {by}.", design_state=DESIGN_RUNNING
    )
    sent: list[tuple[str, list[str]]] = []

    async def deliver(text: str, images: Sequence[Path]) -> None:
        # Held until the card says awaiting_pick: a fast reply must find it.
        sent.append((text, [str(p) for p in images]))

    try:
        steps.prepare()
        install_job_router(HOME_REPO)
        outcome = asyncio.run(
            run_loop(brief, deliver=deliver, title=f"{full} {screen}", **steps.kwargs())
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
    fresh = list(getattr(steps, "fresh", []))
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
            body + f"\n\nCollage: `{outcome.collage}`",
            design_state=DESIGN_AWAITING,
            design_variants="[" + ", ".join(v.branch for v in outcome.offered) + "]",
        )
    else:
        design_card_state(card.path, body, design_state="failed")
    for text, images in sent:
        _deliver(board, full, text, images, notify)
    print(f"{full}: design run {outcome.stop} after {outcome.rounds} round(s)")
    return 0
