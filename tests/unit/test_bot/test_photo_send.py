"""send_photos: album, per-photo retry on timeout, failures reported."""

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from telegram.error import BadRequest, TimedOut

from src.bot.utils.photo_send import RETRY_TIMEOUTS, send_photos, undelivered_note
from src.profiles.registry import ProfileDefinition


@pytest.fixture
def pics(tmp_path: Path) -> list[Path]:
    out = []
    for name in ("a.png", "b.png"):
        p = tmp_path / name
        p.write_bytes(b"\x89PNG fake")
        out.append(p)
    return out


async def test_album_ok(pics: list[Path]) -> None:
    bot = AsyncMock()
    r = await send_photos(bot, 1, pics, caption="hi", message_thread_id=7)
    assert r.caption_sent and not r.failed
    bot.send_media_group.assert_awaited_once()
    assert bot.send_media_group.await_args.kwargs["message_thread_id"] == 7
    bot.send_photo.assert_not_awaited()


async def test_timeout_retries_one_by_one(pics: list[Path]) -> None:
    bot = AsyncMock()
    bot.send_media_group.side_effect = TimedOut()
    bot.send_photo.side_effect = [None, TimedOut()]
    r = await send_photos(bot, 1, pics, caption="hi")
    assert r.caption_sent
    assert r.failed == [pics[1]]
    first = bot.send_photo.await_args_list[0].kwargs
    assert first["caption"] == "hi"
    assert first["write_timeout"] == RETRY_TIMEOUTS["write_timeout"]
    assert bot.send_photo.await_args_list[1].kwargs["caption"] is None


async def test_bad_request_not_retried(pics: list[Path]) -> None:
    bot = AsyncMock()
    bot.send_photo.side_effect = BadRequest("chat not found")
    r = await send_photos(bot, 1, pics[:1])
    assert r.failed == pics[:1]
    assert bot.send_photo.await_count == 1


def test_note_names_files(pics: list[Path]) -> None:
    assert "a.png, b.png" in undelivered_note(pics)


def test_image_dirs_include_read_dirs(tmp_path: Path) -> None:
    p = ProfileDefinition(
        slug="x",
        name="X",
        working_dir=tmp_path,
        memory_dir=tmp_path,
        persona=tmp_path / "p.md",
        extra_dirs=(Path("/w"),),
        sandbox_read_dirs=(Path("/r"),),
    )
    assert p.image_dirs == (Path("/w"), Path("/r"))
