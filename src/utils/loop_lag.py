"""Event-loop lag monitor: logs when the loop runs late (dv-586f)."""

import asyncio
import time

import structlog

logger = structlog.get_logger()


async def monitor_loop_lag(
    interval: float = 0.25, threshold: float = 0.5, cooldown: float = 5.0
) -> None:
    """Sleep *interval* repeatedly; warn when a wake-up is > *threshold* late.

    Runs until cancelled. Warnings are rate-limited by *cooldown* seconds so a
    long stall logs once, with the worst lag seen since the last line.
    """
    loop = asyncio.get_running_loop()
    last_log = 0.0
    worst = 0.0
    while True:
        start = loop.time()
        await asyncio.sleep(interval)
        lag = loop.time() - start - interval
        worst = max(worst, lag)
        if lag > threshold and time.monotonic() - last_log >= cooldown:
            logger.warning("Event loop lag", lag_s=round(worst, 2))
            last_log = time.monotonic()
            worst = 0.0
        elif lag <= threshold and time.monotonic() - last_log >= cooldown:
            worst = 0.0
