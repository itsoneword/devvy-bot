"""dv-84f3: stage prompts carry a digest, counts and byte-capped diffs."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from src.pump2.stages import review
from src.pump2.state import add_comment, add_owner_answer, find_card, read_card

from .test_pump2 import Fake, board, git
from .test_pump2 import repo as _repo  # noqa: F401
from .test_pump2_mergefix import Rig, card_branch, commit_file
from .test_pump2_mergefix import repo as _mrepo  # noqa: F401
from .test_pump2_mergefix import writes
from .test_pump2_step3 import put, setup

repo = _repo
mrepo = _mrepo


# ---- merge-facts -------------------------------------------------------------
def test_merge_facts_has_30_commits_count_and_no_stat(
    mrepo: Path, tmp_path: Path
) -> None:
    card_branch(mrepo, "fname.py", "card\n")
    for i in range(44):
        commit_file(mrepo, f"m{i}.txt", f"{i}\n", f"main change {i}")
    commit_file(mrepo, "fname.py", "main\n", "main conflicts")  # 45 in all
    rig = Rig(mrepo, tmp_path, writes("stop"))
    rig.run()
    facts = rig.facts[0]
    sec = facts.split("commits since", 1)[1].split("\n## ", 1)[0]
    commit_lines = [
        ln for ln in sec.splitlines() if re.match(r"^[0-9a-f]{7,40} \S", ln)
    ]
    assert len(commit_lines) == 30
    assert "45" in sec and "15 older" in sec
    assert " | " not in facts and "changed," not in facts
    assert "fname.py" in facts  # files / conflicts still listed


# ---- card digest -------------------------------------------------------------
def test_card_digest_keeps_owner_and_last_pump2(tmp_path: Path) -> None:
    from src.pump2.state import card_digest

    p = tmp_path / "dv-9.md"
    p.write_text(
        "---\nid: dv-9\ntitle: t\nstatus: in_progress\n---\n\n"
        "## Description\n\nHEADTEXT\n\n## Acceptance Criteria\n\nAC1\n\n"
        "## Notes\n\nNOTE1\n\n## Comments\n"
    )
    for i in range(20):
        add_comment(p, f"stage chatter number {i} " + "x" * 200)
        if i in (5, 12):
            add_owner_answer(p, f"OWNERANSWER{i}")
    d = card_digest(read_card(p))
    assert "id: dv-9" in d and "HEADTEXT" in d and "AC1" in d and "NOTE1" in d
    assert "OWNERANSWER5" in d and "OWNERANSWER12" in d
    assert "stage chatter number 19" in d
    assert "stage chatter number 3 " not in d
    assert str(p) in d
    assert len(d) < len(p.read_text()) // 2


def test_card_digest_keeps_human_comments(tmp_path: Path) -> None:
    from src.pump2.state import card_digest

    p = tmp_path / "dv-9.md"
    p.write_text("---\nid: dv-9\n---\n\n## Description\n\nd\n\n## Comments\n")
    add_comment(p, "machine one")
    p.write_text(p.read_text() + "\n### @devops — 2026-11-06T10:00:00Z\nHUMANNOTE\n")
    add_comment(p, "machine two")
    d = card_digest(read_card(p))
    assert "HUMANNOTE" in d and "machine one" not in d and "machine two" in d


def test_stage_prompts_do_not_inline_whole_card() -> None:
    out = subprocess.run(
        [
            "grep",
            "-n",
            "card.path.read_text()",
            *map(str, Path("src/pump2/stages").glob("*.py")),
        ],
        capture_output=True,
        text=True,
    ).stdout
    assert out == ""


def test_review_prompt_uses_digest(repo: Path, tmp_path: Path) -> None:
    b, f = board(repo, "true"), Fake()
    wt = setup(repo, tmp_path, "review", b, f)
    c = find_card(b, "dv-0001")
    for i in range(10):
        add_comment(c.path, f"OLDCHATTER{i}")
    add_comment(c.path, "LASTCHATTER")
    p = review.prompt(b, find_card(b, "dv-0001"), wt)
    assert "LASTCHATTER" in p and "OLDCHATTER3" not in p and str(c.path) in p


# ---- review byte cap ---------------------------------------------------------
def test_review_diff_byte_cap_constant() -> None:
    assert 30_000 <= review.DIFF_CAP_BYTES <= 100_000


def test_review_byte_cap_drops_docs_before_code(repo: Path, tmp_path: Path) -> None:
    b, f = board(repo, "true"), Fake()
    wt = setup(repo, tmp_path, "review", b, f)
    put(wt, "small.py", "small_marker = 1\n")
    put(wt, "docs/huge.md", "docline\n" * (review.DIFF_CAP_BYTES // 4))
    git(wt, "add", "-A")
    git(wt, "commit", "-qm", "c")
    p = review.prompt(b, find_card(b, "dv-0001"), wt)
    assert "+small_marker = 1" in p
    assert "docs/huge.md" in p and "+docline" not in p
    assert len(p.encode()) < review.DIFF_CAP_BYTES * 2
    # only a doc was left out: no changed code file unread
    assert review.capped_names(b, wt) in (None, [])


def test_review_byte_cap_over_by_code_lists_names(repo: Path, tmp_path: Path) -> None:
    b, f = board(repo, "true"), Fake()
    wt = setup(repo, tmp_path, "review", b, f)
    put(wt, "big.py", "y = 1\n" * (review.DIFF_CAP_BYTES // 3))
    git(wt, "add", "-A")
    git(wt, "commit", "-qm", "c")
    p = review.prompt(b, find_card(b, "dv-0001"), wt)
    assert "big.py" in p and "+y = 1" not in p
    assert "big.py" in (review.capped_names(b, wt) or [])
    assert len(p.encode()) < review.DIFF_CAP_BYTES * 2
