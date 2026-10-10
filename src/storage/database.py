"""Database connection and initialization.

Features:
- Connection pooling
- Automatic migrations (Alembic, src/storage/schema/migrate.py)
- Health checks
"""

import asyncio
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import AsyncIterator, cast

import aiosqlite
import structlog
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import AsyncAdaptedQueuePool

from .schema.migrate import ensure_schema

logger = structlog.get_logger()


# Python 3.12+: sqlite3's default datetime adapter is deprecated.
# Register explicit adapters/converters once at import time to avoid warnings
# and keep consistent ISO-8601 persistence for datetime values.
sqlite3.register_adapter(datetime, lambda value: value.isoformat())
sqlite3.register_converter("TIMESTAMP", lambda b: datetime.fromisoformat(b.decode()))
sqlite3.register_converter("DATETIME", lambda b: datetime.fromisoformat(b.decode()))
# Keep DATE columns as raw ISO strings (matches existing model expectations).
sqlite3.register_converter("DATE", lambda b: b.decode())


class DatabaseManager:
    """Manage database connections and initialization."""

    # Alembic section / script dir this file's schema lives in.
    schema_name = "bot"

    def __init__(
        self,
        database_url: str,
        *,
        pool_size: int = 5,
        max_overflow: int = 10,
        pool_timeout: float = 30.0,
        pool_pre_ping: bool = True,
    ):
        """Initialize database manager.

        The pool (dv-5e4f Phase 2) is built here, not in :meth:`initialize`:
        ``scripts/golden_replay.py`` deliberately constructs a bare
        ``DatabaseManager`` and calls :meth:`get_connection` WITHOUT ever
        calling :meth:`initialize` (that would run migrations -- writes --
        against a prod copy it only wants to read). The engine itself opens
        no connection at construction time; connections are created lazily,
        on checkout, by :meth:`_new_connection`.
        """
        self.database_path = self._parse_database_url(database_url)
        self._pool_size = pool_size
        self._max_overflow = max_overflow
        self._pool_timeout = pool_timeout
        self._pool_pre_ping = pool_pre_ping
        self._engine: AsyncEngine = self._build_engine()

    def _parse_database_url(self, database_url: str) -> Path:
        """Parse database URL to path."""
        if database_url.startswith("sqlite:///"):
            return Path(database_url[10:])
        elif database_url.startswith("sqlite://"):
            return Path(database_url[9:])
        else:
            return Path(database_url)

    def _build_engine(self) -> AsyncEngine:
        """SQLAlchemy's ``AsyncAdaptedQueuePool`` replaces the hand-rolled
        pool (dv-5e4f Phase 2 decision 1A): bounded size + overflow with a
        real checkout timeout (old pool grew without limit), pre-ping (dead
        connections were never detected/replaced), and rollback-on-return
        (an uncommitted write used to survive in a pooled connection and leak
        into the next checkout's transaction).

        ``async_creator=self._new_connection`` means EVERY physical
        connection the pool ever opens -- including overflow ones -- goes
        through the exact same path as before: daemon thread flag set
        pre-await, ``detect_types=PARSE_DECLTYPES``, ``row_factory =
        aiosqlite.Row``, ``PRAGMA foreign_keys=ON``, and (for
        ``UserDataManager``) the ``py_casefold`` connection hook, since that
        subclass overrides ``_new_connection`` and Python dispatches to it
        normally here too.

        The URL's path component is cosmetic -- ``async_creator`` bypasses
        SQLAlchemy's own connect args entirely (every real parameter lives in
        ``_new_connection``) -- but ``URL.create`` still builds it safely
        rather than hand-formatting a string that could break on special
        characters in ``database_path``.

        ``poolclass`` is forced explicitly: the sqlite+aiosqlite dialect does
        not reliably default to ``AsyncAdaptedQueuePool`` on every
        SQLAlchemy version, and ``pool_size``/``max_overflow`` are silently
        ignored by any pool class that is not queue-based.
        """
        url = URL.create("sqlite+aiosqlite", database=str(self.database_path))
        return create_async_engine(
            url,
            async_creator=self._new_connection,
            poolclass=AsyncAdaptedQueuePool,
            pool_size=self._pool_size,
            max_overflow=self._max_overflow,
            pool_timeout=self._pool_timeout,
            pool_pre_ping=self._pool_pre_ping,
            pool_reset_on_return="rollback",
        )

    async def initialize(self) -> None:
        """Bring the schema to head (Alembic; legacy DBs bridged + stamped),
        before the pool hands out any connection. Off-thread: the runner is
        sync sqlite3 and may take a backup first (dv-5e4f Phase 1)."""
        logger.info("Initializing database", path=str(self.database_path))
        action = await asyncio.to_thread(
            ensure_schema, self.schema_name, self.database_path
        )
        logger.info("Database initialization complete", schema=action)

    async def _new_connection(self) -> aiosqlite.Connection:
        """Open a configured aiosqlite connection on a daemon thread.

        The connection backing thread is marked daemon (set before the thread
        starts, on await) so a process can always exit even if close() is
        missed -- e.g. tests that forget teardown, or an ungraceful shutdown.
        SQLite keeps committed data durable and rolls back incomplete txns.
        """
        conn = aiosqlite.connect(
            self.database_path, detect_types=sqlite3.PARSE_DECLTYPES
        )
        conn.daemon = True  # must be set before the thread starts (on await)
        conn = await conn
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA foreign_keys = ON")
        return conn

    @asynccontextmanager
    async def get_connection(self) -> AsyncIterator[aiosqlite.Connection]:
        """Checkout a pooled connection and yield the raw ``aiosqlite``
        connection -- zero call-site changes (dv-5e4f Phase 2).

        SQLAlchemy's pool owns checkout/checkin, overflow, the timeout and
        pre-ping; everything past that is unwrapped immediately down to the
        driver-level connection and handed to the caller, which keeps using
        plain ``conn.execute(sql, params)`` / ``conn.commit()`` /
        ``conn.executescript()`` exactly as it did against the old hand
        -rolled pool. Nested calls (a ``get_connection()`` inside the body of
        another, in the same task) check out a SECOND connection rather than
        reentering one -- safe as long as total concurrent checkouts stay
        under ``pool_size + max_overflow``, which is why that headroom
        exists.
        """
        async_conn = await self._engine.connect()
        try:
            raw = await async_conn.get_raw_connection()
            driver_connection = cast(aiosqlite.Connection, raw.driver_connection)
            yield driver_connection
        finally:
            await async_conn.close()

    async def close(self):
        """Dispose the pool -- closes every idle and checked-in connection."""
        logger.info("Closing database connections")
        await self._engine.dispose()

    async def health_check(self) -> bool:
        """Check database health."""
        try:
            async with self.get_connection() as conn:
                await conn.execute("SELECT 1")
                return True
        except Exception as e:
            logger.error("Database health check failed", error=str(e))
            return False
