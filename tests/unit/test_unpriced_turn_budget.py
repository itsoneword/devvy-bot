"""dv-0722: a turn with no USD price is budgeted in tokens, never as free.

Codex on a ChatGPT subscription reports no dollar figure and the model
catalog carries no prices, so ``ClaudeResponse.cost`` is ``None`` for those
turns (it used to be ``0.0``, which the rate limiter, the watchdog and
``/usage`` all read as "free"). These tests pin the seam end to end: the
backend reports ``None``, the orchestrator routes ``None`` to
``RateLimiter.record_tokens`` (and a priced turn to ``record_cost``), and the
USD ledgers store ``0.0`` rather than crashing on ``None``.
"""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.agent_backends.codex_cli import CodexCliIntegration, InMemoryCodexSessionStore
from src.bot.orchestrator import MessageOrchestrator
from src.claude.sdk_integration import ClaudeResponse
from src.config.settings import Settings
from tests.unit.test_codex_cli_integration import _fake_codex


def _settings() -> Settings:
    return Settings(
        telegram_bot_token="test:token",
        telegram_bot_username="testbot",
        approved_directory=None,
        enable_mcp=False,
        mcp_config_path=None,
    )


# --- backend -----------------------------------------------------------------


async def test_codex_turn_reports_no_price_not_zero(tmp_path):
    """The Codex backend has no USD figure: cost is None, tokens are real."""
    integ = CodexCliIntegration(
        config=_settings(),
        codex_bin=str(_fake_codex(tmp_path)),
        session_store=InMemoryCodexSessionStore(),
    )
    resp = await integ.run_command(
        prompt="hello",
        working_directory=tmp_path,
        user_id=7,
        session_id=None,
        force_new=True,
    )
    assert resp.cost is None
    assert resp.input_tokens + resp.output_tokens == 15
    # The USD ledgers get a number, not None.
    assert resp.cost_usd == 0.0


def test_cost_usd_maps_none_to_zero_and_keeps_prices():
    def _resp(cost):
        return ClaudeResponse(
            content="x", session_id="s", cost=cost, duration_ms=1, num_turns=1
        )

    assert _resp(None).cost_usd == 0.0
    assert _resp(0.0).cost_usd == 0.0
    assert _resp(1.25).cost_usd == 1.25


# --- orchestrator: record_cost vs record_tokens ------------------------------


def _mock_response(cost, input_tokens=10, output_tokens=20) -> MagicMock:
    r = MagicMock()
    r.session_id = "sess-1"
    r.content = "the answer"
    r.tools_used = []
    r.input_tokens = input_tokens
    r.output_tokens = output_tokens
    r.context_tokens = 100
    r.cost = cost
    r.num_turns = 1
    r.error_type = None
    r.session_restarted = False
    return r


def _harness(settings: Settings, tmp_path: Path, response: MagicMock):
    claude_integration = MagicMock()
    claude_integration.run_command = AsyncMock(return_value=response)

    rate_limiter = MagicMock()
    rate_limiter.check_rate_limit = AsyncMock(return_value=(True, None))
    rate_limiter.record_cost = AsyncMock()
    rate_limiter.record_tokens = AsyncMock()
    rate_limiter.record_watchdog_turn = MagicMock(return_value=[])

    watchdog_manager = MagicMock()
    watchdog_manager.trigger = AsyncMock()

    update = MagicMock()
    update.effective_user.id = 123
    update.message.text = "hi"
    update.message.message_id = 1
    update.message.message_thread_id = None
    update.message.reply_to_message = None
    update.message.chat.id = 999
    update.message.chat.type = "group"
    update.message.chat.send_action = AsyncMock()
    update.message.reply_text = AsyncMock(return_value=AsyncMock())

    context = MagicMock()
    context.user_data = {"current_directory": tmp_path}
    context.bot_data = {
        "settings": settings,
        "claude_integration": claude_integration,
        "storage": None,
        "rate_limiter": rate_limiter,
        "watchdog_manager": watchdog_manager,
        "audit_logger": None,
        "window_repo": None,
    }
    return update, context, rate_limiter


@pytest.fixture(autouse=True)
def _denylist_env(monkeypatch):
    monkeypatch.delenv("APPROVED_DIRECTORY", raising=False)


async def test_unpriced_turn_is_budgeted_in_tokens(tmp_path):
    settings = _settings()
    orchestrator = MessageOrchestrator(settings, {})
    update, context, rate_limiter = _harness(
        settings, tmp_path, _mock_response(cost=None, input_tokens=11, output_tokens=4)
    )

    await orchestrator.agentic_text(update, context)

    rate_limiter.record_tokens.assert_awaited_once_with(123, 15)
    rate_limiter.record_cost.assert_not_awaited()
    # The watchdog sees the same split: no dollars, the token delta instead.
    rate_limiter.record_watchdog_turn.assert_called_once_with(123, None, 1, tokens=15)


async def test_priced_turn_still_goes_to_record_cost(tmp_path):
    settings = _settings()
    orchestrator = MessageOrchestrator(settings, {})
    update, context, rate_limiter = _harness(
        settings, tmp_path, _mock_response(cost=0.42)
    )

    await orchestrator.agentic_text(update, context)

    rate_limiter.record_cost.assert_awaited_once_with(123, 0.42)
    rate_limiter.record_tokens.assert_not_awaited()
    rate_limiter.record_watchdog_turn.assert_called_once_with(123, 0.42, 1, tokens=30)


async def test_free_priced_turn_records_nothing(tmp_path):
    """0.0 keeps its old meaning -- priced and free -- and is not tokenised."""
    settings = _settings()
    orchestrator = MessageOrchestrator(settings, {})
    update, context, rate_limiter = _harness(
        settings, tmp_path, _mock_response(cost=0.0)
    )

    await orchestrator.agentic_text(update, context)

    rate_limiter.record_cost.assert_not_awaited()
    rate_limiter.record_tokens.assert_not_awaited()


async def test_unpriced_turn_footer_has_no_dollar_figure(tmp_path):
    """verbose>=2 footer: tokens shown, no '$0.0000' pretending a price."""
    settings = _settings()
    orchestrator = MessageOrchestrator(settings, {})
    update, context, _ = _harness(
        settings, tmp_path, _mock_response(cost=None, input_tokens=11, output_tokens=4)
    )
    context.user_data["verbose_level"] = 2

    await orchestrator.agentic_text(update, context)

    sent = [c.args[0] for c in update.message.reply_text.call_args_list if c.args]
    footer = next((t for t in sent if "11 in" in t), None)
    assert footer is not None, sent
    assert "$" not in footer.split("---")[-1]
