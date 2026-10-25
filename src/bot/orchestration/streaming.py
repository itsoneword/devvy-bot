"""Streaming and progress-rendering helpers for the orchestrator.

Stream callbacks, verbose progress formatting, typing heartbeat and image
delivery. Code is moved verbatim from src/bot/orchestrator.py.
"""

import asyncio
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import structlog
from telegram import InputMediaPhoto, Update

from ...claude.sdk_integration import StreamUpdate
from ..utils.draft_streamer import DraftStreamer
from ..utils.ephemeral_updater import EphemeralUpdater
from ..utils.image_extractor import (
    ImageAttachment,
    should_send_as_photo,
    validate_image_path,
)
from .base import OrchestratorBase
from .formatting import _condense, _redact_secrets, _tool_icon, _truncate

logger = structlog.get_logger()


# Thought balloon: model reasoning, deliberately distinct from the speech
# balloon used for the assistant's own commentary (dv-4b71). 🧠 is taken by
# the Task tool above.
_THINKING_ICON = "\U0001f4ad"
# How much reasoning to show per verbose level. Thinking is long, repetitive
# and usually restated better by the final answer, so it truncates harder
# than assistant text (80 / unbounded).
_THINKING_SNIPPET_CHARS = {1: 60, 2: 200}
# Ceiling on what the callback keeps in the activity log, so a multi-page
# thought never bloats the progress message even at the highest level.
_THINKING_LOG_CHARS = 400


class StreamingMixin(OrchestratorBase):
    """Stream callbacks, verbose progress and image delivery."""

    def _format_verbose_progress(
        self,
        activity_log: List[Dict[str, Any]],
        verbose_level: int,
        start_time: float,
    ) -> str:
        """Build the progress message text based on activity so far."""
        if not activity_log:
            return "Working..."

        elapsed = time.time() - start_time
        lines: List[str] = [f"Working... ({elapsed:.0f}s)\n"]

        for entry in activity_log[-15:]:  # Show last 15 entries max
            kind = entry.get("kind", "tool")
            if kind == "text":
                # Claude's intermediate reasoning/commentary
                snippet = entry.get("detail", "")
                if verbose_level >= 2:
                    lines.append(f"\U0001f4ac {snippet}")
                else:
                    # Level 1: one short line
                    lines.append(f"\U0001f4ac {snippet[:80]}")
            elif kind == "thinking":
                # Model reasoning from a ThinkingBlock. Never a repr: the
                # callback only ever stores extracted, condensed text.
                # Level 0 shows nothing (belt and braces -- the callback is
                # not even created at that level).
                snippet = entry.get("detail", "")
                if snippet and verbose_level >= 1:
                    limit = _THINKING_SNIPPET_CHARS.get(
                        min(verbose_level, 2), _THINKING_SNIPPET_CHARS[1]
                    )
                    lines.append(f"{_THINKING_ICON} {_truncate(snippet, limit)}")
            else:
                # Tool call
                icon = _tool_icon(entry["name"])
                if verbose_level >= 2 and entry.get("detail"):
                    lines.append(f"{icon} {entry['name']}: {entry['detail']}")
                else:
                    lines.append(f"{icon} {entry['name']}")

        if len(activity_log) > 15:
            lines.insert(1, f"... ({len(activity_log) - 15} earlier entries)\n")

        return "\n".join(lines)

    @staticmethod
    def _summarize_tool_input(tool_name: str, tool_input: Dict[str, Any]) -> str:
        """Return a short summary of tool input for verbose level 2."""
        if not tool_input:
            return ""
        if tool_name in ("Read", "Write", "Edit", "MultiEdit"):
            path = tool_input.get("file_path") or tool_input.get("path", "")
            if path:
                # Show just the filename, not the full path
                return path.rsplit("/", 1)[-1]
        if tool_name in ("Glob", "Grep"):
            pattern = tool_input.get("pattern", "")
            if pattern:
                return pattern[:60]
        if tool_name == "Bash":
            cmd = tool_input.get("command", "")
            if cmd:
                return _redact_secrets(cmd[:100])[:80]
        if tool_name in ("WebFetch", "WebSearch"):
            return (tool_input.get("url", "") or tool_input.get("query", ""))[:60]
        if tool_name == "Task":
            desc = tool_input.get("description", "")
            if desc:
                return desc[:60]
        # Generic: show first key's value
        for v in tool_input.values():
            if isinstance(v, str) and v:
                return v[:60]
        return ""

    @staticmethod
    def _start_typing_heartbeat(
        chat: Any,
        interval: float = 2.0,
        message_thread_id: Optional[int] = None,
    ) -> "asyncio.Task[None]":
        """Start a background typing indicator task.

        Sends typing every *interval* seconds, independently of
        stream events. Cancel the returned task in a ``finally``
        block.
        """

        async def _heartbeat() -> None:
            try:
                while True:
                    await asyncio.sleep(interval)
                    try:
                        if message_thread_id is None:
                            await chat.send_action("typing")
                        else:
                            await chat.send_action(
                                "typing", message_thread_id=message_thread_id
                            )
                    except Exception:
                        pass
            except asyncio.CancelledError:
                pass

        return asyncio.create_task(_heartbeat())

    def _make_stream_callback(
        self,
        verbose_level: int,
        progress_msg: Any,
        tool_log: List[Dict[str, Any]],
        start_time: float,
        mcp_images: Optional[List[ImageAttachment]] = None,
        approved_directory: Optional[Path] = None,
        draft_streamer: Optional[DraftStreamer] = None,
        updater: Optional[EphemeralUpdater] = None,
    ) -> Optional[Callable[[StreamUpdate], Any]]:
        """Create a stream callback for verbose progress updates.

        When *mcp_images* is provided, the callback also intercepts
        ``send_image_to_user`` tool calls and collects validated
        :class:`ImageAttachment` objects for later Telegram delivery.

        When *updater* is given, progress edits are handed to it instead of
        being awaited inline (the stream reader must never wait on Telegram).

        When *draft_streamer* is provided, tool activity and assistant
        text are streamed to the user in real time via
        ``sendMessageDraft``.

        Returns None when verbose_level is 0 **and** no MCP image
        collection or draft streaming is requested.
        Typing indicators are handled by a separate heartbeat task.
        """
        need_mcp_intercept = mcp_images is not None and approved_directory is not None

        if verbose_level == 0 and not need_mcp_intercept and draft_streamer is None:
            return None

        last_edit_time = [0.0]  # mutable container for closure

        async def _on_stream(update_obj: StreamUpdate) -> None:
            # Intercept send_image_to_user MCP tool calls.
            # The SDK namespaces MCP tools as "mcp__<server>__<tool>",
            # so match both the bare name and the namespaced variant.
            if update_obj.tool_calls and need_mcp_intercept:
                for tc in update_obj.tool_calls:
                    tc_name = tc.get("name", "")
                    bare = tc_name.rsplit("__", 1)[-1]
                    if bare in ("send_image_to_user", "send_file_to_user"):
                        tc_input = tc.get("input", {})
                        file_path = tc_input.get("file_path", "")
                        caption = tc_input.get("caption", "")
                        img = validate_image_path(
                            file_path,
                            approved_directory,
                            caption,
                            any_file=bare == "send_file_to_user",
                        )
                        if img:
                            mcp_images.append(img)
                        else:
                            logger.warning(
                                "send_image_to_user path failed validation "
                                "(image will not reach the user despite the "
                                "tool call returning success)",
                                file_path=file_path,
                                approved_directory=str(approved_directory),
                            )

            # Capture tool calls
            if update_obj.tool_calls:
                for tc in update_obj.tool_calls:
                    name = tc.get("name", "unknown")
                    detail = self._summarize_tool_input(name, tc.get("input", {}))
                    if verbose_level >= 1:
                        tool_log.append(
                            {"kind": "tool", "name": name, "detail": detail}
                        )
                    if draft_streamer:
                        icon = _tool_icon(name)
                        line = (
                            f"{icon} {name}: {detail}" if detail else f"{icon} {name}"
                        )
                        await draft_streamer.append_tool(line)

            # Capture model reasoning (ThinkingBlock text). Redacted blocks
            # never reach here -- the SDK layer drops them -- so anything we
            # get has real text. Verbose level 0 shows nothing, including in
            # the draft stream.
            if (
                update_obj.type == "thinking"
                and update_obj.content
                and verbose_level >= 1
            ):
                thought = _condense(update_obj.content)
                if thought:
                    tool_log.append(
                        {"kind": "thinking", "detail": thought[:_THINKING_LOG_CHARS]}
                    )
                    if draft_streamer:
                        limit = _THINKING_SNIPPET_CHARS.get(
                            min(verbose_level, 2), _THINKING_SNIPPET_CHARS[1]
                        )
                        await draft_streamer.append_tool(
                            f"{_THINKING_ICON} {_truncate(thought, limit)}"
                        )

            # Capture assistant text (reasoning / commentary)
            if update_obj.type == "assistant" and update_obj.content:
                text = update_obj.content.strip()
                if text:
                    first_line = text.split("\n", 1)[0].strip()
                    if first_line:
                        if verbose_level >= 1:
                            tool_log.append(
                                {"kind": "text", "detail": first_line[:120]}
                            )
                        if draft_streamer:
                            await draft_streamer.append_tool(
                                f"\U0001f4ac {first_line[:120]}"
                            )

            # Stream text to user via draft (prefer token deltas;
            # skip full assistant messages to avoid double-appending)
            if draft_streamer and update_obj.content:
                if update_obj.type == "stream_delta":
                    await draft_streamer.append_text(update_obj.content)

            # Throttle progress message edits to avoid Telegram rate limits
            if not draft_streamer and verbose_level >= 1:
                now = time.time()
                if (now - last_edit_time[0]) >= 2.0 and tool_log:
                    last_edit_time[0] = now

                    async def _edit() -> None:
                        await progress_msg.edit_text(
                            self._format_verbose_progress(
                                tool_log, verbose_level, start_time
                            )
                        )

                    if updater is not None:
                        # Off the reader path: latest wins, formatted at send time.
                        updater.submit(_edit)
                    else:
                        try:
                            await _edit()
                        except Exception:
                            pass

        return _on_stream

    async def _send_images(
        self,
        update: Update,
        images: List[ImageAttachment],
        reply_to_message_id: Optional[int] = None,
        caption: Optional[str] = None,
        caption_parse_mode: Optional[str] = None,
    ) -> bool:
        """Send extracted images as a media group (album) or documents.

        If *caption* is provided and fits (≤1024 chars), it is attached to the
        photo / first album item so text + images appear as one message.

        Returns True if the caption was successfully embedded in the photo message.
        """
        photos: List[ImageAttachment] = []
        documents: List[ImageAttachment] = []
        for img in images:
            if should_send_as_photo(img.path):
                photos.append(img)
            else:
                documents.append(img)

        # Telegram caption limit
        use_caption = bool(
            caption and len(caption) <= 1024 and photos and not documents
        )
        caption_sent = False

        # Send raster photos as a single album (Telegram groups 2-10 items)
        if photos:
            try:
                if len(photos) == 1:
                    with open(photos[0].path, "rb") as f:
                        await update.message.reply_photo(
                            photo=f,
                            reply_to_message_id=reply_to_message_id,
                            caption=caption if use_caption else None,
                            parse_mode=caption_parse_mode if use_caption else None,
                        )
                    caption_sent = use_caption
                else:
                    media = []
                    file_handles = []
                    for idx, img in enumerate(photos[:10]):
                        fh = open(img.path, "rb")  # noqa: SIM115
                        file_handles.append(fh)
                        media.append(
                            InputMediaPhoto(
                                media=fh,
                                caption=caption if use_caption and idx == 0 else None,
                                parse_mode=(
                                    caption_parse_mode
                                    if use_caption and idx == 0
                                    else None
                                ),
                            )
                        )
                    try:
                        await update.message.chat.send_media_group(
                            media=media,
                            reply_to_message_id=reply_to_message_id,
                        )
                        caption_sent = use_caption
                    finally:
                        for fh in file_handles:
                            fh.close()
            except Exception as e:
                logger.warning("Failed to send photo album", error=str(e))

        # Send SVGs / large files as documents (one by one — can't mix in album)
        for img in documents:
            try:
                with open(img.path, "rb") as f:
                    await update.message.reply_document(
                        document=f,
                        filename=img.path.name,
                        reply_to_message_id=reply_to_message_id,
                    )
                await asyncio.sleep(0.5)
            except Exception as e:
                logger.warning(
                    "Failed to send document image",
                    path=str(img.path),
                    error=str(e),
                )

        return caption_sent
