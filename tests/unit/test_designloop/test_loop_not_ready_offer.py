"""dv-2d00: a loop that ends on rounds without a ready variant still offers
the critiqued variants, best score first, with the open remarks."""

from pathlib import Path

from src.designloop.critic import CRITERIA, Critique
from tests.unit.test_designloop.test_loop import (  # noqa: F401
    BRANCHES,
    Steps,
    brief,
    ready,
    run,
)

SCORES = {BRANCHES[0]: 6, BRANCHES[1]: 7, BRANCHES[2]: 8}


def scored(n: int, branch: str) -> Critique:
    crit = {k: "ok" for k in CRITERIA}
    crit["hierarchy"] = f"fix: {branch} round {n} empty state needs a Browse action"
    return Critique(verdict="revise", criteria=crit, score=SCORES[branch])


async def test_C1_rounds_offer_all_variants_sorted_by_score(brief, tmp_path):
    steps = Steps(tmp_path, verdicts=scored)

    outcome = await run(brief, steps)

    assert outcome.stop == "rounds"
    assert [v.branch for v in outcome.offered] == [
        BRANCHES[2],
        BRANCHES[1],
        BRANCHES[0],
    ]
    assert len(steps.collages) == 1
    assert [v.branch for v in steps.collages[0]] == [v.branch for v in outcome.offered]
    assert outcome.collage is not None
    assert len(steps.delivered) == 1
    assert steps.delivered[0][1] == [Path(outcome.collage)]


async def test_C1_repeated_stop_also_offers(brief, tmp_path):
    def same(n: int, branch: str) -> Critique:
        return scored(0, branch)

    steps = Steps(tmp_path, verdicts=same)
    outcome = await run(brief, steps)
    assert outcome.stop == "repeated"
    assert len(outcome.offered) == 3
    assert steps.delivered[0][1]


async def test_C2_offer_text_marks_recommended_and_lists_remarks(brief, tmp_path):
    steps = Steps(tmp_path, verdicts=scored)

    outcome = await run(brief, steps)

    text = steps.delivered[0][0]
    assert "design failed" not in text
    assert "critic: ready" not in text
    assert "recommended" in text.lower()
    assert "3 " in text or "3 round" in text
    lines = text.splitlines()
    first = next(i for i, l in enumerate(lines) if l.startswith("1."))
    assert "8/10" in lines[first]
    assert "Browse action" in "\n".join(lines[first : first + 3])
    assert "7/10" in text and "6/10" in text
    assert text.index("8/10") < text.index("7/10") < text.index("6/10")
    assert outcome.message == text
