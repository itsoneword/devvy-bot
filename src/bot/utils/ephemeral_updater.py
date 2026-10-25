"""Per-turn latest-wins background updater for ephemeral Telegram UI.

Progress edits, message drafts and tool lines must never run on the SDK stream
reader path: a rate-limited Telegram call there blocks the reader, the CLI
stalls on its stdout pipe (dv-586f). ``submit`` only stores the newest job in a
single slot and returns; one worker task runs jobs one at a time. No queue:
an unsent older job is superseded.
"""

import asyncio
from typing import Awaitable, Callable, Optional

import structlog

logger = structlog.get_logger()

Job = Callable[[], Awaitable[None]]


class EphemeralUpdater:
    def __init__(self, name: str = "") -> None:
        self.name = name
        self.sent = 0
        self.failed = 0
        self.superseded = 0
        self._slot: Optional[Job] = None
        self._wake = asyncio.Event()
        self._stopped = False
        self._idle = asyncio.Event()
        self._idle.set()
        self._task: Optional["asyncio.Task[None]"] = None

    def submit(self, job: Job) -> None:
        """Store *job* as the next update (replacing any unsent one). Never blocks."""
        if self._stopped:
            return
        if self._slot is not None:
            self.superseded += 1
        self._slot = job
        if self._task is None:
            self._task = asyncio.create_task(self._run())
        self._wake.set()

    async def _run(self) -> None:
        while True:
            await self._wake.wait()
            self._wake.clear()
            job, self._slot = self._slot, None
            if job is None:
                continue
            self._idle.clear()
            try:
                await job()
                self.sent += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.failed += 1
                logger.warning(
                    "Ephemeral UI update failed",
                    updater=self.name,
                    error=str(exc),
                    failed=self.failed,
                )
            finally:
                self._idle.set()

    async def stop(self, timeout: float = 15.0) -> None:
        """Drop the pending job and wait for the in-flight one, then end.

        After this returns no further send can land (call before the final
        reply). A hung in-flight call is cancelled after *timeout*.
        """
        self._stopped = True
        self._slot = None
        task, self._task = self._task, None
        if task is None:
            return
        try:
            await asyncio.wait_for(self._idle.wait(), timeout)
        except asyncio.TimeoutError:
            logger.warning("Ephemeral UI update hung, cancelling", updater=self.name)
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    def summary(self) -> dict:  # type: ignore[type-arg]
        return {
            "sent": self.sent,
            "failed": self.failed,
            "superseded": self.superseded,
        }
