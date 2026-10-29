import tempfile

import pytest

from src.storage.database import DatabaseManager
from src.storage.window_repository import WindowRepository


@pytest.fixture
async def repo():
    db = DatabaseManager(tempfile.mktemp(suffix=".db"))
    await db.initialize()
    return WindowRepository(db)


async def _seed_parents(db, user_id=7, session_id="s"):
    """messages has FKs to users + sessions; seed them so inserts are valid."""
    async with db.get_connection() as conn:
        await conn.execute("INSERT INTO users (user_id) VALUES (?)", (user_id,))
        await conn.execute(
            "INSERT INTO sessions (session_id, user_id, project_path) "
            "VALUES (?, ?, '/p')",
            (session_id, user_id),
        )
        await conn.commit()


async def test_consume_pending_seed_builds_preamble(repo):
    from src.bot.orchestration.windowing import consume_pending_seed

    await _seed_parents(repo._db)
    async with repo._db.get_connection() as conn:
        await conn.execute(
            "INSERT INTO messages (session_id,user_id,prompt,response,window_id) "
            "VALUES ('s',7,'old-q','old-a',1)"
        )
        await conn.commit()
    user_data = {"pending_seed_window": 1}
    prompt = await consume_pending_seed(
        repo,
        user_data,
        user_id=7,
        working_dir="/p",
        user_message="new-msg",
    )
    assert "old-q" in prompt and prompt.rstrip().endswith("new-msg")
    assert "pending_seed_window" not in user_data  # consumed


async def test_seed_survives_evaluator_checkpoint_at_end_of_window(repo):
    """Regression: the seed must NOT be bounded by the evaluator checkpoint.

    Rotation fires at 150k tokens and the evaluator at every 50k; 150k is an
    exact multiple of 50k, so in production the two ALWAYS land on the same
    turn. mark_evaluator_run then sets last_checkpoint_message_id to that
    final message id, so the old "messages after the checkpoint" seed was
    empty by construction on every single compaction — the fresh session
    started blank and the agent answered a direct follow-up with "I have no
    idea what you're referring to".
    """
    from src.bot.orchestration.windowing import consume_pending_seed

    await _seed_parents(repo._db)
    async with repo._db.get_connection() as conn:
        await conn.execute(
            "INSERT INTO messages (session_id,user_id,prompt,response,window_id) "
            "VALUES ('s',7,'weather at caparica','san jose costa rica',1)"
        )
        await conn.commit()
    # Evaluator checkpointed at the LAST message of the window (production case).
    await repo.record_turn(7, "/p", backend="claude", added_tokens=150_000)
    async with repo._db.get_connection() as conn:
        cur = await conn.execute("SELECT MAX(message_id) FROM messages")
        last_id = (await cur.fetchone())[0]
    await repo.mark_evaluator_run(7, "/p", max_message_id=last_id)
    old, _new = await repo.roll_window(7, "/p", new_backend="claude")

    user_data = {"pending_seed_window": old}
    prompt = await consume_pending_seed(
        repo,
        user_data,
        user_id=7,
        working_dir="/p",
        user_message="why did you pick costa rica?",
    )
    assert "weather at caparica" in prompt, "seed lost the pre-rotation context"
    assert prompt.rstrip().endswith("why did you pick costa rica?")


async def test_seed_is_capped_to_recent_turns(repo):
    """The tail is the last SEED_TAIL_TURNS turns, not the whole window."""
    from src.bot.orchestration.windowing import consume_pending_seed
    from src.bot.window_seed import SEED_TAIL_TURNS

    await _seed_parents(repo._db)
    async with repo._db.get_connection() as conn:
        for i in range(SEED_TAIL_TURNS + 5):
            await conn.execute(
                "INSERT INTO messages "
                "(session_id,user_id,prompt,response,window_id) "
                "VALUES ('s',7,?,?,1)",
                (f"q{i}", f"a{i}"),
            )
        await conn.commit()
    prompt = await consume_pending_seed(
        repo,
        {"pending_seed_window": 1},
        user_id=7,
        working_dir="/p",
        user_message="next",
    )
    # Newest kept, oldest dropped, chronological order preserved. (Inserted
    # SEED_TAIL_TURNS + 5 rows, so the kept tail is q{N+2}..q{N+4} regardless
    # of what N is set to.)
    assert f"q{SEED_TAIL_TURNS + 4}" in prompt
    assert "q0 " not in prompt and "q0\n" not in prompt
    assert prompt.index(f"q{SEED_TAIL_TURNS + 2}") < prompt.index(
        f"q{SEED_TAIL_TURNS + 3}"
    )


async def test_consume_pending_seed_noop_without_flag(repo):
    from src.bot.orchestration.windowing import consume_pending_seed

    prompt = await consume_pending_seed(
        repo,
        {},
        user_id=7,
        working_dir="/p",
        user_message="hello",
    )
    assert prompt == "hello"


def _snapshot_memory_repo():
    """A memory repo double exposing the facts_snapshot contract (dv-8254)."""
    from datetime import UTC, datetime
    from unittest.mock import AsyncMock, MagicMock

    from src.storage.memory_repository import (
        FactsSnapshot,
        MemoryFact,
        ResultPointer,
    )

    def fact(fid, content, tier="general"):
        return MemoryFact(
            id=fid,
            agent_id="devops",
            tier=tier,
            label=None,
            content=content,
            status="active",
            source="evaluator",
            valid_from=datetime(2026, 8, 4, tzinfo=UTC),
        )

    snapshot = FactsSnapshot(
        pinned=[fact(1, "never deploy without approval", tier="pinned")],
        general=[
            fact(2, "User deploys on Fridays"),
            fact(3, "Server has 3.8G RAM"),
            fact(4, "prefers walnuts"),
            fact(5, "walnuts give a rash"),
        ],
        conflicts=[(fact(4, "prefers walnuts"), fact(5, "walnuts give a rash"))],
        pointers=[
            ResultPointer(
                id=6,
                recorded_at=datetime(2026, 8, 3, tzinfo=UTC),
                label="supplement research",
                source_ref="supplement_review.md",
                summary="supplement research",
            )
        ],
    )
    memory_repo = MagicMock()
    memory_repo.facts_snapshot = AsyncMock(return_value=snapshot)
    return memory_repo


async def test_seed_includes_session_notes_from_memory(repo):
    """The seed carries the freshest extracted facts, not just the raw tail."""
    from src.bot.orchestration.windowing import consume_pending_seed

    await _seed_parents(repo._db)
    async with repo._db.get_connection() as conn:
        await conn.execute(
            "INSERT INTO messages (session_id,user_id,prompt,response,window_id) "
            "VALUES ('s',7,'we deploy on fridays','noted',1)"
        )
        await conn.commit()

    memory_repo = _snapshot_memory_repo()
    prompt = await consume_pending_seed(
        repo,
        {"pending_seed_window": 1},
        user_id=7,
        working_dir="/p",
        user_message="continue",
        memory_repo=memory_repo,
        agent_id="devops",
    )
    assert "<session_notes>" in prompt
    assert "- [04.08.26] User deploys on Fridays" in prompt
    assert "we deploy on fridays" in prompt  # raw tail still present
    # Same snapshot contract as the bundle: one call, shared renderers.
    memory_repo.facts_snapshot.assert_awaited_once_with("devops")


async def test_seed_notes_taxonomy_facts_vs_own_conclusions(repo):
    """dv-8254 taxonomy: user attributes vs the agent's past conclusions.

    Fact lines are dated [DD.MM.YY]; conversation_result rows appear only as
    pointers; conflict-pair members are excluded (they render with a warning
    in the bundle, not as settled truth here)."""
    from src.bot.orchestration.windowing import consume_pending_seed

    await _seed_parents(repo._db)
    async with repo._db.get_connection() as conn:
        await conn.execute(
            "INSERT INTO messages (session_id,user_id,prompt,response,window_id) "
            "VALUES ('s',7,'q','a',1)"
        )
        await conn.commit()

    prompt = await consume_pending_seed(
        repo,
        {"pending_seed_window": 1},
        user_id=7,
        working_dir="/p",
        user_message="continue",
        memory_repo=_snapshot_memory_repo(),
        agent_id="devops",
    )
    assert "About the user (dated [DD.MM.YY] = when each became true):" in prompt
    assert "Your own earlier conclusions (pointers, not content" in prompt
    assert "- [04.08.26] never deploy without approval" in prompt
    # Pointer is dated, names the artifact, and demands retrieval.
    assert "supplement research" in prompt
    assert "supplement_review.md" in prompt
    assert "retrieve via recall()/Read before relying on it" in prompt
    # Conflicted facts must not surface unflagged.
    assert "prefers walnuts" not in prompt
    assert "walnuts give a rash" not in prompt


async def test_seed_labels_shown_of_total_turns(repo):
    """Honest labelling: the seed states 'last N of TOTAL turns' from the
    real per-window count and flags the trim inside <recent_conversation>."""
    from src.bot.orchestration.windowing import consume_pending_seed
    from src.bot.window_seed import SEED_TAIL_TURNS

    await _seed_parents(repo._db)
    total = SEED_TAIL_TURNS + 7
    async with repo._db.get_connection() as conn:
        for i in range(total):
            await conn.execute(
                "INSERT INTO messages "
                "(session_id,user_id,prompt,response,window_id) "
                "VALUES ('s',7,?,?,1)",
                (f"q{i}", f"a{i}"),
            )
        await conn.commit()
    prompt = await consume_pending_seed(
        repo,
        {"pending_seed_window": 1},
        user_id=7,
        working_dir="/p",
        user_message="next",
    )
    assert prompt.startswith("<conversation_context>")
    assert f"last {SEED_TAIL_TURNS} of its {total} turns" in prompt
    assert (
        f"[last {SEED_TAIL_TURNS} of {total} turns; earlier turns trimmed — "
        "use recall_conversation if needed]" in prompt
    )
    assert "recall_conversation" in prompt


def test_resume_failed_reason_swaps_header_wording():
    """dv-b41f: the resume-failed seed must not claim a deliberate rotation."""
    from src.bot.window_seed import build_seed_preamble, select_tail

    tail = select_tail([("q1", "a1"), ("q2", "a2")], total_turns=9)
    prompt = build_seed_preamble(tail, "next", reason="resume_failed")
    assert prompt.startswith("<conversation_context>")
    assert "could not be resumed" in prompt
    assert "dropped deliberately" not in prompt
    assert "rotated" not in prompt
    assert "recall_conversation" in prompt
    assert "last 2 of its 9 turns" in prompt
    assert "<recent_conversation>" in prompt
    assert prompt.rstrip().endswith("next")


def test_default_reason_output_unchanged():
    """Guard for dv-e574 callers and llm_analytics SEED_MARKERS: omitting
    `reason` must be byte-identical to passing the default explicitly."""
    from src.bot.window_seed import build_seed_preamble, select_tail

    tail = select_tail([("q1", "a1")], total_turns=4)
    assert build_seed_preamble(tail, "next") == build_seed_preamble(
        tail, "next", reason="rotation"
    )
    assert "were dropped deliberately" in build_seed_preamble(tail, "next")


async def test_seed_works_without_memory_repo(repo):
    from src.bot.orchestration.windowing import consume_pending_seed

    await _seed_parents(repo._db)
    async with repo._db.get_connection() as conn:
        await conn.execute(
            "INSERT INTO messages (session_id,user_id,prompt,response,window_id) "
            "VALUES ('s',7,'hello','hi',1)"
        )
        await conn.commit()
    prompt = await consume_pending_seed(
        repo,
        {"pending_seed_window": 1},
        user_id=7,
        working_dir="/p",
        user_message="continue",
    )
    assert "<session_notes>" not in prompt
    assert "hello" in prompt


async def test_roll_window_starts_counter_at_seed_estimate(repo):
    """After a roll the counter reflects the carried-over seed, not 0/150k."""
    await repo.get_or_create(7, "/p")
    await repo.record_turn(7, "/p", backend="claude", added_tokens=150_000)
    await repo.roll_window(7, "/p", new_backend="claude", initial_tokens=4_200)
    state = await repo.get_or_create(7, "/p")
    assert state.cumulative_tokens == 4_200
    assert state.window_id == 2


async def test_refresh_window_estimates_seed_tokens(repo):
    """refresh_window rolls with an initial counter ≈ seed chars / 4."""
    from src.bot.orchestration.windowing import refresh_window

    await _seed_parents(repo._db)
    body = "x" * 4000
    async with repo._db.get_connection() as conn:
        await conn.execute(
            "INSERT INTO messages (session_id,user_id,prompt,response,window_id) "
            "VALUES ('s',7,?,?,1)",
            (body, body),
        )
        await conn.commit()
    await repo.record_turn(7, "/p", backend="claude", added_tokens=150_000)

    user_data = {}
    await refresh_window(
        repo,
        None,  # no evaluator
        user_data,
        user_id=7,
        working_dir="/p",
        backend="claude",
        conversation_text="",
    )
    state = await repo.get_or_create(7, "/p")
    assert state.window_id == 2
    # ~8000 chars of tail → ~2000 estimated tokens; loose bounds, not exact.
    assert 1500 < state.cumulative_tokens < 2500
    assert user_data["pending_seed_window"] == 1
    assert user_data["force_new_session"] is True


async def test_transition_survives_a_restart(repo):
    """dv-9f31 regression.

    On 2026-08-01T19:50 a rotation rolled window 6->7 and armed the fresh
    session in PTB user_data. The service restarted at 2026-08-02T07:11, the
    dict went with it, and the next message resumed the very session the roll
    had retired (f95398bd) — which then ran on to 418k of context.

    A restart is modelled here the only way that matters: the in-memory dict
    is empty and a brand-new repository object reads the database.
    """
    from src.bot.orchestration.windowing import consume_pending_seed, refresh_window

    await _seed_parents(repo._db)
    async with repo._db.get_connection() as conn:
        await conn.execute(
            "INSERT INTO messages (session_id,user_id,prompt,response,window_id) "
            "VALUES ('s',7,'what about the restart button','it needs approval',1)"
        )
        await conn.commit()

    user_data = {}
    await refresh_window(
        repo,
        None,
        user_data,
        user_id=7,
        working_dir="/p",
        backend="claude",
        conversation_text="convo",
    )
    assert user_data["force_new_session"] is True  # in-process path still set

    # --- restart: process state is gone, only the DB survives ---
    user_data.clear()
    after_restart = WindowRepository(repo._db)

    state = await after_restart.get_or_create(7, "/p")
    assert state.force_new_session is True, "restart cancelled the rotation"
    assert state.pending_seed_window == 1

    prompt = await consume_pending_seed(
        after_restart,
        user_data,
        user_id=7,
        working_dir="/p",
        user_message="so did you fix it?",
    )
    assert "what about the restart button" in prompt
    assert prompt.rstrip().endswith("so did you fix it?")

    await after_restart.clear_pending_transition(7, "/p")
    state = await after_restart.get_or_create(7, "/p")
    assert state.force_new_session is False
    assert state.pending_seed_window is None


async def test_seed_notes_cap_pointers_at_15_with_raised_snapshot_caps(repo):
    """dv-cf07: the bundle now carries up to 60 pointers; the roll seed stays
    compact -- at most 15 pointer lines (and 15 fact lines), not a second
    copy of the bundle's pointer list."""
    from src.bot.orchestration.windowing import _session_notes_for_seed
    from src.storage.memory_repository import MemoryRepository

    memory_repo = MemoryRepository(repo._db)
    for i in range(30):
        await memory_repo.add_fact("devops", "general", f"seed general fact {i}")
    for i in range(71):
        await memory_repo.add_fact(
            "devops",
            "general",
            f"body {i}",
            label=f"seed conclusion {i:02d}",
            kind="conversation_result",
        )

    notes = await _session_notes_for_seed(memory_repo, "devops")
    head, sep, pointer_block = notes.partition("Your own earlier conclusions")
    assert sep, notes
    pointer_lines = [ln for ln in pointer_block.splitlines() if ln.startswith("- ")]
    fact_lines = [ln for ln in head.splitlines() if ln.startswith("- ")]
    assert 0 < len(pointer_lines) <= 15
    assert 0 < len(fact_lines) <= 15
    # The newest conclusion is the one that survives the cut.
    assert "seed conclusion 70" in pointer_block
