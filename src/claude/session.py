"""Claude Code session management."""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Awaitable, Callable, Dict, List, Optional

import structlog

from ..config.settings import DEFAULT_WINDOW_REFRESH_THRESHOLD_TOKENS, Settings
from .sdk_integration import ClaudeResponse

logger = structlog.get_logger()


def _to_utc(dt: datetime) -> datetime:
    """Normalize datetime to timezone-aware UTC.

    Backward compatibility: legacy persisted sessions may contain naive
    timestamps; treat naive values as UTC.
    """
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


@dataclass
class ClaudeSession:
    """Claude Code session state."""

    session_id: str
    user_id: int
    project_path: Path
    created_at: datetime
    last_used: datetime
    total_cost: float = 0.0
    total_turns: int = 0
    message_count: int = 0
    tools_used: List[str] = field(default_factory=list)
    is_new_session: bool = False  # True if session hasn't been sent to Claude Code yet
    cumulative_tokens: int = 0
    last_eval_checkpoint: int = 0

    def is_expired(self, timeout_hours: int) -> bool:
        """Age of this session against an arbitrary wall-clock window.

        **Not a resume gate, and must never become one again.** Conversation
        lifetime is owned by the window mechanism and measured in context
        size (WindowRepository.should_refresh); wiring elapsed time back into
        session lookup is what silently dropped a 51h-idle profile's context
        on 2026-09-10 (docs/DECISIONS.md). Kept only as a reporting helper.
        """
        age = datetime.now(UTC) - _to_utc(self.last_used)
        return age > timedelta(hours=timeout_hours)

    def update_usage(self, response: ClaudeResponse) -> None:
        """Update session with usage from response."""
        self.last_used = _to_utc(datetime.now(UTC))
        self.total_cost += response.cost_usd
        self.total_turns += response.num_turns
        self.message_count += 1
        self.cumulative_tokens += response.input_tokens + response.output_tokens

        # Track unique tools
        if response.tools_used:
            for tool in response.tools_used:
                tool_name = tool.get("name")
                if tool_name and tool_name not in self.tools_used:
                    self.tools_used.append(tool_name)

    def should_run_evaluator(self) -> bool:
        """Check if evaluator should run (every 50k tokens)."""
        checkpoint = self.cumulative_tokens // 50_000
        return checkpoint > self.last_eval_checkpoint

    def mark_evaluator_run(self) -> None:
        """Mark that evaluator has run at current checkpoint."""
        self.last_eval_checkpoint = self.cumulative_tokens // 50_000

    def should_refresh(self) -> bool:
        """Check if context refresh is needed (default window threshold)."""
        return self.cumulative_tokens >= DEFAULT_WINDOW_REFRESH_THRESHOLD_TOKENS

    def to_dict(self) -> Dict:
        """Convert session to dictionary for storage."""
        return {
            "session_id": self.session_id,
            "user_id": self.user_id,
            "project_path": str(self.project_path),
            "created_at": self.created_at.isoformat(),
            "last_used": self.last_used.isoformat(),
            "total_cost": self.total_cost,
            "total_turns": self.total_turns,
            "message_count": self.message_count,
            "tools_used": self.tools_used,
            "cumulative_tokens": self.cumulative_tokens,
            "last_eval_checkpoint": self.last_eval_checkpoint,
        }

    @classmethod
    def from_dict(cls, data: Dict) -> "ClaudeSession":
        """Create session from dictionary."""
        return cls(
            session_id=data["session_id"],
            user_id=data["user_id"],
            project_path=Path(data["project_path"]),
            created_at=_to_utc(datetime.fromisoformat(data["created_at"])),
            last_used=_to_utc(datetime.fromisoformat(data["last_used"])),
            total_cost=data.get("total_cost", 0.0),
            total_turns=data.get("total_turns", 0),
            message_count=data.get("message_count", 0),
            tools_used=data.get("tools_used", []),
            cumulative_tokens=data.get("cumulative_tokens", 0),
            last_eval_checkpoint=data.get("last_eval_checkpoint", 0),
        )


class SessionStorage:
    """Abstract base class for session storage."""

    async def save_session(self, session: ClaudeSession) -> None:
        """Save session to storage."""
        raise NotImplementedError

    async def load_session(
        self, session_id: str, user_id: int
    ) -> Optional[ClaudeSession]:
        """Load session owned by user_id from storage."""
        raise NotImplementedError

    async def delete_session(
        self, session_id: str, reason: str = "unspecified"
    ) -> None:
        """Retire a session's resume mapping, recording why (dv-2a5a)."""
        raise NotImplementedError

    async def get_user_sessions(self, user_id: int) -> List[ClaudeSession]:
        """Get all sessions for a user."""
        raise NotImplementedError

    async def get_all_sessions(self) -> List[ClaudeSession]:
        """Get all sessions."""
        raise NotImplementedError


class SessionManager:
    """Manage Claude Code sessions."""

    def __init__(self, config: Settings, storage: SessionStorage):
        """Initialize session manager."""
        self.config = config
        self.storage = storage
        self.active_sessions: Dict[str, ClaudeSession] = {}
        # Called after every retirement with (session_id, reason, project_path).
        # The facade installs a lifecycle emitter here so eviction inside
        # this class still lands in lifecycle_events (dv-2a5a).
        self.on_retire: Optional[
            Callable[[str, str, Optional[Path]], Awaitable[None]]
        ] = None

    async def get_or_create_session(
        self,
        user_id: int,
        project_path: Path,
        session_id: Optional[str] = None,
    ) -> ClaudeSession:
        """Get existing session or create new one."""
        logger.info(
            "Getting or creating session",
            user_id=user_id,
            project_path=str(project_path),
            session_id=session_id,
        )

        # Check for existing session. Ownership is the only gate: session age
        # deliberately does NOT disqualify a session here, because lifetime is
        # measured in context size by the window mechanism, not in wall-clock
        # hours (docs/DECISIONS.md 2026-09-10).
        if session_id and session_id in self.active_sessions:
            session = self.active_sessions[session_id]
            if session.user_id != user_id:
                logger.warning(
                    "Session ownership mismatch in active cache",
                    session_id=session_id,
                    session_owner=session.user_id,
                    requesting_user=user_id,
                )
            else:
                logger.debug("Using active session", session_id=session_id)
                return session

        # Try to load from storage (filtered by user_id)
        if session_id:
            session = await self.storage.load_session(session_id, user_id)
            if session:
                self.active_sessions[session_id] = session
                logger.info("Loaded session from storage", session_id=session_id)
                return session

        # Enforce the session cap WITHIN this working directory only.
        #
        # It used to be a global per-user cap that evicted the oldest session
        # across every directory. With a cap of 5 and more profiles than that
        # (devops, channel, general, chef, coach, pm, research), opening a
        # session in profile #6 silently retired the resume mapping of
        # whichever profile the user had left alone longest — the same
        # "context disappears for no reason the user can see" failure the
        # wall-clock timeout caused, reached by a different door. Observed
        # 2026-09-10: the owner sitting exactly at the cap.
        #
        # Scoping the cap per (user, working_dir) keeps its original purpose
        # (bounding runaway session churn inside one project) and makes the
        # profiles independent: no profile can evict another's live head.
        dir_sessions = [
            s
            for s in await self._get_user_sessions(user_id)
            if s.project_path == project_path
        ]
        if len(dir_sessions) >= self.config.max_sessions_per_user:
            # Retire the oldest session *of this directory*.
            oldest = min(dir_sessions, key=lambda s: _to_utc(s.last_used))
            await self.remove_session(
                oldest.session_id,
                reason=f"dir_cap:{self.config.max_sessions_per_user}",
                project_path=project_path,
            )
            logger.info(
                "Removed oldest session in directory due to limit",
                removed_session_id=oldest.session_id,
                user_id=user_id,
                project_path=str(project_path),
                limit=self.config.max_sessions_per_user,
            )

        # Create session with empty ID — Claude will provide the real one
        new_session = ClaudeSession(
            session_id="",
            user_id=user_id,
            project_path=project_path,
            created_at=datetime.now(UTC),
            last_used=datetime.now(UTC),
            is_new_session=True,
        )

        # Don't save to storage yet — deferred until after Claude responds
        # with a real session_id (via update_session)

        logger.info(
            "Created new session (pending Claude session ID)",
            user_id=user_id,
            project_path=str(project_path),
        )

        return new_session

    async def update_session(
        self, session: ClaudeSession, response: ClaudeResponse
    ) -> None:
        """Update session with response data and persist."""
        if session.is_new_session:
            # Assign the real session ID from Claude
            if response.session_id:
                session.session_id = response.session_id
            else:
                logger.warning(
                    "Claude returned no session_id for new session; "
                    "session will not be resumable",
                    user_id=session.user_id,
                    project_path=str(session.project_path),
                )
            session.is_new_session = False

            logger.info(
                "New session assigned Claude session ID",
                session_id=session.session_id,
            )
        elif response.session_id and response.session_id != session.session_id:
            # Resume path: the SDK discarded the saved transcript (e.g. it ended
            # with an orphaned tool_use) and started fresh, returning a new
            # session_id. Adopt it — otherwise we re-resume the stale/poisoned id
            # every turn, the orphaned guard trips again, and context is lost
            # permanently. See bug
            # poisoned-session-id-loops-forever-after-mid-tool-call-restart.
            old_session_id = session.session_id
            session.session_id = response.session_id
            self.active_sessions.pop(old_session_id, None)
            logger.warning(
                "Session id rotated on resume; adopting fresh Claude session id",
                old_session_id=old_session_id,
                new_session_id=response.session_id,
                user_id=session.user_id,
            )

        session.update_usage(response)

        # Persist to storage and track as active
        if session.session_id:
            self.active_sessions[session.session_id] = session
            await self.storage.save_session(session)

        logger.debug(
            "Session updated",
            session_id=session.session_id,
            total_cost=session.total_cost,
            message_count=session.message_count,
        )

    async def remove_session(
        self,
        session_id: str,
        reason: str = "unspecified",
        project_path: Optional[Path] = None,
    ) -> None:
        """Retire a session's resume mapping.

        ``reason`` is mandatory in spirit: it is written to
        ``sessions.deactivated_reason`` and logged, so that a conversation
        that vanished can be explained from the database afterwards
        (dv-2a5a). ``project_path`` lets the retire hook attribute the event
        to a profile; it falls back to the cached session's path.
        """
        cached = self.active_sessions.pop(session_id, None)
        if project_path is None and cached is not None:
            project_path = cached.project_path

        await self.storage.delete_session(session_id, reason=reason)
        logger.info(
            "Session removed",
            session_id=session_id,
            reason=reason,
            project_path=str(project_path) if project_path else None,
        )
        if self.on_retire is not None:
            try:
                await self.on_retire(session_id, reason, project_path)
            except Exception as hook_error:  # pragma: no cover - never fatal
                logger.warning(
                    "Session retire hook failed",
                    session_id=session_id,
                    error=str(hook_error),
                )

    # NOTE: `cleanup_expired_sessions()` was removed on 2026-09-10.
    #
    # It ran on every shutdown and marked is_active=FALSE on every session
    # older than session_timeout_hours, which made each `/restart` a silent
    # context wipe for any profile the user had not touched that day — a soft
    # delete no code path can undo. Session rows are inert bookkeeping
    # (~100 bytes) and only the newest per (user, working_dir) is ever
    # resumed, so there is nothing to reclaim and age is never a reason to
    # retire a session. See docs/DECISIONS.md 2026-09-10.

    async def _get_user_sessions(self, user_id: int) -> List[ClaudeSession]:
        """Get all sessions for a user."""
        return await self.storage.get_user_sessions(user_id)

    async def get_session_info(self, session_id: str, user_id: int) -> Optional[Dict]:
        """Get session info dict for a session owned by user_id."""
        session = self.active_sessions.get(session_id)

        if session and session.user_id != user_id:
            logger.warning(
                "Session ownership mismatch in get_session_info",
                session_id=session_id,
                session_owner=session.user_id,
                requesting_user=user_id,
            )
            session = None

        if not session:
            session = await self.storage.load_session(session_id, user_id)

        if session:
            return {
                "session_id": session.session_id,
                "project": str(session.project_path),
                "created": session.created_at.isoformat(),
                "last_used": session.last_used.isoformat(),
                "cost": session.total_cost,
                "turns": session.total_turns,
                "messages": session.message_count,
                "tools_used": session.tools_used,
            }

        return None

    async def get_user_session_summary(self, user_id: int) -> Dict:
        """Get summary of user's sessions."""
        sessions = await self._get_user_sessions(user_id)

        total_cost = sum(s.total_cost for s in sessions)
        total_messages = sum(s.message_count for s in sessions)

        return {
            "user_id": user_id,
            "total_sessions": len(sessions),
            # _get_user_sessions already returns only is_active rows, and a
            # session no longer goes inactive with age — so every session
            # listed here is resumable. No wall-clock partition to make.
            "active_sessions": len(sessions),
            "total_cost": total_cost,
            "total_messages": total_messages,
            "projects": list(set(str(s.project_path) for s in sessions)),
        }
