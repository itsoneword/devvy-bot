"""dv-586f: a slow Telegram must not slow the SDK stream reader."""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

from src.bot.orchestration.streaming import StreamingMixin
from src.bot.rate_limit import DevvyRateLimiter
from src.bot.utils.draft_streamer import DraftStreamer
from src.bot.utils.ephemeral_updater import EphemeralUpdater

SLOW = 5.0


class _Orch(StreamingMixin):
    pass


class _Upd:
    def __init__(self, type_="assistant", content="", tool_calls=None):
        self.type = type_
        self.content = content
        self.tool_calls = tool_calls


async def test_reader_not_blocked_and_final_after_progress(monkeypatch):
    order = []
    real_sleep = asyncio.sleep

    async def slow_edit(text):
        await real_sleep(1.0)  # artificially slow Telegram (limiter wait)
        order.append("progress")

    progress = MagicMock()
    progress.edit_text = slow_edit
    updater = EphemeralUpdater("t")
    tool_log: list = []
    cb = _Orch(MagicMock(), {})._make_stream_callback(
        1, progress, tool_log, time.time(), updater=updater
    )
    # make every edit eligible (bypass the 2 s throttle)
    import src.bot.orchestration.streaming as st

    clock = iter(range(0, 10000, 10))
    monkeypatch.setattr(st.time, "time", lambda: next(clock))

    t0 = time.monotonic()
    for i in range(200):
        await cb(_Upd(tool_calls=[{"name": "Bash", "input": {"command": f"c{i}"}}]))
    reader_time = time.monotonic() - t0
    assert reader_time < 0.5  # 200 events, each edit would be 0.1 s inline

    await asyncio.sleep(0.05)
    await updater.stop()
    order.append("final")
    assert updater.sent >= 1 and updater.superseded > 0
    assert order[-1] == "final"
    await real_sleep(1.2)
    assert order.count("progress") == updater.sent  # nothing lands after stop


async def test_draft_streamer_uses_updater_and_final_after():
    bot = MagicMock()
    sent = []

    async def slow_draft(**kw):
        await asyncio.sleep(0.2)
        sent.append(kw["text"])

    bot.send_message_draft = slow_draft
    up = EphemeralUpdater("d")
    ds = DraftStreamer(bot, 1, 7, throttle_interval=0.0, updater=up)
    t0 = time.monotonic()
    for i in range(50):
        await ds.append_text("x")
    assert time.monotonic() - t0 < 0.1
    await asyncio.sleep(0.05)  # worker picks up a job
    await up.stop()
    n = len(sent)
    await asyncio.sleep(0.4)
    assert len(sent) == n and n >= 1


async def test_updater_counts_failures():
    up = EphemeralUpdater("f")

    async def boom():
        raise RuntimeError("x")

    up.submit(boom)
    await asyncio.sleep(0.05)
    await up.stop()
    assert up.failed == 1


async def test_chat_action_bypasses_group_limiter():
    rl = DevvyRateLimiter(group_max_rate=1, group_time_period=60)
    cb = AsyncMock(return_value=True)
    t0 = time.monotonic()
    for _ in range(5):
        await rl.process_request(
            cb, (), {}, "sendChatAction", {"chat_id": -100123}, None
        )
    assert time.monotonic() - t0 < 0.5
    assert cb.await_count == 5


async def test_loop_lag_monitor_logs(monkeypatch):
    import time as _t

    from src.utils import loop_lag

    seen = []
    monkeypatch.setattr(loop_lag.logger, "warning", lambda *a, **k: seen.append(k))
    task = asyncio.create_task(loop_lag.monitor_loop_lag(interval=0.05, threshold=0.2))
    await asyncio.sleep(0.1)
    _t.sleep(0.6)  # block the loop
    await asyncio.sleep(0.2)
    task.cancel()
    assert seen and seen[0]["lag_s"] >= 0.2
