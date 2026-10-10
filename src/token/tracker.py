"""Token usage tracking service.

Records per-turn token usage in SQLite and provides query methods
for cumulative session tracking.

Thin wrapper over TokenTrackingRepository / MemoryExtractionRepository
(dv-5e4f Phase 0) -- the SQL moved into src/storage/, this class's public
API is unchanged.
"""

from datetime import datetime
from typing import Dict, Optional

import structlog

from ..storage.database import DatabaseManager
from ..storage.extraction_repository import MemoryExtractionRepository
from ..storage.models import MemoryExtractionModel
from ..storage.token_repository import TokenTrackingRepository

logger = structlog.get_logger()


class TokenTracker:
    """Track token usage per session turn."""

    def __init__(self, db: DatabaseManager) -> None:
        self._db = db
        self._tokens = TokenTrackingRepository(db)
        self._extractions = MemoryExtractionRepository(db)

    async def record_turn(
        self,
        session_id: str,
        user_id: int,
        input_tokens: int,
        output_tokens: int,
        cumulative_tokens: int,
        cost: float,
    ) -> None:
        """Insert a row into session_tracking."""
        await self._tokens.record_turn(
            session_id, user_id, input_tokens, output_tokens, cumulative_tokens, cost
        )
        logger.debug(
            "Recorded token turn",
            session_id=session_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cumulative_tokens=cumulative_tokens,
        )

    async def get_session_totals(self, session_id: str) -> Dict[str, int]:
        """Return {input_tokens, output_tokens, total_tokens} for a session."""
        return await self._tokens.get_session_totals(session_id)

    async def get_cumulative_for_session(self, session_id: str) -> int:
        """Return the latest cumulative_tokens value for a session."""
        return await self._tokens.get_cumulative_for_session(session_id)

    async def get_last_extraction(
        self, session_id: str
    ) -> Optional[MemoryExtractionModel]:
        """Return the most recent memory extraction for a session."""
        return await self._extractions.get_last_extraction(session_id)

    async def get_usage_by_project(
        self, user_id: int, since: Optional[datetime] = None
    ) -> Dict[str, Dict[str, float]]:
        """Aggregate token/cost usage per project path (profile working_dir).

        Joins the per-turn ``session_tracking`` rows to ``sessions`` (which
        maps a Claude session_id to the working directory it ran in) and
        sums input+output tokens, cost, and turn count per project_path.
        ``since`` filters to turns at/after that UTC timestamp; ``None``
        means all-time. Codex-backend turns aren't recorded in
        session_tracking today, so this only covers Claude usage.
        """
        return await self._tokens.get_usage_by_project(user_id, since)
