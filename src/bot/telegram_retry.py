"""Retry for transient Telegram network failures (dv-2e34).

A TimedOut / NetworkError on a Telegram call is the network's doing, not a bot
fault or an attack. Retry with backoff; if it still fails the global handler
tells the user the message was not processed (a media message instead
goes to the background retry, dv-c977).
"""

import asyncio
from typing import Any, Awaitable, Callable, Dict, Optional, TypeVar

import structlog
from telegram.error import BadRequest, NetworkError, TimedOut

logger = structlog.get_logger()

T = TypeVar("T")

RETRY_DELAYS = (0.5, 1.5, 4.0)
# Media fetches (getFile, download), the inline window: three attempts (each
# can hang for the full 30 s request timeout), about a minute. A file still
# missing then goes to the background retry (dv-c977, src/bot/media_retry.py)
# and the chat's turn lock is released; 6 minutes inline blocked the chat.
MEDIA_RETRY_DELAYS = (2.0, 5.0)

# Bytes the background retrier already fetched, by file_id: the resumed turn
# runs the normal handler, whose fetch takes them from here (dv-c977).
_PREFETCHED: Dict[str, bytes] = {}


def prefetch(file_id: str, data: bytes) -> None:
    _PREFETCHED[file_id] = bytes(data)


def discard_prefetched(file_id: str) -> None:
    _PREFETCHED.pop(file_id, None)


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


async def fetch_media_bytes(
    media: Any,
    *,
    delays: tuple = MEDIA_RETRY_DELAYS,
    on_file: Optional[Callable[[Any], None]] = None,
    read_timeout: Optional[float] = None,
) -> bytearray:
    """getFile + download of a PhotoSize / Document / Voice, the one media
    fetch every handler uses. ``on_file`` checks the getFile result (size
    limits) before the download. A getFile timeout fires the C5 probe once per
    file (``media_retry.maybe_probe``). Bytes the background retrier already
    holds are returned without a call. ``read_timeout`` overrides the
    client's 30 s for both calls (background retry: a getFile that needs
    longer than 30 s would otherwise be cut off on every attempt)."""
    file_id = getattr(media, "file_id", None)
    if isinstance(file_id, str) and file_id in _PREFETCHED:
        return bytearray(_PREFETCHED[file_id])

    kw: Dict[str, Any] = {} if read_timeout is None else {"read_timeout": read_timeout}

    async def get_file() -> Any:
        try:
            return await media.get_file(**kw)
        except TimedOut:
            from .media_retry import maybe_probe

            maybe_probe(media)
            raise

    file = await with_telegram_retry(get_file, delays=delays, what="getFile")
    if on_file is not None:
        on_file(file)
    data: bytearray = await with_telegram_retry(
        lambda: file.download_as_bytearray(**kw), delays=delays, what="download"
    )
    return data


async def fetch_via_local_api(
    base_url: str, bot_key: str, file_id: str, timeout: float
) -> bytes:
    """getFile on a local ``telegram-bot-api --local`` server, then read the
    file it saved (``file_path`` is absolute, same path on the host) and
    delete it. Raises on any failure; the caller falls back to the cloud."""
    import os

    import httpx

    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(
            f"{base_url.rstrip('/')}/bot{bot_key}/getFile", data={"file_id": file_id}
        )
    body = resp.json()
    if not body.get("ok"):
        raise RuntimeError(f"local getFile: {body.get('description')}")
    path = str(body["result"]["file_path"])
    if not os.path.isabs(path):
        raise RuntimeError("local getFile: relative file_path (server not --local)")
    with open(path, "rb") as f:
        data = f.read()
    try:
        os.remove(path)
    except OSError:
        pass
    return data
