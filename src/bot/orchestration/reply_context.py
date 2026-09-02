"""Reply-context envelope for quoted messages (dv-7783).

When a user replies to a message, the quoted text is prepended so the agent
knows what they refer to. The old format blindly inlined the quote, which
had two failure modes (both proven on msg 766, 2026-08-05):

- the «запомни» force-eval trigger regexed the WHOLE prompt, so a quoted bot
  reply containing «запомнил» tripped it — the fix is structural: the caller
  gets ``user_text`` back separately and runs the trigger on that alone, so
  the envelope physically cannot reach the regex whatever its wording;
- a quoted bot message was labelled "your previous response" even when it
  was a system notification ("Memory updated: 2 fact(s)") the agent never
  wrote. Origin now resolves through the ``sent_messages`` ledger.

A third failure mode (dv-f2a8, proven on msg 850, 2026-08-07): a turn reply
authored by ANOTHER profile was labelled "your own" with the text stripped,
inviting the agent to confidently "remember" a message it can never see.
The ledger's ``profile_slug`` is now compared against the current turn's
agent id.
"""

from __future__ import annotations

from typing import Any, Optional, Tuple

import structlog

logger = structlog.get_logger()


async def build_reply_context(
    update: Any,
    bot_id: Optional[int],
    ledger: Any,
    user_text: str,
    current_agent_id: Optional[str] = None,
) -> Tuple[str, str]:
    """Return ``(prompt_text, user_text)``.

    ``prompt_text`` is the user's message with a labelled reply envelope
    prepended when they replied to something; ``user_text`` passes through
    unchanged — the caller must use IT (never ``prompt_text``) for trigger
    matching.

    ``current_agent_id`` is the agent id THIS turn resolved to (dv-f2a8):
    a ledgered turn reply is only "your own" when its recorded
    ``profile_slug`` matches it. Another profile's reply keeps its quoted
    text and an honest label — the calling agent cannot see that message
    any other way (recall_conversation is scoped to its own profile).
    """
    message = getattr(update, "message", None)
    quoted = getattr(message, "reply_to_message", None) if message else None
    if quoted is None:
        return user_text, user_text
    quoted_text = quoted.text or quoted.caption or ""
    if not quoted_text:
        return user_text, user_text

    sender = quoted.from_user
    is_own_bot = bool(bot_id and sender and sender.id == bot_id)

    entry = None
    if is_own_bot and ledger is not None:
        try:
            entry = await ledger.lookup(quoted.chat.id, quoted.message_id)
        except Exception as e:
            logger.warning("Reply-context ledger lookup failed", error=str(e))

    entry_slug = getattr(entry, "profile_slug", None)
    if (
        is_own_bot
        and entry is not None
        and entry.kind == "turn_reply"
        and entry_slug
        and current_agent_id
        and entry_slug != current_agent_id
    ):
        # Another profile's turn reply (dv-f2a8): it must NOT be labelled
        # "your own" with the text stripped — the message lives in another
        # session this agent can never reach, so that label is a
        # confabulation trap. Keep the quoted text, attribute it honestly.
        envelope = (
            f"[replied-to message, from another agent profile "
            f'({entry_slug}), not you:\n"{quoted_text}"]'
        )
    elif is_own_bot and entry is not None and entry.kind == "turn_reply":
        # Own agent reply, resolved in the ledger: reference by turn, omit
        # the text — it is the agent's own message and re-injecting it both
        # doubles context and feeds the agent its own words as if new.
        sent_at = str(entry.created_at) if entry.created_at else "unknown time"
        envelope = (
            f"[replied-to: your own earlier reply (turn "
            f"message_id={entry.messages_message_id}, sent {sent_at}). "
            "Text omitted. If you do not actually see that message in your "
            "context, do not describe it from memory — say so, or retrieve "
            "it with recall_conversation first.]"
        )
    elif is_own_bot and entry is not None and entry.kind == "scheduled_reminder":
        # A verbatim scheduled reminder: composed by the profile's agent at
        # schedule time, delivered without an agent turn — so it is in no
        # transcript. Keep the text; the reply is the user's answer to it.
        if entry_slug and current_agent_id and entry_slug != current_agent_id:
            envelope = (
                f"[replied-to: a scheduled reminder set by another agent "
                f'profile ({entry_slug}), delivered verbatim:\n"{quoted_text}"]'
            )
        else:
            envelope = (
                "[replied-to: a scheduled reminder you composed when "
                "scheduling it; it was delivered to the user verbatim, "
                "without an agent turn, so it is not in your transcript. "
                f'The reminder said:\n"{quoted_text}"]'
            )
    elif is_own_bot and entry is not None:
        envelope = (
            "[replied-to message, from: a system notification (not written "
            f'by you):\n"{quoted_text}"]'
        )
    elif is_own_bot:
        # Bot-sent but not in the ledger: pre-ledger history, evaluator
        # notifies until dv-c5d9 lands, edited messages. Keep the text,
        # attribute it honestly.
        envelope = (
            "[replied-to message, from: you (this agent), earlier reply:\n"
            f'"{quoted_text}"]'
        )
    else:
        origin = _origin_label(quoted)
        envelope = f'[replied-to message, from: {origin}:\n"{quoted_text}"]'

    return f"{envelope}\n\n{user_text}", user_text


def _origin_label(quoted: Any) -> str:
    forward = getattr(quoted, "forward_origin", None)
    if forward is not None:
        name = (
            getattr(getattr(forward, "sender_user", None), "first_name", None)
            or getattr(forward, "sender_user_name", None)
            or getattr(getattr(forward, "chat", None), "title", None)
            or "an unknown sender"
        )
        return f"a forwarded message originally from {name}"
    sender = quoted.from_user
    first_name = getattr(sender, "first_name", None) if sender else None
    return f"the user {first_name}" if first_name else "another user"
