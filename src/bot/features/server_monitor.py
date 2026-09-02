"""Server health monitoring — on-demand via /servercheck, scheduled via cron."""

import asyncio
import subprocess
from pathlib import Path

import structlog
from telegram import Update
from telegram.ext import ContextTypes

logger = structlog.get_logger(__name__)

# Path to the monitor agent script
MONITOR_SCRIPT = (
    Path(__file__).resolve().parents[3]
    / "agents"
    / "server-monitor"
    / "run-analysis.sh"
)


async def servercheck_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Run server health check on demand via /servercheck."""
    if not MONITOR_SCRIPT.exists():
        await update.message.reply_text("Server monitor script not found.")
        logger.warning("server_monitor_script_missing", path=str(MONITOR_SCRIPT))
        return

    await update.message.reply_text("Running server health check...")
    logger.info("servercheck_triggered", user_id=update.effective_user.id)

    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(
            None,
            lambda: subprocess.run(
                [str(MONITOR_SCRIPT)],
                capture_output=True,
                text=True,
                timeout=120,
            ),
        )
        if result.returncode == 0:
            await update.message.reply_text("Server check complete — report sent.")
        else:
            err = (result.stderr or result.stdout or "Unknown error")[:1000]
            await update.message.reply_text(f"Server check failed:\n{err}")
            logger.error("servercheck_failed", returncode=result.returncode, stderr=err)
    except subprocess.TimeoutExpired:
        await update.message.reply_text("Server check timed out (120s).")
        logger.error("servercheck_timeout")
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")
        logger.error("servercheck_error", error=str(e))
