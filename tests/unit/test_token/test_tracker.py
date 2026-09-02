"""Tests for TokenTracker service."""

import pytest

from src.storage.database import DatabaseManager
from src.token.tracker import TokenTracker


async def _seed_user_and_sessions(db: DatabaseManager, session_ids: list) -> None:
    """Insert prerequisite user and session rows for FK constraints."""
    async with db.get_connection() as conn:
        await conn.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (42,))
        for sid in session_ids:
            await conn.execute(
                "INSERT OR IGNORE INTO sessions "
                "(session_id, user_id, project_path) VALUES (?, 42, '/tmp')",
                (sid,),
            )
        await conn.commit()


@pytest.fixture
async def tracker(tmp_path):
    """Create a TokenTracker with an in-memory-like temp SQLite database."""
    db_path = tmp_path / "test.db"
    db = DatabaseManager(str(db_path))
    await db.initialize()
    # Seed common test sessions
    await _seed_user_and_sessions(
        db, ["sess-1", "sess-2", "sess-3", "sess-4", "sess-5"]
    )
    return TokenTracker(db)


class TestRecordTurn:
    async def test_inserts_row(self, tracker: TokenTracker) -> None:
        await tracker.record_turn(
            session_id="sess-1",
            user_id=42,
            input_tokens=100,
            output_tokens=200,
            cumulative_tokens=300,
            cost=0.005,
        )
        totals = await tracker.get_session_totals("sess-1")
        assert totals["input_tokens"] == 100
        assert totals["output_tokens"] == 200
        assert totals["total_tokens"] == 300

    async def test_multiple_turns_accumulate(self, tracker: TokenTracker) -> None:
        await tracker.record_turn("sess-1", 42, 100, 200, 300, 0.005)
        await tracker.record_turn("sess-1", 42, 150, 250, 700, 0.008)

        totals = await tracker.get_session_totals("sess-1")
        assert totals["input_tokens"] == 250
        assert totals["output_tokens"] == 450
        assert totals["total_tokens"] == 700


class TestGetSessionTotals:
    async def test_empty_session(self, tracker: TokenTracker) -> None:
        totals = await tracker.get_session_totals("nonexistent")
        assert totals == {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}

    async def test_sums_correctly(self, tracker: TokenTracker) -> None:
        await tracker.record_turn("sess-2", 42, 50, 75, 125, 0.001)
        await tracker.record_turn("sess-2", 42, 60, 80, 265, 0.002)
        await tracker.record_turn("sess-2", 42, 70, 90, 425, 0.003)

        totals = await tracker.get_session_totals("sess-2")
        assert totals["input_tokens"] == 180
        assert totals["output_tokens"] == 245
        assert totals["total_tokens"] == 425


class TestGetCumulativeForSession:
    async def test_returns_latest(self, tracker: TokenTracker) -> None:
        await tracker.record_turn("sess-3", 42, 100, 200, 300, 0.005)
        await tracker.record_turn("sess-3", 42, 150, 250, 700, 0.008)

        cumulative = await tracker.get_cumulative_for_session("sess-3")
        assert cumulative == 700

    async def test_empty_session_returns_zero(self, tracker: TokenTracker) -> None:
        cumulative = await tracker.get_cumulative_for_session("nonexistent")
        assert cumulative == 0


class TestGetLastExtraction:
    async def test_no_extractions_returns_none(self, tracker: TokenTracker) -> None:
        result = await tracker.get_last_extraction("sess-4")
        assert result is None

    async def test_returns_latest_extraction(self, tracker: TokenTracker) -> None:
        # Insert directly into memory_extractions
        async with tracker._db.get_connection() as conn:
            await conn.execute(
                "INSERT INTO memory_extractions "
                "(session_id, user_id, prompt_hash, validation_status, "
                "cumulative_tokens_at_run) VALUES (?, ?, ?, ?, ?)",
                ("sess-5", 42, "hash1", "complete", 50000),
            )
            await conn.execute(
                "INSERT INTO memory_extractions "
                "(session_id, user_id, prompt_hash, validation_status, "
                "cumulative_tokens_at_run) VALUES (?, ?, ?, ?, ?)",
                ("sess-5", 42, "hash2", "pending", 100000),
            )
            await conn.commit()

        result = await tracker.get_last_extraction("sess-5")
        assert result is not None
        assert result.prompt_hash == "hash2"
        assert result.cumulative_tokens_at_run == 100000
