"""Unified storage interface.

Provides simple API for the rest of the application.
"""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Dict, Optional

import structlog

from ..claude.sdk_integration import ClaudeResponse
from .database import DatabaseManager
from .deviz_board import DevizBoard
from .extraction_repository import MemoryExtractionRepository
from .hold_repository import UserHoldRepository
from .lifecycle_repository import LifecycleRepository
from .memory_repository import MemoryRepository
from .models import (
    AuditLogModel,
    MessageModel,
    SessionModel,
    UserModel,
)
from .pending_notes import PendingNoteRepository
from .pinned_messages import PinnedMessageRepository
from .repositories import (
    AnalyticsRepository,
    AuditLogRepository,
    CostTrackingRepository,
    MessageRepository,
    ProjectThreadRepository,
    SessionRepository,
    UserPreferencesRepository,
    UserRepository,
)
from .scheduler_repository import SchedulerRepository
from .sent_messages import SentMessageRepository
from .task_repository import TaskRepository
from .token_repository import TokenTrackingRepository
from .tool_calls import ToolCallRepository
from .user_data import UserDataManager, UserDataRepository
from .user_repository import BotUserRepository
from .webhook_repository import WebhookEventRepository

logger = structlog.get_logger()


class Storage:
    """Main storage interface."""

    def __init__(
        self,
        database_url: str,
        tasks_dir: Optional[Path] = None,
        user_database_url: Optional[str] = None,
    ):
        """Initialize storage with database URL.

        ``tasks_dir`` is the deviz board (dv-1327). ``self.tasks`` is that
        board; ``self.legacy_tasks`` is the retired ``agent_tasks`` table, kept
        only so the migration script and any straggling reader can still see
        the old rows.

        ``user_database_url`` is data.db (dv-5f3a): the per-user data store
        with its own migration chain. Optional so existing callers/tests that
        only need bot.db keep working; ``self.user_data`` is then None.
        """
        self.db_manager = DatabaseManager(database_url)
        self.user_data_manager: Optional[UserDataManager] = None
        self.user_data: Optional[UserDataRepository] = None
        if user_database_url:
            self.user_data_manager = UserDataManager(user_database_url)
            self.user_data = UserDataRepository(self.user_data_manager)
        self.users = UserRepository(self.db_manager)
        self.sessions = SessionRepository(self.db_manager)
        self.project_threads = ProjectThreadRepository(self.db_manager)
        self.messages = MessageRepository(self.db_manager)
        self.tool_calls = ToolCallRepository(self.db_manager)
        self.audit = AuditLogRepository(self.db_manager)
        self.costs = CostTrackingRepository(self.db_manager)
        self.analytics = AnalyticsRepository(self.db_manager)
        self.preferences = UserPreferencesRepository(self.db_manager)
        self.memory_repository = MemoryRepository(self.db_manager)
        self.tasks = DevizBoard(tasks_dir or Path(".deviz/tasks"))
        self.legacy_tasks = TaskRepository(self.db_manager)
        self.bot_users = BotUserRepository(self.db_manager)
        self.sent_messages = SentMessageRepository(self.db_manager)
        self.pending_notes = PendingNoteRepository(self.db_manager)
        self.holds = UserHoldRepository(self.db_manager)
        self.pinned_messages = PinnedMessageRepository(self.db_manager)
        # dv-5e4f Phase 0: repos for SQL moved out of
        # scheduler/token-tracker/evaluator/observability/api-server.
        self.scheduled_jobs = SchedulerRepository(self.db_manager)
        self.token_tracking = TokenTrackingRepository(self.db_manager)
        self.extractions = MemoryExtractionRepository(self.db_manager)
        self.webhook_events = WebhookEventRepository(self.db_manager)
        self.lifecycle = LifecycleRepository(self.db_manager)

    async def initialize(self):
        """Initialize storage system."""
        logger.info("Initializing storage system")
        await self.db_manager.initialize()
        if self.user_data_manager:
            await self.user_data_manager.initialize()
        logger.info("Storage system initialized")

    async def close(self):
        """Close storage connections."""
        logger.info("Closing storage system")
        await self.db_manager.close()
        if self.user_data_manager:
            await self.user_data_manager.close()

    async def health_check(self) -> bool:
        """Check storage system health."""
        healthy = await self.db_manager.health_check()
        if self.user_data_manager:
            healthy = healthy and await self.user_data_manager.health_check()
        return healthy

    # High-level operations

    async def save_claude_interaction(
        self,
        user_id: int,
        session_id: str,
        prompt: str,
        response: ClaudeResponse,
        ip_address: Optional[str] = None,
        window_id: Optional[int] = None,
    ) -> int:
        """Save complete Claude interaction. Returns the new message_id so
        callers (the save-honesty hook, the sent_messages ledger) can join
        their records to this turn without re-querying."""
        logger.info(
            "Saving Claude interaction",
            user_id=user_id,
            session_id=session_id,
            cost=response.cost,
        )

        # Save message
        message = MessageModel(
            message_id=None,
            session_id=session_id,
            user_id=user_id,
            timestamp=datetime.now(UTC),
            prompt=prompt,
            response=response.content,
            cost=response.cost_usd,
            duration_ms=response.duration_ms,
            error=response.error_type if response.is_error else None,
            window_id=window_id,
        )

        message_id = await self.messages.save_message(message)

        # The turn's tool calls were written by the harness trace as it ran
        # (dv-2260); tie them to this message.
        trace_id = getattr(response, "trace_id", None)
        if trace_id:
            await self.tool_calls.link_message(trace_id, message_id, session_id)

        # Update cost tracking
        # None (unpriced turn, dv-0722) is written as 0.0: these are
        # dollars-spent ledgers with no "unknown" state.
        booked_on = datetime.now(UTC).strftime("%Y-%m-%d")
        await self.costs.update_daily_cost(user_id, response.cost_usd, booked_on)

        # Update user stats
        user = await self.users.get_user(user_id)
        if user:
            user.total_cost += response.cost_usd
            user.message_count += 1
            user.last_active = datetime.now(UTC)
            await self.users.update_user(user)

        # Update session stats
        session = await self.sessions.get_session(session_id)
        if session:
            session.total_cost += response.cost_usd
            session.total_turns += response.num_turns
            session.message_count += 1
            session.last_used = datetime.now(UTC)
            await self.sessions.update_session(session)

        # Log audit event
        audit_event = AuditLogModel(
            id=None,
            user_id=user_id,
            event_type="claude_interaction",
            event_data={
                "session_id": session_id,
                "cost": response.cost,
                "duration_ms": response.duration_ms,
                "num_turns": response.num_turns,
                "is_error": response.is_error,
                "tools_used": [t["name"] for t in response.tools_used],
            },
            success=not response.is_error,
            timestamp=datetime.now(UTC),
            ip_address=ip_address,
        )
        await self.audit.log_event(audit_event)
        # Last: the user/session totals above are read-modify-write.
        self._true_up_when_settled(user_id, session_id, message_id, response, booked_on)
        return message_id

    def _true_up_when_settled(
        self,
        user_id: int,
        session_id: str,
        message_id: int,
        response: ClaudeResponse,
        booked_on: str,
    ) -> None:
        """Swap the booked estimate for the settled cost (dv-d2f6) in the
        message row, the daily ledger and the user/session totals."""
        from ..model_router.turn_cost import Settlement, when_settled

        async def _apply(settled: Settlement) -> None:
            await self.costs.apply_correction(
                user_id=user_id,
                session_id=session_id,
                message_id=message_id,
                delta=settled.delta,
                date=booked_on,
            )

        when_settled(
            getattr(response, "cost_settlement", None), _apply, what="cost_tracking"
        )

    async def get_or_create_user(
        self, user_id: int, username: Optional[str] = None
    ) -> UserModel:
        """Get or create user."""
        user = await self.users.get_user(user_id)

        if not user:
            logger.info("Creating new user", user_id=user_id, username=username)
            user = UserModel(
                user_id=user_id,
                telegram_username=username,
                first_seen=datetime.now(UTC),
                last_active=datetime.now(UTC),
                is_allowed=False,  # Default to not allowed
            )
            await self.users.create_user(user)

        return user

    async def create_session(
        self, user_id: int, project_path: str, session_id: str
    ) -> SessionModel:
        """Create new session."""
        session = SessionModel(
            session_id=session_id,
            user_id=user_id,
            project_path=project_path,
            created_at=datetime.now(UTC),
            last_used=datetime.now(UTC),
        )

        await self.sessions.create_session(session)

        # Update user session count
        user = await self.users.get_user(user_id)
        if user:
            user.session_count += 1
            await self.users.update_user(user)

        return session

    async def log_security_event(
        self,
        user_id: int,
        event_type: str,
        event_data: Dict[str, Any],
        success: bool = True,
        ip_address: Optional[str] = None,
    ):
        """Log security-related event."""
        audit_event = AuditLogModel(
            id=None,
            user_id=user_id,
            event_type=event_type,
            event_data=event_data,
            success=success,
            timestamp=datetime.now(UTC),
            ip_address=ip_address,
        )
        await self.audit.log_event(audit_event)

    async def log_bot_event(
        self,
        user_id: int,
        event_type: str,
        event_data: Dict[str, Any],
        success: bool = True,
    ):
        """Log bot-related event."""
        audit_event = AuditLogModel(
            id=None,
            user_id=user_id,
            event_type=event_type,
            event_data=event_data,
            success=success,
            timestamp=datetime.now(UTC),
        )
        await self.audit.log_event(audit_event)

    # Convenience methods

    async def is_user_allowed(self, user_id: int) -> bool:
        """Check if user is allowed."""
        user = await self.users.get_user(user_id)
        return user.is_allowed if user else False

    async def get_user_session_summary(self, user_id: int) -> Dict[str, Any]:
        """Get user session summary."""
        sessions = await self.sessions.get_user_sessions(user_id, active_only=False)
        active_sessions = [s for s in sessions if s.is_active]

        return {
            "total_sessions": len(sessions),
            "active_sessions": len(active_sessions),
            "total_cost": sum(s.total_cost for s in sessions),
            "total_messages": sum(s.message_count for s in sessions),
            "projects": list(set(s.project_path for s in sessions)),
        }

    async def get_session_history(
        self, session_id: str, limit: int = 50
    ) -> Dict[str, Any]:
        """Get session history with messages and tools."""
        session = await self.sessions.get_session(session_id)
        if not session:
            return None

        messages = await self.messages.get_session_messages(session_id, limit)
        tools = await self.tool_calls.for_session(session_id)

        return {
            "session": session.to_dict(),
            "messages": [m.to_dict() for m in messages],
            "tool_calls": tools,
        }

    async def get_user_dashboard(self, user_id: int) -> Dict[str, Any]:
        """Get comprehensive user dashboard data."""
        # Get user info
        user = await self.users.get_user(user_id)
        if not user:
            return None

        # Get user stats
        stats = await self.analytics.get_user_stats(user_id)

        # Get recent sessions
        sessions = await self.sessions.get_user_sessions(user_id, active_only=True)

        # Get recent messages
        messages = await self.messages.get_user_messages(user_id, limit=10)

        # Get recent audit log
        audit_logs = await self.audit.get_user_audit_log(user_id, limit=20)

        # Get daily costs
        daily_costs = await self.costs.get_user_daily_costs(user_id, days=30)

        return {
            "user": user.to_dict(),
            "stats": stats,
            "recent_sessions": [s.to_dict() for s in sessions[:5]],
            "recent_messages": [m.to_dict() for m in messages],
            "recent_audit": [a.to_dict() for a in audit_logs],
            "daily_costs": [c.to_dict() for c in daily_costs],
        }

    async def get_admin_dashboard(self) -> Dict[str, Any]:
        """Get admin dashboard data."""
        # Get system stats
        system_stats = await self.analytics.get_system_stats()

        # Get all users
        users = await self.users.get_all_users()

        # Get recent audit log
        recent_audit = await self.audit.get_recent_audit_log(hours=24)

        # Get total costs
        total_costs = await self.costs.get_total_costs(days=30)

        # Get tool stats
        tool_stats = await self.tool_calls.get_tool_stats()

        return {
            "system_stats": system_stats,
            "users": [u.to_dict() for u in users],
            "recent_audit": [a.to_dict() for a in recent_audit],
            "total_costs": total_costs,
            "tool_stats": tool_stats,
        }
