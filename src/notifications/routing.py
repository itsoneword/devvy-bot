"""Reports-group routing for out-of-band notifications (dv-e215).

Decides, per AgentResponseEvent, whether it goes to a topic of the reports
forum supergroup (``REPORTS_CHAT_ID``) instead of the owner's DM, and owns the
topic lifecycle (create on first use, persist, recreate once if deleted).

The decision is made from ``event.source`` only. Anything that waits for the
owner, and any message addressed to a specific non-owner chat, stays in the DM.
Topic ids are persisted in ``project_threads`` (slug ``reports:<topic>``) through
``ProjectThreadRepository`` (the table predates this feature; it is the only
topic store).
"""

import asyncio
import time
from typing import Any, Dict, Iterable, Optional

import structlog
from telegram import Bot
from telegram.error import TelegramError

from ..events.types import AgentResponseEvent

logger = structlog.get_logger()

_TOPIC_UNUSABLE_MARKERS = (
    "topic deleted",
    "topic was deleted",
    "topic_closed",
    "topic closed",
    "message thread not found",
    "thread not found",
    "invalid message thread id",
    "forum topic not found",
)


def is_topic_unusable_error(error: TelegramError) -> bool:
    """True when the topic no longer exists or the thread id is invalid."""
    text = str(error).lower()
    return any(marker in text for marker in _TOPIC_UNUSABLE_MARKERS)


SLUG_PREFIX = "reports:"

# Sources that are DM-only no matter what the routing map says: the owner has
# to see them on the device they answer from. "taskpump:ask" is the pump's
# question/blocked/failed card (status cards are "taskpump:status"); the rest
# are reminders / item jobs, family notices and the routing's own errors.
# Interactive prompts (tool-call approvals, watchdog) are sent directly by the
# bot with inline keyboards and never pass through this service at all.
ALWAYS_DM_SOURCES = (
    "taskpump:ask",
    "reminder",
    "item",
    "family",
    "approval",
    "policy",
    "routing",
)


def source_matches(source: str, key: str) -> bool:
    """True when ``source`` equals ``key`` or sits under it (``key:...``)."""
    return source == key or source.startswith(key + ":")


# A plain post in a reply-only topic gets one hint per this many seconds.
HINT_INTERVAL_S = 600.0
JOBS_HINT = (
    "This topic is read-only: reply to a job message (start, finish or "
    "digest) to talk to its agent."
)


class ReportsRouter:
    """source -> (reports chat, topic thread id), with persisted topics."""

    def __init__(
        self,
        chat_id: Optional[int],
        routes: Dict[str, str],
        repository: Any,
        owner_chat_ids: Iterable[int] = (),
        bindings: Optional[Dict[str, str]] = None,
        inbound_policy: str = "owner",
        reply_only: Iterable[str] = (),
        ledger: Any = None,
    ) -> None:
        self.chat_id = chat_id
        self.routes = {k.lower(): v for k, v in (routes or {}).items()}
        self.repository = repository
        self.owner_chat_ids = set(owner_chat_ids)
        # Inbound (v2): topic name -> profile slug. The same topics (and the
        # same persisted thread ids) as the outbound routes use.
        self.bindings = {k: v.lower() for k, v in (bindings or {}).items()}
        self.inbound_policy = inbound_policy if inbound_policy == "allowed" else "owner"
        # Reply-only topics (dv-c41e, Jobs): only a reply to a ledgered message
        # tagged "job:<id>" is inbound; the turn runs on that message's profile.
        reply_only_l = {t.lower() for t in reply_only}
        self.reply_only = {t for t in self.bindings if t.lower() in reply_only_l}
        self.ledger = ledger
        self._hint_at: Dict[str, float] = {}
        self._threads: Dict[str, int] = {}
        self._lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self.chat_id) and bool(self.routes)

    @property
    def inbound_enabled(self) -> bool:
        return bool(self.chat_id) and bool(self.bindings)

    def sender_allowed(self, role: Optional[str]) -> bool:
        """Policy gate for who may write to bound topics."""
        if self.inbound_policy == "allowed":
            return role is not None
        return role == "owner"

    async def ensure_bound_topics(self, bot: Bot) -> None:
        """Create the bound topics that do not exist yet (startup, best effort)."""
        for topic in self.bindings:
            try:
                await self.thread_id(bot, topic)
            except Exception as e:
                logger.warning("Bound topic unavailable", topic=topic, error=str(e))

    async def topic_name_for_thread(
        self, chat_id: Optional[int], thread_id: Optional[int]
    ) -> Optional[str]:
        """Bound topic name for this (chat, topic), or None = unbound."""
        if not self.inbound_enabled or chat_id != self.chat_id or not thread_id:
            return None
        for topic in self.bindings:
            known = self._threads.get(topic)
            if known is None:
                row = await self.repository.get_by_chat_project(
                    self.chat_id, SLUG_PREFIX + topic.lower()
                )
                if row and row.is_active:
                    known = int(row.message_thread_id)
                    self._threads[topic] = known
            if known == thread_id:
                return topic
        return None

    async def profile_for_thread(
        self, chat_id: Optional[int], thread_id: Optional[int]
    ) -> Optional[str]:
        """Profile slug bound to this (chat, topic), or None = unbound/ignored."""
        topic = await self.topic_name_for_thread(chat_id, thread_id)
        return self.bindings[topic] if topic else None

    async def profile_for_message(self, message: Any) -> Optional[str]:
        """Profile slug this message is routed to, or None = ignored.

        The one lookup behind the inbound gate, the turn-lock key and the
        turn reroute, so they cannot disagree. Normal bound topic: the static
        binding. Reply-only topic: the profile recorded for the job message
        being replied to (a ledger hit tagged ``job:``); a topic's service
        message as ``reply_to_message`` or an unledgered target gives None.
        """
        topic = await self.topic_name_for_thread(
            message.chat_id,
            message.message_thread_id if message.is_topic_message else None,
        )
        if topic is None:
            return None
        if topic not in self.reply_only:
            return self.bindings[topic]
        entry = await self._job_entry(message)
        if entry is None:
            return None
        return (entry.profile_slug or self.bindings[topic]).lower()

    async def _job_entry(self, message: Any) -> Any:
        quoted = getattr(message, "reply_to_message", None)
        if quoted is None or self.ledger is None:
            return None
        try:
            entry = await self.ledger.lookup(quoted.chat.id, quoted.message_id)
        except Exception as e:
            logger.warning("Jobs reply ledger lookup failed", error=str(e))
            return None
        ref = getattr(entry, "ref", None)
        return entry if isinstance(ref, str) and ref.startswith("job:") else None

    def hint_due(self, topic: str, now: Optional[float] = None) -> bool:
        """True at most once per ``HINT_INTERVAL_S`` per topic (and records it)."""
        t = time.monotonic() if now is None else now
        last = self._hint_at.get(topic)
        if last is not None and t - last < HINT_INTERVAL_S:
            return False
        self._hint_at[topic] = t
        return True

    def topic_for(
        self, event: AgentResponseEvent, target_chat_id: int
    ) -> Optional[str]:
        """Topic name for this event, or None = stay where it was addressed."""
        if not self.enabled or target_chat_id == self.chat_id:
            return None
        if event.ledger_kind:  # reminders: replies must be attributable in DM
            return None
        # Only the owner's notification chats (NOTIFICATION_CHAT_IDS) have reports;
        # a message for another person's chat is not the owner's report.
        if target_chat_id not in self.owner_chat_ids:
            return None
        source = (event.source or "").lower()
        if any(source_matches(source, k) for k in ALWAYS_DM_SOURCES):
            return None
        best = ""
        for key in self.routes:
            if source_matches(source, key) and len(key) > len(best):
                best = key
        return self.routes[best] if best else None

    async def thread_id(self, bot: Bot, topic: str, recreate: bool = False) -> int:
        """Thread id for ``topic``; creates (and persists) it when missing."""
        assert self.chat_id is not None
        slug = SLUG_PREFIX + topic.lower()
        async with self._lock:
            if recreate:
                self._threads.pop(topic, None)
            elif topic in self._threads:
                return self._threads[topic]

            if not recreate:
                row = await self.repository.get_by_chat_project(self.chat_id, slug)
                if row and row.is_active:
                    self._threads[topic] = row.message_thread_id
                    return int(row.message_thread_id)
            created = await bot.create_forum_topic(chat_id=self.chat_id, name=topic)
            await self.repository.upsert_mapping(
                project_slug=slug,
                chat_id=self.chat_id,
                message_thread_id=created.message_thread_id,
                topic_name=topic,
                is_active=True,
            )
            logger.info(
                "Reports topic created",
                topic=topic,
                chat_id=self.chat_id,
                message_thread_id=created.message_thread_id,
            )
            self._threads[topic] = created.message_thread_id
            return created.message_thread_id

    @staticmethod
    def is_thread_gone(error: Exception) -> bool:
        return isinstance(error, TelegramError) and (is_topic_unusable_error(error))
