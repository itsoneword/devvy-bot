"""The one "the owner's next message is input, not a turn" hook (dv-3881).

Three things wait for a message that must never reach the agent: a /auth
login code (``auth.consume_login_code``, keyed by chat), the onboarding
interview (``setup.gate_text`` / ``gate_media``, keyed by user) and a Pump
answer draft after ✏ on a task card (``admin_menu.take_pump_draft``, keyed by
chat + topic + user, 10 minutes). Each keeps its own state; this module is the
single place ``turns.py`` asks, for text, voice transcripts and media alike,
so a fourth waiter is one more entry in ``CONSUMERS``, not another intercept.

Order matters, first True wins: a login code is a secret and is consumed
before anything could echo it; onboarding traps every owner message of an
unconfigured bot; the Pump draft only takes what was explicitly asked for.
"""

from typing import Awaitable, Callable, Literal, Optional, Tuple

from telegram import Update
from telegram.ext import ContextTypes

Kind = Literal["text", "voice", "media"]
Consumer = Callable[
    [Update, ContextTypes.DEFAULT_TYPE, Kind, Optional[str], str], Awaitable[bool]
]


async def _login_code(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    kind: Kind,
    text: Optional[str],
    role: str,
) -> bool:
    """A /auth Claude code arrives as typed text only."""
    if kind != "text":
        return False
    from .commands import auth

    return await auth.consume_login_code(update, context)


async def _onboarding(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    kind: Kind,
    text: Optional[str],
    role: str,
) -> bool:
    from .commands import setup

    if kind == "media":
        return await setup.gate_media(update, context, role)
    return await setup.gate_text(update, context, text or "", role)


async def _pump_draft(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    kind: Kind,
    text: Optional[str],
    role: str,
) -> bool:
    """A file can't be an answer: media is left to the normal path."""
    if kind == "media" or role != "owner" or not text:
        return False
    from .admin_menu import take_pump_draft

    return await take_pump_draft(update, context, text)


CONSUMERS: Tuple[Consumer, ...] = (_login_code, _onboarding, _pump_draft)


async def consume(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    kind: Kind,
    text: Optional[str],
    role: str,
) -> bool:
    """True = a waiter took the message; the caller starts no turn.

    ``text`` is what the message says (voice: the transcript; media: None).
    """
    for consumer in CONSUMERS:
        if await consumer(update, context, kind, text, role):
            return True
    return False
