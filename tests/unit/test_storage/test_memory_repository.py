"""Tests for DB-backed agent memory."""

import re

import pytest

from src.storage.database import DatabaseManager
from src.storage.memory_repository import (
    MemoryRepository,
    MemoryRetriever,
    truncate_at_word,
)


@pytest.fixture
async def repo(tmp_path):
    db = DatabaseManager(str(tmp_path / "mem.db"))
    await db.initialize()
    yield MemoryRepository(db)
    await db.close()


async def test_add_and_get_facts(repo):
    await repo.add_fact("chef", "general", "likes spicy food")
    facts = await repo.get_facts("chef")
    assert len(facts) == 1
    assert facts[0].content == "likes spicy food"
    assert facts[0].tier == "general"
    assert facts[0].status == "active"


async def test_facts_isolated_by_agent(repo):
    await repo.add_fact("chef", "general", "likes spicy food")
    await repo.add_fact("devops", "general", "prefers terse output")
    chef = await repo.get_facts("chef")
    assert [f.content for f in chef] == ["likes spicy food"]


async def test_supersede_marks_old_and_keeps_history(repo):
    old = await repo.add_fact("chef", "general", "has no oven")
    new = await repo.supersede_fact(old, "chef", "general", "has an oven")

    active = await repo.get_facts("chef")  # status='active' default
    assert [f.content for f in active] == ["has an oven"]

    superseded = await repo.get_facts("chef", status="superseded")
    assert [f.content for f in superseded] == ["has no oven"]
    assert new != old


async def test_search_general_ranks_keyword_match_first(repo):
    await repo.add_fact("chef", "general", "loves beef stew")
    await repo.add_fact("chef", "general", "allergic to peanuts")
    hits = await repo.search_general("chef", "beef", limit=20)
    # FTS match leads; remaining facts fill up to the cap by recency.
    assert [f.content for f in hits] == ["loves beef stew", "allergic to peanuts"]

    # With limit=1 only the FTS match survives.
    hits = await repo.search_general("chef", "beef", limit=1)
    assert [f.content for f in hits] == ["loves beef stew"]


async def test_search_general_excludes_pinned_and_superseded(repo):
    await repo.add_fact("chef", "pinned", "no pork ever")
    sid = await repo.add_fact("chef", "general", "no pork last week")
    await repo.supersede_fact(sid, "chef", "general", "ok with pork now")
    hits = await repo.search_general("chef", "pork", limit=20)
    # pinned excluded (injected separately); superseded excluded
    assert [f.content for f in hits] == ["ok with pork now"]


async def test_retriever_returns_all_pinned_and_matched_general(repo):
    await repo.add_fact("chef", "pinned", "allergic to peanuts")
    await repo.add_fact("chef", "pinned", "no oven")
    await repo.add_fact("chef", "general", "loves beef stew")
    await repo.add_fact("chef", "general", "dislikes cilantro")

    retriever = MemoryRetriever(repo)
    pinned, general, pointers = await retriever.select(
        "chef", query="what beef dish tonight?"
    )

    # Every injected line is date-prefixed [DD.MM.YY] (owner decision D2).
    date_re = re.compile(r"^\[\d{2}\.\d{2}\.\d{2}\] ")
    assert all(date_re.match(line) for line in pinned + general)
    assert {line.split("] ", 1)[1] for line in pinned} == {
        "allergic to peanuts",
        "no oven",
    }
    # FTS match first, then recency fill up to the cap — cilantro included.
    assert [line.split("] ", 1)[1] for line in general] == [
        "loves beef stew",
        "dislikes cilantro",
    ]
    assert pointers == []


async def test_retriever_renders_conversation_results_as_pointers(repo):
    """conversation_result content is never injected — only a dated one-line
    pointer to its source file (dv-8254 criterion 1)."""
    research = "Hericium research findings: " + "x" * 500
    await repo.add_fact(
        "coach",
        "general",
        research,
        kind="conversation_result",
        source_ref="supplement_review_hericium.md",
    )
    await repo.add_fact("coach", "general", "prefers morning workouts")

    retriever = MemoryRetriever(repo)
    pinned, general, pointers = await retriever.select("coach", query="hericium")

    joined = "\n".join(pinned + general)
    assert research not in joined
    assert len(pointers) == 1
    assert "supplement_review_hericium.md" in pointers[0]
    assert re.match(r"^\[\d{2}\.\d{2}\.\d{2}\] ", pointers[0])
    # Pointer carries a short summary, never the full blob.
    assert research[:200] not in pointers[0]


async def test_supersede_is_metadata_only(repo):
    """The 161→224 regression class: old row's content must stay
    byte-identical after supersede; only metadata closes it."""
    old_content = "User had shoulder pain during bench press"
    old_id = await repo.add_fact("coach", "general", old_content)
    new_id = await repo.supersede_fact(
        old_id, "coach", "general", "Shoulder pain is gone"
    )

    superseded = await repo.get_facts("coach", status="superseded")
    assert len(superseded) == 1
    assert superseded[0].content == old_content  # byte-identical, never merged
    assert superseded[0].invalid_at is not None
    assert superseded[0].superseded_by == new_id

    active = await repo.get_facts("coach")
    assert [f.content for f in active] == ["Shoulder pain is gone"]
    assert active[0].valid_from is not None


async def test_invalidate_fact_closes_without_replacement(repo):
    fid = await repo.add_fact("chef", "general", "on a strict diet")
    ok = await repo.invalidate_fact(fid, "chef", source="user_request")
    assert ok
    assert await repo.get_facts("chef") == []
    closed = await repo.get_facts("chef", status="superseded")
    assert closed[0].invalid_at is not None
    assert closed[0].invalidated_by == "user_request"
    assert closed[0].superseded_by is None
    # Second invalidate is a no-op (already closed).
    assert not await repo.invalidate_fact(fid, "chef")


async def test_flag_conflict_keeps_both_active(repo):
    """Safety conflicts are never auto-resolved: both rows stay active,
    paired via the same conflict_group value on both (review R1)."""
    id_a = await repo.add_fact("chef", "general", "no nuts in desserts")
    id_b = await repo.add_fact("chef", "pinned", "allergic to nuts")
    await repo.flag_conflict(id_a, id_b, "chef")

    active = {f.id: f for f in await repo.get_facts("chef")}
    assert set(active) == {id_a, id_b}
    group = str(min(id_a, id_b))
    assert active[id_a].conflict_group == group
    assert active[id_b].conflict_group == group


async def test_search_include_invalid_returns_history(repo):
    old_id = await repo.add_fact("chef", "general", "no pork last week")
    await repo.supersede_fact(old_id, "chef", "general", "ok with pork now")

    hits = await repo.search("chef", "pork", limit=10)
    assert [f.content for f in hits] == ["ok with pork now"]

    history = await repo.search("chef", "pork", limit=10, include_invalid=True)
    contents = {f.content for f in history}
    assert contents == {"no pork last week", "ok with pork now"}
    closed = next(f for f in history if f.content == "no pork last week")
    assert closed.invalid_at is not None


async def test_facts_snapshot_shape(repo):
    p = await repo.add_fact("chef", "pinned", "allergic to peanuts")
    g = await repo.add_fact("chef", "general", "loves beef stew")
    await repo.add_fact(
        "chef",
        "general",
        "Long research blob " + "y" * 900,
        kind="conversation_result",
        source_ref="research.md",
    )
    c1 = await repo.add_fact("chef", "general", "no nuts in desserts")
    c2 = await repo.add_fact("chef", "pinned", "allergic to nuts")
    await repo.flag_conflict(c1, c2, "chef")

    snap = await repo.facts_snapshot("chef")
    assert {f.id for f in snap.pinned} >= {p}
    assert g in {f.id for f in snap.general}
    # conversation_result never appears in pinned/general
    assert all(f.kind == "user_fact" for f in snap.pinned + snap.general)
    assert len(snap.pointers) == 1
    assert snap.pointers[0].source_ref == "research.md"
    assert len(snap.pointers[0].summary) <= 100
    assert len(snap.conflicts) == 1
    assert {snap.conflicts[0][0].id, snap.conflicts[0][1].id} == {c1, c2}


async def test_facts_snapshot_core_uncapped_and_first(repo):
    """dv-7517 follow-up: core identity facts are exempt from the general
    cap and render ahead of general — recency must never push identity out
    of the prompt."""
    from src.storage.memory_repository import SNAPSHOT_GENERAL_CAP

    core_ids = []
    for i in range(3):
        core_ids.append(await repo.add_fact("coach", "core", f"core fact {i}"))
    for i in range(SNAPSHOT_GENERAL_CAP + 5):
        await repo.add_fact("coach", "general", f"general fact {i}")

    snap = await repo.facts_snapshot("coach")
    assert len(snap.general) == 3 + SNAPSHOT_GENERAL_CAP
    # Core first (all of them, despite being the OLDEST rows), then general.
    assert [f.id for f in snap.general[:3]] == sorted(core_ids, reverse=True)
    assert all(f.tier == "general" for f in snap.general[3:])


async def test_retriever_always_includes_core(repo):
    await repo.add_fact("coach", "core", "Age 34, strength not mass")
    await repo.add_fact("coach", "general", "prefers morning workouts")
    retriever = MemoryRetriever(repo)
    _, general, _ = await retriever.select("coach", query="unrelated query")
    assert any("Age 34" in line for line in general)


async def test_facts_snapshot_general_capped_newest_first(repo):
    """dv-7517 step 4: the uncapped bundle path gets SNAPSHOT_GENERAL_CAP;
    newest general facts survive, pinned stays uncapped."""
    from src.storage.memory_repository import SNAPSHOT_GENERAL_CAP

    for i in range(SNAPSHOT_GENERAL_CAP + 5):
        await repo.add_fact("coach", "general", f"general fact {i}")
    await repo.add_fact("coach", "pinned", "allergic to nuts")

    snap = await repo.facts_snapshot("coach")
    assert len(snap.general) == SNAPSHOT_GENERAL_CAP
    # Ordered newest-first: the oldest rows are the ones dropped.
    contents = [f.content for f in snap.general]
    assert f"general fact {SNAPSHOT_GENERAL_CAP + 4}" in contents
    assert "general fact 0" not in contents
    assert len(snap.pinned) == 1


def test_truncate_at_word_short_text_unchanged():
    assert truncate_at_word("short label") == "short label"


def test_truncate_at_word_exact_length_no_ellipsis():
    text = "x" * 100
    assert truncate_at_word(text) == text


def test_truncate_at_word_cuts_at_word_boundary():
    text = "sleep versus daylight analysis for the first week of august " * 3
    out = truncate_at_word(text)
    assert len(out) <= 100
    assert out.endswith("…")
    # No mid-word slice: everything before the ellipsis is a whole word.
    assert text.startswith(out[:-1])
    assert text[len(out) - 1] == " " or out[-2] != " "


def test_truncate_at_word_single_long_word_hard_cut():
    text = "y" * 150
    out = truncate_at_word(text)
    assert out == "y" * 99 + "…"


async def test_pointer_summary_truncates_at_word_boundary(repo):
    label = "sleep versus daylight correlation analysis " * 4  # > 100 chars
    await repo.add_fact(
        "coach",
        "general",
        "full analysis text",
        label=label,
        kind="conversation_result",
    )
    snap = await repo.facts_snapshot("coach")
    summary = snap.pointers[0].summary
    assert len(summary) <= 100
    assert summary.endswith("…")
    assert not summary[:-1].endswith("analysi")  # not sliced mid-word


async def test_search_general_fills_with_recent_on_no_match(repo):
    """Cross-language queries (Russian chat, English facts) match nothing in
    FTS; retrieval must still inject facts via the recency fill."""
    await repo.add_fact("chef", "general", "Has exotic spices: Ras El Hanout")
    await repo.add_fact("chef", "general", "Cooks for two people")
    facts = await repo.search_general("chef", "какие специи у меня есть")
    contents = {f.content for f in facts}
    assert "Has exotic spices: Ras El Hanout" in contents
    assert "Cooks for two people" in contents


async def test_search_general_no_duplicates_when_filling(repo):
    await repo.add_fact("chef", "general", "loves spicy ramen")
    await repo.add_fact("chef", "general", "hates cilantro")
    facts = await repo.search_general("chef", "spicy ramen tonight")
    ids = [f.id for f in facts]
    assert len(ids) == len(set(ids)) == 2


async def test_search_matches_across_all_tiers_no_recency_fill(repo):
    """`search` (the `recall` tool) has no per-turn cap to lean on: a query
    that matches nothing must return nothing, not a recency grab-bag."""
    await repo.add_fact("chef", "pinned", "allergic to peanuts")
    await repo.add_fact("chef", "general", "loves beef stew")
    await repo.add_fact("chef", "general", "dislikes cilantro")

    hits = await repo.search("chef", "peanuts", limit=10)
    assert [f.content for f in hits] == ["allergic to peanuts"]

    hits = await repo.search("chef", "something totally unrelated xyz", limit=10)
    assert hits == []


async def test_search_excludes_superseded(repo):
    sid = await repo.add_fact("chef", "general", "no pork last week")
    await repo.supersede_fact(sid, "chef", "general", "ok with pork now")

    hits = await repo.search("chef", "pork", limit=10)
    assert [f.content for f in hits] == ["ok with pork now"]


async def test_search_isolated_by_agent(repo):
    await repo.add_fact("chef", "general", "loves beef stew")
    await repo.add_fact("devops", "general", "loves beef stew too")

    hits = await repo.search("chef", "beef", limit=10)
    assert len(hits) == 1
    assert hits[0].agent_id == "chef"


async def test_search_respects_limit(repo):
    for i in range(5):
        await repo.add_fact("chef", "general", f"repeated keyword fact {i}")

    hits = await repo.search("chef", "repeated keyword", limit=2)
    assert len(hits) == 2


async def test_search_general_fill_respects_limit(repo):
    for i in range(5):
        await repo.add_fact("chef", "general", f"unrelated fact number {i}")
    facts = await repo.search_general("chef", "совершенно другой язык", limit=3)
    assert len(facts) == 3


# -- dv-5e4f Phase 0: methods added to host consolidation/fact_quality SQL --


async def test_active_scopes_lists_distinct_agents_with_active_facts(repo):
    await repo.add_fact("chef", "general", "fact one")
    await repo.add_fact("devops", "general", "fact two")
    await repo.add_fact("chef", "general", "fact three")
    assert await repo.active_scopes() == ["chef", "devops"]


async def test_active_scopes_excludes_fully_invalidated_agent(repo):
    import datetime as dt

    fact_id = await repo.add_fact("coach", "general", "stale fact")
    await repo.invalidate_fact(fact_id, "coach", source="test")
    assert "coach" not in await repo.active_scopes()
    _ = dt  # not otherwise needed; keeps import local to this test


async def test_expiry_candidates_by_cutoff(repo):
    import datetime as dt

    fact_id = await repo.add_fact("chef", "general", "old fact")
    await repo.invalidate_fact(fact_id, "chef", source="test")

    # Fresh invalidation: not yet a candidate at a tight cutoff.
    recent_cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(days=30)
    assert await repo.expiry_candidates("chef", recent_cutoff) == []

    # A cutoff in the future catches anything invalidated before "now".
    future_cutoff = dt.datetime.now(dt.UTC) + dt.timedelta(seconds=1)
    candidates = await repo.expiry_candidates("chef", future_cutoff)
    assert [f.id for f in candidates] == [fact_id]


async def test_new_facts_since_bound_and_scope(repo):
    import datetime as dt

    before = (
        (dt.datetime.now(dt.UTC) - dt.timedelta(minutes=1))
        .replace(tzinfo=None)
        .isoformat(sep=" ")
    )
    await repo.add_fact("chef", "general", "fresh chef fact")
    await repo.add_fact("devops", "general", "fresh devops fact")

    all_scopes = await repo.new_facts_since(before)
    assert {f.agent_id for f in all_scopes} == {"chef", "devops"}

    chef_only = await repo.new_facts_since(before, agent_id="chef")
    assert {f.agent_id for f in chef_only} == {"chef"}

    after = dt.datetime.now(dt.UTC).replace(tzinfo=None).isoformat(sep=" ")
    assert await repo.new_facts_since(after) == []
