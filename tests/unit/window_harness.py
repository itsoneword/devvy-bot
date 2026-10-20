"""Shared harness for window-roll / evaluator tests driven through real turns.

Used by ``test_over_threshold_guard.py`` and ``test_core_window_cycle.py``
(dv-5206): one recording evaluator double, a real SQLite ``WindowRepository``
with configurable thresholds, and an update/context pair wired like the live
text pipeline. Plain module (not a conftest) so imports are explicit.
"""

import asyncio
import tempfile
from typing import Any, List, Optional
from unittest.mock import AsyncMock, MagicMock

from src.storage.database import DatabaseManager
from src.storage.window_repository import WindowRepository

USER_ID = 123
SESSION_ID = "s"


class _Eval:
    """Recording evaluator double: every ``run(**kw)`` is kept in ``calls``."""

    def __init__(self, result: bool = True):
        self.calls: List[dict] = []
        self._result = result

    async def run(self, **kw: Any) -> bool:
        self.calls.append(kw)
        return self._result


async def _make_repo(
    eval_interval: int = 50_000, refresh: int = 150_000
) -> WindowRepository:
    db = DatabaseManager(tempfile.mktemp(suffix=".db"))
    await db.initialize()
    return WindowRepository(
        db, eval_interval_tokens=eval_interval, refresh_threshold_tokens=refresh
    )


async def _seed_conversation(
    repo: WindowRepository, user_id: int, working_dir: str
) -> None:
    """One user + session + a couple of window-1 messages for tail/extraction."""
    async with repo._db.get_connection() as conn:
        await conn.execute("INSERT INTO users (user_id) VALUES (?)", (user_id,))
        await conn.execute(
            "INSERT INTO sessions (session_id, user_id, project_path) "
            "VALUES (?, ?, ?)",
            (SESSION_ID, user_id, working_dir),
        )
        await conn.execute(
            "INSERT INTO messages (session_id,user_id,prompt,response,window_id) "
            "VALUES (?,?,'weather at caparica','san jose costa rica',1)",
            (SESSION_ID, user_id),
        )
        await conn.commit()


def _mock_response(context_tokens: int = 5_000) -> MagicMock:
    r = MagicMock()
    r.session_id = "fresh-session-1"
    r.content = "answer"
    r.tools_used = []
    r.input_tokens = 100
    r.output_tokens = 50
    r.context_tokens = context_tokens
    r.cost = 0.0
    r.error_type = None
    return r


def _real_message_saver(repo: WindowRepository) -> AsyncMock:
    """``storage.save_claude_interaction`` that writes a REAL ``messages`` row
    (stamped with the window the turn ran in) so conversation text and the
    checkpoint cursor see each turn, like the live storage facade."""

    async def _save(**kw: Any) -> int:
        async with repo._db.get_connection() as conn:
            cur = await conn.execute(
                "INSERT INTO messages "
                "(session_id,user_id,prompt,response,window_id) "
                "VALUES (?,?,?,?,?)",
                (
                    SESSION_ID,
                    kw["user_id"],
                    kw["prompt"],
                    getattr(kw["response"], "content", "") or "",
                    kw.get("window_id"),
                ),
            )
            await conn.commit()
            return int(cur.lastrowid)

    return AsyncMock(side_effect=_save)


def _harness(
    settings: Any,
    repo: WindowRepository,
    evaluator: Any,
    run_command: AsyncMock,
    *,
    save_interaction: Optional[AsyncMock] = None,
) -> tuple:
    """update/context pair wired like the live text pipeline."""
    claude_integration = MagicMock()
    claude_integration.run_command = run_command

    storage = MagicMock()
    storage.db_manager = repo._db
    storage.memory_repository = None
    storage.save_claude_interaction = save_interaction or AsyncMock(return_value=1)
    storage.log_bot_event = AsyncMock()
    storage.pending_notes.consume = AsyncMock(return_value=[])
    storage.sent_messages.record = AsyncMock()

    update = MagicMock()
    update.effective_user.id = USER_ID
    update.message.text = "so did you fix it?"
    update.message.message_id = 1
    update.message.reply_to_message = None
    update.message.chat.type = "private"
    update.message.chat.send_action = AsyncMock()
    update.message.reply_text = AsyncMock()
    progress_msg = AsyncMock()
    update.message.reply_text.return_value = progress_msg

    context = MagicMock()
    context.user_data = {}
    context.bot_data = {
        "settings": settings,
        "claude_integration": claude_integration,
        "storage": storage,
        "rate_limiter": None,
        "audit_logger": None,
        "window_repo": repo,
        "evaluator_service": evaluator,
    }
    return update, context


async def _drain(rounds: int = 5) -> None:
    """Let background evaluator tasks (``asyncio.create_task``) and their
    checkpoint writes finish."""
    for _ in range(rounds):
        await asyncio.sleep(0.01)
