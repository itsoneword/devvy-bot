"""Send raster photos to a chat, surviving an upload that times out.

16.11.26: from 13:33 every album and photo the live bot sent timed out at 30 s
(``Failed to send photo album: Timed out``) while text went through and the
same upload from a fresh client of the same setup took 0.15 s. The send is
retried one photo at a time with long timeouts. The caller is told which
paths never arrived, so it can say so in the chat instead of losing them
silently (the agent's tool only queues the picture).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Optional, Sequence

import structlog
from telegram import InputMediaPhoto
from telegram.error import BadRequest, NetworkError

logger = structlog.get_logger()

# Per-photo retry: generous, so a slow upload is not cut off again.
RETRY_TIMEOUTS = {
    "connect_timeout": 30.0,
    "read_timeout": 120.0,
    "write_timeout": 120.0,
    "pool_timeout": 30.0,
}


@dataclass
class PhotoSendResult:
    """``caption_sent``: the caption went with the first photo;
    ``failed``: paths that did not reach the chat."""

    caption_sent: bool = False
    failed: List[Path] = field(default_factory=list)


async def send_photos(
    bot: Any,
    chat_id: Any,
    photos: Sequence[Path],
    *,
    caption: Optional[str] = None,
    parse_mode: Optional[str] = None,
    **kw: Any,
) -> PhotoSendResult:
    """Up to 10 *photos* as one album (one photo: a plain photo); on a
    network error or timeout, each photo again on its own with long timeouts.

    *kw* goes to every send (``reply_to_message_id``, ``message_thread_id``).
    Errors other than network ones (bad request, file gone) are not retried.
    """
    photos = list(photos)[:10]
    if not photos:
        return PhotoSendResult()
    try:
        await _send_batch(bot, chat_id, photos, caption, parse_mode, kw)
        return PhotoSendResult(caption_sent=bool(caption))
    except BadRequest as e:  # a NetworkError subclass in PTB: not transient
        logger.warning("photo send rejected", error=str(e))
        return PhotoSendResult(failed=photos)
    except NetworkError as e:  # TimedOut is a NetworkError
        logger.warning(
            "photo send failed, retrying one by one",
            error=str(e),
            error_type=type(e).__name__,
            photos=len(photos),
        )
    except Exception as e:
        logger.warning("photo send failed", error=str(e), error_type=type(e).__name__)
        return PhotoSendResult(failed=photos)

    result = PhotoSendResult()
    retry_bot, close = await _fresh_bot(bot)
    try:
        await _retry_each(retry_bot, chat_id, photos, caption, parse_mode, kw, result)
    finally:
        await close()
    return result


async def _fresh_bot(bot: Any) -> tuple[Any, Any]:
    """A Bot on its own new connection pool for the retry.

    16.11.26 20:29-20:41: every retry on the live pool timed out again; its
    one Telegram connection showed 3615 TCP retransmits, while a fresh
    connection to the same IP uploaded the same file in 0.5 s. Falls back to
    *bot* when it has no token (tests, fakes).
    """
    token = getattr(bot, "token", None)
    if not isinstance(token, str) or not token:
        return bot, _noop
    import httpx
    from telegram import Bot
    from telegram.request import HTTPXRequest

    transport = httpx.AsyncHTTPTransport(
        local_address="0.0.0.0", limits=httpx.Limits(max_connections=1)
    )
    fresh = Bot(
        token=token,
        request=HTTPXRequest(
            connection_pool_size=1, httpx_kwargs={"transport": transport}
        ),
    )
    await fresh.initialize()
    return fresh, fresh.shutdown


async def _noop() -> None:
    return None


async def _retry_each(
    bot: Any,
    chat_id: Any,
    photos: List[Path],
    caption: Optional[str],
    parse_mode: Optional[str],
    kw: Any,
    result: PhotoSendResult,
) -> None:
    for idx, path in enumerate(photos):
        first = idx == 0 and bool(caption)
        try:
            with open(path, "rb") as f:
                await bot.send_photo(
                    chat_id=chat_id,
                    photo=f,
                    caption=caption if first else None,
                    parse_mode=parse_mode if first else None,
                    **kw,
                    **RETRY_TIMEOUTS,
                )
            result.caption_sent = result.caption_sent or first
        except Exception as e:
            logger.warning(
                "photo retry failed",
                path=str(path),
                error=str(e),
                error_type=type(e).__name__,
            )
            result.failed.append(path)


async def _send_batch(
    bot: Any,
    chat_id: Any,
    photos: List[Path],
    caption: Optional[str],
    parse_mode: Optional[str],
    kw: Any,
) -> None:
    if len(photos) == 1:
        with open(photos[0], "rb") as f:
            await bot.send_photo(
                chat_id=chat_id,
                photo=f,
                caption=caption,
                parse_mode=parse_mode if caption else None,
                **kw,
            )
        return
    handles = [open(p, "rb") for p in photos]  # noqa: SIM115
    try:
        await bot.send_media_group(
            chat_id=chat_id,
            media=[
                InputMediaPhoto(
                    media=h,
                    caption=caption if i == 0 else None,
                    parse_mode=parse_mode if i == 0 and caption else None,
                )
                for i, h in enumerate(handles)
            ],
            **kw,
        )
    finally:
        for h in handles:
            h.close()


def undelivered_note(failed: Sequence[Path]) -> str:
    """Chat text for pictures that did not arrive."""
    names = ", ".join(p.name for p in failed)
    return (
        f"⚠️ Telegram did not take {len(failed)} picture(s): {names}. "
        "Ask me to send them again."
    )


async def send_document(bot: Any, chat_id: Any, path: Path, **kw: Any) -> bool:
    """One file as a document; on a network error, once more over a fresh
    connection pool with long timeouts (as the photo retry). False = it did
    not reach the chat (16.11.26 21:11: a 16 MB PDF timed out silently)."""
    for attempt in (1, 2):
        sender, close = (bot, _noop) if attempt == 1 else await _fresh_bot(bot)
        try:
            with open(path, "rb") as f:
                await sender.send_document(
                    chat_id=chat_id,
                    document=f,
                    filename=path.name,
                    **kw,
                    **({} if attempt == 1 else RETRY_TIMEOUTS),
                )
            return True
        except BadRequest as e:
            logger.warning("document send rejected", path=str(path), error=str(e))
            return False
        except Exception as e:
            logger.warning(
                "document send failed",
                path=str(path),
                attempt=attempt,
                error=str(e),
                error_type=type(e).__name__,
            )
        finally:
            await close()
    return False
