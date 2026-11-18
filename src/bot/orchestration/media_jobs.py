"""Media turns that survive a getFile hang (dv-c977).

Every voice / photo / document message gets a ``pending_media`` row at
receipt, before the first getFile (:func:`store_pending_media`). The handler's
download / transcription step runs as the cancellable media phase
(``_media_step``): /stop cancels it, and a file Telegram still withholds after
the inline window is handed to :class:`~src.bot.media_retry.MediaRetrier`
while the chat's turn lock is released. When the file arrives (minutes later
or after a restart) the retrier rebuilds the stored update and runs the SAME
handler (``_resume_media``): one turn path for fresh, late and resumed media.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import MutableMapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import (
    Any,
    AsyncIterator,
    Awaitable,
    Callable,
    Dict,
    Iterator,
    List,
    Optional,
    TypeVar,
)

import structlog
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from ..media_retry import (
    MEDIA_RETRY_CALLBACK,
    MediaRetrier,
    deferred_status_text,
    failed_text,
    kind_label,
    late_note,
)
from ..telegram_retry import (
    discard_prefetched,
    fetch_media_bytes,
    fetch_via_local_api,
    is_transient_network_error,
    prefetch,
    with_telegram_retry,
)
from .base import OrchestratorBase

logger = structlog.get_logger()

# Background media fetch: per-call read timeout instead of the client's 30 s
# (16.11.26: getFile of fresh voices hung 30 s on every attempt for an hour).
BACKGROUND_FETCH_TIMEOUT = 180.0

T = TypeVar("T")


def _media_object(message: Any) -> Any:
    voice = getattr(message, "voice", None)
    if voice:
        return voice
    photo = getattr(message, "photo", None)
    if photo:
        return photo[-1]
    return getattr(message, "document", None) or None


def media_row_fields(update: Any) -> Optional[Dict[str, str]]:
    """``kind`` and ``file_id`` of a media message, the same for a forwarded
    copy as for a fresh one (C6); None for a non-media message."""
    message = getattr(update, "message", None)
    if message is None:
        return None
    media = _media_object(message)
    file_id = getattr(media, "file_id", None) if media is not None else None
    if file_id is None:
        return None
    if getattr(message, "voice", None):
        kind = "voice"
    elif getattr(message, "photo", None):
        kind = "photo"
    else:
        kind = "document"
    return {"kind": kind, "file_id": str(file_id)}


async def store_pending_media(
    storage: Any,
    update: Any,
    profile_slug: Optional[str],
    *,
    items: Optional[List[Any]] = None,
) -> Optional[int]:
    """Write the row for a media message before its first getFile; claimed
    (``running``) by the inline handler. ``items``: an album's updates, all
    stored in one row. None when there is no storage or the write failed."""
    repo = getattr(storage, "pending_media", None)
    fields = media_row_fields(update)
    if repo is None or fields is None:
        return None
    message = update.message
    try:
        if items and len(items) > 1:
            update_json = json.dumps({"album": [u.to_dict() for u in items]})
        else:
            update_json = update.to_json()
        date = getattr(message, "date", None)
        return int(
            await repo.add(
                chat_id=message.chat_id,
                thread_id=(
                    message.message_thread_id if message.is_topic_message else None
                ),
                message_id=message.message_id,
                user_id=update.effective_user.id,
                profile_slug=profile_slug,
                kind=fields["kind"],
                file_id=fields["file_id"],
                update_json=update_json,
                sent_at=date.timestamp() if isinstance(date, datetime) else None,
                status="running",
            )
        )
    except Exception as exc:
        logger.warning("pending_media write failed", error=str(exc))
        return None


def updates_from_row(row: Dict[str, Any], bot: Any) -> List[Update]:
    payload = json.loads(row["update_json"])
    raw = (
        payload["album"]
        if isinstance(payload, dict) and "album" in payload
        else [payload]
    )
    return [Update.de_json(u, bot) for u in raw]


@dataclass
class MediaJob:
    """One media message on its way to a turn."""

    rid: Optional[int]
    user_id: int
    slug: str
    kind: str
    message_id: int
    resumed: bool = False
    sent_at: Optional[datetime] = None
    items: Optional[List[Any]] = None
    # None (normal) | "deferred" (handed to the retrier) | "stopped" (/stop)
    outcome: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)


class _ProfileOverlay(MutableMapping):  # type: ignore[type-arg]
    """``user_data`` of a resumed turn: ``active_profile`` is the stored slug
    (the profile the media was sent to), everything else is the user's own."""

    def __init__(self, base: Any, slug: str) -> None:
        self._base = base
        self._slug = slug

    def __getitem__(self, key: Any) -> Any:
        if key == "active_profile":
            return self._slug
        return self._base[key]

    def __setitem__(self, key: Any, value: Any) -> None:
        if key == "active_profile":
            return  # the user's sticky profile is not this turn's to change
        self._base[key] = value

    def __delitem__(self, key: Any) -> None:
        if key != "active_profile":
            del self._base[key]

    def __iter__(self) -> Iterator[Any]:
        yield "active_profile"
        for key in self._base:
            if key != "active_profile":
                yield key

    def __len__(self) -> int:
        return len([k for k in self._base if k != "active_profile"]) + 1


class _ResumeContext:
    """A handler context for an update rebuilt from its stored JSON."""

    def __init__(self, ctx: Any, user_data: Any) -> None:
        self._ctx = ctx
        self.user_data = user_data

    def __getattr__(self, name: str) -> Any:
        return getattr(self._ctx, name)


async def _quiet(coro: Awaitable[Any]) -> None:
    try:
        await coro
    except Exception:
        pass


class MediaJobsMixin(OrchestratorBase):
    """Store, defer, stop and resume media turns."""

    # --- handler side ------------------------------------------------------

    async def _open_media_job(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        items: Optional[List[Any]] = None,
    ) -> MediaJob:
        from ...profiles.registry import DEFAULT_AGENT_ID

        message: Any = update.message
        user: Any = update.effective_user
        self._init_media_state()
        resumed = self._media_resume.get((message.chat_id, message.message_id))
        if resumed is not None:
            return resumed  # type: ignore[no-any-return]
        slug = await self._turn_profile_slug(update, context) or DEFAULT_AGENT_ID
        fields = media_row_fields(update) or {"kind": "media"}
        rid = await store_pending_media(
            context.bot_data.get("storage"), update, slug, items=items
        )
        if rid is not None:
            self._media_pending_ids.add(message.message_id)
        return MediaJob(
            rid=rid,
            user_id=user.id,
            slug=slug,
            kind=fields["kind"],
            message_id=message.message_id,
        )

    @contextlib.asynccontextmanager
    async def _media_job_scope(
        self, job: MediaJob, context: ContextTypes.DEFAULT_TYPE
    ) -> AsyncIterator[MediaJob]:
        """Close the row when the handler is done; a transient Telegram error
        escaping the handler (progress message, typing action) defers it to
        the background retry instead of losing it."""
        try:
            yield job
        except Exception as exc:
            if (
                job.outcome is None
                and not job.resumed
                and job.rid is not None
                and is_transient_network_error(exc)
            ):
                await self._defer_media_job(job, None, context)
            else:
                await self._finish_media_job(job, context)
            raise
        else:
            await self._finish_media_job(job, context)

    async def _finish_media_job(
        self, job: MediaJob, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if job.resumed or job.rid is None or job.outcome == "deferred":
            return  # the retrier owns resumed and deferred rows
        storage: Any = context.bot_data.get("storage")
        await _quiet(storage.pending_media.close(job.rid, job.outcome or "done"))
        self._media_pending_ids.discard(job.message_id)

    async def _media_step(
        self,
        job: MediaJob,
        progress_msg: Any,
        context: ContextTypes.DEFAULT_TYPE,
        step: Callable[[], Awaitable[T]],
    ) -> Optional[T]:
        """Run the download / transcription step as the media phase.

        Returns None when the turn ends here: /stop cancelled it (row closed
        ``stopped``) or the file is still missing after the inline window
        (row handed to the background retrier, progress = one status line).
        """
        task = asyncio.current_task()
        if task is not None:
            self._media_registry.track(job.user_id, job.slug, task)
        try:
            return await step()
        except asyncio.CancelledError:
            if self._media_closing:
                raise  # shutdown: the row stays open for the restart pickup
            if task is not None:
                task.uncancel()
            job.outcome = "stopped"
            await self._finish_media_job(job, context)
            await _quiet(
                progress_msg.edit_text(
                    f"⏹ Stopped: the {kind_label(job.kind)} download/"
                    "transcription was cancelled."
                )
            )
            return None
        except Exception as exc:
            if job.rid is None or not is_transient_network_error(exc):
                raise
            logger.warning(
                "media not available inline, deferring",
                row_id=job.rid,
                kind=job.kind,
                error=str(exc) or repr(exc),
            )
            await self._defer_media_job(job, progress_msg, context)
            return None
        finally:
            if task is not None:
                self._media_registry.untrack(job.user_id, job.slug, task)

    async def _defer_media_job(
        self,
        job: MediaJob,
        progress_msg: Any,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        job.outcome = "deferred"
        if job.resumed:
            # The retrier keeps going; the first status line still stands.
            if progress_msg is not None:
                await _quiet(progress_msg.delete())
            return
        storage: Any = context.bot_data.get("storage")
        assert job.rid is not None
        await _quiet(storage.pending_media.release(job.rid))
        text = deferred_status_text(job.kind)
        if progress_msg is not None:
            try:
                await progress_msg.edit_text(text)
                await storage.pending_media.set_status_message(
                    job.rid, progress_msg.message_id
                )
            except Exception as exc:
                logger.warning("deferred status edit failed", error=str(exc))
        retrier = self._get_media_retrier(context)
        if retrier is None:
            logger.warning("media retrier unavailable; row waits for restart")
            return
        retrier.schedule(job.rid, job.user_id, job.slug)

    def _late_prompt(self, job: MediaJob, prompt: str) -> str:
        """C10: a turn run late from the background names the send time."""
        if not job.resumed or job.sent_at is None:
            return prompt
        return f"{late_note(job.sent_at)}\n{prompt}"

    # --- retrier side --------------------------------------------------------

    def _get_media_retrier(
        self, context: Optional[ContextTypes.DEFAULT_TYPE] = None
    ) -> Optional[MediaRetrier]:
        if self._media_retrier_obj is not None:
            return self._media_retrier_obj  # type: ignore[no-any-return]
        if self._media_app is None and context is not None:
            self._media_app = getattr(context, "application", None)
        storage: Any = self.deps.get("storage")
        if storage is None and context is not None:
            storage = context.bot_data.get("storage")
        if storage is None or self._media_app is None:
            return None
        self._media_retrier_obj = MediaRetrier(
            storage=storage,
            run_turn=self._resume_media,
            fetch=self._fetch_media_row,
            notify=self._notify_media_failed,
            registry=self._media_registry,
        )
        return self._media_retrier_obj  # type: ignore[no-any-return]

    def shutdown_media(self) -> None:
        """Bot shutdown: cancellations from here on are not /stop."""
        self._init_media_state()
        self._media_closing = True
        if self._media_retrier_obj is not None:
            self._media_retrier_obj.closing = True

    async def resume_pending_media(self, app: Any) -> int:
        """Startup pickup (C3): retry every row a restart left open."""
        self._media_app = app
        retrier = self._get_media_retrier()
        if retrier is None:
            return 0
        storage: Any = self.deps.get("storage")
        for row in await storage.pending_media.list_open():
            self._media_pending_ids.add(row["message_id"])
        return await retrier.resume_all()

    async def _fetch_media_row(self, row: Dict[str, Any]) -> Dict[str, bytes]:
        """One getFile + download per file of the row, no inline retries."""
        out: Dict[str, bytes] = {}
        for update in updates_from_row(row, self._media_app.bot):
            media = _media_object(update.message)
            if media is None:
                continue
            started = time.monotonic()
            via = "cloud"
            data: Optional[bytes] = None
            local_url = getattr(self.settings, "telegram_file_api_url", None)
            if local_url:
                try:
                    data = await fetch_via_local_api(
                        local_url,
                        str(getattr(self._media_app.bot, "token")),
                        media.file_id,
                        BACKGROUND_FETCH_TIMEOUT,
                    )
                    via = "local"
                except Exception as e:
                    logger.warning(
                        "media retry: local Bot API failed, using cloud",
                        error=str(e)[:200],
                        seconds=round(time.monotonic() - started, 1),
                    )
            if data is None:
                data = bytes(
                    await fetch_media_bytes(
                        media, delays=(), read_timeout=BACKGROUND_FETCH_TIMEOUT
                    )
                )
            out[media.file_id] = data
            # 16.11.26 test: which path got the file, and how long it took.
            logger.info(
                "media retry: fetched",
                file_id=media.file_id,
                via=via,
                seconds=round(time.monotonic() - started, 1),
            )
        return out

    async def _resume_media(self, row: Dict[str, Any], data: Any) -> None:
        """Run the normal handler for a stored media message whose file has
        arrived. Raises TimedOut if it is still missing, CancelledError on
        /stop, so the retrier keeps its loop."""
        from telegram.error import TimedOut
        from telegram.ext import CallbackContext

        app = self._media_app
        updates = updates_from_row(row, app.bot)
        leader = updates[0]
        message: Any = leader.message
        sent_at = (
            datetime.fromtimestamp(float(row["sent_at"]), UTC)
            if row.get("sent_at")
            else None
        )
        job = MediaJob(
            rid=row["id"],
            user_id=row["user_id"],
            slug=row["profile_slug"] or "",
            kind=row["kind"],
            message_id=row["message_id"],
            resumed=True,
            sent_at=sent_at,
            items=updates if len(updates) > 1 else None,
        )
        key = (message.chat_id, message.message_id)
        ctx = CallbackContext.from_update(leader, app)
        user_data: Any = ctx.user_data
        registry = self.deps.get("profile_registry") or app.bot_data.get(
            "profile_registry"
        )
        if registry is not None and row["profile_slug"] in {
            p.slug for p in registry.enabled_profiles
        }:
            user_data = _ProfileOverlay(user_data, row["profile_slug"])
        handler = {
            "voice": self.agentic_voice,  # type: ignore[attr-defined]
            "photo": self.agentic_photo,  # type: ignore[attr-defined]
            "document": self.agentic_document,  # type: ignore[attr-defined]
        }[row["kind"]]
        for file_id, blob in (data or {}).items():
            prefetch(file_id, blob)
        self._media_resume[key] = job
        try:
            await self._inject_deps(handler)(leader, _ResumeContext(ctx, user_data))
        finally:
            self._media_resume.pop(key, None)
            for file_id in data or {}:
                discard_prefetched(file_id)
        if job.outcome == "stopped":
            raise asyncio.CancelledError()
        if job.outcome == "deferred":
            raise TimedOut("media still not available")
        self._media_pending_ids.discard(row["message_id"])
        if row.get("status_message_id"):
            await _quiet(
                app.bot.delete_message(row["chat_id"], row["status_message_id"])
            )

    async def _notify_media_failed(self, row: Dict[str, Any]) -> None:
        """C9: one message naming the media by time, with a Retry button."""
        bot = self._media_app.bot
        sent_at = (
            datetime.fromtimestamp(float(row["sent_at"]), UTC)
            if row.get("sent_at")
            else None
        )
        tz = getattr(self.settings, "user_timezone", None)
        if sent_at is not None and isinstance(tz, str) and tz:
            try:
                from zoneinfo import ZoneInfo

                sent_at = sent_at.astimezone(ZoneInfo(tz))
            except Exception:
                pass
        text = failed_text(row["kind"], sent_at)
        markup = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "🔁 Retry", callback_data=f"{MEDIA_RETRY_CALLBACK}{row['id']}"
                    )
                ]
            ]
        )
        self._media_pending_ids.discard(row["message_id"])
        if row.get("status_message_id"):
            try:
                await bot.edit_message_text(
                    text,
                    chat_id=row["chat_id"],
                    message_id=row["status_message_id"],
                    reply_markup=markup,
                )
                return
            except Exception:
                pass
        from telegram import ReplyParameters

        await with_telegram_retry(
            lambda: bot.send_message(
                row["chat_id"],
                text,
                message_thread_id=row.get("thread_id"),
                reply_parameters=ReplyParameters(
                    message_id=row["message_id"], allow_sending_without_reply=True
                ),
                reply_markup=markup,
            ),
            what="media failure notice",
        )

    async def _handle_media_retry_callback(
        self, query: Any, context: ContextTypes.DEFAULT_TYPE, data: str
    ) -> None:
        """``mr:<row id>``: retry a failed media row (C9)."""
        self._init_media_state()
        storage: Any = context.bot_data.get("storage")
        try:
            row_id = int(data[len(MEDIA_RETRY_CALLBACK) :])
        except ValueError:
            await _quiet(query.answer())
            return
        row = await storage.pending_media.get(row_id) if storage else None
        presser = query.from_user.id
        if row is None or (
            row["user_id"] != presser
            and self._resolve_role(presser, context) != "owner"
        ):
            await _quiet(query.answer("Not available.", show_alert=True))
            return
        retrier = self._get_media_retrier(context)
        if retrier is None or row["status"] != "failed":
            await _quiet(query.answer("Nothing to retry."))
            return
        await _quiet(query.answer())
        await _quiet(query.edit_message_text(deferred_status_text(row["kind"])))
        message = getattr(query, "message", None)
        if message is not None:
            await _quiet(
                storage.pending_media.set_status_message(row_id, message.message_id)
            )
        task = asyncio.ensure_future(retrier.retry_failed(row_id))
        self._media_registry.track(row["user_id"], row["profile_slug"], task)
        task.add_done_callback(
            lambda t: self._media_registry.untrack(
                row["user_id"], row["profile_slug"], t
            )
        )
