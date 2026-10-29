"""Harness trace retention (dv-2260 phase 2): ``tool_calls`` rows older than
90 days are deleted at bot startup and at most once a day after that.

Contract pinned here (docs/research/dv-2260/analysis.md, decision 6):

* ``Settings.trace_retention_days`` defaults to 90.
* ``ToolCallRepository.prune(days=90, now=None) -> int`` deletes rows whose
  ``started_at`` is older than ``now - days`` and returns how many.
* ``Storage.initialize()`` (what ``src/main.py`` runs at startup) prunes.
* ``ToolCallRepository.prune_if_due(now=None) -> int`` prunes only if this
  repository has not pruned in the last 24h (the startup prune counts);
  ``upsert`` (the write path) calls it. No scheduler job.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, List
from unittest.mock import AsyncMock

import pytest

from src.storage.facade import Storage


def _row(use_id: str, started_at: datetime, seq: int = 1) -> dict:
    return {
        "tool_use_id": use_id,
        "seq": seq,
        "tool": "Read",
        "status": "ok",
        "started_at": started_at,
    }


async def _ids(storage: Storage) -> List[str]:
    async with storage.db_manager.get_connection() as conn:
        cur = await conn.execute("SELECT tool_use_id FROM tool_calls ORDER BY id")
        return [r[0] for r in await cur.fetchall()]


async def _open(path: Path) -> Storage:
    st = Storage(f"sqlite:///{path}")
    await st.initialize()
    return st


@pytest.fixture
async def storage(tmp_path: Path):
    st = await _open(tmp_path / "bot.db")
    yield st
    await st.close()


def test_retention_default_is_90_days() -> None:
    from src.config.settings import Settings

    s = Settings(telegram_bot_token="x", telegram_bot_username="x")
    assert s.trace_retention_days == 90


async def test_prune_deletes_rows_older_than_90_days_keeps_newer(storage) -> None:
    now = datetime.now(UTC)
    await storage.tool_calls.upsert(
        "t-old",
        [
            _row("old", now - timedelta(days=91)),
            _row("edge", now - timedelta(days=89), 2),
        ],
    )
    await storage.tool_calls.upsert("t-new", [_row("new", now - timedelta(hours=1))])

    deleted = await storage.tool_calls.prune(days=90)

    assert deleted == 1
    assert await _ids(storage) == ["edge", "new"]


async def test_rows_older_than_90_days_deleted_at_startup(tmp_path: Path) -> None:
    """Rows older than 90 days deleted at startup."""
    path = tmp_path / "bot.db"
    st = await _open(path)
    now = datetime.now(UTC)
    await st.tool_calls.upsert(
        "t1",
        [
            _row("old", now - timedelta(days=120)),
            _row("new", now - timedelta(days=2), 2),
        ],
    )
    await st.close()

    st = await _open(path)  # the bot starting up again
    try:
        assert await _ids(st) == ["new"]
    finally:
        await st.close()


async def test_rows_older_than_90_days_deleted_once_a_day(storage) -> None:
    """Rows older than 90 days deleted once a day: not on every write, and
    again once 24h have passed since the last prune."""
    repo = storage.tool_calls
    now = datetime.now(UTC)
    # Startup already pruned (initialize); an old row written after that ...
    await repo.upsert("t1", [_row("old", now - timedelta(days=100))])
    # ... survives further writes the same day: no prune per write.
    await repo.upsert("t2", [_row("new", now)])
    assert await _ids(storage) == ["old", "new"]
    assert await repo.prune_if_due(now=now + timedelta(hours=1)) == 0
    assert await _ids(storage) == ["old", "new"]

    # A day later the next due check prunes it.
    assert await repo.prune_if_due(now=now + timedelta(hours=25)) == 1
    assert await _ids(storage) == ["new"]
    # And not again right after.
    await repo.upsert("t3", [_row("old2", now - timedelta(days=200))])
    assert await repo.prune_if_due(now=now + timedelta(hours=26)) == 0
    assert "old2" in await _ids(storage)


async def test_write_path_runs_the_daily_prune_check(storage) -> None:
    """The once-a-day prune hangs off the write path (no scheduler job)."""
    repo = storage.tool_calls
    check: Any = AsyncMock(return_value=0)
    repo.prune_if_due = check  # type: ignore[method-assign]

    await repo.upsert("t1", [_row("x", datetime.now(UTC))])

    check.assert_awaited()


async def test_prune_handles_migrated_rows_with_plain_timestamps(storage) -> None:
    """added: rows copied from tool_usage carry SQLite CURRENT_TIMESTAMP text
    ('YYYY-MM-DD HH:MM:SS', no T, no zone); they age out the same way."""
    old = (datetime.now(UTC) - timedelta(days=95)).strftime("%Y-%m-%d %H:%M:%S")
    recent = (datetime.now(UTC) - timedelta(days=5)).strftime("%Y-%m-%d %H:%M:%S")
    async with storage.db_manager.get_connection() as conn:
        for seq, (use_id, ts) in enumerate((("m-old", old), ("m-new", recent)), 1):
            await conn.execute(
                "INSERT INTO tool_calls (seq, tool_use_id, tool, status, started_at)"
                " VALUES (?, ?, 'Bash', 'ok', ?)",
                (seq, use_id, ts),
            )
        await conn.commit()

    assert await storage.tool_calls.prune(days=90) == 1
    assert await _ids(storage) == ["m-new"]
