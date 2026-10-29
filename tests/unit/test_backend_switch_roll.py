"""A backend switch rebuilds the current window; it never rolls (dv-168e).

Contract (owner, 2026-10-10):

* Switching claude <-> openai_codex keeps the window id and its token
  counter, and runs no extraction (the 150k rotation still does its own).
* The first turn on the new backend starts a fresh native session seeded with
  EVERY stored turn of the current window -- not the 5-turn rotation tail and
  not capped by ``SEED_MAX_CHARS``.
* Switching back to a backend used earlier in the window must not resume its
  stale session (it lacks the other model's turns): same full rebuild.
* The intent is armed in ``conversation_windows``, so a restart between the
  switch and the next message still rebuilds (dv-9f31/dv-a826).
* A same-backend tier change stays a no-op here: the session resumes.
"""

import asyncio
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.bot.orchestration.windowing import consume_pending_seed
from src.bot.orchestrator import MessageOrchestrator
from src.bot.window_seed import SEED_MAX_CHARS, SEED_TAIL_TURNS
from src.config import create_test_config
from src.storage.database import DatabaseManager
from src.storage.facade import Storage
from src.storage.window_repository import WindowRepository

USER_ID = 777


@pytest.fixture
async def storage():
    with tempfile.TemporaryDirectory() as temp_dir:
        st = Storage(f"sqlite:///{Path(temp_dir) / 'bot.db'}")
        await st.initialize()
        yield st
        await st.close()


async def _seed_turns(db, workdir: Path, window_id: int, turns, user_id=USER_ID):
    """Store ``turns`` [(prompt, response)] in ``window_id`` for ``workdir``.

    Messages join to sessions on project_path, so the sessions row carries
    the same dir the callback resolves.
    """
    async with db.get_connection() as conn:
        await conn.execute(
            "INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,)
        )
        await conn.execute(
            "INSERT OR IGNORE INTO sessions (session_id, user_id, project_path) "
            "VALUES ('s', ?, ?)",
            (user_id, str(workdir)),
        )
        for prompt, response in turns:
            await conn.execute(
                "INSERT INTO messages "
                "(session_id,user_id,prompt,response,window_id) "
                "VALUES ('s', ?, ?, ?, ?)",
                (user_id, prompt, response, window_id),
            )
        await conn.commit()


def _context(storage, window_repo, workdir: Path, evaluator, *, backend: str):
    context = MagicMock()
    context.user_data = {
        "current_directory": workdir,
        "agent_backend": backend,
        "role": "owner",
    }
    context.bot_data = {
        "window_repo": window_repo,
        "storage": storage,
        "evaluator_service": evaluator,
        "profile_registry": None,
    }
    return context


def _query(user_id=USER_ID):
    query = MagicMock()
    query.from_user.id = user_id
    query.edit_message_text = AsyncMock()
    return query


async def _drain_tasks() -> None:
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    if pending:
        await asyncio.gather(*pending)


async def test_switch_arms_full_rebuild_without_roll(storage, tmp_path):
    workdir = tmp_path / "ws"
    workdir.mkdir()
    settings = create_test_config(approved_directory=str(workdir))
    window_repo = WindowRepository(storage.db_manager)
    evaluator = MagicMock()
    evaluator.run = AsyncMock(return_value=True)

    await window_repo.get_or_create(USER_ID, workdir)
    await _seed_turns(storage.db_manager, workdir, 1, [("old-q", "old-a")])
    await window_repo.record_turn(
        USER_ID,
        workdir,
        backend="openai_codex",
        added_tokens=900,
        context_tokens=26_000,
    )
    before = await window_repo.get_or_create(USER_ID, workdir)

    orch = MessageOrchestrator(settings, {})
    context = _context(storage, window_repo, workdir, evaluator, backend="openai_codex")
    await orch._handle_model_callback(_query(), context, "model:claude:opus")
    await _drain_tasks()

    # No roll, no extraction, counter untouched.
    evaluator.run.assert_not_awaited()
    after = await window_repo.get_or_create(USER_ID, workdir)
    assert after.window_id == before.window_id
    assert after.last_context_tokens == 26_000
    assert after.cumulative_tokens == before.cumulative_tokens
    # Durable intent: fresh session, seeded from the CURRENT window.
    assert after.force_new_session is True
    assert after.pending_seed_window == after.window_id
    assert context.user_data["force_new_session"] is True


async def test_switch_transition_survives_a_restart(storage, tmp_path):
    """Switch backend, restart (fresh user_data, fresh repo object), next turn
    still gets force_new and the full-window rebuild."""
    workdir = tmp_path / "ws"
    workdir.mkdir()
    settings = create_test_config(approved_directory=str(workdir))
    window_repo = WindowRepository(storage.db_manager)

    before = await window_repo.get_or_create(USER_ID, workdir)
    await _seed_turns(storage.db_manager, workdir, 1, [("old-q", "old-a")])

    orch = MessageOrchestrator(settings, {})
    context = _context(storage, window_repo, workdir, None, backend="claude")
    await orch._handle_model_callback(_query(), context, "model:codex:gpt-5.5")
    await _drain_tasks()
    assert context.user_data["agent_backend"] == "openai_codex"

    # --- restart: in-memory state is gone ---
    fresh_user_data: dict = {}
    after_restart = WindowRepository(storage.db_manager)

    state = await after_restart.get_or_create(USER_ID, workdir)
    assert state.window_id == before.window_id
    assert state.force_new_session is True
    assert state.pending_seed_window == state.window_id

    prompt = await consume_pending_seed(
        after_restart,
        fresh_user_data,
        user_id=USER_ID,
        working_dir=workdir,
        user_message="new-msg",
    )
    assert "rebuilt in full" in prompt
    assert "old-q" in prompt and "old-a" in prompt
    assert prompt.rstrip().endswith("new-msg")


async def test_same_backend_is_a_no_op(storage, tmp_path):
    """Re-picking a tier on the current backend must not arm or extract."""
    workdir = tmp_path / "ws"
    workdir.mkdir()
    settings = create_test_config(approved_directory=str(workdir))
    window_repo = WindowRepository(storage.db_manager)
    evaluator = MagicMock()
    evaluator.run = AsyncMock(return_value=True)

    before = await window_repo.get_or_create(USER_ID, workdir)
    orch = MessageOrchestrator(settings, {})
    context = _context(storage, window_repo, workdir, evaluator, backend="claude")

    await orch._handle_model_callback(_query(), context, "model:claude:opus")
    await _drain_tasks()

    after = await window_repo.get_or_create(USER_ID, workdir)
    assert after.window_id == before.window_id
    assert after.force_new_session is False
    assert after.pending_seed_window is None
    evaluator.run.assert_not_awaited()


async def test_full_window_seed_has_every_turn_and_ignores_tail_cap(storage, tmp_path):
    """More than SEED_TAIL_TURNS turns and more than SEED_MAX_CHARS of text:
    every turn is carried, in order, with no trim note."""
    workdir = tmp_path / "ws"
    workdir.mkdir()
    window_repo = WindowRepository(storage.db_manager)
    await window_repo.get_or_create(USER_ID, workdir)
    n = SEED_TAIL_TURNS + 7
    filler = "x" * (SEED_MAX_CHARS // n + 1_000)
    turns = [(f"question-{i:02d} {filler}", f"answer-{i:02d}") for i in range(n)]
    await _seed_turns(storage.db_manager, workdir, 1, turns)

    from src.bot.orchestration.windowing import arm_backend_switch

    await arm_backend_switch(window_repo, {}, user_id=USER_ID, working_dir=workdir)
    prompt = await consume_pending_seed(
        window_repo, {}, user_id=USER_ID, working_dir=workdir, user_message="go"
    )
    assert len(prompt) > SEED_MAX_CHARS
    positions = [prompt.index(f"question-{i:02d}") for i in range(n)]
    assert positions == sorted(positions)
    assert all(f"answer-{i:02d}" in prompt for i in range(n))
    assert f"all {n} turns of it appear below" in prompt
    assert "earlier turns trimmed" not in prompt
    assert "Your context was rotated" not in prompt


# --- turn-level: what the backend is actually asked to do -------------------


def _mock_response(session_id: str, context_tokens: int):
    r = MagicMock()
    r.session_id = session_id
    r.content = "answer"
    r.tools_used = []
    r.input_tokens = 100
    r.output_tokens = 50
    r.context_tokens = context_tokens
    r.cost = 0.0
    r.error_type = None
    return r


def _turn_harness(settings, repo, run_command):
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
    update.message.text = "and now?"
    update.message.message_id = 1
    update.message.reply_to_message = None
    update.message.chat.type = "private"
    update.message.chat.send_action = AsyncMock()
    update.message.reply_text = AsyncMock()
    update.message.reply_text.return_value = AsyncMock()

    context = MagicMock()
    context.user_data = {}
    context.bot_data = {
        "settings": settings,
        "claude_integration": claude_integration,
        "storage": storage,
        "rate_limiter": None,
        "audit_logger": None,
        "window_repo": repo,
        "evaluator_service": None,
    }
    return update, context


async def test_switch_back_rebuilds_instead_of_resuming_stale_session(tmp_path):
    """claude (session A) -> codex turns -> back to claude: the turn must not
    resume A (explicitly or via the user_data id), it gets a fresh session
    with the whole window; window id stays, the counter takes the rebuilt
    context's real level; the next plain turn resumes the NEW session."""
    settings = create_test_config(approved_directory=str(tmp_path), agentic_mode=True)
    db = DatabaseManager(tempfile.mktemp(suffix=".db"))
    await db.initialize()
    repo = WindowRepository(
        db, eval_interval_tokens=0, refresh_threshold_tokens=150_000
    )
    wd = Path(tmp_path)
    await repo.get_or_create(123, wd)
    turns = [(f"t{i}-q", f"t{i}-a") for i in range(SEED_TAIL_TURNS + 3)]
    await _seed_turns(db, wd, 1, turns, user_id=123)
    await repo.record_turn(
        123, wd, backend="openai_codex", added_tokens=1_000, context_tokens=60_000
    )

    calls = []

    async def run_command(**kw):
        calls.append(kw)
        return _mock_response("fresh-B", context_tokens=58_000 + len(calls))

    orch = MessageOrchestrator(settings, {"claude_integration": MagicMock()})
    update, context = _turn_harness(settings, repo, AsyncMock(side_effect=run_command))
    # Stale claude session from before the codex leg (no profile registry,
    # so the turn path would otherwise pass this id explicitly).
    context.user_data["claude_session_id"] = "stale-A"

    await orch._maybe_roll_on_switch(
        context, _query(123), new_backend="claude", prev_backend="openai_codex"
    )
    await orch.agentic_text(update, context)

    first = calls[0]
    assert first["force_new"] is True
    assert first["session_id"] is None
    assert "rebuilt in full" in first["prompt"]
    assert all(q in first["prompt"] for q, _ in turns)
    assert first["prompt"].rstrip().endswith("and now?")

    state = await repo.get_or_create(123, wd)
    assert state.window_id == 1
    assert state.force_new_session is False
    assert state.pending_seed_window is None
    assert state.last_context_tokens == 58_001  # the rebuilt session's level

    # Next turn: nothing armed, resumes the session the rebuild created.
    await orch.agentic_text(update, context)
    second = calls[1]
    assert second["force_new"] is False
    assert second["session_id"] == "fresh-B"
    assert "<conversation_context>" not in second["prompt"]


async def test_tier_change_resumes_same_session(tmp_path):
    """Opus -> Sonnet on Claude: no rebuild, the live session resumes."""
    settings = create_test_config(approved_directory=str(tmp_path), agentic_mode=True)
    db = DatabaseManager(tempfile.mktemp(suffix=".db"))
    await db.initialize()
    repo = WindowRepository(db)
    wd = Path(tmp_path)
    await repo.get_or_create(123, wd)
    await _seed_turns(db, wd, 1, [("q", "a")], user_id=123)

    calls = []

    async def run_command(**kw):
        calls.append(kw)
        return _mock_response("live-A", context_tokens=30_000)

    orch = MessageOrchestrator(settings, {"claude_integration": MagicMock()})
    update, context = _turn_harness(settings, repo, AsyncMock(side_effect=run_command))
    context.user_data["claude_session_id"] = "live-A"

    await orch._maybe_roll_on_switch(
        context, _query(123), new_backend="claude", prev_backend="claude"
    )
    await orch.agentic_text(update, context)

    assert calls[0]["force_new"] is False
    assert calls[0]["session_id"] == "live-A"
    assert calls[0]["prompt"] == "and now?"


async def test_full_window_seed_lists_tool_names_per_turn(storage, tmp_path):
    """Each turn that called tools gets one compact names-only line after its
    response (deduped with counts, call order); tool-less turns get none."""
    workdir = tmp_path / "ws"
    workdir.mkdir()
    window_repo = WindowRepository(storage.db_manager)
    await window_repo.get_or_create(USER_ID, workdir)
    await _seed_turns(
        storage.db_manager,
        workdir,
        1,
        [("find a pan", "here are three"), ("thanks", "you're welcome")],
    )
    async with storage.db_manager.get_connection() as conn:
        cur = await conn.execute(
            "SELECT message_id FROM messages WHERE prompt='find a pan'"
        )
        first_id = (await cur.fetchone())[0]
        for seq, name in enumerate(
            ["WebSearch", "WebFetch", "WebSearch", "product_find"]
        ):
            await conn.execute(
                "INSERT INTO tool_calls (session_id, message_id, seq, tool, "
                "input, status) VALUES ('s', ?, ?, ?, '{\"secret\": \"input\"}', 'ok')",
                (first_id, seq, name),
            )
        await conn.commit()

    from src.bot.orchestration.windowing import arm_backend_switch

    await arm_backend_switch(window_repo, {}, user_id=USER_ID, working_dir=workdir)
    prompt = await consume_pending_seed(
        window_repo, {}, user_id=USER_ID, working_dir=workdir, user_message="go"
    )
    assert (
        "Assistant: here are three\n[tools: WebSearch×2, WebFetch, product_find]"
        in prompt
    )
    # The tool-less turn ends at its response: no metadata line.
    assert "Assistant: you're welcome\n</recent_conversation>" in prompt
    assert prompt.count("[tools: W") == 1
    assert "secret" not in prompt  # names only, never inputs


def test_tools_line_format():
    from src.bot.window_seed import tools_line

    assert tools_line([]) == ""
    assert tools_line(["a", "b", "a", "a"]) == "[tools: a×3, b]"
