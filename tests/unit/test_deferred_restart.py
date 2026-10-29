"""Deferred restart from an agent turn (dv-da50).

A turn that ran ``sudo systemctl restart claude-bot`` killed itself before
``_store_interaction``: no messages row, ``model_calls.outcome=error``, and the
next turn did not know it had happened. The fix is one restart executor in
``src/bot/orchestration/commands/system.py`` with two entry points:

* ``restart_now(reason, user_id)`` — the SIGTERM ``/restart`` already sends;
* ``await request_restart(turn_locks, reason=..., user_id=...) -> str`` —
  schedules ONE restart that fires (via ``restart_now``) only once no
  ``TurnLocks`` lock is held, or after ``RESTART_MAX_WAIT_S`` (600 s) anyway.
  A second request while one is pending answers "already scheduled".
  Tunables are module constants: ``RESTART_MAX_WAIT_S``, ``RESTART_POLL_S``,
  ``RESTART_GRACE_S`` (tests shrink them).

The agent reaches it through the ``restart_bot`` tool (``agent_tools``), which
calls ``AgentServices.request_restart(reason, ctx)``; ``main.py`` wires that
as a closure over the process's one ``TurnLocks``. These tests wire it the
same way.

Turn tests run the real ``MessageOrchestrator.agentic_text`` with a real
SQLite ``Storage`` and a recording ``ModelRouter``; only the backend is a
mock. ``os.kill`` is replaced, so nothing is signalled.
"""

import asyncio
import sqlite3
import tempfile
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock

import pytest
from structlog.testing import capture_logs

from src.bot.orchestration.commands import system
from src.bot.orchestrator import MessageOrchestrator
from src.claude import agent_tools
from src.claude.sdk_integration import ClaudeResponse
from src.claude.tool_context import AgentServices, TurnContext
from src.config import create_test_config
from src.model_router import ModelRouter
from src.model_router.harness import HarnessVersions
from src.storage.facade import Storage
from src.storage.model_calls import ModelCallRepository
from src.storage.window_repository import WindowRepository
from src.utils.turn_locks import TurnLocks

USER_ID = 777
CHAT_ID = 999
SESSION_ID = "sess-restart-1"
REPLY = "PUMP_MAX_CONCURRENT is now 5; restarting the bot to apply it."


# --- fixtures -----------------------------------------------------------------


class _Versions(HarnessVersions):
    async def get(self, harness: str) -> str:
        return f"{harness}-9.9"


@pytest.fixture(autouse=True)
def _denylist_env(monkeypatch):
    monkeypatch.delenv("APPROVED_DIRECTORY", raising=False)


@pytest.fixture
def fast_restart(monkeypatch):
    """Shrink the drain timings so a test runs in well under a second."""
    monkeypatch.setattr(system, "RESTART_POLL_S", 0.02, raising=False)
    monkeypatch.setattr(system, "RESTART_GRACE_S", 0.05, raising=False)
    monkeypatch.setattr(system, "RESTART_MAX_WAIT_S", 5.0, raising=False)


@pytest.fixture
def kills(monkeypatch) -> List[Dict[str, Any]]:
    """Replace os.kill; each call is recorded (with an optional snapshot)."""
    calls: List[Dict[str, Any]] = []
    _SNAPSHOT_HOOKS.clear()

    def fake_kill(pid: int, sig: int) -> None:
        entry: Dict[str, Any] = {"pid": pid, "sig": sig}
        for hook in _SNAPSHOT_HOOKS:
            entry.update(hook())
        calls.append(entry)

    monkeypatch.setattr(system.os, "kill", fake_kill)
    yield calls
    _SNAPSHOT_HOOKS.clear()


# Tests that need the world as it was at kill time register a hook here.
_SNAPSHOT_HOOKS: List[Any] = []


async def _wait_for(predicate: Any, timeout: float = 3.0) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return predicate()


@pytest.fixture
async def env():
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "bot.db"
        st = Storage(f"sqlite:///{db_path}")
        await st.initialize()
        st.sent_messages = MagicMock(record=AsyncMock())
        st.log_bot_event = AsyncMock()
        workdir = Path(temp_dir) / "devops"
        workdir.mkdir()
        # The devops conversation already exists (an earlier turn made it).
        async with st.db_manager.get_connection() as conn:
            await conn.execute("INSERT INTO users (user_id) VALUES (?)", (USER_ID,))
            await conn.execute(
                "INSERT INTO sessions (session_id, user_id, project_path) "
                "VALUES (?, ?, ?)",
                (SESSION_ID, USER_ID, str(workdir)),
            )
            await conn.commit()
        yield {"storage": st, "db_path": db_path, "workdir": workdir}
        await st.close()


def _rows(db_path: Path, sql: str) -> List[dict]:
    """Synchronous read — usable from inside the fake os.kill."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql).fetchall()]
    finally:
        conn.close()


def _response(content: str = REPLY) -> ClaudeResponse:
    return ClaudeResponse(
        content=content,
        session_id=SESSION_ID,
        cost=0.01,
        duration_ms=10,
        num_turns=1,
        input_tokens=10,
        output_tokens=5,
    )


def _backend(run: Any, storage: Storage) -> MagicMock:
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
    b.backend_id = "claude"
    b.default_model.return_value = "sonnet"
    b.run_command = AsyncMock(side_effect=run)

    async def _resumable(user_id: int, working_directory: Path) -> Any:
        sessions = await storage.sessions.get_sessions_by_project(
            str(working_directory)
        )
        mine = [s for s in sessions if s.user_id == user_id and s.session_id]
        return max(mine, key=lambda s: s.last_used).session_id if mine else None

    b.resumable_session_for = AsyncMock(side_effect=_resumable)
    b.sessions_for_user = AsyncMock(return_value=[])
    return b


def _text_update(text: str) -> MagicMock:
    update = MagicMock()
    update.effective_user.id = USER_ID
    update.message.text = text
    update.message.message_id = 1
    update.message.message_thread_id = None
    update.message.reply_to_message = None
    update.message.chat.id = CHAT_ID
    update.message.chat_id = CHAT_ID
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


def _sent_texts(update: MagicMock) -> List[str]:
    return [str(c.args[0]) for c in update.message.reply_text.call_args_list if c.args]


def _owner_ctx(workdir: Path) -> TurnContext:
    return TurnContext(
        profile_slug="devops",
        working_dir=workdir,
        requester_chat_id=CHAT_ID,
        user_id=USER_ID,
        role="owner",
    )


def _services(storage: Storage, turn_locks: TurnLocks) -> AgentServices:
    """What main.py wires: request_restart is a closure over the one TurnLocks."""

    async def _request(reason: str, ctx: TurnContext) -> str:
        return await system.request_restart(
            turn_locks, reason=reason, user_id=ctx.user_id
        )

    return AgentServices(
        event_bus=MagicMock(publish=AsyncMock()),
        task_repo=AsyncMock(),
        message_repo=storage.messages,
        request_restart=_request,
    )


async def _call_tool(
    services: AgentServices, ctx: TurnContext, name: str, args: Dict[str, Any]
) -> Dict[str, Any]:
    """Call a tool the way either transport does: only if mounted for ctx."""
    specs = {s.name: s for s in agent_tools.available_tool_specs(services, ctx)}
    assert name in specs, f"{name} is not offered to this turn"
    return await specs[name].handler(services, ctx, args)


# --- scenario 1: the restarting turn is saved before the restart --------------


async def test_turn_calling_restart_bot_is_saved_before_restart(
    env, fast_restart, kills
):
    """Reply sent, messages row saved, model_calls ok with a session id —
    and only then the SIGTERM."""
    storage, db_path, workdir = env["storage"], env["db_path"], env["workdir"]
    turn_locks = TurnLocks()
    services = _services(storage, turn_locks)
    update = _text_update("raise the pump concurrency to 5 and restart")
    seen_inside_turn: Dict[str, Any] = {}

    async def run(**kwargs: Any) -> ClaudeResponse:
        result = await _call_tool(
            services, _owner_ctx(workdir), "restart_bot", {"reason": "apply config"}
        )
        seen_inside_turn["tool"] = result
        # The turn keeps going after the tool returns; the restart must wait.
        await asyncio.sleep(0.3)
        seen_inside_turn["kills"] = len(kills)
        return _response()

    _SNAPSHOT_HOOKS.append(
        lambda: {
            "sent": _sent_texts(update),
            "messages": _rows(db_path, "SELECT * FROM messages"),
            "model_calls": _rows(db_path, "SELECT * FROM model_calls"),
        }
    )

    settings = create_test_config(approved_directory=str(workdir), agentic_mode=True)
    backends = {"claude": _backend(run, storage)}
    router = ModelRouter(
        backends=backends,
        recorder=ModelCallRepository(storage.db_manager),
        versions=_Versions(),
    )
    bot_data = {
        "backends": backends,
        "storage": storage,
        "window_repo": WindowRepository(storage.db_manager),
        "model_router": router,
    }
    orch = MessageOrchestrator(settings, {"turn_locks": turn_locks})
    context = _context(
        settings,
        {
            "current_directory": workdir,
            "agent_backend": "claude",
            "claude_session_id": SESSION_ID,
            "role": "owner",
        },
        bot_data,
    )

    await orch.agentic_text(update, context)

    tool_text = seen_inside_turn["tool"]["content"][0]["text"]
    assert seen_inside_turn["tool"].get("isError") is not True, tool_text
    assert "scheduled" in tool_text
    assert seen_inside_turn["kills"] == 0, "restart fired while the turn was running"

    assert await _wait_for(lambda: len(kills) == 1), "restart never fired"
    import os
    import signal

    kill = kills[0]
    assert (kill["pid"], kill["sig"]) == (os.getpid(), signal.SIGTERM)
    # At the moment of the kill the turn was already fully persisted.
    assert any(REPLY[:20] in t for t in kill["sent"]), kill["sent"]
    saved = [m for m in kill["messages"] if m["response"] == REPLY]
    assert len(saved) == 1 and saved[0]["session_id"] == SESSION_ID
    (call,) = kill["model_calls"]
    assert call["outcome"] == "ok"
    assert call["session_id"] == SESSION_ID


# --- scenario 2: after the restart the next turn sees the restarting turn -----


async def test_next_turn_after_restart_resumes_session_and_recalls_it(
    env, fast_restart, kills
):
    storage, workdir = env["storage"], env["workdir"]
    turn_locks = TurnLocks()
    services = _services(storage, turn_locks)
    settings = create_test_config(approved_directory=str(workdir), agentic_mode=True)
    prompts: List[Dict[str, Any]] = []

    async def run(**kwargs: Any) -> ClaudeResponse:
        prompts.append(kwargs)
        if len(prompts) == 1:
            await _call_tool(
                services,
                _owner_ctx(workdir),
                "restart_bot",
                {"reason": "apply config"},
            )
            return _response()
        return _response("Yes — I raised it myself in the previous turn.")

    def _turn_env(user_data: dict, locks: TurnLocks) -> tuple:
        backends = {"claude": _backend(run, storage)}
        bot_data = {
            "backends": backends,
            "storage": storage,
            "window_repo": WindowRepository(storage.db_manager),
            "model_router": ModelRouter(
                backends=backends,
                recorder=ModelCallRepository(storage.db_manager),
                versions=_Versions(),
            ),
        }
        orch = MessageOrchestrator(settings, {"turn_locks": locks})
        return orch, _context(settings, user_data, bot_data)

    orch, context = _turn_env(
        {
            "current_directory": workdir,
            "agent_backend": "claude",
            "claude_session_id": SESSION_ID,
            "role": "owner",
        },
        turn_locks,
    )
    await orch.agentic_text(_text_update("raise concurrency and restart"), context)
    assert await _wait_for(lambda: len(kills) == 1), "restart never fired"

    # "Restart": a new process — fresh orchestrator, fresh locks, empty
    # user_data except what the auth middleware seeds.
    orch2, context2 = _turn_env(
        {"current_directory": workdir, "agent_backend": "claude", "role": "owner"},
        TurnLocks(),
    )
    await orch2._refresh_resumable_sessions(context2, USER_ID, workdir)
    await orch2.agentic_text(
        _text_update("did you raise PUMP_MAX_CONCURRENT?"), context2
    )

    assert len(prompts) == 2
    assert prompts[1].get("session_id") == SESSION_ID
    assert context2.user_data.get("claude_session_id") == SESSION_ID

    recall_services = AgentServices(
        event_bus=MagicMock(publish=AsyncMock()),
        task_repo=AsyncMock(),
        message_repo=storage.messages,
    )
    result = await _call_tool(
        recall_services,
        _owner_ctx(workdir),
        "recall_conversation",
        {"query": "PUMP_MAX_CONCURRENT restarting"},
    )
    assert result.get("isError") is not True
    assert "restarting the bot" in result["content"][0]["text"]


# --- scenario 3: other agents' in-flight turns; the 10-minute cap -------------


async def test_restart_waits_for_another_agents_turn(fast_restart, kills):
    turn_locks = TurnLocks()
    devops_done = asyncio.Event()
    chef_done = asyncio.Event()

    async def turn(profile: str, label: str, done: asyncio.Event) -> None:
        async with turn_locks.hold(USER_ID, label, profile):
            await done.wait()

    devops = asyncio.create_task(turn("devops", "devops turn", devops_done))
    chef = asyncio.create_task(turn("chef", "chef turn", chef_done))
    await asyncio.sleep(0.05)
    assert sorted(turn_locks.busy()) == ["chef turn", "devops turn"]

    status = await system.request_restart(
        turn_locks, reason="apply config", user_id=USER_ID
    )
    assert "scheduled" in status

    devops_done.set()  # the requester's own turn ends
    await devops
    await asyncio.sleep(0.3)
    assert kills == [], "restart cut another agent's turn"

    chef_done.set()
    await chef
    assert await _wait_for(lambda: len(kills) == 1), "restart never fired"


async def test_restart_fires_at_cap_when_a_turn_never_ends(monkeypatch, kills):
    monkeypatch.setattr(system, "RESTART_POLL_S", 0.02, raising=False)
    monkeypatch.setattr(system, "RESTART_GRACE_S", 0.0, raising=False)
    monkeypatch.setattr(system, "RESTART_MAX_WAIT_S", 0.3, raising=False)
    turn_locks = TurnLocks()
    stuck = asyncio.Event()

    async def turn() -> None:
        async with turn_locks.hold(USER_ID, "coach stuck turn", "coach"):
            await stuck.wait()

    task = asyncio.create_task(turn())
    await asyncio.sleep(0.05)
    try:
        with capture_logs() as logs:
            await system.request_restart(turn_locks, reason="x", user_id=USER_ID)
            assert await _wait_for(lambda: len(kills) == 1, timeout=3.0)
        assert any("coach stuck turn" in str(entry) for entry in logs), logs
    finally:
        stuck.set()
        await task


def test_restart_cap_is_ten_minutes():
    assert system.RESTART_MAX_WAIT_S == 600


# --- added: one pending restart, not two ---------------------------------------


async def test_second_request_while_pending_is_already_scheduled(fast_restart, kills):
    turn_locks = TurnLocks()
    release = asyncio.Event()

    async def turn() -> None:
        async with turn_locks.hold(USER_ID, "devops turn", "devops"):
            await release.wait()

    task = asyncio.create_task(turn())
    await asyncio.sleep(0.05)
    first = await system.request_restart(turn_locks, reason="a", user_id=USER_ID)
    second = await system.request_restart(turn_locks, reason="b", user_id=USER_ID)
    assert "already scheduled" not in first
    assert "already scheduled" in second

    release.set()
    await task
    assert await _wait_for(lambda: len(kills) >= 1)
    await asyncio.sleep(0.2)
    assert len(kills) == 1, "two restarts fired for one pending request"
