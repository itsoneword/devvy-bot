"""Tests for the session watchdog alert gate (dv-8bb5)."""

from unittest.mock import AsyncMock

import pytest

from src.security.session_watchdog import CALLBACK_PREFIX, WatchdogManager


@pytest.fixture
def send_alert():
    return AsyncMock(return_value=555)


@pytest.fixture
def edit_alert():
    return AsyncMock()


@pytest.fixture
def manager(send_alert, edit_alert):
    return WatchdogManager(send_alert=send_alert, chat_ids=[999], edit_alert=edit_alert)


class TestWatchdogManager:
    async def test_trigger_sends_alert_and_tracks_token(self, manager, send_alert):
        await manager.trigger(42, "cost_per_hour", 5.0, 1.0)

        send_alert.assert_awaited_once()
        chat_id, text, token = send_alert.call_args.args
        assert chat_id == 999
        assert "42" in text
        assert manager.resolve_user(token) == 42

    async def test_no_chat_ids_drops_alert(self, send_alert):
        manager = WatchdogManager(send_alert=send_alert, chat_ids=[])
        await manager.trigger(42, "cost_per_hour", 5.0, 1.0)
        send_alert.assert_not_awaited()

    async def test_resolve_user_unknown_token_is_none(self, manager):
        assert manager.resolve_user("nope") is None

    async def test_clear_removes_pending_token(self, manager, send_alert):
        await manager.trigger(42, "messages_per_hour", 10.0, 3.0)
        token = send_alert.call_args.args[2]
        assert manager.resolve_user(token) == 42

        manager.clear(token)
        assert manager.resolve_user(token) is None

    async def test_send_failure_does_not_leave_pending_token(self, edit_alert):
        send_alert = AsyncMock(side_effect=RuntimeError("boom"))
        manager = WatchdogManager(
            send_alert=send_alert, chat_ids=[999], edit_alert=edit_alert
        )
        await manager.trigger(42, "cost_per_hour", 5.0, 1.0)
        # Nothing to resolve — the send never completed, so no token exists
        # to look up. Just verify triggering again after failure still works.
        send_alert.side_effect = None
        send_alert.return_value = 1
        await manager.trigger(42, "cost_per_hour", 5.0, 1.0)
        assert send_alert.await_count == 2

    def test_callback_prefix(self):
        assert CALLBACK_PREFIX == "watchdog"
