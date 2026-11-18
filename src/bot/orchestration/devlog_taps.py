"""Taps on a devlog draft preview (dv-fc9d, ``src/devlog/drafts.py``).

The one path to the channel: the owner's tap on the publish button of the
exact preview. Callback data (<= 64 bytes):

    devlog:pub:<draft id>:<first 16 hex of the image sha256>
    devlog:no:<draft id>

A non-owner gets a popup and nothing happens; ``publish_draft`` checks the
owner again, the content binding, the leak check and double taps.
"""

from typing import Any, Optional

import structlog

from ...devlog import drafts

logger = structlog.get_logger()

_STATUS_TEXT = {
    "published": "✅ Опубликовано в канале.",
    "already_published": "Уже опубликовано.",
    "outdated": "Устарело: есть новый превью, жми кнопку под ним.",
    "blocked": "⛔ Проверка на утечки не пройдена, публикации нет.",
    "refused": "🚫 Только владелец.",
}


def parse(data: str) -> Optional[tuple]:
    parts = data.split(":")
    if len(parts) == 4 and parts[1] == "pub" and parts[2].isdigit():
        return ("pub", int(parts[2]), parts[3])
    if len(parts) == 3 and parts[1] == "no" and parts[2].isdigit():
        return ("no", int(parts[2]), "")
    return None


async def handle_devlog_callback(
    update: Any, context: Any, *, owner_id: Optional[int]
) -> None:
    """``owner_id``: the tapper's id when the bot resolved them as the owner,
    else None (then every action is refused)."""
    query = update.callback_query
    data = str(getattr(query, "data", "") or "")
    parsed = parse(data)
    actor = query.from_user.id if query.from_user else -1
    if owner_id is None or actor != owner_id:
        logger.warning("devlog tap refused: not the owner", data=data, user=actor)
        await query.answer(_STATUS_TEXT["refused"], show_alert=True)
        return
    svc = context.bot_data.get("devlog_service")
    if parsed is None or svc is None:
        await query.answer("Кнопка недоступна.", show_alert=True)
        return
    action, draft_id, sha = parsed
    conn = svc.connect()
    try:
        if action == "no":
            ok = drafts.cancel_draft(conn, draft_id)
            await query.answer("Отменено." if ok else "Уже не черновик.")
            if ok:
                await _close(query, "✖ Черновик отменён.")
            return
        draft = drafts.get_draft(conn, draft_id)
        channel = svc.channel_for(draft.profile) if draft is not None else None
        result = await drafts.publish_draft(
            context.bot,
            conn,
            draft_id,
            expected_sha=sha,
            actor_id=actor,
            owner_id=owner_id,
            channel=channel,
            terms_path=svc.terms_path,
        )
    finally:
        conn.close()
    text = _STATUS_TEXT.get(result.status, result.message)
    if result.status == "blocked":
        text = f"{text}\n{result.message}"
    await query.answer(text[:190], show_alert=result.status != "published")
    if result.status in ("published", "already_published", "outdated", "blocked"):
        await _close(query, text)


async def _close(query: Any, status: str) -> None:
    """Replace the preview note's buttons with the outcome."""
    try:
        await query.edit_message_text(status)
    except Exception:  # an old or already edited message
        logger.debug("devlog preview note not edited", exc_info=True)
