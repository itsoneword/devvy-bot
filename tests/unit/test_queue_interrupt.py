"""dv-8b49: "interrupt current and run this" on a queued message."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from src.bot.orchestration.callbacks import CallbacksMixin
from src.utils.turn_locks import TurnLocks, take_interrupt_note


async def _queued(locks, order, name, uid=1, prof="a", jump_after=None):
    lock = locks.get(uid, prof)
    w = locks.enqueue(uid, 9, "text", prof)
    ready = asyncio.Event()

    async def run():
        async with lock:
            order.append(name)
            order.append((name, "note", take_interrupt_note()))

    task = asyncio.ensure_future(run())
    await asyncio.sleep(0)
    return w, task, ready


async def test_jump_runs_first_others_keep_order_note_once():
    locks = TurnLocks()
    lock = locks.get(1, "a")
    await lock.acquire()  # running turn
    order: list = []
    w1, t1, _ = await _queued(locks, order, "m1")
    w2, t2, _ = await _queued(locks, order, "m2")
    w3, t3, _ = await _queued(locks, order, "m3")
    assert lock.jump(w3)
    w3.note_due = True
    lock.release()
    await asyncio.gather(t1, t2, t3)
    names = [o for o in order if isinstance(o, str)]
    assert names == ["m3", "m1", "m2"]
    notes = {o[0]: o[2] for o in order if isinstance(o, tuple)}
    assert notes == {"m3": True, "m1": False, "m2": False}


async def test_fifo_without_jump():
    locks = TurnLocks()
    lock = locks.get(1, "a")
    await lock.acquire()
    order: list = []
    ts = [(await _queued(locks, order, n))[1] for n in ("m1", "m2")]
    lock.release()
    await asyncio.gather(*ts)
    assert [o for o in order if isinstance(o, str)] == ["m1", "m2"]


async def test_stale_waiter_not_found_and_jump_refused():
    locks = TurnLocks()
    lock = locks.get(1, "a")
    await lock.acquire()
    order: list = []
    w, t, _ = await _queued(locks, order, "m1")
    assert locks.find_waiter(w.id) is w
    lock.release()
    await t
    assert locks.find_waiter(w.id) is None
    assert not lock.jump(w)


def _orch(locks, stopped=1, owner=False):
    o = SimpleNamespace()
    o._turn_locks = locks
    o._resolve_role = lambda uid, ctx: "owner" if owner else "user"
    o._stop_scoped_turns = AsyncMock(return_value=stopped)
    o._drop_buttons = CallbacksMixin._drop_buttons
    return o


def _query(uid):
    q = MagicMock()
    q.from_user.id = uid
    q.answer = AsyncMock()
    q.edit_message_text = AsyncMock()
    q.message.reply_text = AsyncMock()
    return q


def _ctx():
    return SimpleNamespace(bot_data={"storage": MagicMock(log_bot_event=AsyncMock())})


async def test_press_scopes_stop_to_author_and_agent_and_is_idempotent():
    locks = TurnLocks()
    await locks.get(1, "a").acquire()
    w = locks.enqueue(1, 9, "text", "a")
    o = _orch(locks)
    ctx = _ctx()
    q = _query(1)
    for _ in range(2):  # double press
        await CallbacksMixin._handle_interrupt_callback(o, q, ctx, f"qi:{w.id}")
    o._stop_scoped_turns.assert_awaited_once_with(ctx, 1, "a")
    assert w.jumped and w.note_due
    ctx.bot_data["storage"].log_bot_event.assert_awaited_once()


async def test_press_by_other_user_refused_owner_allowed():
    locks = TurnLocks()
    await locks.get(1, "a").acquire()
    w = locks.enqueue(1, 9, "text", "a")
    o = _orch(locks)
    await CallbacksMixin._handle_interrupt_callback(o, _query(2), _ctx(), f"qi:{w.id}")
    o._stop_scoped_turns.assert_not_awaited()
    assert not w.jumped
    o2 = _orch(locks, owner=True)
    await CallbacksMixin._handle_interrupt_callback(o2, _query(2), _ctx(), f"qi:{w.id}")
    o2._stop_scoped_turns.assert_awaited_once()
    assert w.jumped


async def test_stale_button_does_not_stop_anything():
    locks = TurnLocks()
    o = _orch(locks)
    q = _query(1)
    await CallbacksMixin._handle_interrupt_callback(o, q, _ctx(), "qi:deadbeef")
    o._stop_scoped_turns.assert_not_awaited()
    q.edit_message_text.assert_awaited_once()


async def test_nothing_running_means_no_note():
    locks = TurnLocks()
    await locks.get(1, "a").acquire()
    w = locks.enqueue(1, 9, "text", "a")
    o = _orch(locks, stopped=0)
    await CallbacksMixin._handle_interrupt_callback(o, _query(1), _ctx(), f"qi:{w.id}")
    assert w.jumped and not w.note_due
