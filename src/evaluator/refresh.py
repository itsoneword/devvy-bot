"""Context refresh service for session reset at 150k cumulative tokens.

Flow: run final evaluator (with decay) -> remove session -> notify user.
Next user message auto-creates a fresh session with memory reloaded via PromptComposer.
"""

from typing import TYPE_CHECKING, Awaitable, Callable, Optional

import structlog

if TYPE_CHECKING:
    from ..claude.session import ClaudeSession, SessionManager
    from .service import EvaluatorService

logger = structlog.get_logger()


class ContextRefreshService:
    """Handles session refresh at 150k cumulative tokens.

    Flow: run final evaluator (with decay) -> remove session -> notify user.
    Next user message auto-creates a fresh session with memory reloaded via PromptComposer.
    """

    def __init__(
        self,
        evaluator_service: "EvaluatorService",
        session_manager: "SessionManager",
        get_conversation: Callable,  # async fn(session) -> str
        notify_callback: Optional[Callable[[int, str], Awaitable[None]]] = None,
    ) -> None:
        self._evaluator = evaluator_service
        self._session_manager = session_manager
        self._get_conversation = get_conversation
        self._notify = notify_callback

    async def refresh(self, session: "ClaudeSession", user_id: int) -> bool:
        """Execute context refresh: final eval + session removal + notification.

        Returns True if refresh completed (even if evaluator had issues).
        """
        logger.info(
            "Starting context refresh",
            session_id=session.session_id,
            cumulative_tokens=session.cumulative_tokens,
            user_id=user_id,
        )

        # 1. Run final evaluator with decay context
        try:
            conversation_text = await self._get_conversation(session)
            await self._evaluator.run(
                session_id=session.session_id,
                user_id=user_id,
                conversation_text=conversation_text,
                cumulative_tokens=session.cumulative_tokens,
            )
        except Exception as e:
            logger.error("Final evaluator failed during refresh", error=str(e))
            # Continue with refresh even if evaluator fails -- don't leave user stuck

        # 2. Remove the old session
        try:
            await self._session_manager.remove_session(session.session_id)
            logger.info("Session removed for refresh", session_id=session.session_id)
        except Exception as e:
            logger.error("Failed to remove session during refresh", error=str(e))
            return False

        # 3. Notify user
        if self._notify:
            try:
                await self._notify(
                    user_id,
                    "Session refreshed at 150k tokens. Memory preserved -- starting fresh session.",
                )
            except Exception as e:
                logger.warning("Failed to send refresh notification", error=str(e))

        logger.info("Context refresh completed", user_id=user_id)
        return True
