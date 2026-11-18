"""TESTS: plan -> failing tests under ``tests/`` + ``.pump/tests.json``.

The model writes the files and runs them once; pump2 (not the model) commits
exactly the listed files on the card branch as ``<card-id>: tests``.
"""

from __future__ import annotations

from pathlib import Path

from .. import criteria as cr
from ..merge import sh
from ..state import Board, Card, Outcome, card_digest
from .common import changed_files, commit, read_tests_json, stopped

# the error of a rejected tests run, for the rerun's prompt
TESTS_DIR = "tests"  # every listed test file lives under it
RED_FILE = "tests-rejected.md"


def prompt(board: Board, card: Card, wt: Path) -> str:
    plan = (wt / ".pump" / "plan.md").read_text()
    red = wt / ".pump" / RED_FILE
    again = (
        "\nYour previous tests were rejected; fix this (keep the good tests):\n"
        + red.read_text()
        if red.exists()
        else ""
    )
    example = (
        "tests/x.test.mjs"
        if board.test_files.split()[:1] == ["node"]
        else "tests/unit/test_x.py"
    )
    return f"""You are the TESTS stage of a task pipeline. Write the tests first.

Repository (your working directory): {wt}

=== card {card.id} ({card.path.name}) ===
{card_digest(card)}
=== end card ===

=== plan (.pump/plan.md: goal, acceptance criteria, notes) ===
{plan}
=== end plan ===

Write FAILING tests under `tests/` (one or more files, following the style of
the existing tests) that encode the acceptance criteria. Do NOT implement the
feature. Run the new tests once and check they fail for the right reason (the
feature is missing), not because of a typo or a broken import of your own.

Name each test for its criterion: every `[BLOCK]` criterion with
`check: test` gets at least one test named `test_C<n>_<what>` (Python
`def test_C2_empty_state`, node `test("test_C2_empty_state", …)`); a missing id
stops the card.
{again}
Then write `.pump/tests.json` with exactly this shape, paths relative to the
repository root, every path under `tests/`:
{{"files": ["{example}"]}}

Write nothing else: no source changes, no other files. No commits, no git
writes: the pipeline commits your test files itself.
"""


def _rerun(card: Card, wt: Path, msg: str) -> Outcome:
    """One rerun of the tests stage (its prompt shows ``msg``), then a stop."""
    n = int(card.pump2.get("tests_red") or 0)
    if n < 1:
        (wt / ".pump" / RED_FILE).write_text(msg + "\n")
        return Outcome("tests", msg + "\nrerunning tests once", {"tests_red": 1})
    return stopped(msg + "\nsecond time: stopped for the owner")


def _missing_names(card: Card, wt: Path, files: list[str]) -> str:
    """The criterion-name error ('' when every BLOCK test criterion has one)."""
    if not cr.frozen(card.pump2):
        return ""
    need = [
        c.id for c in cr.plan_criteria(wt) if c.level == "BLOCK" and c.check == "test"
    ]
    missing = [i for i in need if i not in cr.tested_ids(wt, files, need)]
    if not missing:
        return ""
    return (
        "tests: BLOCK criteria with check: test have no test named "
        f"test_<id>_…: {', '.join(missing)}"
    )


def finish(board: Board, card: Card, wt: Path) -> Outcome:
    files, err = read_tests_json(wt)
    if err:
        return stopped(f"tests: {err}")
    if not files:
        return stopped("tests: tests.json lists no files")
    bad = [
        f
        for f in files
        if Path(f).is_absolute()
        or ".." in Path(f).parts
        or Path(f).parts[:1] != (TESTS_DIR,)
        or not (wt / f).is_file()
    ]
    if bad:
        return stopped(
            f"tests: tests.json lists files that are not existing files under "
            f"tests/: {bad}\nlisted: {files}"
        )
    changed = changed_files(wt)
    unchanged = [f for f in files if f not in changed]
    if unchanged:  # tests already committed on the card branch
        _, on_branch = sh(["git", "diff", "--name-only", f"{board.branch}...HEAD"], wt)
        unchanged = [f for f in unchanged if f not in on_branch.split()]
    # after resolve's `rerun analyze` (rethink) the previous tests may be
    # kept as they are (already committed)
    rethink = bool(card.pump2.get("rethink"))
    if unchanged and not rethink:
        return stopped(
            f"tests: listed files are not new or changed vs the card branch "
            f"HEAD: {unchanged}\nchanged: {changed}"
        )
    extra = [f for f in changed if f not in files]
    if extra:
        return stopped(
            f"tests: files changed that tests.json does not list: {extra}\n"
            f"listed: {files}"
        )
    missing = _missing_names(card, wt, files)
    if missing:  # one rerun: the prompt shows this error
        return _rerun(card, wt, missing)
    fresh = [f for f in files if f in changed]
    if fresh:
        sha, err = commit(wt, f"{card.id}: tests", fresh)
        if err:
            return stopped(f"tests: {err}")
    else:  # kept every test as committed (a rethink or a restart)
        _, sha = sh(["git", "rev-parse", "HEAD"], wt)
        sha = sha.strip() + " (kept, unchanged)"
    listing = "\n".join(f"- {f}" for f in files)
    (wt / ".pump" / RED_FILE).unlink(missing_ok=True)
    return Outcome(
        "build",
        f"tests: {len(files)} test file(s) committed {sha}\n{listing}",
        {"tests_red": None, "rethink": None},
    )
