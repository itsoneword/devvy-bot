"""AgentBackend Protocol + bot_data["backends"] registry (dv-5368).

Orchestration picks a backend by looking ``user_data["agent_backend"]`` up in
``bot_data["backends"]``; /status, /sessions and the cd session lookup use
only the Protocol surface, so a registry with a single Codex backend works.
"""

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.agent_backends import CodexCliIntegration
from src.agent_backends.protocol import (
    _CODEX_DEFAULT_MODEL,
    BACKEND_POLICIES,
    BackendSession,
    policy_for,
)
from src.bot.orchestrator import MessageOrchestrator
from src.claude.facade import ClaudeIntegration
from src.config import create_test_config

PROTOCOL_MEMBERS = (
    "backend_id",
    "default_model",
    "run_command",
    "resumable_session_for",
    "sessions_for_user",
    "attach_tools",
)


@pytest.fixture
def settings(tmp_path):
    return create_test_config(approved_directory=str(tmp_path), agentic_mode=True)


@pytest.fixture
def orch(settings):
    return MessageOrchestrator(settings, {"storage": MagicMock()})


def _backend(backend_id: str, session_id: str = "sess") -> MagicMock:
    """A mock satisfying the AgentBackend surface (and nothing else)."""
    b = MagicMock(spec=PROTOCOL_MEMBERS)
    b.backend_id = backend_id
    response = MagicMock()
    response.session_id = session_id
    response.content = f"hello from {backend_id}"
    response.tools_used = []
    response.cost = None
    response.input_tokens = 0
    response.output_tokens = 0
    response.num_turns = 1
    b.run_command = AsyncMock(return_value=response)
    b.resumable_session_for = AsyncMock(return_value=None)
    b.sessions_for_user = AsyncMock(return_value=[])
    return b


def _text_update(text: str = "hi") -> MagicMock:
    update = MagicMock()
    update.effective_user.id = 123
    update.message.text = text
    update.message.message_id = 1
    update.message.chat.type = "private"
    update.message.chat.send_action = AsyncMock()
    progress = AsyncMock()
    update.message.reply_text = AsyncMock(return_value=progress)
    return update


def _ctx(user_data: dict, bot_data: dict, settings) -> MagicMock:
    context = MagicMock()
    context.user_data = user_data
    context.bot_data = {
        "settings": settings,
        "storage": None,
        "rate_limiter": None,
        "audit_logger": None,
        **bot_data,
    }
    return context


# --- Protocol conformance -------------------------------------------------


def test_concrete_backends_expose_the_protocol_surface():
    for cls in (ClaudeIntegration, CodexCliIntegration):
        missing = [m for m in PROTOCOL_MEMBERS if not hasattr(cls, m)]
        assert not missing, f"{cls.__name__} lacks {missing}"
    assert ClaudeIntegration.backend_id == "claude"
    assert CodexCliIntegration.backend_id == "openai_codex"
    # Every registered policy id has a matching Protocol id space entry.
    assert set(BACKEND_POLICIES) == {"claude", "openai_codex"}


def test_policies_own_session_key_label_and_model_validation():
    claude, codex = policy_for("claude"), policy_for("openai_codex")
    assert (claude.session_key, codex.session_key) == (
        "claude_session_id",
        "codex_session_id",
    )
    assert (claude.label, codex.label) == ("claude", "codex")
    assert claude.resolve_model("opus") == "opus"
    assert claude.resolve_model("openai-codex/gpt-5.5") is None
    assert codex.resolve_model("sonnet") == _CODEX_DEFAULT_MODEL
    assert codex.resolve_model(None) == _CODEX_DEFAULT_MODEL
    # Unknown ids take the Claude policy — the old `!= "openai_codex"` branch.
    assert policy_for("something_else") is claude


# --- _run_turn backend selection --------------------------------------------


@pytest.mark.parametrize("selected", ["claude", "openai_codex"])
async def test_run_turn_dispatches_to_the_registered_backend(orch, settings, selected):
    claude, codex = _backend("claude", "c-1"), _backend("openai_codex", "x-1")
    context = _ctx(
        {"agent_backend": selected},
        {"backends": {"claude": claude, "openai_codex": codex}},
        settings,
    )

    await orch.agentic_text(_text_update(), context)

    chosen, other = (codex, claude) if selected == "openai_codex" else (claude, codex)
    chosen.run_command.assert_awaited_once()
    other.run_command.assert_not_awaited()
    key = policy_for(selected).session_key
    assert context.user_data[key] == chosen.run_command.return_value.session_id


async def test_run_turn_reports_unregistered_backend(orch, settings):
    """Codex selected, only Claude registered: same message as before."""
    update = _text_update()
    context = _ctx(
        {"agent_backend": "openai_codex"},
        {"backends": {"claude": _backend("claude")}},
        settings,
    )

    await orch.agentic_text(update, context)

    progress = update.message.reply_text.return_value
    progress.edit_text.assert_awaited_once()
    assert (
        "openai_codex integration not available" in progress.edit_text.call_args[0][0]
    )


async def test_registry_falls_back_to_legacy_bot_data_keys(orch, settings):
    claude = _backend("claude")
    context = _ctx({}, {"claude_integration": claude}, settings)
    assert orch._active_backend(context) is claude
    context = _ctx(
        {"agent_backend": "openai_codex"}, {"codex_integration": claude}, settings
    )
    assert orch._active_backend(context) is claude
    # The registry, when present, wins even if legacy keys are also set.
    codex = _backend("openai_codex")
    context = _ctx(
        {"agent_backend": "openai_codex"},
        {"backends": {"openai_codex": codex}, "codex_integration": claude},
        settings,
    )
    assert orch._active_backend(context) is codex


# --- Codex-only bot_data: /status, /sessions, cd ------------------------------


async def test_status_works_with_only_codex_registered(orch, settings):
    update = MagicMock()
    update.effective_user.id = 123
    update.message.reply_text = AsyncMock()
    context = _ctx(
        {
            "agent_backend": "openai_codex",
            "codex_session_id": "thread-1",
            "agent_model": "openai-codex/gpt-5.5",
        },
        {"backends": {"openai_codex": _backend("openai_codex")}},
        settings,
    )

    await orch.agentic_status(update, context)

    text = update.message.reply_text.call_args.args[0]
    assert "Session: active" in text
    assert "gpt-5.5" in text


async def test_sessions_works_with_only_codex_registered(orch, settings, tmp_path):
    codex = _backend("openai_codex")
    codex.sessions_for_user = AsyncMock(
        return_value=[
            BackendSession(
                session_id="thread-1",
                working_dir=tmp_path / "devops",
                last_used=datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc),
                cumulative_tokens=4242,
                backend_id="openai_codex",
            )
        ]
    )
    profiles = [
        SimpleNamespace(slug="devops", working_dir=tmp_path / "devops"),
        SimpleNamespace(slug="chef", working_dir=tmp_path / "chef"),
    ]
    registry = SimpleNamespace(profiles=profiles, get_default=lambda: profiles[0])
    update = MagicMock()
    update.effective_user.id = 123
    update.message.reply_text = AsyncMock()
    context = _ctx(
        {"agent_backend": "openai_codex"},
        {
            "backends": {"openai_codex": codex},
            "profile_registry": registry,
            "token_tracker": None,
        },
        settings,
    )

    await orch.agentic_sessions(update, context)

    text = update.message.reply_text.call_args.args[0]
    assert "devops: 4242 tokens (codex), last 2026-09-06T12:00:00+00:00" in text
    assert "chef: no session yet" in text


async def test_sessions_first_registered_backend_wins_a_directory(
    orch, settings, tmp_path
):
    """Registry order is the precedence: Claude first, Codex fills gaps."""
    wd = tmp_path / "devops"
    older = datetime(2026, 9, 1, tzinfo=timezone.utc)
    newer = datetime(2026, 9, 6, tzinfo=timezone.utc)
    claude, codex = _backend("claude"), _backend("openai_codex")
    claude.sessions_for_user = AsyncMock(
        return_value=[BackendSession("c-1", wd, older, 10, "claude")]
    )
    codex.sessions_for_user = AsyncMock(
        return_value=[BackendSession("x-1", wd, newer, 20, "openai_codex")]
    )
    profiles = [SimpleNamespace(slug="devops", working_dir=wd)]
    registry = SimpleNamespace(profiles=profiles, get_default=lambda: profiles[0])
    update = MagicMock()
    update.effective_user.id = 123
    update.message.reply_text = AsyncMock()
    context = _ctx(
        {},
        {
            "backends": {"claude": claude, "openai_codex": codex},
            "profile_registry": registry,
            "token_tracker": None,
        },
        settings,
    )

    await orch.agentic_sessions(update, context)

    text = update.message.reply_text.call_args.args[0]
    assert "devops: 10 tokens (claude)" in text


async def test_cd_refreshes_every_registered_backends_session_key(orch, settings):
    claude, codex = _backend("claude"), _backend("openai_codex")
    claude.resumable_session_for = AsyncMock(return_value="c-resume")
    codex.resumable_session_for = AsyncMock(return_value="x-resume")
    context = _ctx(
        {
            "agent_backend": "openai_codex",
            "claude_session_id": "stale-c",
            "codex_session_id": "stale-x",
        },
        {"backends": {"claude": claude, "openai_codex": codex}},
        settings,
    )

    resumed = await orch._refresh_resumable_sessions(context, 123, Path("/w"))

    assert resumed == "x-resume"
    assert context.user_data["claude_session_id"] == "c-resume"
    assert context.user_data["codex_session_id"] == "x-resume"
    claude.resumable_session_for.assert_awaited_once_with(123, Path("/w"))


async def test_cd_with_only_codex_registered_clears_nothing_else(orch, settings):
    codex = _backend("openai_codex")
    codex.resumable_session_for = AsyncMock(return_value=None)
    context = _ctx(
        {"agent_backend": "claude", "codex_session_id": "stale-x"},
        {"backends": {"openai_codex": codex}},
        settings,
    )

    resumed = await orch._refresh_resumable_sessions(context, 123, Path("/w"))

    # Selected backend is not wired: nothing to resume, key reset (as before).
    assert resumed is None
    assert context.user_data["claude_session_id"] is None
    assert context.user_data["codex_session_id"] is None


# --- concrete adapters -----------------------------------------------------


async def test_codex_sessions_for_user_maps_the_store(tmp_path):
    from src.agent_backends import CodexSessionStore

    store = CodexSessionStore(tmp_path / "codex.json")
    store.record(7, tmp_path / "wd", "thread-7", 100, 50)
    integ = CodexCliIntegration(
        config=create_test_config(approved_directory=str(tmp_path)),
        session_store=store,
    )

    assert await integ.resumable_session_for(7, tmp_path / "wd") == "thread-7"
    assert await integ.resumable_session_for(8, tmp_path / "wd") is None
    (s,) = await integ.sessions_for_user(7)
    assert (s.session_id, s.working_dir, s.cumulative_tokens, s.backend_id) == (
        "thread-7",
        tmp_path / "wd",
        150,
        "openai_codex",
    )
    # Round-trips the store's ISO string exactly (what /sessions prints).
    assert s.last_used.isoformat() == store.info_for(7, tmp_path / "wd")["updated_at"]


def test_attach_tools_each_backend_takes_its_transport(tmp_path):
    services, bridge = MagicMock(), MagicMock(url="http://127.0.0.1:1")
    config = create_test_config(approved_directory=str(tmp_path))

    claude = ClaudeIntegration(config=config, sdk_manager=MagicMock())
    claude.attach_tools(services, bridge)
    claude.sdk_manager.attach_agent_services.assert_called_once_with(services)

    from src.agent_backends import CodexSessionStore

    codex = CodexCliIntegration(
        config=config, session_store=CodexSessionStore(tmp_path / "s.json")
    )
    codex.attach_tools(services, None)
    assert codex.agent_bridge is None
    codex.attach_tools(services, bridge)
    assert codex.agent_bridge is bridge
