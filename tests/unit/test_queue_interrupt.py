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


async def test_internal_prompt_yields_to_parked_user_messages():
    """dv-b399: a job-finish digest arriving while user messages are parked
    runs after them, not ahead (asyncio.Lock FIFO used to let it cut in)."""
    import contextvars

    locks = TurnLocks()
    lock = locks.get(1, "a")
    await lock.acquire()  # running turn
    order: list = []
    _, t1, _ = await _queued(locks, order, "user1")

    async def internal():
        async with locks.hold(1, "job digest", "a"):
            order.append("internal")

    ti = asyncio.get_running_loop().create_task(
        internal(), context=contextvars.Context()
    )
    await asyncio.sleep(0)
    _, t2, _ = await _queued(locks, order, "user2")
    lock.release()
    await asyncio.wait_for(asyncio.gather(t1, t2, ti), 2)
    assert [o for o in order if isinstance(o, str)] == ["user1", "user2", "internal"]


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
    o._interrupt_tasks = set()
    o._finish_interrupt = lambda *a: CallbacksMixin._finish_interrupt(o, *a)
    return o


async def _settle(o):
    """Let the off-handler stop (dv-b399) finish."""
    await asyncio.gather(*o._interrupt_tasks)


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
    gate = asyncio.Event()

    async def slow_stop(*a):
        await gate.wait()
        return 1

    o._stop_scoped_turns = AsyncMock(side_effect=slow_stop)
    for _ in range(2):  # double press while the stop is still running
        await CallbacksMixin._handle_interrupt_callback(o, q, ctx, f"qi:{w.id}")
    assert q.answer.await_args.kwargs.get("show_alert") is True
    gate.set()
    await _settle(o)
    o._stop_scoped_turns.assert_awaited_once_with(ctx, 1, "a")
    assert w.jumped and w.note_due and not w.stopping
    # the refused double press and the jump are both audited (dv-b399)
    assert ctx.bot_data["storage"].log_bot_event.await_count == 2


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
    await _settle(o2)
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
    await _settle(o)
    assert w.jumped and not w.note_due


async def test_press_is_answered_and_shown_before_the_slow_stop():
    """dv-b399 round 2: answer + visible change come first; a stop that takes
    15 s must not make Telegram reject the answer ("Query is too old")."""
    locks = TurnLocks()
    await locks.get(1, "a").acquire()
    w = locks.enqueue(1, 9, "text", "a")
    o = _orch(locks)
    gate = asyncio.Event()

    async def slow_stop(*a):
        await gate.wait()
        return 1

    o._stop_scoped_turns = AsyncMock(side_effect=slow_stop)
    q = _query(1)
    await asyncio.wait_for(
        CallbacksMixin._handle_interrupt_callback(o, q, _ctx(), f"qi:{w.id}"), 1
    )
    q.answer.assert_awaited_once()  # answered while the stop is still pending
    q.edit_message_text.assert_awaited_once()
    assert "Останавливаю" in q.edit_message_text.await_args.args[0]
    assert q.edit_message_text.await_args.kwargs.get("reply_markup") is None
    assert not gate.is_set() and w.stopping and w.note_due
    gate.set()
    await _settle(o)
    assert "остановлен" in q.edit_message_text.await_args.args[0]
    assert not w.stopping


async def test_second_press_after_a_no_op_stop_goes_through():
    """Nothing was running yet -> the button comes back and a second press
    runs the stop again (no permanent 'already pressed')."""
    locks = TurnLocks()
    await locks.get(1, "a").acquire()
    w = locks.enqueue(1, 9, "text", "a")
    o = _orch(locks, stopped=0)
    q = _query(1)
    await CallbacksMixin._handle_interrupt_callback(o, q, _ctx(), f"qi:{w.id}")
    await _settle(o)
    assert q.edit_message_text.await_args.kwargs.get("reply_markup") is not None
    o._stop_scoped_turns.return_value = 1
    q2 = _query(1)
    await CallbacksMixin._handle_interrupt_callback(o, q2, _ctx(), f"qi:{w.id}")
    await _settle(o)
    assert o._stop_scoped_turns.await_count == 2
    q2.answer.assert_awaited_once()
    assert not q2.answer.await_args.kwargs.get("show_alert")
    assert w.note_due


async def test_failing_stop_still_reports_and_clears_state():
    locks = TurnLocks()
    await locks.get(1, "a").acquire()
    w = locks.enqueue(1, 9, "text", "a")
    o = _orch(locks)
    o._stop_scoped_turns = AsyncMock(side_effect=RuntimeError("boom"))
    q = _query(1)
    await CallbacksMixin._handle_interrupt_callback(o, q, _ctx(), f"qi:{w.id}")
    await _settle(o)
    assert not w.stopping and not w.note_due
    assert "стартует" in q.edit_message_text.await_args.args[0]


async def test_queued_voice_and_text_all_run_in_order_after_a_stop():
    """dv-b399 round 2: every queued message runs once, none lost; the
    pressed one first, the rest keep arrival order (voice is transcribed only
    when it is dequeued, inside the lock)."""
    locks = TurnLocks()
    lock = locks.get(1, "a")
    await lock.acquire()  # the running turn
    order: list = []

    async def msg(name, kind):
        w = locks.enqueue(1, 9, kind, "a")

        async def run():
            async with lock:
                order.append(name)  # transcription + turn happen here

        t = asyncio.ensure_future(run())
        await asyncio.sleep(0)
        return w, t

    w1, t1 = await msg("voice1", "voice")
    w2, t2 = await msg("text1", "text")
    w3, t3 = await msg("voice2", "voice")
    w4, t4 = await msg("text2", "text")

    o = _orch(locks)
    q = _query(1)
    await CallbacksMixin._handle_interrupt_callback(o, q, _ctx(), f"qi:{w3.id}")
    await _settle(o)
    lock.release()  # the stopped turn ends
    await asyncio.wait_for(asyncio.gather(t1, t2, t3, t4), 2)
    assert order == ["voice2", "voice1", "text1", "text2"]
    assert not locks.pending()
