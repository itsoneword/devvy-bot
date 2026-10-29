"""Tests for the notification service."""

from unittest.mock import AsyncMock

import pytest
from telegram.error import BadRequest

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
    """The pinned-message map must survive a bot restart (bot.db, dv-ebde)."""

    @pytest.fixture
    async def pins(self, tmp_path):
        from src.storage.facade import Storage

        storage = Storage(f"sqlite:///{tmp_path / 'bot.db'}")
        await storage.initialize()
        yield storage.pinned_messages
        await storage.close()

    async def _started(self, event_bus, mock_bot, pins) -> NotificationService:
        svc = NotificationService(event_bus=event_bus, bot=mock_bot, pin_store=pins)
        await svc._load_pins()  # what start() does, without the sender task
        return svc

    async def test_pin_saved_and_reloaded(self, event_bus, mock_bot, pins):
        from unittest.mock import MagicMock

        svc = await self._started(event_bus, mock_bot, pins)
        mock_bot.send_message.return_value = MagicMock(message_id=42)
        await svc._send_or_edit_pinned(
            100, AgentResponseEvent(chat_id=100, text="running", pin_as="jobs")
        )
        assert svc._pinned_messages == {(100, "jobs"): 42}
        assert await pins.load_all() == {(100, "jobs"): 42}

        restarted = await self._started(event_bus, mock_bot, pins)
        assert restarted._pinned_messages == {(100, "jobs"): 42}

    async def test_retire_removes_from_state(self, event_bus, mock_bot, pins):
        from unittest.mock import MagicMock

        svc = await self._started(event_bus, mock_bot, pins)
        mock_bot.send_message.return_value = MagicMock(message_id=7)
        await svc._send_or_edit_pinned(
            100, AgentResponseEvent(chat_id=100, text="running", pin_as="jobs")
        )
        # Empty text retires the pin (unpin + delete + forget).
        await svc._send_or_edit_pinned(
            100, AgentResponseEvent(chat_id=100, text="", pin_as="jobs")
        )
        assert svc._pinned_messages == {}
        restarted = await self._started(event_bus, mock_bot, pins)
        assert restarted._pinned_messages == {}

    async def test_start_loads_pins(self, event_bus, mock_bot, pins):
        await pins.set(100, "jobs", 9)
        svc = NotificationService(event_bus=event_bus, bot=mock_bot, pin_store=pins)
        await svc.start()
        try:
            assert svc._pinned_messages == {(100, "jobs"): 9}
        finally:
            await svc.stop()

    async def test_unreadable_store_starts_empty(self, event_bus, mock_bot):
        broken = AsyncMock()
        broken.load_all.side_effect = RuntimeError("db gone")
        svc = NotificationService(event_bus=event_bus, bot=mock_bot, pin_store=broken)
        await svc._load_pins()
        assert svc._pinned_messages == {}

    async def test_repository_round_trip(self, pins):
        await pins.set(1, "a", 10)
        await pins.set(1, "b", 11)
        await pins.set(1, "a", 12)  # upsert
        assert await pins.load_all() == {(1, "a"): 12, (1, "b"): 11}
        await pins.delete(1, "a")
        await pins.delete(1, "missing")  # no-op
        assert await pins.load_all() == {(1, "b"): 11}


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
            ref=None,
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


class TestLoneSurrogates:
    """dv-c9d1: a str with lone surrogates (bgjob's byte-cut tail, decoded by
    argv's surrogateescape) must still be delivered -- sanitized to U+FFFD --
    instead of dying in PTB's UTF-8 encode."""

    BROKEN = "ok …\udc80\udc94 `ensure_workspace_for` tail"

    @staticmethod
    def _strict_encode(**kwargs):
        """Mimic PTB: encoding the request body raises on lone surrogates,
        and PTB re-raises that as a NetworkError (a TelegramError)."""
        from unittest.mock import MagicMock

        from telegram.error import NetworkError

        try:
            kwargs["text"].encode("utf-8")
        except UnicodeEncodeError as e:
            raise NetworkError(f"Unknown error in HTTP implementation: {e!r}")
        sent = MagicMock()
        sent.message_id = 55
        sent.chat.id = kwargs["chat_id"]
        return sent

    async def _deliver(self, service: NotificationService, event) -> None:
        """Real entry point: handle_response queues, the sender delivers."""
        await service.handle_response(event)
        queued = service._send_queue.get_nowait()
        for chat_id in service._resolve_chat_ids(queued):
            await service._rate_limited_send(chat_id, queued)

    async def test_lone_surrogates_delivered_with_replacement_char(
        self, service: NotificationService, mock_bot: AsyncMock
    ) -> None:
        mock_bot.send_message.side_effect = self._strict_encode
        await self._deliver(service, AgentResponseEvent(chat_id=100, text=self.BROKEN))

        mock_bot.send_message.assert_called_once()
        text = mock_bot.send_message.call_args.kwargs["text"]
        text.encode("utf-8")  # must not raise
        assert "�" in text
        assert text.startswith("ok …")
        assert text.endswith("`ensure_workspace_for` tail")

    async def test_pinned_new_status_with_lone_surrogates_is_sent(
        self, service: NotificationService, mock_bot: AsyncMock
    ) -> None:
        mock_bot.send_message.side_effect = self._strict_encode
        await self._deliver(
            service,
            AgentResponseEvent(chat_id=100, text=self.BROKEN, pin_as="jobs"),
        )

        mock_bot.send_message.assert_called_once()
        text = mock_bot.send_message.call_args.kwargs["text"]
        text.encode("utf-8")
        assert "�" in text
        mock_bot.pin_chat_message.assert_awaited_once()
        assert service._pinned_messages == {(100, "jobs"): 55}

    async def test_pinned_existing_status_with_lone_surrogates_is_edited(
        self, service: NotificationService, mock_bot: AsyncMock
    ) -> None:
        from telegram.error import NetworkError

        async def strict_edit(**kwargs):
            try:
                kwargs["text"].encode("utf-8")
            except UnicodeEncodeError as e:
                raise NetworkError(f"Unknown error in HTTP implementation: {e!r}")

        mock_bot.edit_message_text.side_effect = strict_edit
        mock_bot.send_message.side_effect = self._strict_encode
        service._pinned_messages[(100, "jobs")] = 9

        await self._deliver(
            service,
            AgentResponseEvent(chat_id=100, text=self.BROKEN, pin_as="jobs"),
        )

        mock_bot.edit_message_text.assert_awaited_once()
        text = mock_bot.edit_message_text.call_args.kwargs["text"]
        text.encode("utf-8")
        assert "�" in text
        assert mock_bot.edit_message_text.call_args.kwargs["message_id"] == 9
        # Edited in place, not dropped and re-created.
        mock_bot.send_message.assert_not_called()
        assert service._pinned_messages == {(100, "jobs"): 9}

    async def test_valid_emoji_and_em_dash_pass_unchanged(
        self, service: NotificationService, mock_bot: AsyncMock
    ) -> None:
        mock_bot.send_message.side_effect = self._strict_encode
        text = '✅ devops · job "t" finished — 😀 👨‍👩‍👧 done…'
        await self._deliver(service, AgentResponseEvent(chat_id=100, text=text))

        mock_bot.send_message.assert_called_once()
        assert mock_bot.send_message.call_args.kwargs["text"] == text

    async def test_text_of_only_lone_surrogates_still_delivered(
        self, service: NotificationService, mock_bot: AsyncMock
    ) -> None:
        """added: the degenerate case -- nothing valid left -- is still sent."""
        mock_bot.send_message.side_effect = self._strict_encode
        await self._deliver(
            service, AgentResponseEvent(chat_id=100, text="\udc80\udc94")
        )

        mock_bot.send_message.assert_called_once()
        text = mock_bot.send_message.call_args.kwargs["text"]
        text.encode("utf-8")
        assert text and set(text) == {"�"}


class TestMarkdownMode:
    async def test_bold_becomes_html(
        self, service: NotificationService, mock_bot: AsyncMock
    ) -> None:
        ev = AgentResponseEvent(chat_id=1, text="**hi** there", parse_mode="markdown")
        await service._rate_limited_send(1, ev)
        kw = mock_bot.send_message.call_args.kwargs
        assert kw["text"] == "<b>hi</b> there"
        assert str(kw["parse_mode"]).lower().endswith("html")

    async def test_parse_error_falls_back_to_plain_original(
        self, service: NotificationService, mock_bot: AsyncMock
    ) -> None:
        mock_bot.send_message.side_effect = [
            BadRequest("Can't parse entities: bad tag"),
            AsyncMock(),
        ]
        ev = AgentResponseEvent(chat_id=1, text="**hi**", parse_mode="markdown")
        await service._rate_limited_send(1, ev)
        assert mock_bot.send_message.call_count == 2
        last = mock_bot.send_message.call_args.kwargs
        assert last["text"] == "**hi**" and "parse_mode" not in last

    async def test_long_text_splits_before_conversion(
        self, service: NotificationService, mock_bot: AsyncMock, monkeypatch
    ) -> None:
        monkeypatch.setattr("src.notifications.service.SEND_INTERVAL_SECONDS", 0)
        text = "\n\n".join(f"**para {i}** " + "x" * 300 for i in range(30))
        ev = AgentResponseEvent(chat_id=1, text=text, parse_mode="markdown")
        await service._rate_limited_send(1, ev)
        sent = [c.kwargs["text"] for c in mock_bot.send_message.call_args_list]
        assert len(sent) > 1
        for t in sent:
            assert len(t) <= 4096
            assert t.count("<b>") == t.count("</b>")

    def test_open_fence_is_closed_and_reopened(
        self, service: NotificationService
    ) -> None:
        text = "```\n" + "\n".join("line %d" % i for i in range(40))
        parts = service._split_markdown(text, 100)
        assert len(parts) > 1
        assert all(p.count("```") % 2 == 0 for p in parts)
