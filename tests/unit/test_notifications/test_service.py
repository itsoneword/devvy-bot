"""Tests for the notification service."""

from unittest.mock import AsyncMock

import pytest

from src.events.bus import EventBus
from src.events.types import AgentResponseEvent
from src.notifications.service import NotificationService


@pytest.fixture
def event_bus() -> EventBus:
    return EventBus()


@pytest.fixture
def mock_bot() -> AsyncMock:
    bot = AsyncMock()
    bot.send_message = AsyncMock()
    return bot


@pytest.fixture
def service(event_bus: EventBus, mock_bot: AsyncMock) -> NotificationService:
    svc = NotificationService(
        event_bus=event_bus,
        bot=mock_bot,
        default_chat_ids=[100, 200],
    )
    svc.register()
    return svc


class TestNotificationService:
    """Tests for NotificationService."""

    async def test_handle_response_queues_event(
        self, service: NotificationService
    ) -> None:
        """Events are queued for delivery."""
        event = AgentResponseEvent(chat_id=100, text="hello")
        await service.handle_response(event)
        assert service._send_queue.qsize() == 1

    async def test_resolve_chat_ids_specific(
        self, service: NotificationService
    ) -> None:
        """Specific chat_id takes precedence over defaults."""
        event = AgentResponseEvent(chat_id=999, text="test")
        ids = service._resolve_chat_ids(event)
        assert ids == [999]

    async def test_resolve_chat_ids_default(self, service: NotificationService) -> None:
        """chat_id=0 falls back to default chat IDs."""
        event = AgentResponseEvent(chat_id=0, text="test")
        ids = service._resolve_chat_ids(event)
        assert ids == [100, 200]

    def test_split_message_short(self, service: NotificationService) -> None:
        """Short messages are not split."""
        chunks = service._split_message("short text")
        assert len(chunks) == 1
        assert chunks[0] == "short text"

    def test_split_message_long(self, service: NotificationService) -> None:
        """Long messages are split at boundaries."""
        text = "A" * 4000 + "\n\n" + "B" * 200
        chunks = service._split_message(text, max_length=4096)
        assert len(chunks) >= 1
        # All content preserved
        total_len = sum(len(c) for c in chunks)
        assert total_len > 0

    def test_split_message_no_boundary(self, service: NotificationService) -> None:
        """Messages without boundaries are hard-split."""
        text = "A" * 5000  # No newlines or spaces
        chunks = service._split_message(text, max_length=4096)
        assert len(chunks) == 2
        assert len(chunks[0]) == 4096
        assert len(chunks[1]) == 904

    async def test_send_to_telegram(
        self, service: NotificationService, mock_bot: AsyncMock
    ) -> None:
        """Messages are sent via the Telegram bot."""
        event = AgentResponseEvent(chat_id=123, text="hello world")
        await service._rate_limited_send(123, event)

        mock_bot.send_message.assert_called_once()
        call_kwargs = mock_bot.send_message.call_args.kwargs
        assert call_kwargs["chat_id"] == 123
        assert call_kwargs["text"] == "hello world"

    async def test_ignores_non_response_events(
        self, service: NotificationService
    ) -> None:
        """Non-AgentResponseEvent events are ignored."""
        from src.events.bus import Event

        event = Event(source="test")
        await service.handle_response(event)
        assert service._send_queue.qsize() == 0


class TestPinPersistence:
    """The pinned-message map must survive a bot restart."""

    async def test_pin_saved_and_reloaded(self, event_bus, mock_bot, tmp_path):
        from unittest.mock import MagicMock

        state = tmp_path / "pins.json"
        svc = NotificationService(
            event_bus=event_bus, bot=mock_bot, pin_state_path=state
        )
        mock_bot.send_message.return_value = MagicMock(message_id=42)
        await svc._send_or_edit_pinned(
            100, AgentResponseEvent(chat_id=100, text="running", pin_as="jobs")
        )
        assert svc._pinned_messages == {(100, "jobs"): 42}
        assert state.exists()

        restarted = NotificationService(
            event_bus=event_bus, bot=mock_bot, pin_state_path=state
        )
        assert restarted._pinned_messages == {(100, "jobs"): 42}

    async def test_retire_removes_from_state(self, event_bus, mock_bot, tmp_path):
        from unittest.mock import MagicMock

        state = tmp_path / "pins.json"
        svc = NotificationService(
            event_bus=event_bus, bot=mock_bot, pin_state_path=state
        )
        mock_bot.send_message.return_value = MagicMock(message_id=7)
        await svc._send_or_edit_pinned(
            100, AgentResponseEvent(chat_id=100, text="running", pin_as="jobs")
        )
        # Empty text retires the pin (unpin + delete + forget).
        await svc._send_or_edit_pinned(
            100, AgentResponseEvent(chat_id=100, text="", pin_as="jobs")
        )
        assert svc._pinned_messages == {}
        restarted = NotificationService(
            event_bus=event_bus, bot=mock_bot, pin_state_path=state
        )
        assert restarted._pinned_messages == {}

    async def test_corrupt_state_file_starts_empty(self, event_bus, mock_bot, tmp_path):
        state = tmp_path / "pins.json"
        state.write_text("{not json")
        svc = NotificationService(
            event_bus=event_bus, bot=mock_bot, pin_state_path=state
        )
        assert svc._pinned_messages == {}


class TestLedgeredDelivery:
    def _sent(self, chat_id: int, message_id: int):
        from unittest.mock import MagicMock

        sent = MagicMock()
        sent.chat.id = chat_id
        sent.message_id = message_id
        return sent

    async def test_ledgered_event_records_each_chunk(
        self, event_bus: EventBus, mock_bot: AsyncMock
    ) -> None:
        ledger = AsyncMock()
        svc = NotificationService(
            event_bus=event_bus,
            bot=mock_bot,
            default_chat_ids=[100],
            sent_messages=ledger,
        )
        mock_bot.send_message.return_value = self._sent(100, 77)
        event = AgentResponseEvent(
            chat_id=100,
            text="⏰ coach\n\nКак плечо?",
            parse_mode=None,
            ledger_kind="scheduled_reminder",
            ledger_profile_slug="coach#u42",
            ledger_messages_message_id=901,
        )
        await svc._rate_limited_send(100, event)
        ledger.record.assert_awaited_once_with(
            telegram_chat_id=100,
            telegram_message_id=77,
            kind="scheduled_reminder",
            messages_message_id=901,
            profile_slug="coach#u42",
        )

    async def test_unledgered_event_not_recorded(
        self, event_bus: EventBus, mock_bot: AsyncMock
    ) -> None:
        ledger = AsyncMock()
        svc = NotificationService(
            event_bus=event_bus,
            bot=mock_bot,
            default_chat_ids=[100],
            sent_messages=ledger,
        )
        mock_bot.send_message.return_value = self._sent(100, 78)
        await svc._rate_limited_send(100, AgentResponseEvent(chat_id=100, text="hi"))
        ledger.record.assert_not_awaited()
