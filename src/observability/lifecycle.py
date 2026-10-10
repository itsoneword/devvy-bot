"""Records profile/session lifecycle events to SQLite; surfaced via /diag.

Thin wrapper over LifecycleRepository (dv-5e4f Phase 0): the
``lifecycle_events`` table's DDL now lives in bot.db migration v26
(src/storage/database.py), owned once, instead of this class creating it
itself on every boot. Never raises -- observability must never break a
response.
"""

from typing import Any, Dict, List

import structlog

from ..storage.lifecycle_repository import LifecycleRepository

logger = structlog.get_logger()


class LifecycleLogger:
    """Append-only lifecycle event recorder. Never raises."""

    def __init__(self, db: Any) -> None:
        self._db = db
        self._repo = LifecycleRepository(db)

    async def ensure_schema(self) -> None:
        """Idempotent against a real bot.db -- migration v26
        (src/storage/database.py) already created the table before this
        class is ever constructed in production. Still delegates to the
        repository (not a pure no-op) so a caller handing in a bare
        db-like stub of its own still gets a working table, e.g. tests."""
        try:
            await self._repo.ensure_schema()
        except Exception as e:  # noqa: BLE001
            logger.warning("lifecycle schema init failed", error=str(e))

    async def record(self, profile: str, event: str, detail: Dict[str, Any]) -> None:
        try:
            await self._repo.record(profile, event, detail)
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "lifecycle record failed", lifecycle_event=event, error=str(e)
            )

    async def recent(self, profile: str, limit: int = 50) -> List[Dict[str, Any]]:
        try:
            return await self._repo.recent(profile, limit)
        except Exception as e:  # noqa: BLE001
            logger.warning("lifecycle recent failed", error=str(e))
            return []
