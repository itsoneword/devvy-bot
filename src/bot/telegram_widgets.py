"""Telegram widgets (Bot API 10.4) through PTB's raw request path.

This module goes when python-telegram-bot supports Bot API 10.4: remove it and
call the library's own ``send_widget`` / ``answer_widget_query`` / ``Update``
fields instead. PTB 22.6 speaks Bot API 9.3, so it has none of them. Until
then this is the ONE place that calls Telegram raw (``Bot.do_api_request``,
the library's own path for methods it does not know) and the one place that
reads a ``widget_query`` update (PTB puts the unknown field in
``update.api_kwargs``). Function names follow the future PTB methods so the
swap is a rename at the call sites (dv-4665).

Parameters from core.telegram.org/bots/api (sendWidget, answerWidgetQuery,
WidgetQuery), read 2026-11-11.
"""

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from telegram import Bot, Update

# Update types PTB 22.6's ``Update.ALL_TYPES`` lacks. The widget docs ask a
# bot that passes allowed_updates to include both.
WIDGET_UPDATE_TYPES = ("widget_query", "shared_widget_query")

HTML_MAX_BYTES = 32768  # raw UTF-8
HTML_MAX_COMPRESSED = 4096  # zlib level 9
DATA_MAX_BYTES = 1024  # chat_data, public_data, an answer's data
CAPTION_MAX_CHARS = 1024


def allowed_updates() -> List[str]:
    """Every update type PTB knows plus the widget ones."""
    return [*Update.ALL_TYPES, *WIDGET_UPDATE_TYPES]


@dataclass(frozen=True)
class WidgetQuery:
    """The fields of Bot API ``WidgetQuery`` the bot uses."""

    id: str
    from_user_id: Optional[int]
    data: str
    chat_id: Optional[int] = None
    message_id: Optional[int] = None


def widget_query_from_update(update: Update) -> Optional[WidgetQuery]:
    """The update's ``widget_query``, or None. A cheap dict lookup: it runs
    for every update."""
    raw = (getattr(update, "api_kwargs", None) or {}).get("widget_query")
    if not isinstance(raw, dict) or not isinstance(raw.get("id"), str):
        return None
    sender: Dict[str, Any] = raw["from"] if isinstance(raw.get("from"), dict) else {}
    chat: Dict[str, Any] = raw["chat"] if isinstance(raw.get("chat"), dict) else {}
    user_id = sender.get("id")
    chat_id = chat.get("id")
    message_id = raw.get("message_id")
    return WidgetQuery(
        id=raw["id"],
        from_user_id=user_id if isinstance(user_id, int) else None,
        data=str(raw.get("data") or ""),
        chat_id=chat_id if isinstance(chat_id, int) else None,
        message_id=message_id if isinstance(message_id, int) else None,
    )


async def send_widget(
    bot: Bot,
    chat_id: int,
    html: str,
    *,
    caption: Optional[str] = None,
    chat_data: Optional[str] = None,
    allow_queries: bool = False,
    message_thread_id: Optional[int] = None,
    height: Optional[int] = None,
) -> Any:
    """``sendWidget``: a message with an HTML widget. Returns the raw Message
    dict; raises what Telegram answers (a TelegramError) on failure."""
    kwargs: Dict[str, Any] = {"chat_id": chat_id, "html": html}
    if caption:
        kwargs["caption"] = caption[:CAPTION_MAX_CHARS]
    if chat_data is not None:
        kwargs["chat_data"] = chat_data
    if allow_queries:
        kwargs["allow_queries"] = True
    if message_thread_id is not None:
        kwargs["message_thread_id"] = message_thread_id
    if height is not None:
        kwargs["height"] = height
    return await bot.do_api_request("sendWidget", api_kwargs=kwargs)


async def answer_widget_query(
    bot: Bot, widget_query_id: str, data: Optional[str] = None
) -> bool:
    """``answerWidgetQuery``: the answer only the asking widget receives
    (≤ 1024 bytes of data, within 15 s). It does not change the state."""
    kwargs: Dict[str, Any] = {"widget_query_id": widget_query_id}
    if data is not None:
        if len(data.encode("utf-8")) > DATA_MAX_BYTES:
            raise ValueError("widget answer data over 1024 bytes")
        kwargs["data"] = data
    result = await bot.do_api_request("answerWidgetQuery", api_kwargs=kwargs)
    return bool(result)


__all__ = [
    "WIDGET_UPDATE_TYPES",
    "WidgetQuery",
    "allowed_updates",
    "answer_widget_query",
    "send_widget",
    "widget_query_from_update",
]
