"""Frozen per-window context bundle (dv-5e2a).

Criteria under test: byte-identity within a window (incl. across "restart"),
mid-window edits invisible until the roll, dated fact lines, pointer-only
conversation_results, adjacent flagged conflict pairs, and the pressure valve
(consolidation trigger, never truncation).
"""

import re
from pathlib import Path

import pytest

from src.claude.prompt_composer import PromptComposer
from src.claude.window_bundle import WindowBundleProvider
from src.storage.database import DatabaseManager
from src.storage.memory_repository import MemoryRepository
from src.storage.window_bundle_repository import WindowBundleRepository
from src.storage.window_repository import WindowRepository

DATE_RE = re.compile(r"\[\d{2}\.\d{2}\.\d{2}\]")  # owner D2: DD.MM.YY


@pytest.fixture
async def db(tmp_path):
    manager = DatabaseManager(str(tmp_path / "bundle.db"))
    await manager.initialize()
    yield manager
    await manager.close()


@pytest.fixture
async def memory_repo(db):
    return MemoryRepository(db)


@pytest.fixture
async def window_repo(db):
    return WindowRepository(db)


@pytest.fixture
async def bundle_repo(db):
    return WindowBundleRepository(db)


@pytest.fixture
def persona_file(tmp_path):
    persona = tmp_path / "agent.md"
    persona.write_text("You are the test agent persona.", encoding="utf-8")
    return persona


@pytest.fixture
def composer(tmp_path, persona_file):
    return PromptComposer(memory_path=tmp_path, agent_md_path=persona_file)


def make_provider(window_repo, bundle_repo, memory_repo, composer, **kwargs):
    return WindowBundleProvider(
        window_repo=window_repo,
        bundle_repo=bundle_repo,
        memory_repo=memory_repo,
        profile_registry=None,
        default_composer=composer,
        **kwargs,
    )


async def test_frozen_within_window_despite_edits(
    window_repo, bundle_repo, memory_repo, composer, persona_file, tmp_path
):
    provider = make_provider(window_repo, bundle_repo, memory_repo, composer)
    wd = tmp_path / "ws"
    await memory_repo.add_fact("devops", "general", "prefers terse output")

    first = await provider.get_or_materialize(wd, user_id=1)
    assert first is not None and not first.reused
    assert "prefers terse output" in first.text

    # Mid-window edits: a new fact and a persona rewrite must NOT leak in.
    await memory_repo.add_fact("devops", "general", "switched to fish shell")
    persona_file.write_text("A completely different persona.", encoding="utf-8")

    second = await provider.get_or_materialize(wd, user_id=1)
    assert second is not None and second.reused
    assert second.text == first.text
    assert second.sha256 == first.sha256
    assert "switched to fish shell" not in second.text


async def test_restart_reloads_persisted_bundle(
    window_repo, bundle_repo, memory_repo, composer, persona_file, tmp_path
):
    provider = make_provider(window_repo, bundle_repo, memory_repo, composer)
    wd = tmp_path / "ws"
    await memory_repo.add_fact("devops", "general", "prefers terse output")
    first = await provider.get_or_materialize(wd, user_id=1)

    # "Restart": fresh provider over the same DB, live state changed on disk.
    persona_file.write_text("Post-restart persona edit.", encoding="utf-8")
    reborn = make_provider(window_repo, bundle_repo, memory_repo, composer)
    after = await reborn.get_or_materialize(wd, user_id=1)
    assert after is not None and after.reused
    assert after.sha256 == first.sha256
    assert "Post-restart persona edit" not in after.text


async def test_roll_recomposes_with_edits(
    window_repo, bundle_repo, memory_repo, composer, tmp_path
):
    provider = make_provider(window_repo, bundle_repo, memory_repo, composer)
    wd = tmp_path / "ws"
    await memory_repo.add_fact("devops", "general", "prefers terse output")
    first = await provider.get_or_materialize(wd, user_id=1)

    await memory_repo.add_fact("devops", "general", "switched to fish shell")
    await window_repo.roll_window(1, wd, new_backend="claude")

    rolled = await provider.get_or_materialize(wd, user_id=1)
    assert rolled is not None and not rolled.reused
    assert rolled.window_id == first.window_id + 1
    assert "switched to fish shell" in rolled.text


async def test_fact_lines_dated_and_pointers_content_free(
    window_repo, bundle_repo, memory_repo, composer, tmp_path
):
    provider = make_provider(window_repo, bundle_repo, memory_repo, composer)
    wd = tmp_path / "ws"
    await memory_repo.add_fact("devops", "pinned", "never push to main")
    await memory_repo.add_fact("devops", "general", "height 185")
    secret_body = "Hericium erinaceus review: dosage 1g/day, evidence level moderate."
    await memory_repo.add_fact(
        "devops",
        "general",
        secret_body,
        label="supplement research",
        kind="conversation_result",
        source_ref="supplement_review_hericium.md",
    )

    bundle = await provider.get_or_materialize(wd, user_id=1)
    assert bundle is not None

    # Every injected fact/pointer line carries a DD.MM.YY date.
    for tag in ("constraints", "user_facts", "memory_pointers"):
        match = re.search(rf"<{tag}>\n(.*?)</{tag}>", bundle.text, re.DOTALL)
        assert match, f"missing section <{tag}>"
        for line in match.group(1).splitlines():
            if line.startswith("- "):
                assert DATE_RE.search(line), f"undated injected line: {line!r}"

    # conversation_result content appears nowhere; the pointer does.
    assert "evidence level moderate" not in bundle.text
    assert "supplement_review_hericium.md" in bundle.text
    assert bundle.pointers_count == 1
    assert bundle.facts_count == 2


async def test_conflict_pairs_adjacent_flagged_not_duplicated(
    window_repo, bundle_repo, memory_repo, composer, tmp_path
):
    provider = make_provider(window_repo, bundle_repo, memory_repo, composer)
    wd = tmp_path / "ws"
    id_a = await memory_repo.add_fact("devops", "general", "allergic to walnuts")
    id_b = await memory_repo.add_fact(
        "devops", "general", "ate walnut cake with no reaction"
    )
    await memory_repo.flag_conflict(id_a, id_b, "devops")

    bundle = await provider.get_or_materialize(wd, user_id=1)
    assert bundle is not None
    assert "⚠ CONFLICT (safety)" in bundle.text

    # Pair rendered adjacently, directly under the flag.
    lines = bundle.text.splitlines()
    flag_idx = next(i for i, line in enumerate(lines) if line.startswith("⚠ CONFLICT"))
    assert "allergic to walnuts" in lines[flag_idx + 1]
    assert "ate walnut cake" in lines[flag_idx + 2]

    # Conflicted facts appear once (in the pair), not again as plain lines.
    # Count inside <user_facts> only -- shared-capabilities.md coincidentally
    # uses "allergic to walnuts" as its example fact.
    user_facts = re.search(r"<user_facts>\n(.*?)</user_facts>", bundle.text, re.DOTALL)
    assert user_facts is not None
    assert user_facts.group(1).count("allergic to walnuts") == 1
    assert bundle.facts_count == 2


async def test_pressure_valve_triggers_consolidation_never_truncates(
    window_repo, bundle_repo, memory_repo, composer, tmp_path
):
    calls = []

    class FakeConsolidation:
        def request_run(self, scope, reason, snapshot_chars=None):
            calls.append((scope, reason, snapshot_chars))

    provider = make_provider(
        window_repo,
        bundle_repo,
        memory_repo,
        composer,
        facts_budget_chars=50,
    )
    provider.attach_consolidation(FakeConsolidation())
    wd = tmp_path / "ws"
    facts = [f"long standing preference number {i} about tooling" for i in range(5)]
    for fact in facts:
        await memory_repo.add_fact("devops", "general", fact)

    bundle = await provider.get_or_materialize(wd, user_id=1)
    assert bundle is not None
    # Nothing dropped: every fact is still injected.
    for fact in facts:
        assert fact in bundle.text
    # And the valve fired exactly once for this (scope, window).
    assert len(calls) == 1
    scope, reason, snapshot_chars = calls[0]
    assert scope == "devops"
    assert snapshot_chars is not None and snapshot_chars > 50


async def test_no_composer_returns_none(
    window_repo, bundle_repo, memory_repo, tmp_path
):
    provider = WindowBundleProvider(
        window_repo=window_repo,
        bundle_repo=bundle_repo,
        memory_repo=memory_repo,
        profile_registry=None,
        default_composer=None,
    )
    assert await provider.get_or_materialize(tmp_path / "ws", user_id=1) is None


async def test_repository_first_write_wins(bundle_repo, tmp_path):
    from src.storage.window_bundle_repository import WindowBundle

    def row(text, sha):
        return WindowBundle(
            user_id=1,
            working_dir=str(tmp_path / "ws"),
            window_id=1,
            agent_id="devops",
            bundle_text=text,
            bundle_sha256=sha,
            persona_chars=10,
            facts_count=0,
            pointers_count=0,
            family_count=0,
            total_chars=len(text),
        )

    first = await bundle_repo.put(row("first text", "sha-first"))
    second = await bundle_repo.put(row("second text", "sha-second"))
    assert first.bundle_text == "first text"
    assert second.bundle_text == "first text"  # INSERT OR IGNORE kept row 1

    stored = await bundle_repo.get(1, Path(tmp_path / "ws"), 1)
    assert stored is not None and stored.bundle_sha256 == "sha-first"
