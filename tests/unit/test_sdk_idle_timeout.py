"""The SDK run timeout is an idle timeout, not a wall-clock budget.

Before 2026-09-17 the whole turn was wrapped in
``asyncio.wait_for(..., claude_timeout_seconds)``, so a turn that was healthily
streaming tool calls died exactly like a hung one. Two real turns were lost at
600s while sub-agents were still reporting.
"""

import asyncio
import time

import pytest

from src.claude.sdk_integration import _await_with_idle_timeout


async def test_long_but_active_run_is_not_killed() -> None:
    """Total runtime far beyond the limit is fine while messages keep arriving."""
    last_activity = time.monotonic()
    limit = 0.15

    async def streaming_work() -> None:
        nonlocal last_activity
        # Runs for ~6x the idle limit, but never goes quiet for longer than
        # half of it — the old wall-clock timeout would have killed this.
        for _ in range(12):
            await asyncio.sleep(limit / 2)
            last_activity = time.monotonic()

    started = time.monotonic()
    await _await_with_idle_timeout(
        asyncio.ensure_future(streaming_work()), lambda: last_activity, limit
    )
    assert time.monotonic() - started > limit * 3


async def test_silent_run_times_out() -> None:
    """A task that goes quiet still trips the watchdog."""
    last_activity = time.monotonic()
    cancelled = False

    async def stalled_work() -> None:
        nonlocal cancelled
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled = True
            raise

    with pytest.raises(asyncio.TimeoutError):
        await _await_with_idle_timeout(
            asyncio.ensure_future(stalled_work()), lambda: last_activity, 0.1
        )

    # The task must be cancelled, not leaked, so the client disconnects.
    await asyncio.sleep(0)
    assert cancelled


async def test_task_exception_propagates() -> None:
    """Errors from the run still surface instead of being swallowed."""
    last_activity = time.monotonic()

    async def failing_work() -> None:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        await _await_with_idle_timeout(
            asyncio.ensure_future(failing_work()), lambda: last_activity, 5
        )


async def test_already_idle_past_limit_times_out_immediately() -> None:
    """A stamp older than the limit trips before any waiting."""
    stale = time.monotonic() - 10

    async def work() -> None:
        await asyncio.sleep(30)

    task = asyncio.ensure_future(work())
    with pytest.raises(asyncio.TimeoutError):
        await _await_with_idle_timeout(task, lambda: stale, 1)
    assert task.cancelled()
