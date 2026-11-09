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

Both are gated by ``preflight()`` (dv-86f4): the boot check of the on-disk
code and config, in a subprocess, before the SIGTERM. A failed check refuses
the restart and names the problem, so a config edit that would crash-loop the
bot never takes it down. ``/restart force`` (or its button on the refusal)
skips the check: the owner's escape hatch when the check itself is wrong.

A passing check also ends safe mode (``leave_safe_mode``): the live config
boots again, so the next boot need not fall back to the snapshot. A forced
restart checked nothing and stays in safe mode.
"""

import asyncio
import os
import signal
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Awaitable, Callable, Optional, Tuple

import structlog
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
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

# Restart preflight (dv-86f4). A timeout counts as a failed check.
PREFLIGHT_TIMEOUT_S = 90.0
PREFLIGHT_REASON_MAX = 300
REPO_ROOT = Path(__file__).resolve().parents[4]
CHECK_SCRIPT = REPO_ROOT / "scripts" / "check_install.py"
# Passed to the check; everything else it reads from the env file and the
# secrets file itself. The bot's own os.environ holds env-file values from
# the LAST boot (loader._export_config_env uses setdefault): inheriting it
# would hide the very edit the check is for.
_PREFLIGHT_ENV_KEYS = ("PATH", "HOME", "LANG", "LC_ALL", "USER", "DEVVY_DATA_ROOT")

FORCE_CALLBACK = "restart:force"


async def preflight() -> Optional[str]:
    """Run the boot check of the on-disk code and config in a subprocess.

    ``scripts/check_install.py --preflight``: imports the code, loads
    Settings, loads and validates the profiles, checks every DB's revision
    read-only. Returns None when the next boot would get past all of that,
    else the first problem (one line, for the owner). Never blocks the loop.
    """
    env = {k: os.environ[k] for k in _PREFLIGHT_ENV_KEYS if k in os.environ}
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            str(CHECK_SCRIPT),
            "--preflight",
            cwd=str(REPO_ROOT),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError as e:
        return f"boot check could not start: {e}"
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), PREFLIGHT_TIMEOUT_S)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return f"boot check timed out after {PREFLIGHT_TIMEOUT_S:.0f} s"
    if proc.returncode == 0:
        return None
    lines = out.decode(errors="replace").strip().splitlines()
    reason = lines[0].strip() if lines else ""
    reason = reason or f"boot check exited {proc.returncode}"
    logger.warning("Restart preflight failed", reason=reason)
    return reason[:PREFLIGHT_REASON_MAX]


def refusal(reason: str) -> Tuple[str, InlineKeyboardMarkup]:
    """The owner's refusal screen: what failed, and a button that restarts
    anyway (owner, 2026-10-31: a check that lags behind a config change must
    not lock him out of restarts)."""
    from html import escape

    text = (
        "⛔ <b>Restart cancelled</b>: the boot check failed.\n\n"
        f"<code>{escape(reason)}</code>\n\n"
        "Fix it and /restart again. Force restart only if you are sure: "
        "the bot will most likely not come back up."
    )
    markup = InlineKeyboardMarkup(
        [[InlineKeyboardButton("⚠ Force restart", callback_data=FORCE_CALLBACK)]]
    )
    return text, markup


def leave_safe_mode() -> None:
    """The live config passed the boot check: the next boot uses it, not the
    last-known-good snapshot (dv-86f4, ``src/boot/safemode.py``)."""
    try:
        from ....boot import safemode
        from ....utils.paths import data_layout

        layout = data_layout()
        if safemode.is_active(layout):
            safemode.leave(layout)
    except Exception:  # never let safe-mode bookkeeping block a restart
        logger.warning("Leaving safe mode failed", exc_info=True)


def restart_now(reason: str, user_id: Optional[int] = None) -> None:
    """SIGTERM this process; main.py's handler shuts down gracefully and
    systemd ``Restart=always`` brings the bot back up."""
    logger.info("Bot restart", reason=reason, user_id=user_id)
    os.kill(os.getpid(), signal.SIGTERM)


async def request_restart(
    turn_locks: "TurnLocks",
    reason: str = "",
    user_id: Optional[int] = None,
    notify: Optional[Callable[[str], Awaitable[None]]] = None,
) -> str:
    """Schedule ONE restart that fires once no turn is in flight.

    Waits for every held turn lock — the requester's own turn (still running
    while its tool call returns) and other agents' turns, which an immediate
    restart would cut the same way. After ``RESTART_MAX_WAIT_S`` it restarts
    anyway and logs what was still busy. Returns "scheduled", "already
    scheduled", or "refused: <why>" when the boot check fails (dv-86f4).

    The check runs again once the turns have drained: config may change
    during the wait. A refusal then has no turn to answer, so it goes to
    ``notify`` (the owner) instead.
    """
    global _pending
    if _pending is not None and not _pending.done():
        logger.info("Restart already scheduled", reason=reason, user_id=user_id)
        return "already scheduled"
    problem = await preflight()
    if problem:
        logger.warning("Restart refused", reason=reason, why=problem, user_id=user_id)
        return f"refused: {problem}"
    logger.info("Restart scheduled", reason=reason, user_id=user_id)
    _pending = asyncio.create_task(
        _drain_then_restart(turn_locks, reason, user_id, notify)
    )
    return "scheduled"


async def _drain_then_restart(
    turn_locks: "TurnLocks",
    reason: str,
    user_id: Optional[int],
    notify: Optional[Callable[[str], Awaitable[None]]] = None,
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
    problem = await preflight()
    if problem:
        logger.warning("Restart refused after drain", reason=reason, why=problem)
        if notify is not None:
            try:
                await notify(
                    f"⛔ Restart cancelled ({reason or 'restart_bot'}): the boot "
                    f"check failed.\n{problem}\nFix it, or /restart force."
                )
            except Exception:
                logger.warning("Restart refusal notify failed", exc_info=True)
        return
    leave_safe_mode()
    restart_now(reason or "requested by an agent turn", user_id)


async def restart_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /restart command - gracefully restart the bot process.

    Runs the boot check first (dv-86f4); a failure replies with the reason
    and a force button, and does not restart. ``/restart force`` skips it.
    Then sends a confirmation message and triggers SIGTERM so systemd
    (or any process manager with restart-on-exit) brings the bot back up.
    Immediate by design: it does not wait for in-flight turns (dv-da50).

    Auth: protected by the auth middleware (group -2) which raises
    ``ApplicationHandlerStop`` for unauthenticated users before any
    handler in group 10 runs.  No per-handler check is needed.
    """
    audit_logger: AuditLogger = context.bot_data.get("audit_logger")
    user_id = update.effective_user.id
    args = context.args if isinstance(context.args, list) else []
    force = "force" in [str(a).lower() for a in args]

    if not force:
        problem = await preflight()
        if problem:
            text, markup = refusal(problem)
            await update.message.reply_text(
                text, parse_mode="HTML", reply_markup=markup
            )
            if audit_logger:
                await audit_logger.log_command(user_id, "restart", [], False)
            return
        leave_safe_mode()

    await update.message.reply_text(
        "🔄 <b>Restarting bot…</b>\n\nBack shortly.",
        parse_mode="HTML",
    )

    if audit_logger:
        await audit_logger.log_command(
            user_id, "restart", ["force"] if force else [], True
        )

    restart_now("/restart force" if force else "/restart command", user_id)


async def restart_force_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """The refusal's "Force restart" button = ``/restart force``: no check.
    Owner-gated by the orchestrator, like ``/restart`` itself."""
    query = update.callback_query
    if query is None or update.effective_user is None:
        return
    audit_logger: Optional[AuditLogger] = context.bot_data.get("audit_logger")
    user_id = update.effective_user.id
    await query.answer()
    await query.edit_message_text(
        "🔄 <b>Force restarting bot…</b>\n\nThe boot check was skipped.",
        parse_mode="HTML",
    )
    if audit_logger:
        await audit_logger.log_command(user_id, "restart", ["force"], True)
    restart_now("/restart force (button)", user_id)
