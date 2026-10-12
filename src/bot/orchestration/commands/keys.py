"""/setkey, /keys, /delkey — owner-only tool key store (dv-0202).

These run as CommandHandlers, so the message never reaches agentic_text, the
message store or a model. /setkey deletes the user's message first, then
writes; replies show the key name and a 4-char tail only. Audit logs carry
service + name, never the value.
"""

from typing import Optional

import structlog
from telegram import Update
from telegram.ext import ContextTypes

from ....security.audit import AuditLogger
from ....security.keystore import KeyStore, KeyStoreError, default_keystore, mask
from ...utils.html_format import escape_html

logger = structlog.get_logger()

_USAGE = (
    "<b>Usage</b>\n"
    "<code>/setkey &lt;service&gt; &lt;NAME&gt; &lt;value&gt;</code>\n"
    "e.g. <code>/setkey bybit BYBIT_API_KEY xxxx</code>\n"
    "Your message is deleted right away; the value is never shown again."
)


def _store(context: ContextTypes.DEFAULT_TYPE) -> Optional[KeyStore]:
    store = context.bot_data.get("keystore")
    if store is None:
        store = default_keystore()
        if store is not None:
            context.bot_data["keystore"] = store
    return store


async def _audit(
    context: ContextTypes.DEFAULT_TYPE, user_id: int, cmd: str, args: list, ok: bool
) -> None:
    audit_logger: Optional[AuditLogger] = context.bot_data.get("audit_logger")
    if audit_logger:
        await audit_logger.log_command(user_id, cmd, args, ok)


async def setkey_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    user_id = update.effective_user.id
    raw = (message.text or "").split(None, 3)  # /setkey service NAME value
    deleted = False
    if len(raw) >= 4:
        # Delete first: whatever happens next, the value leaves the chat.
        try:
            await message.delete()
            deleted = True
        except Exception as e:
            logger.warning("setkey: could not delete message", error=type(e).__name__)
    chat = update.effective_chat

    async def reply(text: str) -> None:
        await context.bot.send_message(chat.id, text, parse_mode="HTML")

    if len(raw) < 4:
        await reply(_USAGE)
        return
    _, service, name, value = raw
    store = _store(context)
    if store is None:
        await reply("❌ Key store unavailable: no data root configured.")
        await _audit(context, user_id, "setkey", [service, name], False)
        return
    try:
        previous = store.env(service.lower()).get(name)
        store.set(service.lower(), name, value)
    except KeyStoreError as e:
        await reply(f"❌ {escape_html(str(e))}")
        await _audit(context, user_id, "setkey", [service, name], False)
        return
    note = "" if deleted else "\n⚠️ Could not delete your message — delete it by hand."
    # A voice key the owner asked for in /voice: check it, then switch (dv-6b6c).
    from .voice import apply_voice_key, takes_voice_key

    if takes_voice_key(context, service.lower(), name):
        ok = await apply_voice_key(
            context, reply, store, service.lower(), name, value, previous, note
        )
        await _audit(context, user_id, "setkey", [service.lower(), name], ok)
        return
    await reply(
        f"✅ Saved <code>{escape_html(service.lower())}</code> / "
        f"<code>{escape_html(name)}</code> = <code>{escape_html(mask(value.strip()))}</code>"
        + note
    )
    await _audit(context, user_id, "setkey", [service.lower(), name], True)
    logger.info("Tool key saved", service=service.lower(), key_name=name)


async def keys_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    store = _store(context)
    if store is None:
        await update.message.reply_text("❌ Key store unavailable.")
        return
    names = store.names()
    if not names:
        await update.message.reply_text(
            "No keys stored yet.\n\n" + _USAGE, parse_mode="HTML"
        )
        return
    lines = ["🔑 <b>Stored keys</b> (names only)"]
    for service, keys in names.items():
        lines.append(
            f"• <code>{escape_html(service)}</code>: "
            + ", ".join(f"<code>{escape_html(k)}</code>" for k in keys)
        )
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")
    await _audit(context, update.effective_user.id, "keys", [], True)


async def delkey_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = list(context.args or [])
    store = _store(context)
    if store is None or not 1 <= len(args) <= 2:
        await update.message.reply_text(
            "Usage: <code>/delkey &lt;service&gt; [NAME]</code> — "
            "without NAME drops the whole service.",
            parse_mode="HTML",
        )
        return
    service = args[0].lower()
    name = args[1] if len(args) == 2 else None
    try:
        ok = store.delete(service, name)
    except KeyStoreError as e:
        await update.message.reply_text(f"❌ {escape_html(str(e))}")
        return
    what = f"{service}/{name}" if name else service
    await update.message.reply_text(
        (
            f"🗑 Deleted <code>{escape_html(what)}</code>"
            if ok
            else f"Nothing stored under <code>{escape_html(what)}</code>"
        ),
        parse_mode="HTML",
    )
    await _audit(context, update.effective_user.id, "delkey", [service, name or ""], ok)
