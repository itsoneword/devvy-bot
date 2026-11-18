"""One tick: read the cards of each board, hold, launch, reap. State only in
the card's ``pump2:`` block and the worktree's ``.pump/`` files."""

from __future__ import annotations

import re
import subprocess
import sys
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from . import cards as cardfiles
from . import cleanup, criteria, design, freeze, identity, issues, pin, slots
from .merge import ensure_branch, ensure_worktree
from .stages import MODULES, custom, module_for, resolve
from .state import (
    PUMP2_KEYS,
    STAGES,
    Board,
    Card,
    Outcome,
    StageSpec,
    add_comment,
    enter_pump2,
    hold_reason,
    list_cards,
    now_iso,
    read_card,
    set_pump2,
    set_status,
    stop,
    tail,
    worktree_path,
)

REPO = Path(__file__).resolve().parents[2]
BGJOB = REPO / "scripts" / "bgjob"
PUMP2 = REPO / "scripts" / "pump2"
_STARTED = re.compile(r"^(?:started|queued) (\S+)", re.M)
# bgjob refusing a launch at its slot cap (BGJOB_MAX_CONCURRENT): not a
# failure of the card, the same launch is tried again next tick.
JOB_CAP = "already running (cap"
# The models.yaml `jobs:` row each model stage runs on (the one route
# table): without it every stage took the default job model (once: a
# 74-file review on Sonnet in 18 s). The merge stage has no model; resolve
# is the board's agent that every stop goes to first (dv-b8bb).
STAGE_ROUTE = {
    "analyze": "pump_analyze",
    "tests": "pump_testdesign",
    "build": "pump_implement",
    "review": "pump_review",
    "resolve": "pump_resolve",
}
# never rewritten by a board's pipeline: not a forward step of it
_OFF_PIPELINE = ("stopped", "waiting_user", "done", "resolve")
REPORTS = {"review": "review.md", "resolve": "resolve.md"}


def route_of(board: Board, stage: str) -> str:
    """The models.yaml `jobs:` row of a model stage; a board's custom stage
    ``x`` runs on ``pump_x`` (jobs.default until configured)."""
    if stage in STAGE_ROUTE:
        return STAGE_ROUTE[stage]
    if board.custom(stage):
        return f"pump_{stage}"
    raise KeyError(f"{stage!r} is not a model stage of board {board.name!r}")


def card_labels(card: Card | None) -> list[str]:
    raw = card.fm.get("labels") if card else None
    if isinstance(raw, str):
        raw = [x.strip() for x in raw.strip("[]").split(",")]
    return [str(x) for x in raw or [] if str(x).strip()]


def stage_matches(
    spec: StageSpec, props: dict[str, Any], labels: list[str], board: Board
) -> bool:
    """A custom stage's ``when`` against the card: AND across keys, OR within
    one. touches/size/kind are the analyze-set pump2 properties (a card
    without one does not match a key that needs it); labels count only when
    they are on the board's closed ``stage_labels`` list."""
    for key, want in spec.when.items():
        if key == "labels":
            have = [x for x in labels if x in board.stage_labels]
        elif key == "touches":
            t = props.get("touches")
            have = [str(x) for x in t] if isinstance(t, list) else []
        else:
            have = [str(props[key])] if props.get(key) else []
        if not set(have) & set(want):
            return False
    return True


def route_next(
    board: Board,
    stage: str,
    nxt: str,
    props: dict[str, Any] | None = None,
    labels: list[str] | None = None,
) -> str:
    """A stage module's next stage, mapped through the board's pipeline: a
    forward step goes to the first custom stage on the way whose ``when``
    matches the card (``props``: its pump2 state, ``labels``) or to ``nxt``;
    a ``nxt`` the pipeline leaves out goes to the entry after ``stage``.
    Returns, stops and a board without pipeline are left as they are."""
    names = board.stage_names()
    if not names or stage not in names or nxt in _OFF_PIPELINE:
        return nxt
    pos = names.index(stage)
    if nxt in names and names.index(nxt) <= pos:
        return nxt  # a return
    ahead = [
        n
        for n in names[pos + 1 :]
        if not (s := board.custom(n))
        or stage_matches(s, props or {}, labels or [], board)
    ]
    for name in ahead:
        if name == nxt or board.custom(name) or nxt not in ahead:
            return name
    return nxt


def warn_labels(board: Board, card: Card) -> None:
    """One comment per card naming its labels outside ``stage_labels``."""
    if not board.stage_labels or card.pump2.get("labels_warned"):
        return
    odd = [x for x in card_labels(card) if x not in board.stage_labels]
    if odd:
        add_comment(
            card.path,
            f"labels {odd} are not in this board's stage_labels "
            f"{board.stage_labels}: they switch no stage on",
        )
        set_pump2(card.path, labels_warned=True)


# (job id) -> (running, exit code or None); injectable for tests
JobStatus = Callable[[str], tuple[bool, int | None]]
# (board, card, boards file) -> job id; injectable for tests
Launcher = Callable[[Board, Card, Path | None], str]
# (board, card, stage, worktree, prompt) -> job id: a model (prompt) job
StageLauncher = Callable[[Board, Card, str, Path, str], str]
# (job id, n lines) -> the tail of the job's output
JobOutput = Callable[[str, int], str]


def bgjob_status(job_id: str) -> tuple[bool, int | None]:
    r = subprocess.run(
        [sys.executable, str(BGJOB), "status", job_id], capture_output=True, text=True
    )
    if r.returncode:
        return False, 1  # unknown job: treated as a failed one, then reported loudly
    running = re.search(r"^running: (\w+)", r.stdout, re.M)
    code = re.search(r"^exit_code: (-?\d+)", r.stdout, re.M)
    return bool(running and running.group(1) == "True"), (
        int(code.group(1)) if code else None
    )


def bgjob_launch(board: Board, card: Card, boards_file: Path | None) -> str:
    cmd = f"{PUMP2} merge {board.name}:{card.id}" + (
        f" --boards {boards_file}" if boards_file else ""
    )
    r = subprocess.run(
        [
            sys.executable,
            str(BGJOB),
            "run",
            "--cmd",
            cmd,
            "--name",
            f"pump2-merge-{card.id}",
            "--profile",
            board.profile,
            "--cwd",
            str(board.repo),
            "--quiet",
            "--force",
        ],
        capture_output=True,
        text=True,
    )
    m = _STARTED.search(r.stdout)
    if r.returncode or not m:
        raise RuntimeError(f"{cmd}: bgjob run rc={r.returncode}\n{r.stdout}{r.stderr}")
    return m.group(1)


def bgjob_output(job_id: str, n: int = 20) -> str:
    r = subprocess.run(
        [sys.executable, str(BGJOB), "output", job_id, "--tail", str(n)],
        capture_output=True,
        text=True,
    )
    text = (r.stdout + r.stderr).strip()
    if not text:  # claude buffers output.md; a box failure only reaches summary.md
        r = subprocess.run(
            [sys.executable, str(BGJOB), "output", job_id],
            capture_output=True,
            text=True,
        )
        text = tail(r.stdout + r.stderr, n)
    return text


PROMPT_ARG_MAX = 100_000  # bytes; the kernel's per-argument cap is 131072


def bgjob_launch_stage(
    board: Board, card: Card, stage: str, wt: Path, prompt: str
) -> str:
    """A prompt job: the one model-call path, run in the card's worktree."""
    name = f"pump2-{stage}-{card.id}"
    if len(prompt.encode()) > PROMPT_ARG_MAX:
        # One argv string is capped by the kernel (MAX_ARG_STRLEN, 128 KiB):
        # a 142 KB merge-fix prompt (merge facts) raised E2BIG and killed
        # every tick for 20 min. Hand the text over as a file.
        f = wt / ".pump" / f"prompt-{stage}.md"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(prompt)
        prompt = (
            f"Your full instructions for the {stage.upper()} stage of card "
            f"{card.id} are in {f} ({len(prompt)} chars). Read that whole file "
            "first (in parts if needed) and follow it exactly; it is the prompt."
        )
    r = subprocess.run(
        [
            sys.executable,
            str(BGJOB),
            "run",
            "--profile",
            board.profile,
            "--cwd",
            str(wt),
            "--name",
            name,
            "--route",
            route_of(board, stage),
            "--quiet",
            "--force",
            "--setting-sources",
            "project",
            prompt,
        ],
        capture_output=True,
        text=True,
    )
    m = _STARTED.search(r.stdout)
    if r.returncode or not m:
        raise RuntimeError(
            f"bgjob run --name {name} --cwd {wt}: rc={r.returncode}\n"
            f"{r.stdout}{r.stderr}"
        )
    return m.group(1)


def tick(
    boards: dict[str, Board],
    *,
    boards_file: Path | None = None,
    status: JobStatus = bgjob_status,
    launch: Launcher = bgjob_launch,
    now: datetime | None = None,
    only: str | None = None,
    locks: Path | None = None,
    launch_stage: StageLauncher = bgjob_launch_stage,
    output: JobOutput = bgjob_output,
    freeze_path: Path | None = None,
) -> list[str]:
    """Returns one line per action or hold, for the log. ``boards_file``
    (else the identity already in force) names this install: a card whose
    assignee is another install is never touched."""
    if boards_file is not None:
        with identity.using(boards_file):
            return _tick(
                boards,
                boards_file,
                status,
                launch,
                now,
                only,
                locks,
                launch_stage,
                output,
                freeze_path,
            )
    return _tick(
        boards,
        boards_file,
        status,
        launch,
        now,
        only,
        locks,
        launch_stage,
        output,
        freeze_path,
    )


def _retire(board: Board, card: Card) -> str:
    """A card in flight left by older code: a stage this code does not run,
    or `pump2:` keys it does not know (``PUMP2_KEYS``). The keys are dropped
    and the card goes to resolve, the old stage's report (or merge facts) as
    the stop reason. '' = nothing to do."""
    st = card.pump2
    at = str(st.get("stage") or "")
    known = at in STAGES or at == "waiting_user" or board.custom(at) is not None
    old = sorted(k for k in st if k not in PUMP2_KEYS)
    if known and not old:
        return ""
    drop: dict[str, Any] = {k: None for k in old}
    if at == "resolve":  # already there: only the old keys go
        set_pump2(card.path, **drop)
        return f"dropped keys of older code: {', '.join(old)}"
    branch = str(st.get("branch") or f"pump2/{card.id}")
    try:
        ensure_branch(board, branch)
        wt = ensure_worktree(board, card.id, branch)
    except (OSError, RuntimeError, subprocess.SubprocessError):
        wt = worktree_path(board, card.id)
    pump = wt / ".pump"
    facts = pump / "merge-facts.md"
    back = at if known else ("merge" if facts.is_file() else "build")
    report = next((f for f in (pump / f"{at}.md", facts) if f.is_file()), None)
    why = tail(report.read_text()) if report else "(no report file)"
    keys = ", ".join(f"{k}={st[k]}" for k in old) or "none"
    set_pump2(card.path, stage=back, job=None, **drop)
    msg = (
        f"{back}: left by older code at stage {at} (keys: {keys})\n"
        f"{report.name if report else 'report'}:\n```\n{why}\n```"
    )
    return f"{at} -> " + stop(board, card.path, msg)


def _tick(
    boards: dict[str, Board],
    boards_file: Path | None,
    status: JobStatus,
    launch: Launcher,
    now: datetime | None,
    only: str | None,
    locks: Path | None,
    launch_stage: StageLauncher,
    output: JobOutput,
    freeze_path: Path | None,
) -> list[str]:
    out: list[str] = []
    ours = identity.ours_check(extra={b.profile for b in boards.values()})
    for board in boards.values():
        if only and board.name != only:
            continue
        cards, bad = list_cards(board)
        out += [
            f"{board.name}:{p.name} ERROR "
            + (f"unparseable card: {e}" if p.is_file() else e)
            for p, e in bad
        ]
        for card in cards:
            if card.fm.get("status") != "in_progress":
                continue
            if not ours(card.fm.get("assignee")):
                continue  # another install's card: read-only here
            tag = f"{board.name}:{card.id}"
            if not card.pump2.get("stage"):
                # moved to in_progress by hand or a board agent; an
                # old `pump:` key (old-pump handoff) or a designloop card is not ours
                if "pump" in card.fm or design.is_designloop(card):
                    continue
                enter_pump2(card.path, "picked up from in_progress")
                card = read_card(card.path)
                out.append(f"{tag} picked up from in_progress")
            st = card.pump2
            if st["stage"] in ("done", "stopped"):
                continue
            moved = _retire(board, card)
            if moved:
                out.append(f"{tag} {moved}")
                continue
            why = hold_reason(card, board, now)
            if why:
                out.append(f"{tag} held: {why}")
                continue
            if st.get("job") and st.get("job_of", st["stage"]) != st["stage"]:
                # a merge job that already handed the card on (to resolve)
                # wrote its result: its id is not the new stage's job
                set_pump2(card.path, job=None, job_of=None)
                card = read_card(card.path)
                st = card.pump2
            if st.get("job"):
                if module_for(board, st["stage"]):
                    out.append(reap_stage(board, card, tag, status, output))
                else:
                    out.append(reap(board, card, tag, status))
                continue
            if not freeze.may_launch(card, freeze_path):
                out.append(f"{tag} held: frozen")
                continue
            if module_for(board, st["stage"]):
                out.append(start_stage(board, card, tag, launch_stage))
                continue
            if st["stage"] == "merge":
                if not slots.free(board.name, locks):
                    out.append(f"{tag} waits: no heavy slot")
                    continue
                try:
                    job = launch(board, card, boards_file)
                except RuntimeError as e:
                    if JOB_CAP in str(e):
                        out.append(f"{tag} merge waits: job cap")
                        continue
                    msg = f"merge: launch failed\n```\n{tail(str(e))}\n```"
                    stop(board, card.path, msg, True)
                    out.append(f"{tag} launch failed")
                    continue
                set_pump2(card.path, job=job, job_of="merge")
                out.append(f"{tag} merge launched {job}")
            else:
                out.append(f"{tag} stage {st['stage']}: no runner yet")
        # cards already done when the tick began give their worktrees back
        # (one that merged in this tick keeps its .pump/ one more
        # tick). Never fails a tick.
        out += cleanup.sweep(board, cards)
        try:  # GitHub issue status posts; never fails a tick
            out += issues.sweep(board, ours)
        except Exception as e:
            out.append(f"{board.name} issue sweep failed: {e}")
        # a moved board's card edits (this tick's and the task tools' since
        # the last one) become one boards-repo commit
        err = cardfiles.commit_cards(board.tasks, f"pump2 tick: {board.name} cards")
        if err:
            out.append(f"{board.name} cards not committed: {tail(err, 3)}")
        try:  # the board's pin after its actions; never fails a tick
            pin.refresh(board, now=now)
        except Exception as e:
            out.append(f"{board.name} pin not refreshed: {e}")
    try:  # one pin for all boards in the Pump topic
        pin.send_all(list(boards.values()))
    except Exception as e:
        out.append(f"pin not sent: {e}")
    return out


def reap(board: Board, card: Card, tag: str, status: JobStatus) -> str:
    """A job that is gone but left the card in its stage crashed or was busy."""
    job = card.pump2["job"]
    running, code = status(job)
    if running:
        return f"{tag} {card.pump2['stage']} running {job}"
    stage = card.pump2["stage"]
    set_pump2(card.path, job=None, job_of=None)
    if stage != "merge" or code == 0:
        return f"{tag} job {job} finished"  # merge wrote its own result
    if code == 75:
        return f"{tag} job {job} busy (no heavy slot); next tick"
    log = worktree_path(board, card.id) / ".pump" / "merge.log"
    text = log.read_text() if log.exists() else ""
    msg = f"merge: job {job} died without a result\ncommand: pump2 merge {tag}"
    return _crashed(board, card, tag, "attempt", f"{msg}\nexit code: {code}", text)


def _crashed(board: Board, card: Card, tag: str, key: str, msg: str, out: str) -> str:
    """A job that died: one automatic rerun (counter ``key``), then a stop."""
    msg += f"\n```\n{tail(out)}\n```"
    if int(card.pump2.get(key) or 0) >= 1:
        set_pump2(card.path, **{key: None})
        stop(board, card.path, msg)
    else:
        set_pump2(card.path, **{key: 1})
        add_comment(card.path, msg + "\nrerunning once")
    return f"{tag} {msg.splitlines()[0]}"


def start_stage(board: Board, card: Card, tag: str, launch: StageLauncher) -> str:
    """Branch + worktree + prompt job for a model stage."""
    stage = card.pump2["stage"]
    branch = card.pump2.get("branch") or f"pump2/{card.id}"
    jobs = int(card.pump2.get("jobs") or 0)
    if jobs >= criteria.JOB_CEILING:
        wt = worktree_path(board, card.id)
        stop(
            board,
            card.path,
            f"{stage}: job ceiling {criteria.JOB_CEILING} reached, stopped "
            f"for the owner\n\n" + criteria.summary(wt, "stopped: job ceiling"),
            True,
        )
        return f"{tag} {stage} job ceiling {criteria.JOB_CEILING} reached"
    try:
        made = ensure_branch(board, branch)
        wt = ensure_worktree(board, card.id, branch)
        plan = wt / ".pump" / "plan.md"
        if (  # keep it for the second pass; a rejected plan stays for its rerun
            stage == "analyze" and plan.exists() and not card.pump2.get("format_retry")
        ):
            plan.replace(wt / ".pump" / "plan.prev.md")
        spec = board.custom(stage)
        report = spec.report if spec else REPORTS.get(stage)
        old = wt / ".pump" / report if report else None
        if old and old.exists():  # a rerun never judges the last report
            old.replace(old.with_suffix(".prev.md"))  # review: sees its remarks
        text = module_for(board, stage).prompt(board, read_card(card.path), wt)
        note = wt / ".pump" / resolve.NOTE_FILE
        if stage != "resolve" and note.exists():
            text += f"\nNote from the resolve step (rerun):\n{note.read_text()}\n"
        if stage in MODULES and board.pipeline:
            extra = custom.prev_reports(board, wt)
            if extra:  # what the board's custom stages wrote (or returned)
                text += "\nReports of this board's custom stages:\n\n" + extra + "\n"
        job = launch(board, card, stage, wt, text)
    except (RuntimeError, OSError) as e:  # one card must never kill the tick
        if JOB_CAP in str(e):  # bgjob's slot cap: transient, same state next tick
            return f"{tag} {stage} waits: job cap"
        # a launcher failure would hit resolve too; a prompt's OSError is the card's
        msg = f"{stage}: launch failed\n```\n{tail(str(e))}\n```"
        stop(board, card.path, msg, isinstance(e, RuntimeError))
        return f"{tag} {stage} launch failed"
    set_pump2(
        card.path, job=job, job_of=stage, branch=branch, since=now_iso(), jobs=jobs + 1
    )
    return f"{tag} {stage} launched {job}" + (
        f" (branch {branch} created)" if made else ""
    )


def reap_stage(
    board: Board, card: Card, tag: str, status: JobStatus, output: JobOutput
) -> str:
    job = card.pump2["job"]
    stage = card.pump2["stage"]
    running, code = status(job)
    if running:
        return f"{tag} {stage} running {job}"
    wt = worktree_path(board, card.id)
    set_pump2(card.path, job=None, job_of=None)
    tail_text = output(job, 20)
    log = wt / ".pump" / f"job-{stage}.log"
    if log.parent.exists():
        log.write_text(output(job, 200) + "\n")
    if code != 0:
        cmd = f"bgjob run --name pump2-{stage}-{card.id} --cwd {wt}  (job {job})"
        msg = f"{stage}: job {job} failed\ncommand: {cmd}\nexit code: {code}"
        return _crashed(board, card, tag, "retry", msg, tail_text)
    res: Outcome = module_for(board, stage).finish(board, read_card(card.path), wt)
    if stage != "resolve":
        (wt / ".pump" / resolve.NOTE_FILE).unlink(missing_ok=True)
    card = read_card(card.path)
    res.next = route_next(
        board, stage, res.next, {**card.pump2, **res.state}, card_labels(card)
    )
    keys = dict(res.state, retry=None)
    if res.next == "stopped":
        return f"{tag} {stage} -> {stop(board, card.path, res.comment, **keys)}"
    add_comment(card.path, res.comment)
    warn_labels(board, card)
    if res.next == "waiting_user":
        set_pump2(card.path, **keys)
        set_status(card.path, "waiting_user")
    else:
        set_pump2(card.path, stage=res.next, **keys)
    return f"{tag} {stage} done -> {res.next}"
