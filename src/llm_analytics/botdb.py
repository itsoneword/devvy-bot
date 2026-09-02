"""bot.db mirror: full refresh each cycle, source opened strictly read-only.

bot_messages stores char lengths only — never prompt/response text. The only
text that crosses over is turns.prompt_excerpt (<=200 chars), which the plan's
turns schema explicitly allows for hover labels.
"""

import sqlite3
from pathlib import Path

import structlog

from .parser import TURN_EXCERPT_MAX, normalize_ts

logger = structlog.get_logger(__name__)


def _norm(ts: object) -> str | None:
    if isinstance(ts, str) and ts:
        try:
            return normalize_ts(ts)
        except ValueError:
            return ts
    return None


def mirror_bot_db(conn: sqlite3.Connection, bot_db_path: Path) -> bool:
    """Refresh bot_sessions/bot_messages/bot_windows + bot turns + session
    user_id/in_bot_db in one transaction. Returns False if bot.db is absent."""
    if not bot_db_path.exists():
        logger.warning("bot_db_missing", path=str(bot_db_path))
        return False
    src = sqlite3.connect(f"file:{bot_db_path}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    try:
        bot_sessions = src.execute(
            "SELECT session_id, user_id, project_path, created_at, last_used,"
            " total_cost, total_turns, message_count, is_active FROM sessions"
        ).fetchall()
        bot_messages = src.execute(
            "SELECT message_id, session_id, user_id, timestamp, cost, duration_ms,"
            " window_id, LENGTH(prompt) AS prompt_chars,"
            " LENGTH(response) AS response_chars,"
            " SUBSTR(prompt, 1, ?) AS prompt_excerpt FROM messages",
            (TURN_EXCERPT_MAX,),
        ).fetchall()
        bot_windows = src.execute(
            "SELECT user_id, working_dir, window_id, active_backend,"
            " cumulative_tokens, last_eval_checkpoint, last_checkpoint_message_id,"
            " updated_at FROM conversation_windows"
        ).fetchall()
    finally:
        src.close()

    with conn:
        conn.execute("DELETE FROM bot_sessions")
        conn.executemany(
            "INSERT INTO bot_sessions (session_id, user_id, project_path,"
            " created_at, last_used, total_cost, total_turns, message_count,"
            " is_active) VALUES (?,?,?,?,?,?,?,?,?)",
            [
                (
                    r["session_id"],
                    r["user_id"],
                    r["project_path"],
                    _norm(r["created_at"]) or "",
                    _norm(r["last_used"]),
                    r["total_cost"],
                    r["total_turns"],
                    r["message_count"],
                    1 if r["is_active"] else 0,
                )
                for r in bot_sessions
            ],
        )
        conn.execute("DELETE FROM bot_messages")
        conn.executemany(
            "INSERT INTO bot_messages (message_id, session_id, user_id, ts, cost,"
            " duration_ms, window_id, prompt_chars, response_chars)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            [
                (
                    r["message_id"],
                    r["session_id"],
                    r["user_id"],
                    _norm(r["timestamp"]) or "",
                    r["cost"],
                    r["duration_ms"],
                    r["window_id"],
                    r["prompt_chars"],
                    r["response_chars"],
                )
                for r in bot_messages
            ],
        )
        conn.execute("DELETE FROM bot_windows")
        conn.executemany(
            "INSERT INTO bot_windows (user_id, working_dir, window_id,"
            " active_backend, cumulative_tokens, last_eval_checkpoint,"
            " last_checkpoint_message_id, updated_at) VALUES (?,?,?,?,?,?,?,?)",
            [
                (
                    r["user_id"],
                    r["working_dir"],
                    r["window_id"],
                    r["active_backend"],
                    r["cumulative_tokens"],
                    r["last_eval_checkpoint"],
                    r["last_checkpoint_message_id"],
                    _norm(r["updated_at"]),
                )
                for r in bot_windows
            ],
        )
        conn.execute("DELETE FROM turns WHERE source = 'bot'")
        conn.executemany(
            "INSERT OR IGNORE INTO turns (session_id, ts, source, user_id,"
            " prompt_excerpt, duration_ms, cost) VALUES (?,?,'bot',?,?,?,?)",
            [
                (
                    r["session_id"],
                    _norm(r["timestamp"]) or "",
                    r["user_id"],
                    r["prompt_excerpt"],
                    r["duration_ms"],
                    r["cost"],
                )
                for r in bot_messages
            ],
        )
        conn.execute(
            "UPDATE sessions SET"
            " user_id = (SELECT b.user_id FROM bot_sessions b"
            "            WHERE b.session_id = sessions.session_id),"
            " in_bot_db = EXISTS (SELECT 1 FROM bot_sessions b"
            "                     WHERE b.session_id = sessions.session_id)"
        )
    logger.debug(
        "bot_db_mirrored",
        sessions=len(bot_sessions),
        messages=len(bot_messages),
        windows=len(bot_windows),
    )
    return True
