"""Background retry for media Telegram will not hand out yet (dv-c977).

Telegram's getFile hung for minutes at a time (09.-11.11.26: every attempt
used up the full 30 s timeout while other calls answered), and a dictated voice
the bot cannot download was lost: its file_id was stored nowhere. Now every
media message has a ``pending_media`` row from receipt
(``turns.store_pending_media``). When the inline window (``MEDIA_RETRY_DELAYS``)
runs out, the handler frees the chat and hands the row to
:class:`MediaRetrier`, which retries with backoff for up to 2 h (also after a
restart, ``resume_all``) and then runs the normal media turn. /stop cancels it
through ``MediaRegistry``.

``probe_get_file`` is the C5 evidence hook: on a getFile timeout it runs one
getMe and one getFile on a fresh connection outside PTB's pool and logs both
outcomes with the pool's state, so the next failure says where the hang is
(docs/FINDINGS.md 2026-11-11).
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, Optional, Set

import structlog

from ..utils.media_registry import MediaRegistry
from .telegram_retry import is_transient_network_error

logger = structlog.get_logger()

# Background backoff: 30 s growing to 5 min, for 2 h from receipt.
BACKGROUND_DELAYS = (30.0, 60.0, 120.0, 300.0)
BACKGROUND_WINDOW_SECONDS = 2 * 3600

MEDIA_RETRY_CALLBACK = "mr:"

_KIND_LABEL = {"voice": "voice message", "photo": "photo", "document": "file"}


def kind_label(kind: str) -> str:
    return _KIND_LABEL.get(kind, "file")


def deferred_status_text(kind: str) -> str:
    """The one status line a deferred media message gets (no "resend")."""
    return (
        f"⏳ Telegram has not handed over this {kind_label(kind)} yet. "
        "I keep trying in the background and answer when it arrives; "
        "/stop cancels."
    )


def late_note(sent_at: datetime) -> str:
    """One prompt line for a turn that runs late from the background."""
    stamp = sent_at.strftime("%H:%M")
    tz = sent_at.tzname() or ""
    when = f"{stamp} {tz}".strip()
    return (
        f"[Late delivery: this message was sent at {when} "
        f"({sent_at.strftime('%d.%m')}); its file arrived only now, so later "
        "messages may already have been answered.]"
    )


def failed_text(kind: str, sent_at: Optional[datetime]) -> str:
    when = f" from {sent_at.strftime('%d.%m %H:%M %Z').strip()}" if sent_at else ""
    return (
        f"⚠️ Telegram did not hand over your {kind_label(kind)}{when} in 2 h "
        "of retries. It is kept: tap Retry to try again."
    )


# --- C5 probe -------------------------------------------------------------


class ProbeGate:
    """Probe each failing file once (bounded memory)."""

    def __init__(self, max_size: int = 512) -> None:
        self._seen: Set[str] = set()
        self._max = max_size

    def should_probe(self, file_id: str) -> bool:
        if file_id in self._seen:
            return False
        if len(self._seen) >= self._max:
            self._seen.clear()
        self._seen.add(file_id)
        return True


async def _timed(call: Callable[[], Awaitable[Any]]) -> Dict[str, Any]:
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    try:
        await call()
        return {"ok": True, "seconds": round(loop.time() - t0, 3)}
    except Exception as exc:
        return {
            "ok": False,
            "seconds": round(loop.time() - t0, 3),
            "error": f"{type(exc).__name__}: {exc}"[:200],
        }


async def probe_get_file(
    *,
    file_id: str,
    get_me: Callable[[], Awaitable[Any]],
    get_file: Callable[..., Awaitable[Any]],
    pool_state: Dict[str, Any],
) -> Dict[str, Any]:
    """Run getMe and getFile(file_id) in parallel, log one line with both."""
    me, gf = await asyncio.gather(_timed(get_me), _timed(lambda: get_file(file_id)))
    result = {"file_id": file_id, "getMe": me, "getFile": gf, "pool": pool_state}
    logger.warning(
        "getFile probe",
        file_id=file_id,
        get_me_ok=me["ok"],
        get_me_seconds=me["seconds"],
        get_me_error=me.get("error"),
        get_file_ok=gf["ok"],
        get_file_seconds=gf["seconds"],
        get_file_error=gf.get("error"),
        pool=pool_state,
    )
    return result


def pool_state(bot: Any) -> Dict[str, Any]:
    """Best-effort snapshot of PTB's httpx connection pool."""
    state: Dict[str, Any] = {}
    try:
        request = bot.request
        client = getattr(request, "_client", None)
        pool = getattr(getattr(client, "_transport", None), "_pool", None)
        conns = list(getattr(pool, "connections", []) or [])
        state["size"] = getattr(pool, "_max_connections", None)
        state["connections"] = len(conns)
        state["idle"] = sum(1 for c in conns if c.is_idle())
        state["waiting"] = len(getattr(pool, "_requests", []) or [])
    except Exception as exc:  # the snapshot must never break the probe
        state["error"] = str(exc)[:100]
    return state


_GATE = ProbeGate()
_PROBE_TASKS: Set["asyncio.Task[Any]"] = set()


def maybe_probe(media: Any) -> None:
    """Fire the probe for a timed-out getFile, once per file, off the call."""
    file_id = getattr(media, "file_id", None)
    if not isinstance(file_id, str) or not _GATE.should_probe(file_id):
        return
    try:
        bot = media.get_bot()
        token = bot.token
    except Exception:
        return
    task = asyncio.ensure_future(_live_probe(bot, str(token), file_id))
    _PROBE_TASKS.add(task)
    task.add_done_callback(_PROBE_TASKS.discard)


async def _live_probe(bot: Any, token: str, file_id: str) -> None:
    import httpx

    base = f"https://api.telegram.org/bot{token}"
    transport = httpx.AsyncHTTPTransport(local_address="0.0.0.0")
    try:
        async with httpx.AsyncClient(transport=transport, timeout=30) as client:

            async def call(method: str, data: Optional[dict] = None) -> None:
                resp = await client.post(f"{base}/{method}", data=data or {})
                body = resp.json()
                if not body.get("ok"):
                    raise RuntimeError(str(body.get("description")))

            await probe_get_file(
                file_id=file_id,
                get_me=lambda: call("getMe"),
                get_file=lambda fid: call("getFile", {"file_id": fid}),
                pool_state=pool_state(bot),
            )
    except Exception as exc:
        # Never log the bot token, even if a future httpx puts the URL in it.
        error = str(exc).replace(token, "<token>") if token else str(exc)
        logger.warning("getFile probe failed", error=error[:200])


# --- background retry -------------------------------------------------------


class MediaRetrier:
    """Retry a pending_media row until its file arrives, then run its turn.

    ``fetch(row)`` downloads the row's file(s) once (no inline retries) and
    returns the data; ``run_turn(row, data)`` runs the normal media turn;
    ``notify(row)`` tells the user the window ended. A transient error from
    either is retried; cancellation (/stop) closes the row as ``stopped``.
    """

    def __init__(
        self,
        *,
        storage: Any,
        run_turn: Callable[[Dict[str, Any], Any], Awaitable[Any]],
        fetch: Callable[[Dict[str, Any]], Awaitable[Any]],
        notify: Callable[[Dict[str, Any]], Awaitable[Any]],
        delays: tuple = BACKGROUND_DELAYS,
        window_seconds: float = BACKGROUND_WINDOW_SECONDS,
        registry: Optional[MediaRegistry] = None,
    ) -> None:
        self._repo = storage.pending_media
        self._run_turn = run_turn
        self._fetch = fetch
        self._notify = notify
        self._delays = tuple(delays) or (0.0,)
        self._window = window_seconds
        self._registry = registry
        self._tasks: Set["asyncio.Task[Any]"] = set()
        # Set at bot shutdown: a cancel then is the process ending, not /stop,
        # so the row stays open for the restart pickup.
        self.closing = False

    def _delay(self, attempt: int) -> float:
        return float(self._delays[min(attempt, len(self._delays) - 1)])

    def schedule(
        self, row_id: int, user_id: int, profile_slug: Optional[str]
    ) -> "asyncio.Task[Any]":
        """Run ``process`` as a task /stop can cancel."""
        task = asyncio.ensure_future(self.process(row_id))
        self._tasks.add(task)
        if self._registry is not None:
            self._registry.track(user_id, profile_slug, task)

        def _done(t: "asyncio.Task[Any]") -> None:
            self._tasks.discard(t)
            if self._registry is not None:
                self._registry.untrack(user_id, profile_slug, t)
            if not t.cancelled() and t.exception() is not None:
                logger.error("media retry crashed", error=str(t.exception()))

        task.add_done_callback(_done)
        return task

    async def process(self, row_id: int) -> None:
        if not await self._repo.claim(row_id):
            return  # someone else runs it, or it is closed
        row = await self._repo.get(row_id)
        if row is None:
            return
        start = row.get("window_start") or row.get("received_at") or time.time()
        deadline = float(start) + self._window
        attempt = 0
        try:
            while True:
                try:
                    data = await self._fetch(row)
                    await self._run_turn(row, data)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    if not is_transient_network_error(exc):
                        logger.error(
                            "media retry gave up",
                            row_id=row_id,
                            error=str(exc)[:200],
                        )
                        await self._fail(row, str(exc))
                        return
                    await self._repo.note_attempt(row_id, str(exc) or repr(exc))
                    now = time.time()
                    if now >= deadline:
                        await self._fail(row, str(exc) or repr(exc))
                        return
                    logger.warning(
                        "media retry: file not available yet",
                        row_id=row_id,
                        attempt=attempt + 1,
                        error=str(exc) or repr(exc),
                    )
                    await asyncio.sleep(min(self._delay(attempt), deadline - now))
                    attempt += 1
                    continue
                await self._repo.close(row_id, "done")
                logger.info("media retry: turn ran", row_id=row_id, attempts=attempt)
                return
        except asyncio.CancelledError:
            if self.closing:
                logger.info("media retry left for restart", row_id=row_id)
                raise
            await self._repo.close(row_id, "stopped")
            logger.info("media retry stopped", row_id=row_id)
            raise

    async def _fail(self, row: Dict[str, Any], error: str) -> None:
        await self._repo.close(row["id"], "failed", error[:500])
        try:
            await self._notify(row)
        except Exception as exc:
            logger.warning("media failure notice failed", error=str(exc))

    async def resume_all(self) -> int:
        """Startup pickup: every open row is retried from its stored update.
        Only at start: a ``running`` row there was cut by the restart."""
        await self._repo.requeue_running()
        try:
            await self._repo.prune()
        except Exception as exc:
            logger.warning("pending_media prune failed", error=str(exc))
        rows = await self._repo.list_open()
        tasks = [self.schedule(r["id"], r["user_id"], r["profile_slug"]) for r in rows]
        if rows:
            logger.info("media retry: resuming open rows", count=len(rows))
        await asyncio.gather(*tasks, return_exceptions=True)
        return len(rows)

    async def retry_failed(self, row_id: int) -> bool:
        """Reopen a ``failed`` row with a fresh window and retry it."""
        if not await self._repo.reopen_failed(row_id):
            return False
        await self.process(row_id)
        return True
