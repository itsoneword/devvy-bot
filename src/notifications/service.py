"""Notification service for delivering proactive agent responses to Telegram.

Subscribes to AgentResponseEvent on the event bus and delivers messages
through the Telegram bot API with rate limiting (1 msg/sec per chat).
"""

import asyncio
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, List, Optional

import structlog
from telegram import Bot
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError

from ..bot.utils.html_format import markdown_to_telegram_html
from ..events.bus import Event, EventBus
from ..events.types import AgentResponseEvent
from .routing import ReportsRouter

logger = structlog.get_logger()

# Telegram rate limit: ~30 msgs/sec globally, ~1 msg/sec per chat
SEND_INTERVAL_SECONDS = 1.1

# Lone UTF-16 surrogate halves. A str that reached us still holding one came
# from a byte cut mid-character upstream (decoded with surrogateescape); it
# cannot be UTF-8 encoded, so the HTTP send fails and the message is lost
# (dv-c9d1). Real pairs never survive into a Python str, so this is safe.
_LONE_SURROGATE = re.compile("[\ud800-\udfff]")

# parse_mode value meaning "model markdown": converted to Telegram HTML here,
# the one place out-of-band messages get formatted.
MARKDOWN_MODE = "markdown"
# Markdown is split BEFORE conversion (so a cut can never land inside a tag);
# headroom because HTML escaping and tags grow the text.
_MD_CHUNK = 3400


def _sanitize_text(text: str) -> str:
    """Replace lone surrogates with U+FFFD so the text is always sendable."""
    return _LONE_SURROGATE.sub("\ufffd", text)


# Edit errors that mean the pinned message no longer exists (dv-3d3d).
_PIN_GONE_MARKERS = (
    "message to edit not found",
    "message can't be edited",
    "message_id_invalid",
    "not found",
)


class NotificationService:
    """Delivers agent responses to Telegram chats with rate limiting."""

    def __init__(
        self,
        event_bus: EventBus,
        bot: Bot,
        default_chat_ids: Optional[List[int]] = None,
        pin_store: Optional[Any] = None,
        sent_messages: Optional[Any] = None,
        reports_router: Optional[ReportsRouter] = None,
    ) -> None:
        self.event_bus = event_bus
        self.bot = bot
        self.default_chat_ids = default_chat_ids or []
        # sent_messages ledger repo: events carrying ledger_kind (verbatim
        # scheduled reminders) get each delivered Telegram message recorded
        # so a later reply to it can be attributed and rerouted.
        self.sent_messages = sent_messages
        # Optional (dv-e215): sends report-type sources to topics of the
        # reports group. None / disabled = every message goes where it was
        # addressed, exactly as before.
        self.reports_router = reports_router
        self._send_queue: asyncio.Queue[AgentResponseEvent] = asyncio.Queue()
        self._last_send_per_chat: dict[int, float] = {}
        # (chat_id, pin_as) -> message_id of the live status post we maintain.
        # Persisted in bot.db (PinnedMessageRepository, dv-ebde): without it
        # a bot restart orphans the current pin (can never be edited/unpinned
        # again) and the next job pins a second. Loaded in start().
        self._pin_store = pin_store
        self._pinned_messages: dict[tuple[int, str], int] = {}
        # (chat_id, pin_as) -> UTC date (YYYY-MM-DD) the current pin message was
        # created. Persisted as pinned_messages.updated_at; gates the daily
        # repin so a restart or a retry never repins twice in a day (dv-6646).
        self._pin_dates: dict[tuple[int, str], str] = {}
        self._running = False
        self._sender_task: Optional[asyncio.Task[None]] = None

    async def _load_pins(self) -> None:
        if self._pin_store is None:
            return
        try:
            self._pinned_messages = await self._pin_store.load_all()
            self._pin_dates = await self._pin_store.load_dates()
        except Exception as e:
            logger.warning("pin state unreadable, starting empty", error=str(e))
            self._pinned_messages = {}
            self._pin_dates = {}

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
                await self._deliver(chat_id, event)

    async def _deliver(self, chat_id: int, event: AgentResponseEvent) -> None:
        """Send to the reports topic for this source if routed, else to chat_id.

        Any failure of the routed send (topic can't be made, group gone, bot
        removed) falls back to the original chat so a report is never lost.
        """
        router = self.reports_router
        topic = router.topic_for(event, chat_id) if router else None
        if event.deliver_profile and topic is None and not event.ledger_kind:
            # DM fallback of a profile-addressed delivery: ledgered like a
            # reminder so a reply to it runs on that profile.
            event.ledger_kind = "scheduled_reminder"
        if router is None or topic is None:
            await self._rate_limited_send(chat_id, event)
            return
        reports_chat = router.chat_id
        assert reports_chat is not None
        try:
            thread_id = await router.thread_id(self.bot, topic)
            try:
                await self._rate_limited_send(
                    reports_chat, event, thread_id=thread_id, reraise=True
                )
                return
            except TelegramError as e:
                if not router.is_thread_gone(e):
                    raise
                logger.warning("Reports topic gone, recreating", topic=topic)
                thread_id = await router.thread_id(self.bot, topic, recreate=True)
                await self._rate_limited_send(
                    reports_chat, event, thread_id=thread_id, reraise=True
                )
                return
        except Exception as e:
            logger.error(
                "Reports routing failed, falling back to DM",
                topic=topic,
                source=event.source,
                error=str(e),
            )
        await self._rate_limited_send(chat_id, event)

    def _resolve_chat_ids(self, event: AgentResponseEvent) -> List[int]:
        """Determine which chats to send to."""
        if event.chat_id and event.chat_id != 0:
            return [event.chat_id]
        return list(self.default_chat_ids)

    async def _rate_limited_send(
        self,
        chat_id: int,
        event: AgentResponseEvent,
        thread_id: Optional[int] = None,
        reraise: bool = False,
    ) -> None:
        """Send message with per-chat rate limiting.

        ``thread_id`` targets a forum topic; ``reraise`` hands Telegram errors
        to the caller (reports routing falls back to the DM) instead of logging.
        """
        loop = asyncio.get_event_loop()
        now = loop.time()
        last_send = self._last_send_per_chat.get(chat_id, 0.0)
        wait_time = SEND_INTERVAL_SECONDS - (now - last_send)

        if wait_time > 0:
            await asyncio.sleep(wait_time)

        if event.pin_as:
            await self._send_or_edit_pinned(chat_id, event, thread_id, reraise)
            return
        topic_kw: dict[str, Any] = (
            {"message_thread_id": thread_id} if thread_id is not None else {}
        )

        try:
            # Split long messages (Telegram limit: 4096 chars)
            text = event.text
            is_md = event.parse_mode == MARKDOWN_MODE
            chunks = (
                self._split_markdown(text, _MD_CHUNK)
                if is_md
                else self._split_message(text)
            )
            # An image-only reply (dv-ff39) has no text; Telegram rejects an
            # empty message and the error would skip the images.
            chunks = [c for c in chunks if c.strip()]

            for chunk in chunks:
                if is_md:
                    sent = await self._send_markdown_chunk(chat_id, chunk, **topic_kw)
                else:
                    sent = await self.bot.send_message(
                        chat_id=chat_id,
                        text=chunk,
                        parse_mode=(
                            ParseMode.HTML if event.parse_mode == "HTML" else None
                        ),
                        **topic_kw,
                    )
                self._last_send_per_chat[chat_id] = asyncio.get_event_loop().time()
                # Reports sent into a topic are ledgered too (as "report"),
                # so replying to one puts it in context (dv-e215 v2).
                ledger_kind = event.ledger_kind or (
                    "report" if thread_id is not None else None
                )
                if ledger_kind and self.sent_messages is not None:
                    # Every chunk is ledgered — a reply can target any of them.
                    await self.sent_messages.record(
                        telegram_chat_id=sent.chat.id,
                        telegram_message_id=sent.message_id,
                        kind=ledger_kind,
                        messages_message_id=event.ledger_messages_message_id,
                        profile_slug=event.ledger_profile_slug,
                        ref=event.ledger_ref,
                    )

                # Rate limit between chunks too
                if len(chunks) > 1:
                    await asyncio.sleep(SEND_INTERVAL_SECONDS)

            if event.image_paths:
                await self._send_images(chat_id, event.image_paths, topic_kw)

            logger.info(
                "Notification sent",
                chat_id=chat_id,
                text_length=len(text),
                chunks=len(chunks),
                originating_event=event.originating_event_id,
            )
        except TelegramError as e:
            if reraise:
                raise
            logger.error(
                "Failed to send notification",
                chat_id=chat_id,
                error=str(e),
                event_id=event.id,
            )

    async def _send_images(
        self, chat_id: int, paths: List[str], topic_kw: dict[str, Any]
    ) -> None:
        """Send already-validated files after the text (dv-f90c): raster
        photos as one album, everything else as documents. Best-effort: a
        failed image never loses the text that was already delivered."""
        from telegram import InputMediaPhoto

        from ..bot.utils.image_extractor import should_send_as_photo

        photos = [Path(p) for p in paths if should_send_as_photo(Path(p))]
        docs = [Path(p) for p in paths if not should_send_as_photo(Path(p))]
        try:
            if len(photos) == 1:
                with open(photos[0], "rb") as f:
                    await self.bot.send_photo(chat_id=chat_id, photo=f, **topic_kw)
            elif photos:
                handles = [open(p, "rb") for p in photos[:10]]  # noqa: SIM115
                try:
                    await self.bot.send_media_group(
                        chat_id=chat_id,
                        media=[InputMediaPhoto(media=h) for h in handles],
                        **topic_kw,
                    )
                finally:
                    for h in handles:
                        h.close()
            for doc in docs:
                with open(doc, "rb") as f:
                    await self.bot.send_document(
                        chat_id=chat_id, document=f, filename=doc.name, **topic_kw
                    )
        except Exception as e:  # noqa: BLE001
            logger.warning("Notification images failed", chat_id=chat_id, error=str(e))

    async def _send_markdown_chunk(
        self, chat_id: int, chunk: str, **kwargs: Any
    ) -> Any:
        """Send one markdown chunk as HTML; on a parse error send it plain."""
        try:
            return await self.bot.send_message(
                chat_id=chat_id,
                text=markdown_to_telegram_html(chunk),
                parse_mode=ParseMode.HTML,
                **kwargs,
            )
        except BadRequest as e:
            if "parse" not in str(e).lower() and "entit" not in str(e).lower():
                raise
            logger.warning("markdown html rejected, sending plain", error=str(e))
            return await self.bot.send_message(chat_id=chat_id, text=chunk, **kwargs)

    def _split_markdown(self, text: str, max_length: int) -> List[str]:
        """Split markdown without leaving a ``` fence open across chunks."""
        chunks: List[str] = []
        reopen = False
        for part in self._split_message(text, max_length):
            if reopen:
                part = "```\n" + part
            reopen = part.count("```") % 2 == 1
            if reopen:
                part = part.rstrip() + "\n```"
            chunks.append(part)
        return chunks

    async def _send_or_edit_pinned(
        self,
        chat_id: int,
        event: AgentResponseEvent,
        thread_id: Optional[int] = None,
        reraise: bool = False,
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

        if (
            existing
            and event.repin
            and self._pin_dates.get(key) != datetime.now(UTC).strftime("%Y-%m-%d")
            and await self._repin(key, existing, text, thread_id)
        ):
            return

        if existing:
            try:
                await self.bot.edit_message_text(
                    chat_id=chat_id, message_id=existing, text=text
                )
                return
            except TelegramError as e:
                # Re-create only when the message is really gone (user deleted
                # it). Anything else (timeout, 429, Bad Gateway) waits for the
                # next push: re-posting then would leave the old pin orphaned
                # next to a new one (dv-3d3d).
                err = str(e).lower()
                if "not modified" in err:
                    return
                if not any(m in err for m in _PIN_GONE_MARKERS):
                    logger.warning(
                        "pinned status edit failed", chat_id=chat_id, error=str(e)
                    )
                    return
                try:
                    await self.bot.unpin_chat_message(
                        chat_id=chat_id, message_id=existing
                    )
                except TelegramError:
                    pass
                self._pinned_messages.pop(key, None)
                await self._save_pin(key)

        try:
            topic_kw: dict[str, Any] = (
                {"message_thread_id": thread_id} if thread_id is not None else {}
            )
            msg = await self.bot.send_message(chat_id=chat_id, text=text, **topic_kw)
            self._remember_pin(key, msg.message_id)
            await self._save_pin(key)
        except TelegramError as e:
            if reraise and thread_id is not None:
                raise
            logger.warning("pinned status update failed", chat_id=chat_id, error=str(e))
            return
        await self._pin_new(chat_id, msg.message_id, thread_id)

    def _remember_pin(self, key: tuple[int, str], message_id: int) -> None:
        self._pinned_messages[key] = message_id
        self._pin_dates[key] = datetime.now(UTC).strftime("%Y-%m-%d")

    async def _repin(
        self,
        key: tuple[int, str],
        old_id: int,
        text: str,
        thread_id: Optional[int],
    ) -> bool:
        """Daily repin (dv-6646): the live status goes out as a NEW message at
        the bottom, gets pinned, the old one is unpinned. True = done; False =
        the send failed and the old pin stays (caller edits it in place)."""
        chat_id = key[0]
        topic_kw: dict[str, Any] = (
            {"message_thread_id": thread_id} if thread_id is not None else {}
        )
        try:
            msg = await self.bot.send_message(chat_id=chat_id, text=text, **topic_kw)
        except TelegramError as e:
            logger.warning("status repin failed, keeping old pin", error=str(e))
            return False
        self._remember_pin(key, msg.message_id)
        await self._save_pin(key)
        await self._pin_new(chat_id, msg.message_id, thread_id)
        try:
            await self.bot.unpin_chat_message(chat_id=chat_id, message_id=old_id)
        except TelegramError:
            pass
        return True

    async def _pin_new(
        self, chat_id: int, message_id: int, thread_id: Optional[int]
    ) -> None:
        if thread_id is not None:
            # A new pin in a bot-owned reports topic clears whatever else is
            # pinned there (e.g. the stray 15.10 pin in Pump), so the topic
            # shows exactly one status. Best-effort.
            try:
                await self.bot.unpin_all_forum_topic_messages(
                    chat_id=chat_id, message_thread_id=thread_id
                )
            except TelegramError as e:
                logger.warning("topic unpin-all failed", chat_id=chat_id, error=str(e))
        try:
            await self.bot.pin_chat_message(
                chat_id=chat_id,
                message_id=message_id,
                disable_notification=True,
            )
        except TelegramError as e:
            # Posted fine; only the pin failed (e.g. no "Pin messages" right).
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
