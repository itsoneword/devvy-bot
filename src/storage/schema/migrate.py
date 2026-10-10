"""Schema runner for the 4 SQLite DBs: Alembic + one-release legacy bridge
(dv-5e4f Phase 1, owner decisions 3 and 4).

``ensure_schema(db_name, path)`` is the one entry point every process calls
before using a DB. It opens its own sqlite3 connection, takes
``BEGIN IMMEDIATE`` (so two processes starting together serialize: the
second waits, then sees the work done and no-ops), decides what the file
needs, and does it in ONE transaction -- a failure rolls the file back
untouched:

- empty file                          -> ``upgrade head``
- ``alembic_version`` at head         -> nothing (no backup)
- ``alembic_version`` behind head     -> backup, ``upgrade head``
- pre-Alembic (``schema_version`` for bot/data, bare tables for
  llm_analytics/channel_intel)        -> backup, replay the frozen legacy
  history (``legacy.py``), verify the result is structurally identical to
  the Alembic baseline, ``stamp`` baseline, ``upgrade head``

``migrate_connection`` is the same thing on a caller's open connection
(llm_analytics/channel_intel ``init_db`` keep their own connections and
in-memory test DBs).

Backups: sqlite3 backup API (WAL-safe, consistent) to
``<db dir>/backups/<db>.<utc>.pre-<target rev>.db``; the last 3 per DB kept.
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Union

import structlog
from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine
from sqlalchemy.engine import Connection
from sqlalchemy.pool import StaticPool

from . import legacy
from .structure import diff, snapshot_connection

logger = structlog.get_logger()

DB_NAMES = ("bot", "data", "llm_analytics", "channel_intel")

# Revision the legacy schema equals; a bridged DB is stamped here, then
# upgraded to head like any other.
BASELINE = {
    "bot": "0002",
    "data": "0001",
    "llm_analytics": "0001",
    "channel_intel": "0002",
}

REPO_ROOT = Path(__file__).resolve().parents[3]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"
MIGRATIONS_DIR = REPO_ROOT / "migrations"
BACKUP_KEEP = 3
BUSY_TIMEOUT_S = 120.0


class SchemaMismatchError(RuntimeError):
    """A legacy DB did not come out structurally equal to the baseline."""


def alembic_config(db_name: str) -> Config:
    if db_name not in DB_NAMES:
        raise ValueError(f"unknown db {db_name!r}")
    cfg = Config(str(ALEMBIC_INI), ini_section=db_name)
    # Absolute, so it works from any cwd (the ini uses %(here)s too).
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR / db_name))
    return cfg


def head_revision(db_name: str) -> str:
    head = ScriptDirectory.from_config(alembic_config(db_name)).get_current_head()
    assert head is not None
    return head


class _Borrowed:
    """The caller's sqlite3 connection, lent to SQLAlchemy.

    SQLAlchemy would commit/roll back/close it on its own schedule (pool
    reset, connection close); the runner owns the one real transaction, so
    those become no-ops here and everything else passes through.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        object.__setattr__(self, "_conn", conn)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(self._conn, name, value)

    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        pass

    def close(self) -> None:
        pass


@contextmanager
def _sa_connection(conn: sqlite3.Connection) -> Iterator[Connection]:
    engine = create_engine(
        "sqlite://", creator=lambda: _Borrowed(conn), poolclass=StaticPool
    )
    try:
        with engine.connect() as sa_conn:
            # pysqlite's BEGIN is a no-op at the driver level; this only marks
            # the transaction as external so Alembic never commits it. The
            # real BEGIN IMMEDIATE / COMMIT belong to migrate_connection.
            sa_conn.begin()
            yield sa_conn
    finally:
        engine.dispose(close=False)


def _run_alembic(conn: sqlite3.Connection, db_name: str, fn: Any, *args: Any) -> None:
    cfg = alembic_config(db_name)
    with _sa_connection(conn) as sa_conn:
        cfg.attributes["connection"] = sa_conn
        fn(cfg, *args)


def _current_revision(conn: sqlite3.Connection) -> Optional[str]:
    with _sa_connection(conn) as sa_conn:
        return MigrationContext.configure(sa_conn).get_current_revision()


def _user_tables(conn: sqlite3.Connection) -> List[str]:
    return [
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'"
        )
    ]


@lru_cache(maxsize=None)
def baseline_snapshot(db_name: str) -> Dict[str, Any]:
    """Structure of a fresh in-memory DB built by Alembic up to BASELINE."""
    mem = sqlite3.connect(":memory:", isolation_level=None)
    try:
        mem.execute("BEGIN")
        _run_alembic(mem, db_name, command.upgrade, BASELINE[db_name])
        mem.execute("COMMIT")
        return snapshot_connection(mem)
    finally:
        mem.close()


def _main_db_file(conn: sqlite3.Connection) -> Optional[Path]:
    for _seq, name, file in conn.execute("PRAGMA database_list"):
        if name == "main":
            return Path(file) if file else None
    return None


def backup(db_path: Path, db_name: str, target_rev: str) -> Path:
    """WAL-safe copy via the sqlite3 backup API; prune to the last 3."""
    backup_dir = db_path.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    dest = backup_dir / f"{db_name}.{stamp}.pre-{target_rev}.db"
    src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=BUSY_TIMEOUT_S)
    dst = sqlite3.connect(str(dest))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    for old in sorted(backup_dir.glob(f"{db_name}.*.pre-*.db"))[:-BACKUP_KEEP]:
        old.unlink()
    logger.info("Schema backup written", db=db_name, path=str(dest))
    return dest


def _ensure_wal(conn: sqlite3.Connection) -> None:
    """Switch a file DB to WAL (every DB here runs WAL). The switch needs an
    exclusive lock and SQLite may report BUSY without waiting when another
    process is mid-switch or holds the migration lock, so retry until the
    busy timeout; a no-op when the DB is already WAL."""
    deadline = time.monotonic() + BUSY_TIMEOUT_S
    while True:
        try:
            if conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal":
                return
            conn.execute("PRAGMA journal_mode=WAL")
            return
        except sqlite3.OperationalError as e:
            if "locked" not in str(e) and "busy" not in str(e):
                raise
            if time.monotonic() > deadline:
                raise
            time.sleep(0.05)


def migrate_connection(
    db_name: str,
    conn: sqlite3.Connection,
    *,
    db_path: Optional[Union[str, Path]] = None,
) -> str:
    """Bring ``conn``'s DB to head. Returns what happened: ``"current"``,
    ``"created"``, ``"upgraded"`` or ``"bridged"``.

    ``db_path`` is only used for the backup; defaults to the connection's
    main file (none for ``:memory:``). The connection must not be inside a
    transaction; it is left in the isolation mode it came in.
    """
    if db_name not in DB_NAMES:
        raise ValueError(f"unknown db {db_name!r}")
    if conn.in_transaction:
        conn.commit()
    previous_isolation = conn.isolation_level
    previous_row_factory = conn.row_factory
    conn.isolation_level = None  # explicit BEGIN/COMMIT below
    conn.row_factory = None  # plain tuples for SQLAlchemy/Alembic
    try:
        path = Path(db_path) if db_path else _main_db_file(conn)
        if path is not None:
            _ensure_wal(conn)  # not allowed inside a txn
        conn.execute("BEGIN IMMEDIATE")
        try:
            action = _migrate_locked(db_name, conn, path)
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.isolation_level = previous_isolation
        conn.row_factory = previous_row_factory
    if action != "current":
        logger.info(
            "Schema migrated", db=db_name, action=action, head=head_revision(db_name)
        )
    return action


def _migrate_locked(
    db_name: str, conn: sqlite3.Connection, path: Optional[Path]
) -> str:
    head = head_revision(db_name)
    tables = _user_tables(conn)

    if "alembic_version" in tables:
        current = _current_revision(conn)
        if current == head:
            return "current"
        if path is not None:
            backup(path, db_name, head)
        _run_alembic(conn, db_name, command.upgrade, "head")
        return "upgraded"

    if not tables:
        _run_alembic(conn, db_name, command.upgrade, "head")
        return "created"

    if db_name in legacy.VERSIONED and not legacy.has_schema_version(conn):
        raise SchemaMismatchError(
            f"{db_name}: tables but neither schema_version nor alembic_version; "
            "refusing to guess its schema"
        )

    # Pre-Alembic DB: frozen legacy replay -> verify -> stamp -> head.
    if path is not None:
        backup(path, db_name, head)
    before, after = legacy.run_legacy(conn, db_name)
    problems = diff(
        snapshot_connection(conn), baseline_snapshot(db_name), "db", "baseline"
    )
    if problems:
        raise SchemaMismatchError(
            f"{db_name}: legacy schema differs from Alembic baseline "
            f"{BASELINE[db_name]}:\n  " + "\n  ".join(problems)
        )
    _run_alembic(conn, db_name, command.stamp, BASELINE[db_name])
    _run_alembic(conn, db_name, command.upgrade, "head")
    logger.info(
        "Legacy schema bridged",
        db=db_name,
        legacy_from=before,
        legacy_to=after,
        stamped=BASELINE[db_name],
    )
    return "bridged"


def ensure_schema(db_name: str, path: Union[str, Path]) -> str:
    """Bring the DB file at ``path`` to head (creating it if needed)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), isolation_level=None, timeout=BUSY_TIMEOUT_S)
    try:
        return migrate_connection(db_name, conn, db_path=path)
    finally:
        conn.close()
