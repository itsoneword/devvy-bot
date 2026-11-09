"""dv-d5aa: per-card criteria (Cn), frozen sha, gated review returns, one
return budget, job ceiling, closing summary (docs/pump-v2.md)."""

import json
import re
from pathlib import Path

import pytest

from src.pump2 import runner
from src.pump2.merge import ensure_worktree, merge
from src.pump2.stages import analyze, review
from src.pump2.stages import tests as tests_stage
from src.pump2.state import Board, find_card, set_pump2

from .test_pump2 import TASKS, Fake, board, branch_with
from .test_pump2 import repo as _repo

repo = _repo
CARD = TASKS / "dv-0001.md"
WT = ".pump-worktrees/dv-0001"

C_OK = (
    "C1 [BLOCK] it prints hello | because: owner asked | check: test\n"
    "C2 [ADVICE] docs mention it | because: nice to have | check: owner"
)


def mkplan(
    criteria: str = C_OK,
    touches: str = "code",
    size: str = "M",
    kind: str = "bug",
    assumptions: str = "none",
    jobs: str = "",
    questions: str = "",
    notes: str = "n",
) -> str:
    return (
        f"## Goal\nDo the thing.\n## Acceptance criteria\n{criteria}\n"
        f"## Touches\n{touches}\n## Size\n{size}\n## Kind\n{kind}\n"
        f"## Assumptions\n{assumptions}\n## Jobs\n{jobs}\n"
        f"## Questions\n{questions}\n## Notes\n{notes}\n"
    )


def ctx(repo: Path, tmp: Path, stage: str = "analyze") -> tuple[Board, Fake, Path]:
    """Board, fake bgjob, worktree (made by a launching tick)."""
    b, f = board(repo, "true"), Fake()
    set_pump2(repo / CARD, stage=stage)
    f.running = True
    f.tick(b, tmp)
    return b, f, repo / WT


def card(b: Board):  # type: ignore[no-untyped-def]
    return find_card(b, "dv-0001")


def write_plan(wt: Path, text: str) -> None:
    (wt / ".pump/plan.md").write_text(text)


def fin(repo: Path, tmp: Path, text: str):  # type: ignore[no-untyped-def]
    b, _, wt = ctx(repo, tmp)
    write_plan(wt, text)
    return analyze.finish(b, card(b), wt)


# ---- criterion 2: parse ----------------------------------------------------
def test_parse_accepts_new_format() -> None:
    p = analyze.parse(mkplan())
    assert p["Size"] == "M" and p["Kind"] == "bug"


BAD = {
    "no size": mkplan().replace("## Size\nM\n", ""),
    "no kind": mkplan().replace("## Kind\nbug\n", ""),
    "size X": mkplan(size="XL"),
    "kind": mkplan(kind="chore"),
    "line shape": mkplan(criteria="C1 [BLOCK] x | check: test"),
    "empty because": mkplan(criteria="C1 [BLOCK] x | because:  | check: test"),
    "bad check": mkplan(criteria="C1 [BLOCK] x | because: y | check: vibes"),
    "bad level": mkplan(criteria="C1 [MUST] x | because: y | check: test"),
    "dup id": mkplan(
        criteria="C1 [BLOCK] x | because: y | check: test\n"
        "C1 [ADVICE] z | because: y | check: owner"
    ),
    "S blocks": mkplan(
        size="S",
        criteria="\n".join(
            f"C{i} [BLOCK] x | because: y | check: test" for i in "1234"
        ),
    ),
    "S total": mkplan(
        size="S",
        criteria="C1 [BLOCK] x | because: y | check: test\n"
        + "\n".join(f"C{i} [ADVICE] x | because: y | check: owner" for i in "234"),
    ),
    "M blocks": mkplan(
        criteria="\n".join(
            f"C{i} [BLOCK] x | because: y | check: test" for i in "123456"
        )
    ),
    "L total": mkplan(
        size="L",
        criteria="C1 [BLOCK] x | because: y | check: test\n"
        + "\n".join(
            f"C{i} [ADVICE] x | because: y | check: model" for i in range(2, 17)
        ),
    ),
    "owner x4": mkplan(
        size="L",
        criteria="\n".join(
            f"C{i} [ADVICE] x | because: y | check: owner" for i in "1234"
        ),
    ),
    "test w/o code": mkplan(touches="docs"),
    "q w/o default": mkplan(questions="Q: which? | matters: m | found: f"),
    "q empty default": mkplan(
        questions="Q: which? | default:  | matters: m | found: f"
    ),
    "jobs empty feature": mkplan(kind="feature", jobs=""),
    "jobs empty new screen": mkplan(kind="new screen", jobs=""),
    "jobs empty ui": mkplan(touches="code\nui", size="L", jobs=""),
}


@pytest.mark.parametrize("name", sorted(BAD))
def test_parse_rejects(name: str) -> None:
    with pytest.raises(analyze.PlanError):
        analyze.parse(BAD[name])


def test_parse_error_messages_name_the_problem() -> None:
    with pytest.raises(analyze.PlanError, match="Size"):
        analyze.parse(BAD["size X"])
    with pytest.raises(analyze.PlanError, match="Kind"):
        analyze.parse(BAD["kind"])
    with pytest.raises(analyze.PlanError, match="C1"):
        analyze.parse(BAD["dup id"])
    with pytest.raises(analyze.PlanError, match="(?i)default"):
        analyze.parse(BAD["q w/o default"])
    with pytest.raises(analyze.PlanError, match="Jobs"):
        analyze.parse(BAD["jobs empty feature"])
    with pytest.raises(analyze.PlanError, match="(?i)owner"):
        analyze.parse(BAD["owner x4"])


def test_parse_limits_at_the_edge_pass() -> None:
    s3 = "\n".join(f"C{i} [BLOCK] x | because: y | check: test" for i in "123")
    analyze.parse(mkplan(size="S", criteria=s3))
    m = "\n".join(f"C{i} [BLOCK] x | because: y | check: test" for i in "12345")
    m += "\n" + "\n".join(f"C{i} [ADVICE] x | because: y | check: model" for i in "678")
    analyze.parse(mkplan(size="M", criteria=m))
    analyze.parse(
        mkplan(
            kind="feature",
            jobs="1. A parent plans a trip",
            questions="Q: which? | default: a | matters: m | found: f",
        )
    )


# ---- criterion 3: size raised by code --------------------------------------
@pytest.mark.parametrize(
    "touches,size,want",
    [
        ("code\nui\ndb", "S", "L"),
        ("code\nui", "S", "M"),
        ("code\ndb", "S", "M"),
        ("code\ndb", "L", "L"),
        ("code", "S", "S"),
    ],
)
def test_size_is_raised_never_lowered(
    repo: Path, tmp_path: Path, touches: str, size: str, want: str
) -> None:
    crit = "C1 [BLOCK] x | because: y | check: test"
    out = fin(
        repo,
        tmp_path,
        mkplan(
            criteria=crit,
            touches=touches,
            size=size,
            kind="feature" if "ui" in touches else "bug",
            jobs="1. a user does a thing" if "ui" in touches else "",
        ),
    )
    assert out.next == "tests", out.comment
    assert out.state["size"] == want and out.state["kind"]
    assert f"Size: {want}" in out.comment


# ---- criterion 4: extra questions move to Assumptions ----------------------
def test_extra_questions_move_to_assumptions(repo: Path, tmp_path: Path) -> None:
    qs = "\n".join(f"Q: q{i}? | default: d{i} | matters: m | found: f" for i in "123")
    b, _, wt = ctx(repo, tmp_path)
    write_plan(wt, mkplan(size="S", questions=qs))
    out = analyze.finish(b, card(b), wt)
    assert out.next == "waiting_user"
    assert "q1?" in out.comment and "q3?" not in out.comment
    assert "2" in out.comment and "moved" in out.comment
    p = analyze.parse((wt / ".pump/plan.md").read_text())
    assert "q2?" in p["Assumptions"] and "q3?" in p["Assumptions"]
    assert "q2?" not in p["Questions"] and "q1?" in p["Questions"]


# ---- criterion 5: format retry ---------------------------------------------
def test_format_error_retries_once_then_stops(repo: Path, tmp_path: Path) -> None:
    b, f, wt = ctx(repo, tmp_path)
    bad = mkplan(size="XL")
    write_plan(wt, bad)
    out = analyze.finish(b, card(b), wt)
    assert out.next == "analyze" and out.state["format_retry"] == 1
    # through the runner: the rerun prompt carries the error and the bad plan
    f.running = False
    f.tick(b, tmp_path)  # reap -> analyze again
    assert card(b).pump2["stage"] == "analyze"
    assert card(b).pump2["format_retry"] == 1
    f.tick(b, tmp_path)  # relaunch
    p = f.launched[-1][3]
    assert "XL" in p and "Size" in p
    assert "SECOND pass" not in p
    assert (wt / ".pump/plan.md").exists() or "Size: XL" in p
    assert not (wt / ".pump/plan.prev.md").exists() or "SECOND pass" not in p
    # a second format error stops
    f.running = False
    write_plan(wt, bad)
    f.tick(b, tmp_path)
    assert card(b).pump2["stage"] == "stopped"


# ---- criterion 6: analyze prompt -------------------------------------------
def test_analyze_prompt_names_format_and_jtbd(repo: Path, tmp_path: Path) -> None:
    b = board(repo, "true")
    p = analyze.prompt(b, card(b), tmp_path)
    for s in (
        "[BLOCK|ADVICE]",
        "because:",
        "check:",
        "Size",
        "Kind",
        "Assumptions",
        "Jobs",
        "default:",
        "matters:",
        "found:",
        "JTBD",
    ):
        assert s in p, s
    assert re.search(r"(?i)volume", p) and re.search(r"(?i)never ask|not ask", p)
    base = 1494 + (len(str(tmp_path)) - 128)  # template size before dv-d5aa
    assert len(p) <= base + 2048


# ---- criterion 7: criteria_sha ---------------------------------------------
def test_criteria_sha_written_and_checked_by_review(repo: Path, tmp_path: Path) -> None:
    b, _, wt = ctx(repo, tmp_path)
    write_plan(wt, mkplan())
    out = analyze.finish(b, card(b), wt)
    sha = out.state["criteria_sha"]
    assert re.fullmatch(r"[0-9a-f]{64}", sha)
    set_pump2(repo / CARD, stage="review", criteria_sha=sha)
    rv = wt / ".pump/review.md"
    rv.write_text("## Verdict\npass\n## Remarks\n")
    # notes may change, criteria may not
    write_plan(wt, mkplan(notes="changed notes"))
    assert review.finish(b, card(b), wt).next == "merge"
    write_plan(wt, mkplan(criteria=C_OK.replace("hello", "bye")))
    res = review.finish(b, card(b), wt)
    assert res.next == "stopped" and "criteria changed after analyze" in res.comment


def test_second_pass_rewrites_sha(repo: Path, tmp_path: Path) -> None:
    q = "Q: which? | default: a | matters: m | found: f"
    b, _, wt = ctx(repo, tmp_path)
    write_plan(wt, mkplan(questions=q))
    first = analyze.finish(b, card(b), wt)
    assert first.next == "waiting_user" and first.state["criteria_sha"]
    write_plan(wt, mkplan(criteria=C_OK.replace("hello", "bye")))
    second = analyze.finish(b, card(b), wt)
    assert second.state["criteria_sha"] != first.state["criteria_sha"]


# ---- criteria 8, 9: remark classification ----------------------------------
def frozen_review(repo: Path, tmp: Path, remarks: str, plan: str | None = None, **st):  # type: ignore[no-untyped-def]
    b, _, wt = ctx(repo, tmp)
    write_plan(wt, plan or mkplan())
    out = analyze.finish(b, card(b), wt)
    set_pump2(repo / CARD, stage="review", criteria_sha=out.state["criteria_sha"], **st)
    (wt / ".pump/review.md").write_text(f"## Verdict\nreturn\n## Remarks\n{remarks}\n")
    return review.finish(b, card(b), wt)


def test_blocking_remark_returns(repo: Path, tmp_path: Path) -> None:
    r = frozen_review(repo, tmp_path, "1. [C1] prints nothing | evidence: a.py:3")
    assert r.next == "build" and r.state["returns"] == 1


@pytest.mark.parametrize(
    "remark",
    [
        "1. nits about naming in a.py",
        "1. [C2] ADVICE criterion | evidence: a.py:3",
        "1. [C9] unknown id | evidence: a.py:3",
        "1. [C1] no evidence",
        "1. [C1] empty evidence | evidence:",
        "1. [D2] class without evidence | evidence: ",
    ],
)
def test_advice_remark_becomes_pass(repo: Path, tmp_path: Path, remark: str) -> None:
    r = frozen_review(repo, tmp_path, remark)
    assert r.next == "merge" and "advice (не блокирует)" in r.comment
    assert "returns" not in r.state or not r.state["returns"]


@pytest.mark.parametrize("d", ["D1", "D2", "D3", "D4"])
def test_defect_class_with_evidence_blocks(repo: Path, tmp_path: Path, d: str) -> None:
    r = frozen_review(repo, tmp_path, f"1. [{d}] broke it | evidence: pytest -k x")
    assert r.next == "build"


# (source card, remark no., id, text); the original wording is in the gymbro
# cards' review-return comments (report §3.4). 14 BLOCK Cn, 3 defects, 2 no id.
FIX = [
    ("gb-1a38", 1, "C1", "AC6 explain script missing"),
    ("gb-1a38", 2, "D3", "translate() sends bind params so the index is not used"),
    ("gb-1a38", 3, "C2", "before/after numbers not posted"),
    ("gb-1a38", 4, None, "rename a helper"),
    ("gb-1ac9", 1, "C1", "empty state not shown"),
    ("gb-1ac9", 2, "C2", "date picker missing"),
    ("gb-1ac9", 3, "C3", "count wrong"),
    ("gb-1ac9", 4, None, "comment wording"),
    ("gb-1ae0", 1, "C1", "route missing"),
    ("gb-1ae0", 2, "C2", "no migration"),
    ("gb-1ae0", 3, "C4", "no test for edge"),
    ("gb-1aeb", 1, "D4", "file changed outside the plan"),
    ("gb-1aeb", 2, "C3", "button not wired"),
    ("gb-1aeb", 3, "C5", "label not localised"),
    ("gb-1aee", 1, "C1", "limit not enforced"),
    ("gb-1aee", 2, "D3", "regression: list order changed"),
    ("gb-1aee", 3, "C2", "doc not updated"),
    ("gb-1af2", 1, "C4", "query not paginated"),
    ("gb-1af2", 2, "C5", "error not shown"),
]
FIX_PLAN = mkplan(
    size="L",
    touches="code\ndb",
    criteria="\n".join(
        f"C{i} [BLOCK] crit {i} | because: why | check: test" for i in range(1, 6)
    ),
)


def fix_lines(subset: list[int]) -> str:
    out = []
    for n, i in enumerate(subset, 1):
        src, no, cid, text = FIX[i]
        tag = f"[{cid}] " if cid else ""
        out.append(f"{n}. {tag}R{i:02d} {text} | evidence: f.py:{i + 1}")
    return "\n".join(out)


def test_report_fixture_17_blocking_2_advice(repo: Path, tmp_path: Path) -> None:
    assert len(FIX) == 19
    r = frozen_review(repo, tmp_path, fix_lines(list(range(19))), FIX_PLAN)
    assert r.next == "build"
    head, _, tail = r.comment.partition("advice (не блокирует)")
    assert tail
    advice = {i for i, x in enumerate(FIX) if x[2] is None}
    assert len(advice) == 2
    for i in range(19):
        tok = f"R{i:02d}"
        assert (tok in tail) == (i in advice), tok
        assert (tok in head) == (i not in advice), tok
    only_advice = frozen_review(repo, tmp_path, fix_lines(sorted(advice)), FIX_PLAN)
    assert only_advice.next == "merge"


# ---- criterion 10: tests coverage of Cn ------------------------------------
PLAN_T = mkplan(
    criteria=(
        "C1 [BLOCK] a | because: y | check: test\n"
        "C2 [BLOCK] b | because: y | check: test\n"
        "C3 [ADVICE] c | because: y | check: test\n"
        "C4 [BLOCK] d | because: y | check: script"
    )
)


def mk_tests_ctx(repo: Path, tmp: Path, files: dict[str, str], plan: str = PLAN_T):  # type: ignore[no-untyped-def]
    b, f, wt = ctx(repo, tmp)
    write_plan(wt, plan)
    sha = analyze.finish(b, card(b), wt).state["criteria_sha"]
    set_pump2(repo / CARD, stage="tests", criteria_sha=sha)
    for rel, text in files.items():
        (wt / rel).parent.mkdir(parents=True, exist_ok=True)
        (wt / rel).write_text(text)
    (wt / ".pump/tests.json").write_text(json.dumps({"files": list(files)}))
    return b, wt


def test_tests_cover_every_block_test_criterion(repo: Path, tmp_path: Path) -> None:
    b, wt = mk_tests_ctx(
        repo,
        tmp_path,
        {"tests/unit/test_x.py": "def test_C1_prints():\n    assert 0\n"},
    )
    out = tests_stage.finish(b, card(b), wt)
    # first time: one rerun (dv-80b8 C6); the comment names the missing ids
    assert out.next == "tests" and "C2" in out.comment
    assert "C1" not in out.comment.replace("test_C1", "")
    assert "C3" not in out.comment and "C4" not in out.comment


def test_tests_cover_ok_python_and_mjs(repo: Path, tmp_path: Path) -> None:
    b, wt = mk_tests_ctx(
        repo,
        tmp_path,
        {
            "tests/unit/test_x.py": "def test_C1_prints():\n    assert 0\n",
            "tests/x.test.mjs": 'test("test_C2_other", () => {});\n',
        },
    )
    assert tests_stage.finish(b, card(b), wt).next == "build"


def test_tests_prompt_asks_for_naming(repo: Path, tmp_path: Path) -> None:
    b, _, wt = ctx(repo, tmp_path)
    write_plan(wt, PLAN_T)
    assert "test_C" in tests_stage.prompt(b, card(b), wt)


# ---- criterion 11: one return budget ---------------------------------------
def test_second_return_stops_with_ids(repo: Path, tmp_path: Path) -> None:
    r = frozen_review(
        repo, tmp_path, "1. [C1] still broken | evidence: a.py:3", returns=1
    )
    assert r.next == "stopped" and "C1" in r.comment


def test_budget_is_shared_with_d_class(repo: Path, tmp_path: Path) -> None:
    r = frozen_review(
        repo, tmp_path, "1. [D3] regression | evidence: pytest -k y", returns=1
    )
    assert r.next == "stopped" and "D3" in r.comment


# ---- criterion 12: job ceiling ---------------------------------------------
def test_job_ceiling_of_12(repo: Path, tmp_path: Path) -> None:
    b, f = board(repo, "true"), Fake()
    f.running = True
    for _ in range(12):
        set_pump2(repo / CARD, stage="analyze", job=None)
        f.tick(b, tmp_path)
    assert len(f.launched) == 12
    set_pump2(repo / CARD, stage="analyze", job=None)
    f.tick(b, tmp_path)
    assert len(f.launched) == 12
    c = card(b)
    assert c.pump2["stage"] == "stopped" and "job ceiling 12 reached" in c.body


# ---- criterion 13: summary comment -----------------------------------------
HEADS = ("проверено", "не проверено", "советы", "посмотри сам")


def test_summary_on_return_budget_stop(repo: Path, tmp_path: Path) -> None:
    b, f, wt = ctx(repo, tmp_path)
    write_plan(wt, mkplan())
    sha = analyze.finish(b, card(b), wt).state["criteria_sha"]
    set_pump2(repo / CARD, stage="review", job=None, criteria_sha=sha, returns=1)
    f.running = False
    f.tick(b, tmp_path)  # launches review
    f.running = False
    (wt / ".pump/review.md").write_text(
        "## Verdict\nreturn\n## Remarks\n1. [C1] bad | evidence: a.py:1\n"
    )
    f.tick(b, tmp_path)  # reaps
    c = card(b)
    assert c.pump2["stage"] == "stopped"
    for h in HEADS:
        assert h in c.body, h


def test_summary_when_merged(repo: Path, tmp_path: Path) -> None:
    branch_with(repo, "pump2/x", "b.txt", "b\n")
    b = board(repo, "test -f b.txt")
    set_pump2(repo / CARD, stage="merge", branch="pump2/x")
    wt = ensure_worktree(b, "dv-0001", "pump2/x")
    (wt / ".pump").mkdir(exist_ok=True)
    write_plan(wt, mkplan())
    assert merge(b, card(b), locks=tmp_path / "l").outcome == "merged"
    body = card(b).body
    for h in HEADS:
        assert h in body, h
    assert "C1" in body


# ---- criterion 14: legacy plans --------------------------------------------
OLD_PLAN = (
    "## Goal\nx\n## Acceptance criteria\n1. it prints\n## Touches\ncode\n"
    "## Questions\n## Notes\nn\n"
)


def test_legacy_review_unchanged(repo: Path, tmp_path: Path) -> None:
    b, _, wt = ctx(repo, tmp_path, "review")
    write_plan(wt, OLD_PLAN)
    set_pump2(repo / CARD, stage="review")  # no criteria_sha
    (wt / ".pump/review.md").write_text(
        "## Verdict\nreturn\n## Remarks\n1. fix a.py somehow\n"
    )
    r = review.finish(b, card(b), wt)
    assert r.next == "build" and r.state["returns"] == 1
    set_pump2(repo / CARD, returns=1)
    assert review.finish(b, card(b), wt).next == "stopped"


def test_legacy_tests_unchanged(repo: Path, tmp_path: Path) -> None:
    b, f, wt = ctx(repo, tmp_path)
    write_plan(wt, OLD_PLAN)
    set_pump2(repo / CARD, stage="tests")
    (wt / "tests").mkdir(exist_ok=True)
    (wt / "tests/test_y.py").write_text("def test_y():\n    assert 0\n")
    (wt / ".pump/tests.json").write_text(json.dumps({"files": ["tests/test_y.py"]}))
    assert tests_stage.finish(b, card(b), wt).next == "build"


def test_runner_exports_stage_modules() -> None:
    assert hasattr(runner, "start_stage")
