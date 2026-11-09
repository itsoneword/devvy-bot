"""BUILD: plan + committed failing tests -> code that makes them pass.

The model may not touch the files in ``tests.json``; finish() checks that from
the git status, runs those tests under the memory cap, and on green commits
everything as ``<card-id>: build``.
"""

from __future__ import annotations

import shlex
from pathlib import Path

from ..merge import TIMEOUT_RC, failed_ids, run_tests, sh
from ..state import Board, Card, Outcome, card_digest, tail
from . import review
from .common import changed_files, commit, read_tests_json, stopped

# the last RED gate run (failing ids + log tail), for the rerun's prompt;
# removed on green
RED_FILE = "build-red.md"


def prompt(board: Board, card: Card, wt: Path) -> str:
    files, _ = read_tests_json(wt)
    plan = (wt / ".pump" / "plan.md").read_text()
    listing = "\n".join(f"- {f}" for f in files)
    core = board.test_core or board.test or "(none configured)"
    log = wt / ".pump" / "merge.log"
    again = ""
    if log.exists():
        again = (
            "\nA previous build was merged into the integration branch and the "
            "board tests went RED. Fix that; the end of the merge log:\n"
            f"```\n{tail(log.read_text(), 60)}\n```\n"
        )
    red = wt / ".pump" / RED_FILE
    if red.exists():
        again += (
            "\nYour previous build left the tests in tests.json RED. Fix the "
            "code (not the tests); the gate's result:\n" + red.read_text()
        )
    if not (wt / ".pump" / "tests.json").exists():
        return _docs_prompt(card, wt, plan, again)
    remarks = review.returned_remarks(wt)
    if remarks:
        again += (
            "\nReview remarks to address (the reviewer returned your earlier "
            f"build):\n{remarks}\n"
        )
    return f"""You are the BUILD stage of a task pipeline. Make the tests pass.

Repository (your working directory): {wt}

=== card {card.id} ({card.path.name}) ===
{card_digest(card)}
=== end card ===

=== plan (.pump/plan.md) ===
{plan}
=== end plan ===

The tests were written already and are committed. Test files:
{listing}

You may NOT edit these files. If a test is wrong, say so in `.pump/build.md`
and stop; do not work around it.
{again}
Make those tests pass and keep the rest of the suite green. The board's core
test command is: `{core}`. Run the new tests with the repository's python, and
the core command when you have touched shared code.

When done write `.pump/build.md`: what changed, 5-15 lines. No commits, no git
writes: the pipeline commits your work itself.
"""


def _docs_prompt(card: Card, wt: Path, plan: str, again: str) -> str:
    """A docs-only plan (analyze skips the tests stage): build writes the docs."""
    remarks = review.returned_remarks(wt)
    if remarks:
        again += (
            "\nReview remarks to address (the reviewer returned your earlier "
            f"build):\n{remarks}\n"
        )
    return f"""You are the BUILD stage of a task pipeline. This plan touches docs
only: there is no test stage. Write the changes the plan's criteria ask for.

Repository (your working directory): {wt}

=== card {card.id} ({card.path.name}) ===
{card_digest(card)}
=== end card ===

=== plan (.pump/plan.md) ===
{plan}
=== end plan ===
{again}
Do not change code. If a criterion needs a code change, say so in
`.pump/build.md` and stop.

When done write `.pump/build.md`: what changed, 5-15 lines. No commits, no git
writes: the pipeline commits your work itself.
"""


# finish runs inside the pump2 tick (TimeoutStartSec=300). A gate run past this
# got the tick killed mid-finish, the card stayed at build with no job and was
# rebuilt: once 12 Opus builds in 4 h.
GATE_TIMEOUT = 240


def finish(board: Board, card: Card, wt: Path) -> Outcome:
    if not (wt / ".pump" / "tests.json").exists():
        return _finish_docs(card, wt)
    files, err = read_tests_json(wt)
    if err or not files:
        return stopped(f"build: {err or 'tests.json lists no files'}")
    # a mergefix may delete a listed file that the integration branch removed:
    # judge the files that still exist
    listed = files
    gone = [f for f in files if not (wt / f).exists()]
    files = [f for f in files if f not in gone]
    if not files:
        return stopped(f"build: every file in tests.json is gone: {gone}")
    changed = changed_files(wt)
    touched = [f for f in changed if f in listed]  # a deletion counts too
    if touched:
        return stopped(f"build edited test files: {touched}")
    if not changed:
        return stopped("build changed nothing")
    # New files are untracked until the commit below: a test that reads
    # `git ls-files` (the export whitelist, dv-a3d1) would not see them and go
    # red. Intent-to-add lists them in the index without staging content.
    # A deleted path (git rm'd: gone from index and disk) makes `git add -N`
    # fail with "pathspec did not match" (dv-92ee); `git add -A` in commit()
    # stages the deletion anyway. A failure here never blocks a green build.
    warnings: list[str] = []
    present = [f for f in changed if (wt / f).exists()]
    if present:
        rc, out = sh(["git", "add", "--intent-to-add", "--", *present], wt)
        if rc:
            warnings.append(
                f"build: warning: git add --intent-to-add failed: {out.strip()}"
            )
    quoted = " ".join(shlex.quote(f) for f in files)
    if board.test_files:  # a non-pytest board (node --test, a shell gate)
        cmd = board.test_files.replace("{files}", quoted)
    else:
        # --no-cov only where the board's own test command uses it: a repo
        # without pytest-cov rejects the flag (exit 4 on every
        # build).
        nocov = " --no-cov" if "--no-cov" in board.test.split() else ""
        cmd = (
            f"{shlex.quote(board.test_python())} -m pytest "
            f"{quoted} -q{nocov} -p no:cacheprovider"
        )
    log = wt / ".pump" / "build-tests.log"
    rc, out, full = run_tests(board, wt, cmd, log, timeout=GATE_TIMEOUT)
    if rc == TIMEOUT_RC:
        return stopped(
            f"build: the tests in tests.json ran past {GATE_TIMEOUT} s, the "
            f"limit for a run inside the pump2 tick; not judged\n"
            f"command: {shlex.join(full)}\n```\n{tail(out)}\n```"
        )
    lines = [
        ln for ln in out.splitlines() if ln.strip() and not ln.startswith("testrun:")
    ]
    result = lines[-1] if lines else "(no output)"
    red_file = wt / ".pump" / RED_FILE
    if rc:
        ids = failed_ids(out)
        listing = ("failing tests:\n" + "\n".join(ids) + "\n") if ids else ""
        red_file.write_text(f"{listing}```\n{tail(out, 60)}\n```\n")
        msg = (
            f"build: the tests in tests.json are RED\n{listing}"
            f"command: {shlex.join(full)}\nexit code: {rc}\n```\n{tail(out)}\n```"
        )
        n = int(card.pump2.get("build_red") or 0)
        if n < 1:  # one fix round: the rerun sees the failing list
            return Outcome(
                "build", msg + "\nrerunning build once", {"build_red": n + 1}
            )
        return stopped(msg + "\nsecond red: stopped for the owner")
    red_file.unlink(missing_ok=True)
    sha, err = commit(wt, f"{card.id}: build")
    if err:
        return stopped(f"build: {err}")
    notes = wt / ".pump" / "build.md"
    body = notes.read_text().rstrip() if notes.exists() else "(build.md not written)"
    return Outcome(
        "review",
        f"build: {len(changed)} file(s) changed, committed {sha}\n"
        f"tests: {result}\n"
        + (f"not run, gone from the branch: {', '.join(gone)}\n" if gone else "")
        + "".join(w + "\n" for w in warnings)
        + body,
        {"build_red": None},
    )


def _finish_docs(card: Card, wt: Path) -> Outcome:
    """Docs-only build: nothing to run, commit what changed and go to review."""
    changed = changed_files(wt)
    if not changed:
        return stopped("build changed nothing")
    sha, err = commit(wt, f"{card.id}: build")
    if err:
        return stopped(f"build: {err}")
    notes = wt / ".pump" / "build.md"
    body = notes.read_text().rstrip() if notes.exists() else "(build.md not written)"
    return Outcome(
        "review",
        f"build: {len(changed)} file(s) changed, committed {sha}\n"
        "tests: none (docs-only plan)\n" + body,
        {"build_red": None},
    )
