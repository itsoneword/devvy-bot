"""Backend seam, end to end through ``agentic_text`` -> ``_run_turn`` (dv-e748).

Closes the last gaps from the 2026-09-06 backend review that the bug and
Protocol tickets left open:

* a backend switch (``model:`` callback) followed by the next text turn must
  run on the NEW backend with ``force_new`` and the armed seed in the prompt
  — with and without a restart in between (the DB row, not ``user_data``,
  is what the turn reads; dv-a826 / dv-9f31);
* the per-user budget gate in front of ``_run_turn`` (dv-0722): a Codex turn
  that exhausts the unmetered-token ledger blocks the next turn, a Claude
  turn that exhausts the USD cap blocks the next turn, and the block is per
  person — switching backend does not dodge it;
* the ``cd:`` callback (callbacks.py) works with only Codex registered.

Backends are mocks satisfying the AgentBackend surface; storage and the
window repository are real (SQLite in a temp dir) so the seed and the
transition flags go through the same rows the live bot uses.
"""

import asyncio
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.agent_backends.protocol import _CODEX_DEFAULT_MODEL
from src.bot.orchestration.routing import seed_prefs_into_user_data
from src.bot.orchestrator import MessageOrchestrator
from src.config import create_test_config
from src.security.rate_limiter import RateLimiter
from src.storage.facade import Storage
from src.storage.window_repository import WindowRepository

USER_ID = 777
CODEX_MODEL_KEY = _CODEX_DEFAULT_MODEL.split("/", 1)[1]  # always catalog-valid


# --- fixtures / builders ------------------------------------------------------


@pytest.fixture
async def storage():
    with tempfile.TemporaryDirectory() as temp_dir:
        st = Storage(f"sqlite:///{Path(temp_dir) / 'bot.db'}")
        await st.initialize()
        # Rows the turn writes with mock ids: stub the two writers whose
        # inputs are MagicMocks here; everything else (prefs, windows,
        # messages tail, pending notes) is the real repository.
        st.save_claude_interaction = AsyncMock(return_value=1)
        st.sent_messages = MagicMock(record=AsyncMock())
        st.log_bot_event = AsyncMock()
        yield st
        await st.close()


@pytest.fixture(autouse=True)
def _denylist_env(monkeypatch):
    monkeypatch.delenv("APPROVED_DIRECTORY", raising=False)


def _response(
    session_id: str, *, cost, input_tokens: int = 10, output_tokens: int = 5
) -> MagicMock:
    r = MagicMock()
    r.session_id = session_id
    r.content = "answer"
    r.tools_used = []
    r.input_tokens = input_tokens
    r.output_tokens = output_tokens
    r.context_tokens = 1_000
    r.cost = cost
    r.num_turns = 1
    r.error_type = None
    r.session_restarted = False
    return r


def _backend(backend_id: str, response: MagicMock) -> MagicMock:
    b = MagicMock(
        spec=(
            "backend_id",
            "default_model",
            "run_command",
            "resumable_session_for",
            "sessions_for_user",
            "attach_tools",
        )
    )
    b.backend_id = backend_id
    b.run_command = AsyncMock(return_value=response)
    b.resumable_session_for = AsyncMock(return_value=None)
    b.sessions_for_user = AsyncMock(return_value=[])
    return b


def _backends(*, claude_cost=0.01, codex_tokens=(10, 5)):
    claude = _backend("claude", _response("c-1", cost=claude_cost))
    codex = _backend(
        "openai_codex",
        _response(
            "x-1",
            cost=None,
            input_tokens=codex_tokens[0],
            output_tokens=codex_tokens[1],
        ),
    )
    return {"claude": claude, "openai_codex": codex}


def _text_update(text: str = "so did you fix it?") -> MagicMock:
    update = MagicMock()
    update.effective_user.id = USER_ID
    update.message.text = text
    update.message.message_id = 1
    update.message.message_thread_id = None
    update.message.reply_to_message = None
    update.message.chat.id = 999
    update.message.chat.type = "private"
    update.message.chat.send_action = AsyncMock()
    update.message.reply_text = AsyncMock(return_value=AsyncMock())
    return update


def _context(settings, user_data: dict, bot_data: dict) -> MagicMock:
    context = MagicMock()
    context.user_data = user_data
    context.bot_data = {
        "settings": settings,
        "storage": None,
        "rate_limiter": None,
        "audit_logger": None,
        "window_repo": None,
        "evaluator_service": None,
        "profile_registry": None,
        **bot_data,
    }
    return context


def _query() -> MagicMock:
    query = MagicMock()
    query.from_user.id = USER_ID
    query.edit_message_text = AsyncMock()
    return query


def _replies(update: MagicMock) -> list:
    return [c.args[0] for c in update.message.reply_text.call_args_list if c.args]


async def _seed_window(storage: Storage, workdir: Path, window_id: int) -> None:
    """One stored turn in ``window_id`` -- what the seed tail is built from."""
    async with storage.db_manager.get_connection() as conn:
        await conn.execute("INSERT INTO users (user_id) VALUES (?)", (USER_ID,))
        await conn.execute(
            "INSERT INTO sessions (session_id, user_id, project_path) "
            "VALUES ('s', ?, ?)",
            (USER_ID, str(workdir)),
        )
        await conn.execute(
            "INSERT INTO messages (session_id,user_id,prompt,response,window_id) "
            "VALUES ('s', ?, 'old-q', 'old-a', ?)",
            (USER_ID, window_id),
        )
        await conn.commit()


async def _drain_tasks() -> None:
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    if pending:
        await asyncio.gather(*pending)


# --- switch -> turn consumes the seed on the new backend ----------------------


@pytest.mark.parametrize("restart", [False, True], ids=["same-process", "restart"])
async def test_switch_then_turn_runs_new_backend_with_seed_and_force_new(
    storage, tmp_path, restart
):
    workdir = tmp_path / "ws"
    workdir.mkdir()
    settings = create_test_config(approved_directory=str(workdir), agentic_mode=True)
    window_repo = WindowRepository(storage.db_manager)
    before = await window_repo.get_or_create(USER_ID, workdir)
    await _seed_window(storage, workdir, before.window_id)

    backends = _backends()
    bot_data = {"backends": backends, "storage": storage, "window_repo": window_repo}
    orch = MessageOrchestrator(settings, {})
    context = _context(
        settings,
        {
            "current_directory": workdir,
            "agent_backend": "claude",
            "claude_session_id": "c-old",
            "role": "owner",
        },
        bot_data,
    )

    await orch._handle_model_callback(
        _query(), context, f"model:codex:{CODEX_MODEL_KEY}"
    )
    await _drain_tasks()

    if restart:
        # A restart is an empty user_data; the auth middleware re-seeds only
        # the persisted prefs row. current_directory falls back to the
        # configured approved_directory (== workdir here).
        fresh: dict = {"role": "owner"}
        seed_prefs_into_user_data(fresh, await storage.preferences.get(USER_ID))
        assert "force_new_session" not in fresh and "pending_seed_window" not in fresh
        context = _context(settings, fresh, bot_data)
    assert context.user_data["agent_backend"] == "openai_codex"

    update = _text_update("so did you fix it?")
    await orch.agentic_text(update, context)

    claude, codex = backends["claude"], backends["openai_codex"]
    claude.run_command.assert_not_awaited()
    codex.run_command.assert_awaited_once()
    kwargs = codex.run_command.await_args.kwargs
    assert kwargs["force_new"] is True
    assert kwargs["session_id"] is None  # never the retired Claude session
    assert kwargs["model"] == _CODEX_DEFAULT_MODEL
    assert Path(kwargs["working_directory"]) == workdir
    # The armed seed: the current window, rebuilt in full (dv-168e: a
    # backend switch no longer rolls), precedes the user's message.
    prompt = kwargs["prompt"]
    assert "rebuilt in full" in prompt
    assert "old-q" in prompt and "old-a" in prompt
    assert prompt.rstrip().endswith("so did you fix it?")
    assert prompt.index("old-q") < prompt.index("so did you fix it?")

    # The new backend's session is what user_data now holds ...
    assert context.user_data["codex_session_id"] == "x-1"
    assert context.user_data.get("force_new_session") is False
    # ... and the transition is consumed in the DB, so the NEXT turn resumes.
    after = await window_repo.get_or_create(USER_ID, workdir)
    assert after.window_id == before.window_id
    assert after.force_new_session is False
    assert after.pending_seed_window is None

    codex.run_command.reset_mock()
    await orch.agentic_text(_text_update("and then?"), context)
    kwargs = codex.run_command.await_args.kwargs
    assert kwargs["force_new"] is False
    assert kwargs["session_id"] == "x-1"
    assert kwargs["prompt"] == "and then?"


# --- budget gates in front of _run_turn ---------------------------------------


def _budget_settings(tmp_path, **overrides):
    base = dict(
        approved_directory=str(tmp_path),
        agentic_mode=True,
        rate_limit_requests=1_000,
        rate_limit_burst=1_000,
        claude_max_cost_per_user_guest=1.0,
        unmetered_max_tokens_per_user_guest=100,
    )
    base.update(overrides)
    return create_test_config(**base)


async def test_token_ledger_gate_blocks_the_next_codex_turn_when_exhausted(tmp_path):
    settings = _budget_settings(tmp_path)
    limiter = RateLimiter(settings)
    # One Codex turn worth exactly the guest token cap (60 + 40 = 100).
    backends = _backends(codex_tokens=(60, 40))
    context = _context(
        settings,
        {
            "agent_backend": "openai_codex",
            "role": "guest",
            "current_directory": tmp_path,
        },
        {"backends": backends, "rate_limiter": limiter},
    )
    orch = MessageOrchestrator(settings, {})

    first = _text_update("first")
    await orch.agentic_text(first, context)
    codex = backends["openai_codex"]
    codex.run_command.assert_awaited_once()
    assert limiter.token_tracker[USER_ID] == 100
    assert not any(t.startswith("⏱️") for t in _replies(first))

    second = _text_update("second")
    await orch.agentic_text(second, context)
    codex.run_command.assert_awaited_once()  # still one: the gate held
    blocked = [t for t in _replies(second) if t.startswith("⏱️")]
    assert len(blocked) == 1
    assert "Token budget exceeded" in blocked[0]
    assert "100/100" in blocked[0]

    # The budget is per person, not per backend: switching to Claude does
    # not buy a fresh allowance.
    context.user_data["agent_backend"] = "claude"
    third = _text_update("third")
    await orch.agentic_text(third, context)
    backends["claude"].run_command.assert_not_awaited()
    assert any("Token budget exceeded" in t for t in _replies(third))


async def test_usd_gate_blocks_the_next_claude_turn_when_exhausted(tmp_path):
    settings = _budget_settings(tmp_path)
    limiter = RateLimiter(settings)
    # One Claude turn that spends the whole guest USD cap.
    backends = _backends(claude_cost=1.0)
    context = _context(
        settings,
        {"agent_backend": "claude", "role": "guest", "current_directory": tmp_path},
        {"backends": backends, "rate_limiter": limiter},
    )
    orch = MessageOrchestrator(settings, {})

    first = _text_update("first")
    await orch.agentic_text(first, context)
    claude = backends["claude"]
    claude.run_command.assert_awaited_once()
    # Real cost trued up after the turn (plus the ~$0.001 pre-check estimate).
    assert limiter.cost_tracker[USER_ID] == pytest.approx(1.001)
    assert limiter.token_tracker[USER_ID] == 0  # priced turn: no token entry

    second = _text_update("second")
    await orch.agentic_text(second, context)
    claude.run_command.assert_awaited_once()
    blocked = [t for t in _replies(second) if t.startswith("⏱️")]
    assert len(blocked) == 1
    assert "Cost limit exceeded" in blocked[0]

    # Same person, other backend: still blocked.
    context.user_data["agent_backend"] = "openai_codex"
    third = _text_update("third")
    await orch.agentic_text(third, context)
    backends["openai_codex"].run_command.assert_not_awaited()
    assert any("Cost limit exceeded" in t for t in _replies(third))


async def test_owner_is_exempt_from_both_gates_by_default(tmp_path):
    settings = _budget_settings(
        tmp_path, claude_max_cost_per_user=1.0, unmetered_max_tokens_per_user=100
    )
    limiter = RateLimiter(settings)
    await limiter.record_tokens(USER_ID, 100)
    await limiter.record_cost(USER_ID, 1.0)
    backends = _backends()
    context = _context(
        settings,
        {
            "agent_backend": "openai_codex",
            "role": "owner",
            "current_directory": tmp_path,
        },
        {"backends": backends, "rate_limiter": limiter},
    )
    orch = MessageOrchestrator(settings, {})

    update = _text_update("still allowed")
    await orch.agentic_text(update, context)

    backends["openai_codex"].run_command.assert_awaited_once()
    assert not any(t.startswith("⏱️") for t in _replies(update))


# --- cd: callback with only Codex registered ----------------------------------


async def test_cd_callback_resumes_the_codex_session_with_only_codex_registered(
    tmp_path,
):
    (tmp_path / "proj").mkdir()
    settings = create_test_config(approved_directory=str(tmp_path), agentic_mode=True)
    codex = _backend("openai_codex", _response("x-1", cost=None))
    codex.resumable_session_for = AsyncMock(return_value="x-resume")
    context = _context(
        settings,
        {"agent_backend": "openai_codex", "codex_session_id": "stale-x"},
        {"backends": {"openai_codex": codex}},
    )
    update = MagicMock()
    update.callback_query.data = "cd:proj"
    update.callback_query.from_user.id = USER_ID
    update.callback_query.answer = AsyncMock()
    update.callback_query.edit_message_text = AsyncMock()
    orch = MessageOrchestrator(settings, {})

    await orch._agentic_callback(update, context)

    codex.resumable_session_for.assert_awaited_once_with(USER_ID, tmp_path / "proj")
    assert context.user_data["current_directory"] == tmp_path / "proj"
    assert context.user_data["codex_session_id"] == "x-resume"
    assert "claude_session_id" not in context.user_data
    text = update.callback_query.edit_message_text.call_args.args[0]
    assert "Switched to <code>proj/</code>" in text
    assert "session resumed" in text
