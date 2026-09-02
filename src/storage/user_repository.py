"""DB-backed bot users and families (users & families v1).

Mirrors src/storage/task_repository.py. ``bot_users`` holds runtime-managed
access (guests added via /adduser or /family add, plus GUEST_USERS env seeds);
``families`` groups users. Owners still bootstrap from the ALLOWED_USERS env —
DB rows with role='owner' are honored by auth but never created by /adduser.

``UserStore`` layers an in-memory role snapshot on top of the repository so the
auth hot path (``resolve_role``, called on every Telegram update) stays sync
and cheap. Every mutation through the store refreshes the snapshot inline;
``invalidate()`` marks it dirty for external writers, and ``ensure_fresh()``
reloads on the next async auth pass.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Dict, List, Optional

import structlog

if TYPE_CHECKING:
    from .database import DatabaseManager

logger = structlog.get_logger()

SOURCE_ENV = "env"
SOURCE_ADMIN = "admin"
SOURCE_FAMILY = "family"

# Mirrors src.security.auth.ROLE_*; duplicated rather than imported to keep
# storage from depending on the security layer for two string literals.
ROLE_OWNER = "owner"
ROLE_GUEST = "guest"


@dataclass
class BotUser:
    telegram_id: int
    role: str
    family_id: Optional[int]
    added_by: Optional[int]
    source: str
    created_at: Optional[datetime]


@dataclass
class Family:
    id: int
    created_by: int
    created_at: Optional[datetime]


_USER_COLUMNS = "telegram_id, role, family_id, added_by, source, created_at"


def _coerce_dt(val: Any) -> Optional[datetime]:
    if val is None or isinstance(val, datetime):
        return val
    if isinstance(val, str):
        try:
            return datetime.fromisoformat(val)
        except ValueError:
            return None
    return None


def _user_from_row(row: Any) -> BotUser:
    return BotUser(
        telegram_id=row[0],
        role=row[1],
        family_id=row[2],
        added_by=row[3],
        source=row[4],
        created_at=_coerce_dt(row[5]),
    )


def _family_from_row(row: Any) -> Family:
    return Family(id=row[0], created_by=row[1], created_at=_coerce_dt(row[2]))


class BotUserRepository:
    """CRUD over bot_users + families."""

    def __init__(self, db: "DatabaseManager") -> None:
        self._db = db

    # --- users ---

    async def get_user(self, telegram_id: int) -> Optional[BotUser]:
        async with self._db.get_connection() as conn:
            cur = await conn.execute(
                f"SELECT {_USER_COLUMNS} FROM bot_users WHERE telegram_id=?",
                (telegram_id,),
            )
            row = await cur.fetchone()
            return _user_from_row(row) if row else None

    async def list_users(self) -> List[BotUser]:
        async with self._db.get_connection() as conn:
            cur = await conn.execute(
                f"SELECT {_USER_COLUMNS} FROM bot_users ORDER BY telegram_id"
            )
            return [_user_from_row(row) for row in await cur.fetchall()]

    async def add_user(
        self,
        telegram_id: int,
        role: str = "guest",
        source: str = SOURCE_ADMIN,
        added_by: Optional[int] = None,
        family_id: Optional[int] = None,
    ) -> BotUser:
        """Insert a user row; refuses to overwrite an existing row."""
        async with self._db.get_connection() as conn:
            await conn.execute(
                "INSERT INTO bot_users "
                "(telegram_id, role, family_id, added_by, source) "
                "VALUES (?, ?, ?, ?, ?)",
                (telegram_id, role, family_id, added_by, source),
            )
            await conn.commit()
        logger.info(
            "bot_user added",
            telegram_id=telegram_id,
            role=role,
            source=source,
            added_by=added_by,
        )
        user = await self.get_user(telegram_id)
        assert user is not None
        return user

    async def remove_user(self, telegram_id: int) -> bool:
        """Delete the row (also clears family membership with it)."""
        async with self._db.get_connection() as conn:
            cur = await conn.execute(
                "DELETE FROM bot_users WHERE telegram_id=?", (telegram_id,)
            )
            await conn.commit()
            removed = cur.rowcount > 0
        if removed:
            logger.info("bot_user removed", telegram_id=telegram_id)
        return removed

    async def _seed_env_role(self, telegram_ids: List[int], role: str) -> int:
        """Upsert env-configured ids at ``role``, source=env.

        Idempotent: existing rows (including admin/family-added ones) are left
        untouched — seeding never downgrades or re-sources a row.
        """
        if not telegram_ids:
            return 0
        inserted = 0
        async with self._db.get_connection() as conn:
            for tg_id in telegram_ids:
                cur = await conn.execute(
                    "INSERT INTO bot_users (telegram_id, role, source) "
                    "VALUES (?, ?, ?) "
                    "ON CONFLICT(telegram_id) DO NOTHING",
                    (tg_id, role, SOURCE_ENV),
                )
                inserted += cur.rowcount if cur.rowcount > 0 else 0
            await conn.commit()
        if inserted:
            logger.info("env users seeded", role=role, count=inserted)
        return inserted

    async def seed_env_guests(self, telegram_ids: List[int]) -> int:
        """Upsert GUEST_USERS env ids as (guest, source=env)."""
        return await self._seed_env_role(telegram_ids, ROLE_GUEST)

    async def seed_env_owners(self, telegram_ids: List[int]) -> int:
        """Upsert ALLOWED_USERS env ids as (owner, source=env).

        The installer's captured owner arrives this way (dv-5c83): it writes
        ALLOWED_USERS into .env, and first boot turns that into a real row. An
        id already present as a guest is NOT promoted — the same
        never-downgrade, never-re-source rule as guests, applied in both
        directions.
        """
        return await self._seed_env_role(telegram_ids, ROLE_OWNER)

    async def set_family(self, telegram_id: int, family_id: Optional[int]) -> bool:
        async with self._db.get_connection() as conn:
            cur = await conn.execute(
                "UPDATE bot_users SET family_id=? WHERE telegram_id=?",
                (family_id, telegram_id),
            )
            await conn.commit()
            return cur.rowcount > 0

    async def count_users(self) -> int:
        async with self._db.get_connection() as conn:
            cur = await conn.execute("SELECT COUNT(*) FROM bot_users")
            row = await cur.fetchone()
            return int(row[0]) if row else 0

    # --- families ---

    async def create_family(self, created_by: int) -> Family:
        async with self._db.get_connection() as conn:
            cur = await conn.execute(
                "INSERT INTO families (created_by) VALUES (?)", (created_by,)
            )
            await conn.commit()
            family_id = int(cur.lastrowid or 0)
        logger.info("family created", family_id=family_id, created_by=created_by)
        family = await self.get_family(family_id)
        assert family is not None
        return family

    async def get_family(self, family_id: int) -> Optional[Family]:
        async with self._db.get_connection() as conn:
            cur = await conn.execute(
                "SELECT id, created_by, created_at FROM families WHERE id=?",
                (family_id,),
            )
            row = await cur.fetchone()
            return _family_from_row(row) if row else None

    async def list_family_members(self, family_id: int) -> List[BotUser]:
        async with self._db.get_connection() as conn:
            cur = await conn.execute(
                f"SELECT {_USER_COLUMNS} FROM bot_users "
                "WHERE family_id=? ORDER BY created_at, telegram_id",
                (family_id,),
            )
            return [_user_from_row(row) for row in await cur.fetchall()]

    async def count_family_members(self, family_id: int) -> int:
        async with self._db.get_connection() as conn:
            cur = await conn.execute(
                "SELECT COUNT(*) FROM bot_users WHERE family_id=?", (family_id,)
            )
            row = await cur.fetchone()
            return int(row[0]) if row else 0


class UserStore:
    """Repository facade with a sync role cache for the auth hot path.

    All access-changing mutations go through this store so the snapshot is
    refreshed inline (exact invalidation, no TTL). ``get_role_cached`` is the
    only method auth calls synchronously per update.
    """

    def __init__(self, repo: BotUserRepository) -> None:
        self._repo = repo
        self._roles: Dict[int, str] = {}
        self._family_ids: Dict[int, Optional[int]] = {}
        self._dirty = True

    # --- cache (sync hot path) ---

    def get_role_cached(self, telegram_id: int) -> Optional[str]:
        """Role from the in-memory snapshot; None if unknown."""
        return self._roles.get(telegram_id)

    def get_family_id_cached(self, telegram_id: int) -> Optional[int]:
        """Family id from the in-memory snapshot; None = no family (or
        unknown user). Refreshed alongside roles on every mutation."""
        return self._family_ids.get(telegram_id)

    def guest_ids(self) -> List[int]:
        """All cached ids with role guest (for scoped command menus)."""
        return [uid for uid, role in self._roles.items() if role == ROLE_GUEST]

    def owner_ids(self) -> List[int]:
        """All cached ids with role owner.

        Used at boot to decide whether an auth provider is warranted: an
        install whose owner lives only in the DB (env later cleared) must
        still start with a whitelist, not fall through to the refusal.
        """
        return [uid for uid, role in self._roles.items() if role == ROLE_OWNER]

    def invalidate(self) -> None:
        """Mark the snapshot dirty; reloaded on the next ensure_fresh()."""
        self._dirty = True

    async def ensure_fresh(self) -> None:
        if self._dirty:
            await self.refresh()

    async def refresh(self) -> None:
        users = await self._repo.list_users()
        self._roles = {u.telegram_id: u.role for u in users}
        self._family_ids = {u.telegram_id: u.family_id for u in users}
        self._dirty = False

    # --- pass-through reads ---

    @property
    def repo(self) -> BotUserRepository:
        return self._repo

    async def get_user(self, telegram_id: int) -> Optional[BotUser]:
        return await self._repo.get_user(telegram_id)

    async def get_family(self, family_id: int) -> Optional[Family]:
        return await self._repo.get_family(family_id)

    async def list_family_members(self, family_id: int) -> List[BotUser]:
        return await self._repo.list_family_members(family_id)

    async def count_family_members(self, family_id: int) -> int:
        return await self._repo.count_family_members(family_id)

    # --- mutations (refresh the snapshot inline) ---

    async def seed_env_guests(self, telegram_ids: List[int]) -> int:
        inserted = await self._repo.seed_env_guests(telegram_ids)
        await self.refresh()
        return inserted

    async def seed_env_owners(self, telegram_ids: List[int]) -> int:
        inserted = await self._repo.seed_env_owners(telegram_ids)
        await self.refresh()
        return inserted

    async def add_user(
        self,
        telegram_id: int,
        role: str = "guest",
        source: str = SOURCE_ADMIN,
        added_by: Optional[int] = None,
        family_id: Optional[int] = None,
    ) -> BotUser:
        user = await self._repo.add_user(
            telegram_id,
            role=role,
            source=source,
            added_by=added_by,
            family_id=family_id,
        )
        await self.refresh()
        return user

    async def remove_user(self, telegram_id: int) -> bool:
        removed = await self._repo.remove_user(telegram_id)
        await self.refresh()
        return removed

    async def set_family(self, telegram_id: int, family_id: Optional[int]) -> bool:
        changed = await self._repo.set_family(telegram_id, family_id)
        await self.refresh()
        return changed

    async def create_family(self, created_by: int) -> Family:
        return await self._repo.create_family(created_by)
