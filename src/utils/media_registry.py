"""The media phase of a turn, per (user, agent), as cancellable tasks (dv-c977).

A voice / photo / document turn first downloads (and for voice transcribes)
before any backend turn exists, and a file Telegram will not hand out is
retried in the background for up to 2 h. Neither is a backend turn, so
``/stop`` used to answer "Nothing is running" during "Transcribing...".
Both register their task here; ``_stop_scoped_turns`` cancels them. Lives
next to ``TurnLocks``: one stop mechanism, keyed like the turn lock.
"""

import asyncio
from typing import Dict, Optional, Set, Tuple

_Key = Tuple[int, str]


def _key(user_id: int, profile_id: Optional[str]) -> _Key:
    return (int(user_id), profile_id or "")


class MediaRegistry:
    """In-memory set of live media-phase tasks per (user, profile)."""

    def __init__(self) -> None:
        self._tasks: Dict[_Key, Set["asyncio.Future[object]"]] = {}

    def track(
        self, user_id: int, profile_id: Optional[str], task: "asyncio.Future"
    ) -> None:
        self._tasks.setdefault(_key(user_id, profile_id), set()).add(task)

    def untrack(
        self, user_id: int, profile_id: Optional[str], task: "asyncio.Future"
    ) -> None:
        key = _key(user_id, profile_id)
        tasks = self._tasks.get(key)
        if tasks is None:
            return
        tasks.discard(task)
        if not tasks:
            self._tasks.pop(key, None)

    def active(self, user_id: int, profile_id: Optional[str]) -> int:
        tasks = self._tasks.get(_key(user_id, profile_id), set())
        return sum(1 for t in tasks if not t.done())

    def cancel(self, user_id: int, profile_id: Optional[str]) -> int:
        """Cancel every live media task of the scope; returns how many."""
        tasks = self._tasks.pop(_key(user_id, profile_id), set())
        n = 0
        for task in tasks:
            if not task.done():
                task.cancel()
                n += 1
        return n

    def cancel_all(self) -> int:
        n = 0
        for key in list(self._tasks):
            user_id, profile = key
            n += self.cancel(user_id, profile)
        return n
