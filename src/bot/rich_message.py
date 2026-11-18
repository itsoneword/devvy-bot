"""Telegram Rich Messages (Bot API 10.1) through PTB's raw request path (dv-1c98).

``sendRichMessage`` takes GitHub-style markdown (tables, headings, lists,
quotes) and renders it natively. PTB 22.6 does not know the method, so this
goes through ``Bot.do_api_request`` exactly as ``telegram_widgets`` does (the
same base URL PTB uses, local Bot API server included). Goes when PTB gains
``send_rich_message``.

Streaming would use ``sendRichMessageDraft`` the way ``DraftStreamer`` uses
``sendMessageDraft``; not done here: only the final message is rich.

Callers keep their text path: :func:`try_send_rich` returns None on ANY
problem (too long, error, timeout) after logging it, and the caller then sends
the message the old way. Nothing is lost.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

import structlog

logger = structlog.get_logger()

# Bot API 10.1: markdown up to 32768 chars and 500 blocks. Blocks are not
# counted exactly; non-empty lines over-count them, so the check is safe.
MAX_CHARS = 32768
MAX_BLOCKS = 500


@dataclass(frozen=True)
class RichSent:
    """The part of the returned Message the callers use (ledger, pointers)."""

    chat_id: int
    message_id: int


def fits_rich(markdown: str) -> bool:
    """Within the documented rich limits (length, block count upper bound)."""
    if not markdown.strip() or len(markdown) > MAX_CHARS:
        return False
    return sum(1 for ln in markdown.splitlines() if ln.strip()) <= MAX_BLOCKS


async def send_rich_message(
    bot: Any,
    chat_id: int,
    markdown: str,
    *,
    message_thread_id: Optional[int] = None,
    reply_to_message_id: Optional[int] = None,
) -> RichSent:
    """``sendRichMessage``. Raises what Telegram answers (a TelegramError)."""
    # The markdown sits inside a ``rich_message`` object (probed live 2026-11-18:
    # a flat ``markdown`` field is answered "Rich message must be non-empty").
    kwargs: Dict[str, Any] = {
        "chat_id": chat_id,
        "rich_message": {"markdown": markdown},
    }
    if message_thread_id is not None:
        kwargs["message_thread_id"] = message_thread_id
    if reply_to_message_id is not None:
        kwargs["reply_parameters"] = {
            "message_id": reply_to_message_id,
            "allow_sending_without_reply": True,
        }
    result = await bot.do_api_request("sendRichMessage", api_kwargs=kwargs)
    chat = result.get("chat") if isinstance(result, dict) else None
    message_id = result.get("message_id") if isinstance(result, dict) else None
    if not isinstance(message_id, int):
        raise ValueError(f"sendRichMessage returned no message_id: {result!r}")
    sent_chat = chat.get("id") if isinstance(chat, dict) else None
    return RichSent(
        chat_id=sent_chat if isinstance(sent_chat, int) else chat_id,
        message_id=message_id,
    )


async def try_send_rich(
    bot: Any,
    chat_id: int,
    markdown: str,
    *,
    message_thread_id: Optional[int] = None,
    reply_to_message_id: Optional[int] = None,
) -> Optional[RichSent]:
    """Send *markdown* rich, or return None (logged) so the caller falls back.

    Never raises. A timeout is a fallback too: the message may then arrive
    twice, which beats losing it.
    """
    if not fits_rich(markdown):
        logger.info("rich message skipped: empty or over limits", chars=len(markdown))
        return None
    try:
        return await send_rich_message(
            bot,
            chat_id,
            markdown,
            message_thread_id=message_thread_id,
            reply_to_message_id=reply_to_message_id,
        )
    except Exception as e:  # noqa: BLE001 - every failure falls back to text
        logger.warning(
            "rich message failed, falling back to text",
            chat_id=chat_id,
            error_type=type(e).__name__,
            error=str(e)[:200],
        )
        return None
