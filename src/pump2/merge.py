"""The merge stage, no model: integration branch -> card branch, the board's
test command under a memory cap, green -> fast-forward the integration branch.
Only this module writes to the integration branch (docs/pump.md)."""

from __future__ import annotations

import os
import re
import shlex
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import criteria, slots
from .state import (
    WORKTREES,
    Board,
    Card,
    add_comment,
    read_card,
    set_pump2,
    set_status,
    tail,
    worktree_path,
)

TESTRUN = Path(__file__).resolve().parents[2] / "scripts" / "testrun"
BUSY_RC = 75  # EX_TEMPFAIL: no heavy slot, state untouched, try next tick
_FAILED = re.compile(r"^(?:FAILED|ERROR) (\S+)", re.M)


def failed_ids(out: str) -> list[str]:
    return sorted(set(_FAILED.findall(out)))


TIMEOUT_RC = 124  # the coreutils timeout(1) code


def run_tests(
    board: Board,
    wt: Path,
    test: str,
    log: Path | None = None,
    timeout: float | None = None,
) -> tuple[int, str, list[str]]:
    """The one way pump2 runs tests: ``test`` through bash under scripts/testrun
    and the memory cap. Returns (rc, output, the command that was run); past
    ``timeout`` seconds the run is killed and rc is TIMEOUT_RC."""
    mem = board.mem or os.environ.get("PUMP_TEST_MEMORY_MAX", "")
    cmd = [str(TESTRUN)] + (["--mem", mem] if mem else []) + ["--", "bash", "-c", test]
    rc, out = sh(cmd, wt, log, timeout=timeout)
    return rc, out, cmd


@dataclass
class Result:
    outcome: str  # merged | red | conflict | busy | error | mergefix
    detail: str = ""


def _env(cwd: Path) -> dict[str, str]:
    """Merge commits need an identity; use the pump's own only if git has none."""
    env = dict(os.environ)
    ident = subprocess.run(
        ["git", "var", "GIT_COMMITTER_IDENT"], cwd=cwd, capture_output=True, env=env
    )
    if ident.returncode:
        env.update(GIT_COMMITTER_NAME="pump2", GIT_AUTHOR_NAME="pump2")
        env.update(GIT_COMMITTER_EMAIL="pump2@localhost")
        env.update(GIT_AUTHOR_EMAIL="pump2@localhost")
    return env


def sh(
    cmd: list[str], cwd: Path, log: Path | None = None, timeout: float | None = None
) -> tuple[int, str]:
    """Run, return (rc, combined output); append the command and output to log.
    Past ``timeout`` the whole process group is killed: rc TIMEOUT_RC."""
    p = subprocess.Popen(
        cmd,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=_env(cwd),
        start_new_session=timeout is not None,
    )
    try:
        out, _ = p.communicate(timeout=timeout)
        rc = p.returncode
    except subprocess.TimeoutExpired:
        os.killpg(p.pid, signal.SIGKILL)
        out, _ = p.communicate()
        out = (out or "") + f"\npump2: killed after {timeout:.0f} s\n"
        rc = TIMEOUT_RC
    out = out or ""
    if log:
        with open(log, "a") as f:
            f.write(f"$ {shlex.join(cmd)}  (cwd {cwd})\n{out}rc={rc}\n")
    return rc, out


def ensure_ignored(repo: Path) -> str:
    """Keep .pump-worktrees/ out of ``git status`` without touching a tracked
    file: the line goes to the repo's .git/info/exclude. The tracked
    .gitignore line is committed on the card branch (ensure_worktree)."""
    rc, out = sh(["git", "rev-parse", "--git-common-dir"], repo)
    if rc:
        return ""
    ex = (repo / out.strip()) / "info" / "exclude"
    cur = ex.read_text() if ex.exists() else ""
    if any(ln.strip().rstrip("/") == WORKTREES for ln in cur.splitlines()):
        return ""
    ex.parent.mkdir(parents=True, exist_ok=True)
    sep = "" if cur.endswith("\n") or not cur else "\n"
    ex.write_text(cur + sep + WORKTREES + "/\n")
    return f"added {WORKTREES}/ to {ex}"


IGNORES = (WORKTREES + "/", ".pump/")


def commit_ignores(wt: Path) -> str:
    """Commit the missing ignore lines in .gitignore on the worktree's branch."""
    gi = wt / ".gitignore"
    cur = gi.read_text() if gi.exists() else ""
    have = {ln.strip().rstrip("/") for ln in cur.splitlines()}
    miss = [i for i in IGNORES if i.rstrip("/") not in have]
    if not miss:
        return ""
    sep = "" if cur.endswith("\n") or not cur else "\n"
    gi.write_text(cur + sep + "\n".join(miss) + "\n")
    msg = "pump2: ignore " + ", ".join(miss)
    sh(["git", "add", ".gitignore"], wt)
    rc, out = sh(["git", "commit", "-q", "-m", msg, ".gitignore"], wt)
    return "" if rc == 0 else f"commit of .gitignore failed: {out}"


def ensure_branch(board: Board, branch: str) -> bool:
    """Create the card branch from the integration branch if missing."""
    ref = f"refs/heads/{branch}"
    rc, _ = sh(["git", "rev-parse", "--verify", "-q", ref], board.repo)
    if rc == 0:
        return False
    rc, out = sh(["git", "branch", branch, board.branch], board.repo)
    if rc:
        raise RuntimeError(f"git branch {branch} {board.branch} failed: {out}")
    return True


def ensure_worktree(board: Board, card_id: str, branch: str) -> Path:
    wt = worktree_path(board, card_id)
    if not wt.exists():
        ensure_ignored(board.repo)
        rc, out = sh(["git", "worktree", "add", str(wt), branch], board.repo)
        if rc:
            raise RuntimeError(f"git worktree add {wt} {branch} failed: {out}")
        err = commit_ignores(wt)
        if err:
            raise RuntimeError(err)
    (wt / ".pump").mkdir(exist_ok=True)
    return wt


def _stop(
    card: Card, stage: str, title: str, cmd: str, rc: int, out: str, **kw: object
) -> None:
    kw.setdefault("mergefix", None)  # the next merge attempt gets its own
    kw.setdefault("stray", None)
    set_pump2(card.path, stage=stage, job=None, **kw)
    add_comment(
        card.path, f"{title}\ncommand: {cmd}\nexit code: {rc}\n```\n{tail(out)}\n```"
    )


def _integration_checkout(board: Board, log: Path) -> Path:
    """Where the integration branch is checked out: the live repo if it is on
    that branch (as today), else a private worktree of it."""
    _, cur = sh(["git", "rev-parse", "--abbrev-ref", "HEAD"], board.repo)
    if cur.strip() == board.branch:
        return board.repo
    wt = board.repo / WORKTREES / "_integration"
    if not wt.exists():
        ensure_ignored(board.repo)
        sh(["git", "worktree", "add", str(wt), board.branch], board.repo, log)
    return wt


def dirty_overlap(board: Board, branch: str) -> list[str]:
    """Uncommitted files in the live integration checkout that the fast-forward
    to ``branch`` would overwrite (another session's work in progress, e.g. an
    agent's docs/DECISIONS.md line). [] when the integration
    branch lives in pump2's own worktree, which nobody else edits."""
    _, cur = sh(["git", "rev-parse", "--abbrev-ref", "HEAD"], board.repo)
    if cur.strip() != board.branch:
        return []
    _, st = sh(["git", "status", "--porcelain", "--untracked-files=all"], board.repo)
    dirty = {ln[3:].split(" -> ")[-1].strip('"') for ln in st.splitlines() if ln}
    _, d = sh(["git", "diff", "--name-only", board.branch, branch], board.repo)
    # pump2 itself writes card state there: never someone else's work
    return sorted(f for f in dirty & set(d.split()) if not is_card(f))


STRAY_MSG = "pump2: commit stray uncommitted edit ({}) left by another session"
MARKERS = re.compile(r"^(?:<<<<<<<|>>>>>>>)(?: |$)", re.M)


def is_noncode(path: str) -> bool:
    """Docs and cards: a change to these alone does not need a test run.
    Everything else counts as code, comment-only edits included."""
    return path.endswith(".md") or path.startswith(("docs/", ".deviz/", ".pump/"))


CARDS = ".deviz/tasks/"
CARDS_MSG = "pump2: card files follow {}"


def is_card(path: str) -> bool:
    """pump2 owns .deviz/tasks/: it writes card state in the live tree, so the
    integration branch's version of a card file always wins."""
    return path.startswith(CARDS)


def resolve_card_conflicts(wt: Path, log: Path | None = None) -> list[str]:
    """Resolve the in-progress merge's conflicts in card files by taking the
    integration branch's side (theirs). Returns the other conflicted files."""
    rest = []
    for f in unmerged(wt):
        if not is_card(f):
            rest.append(f)
            continue
        rc, _ = sh(["git", "checkout", "--theirs", "--", f], wt, log)
        if rc == 0:
            sh(["git", "add", "--", f], wt, log)
        else:  # deleted on the integration side
            sh(["git", "rm", "-q", "--", f], wt, log)
    return rest


def align_cards(board: Board, wt: Path, log: Path | None = None) -> bool:
    """Set the card branch's card files back to the integration branch's
    committed version, so the fast-forward never touches pump2's live card
    writes. A branch-only card file the live tree does not have (a follow-up
    card) is kept. Card files only: no test run needed. True = committed."""
    _, d = sh(["git", "diff", "--name-only", board.branch, "HEAD"], wt)
    todo = []
    for f in sorted(set(d.split())):
        if not is_card(f):
            continue
        rc, _ = sh(["git", "cat-file", "-e", f"{board.branch}:{f}"], wt)
        if rc == 0:
            sh(["git", "checkout", board.branch, "--", f], wt, log)
        elif (board.repo / f).exists():
            sh(["git", "rm", "-q", "--", f], wt, log)
        else:
            continue
        todo.append(f)
    if not todo:
        return False
    msg = CARDS_MSG.format(board.branch)
    rc, _ = sh(["git", "commit", "-q", "-m", msg, "--", *todo], wt, log)
    return rc == 0


def unmerged(wt: Path) -> list[str]:
    _, out = sh(["git", "diff", "--name-only", "--diff-filter=U"], wt)
    return sorted(set(out.split()))


def merging(wt: Path) -> bool:
    rc, _ = sh(["git", "rev-parse", "-q", "--verify", "MERGE_HEAD"], wt)
    return rc == 0


def commit_stray(board: Board, files: list[str], log: Path) -> tuple[list[str], str]:
    """Commit another session's uncommitted docs edit in the live integration
    checkout, as-is, so the fast-forward can land. Only files that
    are still dirty and are docs; never code. Returns (committed, error)."""
    where = _integration_checkout(board, log)
    _, st = sh(["git", "status", "--porcelain", "--untracked-files=all"], where)
    dirty = {ln[3:].split(" -> ")[-1].strip('"') for ln in st.splitlines() if ln}
    todo = sorted(f for f in files if f in dirty)
    code = [f for f in todo if not is_noncode(f)]
    if code:
        return [], "refusing to commit someone's uncommitted code: " + ", ".join(code)
    if not todo:
        return [], ""
    rc, out = sh(["git", "add", "--", *todo], where, log)
    if rc == 0:
        msg = STRAY_MSG.format(", ".join(todo))
        rc, out = sh(["git", "commit", "-q", "-m", msg, "--", *todo], where, log)
    return (todo, "") if rc == 0 else ([], out)


def merge(board: Board, card: Card, locks: Path | None = None) -> Result:
    st = card.pump2
    branch = st.get("branch")
    if not branch:
        _stop(card, "stopped", "merge: card has no pump2.branch", "-", 1, "")
        return Result("error", "no branch")
    note = ensure_ignored(board.repo)
    wt = ensure_worktree(board, card.id, branch)
    log = wt / ".pump" / "merge.log"
    if note:
        log.open("a").write(note + "\n")
    return _attempt(board, card, wt, log, locks)


FACTS_COMMITS = 30  # integration commits listed in merge-facts
FACTS_FILES = 60  # integration-side files listed (the card's own first)
DIRTY_FILE_BYTES = 8_000  # per uncommitted file's diff in merge-facts
DIRTY_TOTAL_BYTES = 30_000  # all of them; past it, names only


def _tail_bytes(text: str, cap: int) -> str:
    """The last whole lines of ``text`` that fit in ``cap`` bytes."""
    if len(text.encode()) <= cap:
        return text
    kept: list[str] = []
    size = 0
    for ln in reversed(text.split("\n")):
        size += len(ln.encode()) + 1
        if size > cap:
            break
        kept.append(ln)
    return "(older lines cut)\n" + "\n".join(reversed(kept))


def _handoff(
    board: Board,
    card: Card,
    wt: Path,
    why: str,
    green: str | None,
    *,
    base: str = "",
    conflicts: list[str] | None = None,
    dirty: list[str] | None = None,
) -> Result:
    """The mechanical merge did not land: write ``.pump/merge-facts.md`` and
    hand the card to the mergefix stage. ``base``: the commit the
    integration branch's new commits are counted from."""
    facts = merge_facts(
        board, card.id, wt, why, green, base=base, conflicts=conflicts, dirty=dirty
    )
    (wt / ".pump" / "merge-facts.md").write_text(facts)
    set_pump2(card.path, stage="mergefix", job=None, mergefix=_mergefixes(card) + 1)
    add_comment(
        card.path, f"merge: {why}\nto mergefix: {wt / '.pump' / 'merge-facts.md'}"
    )
    return Result("mergefix", why)


def merge_facts(
    board: Board,
    card_id: str,
    wt: Path,
    why: str,
    green: str | None,
    *,
    base: str = "",
    conflicts: list[str] | None = None,
    dirty: list[str] | None = None,
) -> str:
    """The text of ``merge-facts.md`` (read-only): counts, the last
    FACTS_COMMITS integration commits, the files each side changes, the
    byte-capped uncommitted diffs and the conflicted files."""
    out = [f"# merge facts: {card_id}", "", f"why: {why}", "", "## Green tip"]
    out.append(green or "not tested (the board tests have not run on this merge)")
    since = base or green or "HEAD"
    nocards = ["--", ".", f":(exclude){CARDS}"]  # pump2's own, never the card's
    _, lg = sh(["git", "log", "--oneline", f"{since}..{board.branch}", *nocards], wt)
    commits = lg.splitlines()
    shown = commits[:FACTS_COMMITS]
    out += ["", f"## {board.branch} commits since {since[:12]}: {len(commits)}"]
    out += ["```", "\n".join(shown) or "(none)", "```"]
    if len(commits) > len(shown):
        out.append(f"({len(commits) - len(shown)} older not shown)")
    _, side_raw = sh(["git", "diff", "--name-only", f"{since}..{board.branch}"], wt)
    _, names_raw = sh(["git", "diff", "--name-only", f"{board.branch}...{since}"], wt)
    mine = [f for f in names_raw.split() if not is_card(f)]
    theirs = [f for f in side_raw.split() if not is_card(f)]
    both = set(mine)
    theirs = [f for f in theirs if f in both] + [f for f in theirs if f not in both]
    out += ["", f"## Files {board.branch} changes since {since[:12]}: {len(theirs)}"]
    out.append("\n".join(theirs[:FACTS_FILES]) or "(none)")
    if len(theirs) > FACTS_FILES:
        out.append(f"({len(theirs) - FACTS_FILES} more not shown; both sides' first)")
    out += ["", "## Files the card branch changes", "\n".join(mine) or "(none)"]
    if dirty:
        out += ["", f"## Uncommitted files in the live checkout {board.repo}"]
        budget = DIRTY_TOTAL_BYTES
        for f in dirty:
            _, d = sh(["git", "diff", "--", f], board.repo)
            if not d.strip():
                p = board.repo / f
                d = "(untracked)\n" + (
                    p.read_text(errors="replace") if p.is_file() else ""
                )
            d = _tail_bytes(tail(d, 200), DIRTY_FILE_BYTES)
            if len(d.encode()) > budget:
                out.append(
                    f"### {f} (diff not shown: over the {DIRTY_TOTAL_BYTES} B budget)"
                )
                continue
            budget -= len(d.encode())
            out += [f"### {f}", "```", d, "```"]
    if conflicts:
        out += ["", "## Conflicted files (merge left in progress in the worktree)"]
        out += [f"- {f}" for f in conflicts]
    return "\n".join(out) + "\n"


MERGEFIX_MAX = 3  # mergefixes per merge attempt for docs-only re-failures
KILLED_RC = 137  # the merge test run died of SIGKILL (testrun's OOM kill)
KILLED_MAX = 2  # reruns of a killed merge test run before the owner is asked


def _mergefixes(card: Card) -> int:
    """Mergefixes so far in this merge attempt (an old ``true`` counts 1)."""
    v = read_card(card.path).pump2.get("mergefix")
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 1


def _second(card: Card) -> bool:
    """A mergefix already ran in this merge attempt."""
    return _mergefixes(card) > 0


def _again(card: Card, files: list[str]) -> str:
    """After a mergefix, may a new failure over ``files`` go to mergefix again?
    "" = yes (none yet, or docs only and under MERGEFIX_MAX), else
    why it stops for the owner."""
    m = _mergefixes(card)
    if not m:
        return ""
    if not all(is_noncode(f) for f in files):
        return "second merge failure after a mergefix"
    if m >= MERGEFIX_MAX:
        return f"merge failure after {m} mergefixes in this merge attempt"
    return ""


def _incoming(board: Board, wt: Path, green: str) -> list[str]:
    """Files the integration branch changed since ``green`` was merged."""
    _, d = sh(["git", "diff", "--name-only", f"{green}...{board.branch}"], wt)
    return sorted(f for f in set(d.split()) if not is_card(f))


def _after_sync(board: Board, card: Card, wt: Path, log: Path) -> Result | None:
    """Run the board's ``after_sync`` argv in the card worktree and commit what
    it changed on the card branch. None = go on to the tests."""
    if not board.after_sync:
        return None
    argv = board.after_sync
    rc, out = sh(argv, wt, log)
    if rc:
        _stop(card, "stopped", "merge: after_sync FAILED", shlex.join(argv), rc, out)
        return Result("error", out)
    _, st = sh(["git", "status", "--porcelain", "--untracked-files=all"], wt)
    if not st.strip():
        return None
    msg = f"{card.id}: after_sync: {shlex.join(argv)}"
    rc, cout = sh(["git", "add", "-A"], wt, log)
    if rc == 0:
        rc, cout = sh(["git", "commit", "-q", "-m", msg], wt, log)
    if rc:
        _stop(
            card,
            "stopped",
            "merge: commit of the after_sync changes FAILED",
            shlex.join(argv),
            rc,
            out + cout,
        )
        return Result("error", cout)
    return None


def _attempt(
    board: Board, card: Card, wt: Path, log: Path, locks: Path | None
) -> Result:
    st = card.pump2
    n = int(st.get("attempt") or 0)
    test = board.test
    if st.get("green") and _still_green(board, wt, str(st["green"])):
        # tests passed on this exact branch tip, which already holds the
        # current integration branch: only the fast-forward was waiting
        stray = [str(f) for f in st.get("stray") or []]
        if stray:
            err = _land_stray(board, card, wt, log, stray)
            if err:
                return err
        return _fast_forward(board, card, wt, log, test)
    mcmd = ["git", "merge", "--no-ff", "--no-edit", board.branch]
    rc, out = sh(mcmd, wt, log)
    if rc and unmerged(wt):
        if not resolve_card_conflicts(wt, log):  # card files only
            rc, out = sh(["git", "commit", "-q", "--no-edit"], wt, log)
    if rc:
        files = unmerged(wt)
        what = "CONFLICT" if "CONFLICT" in out else "git merge FAILED"
        why = _again(card, files)
        if what == "CONFLICT" and files and not why:
            return _handoff(
                board,
                card,
                wt,
                f"CONFLICT merging {board.branch} into the card branch",
                None,
                conflicts=files,
            )
        sh(["git", "merge", "--abort"], wt, log)
        again = f"\n{why}; stopped for the owner" if why else ""
        _stop(
            card,
            "stopped",
            f"merge: {what} merging {board.branch} into the card branch{again}",
            shlex.join(mcmd),
            rc,
            out,
        )
        return Result("conflict", out)
    err = _after_sync(board, card, wt, log)
    if err:
        return err
    if not test:
        _stop(
            card,
            "stopped",
            "merge: board has no test command (pump2_test / test)",
            "-",
            1,
            "",
        )
        return Result("error", "no test command")
    with slots.heavy_slot(board.name, locks) as ok:
        if not ok:
            return Result("busy")
        rc, out, cmd = run_tests(board, wt, test, log)
    if rc == KILLED_RC:
        # SIGKILL is not a verdict on the code: a card was once sent back
        # to build by a host-wide OOM, and the build then had nothing to change
        k = int(st.get("killed") or 0) + 1
        over = k > KILLED_MAX
        _stop(
            card,
            "stopped" if over else "merge",
            f"merge: tests killed (exit {rc}, memory), not judged; "
            + ("stopped for the owner" if over else f"retry {k}/{KILLED_MAX}"),
            shlex.join(cmd),
            rc,
            out,
            killed=None if over else k,
        )
        return Result("busy" if not over else "error", "killed")
    if rc:
        failed = failed_ids(out)
        listing = "failing tests:\n" + "\n".join(failed) + "\n" if failed else ""
        title = f"merge: tests RED (attempt {n + 1})\n{listing}".rstrip()
        if n >= 1:
            _stop(
                card,
                "stopped",
                title + "\nsecond red: stopped for the owner",
                shlex.join(cmd),
                rc,
                out,
            )
        else:
            _stop(
                card,
                "build",
                title + "\nback to build",
                shlex.join(cmd),
                rc,
                out,
                attempt=n + 1,
            )
        return Result("red", "\n".join(failed))
    _, tip = sh(["git", "rev-parse", "HEAD"], wt)
    set_pump2(card.path, green=tip.strip(), killed=None)
    return _fast_forward(board, card, wt, log, test)


def _land_stray(
    board: Board, card: Card, wt: Path, log: Path, stray: list[str]
) -> Result | None:
    """mergefix said commit-stray: commit the docs edit on the integration
    branch, bring that commit into the card branch (docs only: no test run)
    and keep the green result for the new tip. None = go on to the ff."""
    done, err = commit_stray(board, stray, log)
    if err:
        _stop(card, "stopped", "merge: commit of the stray edit FAILED", "-", 1, err)
        return Result("error", err)
    if done:
        mcmd = ["git", "merge", "--no-ff", "--no-edit", board.branch]
        rc, out = sh(mcmd, wt, log)
        if rc:
            files = unmerged(wt)
            why = _again(card, files)
            if files and _second(card) and not why:
                green = str(read_card(card.path).pump2.get("green") or "") or None
                set_pump2(card.path, stray=None)
                return _handoff(
                    board,
                    card,
                    wt,
                    f"CONFLICT merging the stray commit of {board.branch}",
                    green,
                    conflicts=files,
                )
            sh(["git", "merge", "--abort"], wt, log)
            _stop(
                card,
                "stopped",
                f"merge: merging the stray commit of {board.branch} FAILED "
                f"({why or 'second merge failure after a mergefix'})",
                shlex.join(mcmd),
                rc,
                out,
            )
            return Result("conflict", out)
        _, tip = sh(["git", "rev-parse", "HEAD"], wt)
        set_pump2(card.path, green=tip.strip())
    set_pump2(card.path, stray=None)
    return None


def _still_green(board: Board, wt: Path, green: str) -> bool:
    _, tip = sh(["git", "rev-parse", "HEAD"], wt)
    if tip.strip() != green:
        return False
    rc, _ = sh(["git", "merge-base", "--is-ancestor", board.branch, green], wt)
    return rc == 0


def _fast_forward(board: Board, card: Card, wt: Path, log: Path, test: str) -> Result:
    """Green: fast-forward the integration branch to the card branch. The only
    writer of the integration branch (besides commit_stray's docs commit)."""
    where = _integration_checkout(board, log)
    if align_cards(board, wt, log):
        _, tip = sh(["git", "rev-parse", "HEAD"], wt)
        if read_card(card.path).pump2.get("green"):
            set_pump2(card.path, green=tip.strip())
    ffcmd = ["git", "merge", "--ff-only", card.pump2["branch"]]
    rc, out = sh(ffcmd, where, log)
    if rc:
        green = str(read_card(card.path).pump2.get("green") or "") or None
        moved = "Not possible to fast-forward" in out
        files = [] if moved else dirty_overlap(board, card.pump2["branch"])
        again = ""
        if moved and _second(card):
            # docs only came in since green: mergefix again, bounded
            again = (
                _again(card, _incoming(board, wt, green)) if green else "no green tip"
            )
        if again or (files and _second(card)):
            why = (
                f"{board.branch} moved again"
                if moved
                else "uncommitted changes in " + ", ".join(files)
            )
            if again.startswith("merge failure after"):
                why += f" ({again})"
            _stop(
                card,
                "stopped",
                f"merge: tests green, fast-forward of {board.branch} in {where} "
                f"FAILED again after a mergefix: {why}; stopped for the owner",
                shlex.join(ffcmd),
                rc,
                out,
            )
            return Result("error", out)
        if moved:
            # the integration branch moved during/after the tests: merge it
            # into the card branch here (git writes stay in code), then let
            # mergefix judge what came in
            mcmd = ["git", "merge", "--no-ff", "--no-edit", board.branch]
            mrc, mout = sh(mcmd, wt, log)
            conflicts = resolve_card_conflicts(wt, log) if mrc else []
            if mrc and not conflicts and merging(wt):  # card files only
                mrc, mout = sh(["git", "commit", "-q", "--no-edit"], wt, log)
            if mrc and not conflicts:
                sh(["git", "merge", "--abort"], wt, log)
                _stop(
                    card,
                    "stopped",
                    "merge: git merge FAILED",
                    shlex.join(mcmd),
                    mrc,
                    mout,
                )
                return Result("error", mout)
            return _handoff(
                board,
                card,
                wt,
                f"{board.branch} moved after the tests went green",
                green,
                conflicts=conflicts,
            )
        if files:
            # someone's uncommitted edit in the live checkout (e.g.
            # docs/DECISIONS.md): mergefix decides
            return _handoff(
                board,
                card,
                wt,
                f"fast-forward of {board.branch} blocked by uncommitted changes "
                f"in {where} to " + ", ".join(files),
                green,
                dirty=files,
            )
        # anything else is not a race: stop loud with git's words
        _stop(
            card,
            "stopped",
            f"merge: tests green, fast-forward of {board.branch} in {where} FAILED",
            shlex.join(ffcmd),
            rc,
            out,
        )
        return Result("error", out)
    _, sha = sh(["git", "rev-parse", "HEAD"], where)
    sha = sha.strip()
    set_pump2(
        card.path,
        stage="done",
        job=None,
        green=None,
        mergefix=None,
        stray=None,
        merged=sha,
    )
    set_status(card.path, "done")  # the board's word for merged; releases deps
    add_comment(
        card.path,
        f"merge: merged {sha} into {board.branch}\ntests: {test}\nlog: {log}",
    )
    add_comment(card.path, criteria.summary(wt, "done"))
    return Result("merged", sha)
