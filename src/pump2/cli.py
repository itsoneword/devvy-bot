"""pump2 <tick|status|doctor|pending|start|merge|answer|new|pin|freeze|thaw|
urgent|design|migrate|cards-move|feedback|run-stage>.

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
    cards,
    criteria,
    design,
    doctor,
    feedback,
    freeze,
    identity,
    migrate,
    pin,
    render,
    runner,
    series,
)
from .merge import BUSY_RC, merge
from .state import (
    RESTART_CLEARS,
    RUN_STAGES,
    Board,
    Card,
    add_comment,
    add_owner_answer,
    enter_pump2,
    find_card,
    list_cards,
    load_boards,
    now_iso,
    set_keys,
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
    nw = sub.add_parser("new", help="create a card with an id no branch or agent holds")
    nw.add_argument("board")
    nw.add_argument("--title", required=True)
    nw.add_argument("--body", default="")
    nw.add_argument("--status", default="todo")
    nw.add_argument("--label", action="append", default=[])
    nw.add_argument("--dep", action="append", default=[])
    nw.add_argument("--parent", default="")
    # no --by: the author is stamped by code (<install>/cli)
    nw.add_argument(
        "--assignee", default="", help="<install> or <install>/<profile>; empty = ours"
    )
    asg = sub.add_parser(
        "assign", help="set a card's assignee: <install>[/<profile>], - = nobody"
    )
    asg.add_argument("card", help="<board>:<card-id>")
    asg.add_argument("who")
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
    dr.add_argument(
        "--mockup", help="build to this mockup file (the owner's pick from a sprint)"
    )
    dp = dsub.add_parser(
        "prompt", help="print a card's builder prompt (read-only, no model call)"
    )
    dp.add_argument("card", help="<board>:<card-id>")
    dn = dsub.add_parser(
        "note", help="log the owner's remark on a design (no run) for the next run"
    )
    dn.add_argument("board")
    dn.add_argument("text", help="the owner's words, verbatim")
    ds = dsub.add_parser(
        "spec", help="the board's style spec: seed a draft from its images, approve"
    )
    ds.add_argument("spec_action", choices=("seed", "approve"))
    ds.add_argument("board")
    mg = sub.add_parser(
        "migrate", help="old pump (1.5/1.6) -> pump2: boards.json and cards"
    )
    mg.add_argument("--apply", action="store_true", help="write (default: dry run)")
    mg.add_argument("--dry-run", action="store_true", help="the default; no writes")
    cm = sub.add_parser("cards-move", help="move a board's cards into the boards repo")
    cm.add_argument("board")
    cm.add_argument("--apply", action="store_true", help="write (default: dry run)")
    fb = sub.add_parser("feedback", help="the owner's Feedback-topic question")
    fb.add_argument("action", choices=["preview", "ask"])
    fb.add_argument("card", help="<board:id>")
    r = sub.add_parser("run-stage")
    r.add_argument("stage")
    r.add_argument("card")
    # --boards may follow the subcommand too (the launcher appends it last)
    for p in (t, s, dc, pe, st, m, an, nw, asg, pn, fz, th, ur, dr, mg, cm, fb, r):
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
    if a.cmd == "cards-move":
        from . import cards_move

        return cards_move.run(path, boards, a.board, apply=a.apply)
    with identity.using(path):
        return _dispatch(a, boards)


def _dispatch(a: argparse.Namespace, boards: dict[str, Board]) -> int:
    if a.cmd in ("start", "merge", "answer", "urgent") or (
        a.cmd == "design" and getattr(a, "action", "") == "run"
    ):
        refused = _theirs(boards, a.card)
        if refused:
            print(refused, file=sys.stderr)
            return 2
    if a.cmd == "doctor":
        return doctor.cmd_doctor(boards, a.board)
    if a.cmd == "feedback":
        return _feedback(boards, a.action, a.card)
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
            lines += render.theirs_lines(boards, a.board)
            for board in boards.values():
                if not a.board or board.name == a.board:
                    cs = list_cards(board)[0]
                    lines.append(f"{board.name} {series.series_line(board, cs)}")
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
        return {"merged": 0, "conflict": 0, "busy": BUSY_RC}.get(
            res.outcome, 1 if res.outcome != "red" else 0
        )
    elif a.cmd == "answer":
        name, _, cid = a.card.partition(":")
        card = find_card(boards[name], cid)
        if design.is_designloop(card):
            # the owner's design pick: the same answer channel
            return design.answer_pick(card, a.text, a.card, boards[name])
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
    elif a.cmd == "new":
        if a.board not in boards:
            print(f"pump2: no board {a.board!r}", file=sys.stderr)
            return 2
        if a.assignee and not identity.valid_assignee(a.assignee):
            print(
                f"pump2: bad assignee {a.assignee!r}: {ASSIGNEE_FORM}", file=sys.stderr
            )
            return 2
        made = cards.create_card(
            boards[a.board],
            a.title,
            a.body,
            status=a.status,
            labels=a.label,
            deps=a.dep,
            parent=a.parent,
            created_by=identity.author("cli"),
            assignee=a.assignee,
        )
        print(f"{a.board}:{made.id} {made.path}")
    elif a.cmd == "assign":
        return _assign(boards, a.card, a.who)
    elif a.cmd == "freeze":
        freeze.freeze(a.by)
        print(f"pump2 frozen by {a.by}: no new launches; running jobs finish")
    elif a.cmd == "thaw":
        freeze.thaw(a.by)
        print(f"pump2 thawed by {a.by}: launches resume on the next tick")
    elif a.cmd == "urgent":
        return _urgent(boards, a.card, a.by)
    elif a.cmd == "design":
        if a.action == "note":
            if a.board not in boards:
                print(f"pump2: no board {a.board!r}", file=sys.stderr)
                return 2
            return design.add_owner_note(boards[a.board], a.text)
        if a.action == "prompt":
            return design.cmd_prompt(boards, a.card)
        if a.action == "spec":
            if a.board not in boards:
                print(f"pump2: no board {a.board!r}", file=sys.stderr)
                return 2
            if a.spec_action == "seed":
                return design.spec_seed(boards[a.board])
            return design.spec_approve(boards[a.board])
        if a.mockup:
            rc = design.use_owner_mockup(boards, a.card, a.mockup)
            if rc:
                return rc
        return design.cmd_design(boards, a.card, a.by)
    else:
        print(f"run-stage {a.stage}: not built yet (analyze/tests/build/review)")
        return 2
    return 0


ASSIGNEE_FORM = "<install> or <install>/<profile>, each [a-z0-9][a-z0-9-]*"


def _card_of(boards: dict[str, Board], full: str) -> Card | None:
    name, _, cid = full.partition(":")
    if name not in boards:
        return None
    try:
        return find_card(boards[name], cid)
    except FileNotFoundError:
        return None


def _theirs(boards: dict[str, Board], full: str) -> str:
    """'' when the card is ours (or not found: the command says so); else
    the one-line refusal naming its assignee (theirs is read-only)."""
    card = _card_of(boards, full)
    if card is None:
        return ""
    who = identity.owner_of(card.fm)
    ours = identity.ours_check(extra={b.profile for b in boards.values()})
    if ours(who):
        return ""
    return (
        f"{full}: assigned to {who} (another install), read-only here -- "
        f"`pump2 assign {full} <name>` hands it over"
    )


def _assign(boards: dict[str, Board], full: str, who: str) -> int:
    """Set (``-``: clear) a card's assignee; a code-authored comment says so."""
    card = _card_of(boards, full)
    if card is None:
        print(f"{full}: no such card on a served board", file=sys.stderr)
        return 2
    who = "" if who.strip() == "-" else who.strip()
    if who and not identity.valid_assignee(who):
        print(f"{full}: bad assignee {who!r}: {ASSIGNEE_FORM}", file=sys.stderr)
        return 2
    was = identity.owner_of(card.fm)
    set_keys(card.path, assignee=who, updated=now_iso())
    add_comment(card.path, f"assigned to {who or 'nobody'} (was {was or 'nobody'})")
    print(f"{full}: assignee {who or '(none)'}")
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


def _feedback(boards: dict[str, Board], action: str, full: str) -> int:
    """``feedback preview`` prints the v2 question (nothing sent, no state);
    ``feedback ask`` sends it now. Each is one paid model call."""
    try:
        if action == "preview":
            print(feedback.preview_card(boards, full))
            return 0
        lines = feedback.ask_card(boards, full)
    except KeyError as e:
        print(str(e).strip("'\""), file=sys.stderr)
        return 2
    for line in lines:
        print(line)
    return 0 if any(" asked " in ln for ln in lines) else 1


def feedback_tick(boards: dict) -> list[str]:
    """The owner's feature survey: at most one question per
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


# a pump2 comment's first line: legacy `### @pump2 — t`, new `### @<me>/pump2 — t`
_LAST_PUMP2 = re.compile(r"^### @(?:[a-z0-9-]+/)?pump2 [^\n]*\n(.*)", re.M)
_STAGE_PREFIX = re.compile(r"(analyze|tests|build|review|merge|resolve)\b")


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
    set_pump2(card.path, stage=at, **{k: None for k in RESTART_CLEARS})
    add_comment(card.path, f"{at}: restarted by {by} at {at}")
    set_status(card.path, "in_progress")
    print(f"{full}: restarted at stage {at}, picked up on the next tick")
    return 0


if __name__ == "__main__":
    sys.exit(main())
