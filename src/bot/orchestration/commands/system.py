"""Process-level command handlers (``/restart``) and the one restart executor.

Module-level functions (not a mixin) moved verbatim from the deleted
``src/bot/handlers/command.py``.

Two ways in, one way out (dv-da50):

* ``/restart`` (owner, Telegram) → ``restart_now`` at once — the escape hatch.
* the agent's ``restart_bot`` tool → ``request_restart``, which waits until no
  turn holds a ``TurnLocks`` lock, so the requesting turn's reply is sent and
  saved (messages row, model_calls row) before the SIGTERM. A turn that ran
  ``systemctl restart claude-bot`` itself killed itself before
  ``_store_interaction``; see ``docs/FINDINGS.md`` 2026-10-25.
"""

import asyncio
import os
import signal
import time
from typing import TYPE_CHECKING, Optional

import structlog
from telegram import Update
from telegram.ext import ContextTypes

from ....security.audit import AuditLogger

if TYPE_CHECKING:
    from ....utils.turn_locks import TurnLocks

logger = structlog.get_logger()

# Deferred restart tunables (module constants so tests can shrink them).
RESTART_MAX_WAIT_S = 600  # restart anyway after 10 min of busy turns
RESTART_POLL_S = 1.0  # how often the drain looks at the locks
RESTART_GRACE_S = 2.0  # after the last lock frees: fire-and-forget post-turn work

# The one pending deferred restart, if any (process-level singleton).
_pending: Optional["asyncio.Task[None]"] = None


def restart_now(reason: str, user_id: Optional[int] = None) -> None:
    """SIGTERM this process; main.py's handler shuts down gracefully and
    systemd ``Restart=always`` brings the bot back up."""
    logger.info("Bot restart", reason=reason, user_id=user_id)
    os.kill(os.getpid(), signal.SIGTERM)


async def request_restart(
    turn_locks: "TurnLocks", reason: str = "", user_id: Optional[int] = None
) -> str:
    """Schedule ONE restart that fires once no turn is in flight.

    Waits for every held turn lock — the requester's own turn (still running
    while its tool call returns) and other agents' turns, which an immediate
    restart would cut the same way. After ``RESTART_MAX_WAIT_S`` it restarts
    anyway and logs what was still busy. Returns "scheduled" or
    "already scheduled".
    """
    global _pending
    if _pending is not None and not _pending.done():
        logger.info("Restart already scheduled", reason=reason, user_id=user_id)
        return "already scheduled"
    logger.info("Restart scheduled", reason=reason, user_id=user_id)
    _pending = asyncio.create_task(_drain_then_restart(turn_locks, reason, user_id))
    return "scheduled"


async def _drain_then_restart(
    turn_locks: "TurnLocks", reason: str, user_id: Optional[int]
) -> None:
    deadline = time.monotonic() + RESTART_MAX_WAIT_S
    while True:
        # Yield first: the requester's tool call returns and its turn goes on.
        await asyncio.sleep(RESTART_POLL_S)
        busy = turn_locks.busy()
        if not busy:
            await asyncio.sleep(RESTART_GRACE_S)
            if not turn_locks.busy():
                break
            continue
        if time.monotonic() >= deadline:
            logger.warning(
                "Restart wait cap reached, restarting with turns in flight",
                busy=busy,
                max_wait_s=RESTART_MAX_WAIT_S,
                reason=reason,
            )
            break
    restart_now(reason or "requested by an agent turn", user_id)


async def restart_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /restart command - gracefully restart the bot process.

    Sends a confirmation message then triggers SIGTERM so systemd
    (or any process manager with restart-on-exit) brings the bot back up.
    Immediate by design: it does not wait for in-flight turns (dv-da50).

    Auth: protected by the auth middleware (group -2) which raises
    ``ApplicationHandlerStop`` for unauthenticated users before any
    handler in group 10 runs.  No per-handler check is needed.
    """
    audit_logger: AuditLogger = context.bot_data.get("audit_logger")
    user_id = update.effective_user.id

    await update.message.reply_text(
        "🔄 <b>Restarting bot…</b>\n\nBack shortly.",
        parse_mode="HTML",
    )

    if audit_logger:
        await audit_logger.log_command(user_id, "restart", [], True)

    restart_now("/restart command", user_id)
