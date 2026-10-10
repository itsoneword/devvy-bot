"""`/model` renders the Codex buttons from the live catalog (dv-0c01).

Before this, the menu hardcoded two model slugs while
`codex debug models` reported four listable models on the owner's
subscription — two of them unreachable from Telegram.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.agent_backends.codex_catalog import CodexModel
from src.bot.orchestration.base import (
    _CODEX_DEFAULT_MODEL,
    _CODEX_HARD_MODEL,
    codex_supported_models,
)
from src.bot.orchestrator import MessageOrchestrator

LIVE = [
    CodexModel("gpt-5.6-sol", "GPT-5.6-Sol", 272000, 4),
    CodexModel("gpt-5.6-terra", "GPT-5.6-Terra", 272000, 7),
    CodexModel("gpt-5.6-luna", "GPT-5.6-Luna", 272000, 8),
    CodexModel("gpt-5.5", "GPT-5.5", 272000, 12),
]


def _orchestrator():
    settings = SimpleNamespace(
        agentic_mode=True,
        agent_backend="claude",
        agent_model="claude",
        claude_model="sonnet",
    )
    return MessageOrchestrator(settings, {})


@pytest.fixture
def catalog(monkeypatch):
    from src.agent_backends import codex_catalog

    get = AsyncMock(return_value=list(LIVE))
    monkeypatch.setattr(codex_catalog.CODEX_CATALOG, "get", get)
    monkeypatch.setattr(
        codex_catalog.CODEX_CATALOG, "_models", list(LIVE), raising=False
    )
    return codex_catalog.CODEX_CATALOG


async def test_model_keyboard_lists_every_live_codex_model(catalog):
    orch = _orchestrator()
    update = SimpleNamespace(message=SimpleNamespace(reply_text=AsyncMock()))
    context = SimpleNamespace(user_data={}, bot_data={})

    await orch.agentic_model(update, context)

    markup = update.message.reply_text.await_args.kwargs["reply_markup"]
    callbacks = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert callbacks[:2] == ["model:claude:sonnet", "model:claude:opus"]
    assert [c for c in callbacks if c.startswith("model:codex:")] == [
        "model:codex:gpt-5.6-sol",
        "model:codex:gpt-5.6-terra",
        "model:codex:gpt-5.6-luna",
        "model:codex:gpt-5.5",
    ]
    # The login/logout row is still last.
    assert callbacks[-2:] == ["model:codex_login", "model:codex_logout"]
    labels = [b.text for row in markup.inline_keyboard for b in row]
    assert "Codex GPT-5.6-Terra" in labels
    catalog.get.assert_awaited_once_with(force=True)


async def test_a_live_only_model_is_accepted_by_the_callback(catalog):
    orch = _orchestrator()
    storage = SimpleNamespace(preferences=AsyncMock())
    context = SimpleNamespace(
        user_data={"role": "owner"}, bot_data={"storage": storage}
    )
    query = SimpleNamespace(
        from_user=SimpleNamespace(id=42),
        edit_message_text=AsyncMock(),
        message=SimpleNamespace(chat_id=1),
    )

    await orch._handle_model_callback(query, context, "model:codex:gpt-5.6-luna")

    assert context.user_data["agent_model"] == "openai-codex/gpt-5.6-luna"
    assert context.user_data["agent_backend"] == "openai_codex"
    # ...and it survives the _selected_model round trip instead of being
    # rewritten to the default.
    assert orch._selected_model(context) == "openai-codex/gpt-5.6-luna"
    # The pick is persisted: that row is what survives a restart.
    storage.preferences.set.assert_awaited_once_with(
        42, agent_backend="openai_codex", agent_model="openai-codex/gpt-5.6-luna"
    )


async def test_codex_pick_reaches_the_turn_as_the_model_kwarg(catalog):
    """dv-4207: the callback writes user_data/DB only. The turn reads the
    pick back through `_model_override` and hands it to `run_command(model=)`;
    the integration object holds no per-user model state."""
    orch = _orchestrator()
    codex_integration = SimpleNamespace()  # no set_user_model to call
    context = SimpleNamespace(
        user_data={"role": "owner"},
        bot_data={
            "storage": SimpleNamespace(preferences=AsyncMock()),
            "codex_integration": codex_integration,
        },
    )
    query = SimpleNamespace(
        from_user=SimpleNamespace(id=42),
        edit_message_text=AsyncMock(),
        message=SimpleNamespace(chat_id=1),
    )

    await orch._handle_model_callback(query, context, "model:codex:gpt-5.6-terra")

    assert vars(codex_integration) == {}
    assert orch._model_override(context) == "openai-codex/gpt-5.6-terra"


def test_codex_pick_survives_a_restart_via_the_prefs_row(catalog):
    """After a restart user_data is empty; the first contact seeds it from the
    DB prefs row (base.py `seed_prefs_into_user_data`), so the kwarg path
    needs nothing else to remember the pick."""
    from src.bot.orchestration.routing import seed_prefs_into_user_data

    orch = _orchestrator()
    context = SimpleNamespace(user_data={}, bot_data={})
    seed_prefs_into_user_data(
        context.user_data,
        {"agent_backend": "openai_codex", "agent_model": "openai-codex/gpt-5.6-luna"},
    )
    assert orch._model_override(context) == "openai-codex/gpt-5.6-luna"


def test_pinned_constants_stay_valid_without_any_catalog(monkeypatch):
    """The two historical constants must never stop working — a box that
    cannot run `codex debug models` still has to be able to pick a model."""
    from src.agent_backends import codex_catalog

    monkeypatch.setattr(codex_catalog.CODEX_CATALOG, "_models", None, raising=False)
    supported = codex_supported_models()
    assert _CODEX_DEFAULT_MODEL in supported
    assert _CODEX_HARD_MODEL in supported

    orch = _orchestrator()
    context = SimpleNamespace(
        user_data={"agent_backend": "openai_codex", "agent_model": _CODEX_HARD_MODEL},
        bot_data={},
    )
    assert orch._selected_model(context) == _CODEX_HARD_MODEL

    # Regression: before startup refreshed entitlements, a cold catalog did
    # not know Sol and rewrote the persisted preference to gpt-5.4-mini.
    context.user_data["agent_model"] = "openai-codex/gpt-5.6-sol"
    assert orch._selected_model(context) == "openai-codex/gpt-5.6-sol"
    assert context.user_data["agent_model"] == "openai-codex/gpt-5.6-sol"


def test_an_unknown_model_ref_is_reset_to_the_default(monkeypatch):
    from src.agent_backends import codex_catalog

    monkeypatch.setattr(codex_catalog.CODEX_CATALOG, "_models", list(LIVE))
    orch = _orchestrator()
    context = SimpleNamespace(
        user_data={
            "agent_backend": "openai_codex",
            "agent_model": "openai-codex/gpt-4-retired",
        },
        bot_data={},
    )
    assert orch._selected_model(context) == _CODEX_DEFAULT_MODEL
    assert context.user_data["agent_model"] == _CODEX_DEFAULT_MODEL
