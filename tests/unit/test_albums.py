"""dv-b399: one turn per Telegram media group."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from src.bot.orchestration.albums import AlbumBuffer


def _upd(mid, caption=None, group="g1", text=None, thread=None, user=1):
    msg = SimpleNamespace(
        chat_id=5,
        message_id=mid,
        media_group_id=group,
        caption=caption,
        text=text,
        message_thread_id=thread,
    )
    return SimpleNamespace(message=msg, effective_user=SimpleNamespace(id=user))


async def test_three_items_one_leader_caption_first():
    buf = AlbumBuffer(debounce=0.05)
    u1, u2, u3 = _upd(1, "my comment"), _upd(2), _upd(3)
    album, lead = buf.join(u1)
    assert lead
    assert buf.join(u2) == (album, False)
    task = asyncio.ensure_future(buf.collect(album))
    await asyncio.sleep(0.02)
    assert buf.join(u3) == (album, False)  # arrives mid-debounce, extends it
    await task
    assert album.items == [u1, u2, u3]
    assert album.captions() == ["my comment"]
    assert not buf._open


async def test_text_message_folded_into_open_album_only():
    buf = AlbumBuffer(debounce=0.05)
    album, _ = buf.join(_upd(1))
    assert buf.absorb_text(_upd(9, text="comment for all", group=None))
    assert not buf.absorb_text(_upd(9, text="x", group=None, thread=77))  # topic
    assert not buf.absorb_text(_upd(9, text="x", group=None, user=2))
    await buf.collect(album)
    assert album.captions() == ["comment for all"]
    assert not buf.absorb_text(_upd(10, text="late", group=None))


async def test_cap_starts_new_album():
    buf = AlbumBuffer(debounce=0.01)
    first, _ = buf.join(_upd(1))
    for i in range(2, 11):
        buf.join(_upd(i))
    assert first.full
    second, lead = buf.join(_upd(11))
    assert lead and second is not first


async def test_agentic_photo_runs_one_turn_with_all_images(tmp_path, monkeypatch):
    from src.bot.orchestration import albums
    from src.bot.orchestrator import MessageOrchestrator

    o = MessageOrchestrator.__new__(MessageOrchestrator)
    o._albums = AlbumBuffer(debounce=0.05)
    ih = MagicMock()

    async def proc(photo, cap):
        return SimpleNamespace(
            base64_data="AAAA", metadata={"format": "jpeg"}, prompt="p"
        )

    ih.process_image = proc
    ih.create_album_prompt = lambda n, c: f"{c}|{n}"
    ih.create_photo_prompt = lambda c: f"single {c}"
    o._resolve_role = lambda uid, ctx: "owner"
    lock = asyncio.Lock()
    o._begin_media_turn = AsyncMock(return_value=(lock, MagicMock(), False))
    o._media_upload_dir = lambda *a, **k: tmp_path
    o._reports_topic_slug = AsyncMock(return_value=None)
    o._run_agentic_media_turn = AsyncMock()
    ctx = SimpleNamespace(
        bot_data={"features": SimpleNamespace(get_image_handler=lambda: ih)}
    )

    import src.bot.orchestration.commands.setup as setup

    monkeypatch.setattr(setup, "gate_media", AsyncMock(return_value=False))

    def mk(mid, cap=None):
        u = _upd(mid, cap)
        u.message.photo = [object()]
        u.message.chat = MagicMock()
        return u

    us = [mk(1, "cap"), mk(2), mk(3)]
    await asyncio.gather(*(o.agentic_photo(u, ctx) for u in us))
    o._run_agentic_media_turn.assert_awaited_once()
    kw = o._run_agentic_media_turn.await_args.kwargs
    assert len(kw["images"]) == 3
    assert kw["prompt"] == "cap|3"
    assert albums.ALBUM_MAX_ITEMS >= 3
