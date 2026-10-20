"""dv-d9e2: every path that ends a backend session preserves data the same way.

Path 1 (normal turn-end roll) is covered by test_orchestrator_window_accounting
and test_window_transitions. This file covers:

* Path 2 — the pre-turn over-threshold guard ("no armed transition" warning):
  must run the SAME transition as a normal crossing (final extraction over the
  outgoing slice, window roll so the bundle re-materializes, armed seed) and
  let the same turn consume it — not the old bare force_new that dropped ~164k
  tokens of context (live incident 2026-08-08 10:21:08Z).
* Path 3 — /new: explicit clean break. Final extraction + roll still happen
  (data care), but deliberately NO continuity seed.
"""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from src.bot.orchestrator import MessageOrchestrator
from src.config import create_test_config
from tests.unit.window_harness import (
    _Eval,
    _harness,
    _make_repo,
    _mock_response,
    _seed_conversation,
)


async def test_guard_runs_full_transition_and_seeds_same_turn(tmp_path):
    """Over threshold + nothing armed: the guard must arm and consume a REAL
    roll — extraction, new window_id, seed on this very turn — not a bare
    seedless fresh session."""
    settings = create_test_config(approved_directory=str(tmp_path), agentic_mode=True)
    repo = await _make_repo()
    wd = str(Path(tmp_path))
    ev = _Eval()

    await _seed_conversation(repo, 123, wd)
    # Live incident shape: level over threshold, NO pending transition.
    await repo.record_turn(
        123, wd, backend="claude", added_tokens=1_000, context_tokens=175_990
    )
    state = await repo.get_or_create(123, wd)
    assert repo.should_refresh(state) and not state.force_new_session

    seen = {}

    async def run_command(**kw):
        # Captured AT RUN TIME: the roll must have already happened, so the
        # bundle provider (keyed on window_id) materializes fresh, and the
        # prompt must carry the continuity seed.
        seen["window_id"] = (await repo.get_or_create(123, wd)).window_id
        seen["prompt"] = kw["prompt"]
        seen["force_new"] = kw["force_new"]
        return _mock_response()

    orchestrator = MessageOrchestrator(settings, {"claude_integration": MagicMock()})
    update, context = _harness(settings, repo, ev, AsyncMock(side_effect=run_command))

    await orchestrator.agentic_text(update, context)
    await asyncio.sleep(0.05)  # drain the background extraction task

    # The turn ran against the NEW window with a fresh session and the seed.
    assert seen["force_new"] is True
    assert seen["window_id"] == 2
    assert "<conversation_context>" in seen["prompt"]
    assert "weather at caparica" in seen["prompt"]
    assert seen["prompt"].rstrip().endswith("so did you fix it?")

    # Exactly ONE extraction — the refresh run over the outgoing slice.
    assert len(ev.calls) == 1
    assert ev.calls[0]["session_id"].endswith(":refresh")
    assert ev.calls[0]["since_message_id"] == 0
    assert "weather at caparica" in ev.calls[0]["conversation_text"]

    # Transition was consumed and cleared; window stayed rolled.
    after = await repo.get_or_create(123, wd)
    assert after.window_id == 2
    assert after.force_new_session is False
    assert after.pending_seed_window is None


async def test_guard_does_not_fire_below_threshold(tmp_path):
    """Non-crossing traffic is untouched: no roll, no seed, no extraction."""
    settings = create_test_config(approved_directory=str(tmp_path), agentic_mode=True)
    repo = await _make_repo()
    wd = str(Path(tmp_path))
    ev = _Eval()

    await _seed_conversation(repo, 123, wd)
    await repo.record_turn(
        123, wd, backend="claude", added_tokens=1_000, context_tokens=40_000
    )

    seen = {}

    async def run_command(**kw):
        seen["prompt"] = kw["prompt"]
        seen["force_new"] = kw["force_new"]
        return _mock_response(context_tokens=41_000)

    orchestrator = MessageOrchestrator(settings, {"claude_integration": MagicMock()})
    update, context = _harness(settings, repo, ev, AsyncMock(side_effect=run_command))

    await orchestrator.agentic_text(update, context)
    await asyncio.sleep(0.05)

    assert seen["force_new"] is False
    assert seen["prompt"] == "so did you fix it?"  # no seed prepended
    assert ev.calls == []
    after = await repo.get_or_create(123, wd)
    assert after.window_id == 1


async def test_new_command_extracts_rolls_but_never_seeds(tmp_path):
    """/new: final extraction over the un-extracted tail + roll (fresh bundle
    next turn), durable seedless force-new — and NO seed on the next turn."""
    from src.bot.orchestration.windowing import consume_pending_seed

    settings = create_test_config(approved_directory=str(tmp_path), agentic_mode=True)
    repo = await _make_repo()
    wd = str(Path(tmp_path))
    ev = _Eval()

    await _seed_conversation(repo, 123, wd)
    await repo.record_turn(
        123, wd, backend="claude", added_tokens=1_000, context_tokens=40_000
    )
    # A previously armed SEEDED transition must not leak into the clean start.
    await repo.set_pending_transition(123, wd, seed_window=1, force_new_session=True)

    storage = MagicMock()
    storage.db_manager = repo._db

    orchestrator = MessageOrchestrator(settings, {"claude_integration": MagicMock()})
    update = MagicMock()
    update.effective_user.id = 123
    update.message.reply_text = AsyncMock()
    context = MagicMock()
    context.user_data = {"pending_seed_window": 1}
    context.bot_data = {
        "window_repo": repo,
        "evaluator_service": ev,
        "storage": storage,
    }

    await orchestrator.agentic_new(update, context)
    await asyncio.sleep(0.05)

    # Un-extracted tail went through a final extraction before the roll.
    assert len(ev.calls) == 1
    assert ev.calls[0]["session_id"].endswith(":reset")
    assert ev.calls[0]["since_message_id"] == 0
    assert "weather at caparica" in ev.calls[0]["conversation_text"]

    # Window rolled; reset is durable and explicitly seedless.
    state = await repo.get_or_create(123, wd)
    assert state.window_id == 2
    assert state.force_new_session is True
    assert state.pending_seed_window is None
    assert context.user_data["force_new_session"] is True
    assert "pending_seed_window" not in context.user_data

    # The next turn starts clean: no seed preamble.
    prompt = await consume_pending_seed(
        repo, context.user_data, user_id=123, working_dir=wd, user_message="hello"
    )
    assert prompt == "hello"
    update.message.reply_text.assert_called_once_with("Session reset. What's next?")


async def test_new_command_skips_extraction_when_nothing_to_extract(tmp_path):
    """Empty un-extracted tail → no evaluator invocation, roll still happens."""
    settings = create_test_config(approved_directory=str(tmp_path), agentic_mode=True)
    repo = await _make_repo()
    wd = str(Path(tmp_path))
    ev = _Eval()
    await repo.get_or_create(123, wd)  # window exists, zero messages

    storage = MagicMock()
    storage.db_manager = repo._db

    orchestrator = MessageOrchestrator(settings, {"claude_integration": MagicMock()})
    update = MagicMock()
    update.effective_user.id = 123
    update.message.reply_text = AsyncMock()
    context = MagicMock()
    context.user_data = {}
    context.bot_data = {
        "window_repo": repo,
        "evaluator_service": ev,
        "storage": storage,
    }

    await orchestrator.agentic_new(update, context)
    await asyncio.sleep(0.05)

    assert ev.calls == []
    state = await repo.get_or_create(123, wd)
    assert state.window_id == 2
    assert state.force_new_session is True
