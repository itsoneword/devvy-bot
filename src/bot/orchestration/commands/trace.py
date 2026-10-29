"""/trace — owner-only view of one turn's harness trace (dv-2260 phase 3).

``/trace`` shows the owner's newest turn, ``/trace <id>`` any ``model_calls``
row by id (a pump / bgjob call has no chat message, only that id): the
question, the answer and the tool-call tree. A reply too long for one message
goes as a ``.txt`` file with subagent results in full. Rendering is shared
with ``scripts/trace`` (:mod:`src.model_router.trace_view`).
"""

import io
from typing import Optional

import structlog
from telegram import Update
from telegram.ext import ContextTypes

from ....model_router.trace_view import render_trace
from ....security.audit import AuditLogger
from ...utils.html_format import escape_html

logger = structlog.get_logger()

# Telegram caps a message at 4096; keep room for the HTML tags.
MESSAGE_MAX = 4000
CHAT_TEXT_SHOWN = 600
CHAT_RESULT_SHOWN = 300

_USAGE = "Usage: <code>/trace</code> (your last turn) or <code>/trace &lt;id&gt;</code>"


async def trace_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None:
        return
    user_id = user.id
    args = list(context.args or [])
    storage = context.bot_data.get("storage")
    repo = getattr(storage, "tool_calls", None)

    async def done(ok: bool) -> None:
        audit_logger: Optional[AuditLogger] = context.bot_data.get("audit_logger")
        if audit_logger:
            await audit_logger.log_command(user_id, "trace", args, ok)

    if repo is None:
        await message.reply_text("Trace store unavailable.")
        await done(False)
        return
    if args and not args[0].lstrip("#").isdigit():
        await message.reply_text(_USAGE, parse_mode="HTML")
        await done(False)
        return

    if args:
        call_id: Optional[int] = int(args[0].lstrip("#"))
    else:
        call_id = await repo.latest_turn_id(user_id)
        if call_id is None:
            await message.reply_text("No traced turns yet.")
            await done(True)
            return
    trace = await repo.trace(call_id)
    if trace is None:
        await message.reply_text(f"No model call #{call_id}.")
        await done(False)
        return

    short = render_trace(
        trace, text_limit=CHAT_TEXT_SHOWN, result_limit=CHAT_RESULT_SHOWN
    )
    html = f"{escape_html(short.head)}\n\n<pre>{escape_html(short.tree)}</pre>"
    if len(html) <= MESSAGE_MAX:
        await message.reply_text(html, parse_mode="HTML")
    else:
        full = render_trace(trace).text
        await message.reply_document(
            document=io.BytesIO(full.encode("utf-8")),
            filename=f"trace-{call_id}.txt",
            caption=f"Trace #{call_id}: {len(trace['rows'])} tool calls",
        )
    await done(True)
