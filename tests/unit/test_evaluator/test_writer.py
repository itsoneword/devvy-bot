"""Tests for MemoryWriter applying ops through MemoryRepository."""

import pytest

from src.evaluator.writer import MemoryWriter, is_duplicate, normalize_fact
from src.storage.database import DatabaseManager
from src.storage.memory_repository import MemoryRepository


@pytest.fixture
async def repo(tmp_path):
    db = DatabaseManager(str(tmp_path / "mem.db"))
    await db.initialize()
    yield MemoryRepository(db)
    await db.close()


async def test_apply_ops_adds_and_supersedes(repo):
    writer = MemoryWriter(repo)
    old_id = await repo.add_fact("chef", "general", "has no oven", source="manual")

    ops = [
        {
            "op": "add",
            "tier": "pinned",
            "content": "allergic to peanuts",
            "label": "allergies",
            "supersedes_id": None,
        },
        {
            "op": "supersede",
            "tier": "general",
            "content": "has an oven",
            "label": None,
            "supersedes_id": old_id,
        },
    ]
    count = await writer.apply_ops("chef", ops)

    assert count.applied == 2
    active = {f.content for f in await repo.get_facts("chef")}
    assert active == {"allergic to peanuts", "has an oven"}


async def test_revocation_maps_and_applies_shoulder_pair(repo):
    """dv-b9c7 step 2 end-to-end: revocation verdict -> supersede -> exactly
    one of the constraint/lift pair active, superseded_by link set."""
    from src.evaluator.reconciler import Verdict, map_verdicts

    writer = MemoryWriter(repo)
    old_id = await repo.add_fact(
        "coach",
        "pinned",
        "Плечо 07.08: жимы запрещены, только реабилитация",
        source="manual",
    )
    cand = {
        "op": "add",
        "kind": "user_fact",
        "tier": "pinned",
        "content": "Плечо: ограничения сняты 11.08, жимы разрешены",
        "label": None,
        "source_ref": None,
        "supersedes_id": None,
        "scope": "private",
    }
    mapped = map_verdicts(
        [cand],
        [Verdict(0, "contradicts", existing_id=old_id, safety=True, revokes=True)],
        {"private": {old_id}},
    )
    assert mapped.safety_flags == []
    count = await writer.apply_ops("coach", mapped.ops)
    assert count.applied == 1

    active = await repo.get_facts("coach")
    assert len(active) == 1
    assert "сняты" in active[0].content
    closed = await repo.get_facts("coach", status="superseded")
    assert len(closed) == 1
    assert closed[0].id == old_id
    assert closed[0].superseded_by == active[0].id


async def test_apply_ops_isolates_failures(repo):
    """A bad op is logged and skipped; good ops still apply."""
    writer = MemoryWriter(repo)
    ops = [
        {
            "op": "add",
            "tier": "general",
            "content": "good",
            "label": None,
            "supersedes_id": None,
        },
        # supersedes a non-existent id: UPDATE no-ops, insert still happens
        {
            "op": "supersede",
            "tier": "general",
            "content": "also good",
            "label": None,
            "supersedes_id": 999,
        },
    ]
    count = await writer.apply_ops("chef", ops)
    assert count.applied == 2
    active = {f.content for f in await repo.get_facts("chef")}
    assert "good" in active and "also good" in active


def test_normalize_fact_ignores_case_punctuation_and_spacing():
    assert normalize_fact("Allergic to peanuts!") == normalize_fact(
        "  allergic  to   peanuts "
    )


def test_is_duplicate_separates_similar_but_distinct_facts():
    """The threshold must not collapse two different allergies."""
    assert is_duplicate(
        normalize_fact("likes spicy food"), normalize_fact("likes spicy foods")
    )
    assert not is_duplicate(
        normalize_fact("allergic to peanuts"), normalize_fact("allergic to walnuts")
    )


async def test_apply_ops_rejects_duplicate_of_existing_fact(repo):
    """Prompt says 'never restate'; the writer enforces it."""
    writer = MemoryWriter(repo)
    await repo.add_fact("chef", "pinned", "Allergic to peanuts", source="manual")

    count = await writer.apply_ops(
        "chef",
        [
            {
                "op": "add",
                "tier": "pinned",
                "content": "allergic to peanuts!",
                "label": None,
                "supersedes_id": None,
            }
        ],
    )

    assert count.applied == 0
    assert writer.duplicates_rejected == 1
    assert len(await repo.get_facts("chef")) == 1


async def test_apply_ops_rejects_duplicate_within_one_batch(repo):
    """Two adds of the same fact in one run collapse to one row."""
    writer = MemoryWriter(repo)
    op = {
        "op": "add",
        "tier": "general",
        "content": "drinks coffee black",
        "label": None,
        "supersedes_id": None,
    }

    count = await writer.apply_ops("chef", [dict(op), dict(op)])

    assert count.applied == 1
    assert writer.duplicates_rejected == 1
    assert len(await repo.get_facts("chef")) == 1


async def test_apply_ops_rejects_noop_supersede(repo):
    """Superseding a fact with its own text is churn, not an update."""
    writer = MemoryWriter(repo)
    fact_id = await repo.add_fact("chef", "general", "has no oven", source="manual")

    count = await writer.apply_ops(
        "chef",
        [
            {
                "op": "supersede",
                "tier": "general",
                "content": "Has no oven.",
                "label": None,
                "supersedes_id": fact_id,
            }
        ],
    )

    assert count.applied == 0
    assert writer.duplicates_rejected == 1
    facts = await repo.get_facts("chef")
    assert [f.id for f in facts] == [fact_id]


# Real fact 161 (devops scope) — the measured inflation chain: 161 (1,100 ch)
# was superseded by 224 whose content was a model-merged 1,888-ch rewrite of
# old+new. Under the metadata-only writer that rewrite class cannot occur:
# the old row is closed byte-identical and the new row is exactly the op's
# content (dv-8254 criterion 3).
FACT_161_CONTENT = (
    "User prefers Poetry over pip for Python dependency management; project "
    "uses Poetry with pyproject.toml. Deployment is a systemd service "
    "claude-bot.service under user testuser with full filesystem access; "
    "logs via journalctl. " + "Configuration history and operational notes. " * 18
)


async def test_supersede_regression_161_224_no_merged_rewrite(repo):
    writer = MemoryWriter(repo)
    old_id = await repo.add_fact(
        "devops", "general", FACT_161_CONTENT, source="evaluator"
    )
    new_statement = "User now prefers uv over Poetry for dependency management"

    count = await writer.apply_ops(
        "devops",
        [
            {
                "op": "supersede",
                "tier": "general",
                "content": new_statement,
                "label": None,
                "supersedes_id": old_id,
                "kind": "user_fact",
            }
        ],
    )
    assert count.applied == 1

    closed = await repo.get_facts("devops", status="superseded")
    assert len(closed) == 1
    assert closed[0].content == FACT_161_CONTENT  # byte-identical
    assert closed[0].invalid_at is not None

    active = await repo.get_facts("devops")
    assert len(active) == 1
    assert active[0].content == new_statement  # exactly the op content
    assert closed[0].superseded_by == active[0].id
    # The merge signature: new row must not contain the old text.
    assert FACT_161_CONTENT not in active[0].content


async def test_scope_difference_coexists_and_flag_conflict_links(repo):
    """dv-8254 criterion 4: 'no nuts in desserts' / 'allergic to nuts' both
    stay active; flag_conflict pairs them without resolving."""
    writer = MemoryWriter(repo)
    id_a = await repo.add_fact("chef", "general", "no nuts in desserts")

    count = await writer.apply_ops(
        "chef",
        [
            {
                "op": "add",
                "tier": "pinned",
                "content": "allergic to nuts",
                "label": None,
                "supersedes_id": None,
            }
        ],
    )
    assert count.applied == 1  # not a duplicate — both facts stand
    active = await repo.get_facts("chef")
    assert len(active) == 2

    id_b = next(f.id for f in active if f.content == "allergic to nuts")
    count = await writer.apply_ops(
        "chef", [{"op": "flag_conflict", "ids": [id_a, id_b]}]
    )
    assert count.applied == 1
    active = await repo.get_facts("chef")
    assert len(active) == 2  # still both active
    groups = {f.conflict_group for f in active}
    assert groups == {str(min(id_a, id_b))}


async def test_apply_ops_invalidate_closes_without_insert(repo):
    writer = MemoryWriter(repo)
    fid = await repo.add_fact("chef", "general", "on a strict diet")

    count = await writer.apply_ops("chef", [{"op": "invalidate", "target_id": fid}])

    assert count.applied == 1
    assert await repo.get_facts("chef") == []
    closed = await repo.get_facts("chef", status="superseded")
    assert closed[0].invalidated_by == "evaluator"
    assert closed[0].superseded_by is None


async def test_apply_ops_invalidate_unknown_id_is_counted_not_applied(repo):
    writer = MemoryWriter(repo)
    count = await writer.apply_ops("chef", [{"op": "invalidate", "target_id": 999}])
    assert count.applied == 0
    assert await repo.get_facts("chef", status="superseded") == []


async def test_add_after_invalidate_in_same_batch_not_deduped_against_closed(repo):
    """Invalidate removes the row from the batch's dedupe set, so re-adding
    a changed statement right after closing the old one works."""
    writer = MemoryWriter(repo)
    fid = await repo.add_fact("chef", "general", "drinks coffee black")

    count = await writer.apply_ops(
        "chef",
        [
            {"op": "invalidate", "target_id": fid},
            {
                "op": "add",
                "tier": "general",
                "content": "drinks coffee black!",
                "label": None,
                "supersedes_id": None,
            },
        ],
    )
    assert count.applied == 2
    active = await repo.get_facts("chef")
    assert [f.content for f in active] == ["drinks coffee black!"]


async def test_apply_ops_conversation_result_kind_and_source_ref(repo):
    writer = MemoryWriter(repo)
    count = await writer.apply_ops(
        "coach",
        [
            {
                "op": "add",
                "tier": "general",
                "content": "Supplement research done, see file",
                "label": None,
                "supersedes_id": None,
                "kind": "conversation_result",
                "source_ref": "supplement_review_hericium.md",
            }
        ],
    )
    assert count.applied == 1
    facts = await repo.get_facts("coach")
    assert facts[0].kind == "conversation_result"
    assert facts[0].source_ref == "supplement_review_hericium.md"


async def test_apply_ops_dedupe_is_per_scope(repo):
    """The same fact may legitimately exist in two different agents' memory."""
    writer = MemoryWriter(repo)
    await repo.add_fact("chef", "general", "lives in Berlin", source="manual")

    count = await writer.apply_ops(
        "coach",
        [
            {
                "op": "add",
                "tier": "general",
                "content": "lives in Berlin",
                "label": None,
                "supersedes_id": None,
            }
        ],
    )

    assert count.applied == 1
    assert writer.duplicates_rejected == 0


async def test_oversized_user_fact_coerced_to_pointer(repo):
    """dv-7517 step 5: a user_fact over MAX_INJECTABLE_FACT_CHARS is stored
    in full but as conversation_result — injected as a pointer, never as a
    fact line. Label defaults to the (word-truncated) first line."""
    from src.evaluator.writer import MAX_INJECTABLE_FACT_CHARS

    writer = MemoryWriter(repo)
    first_line = "Sleep versus daylight analysis for early August"
    blob = first_line + "\n" + "analysis body " * 200  # ~2.8k chars
    assert len(blob) > MAX_INJECTABLE_FACT_CHARS

    count = await writer.apply_ops(
        "coach",
        [{"op": "add", "tier": "general", "content": blob, "label": None}],
    )
    assert count.applied == 1

    snap = await repo.facts_snapshot("coach")
    assert snap.general == []  # not injectable as a fact line
    assert len(snap.pointers) == 1
    assert snap.pointers[0].summary == first_line
    # Full text intact and reachable (recall path reads content).
    facts = await repo.get_facts("coach")
    assert facts[0].content == blob
    assert facts[0].kind == "conversation_result"


async def test_oversized_fact_at_limit_not_coerced(repo):
    from src.evaluator.writer import MAX_INJECTABLE_FACT_CHARS

    writer = MemoryWriter(repo)
    content = "x" * MAX_INJECTABLE_FACT_CHARS
    await writer.apply_ops(
        "coach",
        [{"op": "add", "tier": "general", "content": content, "label": None}],
    )
    facts = await repo.get_facts("coach")
    assert facts[0].kind == "user_fact"


async def test_oversized_pinned_and_core_never_coerced(repo):
    """A pinned/core fact must never silently lose always-injected status
    by being flipped to a pointer, whatever its size."""
    writer = MemoryWriter(repo)
    for tier, filler in (("pinned", "x"), ("core", "y")):
        await writer.apply_ops(
            "coach",
            [{"op": "add", "tier": tier, "content": f"{tier} " + filler * 700}],
        )
    facts = await repo.get_facts("coach")
    assert all(f.kind == "user_fact" for f in facts)
    assert {f.tier for f in facts} == {"pinned", "core"}


async def test_oversized_conversation_result_keeps_own_label(repo):
    writer = MemoryWriter(repo)
    await writer.apply_ops(
        "coach",
        [
            {
                "op": "add",
                "tier": "general",
                "content": "z" * 700,
                "label": "my label",
            }
        ],
    )
    facts = await repo.get_facts("coach")
    assert facts[0].kind == "conversation_result"
    assert facts[0].label == "my label"
