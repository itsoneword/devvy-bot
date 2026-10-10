"""/reminders — every family member's view of their items (dv-752a).

Three sections (set for myself / set by me for others / set for me by
others), a cancel button per item, and the recipient's one control:
"cancel all reminders for me" (owner decision: no mute, no opt-out). Reads
and writes go through the same :class:`ItemService` as the agent tools, so
the creator-or-assignee rule is identical on both paths.
"""

from __future__ import annotations

from typing import Any, List, Optional, Tuple

import structlog
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from ....items.service import ItemError
from ...utils.html_format import escape_html

logger = structlog.get_logger()

CALLBACK_PREFIX = "rem:"
_MAX_BUTTONS = 24
_SECTIONS = (
    ("mine", "For myself"),
    ("by_me", "Set by me for family"),
    ("for_me", "Set for me by family"),
)


def _when(row: dict) -> str:
    if row.get("recurrence"):
        nxt = row.get("next_local")
        return f"🔁 {row['recurrence']}" + (f", next {nxt}" if nxt else "")
    if row.get("next_local"):
        return str(row["next_local"])
    return "todo"


async def render(
    items: Any, user_id: int
) -> Tuple[str, Optional[InlineKeyboardMarkup]]:
    lines: List[str] = ["⏰ <b>Reminders</b>"]
    buttons: List[InlineKeyboardButton] = []
    total = 0
    for scope, title in _SECTIONS:
        rows = await items.list(user_id, scope=scope, status="open")
        if not rows:
            continue
        lines.append(f"\n<b>{title}</b>")
        for row in rows:
            total += 1
            who = ""
            if scope == "by_me":
                who = f" → {escape_html(str(row['for']))}"
            elif scope == "for_me":
                who = f" ← {escape_html(str(row['from']))}"
            agent = f" · {escape_html(str(row['agent']))}" if row.get("agent") else ""
            lines.append(
                f"#{row['id']}{who} · {escape_html(_when(row))}{agent}\n"
                f"   {escape_html(str(row['text'])[:160])}"
            )
            if len(buttons) < _MAX_BUTTONS:
                buttons.append(
                    InlineKeyboardButton(
                        f"✖ #{row['id']}",
                        callback_data=f"{CALLBACK_PREFIX}x:{row['id']}",
                    )
                )
    if total == 0:
        return "⏰ No open reminders or todos.", None
    lines.append("\nTap ✖ to cancel one.")
    keyboard = [buttons[i : i + 4] for i in range(0, len(buttons), 4)]
    keyboard.append(
        [
            InlineKeyboardButton(
                "Cancel ALL reminders for me", callback_data=f"{CALLBACK_PREFIX}all"
            )
        ]
    )
    return "\n".join(lines), InlineKeyboardMarkup(keyboard)


async def reminders_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message, user = update.message, update.effective_user
    if message is None or user is None:
        return
    items = context.bot_data.get("item_service")
    if items is None:
        await message.reply_text("Reminders are not available.")
        return
    user_id = user.id
    text, markup = await render(items, user_id)
    await message.reply_text(text, parse_mode="HTML", reply_markup=markup)
    storage = context.bot_data.get("storage")
    if storage is not None:
        try:
            await storage.log_bot_event(user_id, "command_reminders", {})
        except Exception:  # best-effort audit
            pass


async def handle_reminders_callback(query: Any, context: Any, data: str) -> None:
    items = context.bot_data.get("item_service")
    if items is None:
        await query.edit_message_text("Reminders are not available.")
        return
    user_id = query.from_user.id
    action = data[len(CALLBACK_PREFIX) :]
    note = ""
    try:
        if action.startswith("x:"):
            result = await items.close(user_id, int(action[2:]), "cancelled")
            note = f"Cancelled #{result['id']}."
        elif action == "all":
            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "Yes, cancel all", callback_data=f"{CALLBACK_PREFIX}all:yes"
                        ),
                        InlineKeyboardButton(
                            "No", callback_data=f"{CALLBACK_PREFIX}list"
                        ),
                    ]
                ]
            )
            await query.edit_message_text(
                "Cancel every open timed reminder set for you (by you or by "
                "family)?",
                reply_markup=keyboard,
            )
            return
        elif action == "all:yes":
            result = await items.cancel_all_mine(user_id)
            note = f"Cancelled {result['count']} reminder(s) for you."
        elif action != "list":
            note = "Unknown action."
    except (ItemError, ValueError) as exc:
        note = str(exc)
    logger.info("reminders callback", user_id=user_id, action=action)
    text, markup = await render(items, user_id)
    prefix = f"{escape_html(note)}\n\n" if note else ""
    await query.edit_message_text(prefix + text, parse_mode="HTML", reply_markup=markup)
