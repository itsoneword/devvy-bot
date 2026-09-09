import tempfile

import pytest

from src.bot.orchestration.windowing import mentions_remember_trigger
from src.storage.database import DatabaseManager
from src.storage.window_repository import WindowRepository


@pytest.mark.parametrize(
    "text",
    [
        "please remember I have a knee injury",
        "запомни, что у меня травма колена",
        "не забудь про мою аллергию на орехи",
        "Remember this for next time",
        "don't forget I hate cilantro",
        "keep in mind I'm training for a marathon",
        "ЗАПОМНИ",
    ],
)
def test_mentions_remember_trigger_positive(text):
    assert mentions_remember_trigger(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "what's a good chest workout",
        "как приготовить борщ",
        None,
        "",
    ],
)
def test_mentions_remember_trigger_negative(text):
    assert mentions_remember_trigger(text) is False


class _Eval:
    def __init__(self, result=True):
        self.calls = []
        self._result = result

    async def run(self, **kw):
        self.calls.append(kw)
        return self._result


async def _account(
    window_repo,
    evaluator,
    *,
    user_id,
    working_dir,
    backend,
    added_tokens,
    conversation_text,
    max_message_id,
    agent_id="devops",
    force_eval=False,
    context_tokens=None,
):
    """Mirror of the orchestrator helper under test (see Step 3)."""
    from src.bot.orchestration.windowing import account_turn_and_maybe_eval

    return await account_turn_and_maybe_eval(
        window_repo=window_repo,
        evaluator=evaluator,
        user_id=user_id,
        working_dir=working_dir,
        backend=backend,
        added_tokens=added_tokens,
        context_tokens=context_tokens,
        conversation_text=conversation_text,
        max_message_id=max_message_id,
        agent_id=agent_id,
        force_eval=force_eval,
    )


@pytest.fixture
async def window_repo():
    db = DatabaseManager(tempfile.mktemp(suffix=".db"))
    await db.initialize()
    # Pinned low so these tests assert the helper's *logic* (checkpoint
    # crossing, force_eval, failure handling) rather than today's tuning —
    # the real thresholds are settings and will be re-tuned from live use.
    return WindowRepository(
        db, eval_interval_tokens=50_000, refresh_threshold_tokens=150_000
    )


async def test_evaluator_triggers_at_50k_on_active_backend(window_repo):
    ev = _Eval()
    # below threshold: no eval
    await _account(
        window_repo,
        ev,
        user_id=7,
        working_dir="/p",
        backend="openai_codex",
        added_tokens=49_000,
        context_tokens=49_000,
        conversation_text="...",
        max_message_id=10,
    )
    assert ev.calls == []
    # crosses 50k: eval on codex
    await _account(
        window_repo,
        ev,
        user_id=7,
        working_dir="/p",
        backend="openai_codex",
        added_tokens=2_000,
        # Codex reports a real level since 2026-09-05 (rollout file); the
        # flow sum is no longer its fallback, so the level is passed.
        context_tokens=51_000,
        conversation_text="convo",
        max_message_id=11,
    )
    # the evaluator runs in a background task; give it a tick to fire
    import asyncio

    await asyncio.sleep(0)
    assert len(ev.calls) == 1
    assert ev.calls[0]["backend"] == "openai_codex"
    assert ev.calls[0]["conversation_text"] == "convo"


async def test_evaluator_receives_active_profile_agent_id(window_repo):
    ev = _Eval()
    await _account(
        window_repo,
        ev,
        user_id=9,
        working_dir="/p",
        backend="claude",
        added_tokens=60_000,
        context_tokens=60_000,
        conversation_text="convo",
        max_message_id=5,
        agent_id="channel",
    )
    import asyncio

    await asyncio.sleep(0)
    assert len(ev.calls) == 1
    assert ev.calls[0]["agent_id"] == "channel"


async def test_force_eval_bypasses_token_checkpoint(window_repo):
    """A 'remember ...' turn should trigger the evaluator immediately, well
    below the 50k checkpoint (regression test for the coach/general profile
    bug where short conversations never crossed 50k and facts were silently
    dropped)."""
    ev = _Eval()
    await _account(
        window_repo,
        ev,
        user_id=11,
        working_dir="/p",
        backend="claude",
        added_tokens=1_200,
        conversation_text="user: remember I have a knee injury",
        max_message_id=2,
        agent_id="coach",
        force_eval=True,
    )
    import asyncio

    await asyncio.sleep(0)
    assert len(ev.calls) == 1
    assert ev.calls[0]["agent_id"] == "coach"
    assert ev.calls[0]["cumulative_tokens"] == 1_200


async def test_force_eval_does_not_consume_future_real_checkpoint(window_repo):
    """Forcing an early eval must not advance last_eval_checkpoint past what
    the token count actually earns — otherwise the *real* 50k crossing later
    in the same window would be silently skipped."""
    ev = _Eval()
    await _account(
        window_repo,
        ev,
        user_id=12,
        working_dir="/p",
        backend="claude",
        added_tokens=1_200,
        context_tokens=1_200,
        conversation_text="remember X",
        max_message_id=2,
        agent_id="coach",
        force_eval=True,
    )
    await _account(
        window_repo,
        ev,
        user_id=12,
        working_dir="/p",
        backend="claude",
        added_tokens=49_000,
        context_tokens=50_200,
        conversation_text="convo",
        max_message_id=3,
        agent_id="coach",
    )
    import asyncio

    await asyncio.sleep(0)
    assert len(ev.calls) == 2  # forced run + genuine 50k crossing


async def test_real_context_drives_the_checkpoint_not_the_flow_sum(window_repo):
    """With prompt caching a turn adds ~1k of countable flow while the live
    context jumps by tens of thousands. The evaluator has to follow the
    context, or on a cache-hitting deployment it effectively never runs
    (dv-9f31: 6.9 uncached input tokens per call, measured)."""
    ev = _Eval()
    await _account(
        window_repo,
        ev,
        user_id=21,
        working_dir="/p",
        backend="claude",
        added_tokens=1_100,  # what the old counter would have seen
        context_tokens=60_000,  # what the model actually read
        conversation_text="convo",
        max_message_id=4,
    )
    import asyncio

    await asyncio.sleep(0)
    assert len(ev.calls) == 1


async def test_failed_eval_does_not_advance_checkpoint(window_repo):
    """Regression test: a crashed/errored evaluator run used to advance the
    'already processed' message_id cursor anyway (mark_evaluator_run ran
    before evaluator.run(), unconditionally). That silently threw away the
    conversation slice forever — the fact the user asked to remember was
    gone even though nothing downstream ever surfaced an error to them. The
    cursor must only advance once evaluator.run() actually reports success.
    """
    import asyncio

    ev = _Eval(result=False)  # simulates the ARG_MAX crash / any failure
    await _account(
        window_repo,
        ev,
        user_id=13,
        working_dir="/p",
        backend="claude",
        added_tokens=1_000,
        conversation_text="remember my bench 1RM is 100kg",
        max_message_id=5,
        agent_id="coach",
        force_eval=True,
    )
    # mark_evaluator_run does real DB I/O with multiple internal await
    # points; a bare sleep(0) only yields once, which isn't always enough
    # to drain the background task fully.
    await asyncio.sleep(0.05)
    assert len(ev.calls) == 1  # the failing run did happen
    after_failure = await window_repo.get_or_create(13, "/p")
    assert after_failure.last_checkpoint_message_id == 0  # cursor stayed put

    # A later successful run must therefore still see the earlier content —
    # simulate it by re-forcing and checking the cursor now does advance.
    ev2 = _Eval(result=True)
    await _account(
        window_repo,
        ev2,
        user_id=13,
        working_dir="/p",
        backend="claude",
        added_tokens=500,
        conversation_text="remember my bench 1RM is 100kg\n(retry)",
        max_message_id=6,
        agent_id="coach",
        force_eval=True,
    )
    await asyncio.sleep(0.05)
    assert len(ev2.calls) == 1

    final = await window_repo.get_or_create(13, "/p")
    assert final.last_checkpoint_message_id == 6  # advanced only on success


async def test_no_double_extraction_when_turn_also_rolls_window(window_repo):
    """A turn that crosses the eval checkpoint AND the rotation threshold must
    extract once, not twice.

    Both triggers read the same delta (last_checkpoint_message_id only advances
    once an extraction completes), so the pair produced byte-identical prompts
    seconds apart -- 14 of 66 extractions on 2026-08-04, plus two "Memory
    updated" pings for one turn. refresh_window() owns the extraction in this
    case because its facts feed the next window's continuity seed.
    """
    import asyncio

    ev = _Eval()
    await _account(
        window_repo,
        ev,
        user_id=9,
        working_dir="/p",
        backend="claude",
        added_tokens=160_000,
        context_tokens=160_000,  # past BOTH 50k eval and 150k rotation
        conversation_text="convo",
        max_message_id=42,
    )
    await asyncio.sleep(0.05)
    assert ev.calls == [], "checkpoint eval must defer to the rotation eval"

    state = await window_repo.get_or_create(9, "/p")
    assert window_repo.should_refresh(state) is True


async def test_checkpoint_eval_still_runs_without_rotation(window_repo):
    """The skip above must not suppress ordinary checkpoint extraction."""
    import asyncio

    ev = _Eval()
    await _account(
        window_repo,
        ev,
        user_id=10,
        working_dir="/p",
        backend="claude",
        added_tokens=60_000,
        context_tokens=60_000,  # past 50k eval, below 150k rotation
        conversation_text="convo",
        max_message_id=43,
    )
    await asyncio.sleep(0.05)
    assert len(ev.calls) == 1
