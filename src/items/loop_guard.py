"""Which users are in an automated (scheduler-driven) turn right now (dv-752a).

A reminder that fires an agent turn must not be able to create reminders for
another family member — otherwise two agents can ping-pong reminders at each
other with no human in the loop. Turns are serialized per user (``TurnLocks``),
so "this user currently has a scheduler-driven turn open" is exact: while the
set holds a user id, every agent tool call made for that user comes from the
automated turn.

Process-global on purpose: both tool transports (the in-process SDK server and
the loopback Codex bridge) run inside the bot process, and neither carries a
contextvar from the scheduler's task into the tool call.
"""

from __future__ import annotations

import contextlib
from typing import Dict, Iterator

_active: Dict[int, int] = {}


@contextlib.contextmanager
def automated_turn(user_id: int) -> Iterator[None]:
    """Mark ``user_id`` as inside a scheduler-driven turn for the block."""
    uid = int(user_id or 0)
    _active[uid] = _active.get(uid, 0) + 1
    try:
        yield
    finally:
        left = _active.get(uid, 1) - 1
        if left <= 0:
            _active.pop(uid, None)
        else:
            _active[uid] = left


def is_automated(user_id: object) -> bool:
    """True while ``user_id`` has a scheduler-driven turn open."""
    try:
        return int(user_id or 0) in _active  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return False
