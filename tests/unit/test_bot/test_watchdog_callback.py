"""Tests for the watchdog hold/dismiss callback handler (dv-8bb5)."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.bot.orchestrator import MessageOrchestrator
from src.config import create_test_config

ADMIN_ID = 999
TARGET_ID = 111


@pytest.fixture
def orchestrator(tmp_path):
    settings = create_test_config(approved_directory=str(tmp_path))
    deps = {
        "claude_integration": MagicMock(),
        "storage": MagicMock(),
        "security_validator": MagicMock(),
        "rate_limiter": MagicMock(),
        "audit_logger": MagicMock(),
    }
    return MessageOrchestrator(settings, deps)


def _update_with_callback(data: str):
    update = MagicMock()
    query = MagicMock()
    query.data = data
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    query.message.text = "🐕 Session watchdog alert"
    update.callback_query = query
    update.effective_user.id = ADMIN_ID
    return update, query


def _context(token: str, watchdog_manager=None, hold_store=None, storage=None):
    context = MagicMock()
    context.bot_data = {
        "watchdog_manager": watchdog_manager,
        "hold_store": hold_store,
        "storage": storage,
    }
    return context


class TestWatchdogCallback:
    async def test_hold_10m_sets_hold_and_edits_message(self, orchestrator):
        wm = MagicMock()
        wm.resolve_user.return_value = TARGET_ID
        wm.clear = MagicMock()
        hold_store = AsyncMock()
        from datetime import UTC, datetime

        hold_store.set_hold.return_value = datetime(2026, 1, 1, tzinfo=UTC)

        update, query = _update_with_callback("watchdog:tok123:10m")
        context = _context("tok123", watchdog_manager=wm, hold_store=hold_store)

        await orchestrator._watchdog_callback(update, context)

        hold_store.set_hold.assert_awaited_once()
        args, kwargs = hold_store.set_hold.call_args
        assert args[0] == TARGET_ID
        assert kwargs["set_by"] == ADMIN_ID
        wm.clear.assert_called_once_with("tok123")
        query.answer.assert_awaited_once()
        query.edit_message_text.assert_awaited_once()
        assert "Held" in query.edit_message_text.call_args.args[0]

    async def test_hold_1d_uses_one_day_duration(self, orchestrator):
        wm = MagicMock()
        wm.resolve_user.return_value = TARGET_ID
        hold_store = AsyncMock()
        from datetime import UTC, datetime, timedelta

        hold_store.set_hold.return_value = datetime.now(UTC) + timedelta(days=1)

        update, query = _update_with_callback("watchdog:tok123:1d")
        context = _context("tok123", watchdog_manager=wm, hold_store=hold_store)

        await orchestrator._watchdog_callback(update, context)

        args, kwargs = hold_store.set_hold.call_args
        from datetime import timedelta as td

        assert args[1] == td(days=1)

    async def test_ack_does_not_touch_hold_store(self, orchestrator):
        wm = MagicMock()
        wm.resolve_user.return_value = TARGET_ID
        hold_store = AsyncMock()

        update, query = _update_with_callback("watchdog:tok123:ack")
        context = _context("tok123", watchdog_manager=wm, hold_store=hold_store)

        await orchestrator._watchdog_callback(update, context)

        hold_store.set_hold.assert_not_awaited()
        wm.clear.assert_called_once_with("tok123")
        assert "Dismissed" in query.edit_message_text.call_args.args[0]

    async def test_unknown_token_shows_stale_alert(self, orchestrator):
        wm = MagicMock()
        wm.resolve_user.return_value = None

        update, query = _update_with_callback("watchdog:gone:10m")
        context = _context("gone", watchdog_manager=wm)

        await orchestrator._watchdog_callback(update, context)

        query.answer.assert_awaited_once()
        assert query.answer.call_args.kwargs.get("show_alert") is True
        query.edit_message_text.assert_not_awaited()

    async def test_malformed_callback_data_is_ignored(self, orchestrator):
        update, query = _update_with_callback("watchdog:onlyonepart")
        context = _context("x")

        await orchestrator._watchdog_callback(update, context)

        query.answer.assert_awaited_once()
        query.edit_message_text.assert_not_awaited()

    async def test_missing_hold_store_reports_unavailable(self, orchestrator):
        wm = MagicMock()
        wm.resolve_user.return_value = TARGET_ID

        update, query = _update_with_callback("watchdog:tok123:10m")
        context = _context("tok123", watchdog_manager=wm, hold_store=None)

        await orchestrator._watchdog_callback(update, context)

        wm.clear.assert_called_once_with("tok123")
        assert "unavailable" in query.edit_message_text.call_args.args[0]
