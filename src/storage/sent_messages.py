"""Ledger of Telegram messages this bot sent (dv-7783).

Maps ``(telegram_chat_id, telegram_message_id)`` to what the send WAS: an
agent turn reply (joinable to ``messages``) or a system notification the
agent never wrote. The reply-context builder resolves quoted messages here
instead of fuzzy-matching text — chunked sends, badges and edited messages
make text matching unreliable.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, List, Optional

import structlog

if TYPE_CHECKING:
    from .database import DatabaseManager

logger = structlog.get_logger()


@dataclass
class SentMessage:
    id: int
    telegram_chat_id: int
    telegram_message_id: int
    kind: str  # 'turn_reply' | 'system_notify'
    messages_message_id: Optional[int]
    profile_slug: Optional[str]
    created_at: Optional[datetime]
    # What the message is about, e.g. 'job:<id>' (dv-c41e); None = nothing.
    ref: Optional[str] = None


class SentMessageRepository:
    """CRUD over the sent_messages ledger."""

    def __init__(self, db: "DatabaseManager") -> None:
        self._db = db

    async def record(
        self,
        telegram_chat_id: int,
        telegram_message_id: int,
        kind: str,
        messages_message_id: Optional[int] = None,
        profile_slug: Optional[str] = None,
        ref: Optional[str] = None,
    ) -> None:
        """Record one sent message. Never raises — a ledger failure must not
        break message delivery (it degrades the NEXT reply's attribution,
        nothing else)."""
        try:
            async with self._db.get_connection() as conn:
                await conn.execute(
                    "INSERT OR REPLACE INTO sent_messages "
                    "(telegram_chat_id, telegram_message_id, kind, "
                    "messages_message_id, profile_slug, ref) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        telegram_chat_id,
                        telegram_message_id,
                        kind,
                        messages_message_id,
                        profile_slug,
                        ref,
                    ),
                )
                await conn.commit()
        except Exception as e:
            logger.warning(
                "sent_messages ledger write failed",
                chat_id=telegram_chat_id,
                tg_message_id=telegram_message_id,
                error=str(e),
            )

    async def lookup(
        self, telegram_chat_id: int, telegram_message_id: int
    ) -> Optional[SentMessage]:
        try:
            async with self._db.get_connection() as conn:
                cur = await conn.execute(
                    "SELECT id, telegram_chat_id, telegram_message_id, kind, "
                    "messages_message_id, profile_slug, created_at, ref "
                    "FROM sent_messages "
                    "WHERE telegram_chat_id=? AND telegram_message_id=?",
                    (telegram_chat_id, telegram_message_id),
                )
                row = await cur.fetchone()
                return SentMessage(*row) if row else None
        except Exception as e:
            logger.warning("sent_messages ledger lookup failed", error=str(e))
            return None

    async def list_for_message(self, messages_message_id: int) -> List[SentMessage]:
        """All ``turn_reply`` sends ledgered for one ``messages`` row,
        oldest first (``id`` is autoincrement, so insertion order).

        Used by the recipe search-retry edit-in-place delivery (dv-e7fa) to
        find the LAST physical Telegram message of a turn reply that may
        have been chunked across several messages, and to detect that it
        was chunked at all.
        """
        try:
            async with self._db.get_connection() as conn:
                cur = await conn.execute(
                    "SELECT id, telegram_chat_id, telegram_message_id, kind, "
                    "messages_message_id, profile_slug, created_at, ref "
                    "FROM sent_messages "
                    "WHERE messages_message_id=? AND kind='turn_reply' "
                    "ORDER BY id ASC",
                    (messages_message_id,),
                )
                rows = await cur.fetchall()
                return [SentMessage(*row) for row in rows]
        except Exception as e:
            logger.warning("sent_messages ledger list failed", error=str(e))
            return []
