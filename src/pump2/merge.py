"""The merge stage, no model: integration branch -> card branch, the board's
test command under a memory cap, green -> fast-forward the integration branch.
Only this module writes to the integration branch (docs/pump.md)."""

from __future__ import annotations

import json
import os
import re
import shlex
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import cards, criteria, slots
from .state import (
    WORKTREES,
    Board,
    Card,
    _split,
    add_comment,
    list_cards,
    parse_frontmatter,
    read_card,
    set_pump2,
    set_status,
    stop,
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
    outcome: str  # merged | red | conflict | busy | error
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
    board: Board,
    card: Card,
    title: str,
    cmd: list[str],
    rc: int,
    out: str,
    stage: str = "stopped",
    direct: bool = False,
    **kw: object,
) -> None:
    """``stage`` "stopped" goes through state.stop (resolve first)."""
    kw.setdefault("stray", None)
    text = f"{title}\ncommand: {shlex.join(cmd)}\nexit code: {rc}\n"
    text += f"```\n{tail(out)}\n```"
    if stage == "stopped":
        stop(board, card.path, text, direct, **kw)
        return
    set_pump2(card.path, stage=stage, job=None, **kw)
    add_comment(card.path, text)


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


def _title(text: str) -> str:
    try:
        lines, _ = _split(text)
        return str(parse_frontmatter(lines).get("title") or "").strip()
    except ValueError:
        return ""


def _show(wt: Path, spec: str) -> str | None:
    p = subprocess.run(["git", "show", spec], cwd=wt, capture_output=True, text=True)
    return p.stdout if p.returncode == 0 else None


def _refile(board: Board, card: Card, wt: Path, f: str, text: str, why: str) -> None:
    """The branch added card file ``f`` whose id names a different card
    (``why``): file the branch's card under a fresh id in the live tree and
    say so on the card, with the branch files that still name the old id."""
    old = Path(f).stem
    made = cards.refile(board, text)
    _, changed = sh(["git", "diff", "--name-only", f"{board.branch}...HEAD"], wt)
    mention = []
    for g in sorted(set(changed.split())):
        if g == f:
            continue
        try:
            if old in (wt / g).read_text(errors="replace"):
                mention.append(g)
        except OSError:
            continue
    add_comment(
        card.path,
        f"merge: the branch added card {old} ({_title(text)!r}) but {old} is "
        f"already {why}; the branch's card is kept as {made.id} ({made.path}), "
        f"the other side's {old} is unchanged.\nbranch files that still name "
        f"{old}: " + (", ".join(mention) or "(none)"),
    )


def resolve_card_conflicts(
    wt: Path,
    log: Path | None = None,
    board: Board | None = None,
    card: Card | None = None,
) -> list[str]:
    """Resolve the in-progress merge's conflicts in card files by taking the
    integration branch's side (theirs). A card file added on both sides with
    different titles is two cards: the branch's is re-filed under a fresh id
    before theirs is taken. Returns the other conflicted files."""
    rest = []
    for f in unmerged(wt):
        if not is_card(f):
            rest.append(f)
            continue
        if board is not None and card is not None and _show(wt, f":1:{f}") is None:
            ours, theirs = _show(wt, f":2:{f}"), _show(wt, f":3:{f}")
            if ours is not None and theirs is not None:
                if _title(ours) != _title(theirs):
                    why = f"{_title(theirs)!r} on {board.branch}"
                    _refile(board, card, wt, f, ours, why)
        rc, _ = sh(["git", "checkout", "--theirs", "--", f], wt, log)
        if rc == 0:
            sh(["git", "add", "--", f], wt, log)
        else:  # deleted on the integration side
            sh(["git", "rm", "-q", "--", f], wt, log)
    return rest


def _to_moved(board: Board, wt: Path, f: str, card: Card | None) -> None:
    """A pre-move branch's card file ``f`` on a moved board: the
    cards live in the boards repo, so the branch copy is dropped. A card the
    moved dir lacks (a follow-up the branch added) is created there under its
    id; one whose id holds another title there is re-filed."""
    mine = _show(wt, f"HEAD:{f}")
    if mine is None:
        return
    live = board.tasks / f[len(CARDS) :]
    if live.exists():
        theirs = live.read_text(errors="replace")
        if card is not None and _title(mine) != _title(theirs):
            _refile(board, card, wt, f, mine, f"{_title(theirs)!r} in the cards dir")
        return
    live.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(live, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        return
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(mine)
    cards.commit_cards(board.tasks, f"{board.name}: {live.stem} from {f}")


def align_cards(
    board: Board, wt: Path, log: Path | None = None, card: Card | None = None
) -> bool:
    """Set the card branch's card files back to the integration branch's
    committed version, so the fast-forward never touches pump2's live card
    writes. A branch-only card file the live tree does not have (a follow-up
    card) is kept; one whose path the live tree holds with another title is
    re-filed under a fresh id. Card files only: no test run
    needed. True = committed."""
    _, d = sh(["git", "diff", "--name-only", board.branch, "HEAD"], wt)
    todo = []
    for f in sorted(set(d.split())):
        if not is_card(f):
            continue
        rc, _ = sh(["git", "cat-file", "-e", f"{board.branch}:{f}"], wt)
        if rc == 0:
            sh(["git", "checkout", board.branch, "--", f], wt, log)
        elif board.moved:
            _to_moved(board, wt, f, card)
            sh(["git", "rm", "-q", "--", f], wt, log)
        elif (board.repo / f).exists():
            mine = _show(wt, f"HEAD:{f}")
            live = (board.repo / f).read_text(errors="replace")
            if card is not None and mine is not None and _title(mine) != _title(live):
                _refile(board, card, wt, f, mine, f"{_title(live)!r} in the live tree")
            sh(["git", "rm", "-q", "--", f], wt, log)
        else:
            continue
        todo.append(f)
    if not todo:
        return False
    msg = CARDS_MSG.format(board.branch)
    rc, _ = sh(["git", "commit", "-q", "-m", msg, "--", *todo], wt, log)
    return rc == 0


FOLLOWUPS = "followups.json"


def file_followups(board: Board, card: Card, wt: Path) -> list[str]:
    """After the fast-forward: file the build's ``.pump/followups.json`` cards
    in the live tree (todo, parent = the card) and comment their ids. An item
    whose title a child of the card already has is not filed again."""
    p = wt / ".pump" / FOLLOWUPS
    if not p.exists():
        return []
    try:
        items = json.loads(p.read_text())
    except ValueError as e:
        add_comment(card.path, f"merge: {p} is not JSON ({e}); no follow-ups filed")
        return []
    if not isinstance(items, list):
        items = []
    have = {
        str(c.fm.get("title") or "").strip()
        for c in list_cards(board)[0]
        if str(c.fm.get("parent") or "") == card.id
    }
    made = []
    for it in items:
        if not isinstance(it, dict) or not str(it.get("title") or "").strip():
            continue
        title = " ".join(str(it["title"]).split())
        if title in have:
            continue
        new = cards.create_card(
            board,
            title,
            str(it.get("body") or ""),
            labels=[str(x) for x in it.get("labels") or []],
            deps=[str(x) for x in it.get("deps") or []],
            parent=card.id,
        )
        have.add(title)
        made.append(f"{new.id} ({title})")
    if made:
        add_comment(card.path, "merge: filed follow-ups\n" + "\n".join(made))
    return made


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
        # --no-verify: pre-commit-merge-guard refuses commits to the
        # integration branch while a merge runs, and this one is that merge.
        cmd = ["git", "commit", "-q", "--no-verify", "-m", msg, "--", *todo]
        rc, out = sh(cmd, where, log)
    return (todo, "") if rc == 0 else ([], out)


def merge(board: Board, card: Card, locks: Path | None = None) -> Result:
    st = card.pump2
    branch = st.get("branch")
    if not branch:
        _stop(board, card, "merge: card has no pump2.branch", ["-"], 1, "", direct=True)
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


def _to_resolve(
    board: Board,
    card: Card,
    wt: Path,
    why: str,
    green: str | None,
    cmd: list[str],
    rc: int,
    out: str,
    *,
    conflicts: list[str] | None = None,
    dirty: list[str] | None = None,
) -> Result:
    """The mechanical merge did not land: write ``.pump/merge-facts.md`` (a
    conflict stays in progress in the worktree) and stop to resolve."""
    facts = merge_facts(
        board, card.id, wt, why, green, conflicts=conflicts, dirty=dirty
    )
    f = wt / ".pump" / "merge-facts.md"
    f.write_text(facts)
    _stop(board, card, f"merge: {why}\nfacts: {f}", cmd, rc, out)
    return Result("conflict", why)


def merge_facts(
    board: Board,
    card_id: str,
    wt: Path,
    why: str,
    green: str | None,
    *,
    conflicts: list[str] | None = None,
    dirty: list[str] | None = None,
) -> str:
    """The text of ``merge-facts.md`` (read-only): counts, the last
    FACTS_COMMITS integration commits, the files each side changes, the
    byte-capped uncommitted diffs and the conflicted files."""
    out = [f"# merge facts: {card_id}", "", f"why: {why}", "", "## Green tip"]
    out.append(green or "not tested (the board tests have not run on this merge)")
    since = green or "HEAD"
    nocards = ["--", ".", f":(exclude){CARDS}"]  # pump2's own, never the card's
    rc, _ = sh(["git", "merge-base", "--is-ancestor", since, board.branch], wt)
    _, mb = sh(["git", "merge-base", since, board.branch], wt)
    out += ["", "## Ancestry"]
    out.append(
        f"{since[:12]} is an ancestor of {board.branch}: "
        + ("yes" if rc == 0 else "no (the branch and the card diverged)")
    )
    out.append(f"merge-base: {mb.strip() or '(none)'}")
    _, lg = sh(["git", "log", "--oneline", f"{since}..{board.branch}", *nocards], wt)
    commits = lg.splitlines()
    shown = commits[:FACTS_COMMITS]
    out += ["", f"## {board.branch} commits since {since[:12]}: {len(commits)}"]
    out += ["```", "\n".join(shown) or "(none)", "```"]
    if len(commits) > len(shown):
        out.append(f"({len(commits) - len(shown)} older not shown)")
    # `since..branch` is a tree diff; when `since` is not
    # an ancestor of the branch it listed the card's own files as the
    # branch's. File lists are measured from the merge-base (three-dot).
    _, side_raw = sh(["git", "diff", "--name-only", f"{since}...{board.branch}"], wt)
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


KILLED_RC = 137  # the merge test run died of SIGKILL (testrun's OOM kill)
KILLED_MAX = 2  # reruns of a killed merge test run before resolve
MOVED_MAX = 3  # mechanical re-merges of a moved integration branch, then resolve


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
        _stop(board, card, "merge: after_sync FAILED", argv, rc, out)
        return Result("error", out)
    _, st = sh(["git", "status", "--porcelain", "--untracked-files=all"], wt)
    if not st.strip():
        return None
    msg = f"{card.id}: after_sync: {shlex.join(argv)}"
    rc, cout = sh(["git", "add", "-A"], wt, log)
    if rc == 0:
        rc, cout = sh(["git", "commit", "-q", "-m", msg], wt, log)
    if rc:
        _stop(board, card, "merge: commit of after_sync FAILED", argv, rc, out + cout)
        return Result("error", cout)
    return None


def _attempt(
    board: Board, card: Card, wt: Path, log: Path, locks: Path | None
) -> Result:
    st = card.pump2
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
        if not resolve_card_conflicts(wt, log, board, card):  # card files only
            rc, out = sh(["git", "commit", "-q", "--no-edit"], wt, log)
    if rc:
        files = unmerged(wt)
        what = "CONFLICT" if "CONFLICT" in out else "git merge FAILED"
        why = f"{what} merging {board.branch} into the card branch"
        if what == "CONFLICT" and files:
            return _to_resolve(
                board, card, wt, why, None, mcmd, rc, out, conflicts=files
            )
        sh(["git", "merge", "--abort"], wt, log)
        _stop(board, card, f"merge: {why}", mcmd, rc, out)
        return Result("conflict", out)
    err = _after_sync(board, card, wt, log)
    if err:
        return err
    if not test:
        why = "merge: board has no test command (pump2_test / test)"
        _stop(board, card, why, ["-"], 1, "", direct=True)
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
        why = f"merge: tests killed (exit {rc}, memory), not judged" + (
            "" if over else f"; retry {k}/{KILLED_MAX}"
        )
        stage = "stopped" if over else "merge"
        _stop(board, card, why, cmd, rc, out, stage, killed=None if over else k)
        return Result("busy" if not over else "error", "killed")
    if rc:
        failed = failed_ids(out)
        listing = "failing tests:\n" + "\n".join(failed) + "\n" if failed else ""
        title = f"merge: tests RED\n{listing}".rstrip()
        _stop(board, card, title, cmd, rc, out, green=None)
        return Result("red", "\n".join(failed))
    _, tip = sh(["git", "rev-parse", "HEAD"], wt)
    set_pump2(card.path, green=tip.strip(), killed=None)
    return _fast_forward(board, card, wt, log, test)


def _land_stray(
    board: Board, card: Card, wt: Path, log: Path, stray: list[str]
) -> Result | None:
    """resolve listed stray files: commit the docs edit on the integration
    branch, bring that commit into the card branch (docs only: no test run)
    and keep the green result for the new tip. None = go on to the ff."""
    done, err = commit_stray(board, stray, log)
    if err:
        _stop(board, card, "merge: commit of the stray edit FAILED", ["-"], 1, err)
        return Result("error", err)
    if done:
        mcmd = ["git", "merge", "--no-ff", "--no-edit", board.branch]
        rc, out = sh(mcmd, wt, log)
        if rc:
            files = unmerged(wt)
            why = f"merging the stray commit of {board.branch} FAILED"
            green = str(read_card(card.path).pump2.get("green") or "") or None
            if files:
                return _to_resolve(
                    board, card, wt, why, green, mcmd, rc, out, conflicts=files
                )
            sh(["git", "merge", "--abort"], wt, log)
            _stop(board, card, f"merge: {why}", mcmd, rc, out)
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
    if align_cards(board, wt, log, card):
        _, tip = sh(["git", "rev-parse", "HEAD"], wt)
        if read_card(card.path).pump2.get("green"):
            set_pump2(card.path, green=tip.strip())
    ffcmd = ["git", "merge", "--ff-only", card.pump2["branch"]]
    rc, out = sh(ffcmd, where, log)
    if rc:
        green = str(read_card(card.path).pump2.get("green") or "") or None
        if "Not possible to fast-forward" in out:
            return _moved(board, card, wt, log, test, green)
        files = dirty_overlap(board, card.pump2["branch"])
        if files:
            # someone's uncommitted edit in the live checkout (e.g.
            # docs/DECISIONS.md): resolve decides
            why = (
                f"fast-forward of {board.branch} blocked by uncommitted changes "
                f"in {where} to " + ", ".join(files)
            )
            return _to_resolve(board, card, wt, why, green, ffcmd, rc, out, dirty=files)
        why = f"tests green, fast-forward of {board.branch} in {where} FAILED"
        _stop(board, card, f"merge: {why}", ffcmd, rc, out)
        return Result("error", out)
    _, sha = sh(["git", "rev-parse", "HEAD"], where)
    sha = sha.strip()
    done = dict(stage="done", job=None, green=None, moved=None, stray=None)
    set_pump2(card.path, merged=sha, **done)
    set_status(card.path, "done")  # the board's word for merged; releases deps
    add_comment(
        card.path,
        f"merge: merged {sha} into {board.branch}\ntests: {test}\nlog: {log}",
    )
    add_comment(card.path, criteria.summary(wt, "done"))
    file_followups(board, card, wt)
    return Result("merged", sha)


def _moved(
    board: Board, card: Card, wt: Path, log: Path, test: str, green: str | None
) -> Result:
    """The integration branch moved after green: merge it in here (git writes
    stay in code). Non-code only came in: still green, ff again; code came in:
    the next merge run tests it. A conflict, or past MOVED_MAX, goes to resolve."""
    k = int(card.pump2.get("moved") or 0) + 1
    mcmd = ["git", "merge", "--no-ff", "--no-edit", board.branch]
    rc, out = sh(mcmd, wt, log)
    conflicts = resolve_card_conflicts(wt, log, board, card) if rc else []
    if rc and not conflicts and merging(wt):  # card files only
        rc, out = sh(["git", "commit", "-q", "--no-edit"], wt, log)
    why = f"{board.branch} moved after the tests went green"
    if conflicts or k > MOVED_MAX:
        why += f" ({k - 1} re-merges)" if not conflicts else ""
        return _to_resolve(
            board, card, wt, why, green, mcmd, rc, out, conflicts=conflicts
        )
    if rc:
        sh(["git", "merge", "--abort"], wt, log)
        _stop(board, card, "merge: git merge FAILED", mcmd, rc, out)
        return Result("error", out)
    _, tip = sh(["git", "rev-parse", "HEAD"], wt)
    came = _incoming(board, wt, green) if green else ["(no green tip)"]
    code = [f for f in came if not is_noncode(f)]
    set_pump2(card.path, moved=k, green=None if code else tip.strip())
    how = f"code came in, tests again: {code[:9]}" if code else "non-code: still green"
    add_comment(card.path, f"merge: {why}; re-merged ({k}/{MOVED_MAX}), {how}")
    if code:
        return Result("busy", "moved")
    return _fast_forward(board, read_card(card.path), wt, log, test)
