"""dv-5e4f Phase 2: SQLAlchemy ``AsyncAdaptedQueuePool`` over
``DatabaseManager.get_connection`` (src/storage/database.py).

Proves the three defects the hand-rolled pool had (see the plan, decision 1):
no rollback-on-return, unbounded overflow (no real timeout), no pre-ping --
plus the invariants the swap had to preserve: nested acquisition inside one
task must not deadlock, every physical connection (including an overflow
one) carries ``py_casefold`` when the manager is a ``UserDataManager``, no
``row_factory`` drift across checkouts, and the daemon-thread property that
lets a process exit even with an unclosed manager.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import aiosqlite
import pytest
from sqlalchemy.exc import TimeoutError as SATimeoutError

from src.storage.database import DatabaseManager
from src.storage.user_data import UserDataManager


@pytest.fixture
async def db(tmp_path: Path) -> DatabaseManager:
    manager = DatabaseManager(f"sqlite:///{tmp_path / 'pool.db'}")
    await manager.initialize()
    yield manager
    await manager.close()


async def test_uncommitted_write_rolled_back_on_return(db: DatabaseManager) -> None:
    """``pool_reset_on_return='rollback'``: a write that never called
    commit() must not survive the connection going back to the pool."""
    async with db.get_connection() as conn:
        await conn.execute("INSERT INTO users (user_id) VALUES (12345)")
        # no commit() -- the bug this replaces let this leak into the next
        # checkout's transaction (or just silently vanish on close() without
        # ever being rolled back explicitly).

    async with db.get_connection() as conn:
        cur = await conn.execute("SELECT COUNT(*) FROM users WHERE user_id = 12345")
        row = await cur.fetchone()
        assert row[0] == 0


async def test_committed_write_survives_return(db: DatabaseManager) -> None:
    """The rollback-on-return reset must be a no-op after an explicit
    commit() -- it must never undo real, already-durable work."""
    async with db.get_connection() as conn:
        await conn.execute("INSERT INTO users (user_id) VALUES (12346)")
        await conn.commit()

    async with db.get_connection() as conn:
        cur = await conn.execute("SELECT COUNT(*) FROM users WHERE user_id = 12346")
        row = await cur.fetchone()
        assert row[0] == 1


@pytest.mark.timing
async def test_checkout_past_size_and_overflow_times_out(tmp_path: Path) -> None:
    """Past ``pool_size + max_overflow`` simultaneous checkouts, a new
    request must fail fast with a real timeout -- the old pool grew
    connections without limit instead."""
    manager = DatabaseManager(
        f"sqlite:///{tmp_path / 'small.db'}",
        pool_size=1,
        max_overflow=1,
        pool_timeout=0.3,
    )
    await manager.initialize()
    try:
        cm1 = manager.get_connection()
        await cm1.__aenter__()  # pool_size(1)
        cm2 = manager.get_connection()
        await cm2.__aenter__()  # + max_overflow(1) = 2 held, OK

        start = time.monotonic()
        with pytest.raises(SATimeoutError):
            async with manager.get_connection():
                pass
        elapsed = time.monotonic() - start
        # Should fail close to pool_timeout, not hang indefinitely.
        assert elapsed < 5.0

        await cm1.__aexit__(None, None, None)
        await cm2.__aexit__(None, None, None)
    finally:
        await manager.close()


async def test_nested_acquisition_does_not_deadlock(db: DatabaseManager) -> None:
    """A get_connection() call nested inside another, in the same task
    (e.g. a repository method calling another repository method), must
    check out a SECOND connection rather than deadlock trying to reenter
    the first."""

    async def inner() -> int:
        async with db.get_connection() as conn:
            cur = await conn.execute("SELECT 2")
            row = await cur.fetchone()
            return row[0]

    async with db.get_connection() as outer_conn:
        cur = await outer_conn.execute("SELECT 1")
        outer_row = await cur.fetchone()
        inner_value = await asyncio.wait_for(inner(), timeout=5.0)

    assert outer_row[0] == 1
    assert inner_value == 2


async def test_dead_connection_replaced_by_pre_ping(db: DatabaseManager) -> None:
    """A connection killed out from under the pool (process/network loss,
    not a clean close()) must be detected by pre-ping and transparently
    replaced, not handed back to the caller broken."""
    async with db.get_connection() as conn:
        await conn.close()  # simulate a dead connection, bypassing checkin

    # The next checkout must notice the ping fails and open a fresh one.
    async with db.get_connection() as conn:
        cur = await conn.execute("SELECT 1")
        row = await cur.fetchone()
        assert row[0] == 1


async def test_row_factory_consistent_across_checkouts(db: DatabaseManager) -> None:
    """Every checkout -- including one that reuses a connection a previous
    checkout returned -- must see ``row_factory = aiosqlite.Row`` (dict-like
    named access). Nothing in the pool reset should disturb it."""
    for _ in range(5):
        async with db.get_connection() as conn:
            assert conn.row_factory is aiosqlite.Row
            cur = await conn.execute("SELECT 1 AS one")
            row = await cur.fetchone()
            assert row["one"] == 1


async def test_py_casefold_present_on_overflow_connection(tmp_path: Path) -> None:
    """``UserDataManager._new_connection`` registers ``py_casefold`` on every
    physical connection it opens. With ``pool_size=1``, the SECOND concurrent
    checkout must come from overflow -- and still have the function."""
    manager = UserDataManager(
        f"sqlite:///{tmp_path / 'data.db'}", pool_size=1, max_overflow=2
    )
    await manager.initialize()
    try:
        async with manager.get_connection() as conn1:
            # Held open so the next checkout is forced into overflow.
            async with manager.get_connection() as conn2:
                for conn in (conn1, conn2):
                    cur = await conn.execute("SELECT py_casefold('Ё')")
                    row = await cur.fetchone()
                    assert row[0] == "ё"
    finally:
        await manager.close()


def test_process_exits_with_unclosed_manager(tmp_path: Path) -> None:
    """A process that builds a DatabaseManager (opening pooled connections)
    and exits WITHOUT calling close() must not hang -- the daemon-thread
    flag on every physical connection (set in _new_connection, unchanged by
    the pool swap) is what makes that true."""
    script = textwrap.dedent(f"""
        import asyncio
        import sys
        sys.path.insert(0, {str(Path(__file__).resolve().parents[3])!r})
        from src.storage.database import DatabaseManager

        async def main():
            manager = DatabaseManager("sqlite:///{tmp_path / 'exit.db'}")
            await manager.initialize()
            async with manager.get_connection() as conn:
                await conn.execute("SELECT 1")
            # Deliberately no manager.close() -- process just exits.

        asyncio.run(main())
        """)
    script_path = tmp_path / "unclosed_manager.py"
    script_path.write_text(script)

    result = subprocess.run(
        [sys.executable, str(script_path)],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
