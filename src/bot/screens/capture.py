"""Run a bot handler offline and keep what it would have shown.

The handler gets an ``update`` whose ``message.reply_text`` and
``callback_query.edit_message_text`` / ``edit_message_reply_markup`` record
the reply instead of calling Telegram, the same fakes the handler tests
always used (``test_model_keyboard.py``), factored out once.
"""

import time
from types import SimpleNamespace
from typing import Any, Awaitable, Callable, List, Optional

from .model import Screen

Handler = Callable[[Any, Any], Awaitable[Any]]


async def capture(
    handler: Handler,
    update: Any = None,
    context: Any = None,
    screen_id: str = "",
    goal: str = "",
) -> List[Screen]:
    """Every message ``handler`` sent or edited, in order, each with the
    handler's wall time in ``render_ms``.

    ``update`` None builds a fake one with no ``effective_user``; a given
    ``update`` gets the recorders set on its ``message`` / ``callback_query``.
    """
    sent: List[Screen] = []
    base = screen_id or getattr(handler, "__name__", "screen")

    def _add(text: Optional[str], kwargs: Any) -> None:
        if text is None:  # edit_message_reply_markup: same text, new keyboard
            text = sent[-1].text if sent else ""
        sid = base if not sent else f"{base}.{len(sent) + 1}"
        sent.append(
            Screen.from_markup(
                sid,
                text,
                kwargs.get("reply_markup"),
                parse_mode=kwargs.get("parse_mode"),
                goal=goal,
            )
        )

    async def reply_text(text: str, *args: Any, **kwargs: Any) -> Any:
        _add(text, kwargs)
        return SimpleNamespace(message_id=len(sent))

    async def edit_message_reply_markup(*args: Any, **kwargs: Any) -> Any:
        if args and "reply_markup" not in kwargs:
            kwargs["reply_markup"] = args[0]
        _add(None, kwargs)
        return True

    if update is None:
        update = SimpleNamespace(
            effective_user=None,
            message=SimpleNamespace(chat_id=1),
            callback_query=SimpleNamespace(),
        )
    for target in (
        getattr(update, "message", None),
        getattr(update, "callback_query", None),
    ):
        if target is None:
            continue
        target.reply_text = reply_text
        target.edit_message_text = reply_text
        target.edit_message_reply_markup = edit_message_reply_markup
    if context is None:
        context = SimpleNamespace(user_data={}, bot_data={})

    start = time.perf_counter()
    await handler(update, context)
    elapsed = (time.perf_counter() - start) * 1000
    for screen in sent:
        screen.render_ms = elapsed
    return sent
