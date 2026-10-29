"""/limits as the owner's outage panel (dv-7ad9 phase 3). dv-f6af phase 2
cut it to spend and limits: no buttons; the freeze / chat-switch handlers
(``lim:*``) remain for old messages and the Pump screen, pressed here by
their callback data.

When Claude's subscription limit runs out the agent cannot answer anything,
so the owner's controls must work without a single model call:

* ``/limits`` shows the Claude state from the shared limit record
  (``quota.active()``, ``DataLayout.model_quota``) — "Claude: ... until HH:MM"
  — above the existing Codex limits block;
* its inline buttons freeze / thaw the Pump (``scripts/taskpump freeze|thaw
  --by owner`` as a subprocess, no agent turn) and switch this chat to Codex,
  after which the next message is answered by Codex;
* a chat / scheduled Claude turn that ends on the limit text is an error
  (model_calls ``outcome=error``), not an ``ok`` turn with 0 tokens, and it
  writes the shared limit record.

Buttons are found by what they say, then pressed through the
CallbackQueryHandler the orchestrator really registers (so a prefix missing
from the handler pattern fails here). The Pump's freeze state is read from
``DataLayout.taskpump_state`` under ``DEVVY_DATA_ROOT`` (a tmp dir here).
Subprocesses are faked: the test records argv, nothing real runs.
"""

import asyncio
import json
import re
import subprocess
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, List, Tuple
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from claude_agent_sdk import ResultMessage
from telegram.ext import CallbackQueryHandler

from src.bot.orchestrator import MessageOrchestrator
from src.config import create_test_config
from src.storage.facade import Storage
from src.storage.window_repository import WindowRepository

USER_ID = 777
LIMIT_TEXT = "You've hit your weekly limit · resets 9am (UTC)"

FREEZE_WORDS = ("freeze", "замороз", "пауз", "pause", "⏸", "стоп")
THAW_WORDS = ("thaw", "unfreeze", "размороз", "▶", "resume", "продолж", "возобнов")


# --- fixtures -----------------------------------------------------------------


@pytest.fixture(autouse=True)
def root(tmp_path, monkeypatch):
    """Every path (limit record, pump state) under a tmp data root."""
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("DEVVY_DATA_ROOT", str(data))
    monkeypatch.delenv("APPROVED_DIRECTORY", raising=False)
    return data


@pytest.fixture
async def storage():
    with tempfile.TemporaryDirectory() as temp_dir:
        st = Storage(f"sqlite:///{Path(temp_dir) / 'bot.db'}")
        await st.initialize()
        st.save_claude_interaction = AsyncMock(return_value=1)
        st.sent_messages = MagicMock(record=AsyncMock())
        st.log_bot_event = AsyncMock()
        yield st
        await st.close()


class _Spawned:
    """Fake subprocess layer: records every argv, runs nothing."""

    def __init__(self) -> None:
        self.calls: List[Tuple[str, ...]] = []

    def _proc(self) -> MagicMock:
        proc = MagicMock()
        proc.returncode = 0
        proc.pid = 4242
        proc.communicate = AsyncMock(return_value=(b"ok", b""))
        proc.wait = AsyncMock(return_value=0)
        return proc

    async def exec_(self, *argv: Any, **_kw: Any) -> MagicMock:
        self.calls.append(tuple(str(a) for a in argv))
        return self._proc()

    async def shell(self, cmd: Any, **_kw: Any) -> MagicMock:
        self.calls.append(tuple(str(cmd).split()))
        return self._proc()

    def run(self, argv: Any, *_a: Any, **_kw: Any) -> subprocess.CompletedProcess:
        args = tuple(str(argv).split()) if isinstance(argv, str) else argv
        self.calls.append(tuple(str(a) for a in args))
        return subprocess.CompletedProcess(args, 0, stdout="ok", stderr="")

    def check_output(self, argv: Any, *_a: Any, **kw: Any) -> Any:
        self.run(argv)
        return "ok" if kw.get("text") or kw.get("encoding") else b"ok"

    def taskpump(self, action: str) -> List[Tuple[str, ...]]:
        return [
            c
            for c in self.calls
            if any(Path(a).name == "taskpump" for a in c) and action in c
        ]


@pytest.fixture
def spawned(monkeypatch) -> _Spawned:
    fake = _Spawned()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake.exec_)
    monkeypatch.setattr(asyncio, "create_subprocess_shell", fake.shell)
    monkeypatch.setattr(subprocess, "run", fake.run)
    monkeypatch.setattr(subprocess, "check_output", fake.check_output)
    return fake


# --- builders -----------------------------------------------------------------


def _response(session_id: str, content: str = "answer") -> MagicMock:
    r = MagicMock()
    r.session_id = session_id
    r.content = content
    r.tools_used = []
    r.input_tokens = 10
    r.output_tokens = 5
    r.context_tokens = 1_000
    r.cost = None
    r.num_turns = 1
    r.error_type = None
    r.is_error = False
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
            "read_account_limits",
        )
    )
    b.backend_id = backend_id
    b.run_command = AsyncMock(return_value=response)
    b.resumable_session_for = AsyncMock(return_value=None)
    b.sessions_for_user = AsyncMock(return_value=[])
    return b


def _codex_snapshot() -> dict:
    return {
        "rate_limits": {
            "rateLimitsByLimitId": {
                "codex": {"planType": "plus", "primary": {"usedPercent": 12}}
            }
        },
        "usage": {"summary": {"lifetimeTokens": 123}},
    }


def _backends(codex_limits: Any = None) -> dict:
    claude = _backend("claude", _response("c-1"))
    # Claude is out: any model call on it would be a bug on this path.
    claude.read_account_limits = AsyncMock(side_effect=AssertionError("no LLM"))
    codex = _backend("openai_codex", _response("x-1", "codex answer"))
    if isinstance(codex_limits, BaseException):
        codex.read_account_limits = AsyncMock(side_effect=codex_limits)
    else:
        codex.read_account_limits = AsyncMock(
            return_value=codex_limits or _codex_snapshot()
        )
    return {"claude": claude, "openai_codex": codex}


def _settings(workdir: Path):
    return create_test_config(approved_directory=str(workdir), agentic_mode=True)


def _context(settings, bot_data: dict, role: str = "owner", **user: Any) -> MagicMock:
    context = MagicMock()
    context.user_data = {
        "role": role,
        "agent_backend": "claude",
        "_display_name_checked": True,
        "_prefs_loaded": True,
        **user,
    }
    auth = MagicMock()
    auth.get_user_role = MagicMock(return_value=role)
    context.bot_data = {
        "settings": settings,
        "storage": None,
        "rate_limiter": None,
        "audit_logger": None,
        "window_repo": None,
        "evaluator_service": None,
        "profile_registry": None,
        "auth_manager": auth,
        **bot_data,
    }
    return context


def _command_update() -> MagicMock:
    update = MagicMock()
    update.effective_user.id = USER_ID
    update.message.text = "/limits"
    update.message.message_id = 1
    update.message.message_thread_id = None
    update.message.chat.id = 999
    update.message.chat.type = "private"
    update.message.reply_text = AsyncMock(return_value=MagicMock())
    update.effective_message = update.message
    return update


def _text_update(text: str) -> MagicMock:
    update = MagicMock()
    update.effective_user.id = USER_ID
    update.message.text = text
    update.message.message_id = 2
    update.message.message_thread_id = None
    update.message.reply_to_message = None
    update.message.chat.id = 999
    update.message.chat.type = "private"
    update.message.chat.send_action = AsyncMock()
    update.message.reply_text = AsyncMock(return_value=AsyncMock())
    return update


def _sent(update: MagicMock) -> Tuple[str, List[Any]]:
    """All text /limits replied with, and every inline button it offered."""
    texts, buttons = [], []
    for call in update.message.reply_text.call_args_list:
        text = call.args[0] if call.args else call.kwargs.get("text", "")
        texts.append(str(text))
        markup = call.kwargs.get("reply_markup")
        for row in getattr(markup, "inline_keyboard", None) or []:
            buttons.extend(row)
    return "\n".join(texts), buttons


def _is_thaw(button: Any) -> bool:
    return any(w in button.text.lower() for w in THAW_WORDS)


def _is_freeze(button: Any) -> bool:
    return not _is_thaw(button) and any(w in button.text.lower() for w in FREEZE_WORDS)


def _is_codex(button: Any) -> bool:
    return "codex" in button.text.lower() and not (
        _is_thaw(button) or _is_freeze(button)
    )


def _find(buttons: List[Any], pick) -> Any:
    found = [b for b in buttons if pick(b)]
    assert found, [(b.text, b.callback_data) for b in buttons]
    return found[0]


async def _press(orch: MessageOrchestrator, context: MagicMock, data: str) -> MagicMock:
    """Press a button through the CallbackQueryHandler the bot registers."""
    app = MagicMock()
    app.add_handler = MagicMock()
    orch.register_handlers(app)
    handlers = [
        c.args[0]
        for c in app.add_handler.call_args_list
        if c.args and isinstance(c.args[0], CallbackQueryHandler)
    ]
    matching = [h for h in handlers if h.pattern is None or h.pattern.match(data)]
    assert matching, f"no registered callback handler takes {data!r}"
    query = MagicMock()
    query.data = data
    query.from_user.id = USER_ID
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    query.edit_message_reply_markup = AsyncMock()
    query.message.reply_text = AsyncMock()
    query.message.chat.id = 999
    query.message.message_thread_id = None
    update = MagicMock()
    update.callback_query = query
    update.effective_user.id = USER_ID
    update.effective_chat.id = 999
    update.effective_message = query.message
    await matching[0].callback(update, context)
    return query


def _record_limit(minutes_ahead: int = 125) -> datetime:
    """A Claude limit recorded by an earlier job, resetting in the future."""
    from src.model_router import quota

    until = (datetime.now(UTC) + timedelta(minutes=minutes_ahead)).replace(
        second=0, microsecond=0
    )
    quota.record(
        quota.Limit(kind="weekly limit", until=until, message=LIMIT_TEXT),
        "bgjob:test",
    )
    assert quota.active() is not None
    return until


def _freeze_pump_state(by: str = "owner") -> None:
    from src.utils.paths import data_layout

    path = data_layout().taskpump_state
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"frozen": {"at": "2026-10-26T08:20:00Z", "by": by}}),
        encoding="utf-8",
    )


async def _drain_tasks() -> None:
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


# --- scenario 1: the panel -----------------------------------------------------


async def test_limits_with_claude_out_shows_until_and_codex_limits_without_an_llm(
    tmp_path, spawned
):
    """Scenario: /limits with Claude out shows "Claude: until HH:MM", no LLM;
    the Codex block is hidden while Codex was not used this week."""
    until = _record_limit()
    settings = _settings(tmp_path)
    backends = _backends()
    orch = MessageOrchestrator(settings, {})
    context = _context(settings, {"backends": backends})
    update = _command_update()

    await orch.agentic_limits(update, context)

    text, buttons = _sent(update)
    claude_lines = [ln for ln in text.splitlines() if "Claude" in ln]
    assert claude_lines, text
    assert any(until.strftime("%H:%M") in ln for ln in claude_lines), text
    assert "Plan: plus" not in text
    backends["openai_codex"].read_account_limits.assert_not_awaited()
    # No model call anywhere on this path.
    for backend in backends.values():
        backend.run_command.assert_not_awaited()
    assert not [c for c in spawned.calls if any("claude" == Path(a).name for a in c)]
    assert buttons == []  # spend and limits only


async def test_limits_without_a_limit_record_shows_no_until(tmp_path, spawned):
    """Scenario 1, existing-data case: no limit hit recorded (or an expired
    one) — the Claude line says so and carries no reset time."""
    from src.model_router import quota

    quota.record(
        quota.Limit(
            kind="weekly limit",
            until=datetime.now(UTC) + timedelta(seconds=1),
            message=LIMIT_TEXT,
        ),
        "bgjob:old",
    )
    await asyncio.sleep(1.1)
    assert quota.active() is None
    settings = _settings(tmp_path)
    backends = _backends()
    orch = MessageOrchestrator(settings, {})
    update = _command_update()

    await orch.agentic_limits(update, _context(settings, {"backends": backends}))

    text, _ = _sent(update)
    claude_lines = [ln for ln in text.splitlines() if "Claude" in ln]
    assert claude_lines, text
    assert not any(re.search(r"\b\d{1,2}:\d{2}\b", ln) for ln in claude_lines), text


async def test_limits_still_shows_claude_when_codex_query_fails(
    storage, tmp_path, spawned
):
    """added: the Codex account query failing must not hide the Claude state
    (the panel is for exactly this moment); Codex was used this week."""
    from src.agent_backends.codex_usage import CodexUsageError

    until = _record_limit()
    async with storage.db_manager.get_connection() as conn:
        await conn.execute(
            "INSERT INTO model_calls (ts, purpose, kind, harness, provider, model,"
            " outcome) VALUES (datetime('now'), 'chat', 'turn', 'openai_codex',"
            " 'openai', 'm', 'ok')"
        )
        await conn.commit()
    settings = _settings(tmp_path)
    backends = _backends(codex_limits=CodexUsageError("app-server gone"))
    orch = MessageOrchestrator(settings, {})
    update = _command_update()

    await orch.agentic_limits(
        update, _context(settings, {"backends": backends, "storage": storage})
    )

    text, _ = _sent(update)
    assert any(
        "Claude" in ln and until.strftime("%H:%M") in ln for ln in text.splitlines()
    ), text
    assert "Codex" in text


# --- scenario 2: freeze / thaw buttons -----------------------------------------


async def test_freeze_button_runs_taskpump_freeze_by_owner_and_thaw_button_thaws(
    tmp_path, spawned
):
    """Scenario: a button freezes the Pump (`taskpump freeze --by owner`); the
    other button thaws it. Both run with Claude exhausted, no agent turn."""
    _record_limit()
    settings = _settings(tmp_path)
    backends = _backends()
    orch = MessageOrchestrator(settings, {})
    context = _context(settings, {"backends": backends})

    await _press(orch, context, "lim:freeze")

    (call,) = spawned.taskpump("freeze")
    i = call.index("--by")
    assert call[i + 1] == "owner", call
    assert not spawned.taskpump("thaw")

    await _press(orch, context, "lim:thaw")

    (call,) = spawned.taskpump("thaw")
    assert "--by" in call and call[call.index("--by") + 1] == "owner", call
    for backend in backends.values():
        backend.run_command.assert_not_awaited()


async def test_guest_pressing_the_freeze_button_is_refused(tmp_path, spawned):
    """Scenario 2, bad sequence: a callback bypasses the owner-only command
    gate, so a non-owner pressing a forwarded freeze button runs nothing."""
    _record_limit()
    settings = _settings(tmp_path)
    backends = _backends()
    orch = MessageOrchestrator(settings, {})
    guest_ctx = _context(settings, {"backends": backends}, role="guest")
    await _press(orch, guest_ctx, "lim:freeze")

    assert spawned.taskpump("freeze") == []


# --- scenario 3: chat to Codex --------------------------------------------------


async def test_codex_button_switches_this_chat_and_next_message_is_answered_by_codex(
    storage, tmp_path, spawned
):
    """Scenario: the button switches this chat to Codex; the next message is
    answered by Codex (Claude is never called)."""
    _record_limit()
    workdir = tmp_path / "ws"
    workdir.mkdir()
    settings = _settings(workdir)
    window_repo = WindowRepository(storage.db_manager)
    backends = _backends()
    orch = MessageOrchestrator(settings, {})
    context = _context(
        settings,
        {"backends": backends, "storage": storage, "window_repo": window_repo},
        current_directory=workdir,
        claude_session_id="c-old",
    )

    await _press(orch, context, "lim:codex")
    await _drain_tasks()

    assert context.user_data["agent_backend"] == "openai_codex"
    assert str(context.user_data.get("agent_model", "")).startswith("openai-codex/")
    saved = await storage.preferences.get(USER_ID)
    assert saved is not None and "openai_codex" in str(
        getattr(saved, "agent_backend", saved)
    )

    turn = _text_update("what is the pump doing?")
    await orch.agentic_text(turn, context)

    backends["claude"].run_command.assert_not_awaited()
    backends["openai_codex"].run_command.assert_awaited_once()
    assert (
        backends["openai_codex"]
        .run_command.await_args.kwargs["prompt"]
        .rstrip()
        .endswith("what is the pump doing?")
    )


# --- scenario 4: a chat / scheduled turn on the limit ---------------------------


def _limit_result(**kw: Any) -> ResultMessage:
    fields = dict(
        subtype="success",
        duration_ms=50,
        duration_api_ms=0,
        is_error=True,
        num_turns=1,
        session_id="sess-limit",
        total_cost_usd=0.0,
        usage={"input_tokens": 0, "output_tokens": 0},
        result=LIMIT_TEXT,
    )
    fields.update(kw)
    return ResultMessage(**fields)


def _sdk_client(*messages: Any):
    def factory(options: Any) -> AsyncMock:
        client = AsyncMock()
        client.connect = AsyncMock()
        client.disconnect = AsyncMock()
        client.query = AsyncMock()

        async def receive() -> Any:
            for m in messages:
                yield m

        client._query = AsyncMock()
        client._query.receive_messages = receive
        return client

    return factory


async def _turn_through_router(tmp_path, result: ResultMessage, purpose: str):
    """One real ClaudeSDKManager turn (SDK client faked) through the router,
    which writes the model_calls row."""
    from src.claude.sdk_integration import ClaudeSDKManager
    from src.config.settings import Settings
    from src.model_router import ModelRouter
    from src.model_router.harness import HarnessVersions
    from src.storage.database import DatabaseManager
    from src.storage.model_calls import ModelCallRepository

    class _Versions(HarnessVersions):
        async def get(self, harness: str) -> str:
            return "claude-9.9"

    config = Settings(
        telegram_bot_token="test:token",
        telegram_bot_username="testbot",
        approved_directory=tmp_path,
        claude_timeout_seconds=5,
        enable_mcp=False,
    )
    manager = ClaudeSDKManager(config)
    backend = MagicMock()
    backend.backend_id = "claude"
    backend.default_model.return_value = "sonnet"

    async def run_command(**kw: Any) -> Any:
        return await manager.execute_command(
            prompt=kw["prompt"], working_directory=kw["working_directory"]
        )

    backend.run_command = run_command
    db = DatabaseManager(f"sqlite:///{tmp_path / 'calls.db'}")
    await db.initialize()
    try:
        router = ModelRouter(
            recorder=ModelCallRepository(db),
            versions=_Versions(),
            backends={"claude": backend},
        )
        with (
            patch("src.claude.sdk_integration.parse_message", side_effect=lambda x: x),
            patch(
                "src.claude.sdk_integration.ClaudeSDKClient",
                side_effect=_sdk_client(result),
            ),
        ):
            response = await router.run_turn(
                "claude",
                purpose=purpose,
                attended=purpose == "chat",
                prompt="morning digest",
                working_directory=tmp_path,
            )
        async with db.get_connection() as conn:
            cur = await conn.execute("SELECT * FROM model_calls ORDER BY id")
            rows = [dict(r) for r in await cur.fetchall()]
    finally:
        await db.close()
    return response, rows


@pytest.mark.parametrize("purpose", ["chat", "scheduled"])
async def test_turn_hitting_the_limit_is_logged_error_and_recorded(tmp_path, purpose):
    """Scenario: a chat / scheduled turn hitting the limit is logged as an
    error (not ok with 0 tokens), and the shared limit record is written so
    /limits and the pump see it."""
    from src.model_router import quota

    assert quota.active() is None

    response, rows = await _turn_through_router(tmp_path, _limit_result(), purpose)

    assert response.is_error is True
    assert response.error_type == "quota"
    (row,) = rows
    assert row["outcome"] == "error", row
    assert "quota" in (row["error"] or ""), row
    got = quota.active()
    assert got is not None
    assert "weekly limit" in got.message
    assert got.until > datetime.now(UTC)


async def test_ordinary_turn_is_still_ok_and_records_no_limit(tmp_path):
    """added (guard): a normal answer stays an ok turn and writes no record —
    the limit check must not misfire on ordinary results."""
    from src.model_router import quota

    ok = _limit_result(
        is_error=False,
        result="All good. (You asked about the weekly report: it ships at 9am.)",
        usage={"input_tokens": 12, "output_tokens": 7},
        total_cost_usd=0.01,
    )
    response, rows = await _turn_through_router(tmp_path, ok, "scheduled")

    assert not response.is_error
    (row,) = rows
    assert row["outcome"] == "ok", row
    assert quota.active() is None
