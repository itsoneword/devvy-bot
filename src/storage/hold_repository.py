"""DB-backed session holds (dv-8bb5).

Mirrors ``src/storage/user_repository.py``: ``user_holds`` is temporary
suspension state set by an admin from the watchdog alert's hold buttons, kept
separate from ``bot_users`` (the auth whitelist) so the auth hot path's
identity table stays untouched.

``HoldStore`` layers an in-memory snapshot on top of the repository so
``auth_middleware`` can check "is this user held" without a DB round trip on
every message -- same shape as ``UserStore.get_role_cached``. Every mutation
through the store refreshes the snapshot inline.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Dict, Optional

import structlog

if TYPE_CHECKING:
    from .database import DatabaseManager

logger = structlog.get_logger()


@dataclass
class UserHold:
    user_id: int
    held_until: datetime
    reason: Optional[str]
    set_by: Optional[int]
    set_at: Optional[datetime]


def _coerce_dt(val: Any) -> Optional[datetime]:
    if val is None or isinstance(val, datetime):
        return val
    if isinstance(val, str):
        try:
            return datetime.fromisoformat(val)
        except ValueError:
            return None
    return None


def _hold_from_row(row: Any) -> UserHold:
    held_until = _coerce_dt(row[1])
    assert held_until is not None  # NOT NULL column
    return UserHold(
        user_id=row[0],
        held_until=held_until,
        reason=row[2],
        set_by=row[3],
        set_at=_coerce_dt(row[4]),
    )


_HOLD_COLUMNS = "user_id, held_until, reason, set_by, set_at"


class UserHoldRepository:
    """CRUD over user_holds."""

    def __init__(self, db: "DatabaseManager") -> None:
        self._db = db

    async def get_hold(self, user_id: int) -> Optional[UserHold]:
        async with self._db.get_connection() as conn:
            cur = await conn.execute(
                f"SELECT {_HOLD_COLUMNS} FROM user_holds WHERE user_id=?",
                (user_id,),
            )
            row = await cur.fetchone()
            return _hold_from_row(row) if row else None

    async def list_holds(self) -> Dict[int, UserHold]:
        """All rows, including expired ones -- callers decide freshness."""
        async with self._db.get_connection() as conn:
            cur = await conn.execute(f"SELECT {_HOLD_COLUMNS} FROM user_holds")
            rows = await cur.fetchall()
            return {row[0]: _hold_from_row(row) for row in rows}

    async def upsert_hold(
        self,
        user_id: int,
        held_until: datetime,
        reason: Optional[str],
        set_by: Optional[int],
    ) -> None:
        async with self._db.get_connection() as conn:
            await conn.execute(
                """
                INSERT INTO user_holds (user_id, held_until, reason, set_by, set_at)
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(user_id) DO UPDATE SET
                    held_until = excluded.held_until,
                    reason = excluded.reason,
                    set_by = excluded.set_by,
                    set_at = CURRENT_TIMESTAMP
                """,
                (user_id, held_until, reason, set_by),
            )
            await conn.commit()
        logger.info(
            "user_hold set",
            user_id=user_id,
            held_until=held_until.isoformat(),
            reason=reason,
            set_by=set_by,
        )

    async def delete_hold(self, user_id: int) -> bool:
        async with self._db.get_connection() as conn:
            cur = await conn.execute(
                "DELETE FROM user_holds WHERE user_id=?", (user_id,)
            )
            await conn.commit()
            removed = cur.rowcount > 0
        if removed:
            logger.info("user_hold cleared", user_id=user_id)
        return removed


class HoldStore:
    """Repository facade with an in-memory snapshot for the auth hot path.

    ``held_until`` is the only method ``auth_middleware`` calls, synchronously,
    per message.
    """

    def __init__(self, repo: UserHoldRepository) -> None:
        self._repo = repo
        self._holds: Dict[int, datetime] = {}

    async def load(self) -> None:
        """Seed the snapshot from the DB (startup)."""
        holds = await self._repo.list_holds()
        self._holds = {uid: h.held_until for uid, h in holds.items()}

    def held_until(self, user_id: int) -> Optional[datetime]:
        """The active hold's expiry, or None if not currently held.

        Lazily treats an expired hold as "not held" without deleting the row
        -- the next hold on this user overwrites it anyway.
        """
        until = self._holds.get(user_id)
        if until is None or until <= datetime.now(UTC):
            return None
        return until

    async def set_hold(
        self,
        user_id: int,
        duration: timedelta,
        reason: Optional[str] = None,
        set_by: Optional[int] = None,
    ) -> datetime:
        until = datetime.now(UTC) + duration
        await self._repo.upsert_hold(user_id, until, reason, set_by)
        self._holds[user_id] = until
        return until

    async def clear_hold(self, user_id: int) -> None:
        await self._repo.delete_hold(user_id)
        self._holds.pop(user_id, None)
