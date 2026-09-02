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
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from src.bot.orchestrator import MessageOrchestrator
from src.config import create_test_config
from src.storage.database import DatabaseManager
from src.storage.window_repository import WindowRepository


class _Eval:
    def __init__(self, result: bool = True):
        self.calls = []
        self._result = result

    async def run(self, **kw):
        self.calls.append(kw)
        return self._result


async def _make_repo() -> WindowRepository:
    db = DatabaseManager(tempfile.mktemp(suffix=".db"))
    await db.initialize()
    return WindowRepository(
        db, eval_interval_tokens=50_000, refresh_threshold_tokens=150_000
    )


async def _seed_conversation(repo, user_id: int, working_dir: str) -> None:
    """One user + session + a couple of window-1 messages for tail/extraction."""
    async with repo._db.get_connection() as conn:
        await conn.execute("INSERT INTO users (user_id) VALUES (?)", (user_id,))
        await conn.execute(
            "INSERT INTO sessions (session_id, user_id, project_path) "
            "VALUES ('s', ?, ?)",
            (user_id, working_dir),
        )
        await conn.execute(
            "INSERT INTO messages (session_id,user_id,prompt,response,window_id) "
            "VALUES ('s',?,'weather at caparica','san jose costa rica',1)",
            (user_id,),
        )
        await conn.commit()


def _mock_response(context_tokens: int = 5_000):
    r = MagicMock()
    r.session_id = "fresh-session-1"
    r.content = "answer"
    r.tools_used = []
    r.input_tokens = 100
    r.output_tokens = 50
    r.context_tokens = context_tokens
    r.cost = 0.0
    r.error_type = None
    return r


def _harness(settings, repo, evaluator, run_command: AsyncMock):
    """update/context pair wired like the live text pipeline."""
    claude_integration = MagicMock()
    claude_integration.run_command = run_command

    storage = MagicMock()
    storage.db_manager = repo._db
    storage.memory_repository = None
    storage.save_claude_interaction = AsyncMock(return_value=1)
    storage.log_bot_event = AsyncMock()
    storage.pending_notes.consume = AsyncMock(return_value=[])

    update = MagicMock()
    update.effective_user.id = 123
    update.message.text = "so did you fix it?"
    update.message.message_id = 1
    update.message.reply_to_message = None
    update.message.chat.type = "group"
    update.message.chat.send_action = AsyncMock()
    update.message.reply_text = AsyncMock()
    progress_msg = AsyncMock()
    update.message.reply_text.return_value = progress_msg

    context = MagicMock()
    context.user_data = {}
    context.bot_data = {
        "settings": settings,
        "claude_integration": claude_integration,
        "storage": storage,
        "rate_limiter": None,
        "audit_logger": None,
        "window_repo": repo,
        "evaluator_service": evaluator,
    }
    return update, context


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
