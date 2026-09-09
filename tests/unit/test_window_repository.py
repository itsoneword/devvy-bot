import tempfile

import pytest

from src.storage.database import DatabaseManager
from src.storage.window_repository import WindowRepository


@pytest.fixture
async def repo():
    db = DatabaseManager(tempfile.mktemp(suffix=".db"))
    await db.initialize()
    return WindowRepository(db)


async def test_get_or_create_defaults(repo):
    st = await repo.get_or_create(7, "/home/me/proj")
    assert st.window_id == 1
    assert st.active_backend == "claude"
    assert st.cumulative_tokens == 0


async def test_record_turn_accumulates_and_sets_backend(repo):
    await repo.get_or_create(7, "/p")
    st = await repo.record_turn(7, "/p", backend="openai_codex", added_tokens=120)
    assert st.cumulative_tokens == 120
    assert st.active_backend == "openai_codex"
    st = await repo.record_turn(7, "/p", backend="openai_codex", added_tokens=30)
    assert st.cumulative_tokens == 150


async def test_thresholds_read_the_context_level_not_the_flow_sum(repo):
    """The whole point of dv-9f31: a conversation that has *processed* a lot
    of tokens is not the same as one that is currently *large*. With prompt
    caching the flow sum is ~0.4% of the context, so gating on it never
    fires."""
    # 10 turns, each a small delta of new tokens but a large live context.
    for _ in range(10):
        st = await repo.record_turn(
            7, "/p", backend="claude", added_tokens=900, context_tokens=180_000
        )
    assert st.cumulative_tokens == 9_000  # flow: tiny
    assert st.last_context_tokens == 180_000  # level: the truth
    assert repo.should_run_evaluator(st) is True
    assert repo.should_refresh(st) is False  # 180k < 200k

    st = await repo.record_turn(
        7, "/p", backend="claude", added_tokens=900, context_tokens=201_000
    )
    assert repo.should_refresh(st) is True


async def test_should_run_evaluator_at_eval_interval(repo):
    await repo.get_or_create(7, "/p")
    st = await repo.record_turn(
        7, "/p", backend="claude", added_tokens=1, context_tokens=119_000
    )
    assert repo.should_run_evaluator(st) is False
    st = await repo.record_turn(
        7, "/p", backend="claude", added_tokens=1, context_tokens=121_000
    )
    assert repo.should_run_evaluator(st) is True


async def test_thresholds_are_configurable(repo):
    """The owner tunes these from lived experience, so they must not be
    baked-in constants."""
    repo.eval_interval_tokens = 10_000
    repo.refresh_threshold_tokens = 20_000
    st = await repo.record_turn(
        7, "/p", backend="claude", added_tokens=1, context_tokens=21_000
    )
    assert repo.should_run_evaluator(st) is True
    assert repo.should_refresh(st) is True


async def test_context_tokens_none_falls_back_to_flow_sum(repo):
    """A backend that can never measure a level (none shipped today) gets
    the flow sum, so rotation still eventually fires for it."""
    await repo.get_or_create(7, "/p")
    st = await repo.record_turn(7, "/p", backend="other", added_tokens=80_000)
    assert st.last_context_tokens == 80_000
    st = await repo.record_turn(7, "/p", backend="other", added_tokens=130_000)
    assert st.last_context_tokens == 210_000
    assert repo.should_refresh(st) is True


async def test_codex_holds_the_level_when_a_turn_cannot_measure_it(repo):
    """Codex reports a level from its rollout file (2026-09-05). A turn whose
    rollout could not be read must NOT fall back to the flow sum: that sum is
    the thread's cumulative prompt total and is exactly what rolled the
    window on every turn."""
    await repo.get_or_create(7, "/p")
    st = await repo.record_turn(
        7, "/p", backend="openai_codex", added_tokens=170_000, context_tokens=36_000
    )
    assert st.last_context_tokens == 36_000
    st = await repo.record_turn(7, "/p", backend="openai_codex", added_tokens=180_000)
    assert st.last_context_tokens == 36_000
    assert repo.should_refresh(st) is False


async def test_mark_evaluator_run_advances_checkpoint(repo):
    await repo.get_or_create(7, "/p")
    st = await repo.record_turn(
        7, "/p", backend="claude", added_tokens=51_000, context_tokens=121_000
    )
    await repo.mark_evaluator_run(7, "/p", max_message_id=42)
    st = await repo.get_or_create(7, "/p")
    assert st.last_eval_checkpoint == 1
    assert st.last_checkpoint_message_id == 42
    assert repo.should_run_evaluator(st) is False


async def test_roll_window_bumps_and_resets(repo):
    await repo.get_or_create(7, "/p")
    await repo.record_turn(
        7, "/p", backend="claude", added_tokens=151_000, context_tokens=201_000
    )
    old, new = await repo.roll_window(7, "/p", new_backend="openai_codex")
    assert old == 1 and new == 2
    st = await repo.get_or_create(7, "/p")
    assert st.window_id == 2
    assert st.cumulative_tokens == 0
    assert st.last_eval_checkpoint == 0
    assert st.active_backend == "openai_codex"


async def test_roll_window_baseline_does_not_immediately_trigger_evaluator(repo):
    """dv-r3c1 item 1: roll_window used to hardcode last_eval_checkpoint=0
    while the new window opens non-empty (system prompt + persona + facts +
    tool schemas + seed). If the opening baseline's own quotient is already
    >= 1, a hardcoded-0 checkpoint makes the very first turn look like it
    crossed an interval it never actually crossed. Reproduces the original
    50k-interval scenario the bug was found under."""
    repo.eval_interval_tokens = 50_000
    repo.refresh_threshold_tokens = 150_000
    await repo.record_turn(
        7, "/p", backend="claude", added_tokens=1, context_tokens=160_000
    )
    # Opens at 92k -- above one interval multiple, below two.
    await repo.roll_window(7, "/p", new_backend="claude", initial_tokens=92_000)
    st = await repo.get_or_create(7, "/p")
    assert st.last_context_tokens == 92_000
    assert st.last_eval_checkpoint == 1  # baseline's own quotient, not 0
    # First turn after the roll barely grows the context -- must NOT fire.
    st = await repo.record_turn(
        7, "/p", backend="claude", added_tokens=200, context_tokens=93_500
    )
    assert repo.should_run_evaluator(st) is False

    # A genuine crossing within the same window still must fire.
    st = await repo.record_turn(
        7, "/p", backend="claude", added_tokens=5_000, context_tokens=141_000
    )
    assert repo.should_run_evaluator(st) is True


async def test_roll_window_drops_the_level_or_it_rolls_forever(repo):
    """The level gates the roll. If roll_window reset only the flow sum, every
    turn after the first roll would see should_refresh() still true and roll
    again, shredding the conversation one turn at a time."""
    await repo.record_turn(
        7, "/p", backend="claude", added_tokens=1, context_tokens=250_000
    )
    await repo.roll_window(7, "/p", new_backend="claude", initial_tokens=4_000)
    st = await repo.get_or_create(7, "/p")
    assert st.last_context_tokens == 4_000
    assert repo.should_refresh(st) is False


async def test_pending_transition_survives_as_stored_state(repo):
    """The roll's "fresh session next turn" intent must outlive the process —
    it used to live in PTB user_data and a restart silently cancelled the
    rotation (dv-9f31)."""
    await repo.get_or_create(7, "/p")
    st = await repo.get_or_create(7, "/p")
    assert st.pending_seed_window is None
    assert st.force_new_session is False

    await repo.set_pending_transition(7, "/p", seed_window=3, force_new_session=True)
    st = await repo.get_or_create(7, "/p")
    assert st.pending_seed_window == 3
    assert st.force_new_session is True

    # Surviving a "restart" == being readable from a fresh repo over the same
    # database, with no in-process state carried across.
    fresh = WindowRepository(repo._db)
    st = await fresh.get_or_create(7, "/p")
    assert st.pending_seed_window == 3
    assert st.force_new_session is True

    await fresh.clear_pending_transition(7, "/p")
    st = await repo.get_or_create(7, "/p")
    assert st.pending_seed_window is None
    assert st.force_new_session is False


async def test_list_for_user_returns_all_windows_without_creating(repo):
    assert await repo.list_for_user(7) == []
    await repo.record_turn(7, "/a", backend="claude", added_tokens=9489)
    await repo.record_turn(7, "/b", backend="openai_codex", added_tokens=78023)
    await repo.record_turn(8, "/a", backend="claude", added_tokens=1)
    windows = await repo.list_for_user(7)
    assert {w.working_dir: w.cumulative_tokens for w in windows} == {
        "/a": 9489,
        "/b": 78023,
    }
    # Read-only: no row created for a user that never had a window
    assert await repo.list_for_user(99) == []


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


async def test_tail_messages_returns_window_tail_oldest_first(repo):
    await _seed_parents(repo._db)
    async with repo._db.get_connection() as conn:
        await conn.execute(
            "INSERT INTO messages (session_id,user_id,prompt,response,window_id) "
            "VALUES ('s',7,'q1','a1',1),('s',7,'q2','a2',1)"
        )
        await conn.commit()
    rows = await repo.tail_messages(7, "/p", window_id=1, limit=10)
    assert rows == [("q1", "a1"), ("q2", "a2")]
    # limit keeps the NEWEST rows but still returns them chronologically
    rows = await repo.tail_messages(7, "/p", window_id=1, limit=1)
    assert rows == [("q2", "a2")]


async def test_tail_messages_does_not_leak_other_profiles_same_window_id(repo):
    """Every profile's window counter independently starts at 1 (get_or_create),
    so two different working_dirs for the same user both have a window_id=1.
    tail_messages must scope by working_dir (via sessions.project_path), not
    just window_id, or one profile's tail bleeds into another's rotation seed
    / evaluator conversation text."""
    await _seed_parents(repo._db, session_id="s-devops")
    async with repo._db.get_connection() as conn:
        await conn.execute(
            "INSERT INTO sessions (session_id, user_id, project_path) "
            "VALUES ('s-coach', 7, '/coach')"
        )
        await conn.execute(
            "INSERT INTO messages (session_id,user_id,prompt,response,window_id) "
            "VALUES ('s-devops',7,'restart button','approve/deny',1)"
        )
        await conn.execute(
            "INSERT INTO messages (session_id,user_id,prompt,response,window_id) "
            "VALUES ('s-coach',7,'weather in costa rica','san jose coords',1)"
        )
        await conn.commit()
    rows = await repo.tail_messages(7, "/p", window_id=1, limit=10)
    assert rows == [("restart button", "approve/deny")]
    rows = await repo.tail_messages(7, "/coach", window_id=1, limit=10)
    assert rows == [("weather in costa rica", "san jose coords")]


async def test_unmeasurable_claude_turn_leaves_the_level_alone(repo):
    """dv-9f31 (2026-08-05): on a backend that CAN measure a level,
    context_tokens=None means "not this turn", not "the conversation is now
    tiny". Since the level moved to the per-call peak, an aborted turn can
    legitimately report nothing — and recomputing the level from the flow sum
    there would reset a 300k window to ~1k and postpone its rotation."""
    await repo.record_turn(
        1, "/w", backend="claude", added_tokens=500, context_tokens=310_000
    )
    st = await repo.record_turn(
        1, "/w", backend="claude", added_tokens=900, context_tokens=None
    )
    assert st.last_context_tokens == 310_000
    # The flow sum still advances — only the level is held.
    assert st.cumulative_tokens == 1_400
