"""dv-2c5c review: a mergefix job that fails twice stops the card without
leaving merge.py's conflict merge open or the mergefix counter set."""

from __future__ import annotations

from pathlib import Path

from src.pump2 import runner
from src.pump2.state import find_card, worktree_path

from .test_pump2_mergefix import CID, Rig, conflict, repo, resolver  # noqa: F401


def test_mergefix_job_failing_twice_ends_the_merge(
    repo: Path, tmp_path: Path  # noqa: F811
) -> None:
    conflict(repo, "mod.py", "x = 2\n", "x = 3\n")
    rig = Rig(repo, tmp_path, resolver("mod.py", "retest", "x = 4\n"))
    for _ in range(8):
        if find_card(rig.board, CID).pump2["stage"] in ("done", "stopped"):
            break
        runner.tick(
            {"t": rig.board},
            launch=rig.launch_merge,
            launch_stage=rig.launch_stage,
            status=lambda j: (False, 1 if j.startswith("job-mf") else 0),
            output=lambda j, n: "boom",
            locks=tmp_path / "l",
        )
    card = find_card(rig.board, CID)
    assert card.pump2["stage"] == "stopped"
    assert len(rig.mergefix_prompts) == 2  # the job ran, failed, reran once
    assert "mergefix" not in card.pump2 and "stray" not in card.pump2
    wt = worktree_path(rig.board, CID)
    gitdir = Path(
        (wt / ".git").read_text().split(":", 1)[1].strip()
        if (wt / ".git").is_file()
        else wt / ".git"
    )
    assert not (gitdir / "MERGE_HEAD").exists()
