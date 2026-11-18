"""dv-b8bb after main is merged: stepback / dispute folded into resolve
(C6, C10, C14)."""

from __future__ import annotations

import re
from pathlib import Path

from src.pump2 import runner
from src.pump2.state import find_card, set_pump2
from tests.unit.test_pump2.test_pump2_resolve import repo  # noqa: F401  (fixture)
from tests.unit.test_pump2.test_pump2_resolve import (  # noqa: F401
    CID,
    SRC,
    TASKS,
    Rig,
    conflict,
    noop,
)

OLD_KEYS = (
    "mergefix",
    "mergefix_stops",
    "dispute",
    "disputes",
    "stepback_fixed",
    "build_wip",
    "build_red",
)


def _tick(rig: Rig, tmp_path: Path) -> None:
    runner.tick(
        {"t": rig.board},
        launch=rig.launch_merge,
        launch_stage=rig.launch_stage,
        status=lambda j: (False, 0),
        output=lambda j, n: "",
        locks=tmp_path / "l",
    )


def test_C6_no_special_case_names_left_in_pump2() -> None:
    pat = re.compile(r"mergefix|stepback|dispute|build_red", re.I)
    hits = [
        f"{p.relative_to(SRC)}:{i}"
        for p in SRC.rglob("*.py")
        for i, ln in enumerate(p.read_text().splitlines(), 1)
        if pat.search(ln)
    ]
    assert hits == []
    for gone in ("mergefix.py", "stepback.py"):
        assert not (SRC / "stages" / gone).exists()
    text = "\n".join(p.read_text() for p in SRC.rglob("*.py"))
    for name in ("MERGEFIX_MAX", "mergefix_stops", "_recheck_stop"):
        assert name not in text, name
    # word match: design.py's `timeout_seconds` is not the old `_second` helper
    assert not re.search(r"\b_second\b", text)
    tests = SRC.parents[1] / "tests" / "unit" / "test_pump2"
    for pat_ in ("test_pump2_dispute*", "test_pump2_stepback*", "test_pump2_mergefix*"):
        assert not list(tests.glob(pat_)), pat_


def test_C10_stepback_card_moves_to_resolve(
    repo: Path, tmp_path: Path  # noqa: F811
) -> None:  # noqa: F811
    rig = Rig(repo, tmp_path, noop("continue build"))
    set_pump2(rig.card.path, stage="stepback", stepback_fixed="1", build_red="x")
    _tick(rig, tmp_path)
    c = find_card(rig.board, CID)
    assert c.pump2["stage"] == "resolve"
    for k in OLD_KEYS:
        assert k not in c.pump2 or not c.pump2[k], k


def test_C10_dispute_open_card_moves_to_resolve(
    repo: Path, tmp_path: Path  # noqa: F811
) -> None:
    rig = Rig(repo, tmp_path, noop("continue build"))
    set_pump2(rig.card.path, stage="build", dispute="open", disputes="1", build_wip="x")
    _tick(rig, tmp_path)
    c = find_card(rig.board, CID)
    assert c.pump2["stage"] == "resolve"
    for k in OLD_KEYS:
        assert k not in c.pump2 or not c.pump2[k], k


def test_C10_resolve_accepted_as_restart_stage() -> None:
    from src.pump2 import state

    assert "resolve" in state.STAGES
    assert "resolve" in state.RUN_STAGES


def test_C14_rerun_analyze_sets_rethink_and_saves_leftovers(
    repo: Path, tmp_path: Path  # noqa: F811
) -> None:
    conflict(repo, "mod.py", "x = 2\n", "x = 3\n")

    def agent(wt: Path, prompt: str) -> None:
        (wt / "leftover.py").write_text("junk = 1\n")  # uncommitted build leftover
        (wt / ".pump" / "resolve.md").write_text(
            "## Verdict\nrerun analyze\n\n## Reasoning\nwrong plan\n"
            "\n## Note\nrethink the task\n"
        )

    rig = Rig(repo, tmp_path, agent)
    assert rig.run() == "analyze"
    assert rig.card.pump2.get("rethink")
    wt = repo / ".pump-worktrees" / CID
    saved = [p for p in (wt / ".pump").glob("*.patch")] if wt.exists() else []
    assert saved, "uncommitted leftovers must be saved to a patch under .pump/"
    assert "junk" in saved[0].read_text()
