"""dv-f90c: extra_dirs are deliverable image roots; jobs attach images."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from src.bot.utils.image_extractor import allowed_image_roots, validate_image_path
from src.claude.turn_setup import image_roots_env
from src.events.bus import EventBus
from src.events.types import AgentResponseEvent
from src.notifications.service import NotificationService


def _png(p: Path) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 16)
    return p


def test_extra_dir_image_allowed_only_with_extra_roots(tmp_path):
    wd, extra = tmp_path / "wd", tmp_path / "gymbro"
    wd.mkdir()
    img = _png(extra / "docs" / "a.png")
    assert validate_image_path(str(img), allowed_image_roots(wd)) is None
    roots = allowed_image_roots(wd, [extra])
    assert validate_image_path(str(img), roots) is not None


def test_extra_dir_does_not_unlock_outside_paths(tmp_path):
    wd, extra = tmp_path / "wd", tmp_path / "gymbro"
    wd.mkdir()
    extra.mkdir()
    other = _png(tmp_path / "other" / "x.png")
    assert validate_image_path(str(other), allowed_image_roots(wd, [extra])) is None


def test_image_roots_env_includes_extra_dirs(tmp_path):
    env = image_roots_env(None, tmp_path / "wd", [tmp_path / "gymbro"])
    assert str(tmp_path / "gymbro") in env.split(":")


async def test_notification_sends_images_after_text(tmp_path):
    img = _png(tmp_path / "a.png")
    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=MagicMock(chat=MagicMock(id=1)))
    bot.send_photo = AsyncMock()
    svc = NotificationService(EventBus(), bot, default_chat_ids=[1])
    await svc._rate_limited_send(
        1, AgentResponseEvent(chat_id=1, text="done", image_paths=[str(img)])
    )
    bot.send_message.assert_awaited()
    bot.send_photo.assert_awaited_once()
