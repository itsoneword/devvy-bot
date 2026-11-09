"""dv-b1ae: pump2 owns .deviz/tasks/**. Its own card-file writes in the live
checkout are not someone else's dirty work, and a card branch never fights the
integration branch over card files (the integration branch's version wins)."""

from __future__ import annotations

from pathlib import Path

from src.pump2.merge import dirty_overlap

from .test_pump2_mergefix import (  # noqa: F401
    CID,
    TASKS,
    Rig,
    card_branch,
    commit_file,
    git,
    repo,
    resolver,
    writes,
)

OTHER = ".deviz/tasks/dv-0002.md"
BASE = "---\nid: dv-0002\nstatus: todo\n---\n\nbody\n"


def with_other_card(repo: Path) -> None:
    commit_file(repo, OTHER, BASE, "other card")


def branch_edits_card_and_code(repo: Path) -> None:
    git(repo, "checkout", "-q", "-b", "pump2/x")
    (repo / OTHER).write_text(BASE + "branch edit\n")
    (repo / "b.txt").write_text("b\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "card")
    git(repo, "checkout", "-q", "main")


def test_dirty_card_file_does_not_block_ff(repo: Path, tmp_path: Path) -> None:
    with_other_card(repo)
    branch_edits_card_and_code(repo)
    live = BASE + "pump2 live write\n"
    (repo / OTHER).write_text(live)

    def agent(wt: Path, prompt: str) -> None:
        raise AssertionError("mergefix must not run")

    rig = Rig(repo, tmp_path, agent)
    assert rig.run() == "done"
    assert rig.mergefix_prompts == [] and rig.runs == 1
    assert (repo / OTHER).read_bytes() == live.encode()
    assert (repo / "b.txt").exists()


def test_dirty_overlap_ignores_card_files(repo: Path, tmp_path: Path) -> None:
    with_other_card(repo)
    branch_edits_card_and_code(repo)
    (repo / OTHER).write_text(BASE + "live\n")
    (repo / TASKS / "dv-0077.md").write_text("untracked\n")
    rig = Rig(repo, tmp_path, writes("land"))
    assert dirty_overlap(rig.board, "pump2/x") == []


def card_conflict(repo: Path, extra: bool) -> None:
    with_other_card(repo)
    git(repo, "checkout", "-q", "-b", "pump2/x")
    (repo / OTHER).write_text(BASE + "branch side\n")
    (repo / "b.txt").write_text("b\n")
    if extra:
        (repo / "mod.py").write_text("x = 2\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "card")
    git(repo, "checkout", "-q", "main")
    (repo / OTHER).write_text(BASE + "main side\n")
    if extra:
        (repo / "mod.py").write_text("x = 3\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "main moves")


def test_card_file_conflict_resolves_to_integration_version(
    repo: Path, tmp_path: Path
) -> None:
    card_conflict(repo, extra=False)

    def agent(wt: Path, prompt: str) -> None:
        raise AssertionError("mergefix must not run")

    rig = Rig(repo, tmp_path, agent)
    assert rig.run() == "done"
    assert rig.mergefix_prompts == [] and rig.runs == 1
    assert (repo / OTHER).read_text() == BASE + "main side\n"
    assert (repo / "b.txt").exists()


def test_card_and_code_conflict_hands_over_only_code(
    repo: Path, tmp_path: Path
) -> None:
    card_conflict(repo, extra=True)
    rig = Rig(repo, tmp_path, resolver("mod.py", "retest", "x = 4\n"))
    assert rig.run() == "done"
    assert len(rig.mergefix_prompts) == 1
    assert "mod.py" in rig.facts[0] and "dv-0002.md" not in rig.facts[0]
    assert (repo / OTHER).read_text() == BASE + "main side\n"
    assert (repo / "mod.py").read_text() == "x = 4\n"


def test_branch_only_new_card_file_is_kept(repo: Path, tmp_path: Path) -> None:
    card_branch(repo, ".deviz/tasks/dv-0003.md", "follow-up\n")
    rig = Rig(repo, tmp_path, writes("land"))
    assert rig.run() == "done"
    assert (repo / ".deviz/tasks/dv-0003.md").read_text() == "follow-up\n"
