"""``/trace`` — what a turn did: question, answer, tool-call tree (dv-2260).

Owner-only. ``/trace`` = newest turn, ``/trace <message_id>``,
``/trace call:<id>`` (jobs, pump), ``/trace last [N]``.
"""

import html
from typing import Optional

from telegram import Update
from telegram.ext import ContextTypes

from ....model_router.trace_view import trace_report
from ....security.audit import AuditLogger

_MAX = 3800


async def trace_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    storage = context.bot_data.get("storage")
    if storage is None or update.message is None:
        return
    arg = " ".join(context.args or [])
    text = await trace_report(storage.db_manager, arg)
    if len(text) > _MAX:
        text = text[:_MAX] + "\n…(cut; scripts/trace has the full tree)"
    await update.message.reply_text(
        f"<pre>{html.escape(text)}</pre>", parse_mode="HTML"
    )
    audit: Optional[AuditLogger] = context.bot_data.get("audit_logger")
    if audit and update.effective_user:
        await audit.log_command(update.effective_user.id, "trace", [arg], True)
