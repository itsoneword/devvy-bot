from contextlib import asynccontextmanager

import aiosqlite
import pytest

from src.observability.lifecycle import LifecycleLogger


class _DB:
    """Minimal db_manager stub exposing get_connection() like DatabaseManager."""

    def __init__(self, conn):
        self._conn = conn

    @asynccontextmanager
    async def get_connection(self):
        yield self._conn


@pytest.mark.asyncio
async def test_record_and_recent():
    conn = await aiosqlite.connect(":memory:")
    db = _DB(conn)
    lg = LifecycleLogger(db)
    await lg.ensure_schema()

    await lg.record(profile="devops", event="resume", detail={"session": "abc"})
    await lg.record(profile="devops", event="memory_loaded", detail={"chars": 4200})
    await lg.record(profile="channel", event="fresh", detail={})

    recent = await lg.recent(profile="devops", limit=10)
    kinds = [r["event"] for r in recent]
    assert "resume" in kinds and "memory_loaded" in kinds
    assert all(r["profile"] == "devops" for r in recent)
    await conn.close()


@pytest.mark.asyncio
async def test_record_never_raises_on_bad_db():
    class _Broken:
        def get_connection(self):
            raise RuntimeError("boom")

    lg = LifecycleLogger(_Broken())
    # ensure_schema and record swallow errors
    await lg.ensure_schema()
    await lg.record(profile="x", event="resume", detail={})
    assert await lg.recent(profile="x") == []
