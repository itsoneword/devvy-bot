"""Taps on the «ready to publish» card (dv-b05e, ``src/bot/publish_card.py``).

Every tap is the owner's (a button bypasses the command gate, dv-6b6c; a guest
can tap a forwarded card): a non-owner gets a popup and nothing changes — no
job, no edit of the card. Nothing here starts an agent turn: a tap runs
``scripts/release`` directly.
"""

from typing import Any

import structlog

from .. import publish_card
from ..card_buttons import Rows

logger = structlog.get_logger()

_REFUSAL = "🚫 Только владелец."


def _with_status(query: Any, status: str) -> str:
    """The status line above the card's own text (kept, so the list stays)."""
    text = getattr(query.message, "text", None)
    return f"{status}\n\n{text}" if isinstance(text, str) and text else status


def _markup(rows: Rows) -> Any:
    """Card rows → the inline keyboard, as the notification service sends it."""
    from ...notifications.service import button_markup

    return button_markup(rows)


async def handle_publish_callback(query: Any, data: str, is_owner: bool) -> None:
    """A tap on the «ready to publish» card. Publishing is public and
    irreversible, so «Опубликовать» only asks; «Да» runs ``release publish
    <board> --by owner`` (the one publish path; an agent turn running it gets
    an owner ask, this tap already is the owner's decision)."""
    from ...claude.agent_tools import run_repo_script

    if not is_owner:
        logger.warning("publish card: non-owner tap refused", data=data)
        await query.answer(_REFUSAL, show_alert=True)
        return
    action, _, board = data[len(publish_card.PREFIX) :].partition(":")
    if not board or action not in ("go", "yes", "no", "later"):
        await query.answer("Неизвестная кнопка.", show_alert=True)
        return

    if action == "go":
        await query.answer()
        await query.edit_message_text(
            _with_status(query, publish_card.confirm_status(board)),
            reply_markup=_markup(publish_card.confirm_buttons(board)),
        )
        return
    if action == "no":
        await query.answer("Отменено")
        await query.edit_message_text(
            _with_status(query, f"✖ {board}: не публикую."),
            reply_markup=_markup(publish_card.buttons(board)),
        )
        return
    if action == "later":
        await query.answer()
        rc, out = await run_repo_script("release", "later", board)
        status = (
            f"💤 {board}: напомню о публикации через 24 ч."
            if rc == 0
            else f"❌ {out.strip()[-300:]}"
        )
        await query.edit_message_text(
            _with_status(query, status),
            reply_markup=_markup(publish_card.buttons(board)),
        )
        return

    # Answered first: push + tag + release take a while.
    await query.answer("🚀 Публикую…")
    rc, out = await run_repo_script("release", "publish", board, "--by", "owner")
    logger.info("publish card: release publish", board=board, rc=rc)
    done = publish_card.published_status(out) if rc == 0 else None
    if done is None:
        # Refused or failed: the cause on the card, the buttons stay.
        cause = out.strip()[-500:] or f"rc={rc}"
        await query.edit_message_text(
            _with_status(query, f"❌ {board}: не опубликовано: {cause}"),
            reply_markup=_markup(publish_card.buttons(board)),
        )
        return
    # No buttons: it is public now; a second tap would only be refused.
    await query.edit_message_text(_with_status(query, done), reply_markup=None)
