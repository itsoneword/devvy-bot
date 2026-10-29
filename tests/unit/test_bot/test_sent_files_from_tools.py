"""dv-ff39: tool calls -> deliverable files, shared by the stream intercept
and the scheduled/webhook turn."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from src.bot.utils.image_extractor import (
    attachment_from_tool_call,
    attachments_from_tools_used,
    is_send_file_call,
)


def _png(p: Path) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 50)
    return p


def test_is_send_file_call_matches_bare_and_namespaced() -> None:
    assert is_send_file_call({"name": "send_image_to_user"})
    assert is_send_file_call({"name": "mcp__telegram__send_file_to_user"})
    assert not is_send_file_call({"name": "Read"})
    assert not is_send_file_call({})


def test_attachment_from_tool_call_send_file_accepts_any_extension(
    tmp_path: Path,
) -> None:
    txt = tmp_path / "notes.txt"
    txt.write_text("x")
    as_image = {"name": "send_image_to_user", "input": {"file_path": str(txt)}}
    as_file = {"name": "send_file_to_user", "input": {"file_path": str(txt)}}
    assert attachment_from_tool_call(as_image, tmp_path) is None
    att = attachment_from_tool_call(as_file, tmp_path)
    assert att is not None and att.path == txt.resolve()


def test_attachment_from_tool_call_bad_input_is_none(tmp_path: Path) -> None:
    assert attachment_from_tool_call({"name": "send_image_to_user"}, tmp_path) is None
    assert (
        attachment_from_tool_call(
            {"name": "send_image_to_user", "input": "nope"}, tmp_path
        )
        is None
    )


def test_attachments_from_tools_used_non_list_is_empty(tmp_path: Path) -> None:
    assert attachments_from_tools_used(None, tmp_path) == []
    assert attachments_from_tools_used(MagicMock(), tmp_path) == []


def test_attachments_from_tools_used_skips_error_keeps_order(tmp_path: Path) -> None:
    a = _png(tmp_path / "a.png")
    b = _png(tmp_path / "b.png")
    tools = [
        {"name": "send_image_to_user", "input": {"file_path": str(b)}},
        {"name": "send_image_to_user", "input": {"file_path": str(a)}, "error": "x"},
        {"name": "send_image_to_user", "input": {"file_path": str(a)}},
    ]
    got = attachments_from_tools_used(tools, [tmp_path])
    assert [x.path for x in got] == [b.resolve(), a.resolve()]


async def test_notification_skips_empty_text_chunk_but_sends_text() -> None:
    from src.events.bus import EventBus
    from src.events.types import AgentResponseEvent
    from src.notifications.service import NotificationService

    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=MagicMock(chat=MagicMock(id=1)))
    svc = NotificationService(EventBus(), bot, default_chat_ids=[1])
    await svc._rate_limited_send(1, AgentResponseEvent(chat_id=1, text=""))
    bot.send_message.assert_not_awaited()
    await svc._rate_limited_send(1, AgentResponseEvent(chat_id=1, text="hi"))
    bot.send_message.assert_awaited_once()
