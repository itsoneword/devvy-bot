"""OpenRouter as a provider on the Claude harness (dv-d2f6 step 1)."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.bot.orchestrator import MessageOrchestrator
from src.claude.sdk_integration import ClaudeResponse
from src.model_router import ModelRouter
from src.model_router.routes import (
    PROVIDER_ANTHROPIC_SUB,
    PROVIDER_OPENROUTER,
    Route,
    cost_from_tokens,
    provider_env,
    resolve_turn_route,
    user_pick_allowed,
)
from src.profiles.registry import load_profile_registry
from src.storage.database import DatabaseManager
from src.storage.model_calls import ModelCallRepository

GLM = "z-ai/glm-5.3"
OTHER = "z-ai/glm-5.2"

_BASE = """
profiles:
  - slug: devops
    name: DevOps
    working_dir: {root}/repo
    memory_dir: {root}/repo/memory
    persona: {root}/repo/agent.md
    default: true
    sandbox: false
  - slug: scratch
    name: Scratch
    working_dir: {root}/ws/scratch
    memory_dir: {root}/ws/scratch/memory
    persona: {root}/ws/scratch/persona.md
"""
_OK = f"    provider: openrouter\n    model: {GLM}\n    max_cost_per_request: 0.2\n"


def _load(tmp_path: Path, extra: str = ""):
    p = tmp_path / "profiles.yaml"
    p.write_text(_BASE.format(root=tmp_path) + extra)
    return load_profile_registry(p)


def _scratch(tmp_path: Path, extra: str = _OK):
    return _load(tmp_path, extra).get_by_slug("scratch")


# --- profile keys -----------------------------------------------------------


def test_default_profile_has_no_provider(tmp_path):
    p = _scratch(tmp_path, "")
    assert (p.provider, p.model, p.allowed_models) == (None, None, ())


def test_openrouter_profile_parses(tmp_path):
    p = _scratch(tmp_path, _OK + f"    allowed_models: [{GLM}, {OTHER}]\n")
    assert (p.provider, p.model) == ("openrouter", GLM)
    assert p.allowed_models == (GLM, OTHER)


@pytest.mark.parametrize(
    "extra, message",
    [
        (f"    model: {GLM}\n", "need a provider"),
        ("    provider: openrouter\n    max_cost_per_request: 1\n", "needs a model"),
        (_OK.replace("openrouter", "nowhere"), "unknown provider"),
        (_OK.replace("    max_cost_per_request: 0.2\n", ""), "max_cost_per_request"),
        (_OK + "    sandbox: false\n", "sandboxed"),
        (_OK + f"    allowed_models: [{OTHER}]\n", "must be in allowed_models"),
        (_OK + "    exchange: true\n", "exchange"),
        # dv-d2f6: the cap is checked on tokens x this rule until the
        # provider's charge is in, so an unpriced model fails closed.
        (_OK.replace(GLM, "acme/unpriced"), "no pricing rule"),
        (_OK + f"    allowed_models: [{GLM}, acme/x]\n", "no pricing rule"),
    ],
)
def test_openrouter_profile_fails_closed(tmp_path, extra, message):
    with pytest.raises(ValueError, match=message):
        _load(tmp_path, extra)


def test_openrouter_not_on_devops(tmp_path):
    p = tmp_path / "profiles.yaml"
    text = _BASE.format(root=tmp_path).replace(
        "    sandbox: false\n", "    sandbox: true\n" + _OK, 1
    )
    p.write_text(text)
    with pytest.raises(ValueError, match="devops"):
        load_profile_registry(p)


# --- resolver ---------------------------------------------------------------


def test_subscription_route_is_untouched(tmp_path):
    p = _scratch(tmp_path, "")
    assert resolve_turn_route(p, "opus") == Route(
        "claude", PROVIDER_ANTHROPIC_SUB, "opus"
    )
    assert resolve_turn_route(None, None).model is None


def test_openrouter_route_uses_profile_model(tmp_path):
    p = _scratch(tmp_path)
    r = resolve_turn_route(p, None)
    assert (r.provider, r.model) == (PROVIDER_OPENROUTER, GLM)
    # A Claude tier left over from /model is not a slug on this profile.
    assert resolve_turn_route(p, "sonnet").model == GLM


def test_pick_inside_allowed_models_wins_outside_is_ignored(tmp_path):
    p = _scratch(tmp_path, _OK + f"    allowed_models: [{GLM}, {OTHER}]\n")
    assert resolve_turn_route(p, OTHER).model == OTHER
    assert resolve_turn_route(p, "z-ai/glm-4.5").model == GLM
    assert user_pick_allowed(p, OTHER)
    assert not user_pick_allowed(p, "z-ai/glm-4.5")
    assert user_pick_allowed(None, "anything")


def test_provider_env_points_cli_at_openrouter():
    env = provider_env(Route("claude", PROVIDER_OPENROUTER, GLM), "k-123")
    assert env["ANTHROPIC_BASE_URL"] == "https://openrouter.ai/api"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "k-123"
    assert env["ANTHROPIC_API_KEY"] == ""
    assert env["ANTHROPIC_DEFAULT_SONNET_MODEL"] == GLM
    assert provider_env(Route("claude", PROVIDER_ANTHROPIC_SUB, "opus"), "k") == {}
    with pytest.raises(RuntimeError, match="setkey"):
        provider_env(Route("claude", PROVIDER_OPENROUTER, GLM), None)


# --- cost -------------------------------------------------------------------


def test_cost_from_tokens_prices_glm_not_anthropic():
    # Conservative rule: max over GLM 5.3's OpenRouter endpoints (2026-10-17).
    cost = cost_from_tokens(GLM, input_tokens=1_000_000, output_tokens=1_000_000)
    assert cost == pytest.approx(2.80 + 8.80)
    assert cost_from_tokens("z-ai/glm-9", input_tokens=1_000_000, output_tokens=0) > 0


def test_cost_unknown_model_is_unpriced_not_default():
    assert cost_from_tokens("acme/unknown", input_tokens=10, output_tokens=10) is None
    assert cost_from_tokens(None, input_tokens=10, output_tokens=10) is None


# --- router records provider + model ----------------------------------------


async def test_router_records_openrouter_provider_and_model(tmp_path):
    db = DatabaseManager(f"sqlite:///{tmp_path / 'b.db'}")
    await db.initialize()
    backend = MagicMock()
    backend.backend_id = "claude"
    backend.default_model.return_value = "sonnet"
    backend.run_command = AsyncMock(
        return_value=ClaudeResponse(
            content="hi",
            session_id="s",
            cost=0.0123,
            duration_ms=5,
            num_turns=1,
            input_tokens=100,
            output_tokens=20,
            provider=PROVIDER_OPENROUTER,
            model=GLM,
        )
    )
    router = ModelRouter(backends={"claude": backend}, recorder=ModelCallRepository(db))
    await router.run_turn("claude", purpose="chat", prompt="x", model=None)
    async with db.get_connection() as conn:
        cur = await conn.execute(
            "SELECT provider, model, input_tokens, cost_usd FROM model_calls"
        )
        row = await cur.fetchone()
    assert tuple(row) == ("openrouter", GLM, 100, 0.0123)


# --- /model gate ------------------------------------------------------------


def _orch(profile):
    orch = MessageOrchestrator(
        SimpleNamespace(
            agentic_mode=True,
            agent_backend="claude",
            agent_model="claude",
            claude_model="sonnet",
        ),
        {},
    )
    orch._active_profile = lambda context, user_id: profile  # type: ignore
    return orch


def _profile(tmp_path):
    return _scratch(tmp_path, _OK + f"    allowed_models: [{GLM}, {OTHER}]\n")


def test_model_override_passes_allowed_slug_only(tmp_path):
    orch = _orch(_profile(tmp_path))
    ctx = SimpleNamespace(user_data={"agent_model": OTHER}, bot_data={})
    assert orch._model_override(ctx, 1) == OTHER
    ctx = SimpleNamespace(user_data={"agent_model": "z-ai/glm-4.5"}, bot_data={})
    assert orch._model_override(ctx, 1) is None


async def test_model_callback_refuses_slug_outside_allowed(tmp_path):
    orch = _orch(_profile(tmp_path))
    ctx = SimpleNamespace(user_data={}, bot_data={})
    query = SimpleNamespace(
        from_user=SimpleNamespace(id=1), edit_message_text=AsyncMock()
    )
    await orch._handle_model_callback(query, ctx, "model:or:z-ai/glm-4.5")
    assert "agent_model" not in ctx.user_data
    assert "not allowed" in query.edit_message_text.await_args.args[0]

    orch._save_user_prefs = AsyncMock()
    await orch._handle_model_callback(query, ctx, f"model:or:{OTHER}")
    assert ctx.user_data["agent_model"] == OTHER


async def test_model_keyboard_lists_profile_allowed_models(tmp_path, monkeypatch):
    from src.agent_backends import codex_catalog

    monkeypatch.setattr(codex_catalog.CODEX_CATALOG, "get", AsyncMock(return_value=[]))
    orch = _orch(_profile(tmp_path))
    update = SimpleNamespace(message=SimpleNamespace(reply_text=AsyncMock()))
    ctx = SimpleNamespace(user_data={}, bot_data={})
    await orch.agentic_model(update, ctx)
    markup = update.message.reply_text.await_args.kwargs["reply_markup"]
    cbs = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert [c for c in cbs if c.startswith("model:or:")] == [
        f"model:or:{GLM}",
        f"model:or:{OTHER}",
    ]
