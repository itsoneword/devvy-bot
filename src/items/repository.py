"""SQLite access for family items and members (dv-752a).

``items`` is the one source of truth for todos and reminders; a scheduler job
only points at an item (``scheduled_jobs.item_id``). Members are the
``bot_users`` rows of one family plus the display name / relation columns
added by migration 23.

Every timestamp is an ISO-8601 UTC string (TEXT columns, so the sqlite
PARSE_DECLTYPES converters never touch them).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Sequence

if TYPE_CHECKING:
    from ..storage.database import DatabaseManager

NOTE_KINDS = ("comment", "research", "status_change")
NOTE_SOURCES = ("manual", "auto")
STATUSES = ("open", "fired", "done", "cancelled")
DELIVERIES = ("message", "agent", "none")

_ITEM_COLUMNS = (
    "id, family_id, creator_id, assignee_id, text, status, due_at, recurrence, "
    "tz, ends_at, deliver, profile_slug, source_profile, last_fired_at, "
    "done_at, done_by, created_at, updated_at, deadline"
)


def now_iso() -> str:
    """Event stamps compare as strings, so one fixed format (microseconds)."""
    return datetime.now(UTC).isoformat(timespec="microseconds")


def parse_iso(value: Any) -> Optional[datetime]:
    """An ISO column value as an aware datetime (naive = UTC), or None."""
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value))
        except ValueError:
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


@dataclass
class Item:
    id: int
    family_id: Optional[int]
    creator_id: int
    assignee_id: int
    text: str
    status: str
    due_at: Optional[str]
    recurrence: Optional[str]
    tz: str
    ends_at: Optional[str]
    deliver: str
    profile_slug: Optional[str]
    source_profile: Optional[str]
    last_fired_at: Optional[str]
    done_at: Optional[str]
    done_by: Optional[int]
    created_at: str
    updated_at: str
    deadline: Optional[str] = None

    @property
    def is_scheduled(self) -> bool:
        return bool(self.due_at or self.recurrence) and self.deliver != "none"

    @property
    def cross_member(self) -> bool:
        return self.creator_id != self.assignee_id


@dataclass
class ItemNote:
    id: int
    item_id: int
    author_id: Optional[int]
    kind: str
    body: str
    path: Optional[str]
    source: str
    created_at: str


@dataclass
class Member:
    telegram_id: int
    role: str
    family_id: Optional[int]
    display_name: Optional[str]
    relation: Optional[str]
    is_admin: bool

    @property
    def label(self) -> str:
        """Human name for headers and listings; the id when none is known."""
        return self.display_name or self.relation or f"id {self.telegram_id}"


def _item(row: Any) -> Item:
    return Item(*tuple(row))


def _member(row: Any) -> Member:
    return Member(
        telegram_id=int(row[0]),
        role=row[1],
        family_id=row[2],
        display_name=row[3],
        relation=row[4],
        is_admin=bool(row[5]),
    )


_MEMBER_COLUMNS = "telegram_id, role, family_id, display_name, relation, is_admin"


class ItemRepository:
    """CRUD over items, the injection cursor, and family members."""

    def __init__(self, db: "DatabaseManager") -> None:
        self._db = db

    # --- items ---

    async def insert(self, **fields: Any) -> int:
        stamp = now_iso()
        fields.setdefault("created_at", stamp)
        fields.setdefault("updated_at", stamp)
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        async with self._db.get_connection() as conn:
            cur = await conn.execute(
                f"INSERT INTO items ({cols}) VALUES ({marks})", tuple(fields.values())
            )
            await conn.commit()
            return int(cur.lastrowid or 0)

    async def get(self, item_id: int) -> Optional[Item]:
        async with self._db.get_connection() as conn:
            cur = await conn.execute(
                f"SELECT {_ITEM_COLUMNS} FROM items WHERE id = ?", (item_id,)
            )
            row = await cur.fetchone()
            return _item(row) if row else None

    async def update(self, item_id: int, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = now_iso()
        sets = ", ".join(f"{k} = ?" for k in fields)
        async with self._db.get_connection() as conn:
            await conn.execute(
                f"UPDATE items SET {sets} WHERE id = ?", (*fields.values(), item_id)
            )
            await conn.commit()

    async def list_for(
        self,
        user_id: int,
        scope: str = "all",
        statuses: Sequence[str] = ("open",),
        limit: int = 50,
    ) -> List[Item]:
        """Items visible to ``user_id`` in ``scope``.

        mine   = created by me for me
        by_me  = created by me for someone else
        for_me = created by someone else for me
        all    = any of the above
        """
        where = {
            "mine": "creator_id = :u AND assignee_id = :u",
            "by_me": "creator_id = :u AND assignee_id != :u",
            "for_me": "assignee_id = :u AND creator_id != :u",
            "all": "(creator_id = :u OR assignee_id = :u)",
        }[scope]
        params: Dict[str, Any] = {"u": user_id, "lim": limit}
        status_marks = []
        for i, status in enumerate(statuses):
            params[f"s{i}"] = status
            status_marks.append(f":s{i}")
        sql = (
            f"SELECT {_ITEM_COLUMNS} FROM items WHERE {where} "
            f"AND status IN ({', '.join(status_marks)}) "
            "ORDER BY (due_at IS NULL), due_at, id LIMIT :lim"
        )
        async with self._db.get_connection() as conn:
            cur = await conn.execute(sql, params)
            return [_item(r) for r in await cur.fetchall()]

    async def count_cross_created_since(self, creator_id: int, since: str) -> int:
        async with self._db.get_connection() as conn:
            cur = await conn.execute(
                "SELECT COUNT(*) FROM items WHERE creator_id = ? "
                "AND assignee_id != ? AND created_at > ?",
                (creator_id, creator_id, since),
            )
            row = await cur.fetchone()
            return int(row[0]) if row else 0

    async def activity_since(
        self, user_id: int, since: str, limit: int = 12
    ) -> List[Item]:
        """Items this user is party to with any event (created, fired,
        closed) after ``since``, newest activity first."""
        async with self._db.get_connection() as conn:
            cur = await conn.execute(
                f"SELECT {_ITEM_COLUMNS} FROM items "
                "WHERE (creator_id = ? OR assignee_id = ?) "
                "AND (created_at > ? OR last_fired_at > ? OR done_at > ?) "
                "ORDER BY max(created_at, COALESCE(last_fired_at, ''), "
                "COALESCE(done_at, '')) DESC LIMIT ?",
                (user_id, user_id, since, since, since, limit),
            )
            return [_item(r) for r in await cur.fetchall()]

    # --- notes ---

    async def add_note(
        self,
        item_id: int,
        author_id: Optional[int],
        kind: str,
        body: str,
        path: Optional[str] = None,
        source: str = "manual",
    ) -> int:
        async with self._db.get_connection() as conn:
            cur = await conn.execute(
                "INSERT INTO item_notes (item_id, author_id, kind, body, path, "
                "source, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (item_id, author_id, kind, body, path, source, now_iso()),
            )
            await conn.commit()
            return int(cur.lastrowid or 0)

    async def list_notes(self, item_id: int) -> List[ItemNote]:
        async with self._db.get_connection() as conn:
            cur = await conn.execute(
                "SELECT id, item_id, author_id, kind, body, path, source, "
                "created_at FROM item_notes WHERE item_id = ? "
                "ORDER BY created_at, id",
                (item_id,),
            )
            return [ItemNote(*tuple(r)) for r in await cur.fetchall()]

    async def count_notes(self, item_id: int) -> int:
        async with self._db.get_connection() as conn:
            cur = await conn.execute(
                "SELECT COUNT(*) FROM item_notes WHERE item_id = ?", (item_id,)
            )
            row = await cur.fetchone()
            return int(row[0]) if row else 0

    # --- injection cursor ---

    async def get_seen(self, user_id: int, profile_slug: str) -> Optional[str]:
        async with self._db.get_connection() as conn:
            cur = await conn.execute(
                "SELECT seen_at FROM item_seen WHERE user_id = ? AND profile_slug = ?",
                (user_id, profile_slug),
            )
            row = await cur.fetchone()
            return str(row[0]) if row else None

    async def set_seen(self, user_id: int, profile_slug: str, seen_at: str) -> None:
        async with self._db.get_connection() as conn:
            await conn.execute(
                "INSERT INTO item_seen (user_id, profile_slug, seen_at) "
                "VALUES (?, ?, ?) ON CONFLICT(user_id, profile_slug) "
                "DO UPDATE SET seen_at = excluded.seen_at",
                (user_id, profile_slug, seen_at),
            )
            await conn.commit()

    # --- members ---

    async def get_member(self, telegram_id: int) -> Optional[Member]:
        async with self._db.get_connection() as conn:
            cur = await conn.execute(
                f"SELECT {_MEMBER_COLUMNS} FROM bot_users WHERE telegram_id = ?",
                (telegram_id,),
            )
            row = await cur.fetchone()
            return _member(row) if row else None

    async def family_members(self, family_id: int) -> List[Member]:
        async with self._db.get_connection() as conn:
            cur = await conn.execute(
                f"SELECT {_MEMBER_COLUMNS} FROM bot_users WHERE family_id = ? "
                "ORDER BY created_at, telegram_id",
                (family_id,),
            )
            return [_member(r) for r in await cur.fetchall()]

    async def set_member_fields(
        self,
        telegram_id: int,
        display_name: Optional[str] = None,
        relation: Optional[str] = None,
    ) -> bool:
        fields: Dict[str, Any] = {}
        if display_name is not None:
            fields["display_name"] = display_name
        if relation is not None:
            fields["relation"] = relation
        if not fields:
            return False
        sets = ", ".join(f"{k} = ?" for k in fields)
        async with self._db.get_connection() as conn:
            cur = await conn.execute(
                f"UPDATE bot_users SET {sets} WHERE telegram_id = ?",
                (*fields.values(), telegram_id),
            )
            await conn.commit()
            return cur.rowcount > 0

    async def fill_display_name(self, telegram_id: int, name: str) -> bool:
        """Set display_name only when it is still NULL (Telegram first_name)."""
        async with self._db.get_connection() as conn:
            cur = await conn.execute(
                "UPDATE bot_users SET display_name = ? "
                "WHERE telegram_id = ? AND display_name IS NULL",
                (name, telegram_id),
            )
            await conn.commit()
            return cur.rowcount > 0
