"""pump2 <tick|status|doctor|pending|start|merge|answer|pin|freeze|thaw|
urgent|design|migrate|run-stage>.

The scripts/pump2 front.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

from . import (
    criteria,
    design,
    doctor,
    feedback,
    freeze,
    migrate,
    pin,
    render,
    runner,
    series,
)
from .merge import BUSY_RC, merge
from .state import (
    RUN_STAGES,
    Card,
    add_comment,
    add_owner_answer,
    enter_pump2,
    find_card,
    list_cards,
    load_boards,
    set_pump2,
    set_status,
)

DEFAULT_BOARDS = Path(__file__).resolve().parents[2] / "config/taskpump/boards.json"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="pump2")
    ap.add_argument("--boards", type=Path, default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("tick")
    t.add_argument("board", nargs="?")
    s = sub.add_parser("status")
    s.add_argument("board", nargs="?")
    s.add_argument("--json", action="store_true")
    dc = sub.add_parser("doctor", help="check a board's setup (repo, profile, test)")
    dc.add_argument("board", nargs="?")
    pe = sub.add_parser("pending", help="cards waiting on owner questions")
    pe.add_argument("board", nargs="?")
    pe.add_argument("--json", action="store_true")
    st = sub.add_parser(
        "start", help="hand a card to pump2 (stage analyze); restart a stopped one"
    )
    st.add_argument("card", help="<board>:<card-id>")
    st.add_argument("--by", default="owner")
    st.add_argument(
        "--stage",
        choices=RUN_STAGES,
        default=None,
        help="restart a stopped card at this stage, not the one it stopped in",
    )
    m = sub.add_parser("merge")
    m.add_argument("card", help="<board>:<card-id>")
    an = sub.add_parser("answer", help="answer the owner questions of a card")
    an.add_argument("card", help="<board>:<card-id>")
    an.add_argument("text")
    pn = sub.add_parser("pin", help="redraw each board's pinned summary")
    pn.add_argument("board", nargs="?")
    pn.add_argument("--dry-run", action="store_true", help="print, send nothing")
    fz = sub.add_parser("freeze", help="no new stage launches on any board")
    fz.add_argument("--by", default="owner")
    th = sub.add_parser("thaw", help="end a freeze (and its urgent card)")
    th.add_argument("--by", default="owner")
    ur = sub.add_parser("urgent", help="freeze, but let one card launch")
    ur.add_argument("card", help="<board>:<card-id>")
    ur.add_argument("--by", default="owner")
    dg = sub.add_parser("design", help="the designloop runner")
    dsub = dg.add_subparsers(dest="action", required=True)
    dr = dsub.add_parser("run", help="run a designloop card: variants, gate, critic")
    dr.add_argument("card", help="<board>:<card-id>")
    dr.add_argument("--by", default="owner")
    mg = sub.add_parser(
        "migrate", help="old pump (1.5/1.6) -> pump2: boards.json and cards"
    )
    mg.add_argument("--apply", action="store_true", help="write (default: dry run)")
    mg.add_argument("--dry-run", action="store_true", help="the default; no writes")
    r = sub.add_parser("run-stage")
    r.add_argument("stage")
    r.add_argument("card")
    # --boards may follow the subcommand too (the launcher appends it last)
    for p in (t, s, dc, pe, st, m, an, pn, fz, th, ur, dr, mg, r):
        p.add_argument("--boards", type=Path, default=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    # The default file is guarded by each board's "pump2": true; an explicit
    # --boards file (the proofs) takes every board in it.
    path = a.boards or DEFAULT_BOARDS
    if a.cmd == "migrate":  # before load_boards: it creates / fixes the file
        return migrate.run(path, apply=a.apply and not a.dry_run)
    try:
        boards = load_boards(path, guard=a.boards is None)
    except FileNotFoundError:
        print(
            f"pump2: no boards file {path} -- copy "
            "config/taskpump/boards.example.json there and edit it",
            file=sys.stderr,
        )
        return 2
    if a.cmd == "doctor":
        return doctor.cmd_doctor(boards, a.board)
    if a.cmd == "tick":
        for line in runner.tick(boards, boards_file=a.boards, only=a.board):
            print(line)
        if a.boards is None and not a.board:
            for line in release_tick():
                print(line)
            for line in feedback_tick(boards):
                print(line)
    elif a.cmd == "status":
        if a.json:
            rows = render.status_rows(boards, a.board)
            print(json.dumps(rows, ensure_ascii=False))
        else:
            head = freeze.status_line()
            lines = render.status_lines(boards, a.board)
            for board in boards.values():
                if not a.board or board.name == a.board:
                    cards = list_cards(board)[0]
                    lines.append(f"{board.name} {series.series_line(board, cards)}")
            print("\n".join(([head] if head else []) + lines))
    elif a.cmd == "pending":
        recs = render.pending_records(boards, a.board)
        recs += design.pending_records(boards, a.board)
        if a.json:
            print(json.dumps(recs, ensure_ascii=False, indent=1))
        else:
            print("\n".join(render.pending_lines(recs)).rstrip())
    elif a.cmd == "pin":
        for board in boards.values():
            if a.board and board.name != a.board:
                continue
            text = pin.refresh(board, dry_run=a.dry_run)
            if a.dry_run:
                print(f"=== {board.name} ({board.profile})\n{text}\n")
        if not a.dry_run:
            pin.send_all(list(boards.values()))
    elif a.cmd == "start":
        return _start(boards, a.card, a.by, a.stage)
    elif a.cmd == "merge":
        name, _, cid = a.card.partition(":")
        board = boards[name]
        res = merge(board, find_card(board, cid))
        print(f"{res.outcome}: {res.detail}".rstrip(": "))
        return {"merged": 0, "mergefix": 0, "busy": BUSY_RC}.get(
            res.outcome, 1 if res.outcome != "red" else 0
        )
    elif a.cmd == "answer":
        name, _, cid = a.card.partition(":")
        card = find_card(boards[name], cid)
        if design.is_designloop(card):
            # the owner's design pick: the same answer channel
            return design.answer_pick(card, a.text, a.card)
        if card.fm.get("status") != "waiting_user" or not card.pump2.get("stage"):
            print(
                f"{a.card}: not waiting for an answer (status {card.fm.get('status')})"
            )
            return 2
        add_owner_answer(card.path, a.text)
        set_status(card.path, "in_progress")
        print(
            f"{a.card}: answer recorded, stage {card.pump2['stage']} reruns next tick"
        )
    elif a.cmd == "freeze":
        freeze.freeze(a.by)
        print(f"pump2 frozen by {a.by}: no new launches; running jobs finish")
    elif a.cmd == "thaw":
        freeze.thaw(a.by)
        print(f"pump2 thawed by {a.by}: launches resume on the next tick")
    elif a.cmd == "urgent":
        return _urgent(boards, a.card, a.by)
    elif a.cmd == "design":
        return design.cmd_design(boards, a.card, a.by)
    else:
        print(f"run-stage {a.stage}: not built yet (analyze/tests/build/review)")
        return 2
    return 0


RELEASE = Path(__file__).resolve().parents[2] / "scripts" / "release"


def release_tick() -> list[str]:
    """The release front's periodic part (daily check, reminders,
    reaps) rides this tick: best-effort, a failure is a log line only.
    A frozen pump2 (or an unreadable freeze state) starts no new release
    check, as the old taskpump tick did; reaps and reminders still run."""
    if not RELEASE.exists():
        return []
    st = freeze.read()
    argv = [sys.executable, str(RELEASE), "tick"]
    if st.frozen or st.error:
        argv.append("--frozen")
    try:
        r = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=300,
        )
    except (OSError, subprocess.SubprocessError) as e:
        return [f"release tick failed: {e}"]
    lines = [ln for ln in r.stdout.splitlines() if ln.strip()]
    if r.returncode:
        err = " ".join(r.stderr.split())[-300:]
        lines.append(f"release tick rc={r.returncode}: {err}")
    return lines


def feedback_tick(boards: dict) -> list[str]:
    """The owner's feature survey (dv-a42c): at most one question per
    interval, none while one is open. Best effort: a failure is a log line."""
    try:
        return feedback.run(boards)
    except Exception as e:  # noqa: BLE001 - must never break the tick
        return [f"feedback tick failed: {e}"]


def _urgent(boards: dict, full: str, by: str) -> int:
    name, _, cid = full.partition(":")
    if name not in boards:
        print(f"{full}: board {name!r} is not served by pump2", file=sys.stderr)
        return 2
    try:
        card = find_card(boards[name], cid)
    except FileNotFoundError as e:
        print(f"{full}: {e}", file=sys.stderr)
        return 2
    if card.pump2.get("stage") in (None, "", "done", "stopped"):
        print(f"{full}: not running in pump2", file=sys.stderr)
        return 2
    freeze.urgent(full, card.path, by)
    print(f"pump2 frozen; only {full} launches until it is done or stopped")
    return 0


def _start(boards: dict, full: str, by: str, stage: str | None = None) -> int:
    """What the owner did by hand: stage analyze, then in_progress. On a
    stopped card: a restart at the stage it stopped in (or ``stage``)."""
    name, _, cid = full.partition(":")
    if name not in boards:
        print(f"{full}: board {name!r} is not served by pump2")
        return 2
    try:
        card = find_card(boards[name], cid)
    except FileNotFoundError as e:
        print(f"{full}: {e}")
        return 2
    if design.is_designloop(card):
        print(f"{full}: a designloop card, run by `pump2 design run`, never a stage")
        return 2
    if card.pump2.get("stage") == "stopped":
        return _restart(card, full, by, stage)
    if stage:
        print(f"{full}: --stage only restarts a stopped card")
        return 2
    if card.pump2.get("stage"):
        print(f"{full}: already in pump2 (stage {card.pump2['stage']})")
        return 2
    enter_pump2(card.path, f"owner: started by {by}")
    set_status(card.path, "in_progress")
    print(f"{full}: started in pump2 (stage analyze), picked up on the next tick")
    return 0


_LAST_PUMP2 = re.compile(r"^### @pump2\b[^\n]*\n(.*)", re.M)
_STAGE_PREFIX = re.compile(r"(analyze|tests|build|review|mergefix|merge)\b")
# keys of a stopped run that a restart drops: its job, retry and fix-round
# counters; `jobs` and `returns` stay (the card's cost guards)
_RESTART_CLEARS = (
    "job",
    "job_of",
    "retry",
    "format_retry",
    "build_red",
    "tests_red",
    "stopped_at",
)


def stopped_stage(card: Card) -> str:
    """The stage a stopped card stopped in: ``stopped_at``, else (a card
    stopped before that key) the prefix of its last @pump2 comment."""
    at = card.pump2.get("stopped_at")
    if at in RUN_STAGES:
        return str(at)
    found = _LAST_PUMP2.findall(card.body)
    m = _STAGE_PREFIX.match(found[-1]) if found else None
    return m.group(1) if m else "analyze"


def _restart(card: Card, full: str, by: str, stage: str | None) -> int:
    jobs = int(card.pump2.get("jobs") or 0)
    if jobs >= criteria.JOB_CEILING:
        print(
            f"{full}: not restarted: job ceiling reached ({jobs} of "
            f"{criteria.JOB_CEILING} jobs); the owner decides"
        )
        return 2
    at = stage or stopped_stage(card)
    set_pump2(card.path, stage=at, **{k: None for k in _RESTART_CLEARS})
    add_comment(card.path, f"{at}: restarted by {by} at {at}")
    set_status(card.path, "in_progress")
    print(f"{full}: restarted at stage {at}, picked up on the next tick")
    return 0


if __name__ == "__main__":
    sys.exit(main())
