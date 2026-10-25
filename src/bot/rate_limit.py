"""Telegram rate limiter that does not charge chat actions to the group bucket."""

from typing import Any, Callable, Coroutine, Dict, Optional

from telegram.ext import AIORateLimiter

# PTB passes the Bot API method name as `endpoint` (telegram/ext/_extbot.py
# `_do_post` -> rate_limiter.process_request(endpoint=...)).
_UNMETERED_ENDPOINTS = frozenset({"sendChatAction"})


class DevvyRateLimiter(AIORateLimiter):
    """AIORateLimiter whose typing heartbeat bypasses the per-group limiter.

    The PTB default group limit is 20 calls/min (1 per 3 s) per negative
    chat_id, shared by every forum topic of a supergroup. A typing heartbeat
    per active turn alone fills it, so real edits queue for seconds (dv-586f).
    ``sendChatAction`` is ephemeral and not a message; it skips both the group
    and the global limiter but still honours an active RetryAfter pause.
    """

    async def process_request(
        self,
        callback: Callable[..., Coroutine[Any, Any, Any]],
        args: Any,
        kwargs: Dict[str, Any],
        endpoint: str,
        data: Dict[str, Any],
        rate_limit_args: Optional[int],
    ) -> Any:
        if endpoint in _UNMETERED_ENDPOINTS:
            await self._retry_after_event.wait()
            return await callback(*args, **kwargs)
        return await super().process_request(
            callback, args, kwargs, endpoint, data, rate_limit_args
        )
