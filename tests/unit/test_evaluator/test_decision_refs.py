"""dv-32fe phase 2: a repo agent's per-task decision is a pointer to the repo
when the repo holds it, and a flagged full fact when it does not.

Pinned interface (the build must provide it):
- ``src.evaluator.decision_refs.DecisionRefChecker(agent_repos)`` with
  ``agent_repos: dict[str, tuple[Path, ...]]`` (profile slug -> repos) and
  ``verify(agent_id, ref, content) -> bool`` (never raises).
- ``MemoryWriter(repo, decision_refs=checker)``.
- ``source_ref="missing:<ref>"`` marks a user_fact whose decision is not in the
  repo; ``render_fact_line`` / ``render_pointer_line`` show it as a flag.
- ``build_evaluator_prompt(..., agent_id=..., repo_agents=frozenset)``.
"""

from pathlib import Path

import pytest

from src.evaluator.decision_refs import DecisionRefChecker
from src.evaluator.prompts import build_evaluator_prompt
from src.evaluator.writer import MemoryWriter
from src.storage.database import DatabaseManager
from src.storage.memory_repository import (
    MemoryRepository,
    render_fact_line,
    render_pointer_line,
)

DECISION = (
    "gb-1ab3: the feed header shows the city name only, no country, because "
    "the owner found the long form cluttered on small screens."
)
OTHER = "gb-9f2c: quiet mode mutes push notifications between 22:00 and 08:00."
REF = "docs/DECISIONS.md"


@pytest.fixture
async def repo(tmp_path):
    db = DatabaseManager(str(tmp_path / "mem.db"))
    await db.initialize()
    yield MemoryRepository(db)
    await db.close()


@pytest.fixture
def gymbro_dir(tmp_path: Path) -> Path:
    root = tmp_path / "gymbro"
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "DECISIONS.md").write_text(
        "# Decisions\n\n## 2026-10-20 gb-1ab3 feed header\nCity only.\n"
    )
    return root


@pytest.fixture
def checker(gymbro_dir: Path) -> DecisionRefChecker:
    return DecisionRefChecker({"gymbro": (gymbro_dir,)})


def _op(content: str, ref=REF, kind="conversation_result", tier="general"):
    return {
        "op": "add",
        "kind": kind,
        "tier": tier,
        "content": content,
        "label": None,
        "source_ref": ref,
        "supersedes_id": None,
        "scope": "private",
    }


# --- checker -----------------------------------------------------------


def test_checker_verifies_id_present_in_decisions(checker):
    assert checker.verify("gymbro", REF, DECISION) is True


def test_checker_rejects_id_absent_and_content_without_id(checker):
    assert checker.verify("gymbro", REF, OTHER) is False
    assert checker.verify("gymbro", REF, "the owner prefers short answers") is False


def test_checker_rejects_escaping_and_missing_paths_without_raising(checker):
    assert checker.verify("gymbro", "../../etc/passwd", DECISION) is False
    assert checker.verify("gymbro", "/etc/passwd", DECISION) is False
    assert checker.verify("gymbro", "docs/NOPE/DECISIONS.md", DECISION) is False


def test_checker_card_ref_needs_the_card_file(checker, gymbro_dir):
    (gymbro_dir / ".deviz" / "tasks").mkdir(parents=True)
    assert checker.verify("gymbro", "gb-77aa", "x") is False
    (gymbro_dir / ".deviz" / "tasks" / "gb-77aa.md").write_text("---\nid: gb-77aa\n")
    assert checker.verify("gymbro", "gb-77aa", "x") is True


def test_checker_scope_with_user_suffix_uses_base_slug(checker):
    assert checker.verify("gymbro#u1", REF, DECISION) is True


def test_checker_unknown_agent_is_unverified(checker):
    assert checker.verify("chef", REF, DECISION) is False


# --- writer ------------------------------------------------------------


async def test_verified_decision_becomes_pointer_and_keeps_full_text(repo, checker):
    writer = MemoryWriter(repo, decision_refs=checker)
    res = await writer.apply_ops("gymbro", [_op(DECISION)])
    assert res.added == 1

    facts = await repo.get_facts("gymbro")
    assert len(facts) == 1
    assert facts[0].kind == "conversation_result"
    assert facts[0].source_ref == REF
    assert facts[0].content == DECISION  # full text, not a one-liner

    snap = await repo.facts_snapshot("gymbro")
    assert snap.general == []
    line = render_pointer_line(snap.pointers[0])
    assert f"→ {REF}" in line
    assert "⚑" not in line

    found = await repo.search("gymbro", "city name")
    assert any("no country" in f.content for f in found)  # recall: full text


async def test_decision_absent_from_repo_stays_full_fact_and_is_flagged(repo, checker):
    writer = MemoryWriter(repo, decision_refs=checker)
    res = await writer.apply_ops("gymbro", [_op(OTHER)])
    assert res.added == 1

    facts = await repo.get_facts("gymbro")
    assert len(facts) == 1
    assert facts[0].kind == "user_fact"
    assert facts[0].source_ref == f"missing:{REF}"
    assert facts[0].content == OTHER

    snap = await repo.facts_snapshot("gymbro")
    assert [f.id for f in snap.general] == [facts[0].id]
    line = render_fact_line(snap.general[0])
    assert OTHER in line
    assert "⚑" in line and "not yet in" in line and REF in line


async def test_owner_preference_stays_a_plain_fact(repo, checker):
    writer = MemoryWriter(repo, decision_refs=checker)
    pref = "The owner wants answers short, in Russian, without emoji."
    await writer.apply_ops("gymbro", [_op(pref, ref=None, kind="user_fact")])

    (fact,) = await repo.get_facts("gymbro")
    assert fact.kind == "user_fact"
    assert fact.source_ref is None
    assert "⚑" not in render_fact_line(fact)


async def test_non_repo_agent_with_decision_ref_is_not_checked(repo, checker):
    writer = MemoryWriter(repo, decision_refs=checker)
    await writer.apply_ops("chef", [_op(OTHER, ref=REF)])
    (fact,) = await repo.get_facts("chef")
    assert not (fact.source_ref or "").startswith("missing:")


async def test_non_decision_source_ref_is_left_alone(repo, checker):
    writer = MemoryWriter(repo, decision_refs=checker)
    await writer.apply_ops(
        "gymbro",
        [_op("Findings of the retention study.", ref="docs/research/ret.md")],
    )
    (fact,) = await repo.get_facts("gymbro")
    assert fact.kind == "conversation_result"
    assert fact.source_ref == "docs/research/ret.md"


async def test_writer_without_checker_behaves_as_today(repo):
    writer = MemoryWriter(repo)
    await writer.apply_ops("gymbro", [_op(OTHER)])
    (fact,) = await repo.get_facts("gymbro")
    assert fact.kind == "conversation_result"
    assert fact.source_ref == REF


# --- evaluator prompt --------------------------------------------------

_REPO_AGENTS = frozenset({"gymbro", "pl", "devops"})


def test_repo_agent_prompt_gains_decision_block():
    base = build_evaluator_prompt("User: hi", "(none)")
    prompt = build_evaluator_prompt(
        "User: hi", "(none)", agent_id="gymbro", repo_agents=_REPO_AGENTS
    )
    assert prompt != base
    assert "DECISIONS.md" in prompt
    assert prompt.startswith(base[:200])


@pytest.mark.parametrize("agent", ["chef", "coach#u100", "general#u200"])
def test_non_repo_agent_prompt_is_byte_identical(agent):
    base = build_evaluator_prompt("User: hi", "(none)")
    same = build_evaluator_prompt(
        "User: hi", "(none)", agent_id=agent, repo_agents=_REPO_AGENTS
    )
    assert same == base
