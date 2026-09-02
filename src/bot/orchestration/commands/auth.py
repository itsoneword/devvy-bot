"""``/auth`` — see who is logged in, and log a backend in from Telegram (dv-6a17).

Module-level like ``system.py`` and for the same reason: this must work when
the agent path is exactly what is broken, so it depends on no registry and
never routes through ``agentic_text``.

The in-flight login lives in ``bot_data`` keyed by chat, not in ``user_data``,
because the reply carrying a Claude code is an ordinary text message and the
text handler has to find it from the chat alone.
"""

import asyncio
from typing import Any, Dict, Optional

import structlog
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from ....security.audit import AuditLogger
from ...features.cli_login import SPECS, InteractiveCliLogin, probe_all
from ...utils.html_format import escape_html

logger = structlog.get_logger()

PENDING_KEY = "pending_cli_login"


def _keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "➕ Add / re-login: Codex", callback_data="auth:login:codex"
                )
            ],
            [
                InlineKeyboardButton(
                    "➕ Add / re-login: Claude", callback_data="auth:login:claude"
                )
            ],
            [InlineKeyboardButton("🔄 Refresh", callback_data="auth:refresh")],
        ]
    )


async def _status_text() -> str:
    states = await probe_all()
    body = "\n\n".join(s.render() for s in states)
    return f"🔐 <b>Authentication</b>\n\n{body}"


async def auth_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show each backend's login state and offer to add another."""
    audit_logger: Optional[AuditLogger] = context.bot_data.get("audit_logger")
    user_id = update.effective_user.id
    await update.message.reply_text(
        await _status_text(), parse_mode="HTML", reply_markup=_keyboard()
    )
    if audit_logger:
        await audit_logger.log_command(user_id, "auth", [], True)


async def handle_auth_callback(
    query: Any, context: ContextTypes.DEFAULT_TYPE, data: str
) -> None:
    """Handle ``auth:*`` callbacks."""
    if data == "auth:refresh":
        await query.edit_message_text(
            await _status_text(), parse_mode="HTML", reply_markup=_keyboard()
        )
        return
    if data.startswith("auth:login:"):
        backend = data.split(":", 2)[2]
        if backend not in SPECS:
            await query.edit_message_text(f"Unknown backend: {escape_html(backend)}")
            return
        await _start_login(query, context, backend)
        return
    await query.edit_message_text("Unknown auth action.")


async def _start_login(
    query: Any, context: ContextTypes.DEFAULT_TYPE, backend: str
) -> None:
    chat_id = query.message.chat_id
    pending: Dict[int, Any] = context.bot_data.setdefault(PENDING_KEY, {})
    existing = pending.pop(chat_id, None)
    if existing is not None:
        # One login at a time per chat: a second would race for the same
        # credential file and the user could not tell which prompt is live.
        await existing.kill()

    spec = SPECS[backend]
    login = InteractiveCliLogin(spec)
    await query.edit_message_text(f"Starting {spec.name} login…")
    try:
        await login.start()
    except (OSError, FileNotFoundError) as exc:
        await query.edit_message_text(
            f"Could not run <code>{escape_html(spec.argv[0])}</code>: "
            f"{escape_html(str(exc))}",
            parse_mode="HTML",
        )
        return

    url, code = await login.read_until_prompt()
    if not url:
        # Loud, per the ticket's Risk section: no URL means the CLI's output
        # format moved and the relay needs re-probing, not a silent retry.
        await login.kill()
        await query.edit_message_text(
            f"{spec.name} printed no login link within "
            f"{int(spec.url_timeout)}s. The CLI's output format may have "
            "changed — check the logs and docs/FINDINGS.md."
        )
        logger.warning("cli login: no url matched", backend=backend)
        return

    # One message: the link is useless on mobile if the instruction is in a
    # separate one, and the code must arrive with it.
    lines = [f"<b>{spec.name} login</b>", "", f"1. Open: {escape_html(url)}"]
    if code:
        lines += ["", f"2. Enter this code: <code>{escape_html(code)}</code>"]
    if spec.needs_code_from_user:
        lines += ["", "3. Send the code back here as a plain message."]
        pending[chat_id] = login
    lines += ["", f"<i>Expires — finish within {int(spec.timeout / 60)} minutes.</i>"]
    await context.bot.send_message(
        chat_id=chat_id,
        text="\n".join(lines),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )

    if not spec.needs_code_from_user:
        # Codex: nothing comes back from the user, so just wait it out.
        context.application.create_task(_finish(context, chat_id, login, backend))


async def consume_login_code(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> bool:
    """If this chat has a login waiting on a code, spend the message on it.

    Returns True when the message was consumed and must not reach the agent.
    Called from the top of ``agentic_text``.
    """
    pending = context.bot_data.get(PENDING_KEY)
    # Nothing has ever started a login in this process unless the key holds a
    # real dict; anything else means "no pending login", never "maybe".
    if not isinstance(pending, dict):
        return False
    chat = update.effective_chat
    if chat is None:
        return False
    login = pending.get(chat.id)
    if login is None or not login.awaiting_code:
        return False

    code = (update.message.text or "").strip()
    if not code:
        return False
    pending.pop(chat.id, None)

    # Secret hygiene (FINDINGS 2026-08-04): never log the code, and take the
    # user's copy out of the chat history.
    try:
        await update.message.delete()
    except Exception:
        logger.info("could not delete the message carrying the login code")

    await login.submit_code(code)
    await context.bot.send_message(chat_id=chat.id, text="Code received, finishing…")
    context.application.create_task(_finish(context, chat.id, login, login.spec.name))
    return True


async def _finish(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, login: Any, backend: str
) -> None:
    result = await login.wait()
    pending = context.bot_data.get(PENDING_KEY)
    if isinstance(pending, dict):
        pending.pop(chat_id, None)
    text = result.detail
    if result.ok:
        # Re-probe rather than claim success from an exit code: for Codex the
        # exit code is the only signal and it has been wrong before.
        await asyncio.sleep(0.5)
        text = f"{result.detail}\n\n{await _status_text()}"
    await context.bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode="HTML" if result.ok else None,
        disable_web_page_preview=True,
    )
    logger.info("cli login finished", backend=backend, ok=result.ok)
