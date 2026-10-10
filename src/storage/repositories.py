"""Data access layer using repository pattern.

Features:
- Clean data access API
- Query optimization
- Error handling
"""

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import structlog

from .database import DatabaseManager
from .models import (
    AuditLogModel,
    CostTrackingModel,
    MessageModel,
    ProjectThreadModel,
    SessionModel,
    ToolUsageModel,
    UserModel,
)

logger = structlog.get_logger()

# recall_conversation snippets go straight into a live context window; keep
# each one bounded regardless of how long the original prompt/response was.
_SNIPPET_MAX_CHARS = 300


def _coerce_dt(val: Any) -> Optional[datetime]:
    if val is None or isinstance(val, datetime):
        return val
    if isinstance(val, str):
        try:
            return datetime.fromisoformat(val)
        except ValueError:
            return None
    return None


def _truncate(text: Optional[str]) -> str:
    text = text or ""
    if len(text) <= _SNIPPET_MAX_CHARS:
        return text
    return text[:_SNIPPET_MAX_CHARS].rstrip() + "…"


def _fts_prefix_query(text: str) -> str:
    """Forgiving FTS5 OR query of quoted PREFIX terms.

    Mirrors ``src.storage.memory_repository._fts_query``, but appends ``*``
    to each quoted term so short Cyrillic substrings still match: unicode61
    has no stemmer, so обед/обеда/обедом tokenize as distinct words.
    ``messages_fts`` is built with ``prefix='2 3 4'`` so these prefix queries
    are indexed, not a full scan.
    """
    terms = re.findall(r"[\w]+", text.lower())
    if not terms:
        return '""'
    return " OR ".join(f'"{t}"*' for t in terms)


@dataclass
class MessageSearchHit:
    """One ``recall_conversation`` result: truncated, not the raw row."""

    message_id: int
    timestamp: Optional[datetime]
    prompt_snippet: str
    response_snippet: str
    window_id: Optional[int]


class UserRepository:
    """User data access."""

    def __init__(self, db_manager: DatabaseManager):
        """Initialize repository."""
        self.db = db_manager

    async def get_user(self, user_id: int) -> Optional[UserModel]:
        """Get user by ID."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                "SELECT * FROM users WHERE user_id = ?", (user_id,)
            )
            row = await cursor.fetchone()
            return UserModel.from_row(row) if row else None

    async def create_user(self, user: UserModel) -> UserModel:
        """Create new user."""
        async with self.db.get_connection() as conn:
            await conn.execute(
                """
                INSERT INTO users
                (user_id, telegram_username, first_seen,
                 last_active, is_allowed)
                VALUES (?, ?, ?, ?, ?)
            """,
                (
                    user.user_id,
                    user.telegram_username,
                    user.first_seen or datetime.now(UTC),
                    user.last_active or datetime.now(UTC),
                    user.is_allowed,
                ),
            )
            await conn.commit()

            logger.info(
                "Created user", user_id=user.user_id, username=user.telegram_username
            )
            return user

    async def update_user(self, user: UserModel):
        """Update user data."""
        async with self.db.get_connection() as conn:
            await conn.execute(
                """
                UPDATE users
                SET telegram_username = ?, last_active = ?,
                    total_cost = ?, message_count = ?, session_count = ?
                WHERE user_id = ?
            """,
                (
                    user.telegram_username,
                    user.last_active or datetime.now(UTC),
                    user.total_cost,
                    user.message_count,
                    user.session_count,
                    user.user_id,
                ),
            )
            await conn.commit()

    async def get_allowed_users(self) -> List[int]:
        """Get list of allowed user IDs."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                "SELECT user_id FROM users WHERE is_allowed = TRUE"
            )
            rows = await cursor.fetchall()
            return [row[0] for row in rows]

    async def set_user_allowed(self, user_id: int, allowed: bool):
        """Set user allowed status."""
        async with self.db.get_connection() as conn:
            await conn.execute(
                "UPDATE users SET is_allowed = ? WHERE user_id = ?", (allowed, user_id)
            )
            await conn.commit()

            logger.info("Updated user permissions", user_id=user_id, allowed=allowed)

    async def get_all_users(self) -> List[UserModel]:
        """Get all users."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute("SELECT * FROM users ORDER BY first_seen DESC")
            rows = await cursor.fetchall()
            return [UserModel.from_row(row) for row in rows]


class SessionRepository:
    """Session data access."""

    def __init__(self, db_manager: DatabaseManager):
        """Initialize repository."""
        self.db = db_manager

    async def get_session(self, session_id: str) -> Optional[SessionModel]:
        """Get session by ID."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
            )
            row = await cursor.fetchone()
            return SessionModel.from_row(row) if row else None

    async def create_session(self, session: SessionModel) -> SessionModel:
        """Create new session."""
        async with self.db.get_connection() as conn:
            await conn.execute(
                """
                INSERT INTO sessions
                (session_id, user_id, project_path, created_at, last_used)
                VALUES (?, ?, ?, ?, ?)
            """,
                (
                    session.session_id,
                    session.user_id,
                    session.project_path,
                    session.created_at,
                    session.last_used,
                ),
            )
            await conn.commit()

            logger.info(
                "Created session",
                session_id=session.session_id,
                user_id=session.user_id,
            )
            return session

    async def update_session(self, session: SessionModel):
        """Update session data."""
        async with self.db.get_connection() as conn:
            await conn.execute(
                """
                UPDATE sessions
                SET last_used = ?, total_cost = ?, total_turns = ?,
                    message_count = ?, is_active = ?
                WHERE session_id = ?
            """,
                (
                    session.last_used,
                    session.total_cost,
                    session.total_turns,
                    session.message_count,
                    session.is_active,
                    session.session_id,
                ),
            )
            await conn.commit()

    async def get_user_sessions(
        self, user_id: int, active_only: bool = True
    ) -> List[SessionModel]:
        """Get sessions for user."""
        async with self.db.get_connection() as conn:
            query = "SELECT * FROM sessions WHERE user_id = ?"
            params = [user_id]

            if active_only:
                query += " AND is_active = TRUE"

            query += " ORDER BY last_used DESC"

            cursor = await conn.execute(query, params)
            rows = await cursor.fetchall()
            return [SessionModel.from_row(row) for row in rows]

    # `cleanup_old_sessions(days)` was deleted 2026-09-10 (dv-2a5a). It was
    # a second, uncalled age-based soft delete of resume mappings; see the
    # note in SessionStorage (session_storage.py) for why none may exist.

    async def get_sessions_by_project(self, project_path: str) -> List[SessionModel]:
        """Get sessions for a specific project."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                """
                SELECT * FROM sessions
                WHERE project_path = ? AND is_active = TRUE
                ORDER BY last_used DESC
            """,
                (project_path,),
            )
            rows = await cursor.fetchall()
            return [SessionModel.from_row(row) for row in rows]


class ProjectThreadRepository:
    """Project-thread mapping data access."""

    def __init__(self, db_manager: DatabaseManager):
        """Initialize repository."""
        self.db = db_manager

    async def get_by_chat_thread(
        self, chat_id: int, message_thread_id: int
    ) -> Optional[ProjectThreadModel]:
        """Find active mapping by chat+thread."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                """
                SELECT * FROM project_threads
                WHERE chat_id = ? AND message_thread_id = ? AND is_active = TRUE
            """,
                (chat_id, message_thread_id),
            )
            row = await cursor.fetchone()
            return ProjectThreadModel.from_row(row) if row else None

    async def get_by_chat_project(
        self, chat_id: int, project_slug: str
    ) -> Optional[ProjectThreadModel]:
        """Find mapping by chat+project slug."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                """
                SELECT * FROM project_threads
                WHERE chat_id = ? AND project_slug = ?
            """,
                (chat_id, project_slug),
            )
            row = await cursor.fetchone()
            return ProjectThreadModel.from_row(row) if row else None

    async def upsert_mapping(
        self,
        project_slug: str,
        chat_id: int,
        message_thread_id: int,
        topic_name: str,
        is_active: bool = True,
    ) -> ProjectThreadModel:
        """Create or update mapping by unique chat+project key."""
        async with self.db.get_connection() as conn:
            await conn.execute(
                """
                INSERT INTO project_threads (
                    project_slug, chat_id, message_thread_id, topic_name, is_active
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(chat_id, project_slug) DO UPDATE SET
                    message_thread_id = excluded.message_thread_id,
                    topic_name = excluded.topic_name,
                    is_active = excluded.is_active,
                    updated_at = CURRENT_TIMESTAMP
            """,
                (project_slug, chat_id, message_thread_id, topic_name, is_active),
            )
            await conn.commit()

        mapping = await self.get_by_chat_project(
            chat_id=chat_id, project_slug=project_slug
        )
        if not mapping:
            raise RuntimeError("Failed to upsert project thread mapping")
        return mapping

    async def deactivate_missing_projects(
        self, chat_id: int, active_project_slugs: List[str]
    ) -> int:
        """Deactivate mappings for projects no longer enabled/present."""
        async with self.db.get_connection() as conn:
            if active_project_slugs:
                placeholders = ",".join("?" for _ in active_project_slugs)
                query = f"""
                    UPDATE project_threads
                    SET is_active = FALSE, updated_at = CURRENT_TIMESTAMP
                    WHERE chat_id = ?
                      AND project_slug NOT IN ({placeholders})
                      AND is_active = TRUE
                """
                params = [chat_id] + active_project_slugs
                cursor = await conn.execute(query, params)
            else:
                cursor = await conn.execute(
                    """
                    UPDATE project_threads
                    SET is_active = FALSE, updated_at = CURRENT_TIMESTAMP
                    WHERE chat_id = ? AND is_active = TRUE
                """,
                    (chat_id,),
                )
            await conn.commit()
            return cursor.rowcount

    async def list_stale_active_mappings(
        self, chat_id: int, active_project_slugs: List[str]
    ) -> List[ProjectThreadModel]:
        """List active mappings that are no longer enabled/present."""
        async with self.db.get_connection() as conn:
            if active_project_slugs:
                placeholders = ",".join("?" for _ in active_project_slugs)
                query = f"""
                    SELECT * FROM project_threads
                    WHERE chat_id = ?
                      AND is_active = TRUE
                      AND project_slug NOT IN ({placeholders})
                    ORDER BY project_slug ASC
                """
                params = [chat_id] + active_project_slugs
                cursor = await conn.execute(query, params)
            else:
                cursor = await conn.execute(
                    """
                    SELECT * FROM project_threads
                    WHERE chat_id = ? AND is_active = TRUE
                    ORDER BY project_slug ASC
                """,
                    (chat_id,),
                )
            rows = await cursor.fetchall()
            return [ProjectThreadModel.from_row(row) for row in rows]

    async def set_active(self, chat_id: int, project_slug: str, is_active: bool) -> int:
        """Set active flag for a mapping by chat+project."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                """
                UPDATE project_threads
                SET is_active = ?, updated_at = CURRENT_TIMESTAMP
                WHERE chat_id = ? AND project_slug = ?
            """,
                (is_active, chat_id, project_slug),
            )
            await conn.commit()
            return cursor.rowcount

    async def list_by_chat(
        self, chat_id: int, active_only: bool = True
    ) -> List[ProjectThreadModel]:
        """List mappings for a chat."""
        async with self.db.get_connection() as conn:
            query = "SELECT * FROM project_threads WHERE chat_id = ?"
            params = [chat_id]
            if active_only:
                query += " AND is_active = TRUE"
            query += " ORDER BY project_slug ASC"
            cursor = await conn.execute(query, params)
            rows = await cursor.fetchall()
            return [ProjectThreadModel.from_row(row) for row in rows]


class MessageRepository:
    """Message data access."""

    def __init__(self, db_manager: DatabaseManager):
        """Initialize repository."""
        self.db = db_manager

    async def save_message(self, message: MessageModel) -> int:
        """Save message and return ID."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                """
                INSERT INTO messages
                (session_id, user_id, timestamp, prompt,
                 response, cost, duration_ms, error, window_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    message.session_id,
                    message.user_id,
                    message.timestamp,
                    message.prompt,
                    message.response,
                    message.cost,
                    message.duration_ms,
                    message.error,
                    message.window_id,
                ),
            )
            await conn.commit()
            return cursor.lastrowid

    async def get_session_messages(
        self, session_id: str, limit: int = 50
    ) -> List[MessageModel]:
        """Get messages for session."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                """
                SELECT * FROM messages
                WHERE session_id = ?
                ORDER BY timestamp DESC
                LIMIT ?
            """,
                (session_id, limit),
            )
            rows = await cursor.fetchall()
            return [MessageModel.from_row(row) for row in rows]

    async def get_user_messages(
        self, user_id: int, limit: int = 100
    ) -> List[MessageModel]:
        """Get messages for user."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                """
                SELECT * FROM messages
                WHERE user_id = ?
                ORDER BY timestamp DESC
                LIMIT ?
            """,
                (user_id, limit),
            )
            rows = await cursor.fetchall()
            return [MessageModel.from_row(row) for row in rows]

    async def get_recent_messages(self, hours: int = 24) -> List[MessageModel]:
        """Get recent messages."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                """
                SELECT * FROM messages
                WHERE timestamp > datetime('now', '-' || ? || ' hours')
                ORDER BY timestamp DESC
            """,
                (hours,),
            )
            rows = await cursor.fetchall()
            return [MessageModel.from_row(row) for row in rows]

    async def get_message_timestamp(self, message_id: int) -> Any:
        """Raw ``timestamp`` value for one message, or None. The caller owns
        normalizing it (PARSE_DECLTYPES usually hands back a ``datetime``
        already, but the evaluator's checkpoint lookup defensively accepts
        either -- see src/evaluator/service.py)."""
        async with self.db.get_connection() as conn:
            cur = await conn.execute(
                "SELECT timestamp FROM messages WHERE message_id=?",
                (message_id,),
            )
            row = await cur.fetchone()
        return row[0] if row else None

    async def get_recent_pairs(
        self, session_id: str, limit: int
    ) -> List[Tuple[Optional[str], Optional[str]]]:
        """Last ``limit`` stored (prompt, response) pairs for a session,
        oldest-first. Shared by the Claude facade's cold-start/heal seed and
        the Codex backend's dead-thread seed (dv-5e4f Phase 0: both read the
        identical shape, previously duplicated as raw SQL in each file)."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                "SELECT prompt, response FROM messages "
                "WHERE session_id = ? ORDER BY timestamp DESC LIMIT ?",
                (session_id, limit),
            )
            rows = await cursor.fetchall()
        return [(r[0], r[1]) for r in reversed(rows)]

    async def get_recent_pairs_for_dir(
        self, user_id: int, project_path: str, cutoff_iso: str, limit: int
    ) -> List[Tuple[Optional[str], Optional[str]]]:
        """Last ``limit`` stored (prompt, response) pairs for a WORKSPACE at
        or after ``cutoff_iso`` (an ISO-8601 UTC string, compared
        lexicographically), oldest-first, across every session that
        workspace had. The unit here is the workspace, not one session."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                "SELECT m.prompt, m.response FROM messages m "
                "JOIN sessions s ON s.session_id = m.session_id "
                "WHERE s.project_path = ? AND m.user_id = ? "
                "AND m.timestamp >= ? "
                "ORDER BY m.timestamp DESC LIMIT ?",
                (project_path, user_id, cutoff_iso, limit),
            )
            rows = await cursor.fetchall()
        return [(r[0], r[1]) for r in reversed(rows)]

    async def count_dir_messages(
        self, user_id: int, project_path: str, cutoff_iso: str
    ) -> int:
        """Total stored turns for a workspace at/after ``cutoff_iso``."""
        async with self.db.get_connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT COUNT(*) FROM messages m "
                    "JOIN sessions s ON s.session_id = m.session_id "
                    "WHERE s.project_path = ? AND m.user_id = ? "
                    "AND m.timestamp >= ?",
                    (project_path, user_id, cutoff_iso),
                )
            ).fetchone()
        return int(row[0]) if row else 0

    async def count_session_messages(self, session_id: str) -> int:
        """Total stored turns for a session."""
        async with self.db.get_connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT COUNT(*) FROM messages WHERE session_id = ?",
                    (session_id,),
                )
            ).fetchone()
        return int(row[0]) if row else 0

    async def get_conversation_chronological(
        self, session_id: str
    ) -> List[Tuple[Optional[str], Optional[str]]]:
        """Every (prompt, response) pair for a session, oldest-first -- the
        evaluator's extraction input."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                "SELECT prompt, response FROM messages "
                "WHERE session_id = ? ORDER BY timestamp ASC",
                (session_id,),
            )
            rows = await cursor.fetchall()
        return [(r[0], r[1]) for r in rows]

    async def get_window_messages(
        self, user_id: int, project_path: str, window_id: int, after_message_id: int
    ) -> List[Tuple[Optional[str], Optional[str]]]:
        """(prompt, response) pairs for one profile's conversation window,
        oldest-first, strictly after ``after_message_id``. Joined to
        ``sessions`` and filtered on ``project_path`` -- ``window_id`` alone
        is only unique within a (user_id, working_dir) pair, so a bare
        window_id filter can pull another profile's messages into this
        window's conversation text."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                "SELECT m.prompt, m.response FROM messages m "
                "JOIN sessions s ON m.session_id = s.session_id "
                "WHERE m.user_id=? AND s.project_path=? AND m.window_id=? "
                "AND m.message_id > ? "
                "ORDER BY m.message_id ASC",
                (user_id, project_path, window_id, after_message_id),
            )
            rows = await cursor.fetchall()
        return [(r[0], r[1]) for r in rows]

    async def max_message_id_for_project(self, user_id: int, project_path: str) -> int:
        """Highest message_id stored for this (user, working_dir). Scoped,
        not a global MAX(message_id) -- see windowing/turns callers for why
        (the evaluator checkpoint cursor must never advance on another
        profile's messages)."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                "SELECT COALESCE(MAX(m.message_id), 0) FROM messages m "
                "JOIN sessions s ON m.session_id = s.session_id "
                "WHERE m.user_id=? AND s.project_path=?",
                (user_id, project_path),
            )
            row = await cursor.fetchone()
        return int(row[0]) if row else 0

    async def search_fts(
        self,
        user_id: int,
        working_dir: Union[str, Path],
        query: str,
        limit: int,
    ) -> List[MessageSearchHit]:
        """Keyword search over this user's raw messages in one working dir.

        For the ``recall_conversation`` agent tool: `messages` has no
        `working_dir` column, so scoping joins through `sessions` on
        `project_path` -- same join WindowRepository.tail_messages uses for
        the same reason. This is a hard security boundary, not a UX nicety:
        without the `s.project_path=?` filter, one profile's conversation
        history would leak into another's.
        """
        q = query.strip()
        if not q:
            return []
        wd = str(Path(working_dir))
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                "SELECT m.message_id, m.timestamp, m.prompt, m.response, "
                "m.window_id "
                "FROM messages_fts fts "
                "JOIN messages m ON m.message_id = fts.rowid "
                "JOIN sessions s ON m.session_id = s.session_id "
                "WHERE messages_fts MATCH ? AND m.user_id = ? "
                "AND s.project_path = ? "
                "ORDER BY m.timestamp DESC LIMIT ?",
                (_fts_prefix_query(q), user_id, wd, limit),
            )
            rows = await cursor.fetchall()
        return [
            MessageSearchHit(
                message_id=row[0],
                timestamp=_coerce_dt(row[1]),
                prompt_snippet=_truncate(row[2]),
                response_snippet=_truncate(row[3]),
                window_id=row[4],
            )
            for row in rows
        ]


class ToolUsageRepository:
    """Tool usage data access."""

    def __init__(self, db_manager: DatabaseManager):
        """Initialize repository."""
        self.db = db_manager

    async def save_tool_usage(self, tool_usage: ToolUsageModel) -> int:
        """Save tool usage and return ID."""
        async with self.db.get_connection() as conn:
            tool_input_json = (
                json.dumps(tool_usage.tool_input) if tool_usage.tool_input else None
            )

            cursor = await conn.execute(
                """
                INSERT INTO tool_usage
                (session_id, message_id, tool_name, tool_input,
                 timestamp, success, error_message)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    tool_usage.session_id,
                    tool_usage.message_id,
                    tool_usage.tool_name,
                    tool_input_json,
                    tool_usage.timestamp,
                    tool_usage.success,
                    tool_usage.error_message,
                ),
            )
            await conn.commit()
            return cursor.lastrowid

    async def get_session_tool_usage(self, session_id: str) -> List[ToolUsageModel]:
        """Get tool usage for session."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                """
                SELECT * FROM tool_usage
                WHERE session_id = ?
                ORDER BY timestamp DESC
            """,
                (session_id,),
            )
            rows = await cursor.fetchall()
            return [ToolUsageModel.from_row(row) for row in rows]

    async def get_user_tool_usage(self, user_id: int) -> List[ToolUsageModel]:
        """Get tool usage for user."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                """
                SELECT tu.* FROM tool_usage tu
                JOIN sessions s ON tu.session_id = s.session_id
                WHERE s.user_id = ?
                ORDER BY tu.timestamp DESC
            """,
                (user_id,),
            )
            rows = await cursor.fetchall()
            return [ToolUsageModel.from_row(row) for row in rows]

    async def get_tool_stats(self) -> List[Dict[str, any]]:
        """Get tool usage statistics."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                """
                SELECT
                    tool_name,
                    COUNT(*) as usage_count,
                    COUNT(DISTINCT session_id) as sessions_used,
                    SUM(CASE WHEN success = TRUE THEN 1 ELSE 0 END) as success_count,
                    SUM(CASE WHEN success = FALSE THEN 1 ELSE 0 END) as error_count
                FROM tool_usage
                GROUP BY tool_name
                ORDER BY usage_count DESC
            """
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]


class AuditLogRepository:
    """Audit log data access."""

    def __init__(self, db_manager: DatabaseManager):
        """Initialize repository."""
        self.db = db_manager

    async def log_event(self, audit_log: AuditLogModel) -> int:
        """Log audit event and return ID."""
        async with self.db.get_connection() as conn:
            event_data_json = (
                json.dumps(audit_log.event_data) if audit_log.event_data else None
            )

            cursor = await conn.execute(
                """
                INSERT INTO audit_log
                (user_id, event_type, event_data, success, timestamp, ip_address)
                VALUES (?, ?, ?, ?, ?, ?)
            """,
                (
                    audit_log.user_id,
                    audit_log.event_type,
                    event_data_json,
                    audit_log.success,
                    audit_log.timestamp,
                    audit_log.ip_address,
                ),
            )
            await conn.commit()
            return cursor.lastrowid

    async def get_user_audit_log(
        self, user_id: int, limit: int = 100
    ) -> List[AuditLogModel]:
        """Get audit log for user."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                """
                SELECT * FROM audit_log
                WHERE user_id = ?
                ORDER BY timestamp DESC
                LIMIT ?
            """,
                (user_id, limit),
            )
            rows = await cursor.fetchall()
            return [AuditLogModel.from_row(row) for row in rows]

    async def get_recent_audit_log(self, hours: int = 24) -> List[AuditLogModel]:
        """Get recent audit log entries."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                """
                SELECT * FROM audit_log
                WHERE timestamp > datetime('now', '-' || ? || ' hours')
                ORDER BY timestamp DESC
            """,
                (hours,),
            )
            rows = await cursor.fetchall()
            return [AuditLogModel.from_row(row) for row in rows]


class CostTrackingRepository:
    """Cost tracking data access."""

    def __init__(self, db_manager: DatabaseManager):
        """Initialize repository."""
        self.db = db_manager

    async def update_daily_cost(self, user_id: int, cost: float, date: str = None):
        """Update daily cost for user."""
        if not date:
            date = datetime.now(UTC).strftime("%Y-%m-%d")

        async with self.db.get_connection() as conn:
            await conn.execute(
                """
                INSERT INTO cost_tracking (user_id, date, daily_cost, request_count)
                VALUES (?, ?, ?, 1)
                ON CONFLICT(user_id, date)
                DO UPDATE SET
                    daily_cost = daily_cost + ?,
                    request_count = request_count + 1
            """,
                (user_id, date, cost, cost),
            )
            await conn.commit()

    async def get_user_daily_costs(
        self, user_id: int, days: int = 30
    ) -> List[CostTrackingModel]:
        """Get user's daily costs."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                """
                SELECT * FROM cost_tracking
                WHERE user_id = ? AND date >= date('now', '-' || ? || ' days')
                ORDER BY date DESC
            """,
                (user_id, days),
            )
            rows = await cursor.fetchall()
            return [CostTrackingModel.from_row(row) for row in rows]

    async def get_costs_for_date(self, date: str = None) -> Dict[int, float]:
        """Per-user spend for one date (default today, UTC). Used to seed the
        rate limiter's in-memory budget tracker after a restart."""
        if not date:
            date = datetime.now(UTC).strftime("%Y-%m-%d")
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                "SELECT user_id, daily_cost FROM cost_tracking WHERE date = ?",
                (date,),
            )
            rows = await cursor.fetchall()
            return {int(row["user_id"]): float(row["daily_cost"]) for row in rows}

    async def get_total_costs(self, days: int = 30) -> List[Dict[str, any]]:
        """Get total costs by day."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                """
                SELECT
                    date,
                    SUM(daily_cost) as total_cost,
                    SUM(request_count) as total_requests,
                    COUNT(DISTINCT user_id) as active_users
                FROM cost_tracking
                WHERE date >= date('now', '-' || ? || ' days')
                GROUP BY date
                ORDER BY date DESC
            """,
                (days,),
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]


class AnalyticsRepository:
    """Analytics and reporting."""

    def __init__(self, db_manager: DatabaseManager):
        """Initialize repository."""
        self.db = db_manager

    async def get_user_stats(self, user_id: int) -> Dict[str, any]:
        """Get user statistics."""
        async with self.db.get_connection() as conn:
            # User summary
            cursor = await conn.execute(
                """
                SELECT
                    COUNT(DISTINCT session_id) as total_sessions,
                    COUNT(*) as total_messages,
                    SUM(cost) as total_cost,
                    AVG(cost) as avg_cost,
                    MAX(timestamp) as last_activity,
                    AVG(duration_ms) as avg_duration
                FROM messages
                WHERE user_id = ?
            """,
                (user_id,),
            )

            summary = dict(await cursor.fetchone())

            # Daily usage (last 30 days)
            cursor = await conn.execute(
                """
                SELECT
                    date(timestamp) as date,
                    COUNT(*) as messages,
                    SUM(cost) as cost,
                    COUNT(DISTINCT session_id) as sessions
                FROM messages
                WHERE user_id = ? AND timestamp >= datetime('now', '-30 days')
                GROUP BY date(timestamp)
                ORDER BY date DESC
            """,
                (user_id,),
            )

            daily_usage = [dict(row) for row in await cursor.fetchall()]

            # Most used tools
            cursor = await conn.execute(
                """
                SELECT
                    tu.tool_name,
                    COUNT(*) as usage_count
                FROM tool_usage tu
                JOIN sessions s ON tu.session_id = s.session_id
                WHERE s.user_id = ?
                GROUP BY tu.tool_name
                ORDER BY usage_count DESC
                LIMIT 10
            """,
                (user_id,),
            )

            top_tools = [dict(row) for row in await cursor.fetchall()]

            return {
                "summary": summary,
                "daily_usage": daily_usage,
                "top_tools": top_tools,
            }

    async def get_system_stats(self) -> Dict[str, any]:
        """Get system-wide statistics."""
        async with self.db.get_connection() as conn:
            # Overall stats
            cursor = await conn.execute(
                """
                SELECT
                    COUNT(DISTINCT user_id) as total_users,
                    COUNT(DISTINCT session_id) as total_sessions,
                    COUNT(*) as total_messages,
                    SUM(cost) as total_cost,
                    AVG(duration_ms) as avg_duration
                FROM messages
            """
            )

            overall = dict(await cursor.fetchone())

            # Active users (last 7 days)
            cursor = await conn.execute(
                """
                SELECT COUNT(DISTINCT user_id) as active_users
                FROM messages
                WHERE timestamp > datetime('now', '-7 days')
            """
            )

            active_users = (await cursor.fetchone())[0]
            overall["active_users_7d"] = active_users

            # Top users by cost
            cursor = await conn.execute(
                """
                SELECT
                    u.user_id,
                    u.telegram_username,
                    SUM(m.cost) as total_cost,
                    COUNT(m.message_id) as total_messages
                FROM messages m
                JOIN users u ON m.user_id = u.user_id
                GROUP BY u.user_id
                ORDER BY total_cost DESC
                LIMIT 10
            """
            )

            top_users = [dict(row) for row in await cursor.fetchall()]

            # Tool usage stats
            cursor = await conn.execute(
                """
                SELECT
                    tool_name,
                    COUNT(*) as usage_count,
                    COUNT(DISTINCT session_id) as sessions_used
                FROM tool_usage
                GROUP BY tool_name
                ORDER BY usage_count DESC
                LIMIT 10
            """
            )

            tool_stats = [dict(row) for row in await cursor.fetchall()]

            # Daily activity (last 30 days)
            cursor = await conn.execute(
                """
                SELECT
                    date(timestamp) as date,
                    COUNT(DISTINCT user_id) as active_users,
                    COUNT(*) as total_messages,
                    SUM(cost) as total_cost
                FROM messages
                WHERE timestamp >= datetime('now', '-30 days')
                GROUP BY date(timestamp)
                ORDER BY date DESC
            """
            )

            daily_activity = [dict(row) for row in await cursor.fetchall()]

            return {
                "overall": overall,
                "top_users": top_users,
                "tool_stats": tool_stats,
                "daily_activity": daily_activity,
            }


class UserPreferencesRepository:
    """Per-user persistent selection (active profile, backend, model)."""

    _COLUMNS = (
        "active_profile",
        "agent_backend",
        "agent_model",
        "pinned_switch_message_id",
    )

    def __init__(self, db_manager: DatabaseManager):
        """Initialize repository."""
        self.db = db_manager

    async def get(self, user_id: int) -> Optional[Dict[str, Any]]:
        """Return saved preferences for a user, or None if absent."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                "SELECT active_profile, agent_backend, agent_model, "
                "pinned_switch_message_id "
                "FROM user_preferences WHERE user_id = ?",
                (user_id,),
            )
            row = await cursor.fetchone()
            if row is None:
                return None
            return {col: row[col] for col in self._COLUMNS}

    async def set(self, user_id: int, **fields: Any) -> None:
        """Upsert only the provided columns; leave others untouched."""
        updates = {k: v for k, v in fields.items() if k in self._COLUMNS}
        if not updates:
            return
        cols = list(updates.keys())
        # UPSERT: insert a new row, or on conflict update only the supplied
        # columns (leaving the others untouched). Column names come from the
        # _COLUMNS whitelist; values are bound parameters.
        insert_cols = ", ".join(["user_id", *cols, "updated_at"])
        placeholders = ", ".join(["?"] * (len(cols) + 1) + ["CURRENT_TIMESTAMP"])
        set_clause = ", ".join(f"{c} = excluded.{c}" for c in cols)
        sql = (
            f"INSERT INTO user_preferences ({insert_cols}) "
            f"VALUES ({placeholders}) "
            f"ON CONFLICT(user_id) DO UPDATE SET "
            f"{set_clause}, updated_at = CURRENT_TIMESTAMP"
        )
        async with self.db.get_connection() as conn:
            await conn.execute(sql, (user_id, *[updates[c] for c in cols]))
            await conn.commit()
        logger.info(
            "Updated user preferences", user_id=user_id, fields=list(updates.keys())
        )
