"""Per-(user, agent) turn serialization, shared across entry points.

One agent turn at a time per user *and agent* — regardless of whether the turn
came from a Telegram message (MessageOrchestrator) or an event-driven prompt
(AgentHandler: scheduler, webhooks, job-finish digests). Two turns on the same
agent for the same user race on the resumed session file (last writer wins).
Turns on different agents touch different sessions and run concurrently, so a
long turn in one forum topic does not block a DM to another agent.

The lock also knows who holds it and who waits (dv-19a6), so a message that
queues can say "behind what", and a shutdown can list what it is dropping.
"""

import asyncio
import contextlib
import time
import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Dict, List, Literal, Optional, Tuple


@dataclass(eq=False)
class Waiter:
    """A message parked behind a running turn."""

    user_id: int
    chat_id: int
    kind: str  # "text" | "voice" | "photo" | "document"
    enqueued_at: float = field(default_factory=time.time)
    profile_id: Optional[str] = None
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    jumped: bool = False  # user pressed "interrupt and run this" (dv-8b49)
    stopping: bool = False  # that press's stop is still running (dv-b399)
    note_due: bool = False  # a running turn was really stopped for this one
    gate: asyncio.Event = field(default_factory=asyncio.Event)


# The waiter the current task registered; acquire() clears it once it holds
# the lock, so only messages still blocked are reported as dropped.
_current_waiter: ContextVar[Optional[Waiter]] = ContextVar(
    "turn_lock_waiter", default=None
)

# The waiter whose acquire() this task last completed (turn-scoped, dv-8b49).
_held_waiter: ContextVar[Optional[Waiter]] = ContextVar(
    "turn_lock_held_waiter", default=None
)


def take_interrupt_note() -> bool:
    """True exactly once for a turn that jumped the queue by stopping the
    previous one: the caller prepends "previous turn was interrupted"."""
    waiter = _held_waiter.get()
    if waiter is not None and waiter.note_due:
        waiter.note_due = False
        return True
    return False


CHAT_HOLDER = "your previous message"

# Longest an internal prompt yields to parked user messages (dv-b399).
_INTERNAL_YIELD_MAX_SECONDS = 1800.0


class TrackedLock(asyncio.Lock):
    """asyncio.Lock that remembers its holder label and its waiters."""

    def __init__(self) -> None:
        super().__init__()
        self.holder: Optional[str] = None
        self.waiters: List[Waiter] = []
        # Set while no user message is parked. Unregistered acquirers
        # (job-finish digests, scheduler, webhooks) wait for it, so a user's
        # queued message is never overtaken by an internal prompt (dv-b399).
        self.no_waiters = asyncio.Event()
        self.no_waiters.set()

    def add_waiter(self, waiter: Waiter) -> None:
        self.waiters.append(waiter)
        self.no_waiters.clear()

    async def acquire(self) -> Literal[True]:
        waiter = _current_waiter.get()
        if waiter is None:
            # Bounded: a parked waiter whose task died before acquire() must
            # not hold internal prompts back forever.
            try:
                await asyncio.wait_for(
                    self.no_waiters.wait(), _INTERNAL_YIELD_MAX_SECONDS
                )
            except asyncio.TimeoutError:
                pass
        try:
            if waiter is not None and waiter in self.waiters:
                # Registered waiters go one at a time, in ``waiters`` order
                # (which a "jump" can change); release() opens the head's gate.
                if self.locked() or self.waiters[0] is not waiter:
                    await waiter.gate.wait()
            return await super().acquire()
        finally:
            if waiter is not None:
                _current_waiter.set(None)
                _held_waiter.set(waiter)
                if waiter in self.waiters:
                    self.waiters.remove(waiter)
                if not self.waiters:
                    self.no_waiters.set()
                if not self.locked():
                    self._promote()  # a cancelled head must not strand the rest

    def _promote(self) -> None:
        if self.waiters:
            self.waiters[0].gate.set()

    def release(self) -> None:
        self.holder = None
        super().release()
        self._promote()

    def jump(self, waiter: Waiter) -> bool:
        """Move a parked waiter to the front. False if it is not parked."""
        if waiter not in self.waiters:
            return False
        self.waiters.remove(waiter)
        self.waiters.insert(0, waiter)
        waiter.jumped = True
        return True

    def queue_note(self) -> str:
        """Visible text for a message that has to wait for this lock."""
        behind = self.holder or CHAT_HOLDER
        ahead = len(self.waiters)
        extra = f" (+{ahead} queued before it)" if ahead else ""
        return f"⏳ Queued behind: {behind}{extra}. Runs when it finishes."


class TurnLocks:
    """Registry of per-(user, profile) asyncio locks. Create once, share
    everywhere. ``profile_id`` is the profile slug the turn runs on; None
    means "unknown / single profile" and shares one slot per user."""

    def __init__(self) -> None:
        self._locks: Dict[Tuple[int, str], TrackedLock] = {}

    def get(self, user_id: int, profile_id: Optional[str] = None) -> TrackedLock:
        key = (user_id, profile_id or "")
        lock = self._locks.get(key)
        if lock is None:
            lock = self._locks[key] = TrackedLock()
        return lock

    def enqueue(
        self,
        user_id: int,
        chat_id: int,
        kind: str,
        profile_id: Optional[str] = None,
    ) -> Waiter:
        """Register the calling task as waiting; its next acquire() clears it."""
        waiter = Waiter(
            user_id=user_id, chat_id=chat_id, kind=kind, profile_id=profile_id
        )
        self.get(user_id, profile_id).add_waiter(waiter)
        _current_waiter.set(waiter)
        return waiter

    @contextlib.asynccontextmanager
    async def hold(
        self, user_id: int, label: str, profile_id: Optional[str] = None
    ) -> AsyncIterator[None]:
        """Take the (user, agent) turn slot, labelled so queued messages can
        say what they wait behind."""
        lock = self.get(user_id, profile_id)
        async with lock:
            lock.holder = label
            yield

    def find_waiter(self, waiter_id: str) -> Optional[Waiter]:
        """A still-parked waiter by id (None once it started running)."""
        for w in self.pending():
            if w.id == waiter_id:
                return w
        return None

    def pending(self) -> List[Waiter]:
        """Every message still blocked on a lock (for the shutdown report)."""
        return [w for lock in self._locks.values() for w in lock.waiters]


async def report_dropped_messages(turn_locks: TurnLocks, bot: Any) -> int:
    """On shutdown, tell each user which queued messages are being dropped
    (the in-memory queue does not survive). Returns how many were reported."""
    by_chat: Dict[int, List[Waiter]] = {}
    for w in turn_locks.pending():
        by_chat.setdefault(w.chat_id, []).append(w)
    total = 0
    for chat_id, items in by_chat.items():
        lines = [
            f"• {time.strftime('%H:%M:%S', time.gmtime(w.enqueued_at))} UTC — {w.kind}"
            for w in items
        ]
        text = (
            "⚠️ Restart: these queued messages were not processed, "
            "please resend:\n" + "\n".join(lines)
        )
        try:
            await asyncio.wait_for(bot.send_message(chat_id=chat_id, text=text), 5)
            total += len(items)
        except Exception:
            pass
    return total
