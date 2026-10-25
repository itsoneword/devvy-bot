"""Retry for transient Telegram network failures (dv-2e34).

A TimedOut / NetworkError on a Telegram call is the network's doing, not a bot
fault or an attack. Retry with backoff; if it still fails the global handler
tells the user the message was not processed.
"""

import asyncio
from typing import Awaitable, Callable, TypeVar

import structlog
from telegram.error import BadRequest, NetworkError, TimedOut

logger = structlog.get_logger()

T = TypeVar("T")

RETRY_DELAYS = (0.5, 1.5, 4.0)


def is_transient_network_error(error: object) -> bool:
    """TimedOut / NetworkError, excluding BadRequest (a subclass of NetworkError
    that means the request itself was refused)."""
    if isinstance(error, BadRequest):
        return False
    return isinstance(error, (TimedOut, NetworkError))


async def with_telegram_retry(
    call: Callable[[], Awaitable[T]],
    *,
    delays: tuple = RETRY_DELAYS,
    what: str = "telegram call",
) -> T:
    """Run ``call`` (a zero-arg coroutine factory), retrying transient network
    errors with backoff. The last error propagates."""
    for attempt, delay in enumerate(delays, start=1):
        try:
            return await call()
        except Exception as exc:
            if not is_transient_network_error(exc):
                raise
            logger.warning(
                "Telegram network error, retrying",
                what=what,
                attempt=attempt,
                error=str(exc),
            )
            await asyncio.sleep(delay)
    return await call()
