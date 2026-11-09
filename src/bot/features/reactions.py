"""Telegram reactions on bot turn replies -> answer-quality labels (dv-e1a7).

Only 👎 (bad), 👍 / ❤ (fine) and 🔥 (great) count; every other emoji, custom
emoji and paid reactions are ignored. A reaction labels a turn only when the
message is ledgered in ``sent_messages`` as a ``turn_reply``; system
notifications and unledgered messages are ignored. No model call.

The security/auth/rate-limit middleware are ``MessageHandler``s and never see
a ``message_reaction`` update, so the sender is checked here.
"""

from datetime import UTC, datetime
from typing import Any, Optional, Tuple

import structlog

from ...security.auth import ROLE_GUEST, ROLE_OWNER, fallback_role

logger = structlog.get_logger(__name__)

# Telegram sends ❤ as U+2764 without VS16; keys carry no U+FE0F.
REACTION_SCORES: dict[str, int] = {"👎": -1, "👍": 1, "❤": 1, "🔥": 2}

_LABELLED_ROLES = (ROLE_OWNER, ROLE_GUEST)


def _strongest(reactions: Any) -> Optional[Tuple[str, int]]:
    """The listed emoji with the highest score, or None."""
    best: Optional[Tuple[str, int]] = None
    for r in reactions or ():
        emoji = getattr(r, "emoji", None)
        if not isinstance(emoji, str):  # custom emoji / paid reactions
            continue
        emoji = emoji.replace("\ufe0f", "")
        score = REACTION_SCORES.get(emoji)
        if score is not None and (best is None or score > best[1]):
            best = (emoji, score)
    return best


async def _role(user_id: int, bot_data: dict) -> Optional[str]:
    auth = bot_data.get("auth_manager")
    if auth is None:
        return fallback_role(user_id, bot_data.get("settings"))
    role = auth.get_user_role(user_id)
    if role is None:
        # Session expired or never opened: authenticate the way the auth
        # middleware does; only an accepted user falls back to a role.
        try:
            if await auth.authenticate_user(user_id):
                role = auth.get_user_role(user_id) or fallback_role(
                    user_id, bot_data.get("settings")
                )
        except Exception:
            logger.debug("reaction auth failed", user_id=user_id, exc_info=True)
    return role if isinstance(role, str) else None


async def handle_message_reaction(update: Any, context: Any) -> None:
    """Store, update or delete the reacting user's label on a turn reply."""
    mr = getattr(update, "message_reaction", None)
    if mr is None:  # message_reaction_count etc.
        return
    user = mr.user
    if user is None or getattr(user, "is_bot", False):  # anonymous / actor_chat
        return
    bot_data = context.bot_data
    storage = bot_data.get("storage")
    if storage is None:
        return
    chat_id = mr.chat.id
    message_id = mr.message_id

    sent = await storage.sent_messages.lookup(chat_id, message_id)
    if sent is None or sent.kind != "turn_reply" or sent.messages_message_id is None:
        return
    if await _role(user.id, bot_data) not in _LABELLED_ROLES:
        logger.debug("reaction from unauthorised user ignored", user_id=user.id)
        return

    picked = _strongest(mr.new_reaction)
    if picked is None:
        # Removed, or changed to an unlisted emoji: the label is gone.
        await storage.reactions.delete(chat_id, message_id, user.id)
        return
    emoji, score = picked
    reacted_at = mr.date if isinstance(mr.date, datetime) else datetime.now(UTC)
    if reacted_at.tzinfo is None:
        reacted_at = reacted_at.replace(tzinfo=UTC)
    await storage.reactions.upsert(
        chat_id,
        message_id,
        user.id,
        sent.messages_message_id,
        sent.profile_slug,
        emoji,
        score,
        reacted_at,
    )
    logger.info(
        "turn reaction stored",
        profile=sent.profile_slug,
        messages_message_id=sent.messages_message_id,
        score=score,
    )
