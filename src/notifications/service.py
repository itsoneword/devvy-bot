"""Notification service for delivering proactive agent responses to Telegram.

Subscribes to AgentResponseEvent on the event bus and delivers messages
through the Telegram bot API with rate limiting (1 msg/sec per chat).
"""

import asyncio
import re
from typing import Any, List, Optional

import structlog
from telegram import Bot
from telegram.constants import ParseMode
from telegram.error import TelegramError

from ..events.bus import Event, EventBus
from ..events.types import AgentResponseEvent

logger = structlog.get_logger()

# Telegram rate limit: ~30 msgs/sec globally, ~1 msg/sec per chat
SEND_INTERVAL_SECONDS = 1.1

# Lone UTF-16 surrogate halves. A str that reached us still holding one came
# from a byte cut mid-character upstream (decoded with surrogateescape); it
# cannot be UTF-8 encoded, so the HTTP send fails and the message is lost
# (dv-c9d1). Real pairs never survive into a Python str, so this is safe.
_LONE_SURROGATE = re.compile("[\ud800-\udfff]")


def _sanitize_text(text: str) -> str:
    """Replace lone surrogates with U+FFFD so the text is always sendable."""
    return _LONE_SURROGATE.sub("\ufffd", text)


class NotificationService:
    """Delivers agent responses to Telegram chats with rate limiting."""

    def __init__(
        self,
        event_bus: EventBus,
        bot: Bot,
        default_chat_ids: Optional[List[int]] = None,
        pin_store: Optional[Any] = None,
        sent_messages: Optional[Any] = None,
    ) -> None:
        self.event_bus = event_bus
        self.bot = bot
        self.default_chat_ids = default_chat_ids or []
        # sent_messages ledger repo: events carrying ledger_kind (verbatim
        # scheduled reminders) get each delivered Telegram message recorded
        # so a later reply to it can be attributed and rerouted.
        self.sent_messages = sent_messages
        self._send_queue: asyncio.Queue[AgentResponseEvent] = asyncio.Queue()
        self._last_send_per_chat: dict[int, float] = {}
        # (chat_id, pin_as) -> message_id of the live status post we maintain.
        # Persisted in bot.db (PinnedMessageRepository, dv-ebde): without it
        # a bot restart orphans the current pin (can never be edited/unpinned
        # again) and the next job pins a second. Loaded in start().
        self._pin_store = pin_store
        self._pinned_messages: dict[tuple[int, str], int] = {}
        self._running = False
        self._sender_task: Optional[asyncio.Task[None]] = None

    async def _load_pins(self) -> None:
        if self._pin_store is None:
            return
        try:
            self._pinned_messages = await self._pin_store.load_all()
        except Exception as e:
            logger.warning("pin state unreadable, starting empty", error=str(e))
            self._pinned_messages = {}

    async def _save_pin(self, key: tuple[int, str]) -> None:
        """Write one key's current state (present = upsert, absent = delete)."""
        if self._pin_store is None:
            return
        chat_id, pin_as = key
        try:
            message_id = self._pinned_messages.get(key)
            if message_id is None:
                await self._pin_store.delete(chat_id, pin_as)
            else:
                await self._pin_store.set(chat_id, pin_as, message_id)
        except Exception as e:
            logger.warning("pin state not saved", error=str(e))

    def register(self) -> None:
        """Subscribe to agent response events."""
        self.event_bus.subscribe(AgentResponseEvent, self.handle_response)

    async def start(self) -> None:
        """Start the send queue processor."""
        if self._running:
            return
        await self._load_pins()
        self._running = True
        self._sender_task = asyncio.create_task(self._process_send_queue())
        logger.info("Notification service started")

    async def stop(self) -> None:
        """Stop the send queue processor."""
        if not self._running:
            return
        self._running = False
        if self._sender_task:
            self._sender_task.cancel()
            try:
                await self._sender_task
            except asyncio.CancelledError:
                pass
        logger.info("Notification service stopped")

    async def handle_response(self, event: Event) -> None:
        """Queue an agent response for delivery."""
        if not isinstance(event, AgentResponseEvent):
            return
        # Once, at queue time: covers the plain and the pinned send paths.
        event.text = _sanitize_text(event.text)
        await self._send_queue.put(event)

    async def _process_send_queue(self) -> None:
        """Process queued messages with rate limiting."""
        while self._running:
            try:
                event = await asyncio.wait_for(self._send_queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

            chat_ids = self._resolve_chat_ids(event)
            for chat_id in chat_ids:
                await self._rate_limited_send(chat_id, event)

    def _resolve_chat_ids(self, event: AgentResponseEvent) -> List[int]:
        """Determine which chats to send to."""
        if event.chat_id and event.chat_id != 0:
            return [event.chat_id]
        return list(self.default_chat_ids)

    async def _rate_limited_send(self, chat_id: int, event: AgentResponseEvent) -> None:
        """Send message with per-chat rate limiting."""
        loop = asyncio.get_event_loop()
        now = loop.time()
        last_send = self._last_send_per_chat.get(chat_id, 0.0)
        wait_time = SEND_INTERVAL_SECONDS - (now - last_send)

        if wait_time > 0:
            await asyncio.sleep(wait_time)

        if event.pin_as:
            await self._send_or_edit_pinned(chat_id, event)
            return

        try:
            # Split long messages (Telegram limit: 4096 chars)
            text = event.text
            chunks = self._split_message(text)

            for chunk in chunks:
                sent = await self.bot.send_message(
                    chat_id=chat_id,
                    text=chunk,
                    parse_mode=(ParseMode.HTML if event.parse_mode == "HTML" else None),
                )
                self._last_send_per_chat[chat_id] = asyncio.get_event_loop().time()
                if event.ledger_kind and self.sent_messages is not None:
                    # Every chunk is ledgered — a reply can target any of them.
                    await self.sent_messages.record(
                        telegram_chat_id=sent.chat.id,
                        telegram_message_id=sent.message_id,
                        kind=event.ledger_kind,
                        messages_message_id=event.ledger_messages_message_id,
                        profile_slug=event.ledger_profile_slug,
                    )

                # Rate limit between chunks too
                if len(chunks) > 1:
                    await asyncio.sleep(SEND_INTERVAL_SECONDS)

            logger.info(
                "Notification sent",
                chat_id=chat_id,
                text_length=len(text),
                chunks=len(chunks),
                originating_event=event.originating_event_id,
            )
        except TelegramError as e:
            logger.error(
                "Failed to send notification",
                chat_id=chat_id,
                error=str(e),
                event_id=event.id,
            )

    async def _send_or_edit_pinned(
        self, chat_id: int, event: AgentResponseEvent
    ) -> None:
        """Maintain ONE pinned status message per (chat, pin_as) key.

        A long job's state has to be readable without scrolling, and pinning
        every start/finish ping would fight over the pin. So the status lives
        in a single message that gets edited in place; empty text retires it
        (unpin + forget) once nothing is running.
        """
        key = (chat_id, event.pin_as or "")
        existing = self._pinned_messages.get(key)
        text = event.text.strip()

        if not text:
            if existing:
                try:
                    await self.bot.unpin_chat_message(
                        chat_id=chat_id, message_id=existing
                    )
                except TelegramError:
                    pass
                try:
                    await self.bot.delete_message(chat_id=chat_id, message_id=existing)
                except TelegramError:
                    pass
                self._pinned_messages.pop(key, None)
                await self._save_pin(key)
            return

        if existing:
            try:
                await self.bot.edit_message_text(
                    chat_id=chat_id, message_id=existing, text=text
                )
                return
            except TelegramError as e:
                # Message gone (user deleted it) or unchanged text -- only
                # re-create in the former case.
                if "not modified" in str(e).lower():
                    return
                self._pinned_messages.pop(key, None)
                await self._save_pin(key)

        try:
            msg = await self.bot.send_message(chat_id=chat_id, text=text)
            self._pinned_messages[key] = msg.message_id
            await self._save_pin(key)
            await self.bot.pin_chat_message(
                chat_id=chat_id,
                message_id=msg.message_id,
                disable_notification=True,
            )
        except TelegramError as e:
            logger.warning("pinned status update failed", chat_id=chat_id, error=str(e))

    def _split_message(self, text: str, max_length: int = 4096) -> List[str]:
        """Split long messages at paragraph boundaries."""
        if len(text) <= max_length:
            return [text]

        chunks: List[str] = []
        while text:
            if len(text) <= max_length:
                chunks.append(text)
                break

            # Try to split at a paragraph boundary
            split_pos = text.rfind("\n\n", 0, max_length)
            if split_pos == -1:
                # Try single newline
                split_pos = text.rfind("\n", 0, max_length)
            if split_pos == -1:
                # Try space
                split_pos = text.rfind(" ", 0, max_length)
            if split_pos == -1:
                # Hard split
                split_pos = max_length

            chunks.append(text[:split_pos])
            text = text[split_pos:].lstrip()

        return chunks
