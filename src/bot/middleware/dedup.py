"""Inbound dedup gate (dv-002b): one owner message starts at most one turn.

Registered once in ``ClaudeCodeBot._add_middleware`` ahead of every other
handler group. Two guards, both on ``update.message`` only (edits pass):

- ``same_message_id``: an update whose (chat_id, message_id) was already seen.
- ``burst_repeat``: a non-command text identical to one the same user sent in
  the same chat and thread less than ``BURST_WINDOW_SECONDS`` ago.

``check`` is synchronous on purpose: with ``concurrent_updates`` the copies run
as parallel tasks, and an await between lookup and insert would let all pass.
State is in memory only: polling drops pending updates on start.
"""

import time
from collections import OrderedDict
from typing import Hashable, Optional

import structlog
from telegram import Update

logger = structlog.get_logger()

BURST_WINDOW_SECONDS = 5
SEEN_MAX_ENTRIES = 2000
SEEN_MAX_AGE_SECONDS = 3600


class _BoundedSeen:
    """Insertion-ordered key -> monotonic time, capped by size and age."""

    def __init__(self, max_entries: int, max_age: float) -> None:
        self._items: "OrderedDict[Hashable, float]" = OrderedDict()
        self._max_entries = max_entries
        self._max_age = max_age

    def _prune(self, now: float) -> None:
        while self._items:
            _, ts = next(iter(self._items.items()))
            if now - ts <= self._max_age and len(self._items) <= self._max_entries:
                break
            self._items.popitem(last=False)

    def get(self, key: Hashable, now: float) -> Optional[float]:
        self._prune(now)
        return self._items.get(key)

    def put(self, key: Hashable, now: float) -> None:
        self._items[key] = now
        self._items.move_to_end(key)
        self._prune(now)

    def __len__(self) -> int:
        return len(self._items)


class InboundDedup:
    """Seen-sets for message ids and recent texts; ``check`` decides drops."""

    def __init__(self) -> None:
        self._ids = _BoundedSeen(SEEN_MAX_ENTRIES, SEEN_MAX_AGE_SECONDS)
        self._texts = _BoundedSeen(SEEN_MAX_ENTRIES, BURST_WINDOW_SECONDS)

    def check(self, update: Update) -> Optional[str]:
        """Return a drop reason, or None and record the message as seen."""
        message = update.message
        if message is None:
            return None
        now = time.monotonic()
        chat_id = message.chat_id
        id_key = (chat_id, message.message_id)
        if self._ids.get(id_key, now) is not None:
            return "same_message_id"
        self._ids.put(id_key, now)

        text = message.text
        if not text or text.startswith("/"):
            return None
        user_id = message.from_user.id if message.from_user else None
        text_key = (chat_id, user_id, message.message_thread_id, text)
        last = self._texts.get(text_key, now)
        if last is not None and now - last < BURST_WINDOW_SECONDS:
            return "burst_repeat"
        self._texts.put(text_key, now)
        return None


def log_drop(update: Update, reason: str) -> None:
    """One structlog line per dropped update, with the ids to diagnose it."""
    message = update.message
    logger.warning(
        "Dropped repeated inbound message",
        reason=reason,
        update_id=update.update_id,
        chat_id=message.chat_id if message else None,
        message_id=message.message_id if message else None,
        thread_id=message.message_thread_id if message else None,
    )
