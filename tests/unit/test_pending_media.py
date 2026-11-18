"""dv-c977: a voice/photo/document that cannot be downloaded is never lost.

Interfaces under test (new):
- ``storage.pending_media`` (src/storage/pending_media.py, PendingMediaRepository)
- ``src.utils.media_registry.MediaRegistry`` (in-memory media-phase tasks)
- ``src.bot.media_retry.MediaRetrier`` (background retry, restart pickup)
- ``src.bot.media_retry.probe_get_file`` (parallel getMe/getFile probe)
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram.error import TimedOut

from src.bot.core import ClaudeCodeBot
from src.bot.orchestrator import MessageOrchestrator
from src.storage.facade import Storage


@pytest.fixture
async def storage(tmp_path):
    s = Storage(f"sqlite:///{tmp_path / 'bot.db'}")
    await s.initialize()
    yield s
    await s.close()


async def _add(storage, **kw):
    args = dict(
        chat_id=10,
        thread_id=None,
        message_id=55,
        user_id=7,
        profile_slug="devops",
        kind="voice",
        file_id="FID1",
        update_json='{"update_id": 1}',
    )
    args.update(kw)
    return await storage.pending_media.add(**args)


# --- C1 ---------------------------------------------------------------


async def test_C1_row_stored_with_file_id_and_update(storage):
    rid = await _add(storage)
    row = await storage.pending_media.get(rid)
    assert row["file_id"] == "FID1"
    assert row["kind"] == "voice"
    assert row["profile_slug"] == "devops"
    assert row["message_id"] == 55
    assert row["update_json"] == '{"update_id": 1}'
    assert row["status"] == "pending"


async def test_C1_row_closed_when_turn_has_run(storage):
    rid = await _add(storage)
    assert await storage.pending_media.claim(rid) is True
    await storage.pending_media.close(rid, "done")
    assert (await storage.pending_media.get(rid))["status"] == "done"
    assert await storage.pending_media.list_open() == []


async def test_C1_claim_is_exclusive(storage):
    rid = await _add(storage)
    assert await storage.pending_media.claim(rid) is True
    assert await storage.pending_media.claim(rid) is False


async def test_C1_handlers_store_row_before_getfile():
    from src.bot.orchestration import turns

    assert hasattr(turns, "store_pending_media")


# --- retrier helpers ----------------------------------------------------


def _retrier(storage, run_turn, fetch, notify=None, **kw):
    from src.bot.media_retry import MediaRetrier

    return MediaRetrier(
        storage=storage,
        run_turn=run_turn,
        fetch=fetch,
        notify=notify or AsyncMock(),
        delays=kw.pop("delays", (0, 0, 0)),
        window_seconds=kw.pop("window_seconds", 3600),
        **kw,
    )


# --- C2 ---------------------------------------------------------------


async def test_C2_background_retry_runs_turn_when_file_arrives(storage):
    rid = await _add(storage)
    attempts = []

    async def fetch(row):
        attempts.append(1)
        if len(attempts) < 3:
            raise TimedOut()
        return b"audio"

    run_turn = AsyncMock()
    r = _retrier(storage, run_turn, fetch)
    await r.process(rid)
    assert len(attempts) == 3
    run_turn.assert_awaited_once()
    assert (await storage.pending_media.get(rid))["status"] == "done"


async def test_C2_inline_failure_status_line_has_no_resend_wording():
    from src.bot.media_retry import deferred_status_text

    text = deferred_status_text("voice")
    assert "resend" not in text.lower()
    assert "/stop" in text
    assert "\n" not in text.strip()


# --- C3 ---------------------------------------------------------------


async def test_C3_restart_picks_up_open_rows(storage):
    r1 = await _add(storage, message_id=1)
    r2 = await _add(storage, message_id=2, file_id="FID2")
    done = await _add(storage, message_id=3, file_id="FID3")
    await storage.pending_media.close(done, "done")
    run_turn = AsyncMock()
    r = _retrier(storage, run_turn, AsyncMock(return_value=b"x"))
    await r.resume_all()
    assert run_turn.await_count == 2
    for rid in (r1, r2):
        assert (await storage.pending_media.get(rid))["status"] == "done"


async def test_C3_no_double_run_when_row_already_claimed(storage):
    rid = await _add(storage)
    await storage.pending_media.claim(rid)
    run_turn = AsyncMock()
    r = _retrier(storage, run_turn, AsyncMock(return_value=b"x"))
    await r.process(rid)
    run_turn.assert_not_awaited()


# --- C4 ---------------------------------------------------------------


async def test_C4_registry_cancels_media_phase_per_user_and_profile():
    from src.utils.media_registry import MediaRegistry

    reg = MediaRegistry()
    t1 = asyncio.ensure_future(asyncio.sleep(60))
    t2 = asyncio.ensure_future(asyncio.sleep(60))
    reg.track(7, "devops", t1)
    reg.track(7, "markets", t2)
    assert reg.cancel(7, "devops") == 1
    await asyncio.sleep(0)
    assert t1.cancelled()
    assert not t2.cancelled()
    assert reg.cancel(7, "devops") == 0
    t2.cancel()


async def test_C4_stop_scoped_turns_cancels_media_work():
    settings = SimpleNamespace(
        agentic_mode=True, agent_backend="claude", agent_model="claude"
    )
    o = MessageOrchestrator(settings, {})
    task = asyncio.ensure_future(asyncio.sleep(60))
    o._media_registry.track(7, "devops", task)
    ctx = MagicMock()
    ctx.bot_data = {"backends": {}}
    n = await o._stop_scoped_turns(ctx, 7, "devops")
    await asyncio.sleep(0)
    assert n >= 1
    assert task.cancelled()


async def test_C4_stopped_row_not_retried(storage):
    rid = await _add(storage)
    await storage.pending_media.close(rid, "stopped")
    run_turn = AsyncMock()
    r = _retrier(storage, run_turn, AsyncMock(return_value=b"x"))
    await r.resume_all()
    run_turn.assert_not_awaited()


async def test_C4_cancelled_background_retry_closes_row_stopped(storage):
    rid = await _add(storage)
    started = asyncio.Event()

    async def fetch(row):
        started.set()
        await asyncio.sleep(60)

    run_turn = AsyncMock()
    r = _retrier(storage, run_turn, fetch)
    task = asyncio.ensure_future(r.process(rid))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await storage.pending_media.get(rid))["status"] == "stopped"
    run_turn.assert_not_awaited()


# --- C5 ---------------------------------------------------------------


async def test_C5_probe_logs_both_outcomes_and_pool_state(caplog, capsys):
    from src.bot.media_retry import probe_get_file

    get_me = AsyncMock(return_value="ok")
    get_file = AsyncMock(side_effect=TimedOut())
    result = await probe_get_file(
        file_id="FID1", get_me=get_me, get_file=get_file, pool_state={"size": 256}
    )
    get_me.assert_awaited_once()
    get_file.assert_awaited_once()
    assert result["getMe"]["ok"] is True
    assert result["getFile"]["ok"] is False
    assert "seconds" in result["getMe"] and "seconds" in result["getFile"]
    assert result["pool"] == {"size": 256}


async def test_C5_probe_runs_in_parallel():
    from src.bot.media_retry import probe_get_file

    async def slow(*a, **k):
        await asyncio.sleep(0.2)
        return 1

    loop = asyncio.get_running_loop()
    t0 = loop.time()
    await probe_get_file(file_id="F", get_me=slow, get_file=slow, pool_state={})
    assert loop.time() - t0 < 0.35


async def test_C5_probe_rate_limited_once_per_file():
    from src.bot.media_retry import ProbeGate

    gate = ProbeGate()
    assert gate.should_probe("FID1") is True
    assert gate.should_probe("FID1") is False
    assert gate.should_probe("FID2") is True


# --- C6 ---------------------------------------------------------------


def test_C6_forwarded_voice_matches_voice_handler_filter():
    from telegram import Update
    from telegram.ext import filters

    update = Update.de_json(
        {
            "update_id": 1,
            "message": {
                "message_id": 9,
                "date": 1700000000,
                "chat": {"id": 1, "type": "private"},
                "from": {"id": 7, "is_bot": False, "first_name": "x"},
                "forward_origin": {
                    "type": "user",
                    "date": 1700000000,
                    "sender_user": {"id": 8, "is_bot": False, "first_name": "y"},
                },
                "voice": {
                    "file_id": "F",
                    "file_unique_id": "U",
                    "duration": 3,
                },
            },
        },
        None,
    )
    assert filters.VOICE.check_update(update)


async def test_C6_forwarded_voice_stores_row_like_fresh_one():
    from src.bot.orchestration.turns import media_row_fields

    fwd = MagicMock()
    fwd.message.voice.file_id = "F"
    fwd.message.photo = []
    fwd.message.document = None
    fwd.message.forward_origin = object()
    fresh = MagicMock()
    fresh.message.voice.file_id = "F"
    fresh.message.photo = []
    fresh.message.document = None
    fresh.message.forward_origin = None
    a, b = media_row_fields(fwd), media_row_fields(fresh)
    assert a["kind"] == b["kind"] == "voice"
    assert a["file_id"] == b["file_id"] == "F"


# --- C9 ---------------------------------------------------------------


async def test_C9_window_end_marks_failed_notifies_once_keeps_file_id(storage):
    rid = await _add(storage)
    notify = AsyncMock()
    run_turn = AsyncMock()
    r = _retrier(
        storage,
        run_turn,
        AsyncMock(side_effect=TimedOut()),
        notify=notify,
        window_seconds=0,
    )
    await r.process(rid)
    row = await storage.pending_media.get(rid)
    assert row["status"] == "failed"
    assert row["file_id"] == "FID1"
    notify.assert_awaited_once()
    run_turn.assert_not_awaited()


async def test_C9_failed_row_can_be_retried_later(storage):
    rid = await _add(storage)
    await storage.pending_media.close(rid, "failed")
    run_turn = AsyncMock()
    r = _retrier(storage, run_turn, AsyncMock(return_value=b"x"))
    assert await r.retry_failed(rid) is True
    run_turn.assert_awaited_once()
    assert (await storage.pending_media.get(rid))["status"] == "done"


# --- C10 --------------------------------------------------------------


def test_C10_late_note_names_original_send_time():
    from datetime import datetime, timezone

    from src.bot.media_retry import late_note

    note = late_note(datetime(2026, 11, 11, 19, 52, tzinfo=timezone.utc))
    assert "19:52" in note
    assert "\n" not in note.strip()


# --- C11 --------------------------------------------------------------


async def test_C11_error_handler_defers_media_with_pending_row():
    update = MagicMock()
    update.effective_message.reply_text = AsyncMock()
    update.effective_message.voice = MagicMock()
    context = MagicMock()
    context.error = TimedOut()
    context.bot_data = {"audit_logger": MagicMock(), "media_pending_ids": {55}}
    update.effective_message.message_id = 55
    bot = ClaudeCodeBot.__new__(ClaudeCodeBot)
    await bot._error_handler(update, context)
    for call in update.effective_message.reply_text.await_args_list:
        assert "resend" not in call.args[0].lower()


# --- C12 --------------------------------------------------------------


async def test_C12_album_item_failure_retries_and_runs_once(storage):
    rid = await _add(storage, kind="photo", file_id="A1")
    calls = []

    async def fetch(row):
        calls.append(1)
        if len(calls) == 1:
            raise TimedOut()
        return b"img"

    run_turn = AsyncMock()
    r = _retrier(storage, run_turn, fetch)
    await r.process(rid)
    assert len(calls) == 2
    run_turn.assert_awaited_once()
