"""Per-user turn serialization, shared across entry points.

One agent turn at a time per user — regardless of whether the turn came from
a Telegram message (MessageOrchestrator) or an event-driven prompt
(AgentHandler: scheduler, webhooks, job-finish digests). Two turns running
concurrently for the same user mean two parallel claude processes (OOM risk
on this host) and a last-writer-wins race on the resumed session file.
"""

import asyncio
from typing import Dict


class TurnLocks:
    """Registry of per-user asyncio locks. Create once, share everywhere."""

    def __init__(self) -> None:
        self._locks: Dict[int, asyncio.Lock] = {}

    def get(self, user_id: int) -> asyncio.Lock:
        lock = self._locks.get(user_id)
        if lock is None:
            lock = self._locks[user_id] = asyncio.Lock()
        return lock
