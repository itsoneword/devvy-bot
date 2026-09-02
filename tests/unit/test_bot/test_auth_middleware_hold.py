"""Tests for the session-watchdog hold check in auth_middleware (dv-8bb5)."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

from src.bot.middleware.auth import auth_middleware

USER_ID = 111


def _mock_update(text: str = "hello", is_callback: bool = False):
    event = MagicMock()
    event.effective_user.id = USER_ID
    event.effective_user.username = "held_user"
    event.effective_message.text = text
    event.effective_message.reply_text = AsyncMock()
    event.callback_query = MagicMock() if is_callback else None
    return event


def _authenticated_auth_manager():
    auth_manager = MagicMock()
    auth_manager.is_authenticated.return_value = True
    auth_manager.refresh_session.return_value = False
    auth_manager.get_user_role.return_value = "guest"
    return auth_manager


class FakeHoldStore:
    def __init__(self, held_until=None):
        self._held_until = held_until

    def held_until(self, user_id):
        return self._held_until if user_id == USER_ID else None


class TestAuthMiddlewareHold:
    async def test_held_user_plain_message_is_blocked(self):
        held_until = datetime.now(UTC) + timedelta(minutes=10)
        event = _mock_update("please do something")
        handler = AsyncMock()

        await auth_middleware(
            handler,
            event,
            {
                "auth_manager": _authenticated_auth_manager(),
                "hold_store": FakeHoldStore(held_until),
                "user_data": {},
            },
        )

        handler.assert_not_awaited()
        event.effective_message.reply_text.assert_awaited_once()

    async def test_held_user_command_passes_through(self):
        held_until = datetime.now(UTC) + timedelta(minutes=10)
        event = _mock_update("/usage")
        handler = AsyncMock()

        await auth_middleware(
            handler,
            event,
            {
                "auth_manager": _authenticated_auth_manager(),
                "hold_store": FakeHoldStore(held_until),
                "user_data": {},
            },
        )

        handler.assert_awaited_once()

    async def test_held_user_callback_passes_through(self):
        held_until = datetime.now(UTC) + timedelta(minutes=10)
        event = _mock_update("", is_callback=True)
        handler = AsyncMock()

        await auth_middleware(
            handler,
            event,
            {
                "auth_manager": _authenticated_auth_manager(),
                "hold_store": FakeHoldStore(held_until),
                "user_data": {},
            },
        )

        handler.assert_awaited_once()

    async def test_unheld_user_passes_through(self):
        event = _mock_update("hello")
        handler = AsyncMock()

        await auth_middleware(
            handler,
            event,
            {
                "auth_manager": _authenticated_auth_manager(),
                "hold_store": FakeHoldStore(None),
                "user_data": {},
            },
        )

        handler.assert_awaited_once()

    async def test_no_hold_store_wired_passes_through(self):
        """hold_store missing entirely (e.g. wiring failed) must not block."""
        event = _mock_update("hello")
        handler = AsyncMock()

        await auth_middleware(
            handler,
            event,
            {"auth_manager": _authenticated_auth_manager(), "user_data": {}},
        )

        handler.assert_awaited_once()
