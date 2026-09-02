"""Tests for session_tracking and memory_extractions tables and models."""

import json
from datetime import UTC, datetime

import aiosqlite
import pytest

from src.storage.database import DatabaseManager
from src.storage.models import MemoryExtractionModel, SessionTrackingModel


@pytest.fixture
async def db(tmp_path):
    """Initialize a temp database with full migrations."""
    db_path = tmp_path / "test.db"
    manager = DatabaseManager(str(db_path))
    await manager.initialize()
    # Seed user and session for FK constraints
    async with manager.get_connection() as conn:
        await conn.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (42,))
        await conn.execute(
            "INSERT OR IGNORE INTO sessions "
            "(session_id, user_id, project_path) VALUES (?, 42, '/tmp')",
            ("sess-1",),
        )
        await conn.commit()
    return manager


class TestTablesCreated:
    async def test_session_tracking_table_exists(self, db: DatabaseManager) -> None:
        async with db.get_connection() as conn:
            cursor = await conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='session_tracking'"
            )
            row = await cursor.fetchone()
            assert row is not None

    async def test_memory_extractions_table_exists(self, db: DatabaseManager) -> None:
        async with db.get_connection() as conn:
            cursor = await conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='memory_extractions'"
            )
            row = await cursor.fetchone()
            assert row is not None


class TestSessionTrackingModel:
    def test_to_dict(self) -> None:
        ts = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        model = SessionTrackingModel(
            session_id="sess-1",
            user_id=42,
            input_tokens=100,
            output_tokens=200,
            cumulative_tokens=300,
            cost=0.005,
            timestamp=ts,
            id=1,
        )
        d = model.to_dict()
        assert d["session_id"] == "sess-1"
        assert d["input_tokens"] == 100
        assert d["cumulative_tokens"] == 300
        assert d["timestamp"] == ts.isoformat()

    async def test_from_row(self, db: DatabaseManager) -> None:
        async with db.get_connection() as conn:
            await conn.execute(
                "INSERT INTO session_tracking "
                "(session_id, user_id, input_tokens, output_tokens, "
                "cumulative_tokens, cost) VALUES (?, ?, ?, ?, ?, ?)",
                ("sess-1", 42, 100, 200, 300, 0.005),
            )
            await conn.commit()

            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(
                "SELECT * FROM session_tracking WHERE session_id = ?",
                ("sess-1",),
            )
            row = await cursor.fetchone()

        model = SessionTrackingModel.from_row(row)
        assert model.session_id == "sess-1"
        assert model.input_tokens == 100
        assert model.output_tokens == 200
        assert model.cumulative_tokens == 300


class TestMemoryExtractionModel:
    def test_to_dict_with_json_fields(self) -> None:
        ts = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        model = MemoryExtractionModel(
            session_id="sess-1",
            user_id=42,
            prompt_hash="abc123",
            validation_status="complete",
            parsed_insights={"key": "value"},
            target_files={"files": ["a.md"]},
            timestamp=ts,
            id=1,
        )
        d = model.to_dict()
        assert d["session_id"] == "sess-1"
        assert d["parsed_insights"] == json.dumps({"key": "value"})
        assert d["target_files"] == json.dumps({"files": ["a.md"]})
        assert d["timestamp"] == ts.isoformat()

    async def test_from_row_with_json(self, db: DatabaseManager) -> None:
        insights = json.dumps({"topics": ["auth", "db"]})
        files = json.dumps({"files": ["focus.md"]})
        async with db.get_connection() as conn:
            await conn.execute(
                "INSERT INTO memory_extractions "
                "(session_id, user_id, prompt_hash, validation_status, "
                "parsed_insights, target_files, cumulative_tokens_at_run) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("sess-1", 42, "hash1", "complete", insights, files, 50000),
            )
            await conn.commit()

            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(
                "SELECT * FROM memory_extractions WHERE session_id = ?",
                ("sess-1",),
            )
            row = await cursor.fetchone()

        model = MemoryExtractionModel.from_row(row)
        assert model.session_id == "sess-1"
        assert model.parsed_insights == {"topics": ["auth", "db"]}
        assert model.target_files == {"files": ["focus.md"]}
        assert model.cumulative_tokens_at_run == 50000

    def test_to_dict_none_json_fields(self) -> None:
        model = MemoryExtractionModel(
            session_id="sess-1",
            user_id=42,
            prompt_hash="abc",
        )
        d = model.to_dict()
        assert d["parsed_insights"] is None
        assert d["target_files"] is None
