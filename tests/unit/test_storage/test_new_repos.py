"""dv-5e4f Phase 0: one direct test per new repository method.

These exercise src/storage/{scheduler,token,extraction,webhook,lifecycle}_
repository.py directly, independent of the callers (scheduler.py,
token/tracker.py, evaluator/service.py, api/server.py,
observability/lifecycle.py) that already cover most of this behavior
indirectly. webhook_events dedup in particular had NO prior test coverage
at all before this move.
"""

from pathlib import Path

import pytest

from src.storage.database import DatabaseManager
from src.storage.extraction_repository import MemoryExtractionRepository
from src.storage.lifecycle_repository import LifecycleRepository
from src.storage.scheduler_repository import SchedulerRepository
from src.storage.token_repository import TokenTrackingRepository
from src.storage.webhook_repository import WebhookEventRepository


@pytest.fixture
async def db(tmp_path: Path):
    manager = DatabaseManager(str(tmp_path / "bot.db"))
    await manager.initialize()
    yield manager
    await manager.close()


class TestSchedulerRepository:
    async def test_save_list_deactivate_roundtrip(self, db):
        repo = SchedulerRepository(db)
        await repo.save_job(
            job_id="job-1",
            job_name="digest",
            cron_expression="0 9 * * *",
            prompt="send digest",
            target_chat_ids_csv="111,222",
            working_directory="/work",
            skill_name=None,
            created_by=1,
            delivery="agent",
            item_id=None,
            timezone=None,
            ends_at_iso=None,
        )
        jobs = await repo.list_active_jobs()
        assert len(jobs) == 1
        assert jobs[0]["job_id"] == "job-1"
        assert jobs[0]["target_chat_ids"] == "111,222"

        await repo.deactivate_job("job-1")
        assert await repo.list_active_jobs() == []

    async def test_active_job_ids_for_item(self, db):
        repo = SchedulerRepository(db)
        await repo.save_job(
            job_id="job-2",
            job_name="reminder",
            cron_expression="0 10 * * *",
            prompt="ping",
            target_chat_ids_csv="",
            working_directory="/work",
            skill_name=None,
            created_by=1,
            delivery="message",
            item_id=42,
            timezone=None,
            ends_at_iso=None,
        )
        assert await repo.active_job_ids_for_item(42) == ["job-2"]
        assert await repo.active_job_ids_for_item(99) == []


class TestTokenTrackingRepository:
    async def test_record_and_totals(self, db):
        async with db.get_connection() as conn:
            await conn.execute("INSERT INTO users (user_id) VALUES (7)")
            await conn.execute(
                "INSERT INTO sessions (session_id, user_id, project_path) "
                "VALUES ('s1', 7, '/proj')"
            )
            await conn.commit()

        repo = TokenTrackingRepository(db)
        await repo.record_turn("s1", 7, 10, 5, 15, 0.001)
        await repo.record_turn("s1", 7, 20, 10, 45, 0.002)

        totals = await repo.get_session_totals("s1")
        assert totals == {"input_tokens": 30, "output_tokens": 15, "total_tokens": 45}
        assert await repo.get_cumulative_for_session("s1") == 45
        assert await repo.get_cumulative_for_session("unknown") == 0

        usage = await repo.get_usage_by_project(7)
        assert usage["/proj"]["turns"] == 2
        assert usage["/proj"]["tokens"] == 45


class TestMemoryExtractionRepository:
    async def test_record_already_extracted_and_last(self, db):
        async with db.get_connection() as conn:
            await conn.execute("INSERT INTO users (user_id) VALUES (8)")
            await conn.commit()

        repo = MemoryExtractionRepository(db)
        assert await repo.already_extracted(8, "hash-a") is False

        await repo.record_extraction(
            session_id="s2",
            user_id=8,
            prompt_hash="hash-a",
            raw_response="raw",
            parsed_insights_json=None,
            validation_status="success",
            target_files_json=None,
            error_message=None,
            cumulative_tokens_at_run=100,
            op_stats_json=None,
        )
        assert await repo.already_extracted(8, "hash-a") is True

        last = await repo.get_last_extraction("s2")
        assert last is not None
        assert last.validation_status == "success"
        assert await repo.get_last_extraction("no-such-session") is None


class TestWebhookEventRepository:
    async def test_dedup_on_delivery_id(self, db):
        repo = WebhookEventRepository(db)
        first = await repo.record_if_new(
            event_id="e1",
            provider="github",
            event_type="push",
            delivery_id="delivery-1",
            payload={"a": 1},
        )
        assert first is True

        second = await repo.record_if_new(
            event_id="e2",
            provider="github",
            event_type="push",
            delivery_id="delivery-1",
            payload={"a": 2},
        )
        assert second is False


class TestLifecycleRepository:
    async def test_ensure_schema_record_recent(self, db):
        repo = LifecycleRepository(db)
        await repo.ensure_schema()  # already created by migration v26; idempotent
        await repo.record("devops", "resume", {"session": "abc"})
        await repo.record("devops", "memory_loaded", {"chars": 4200})
        await repo.record("channel", "fresh", {})

        recent = await repo.recent("devops", limit=10)
        events = [r["event"] for r in recent]
        assert "resume" in events and "memory_loaded" in events
        assert all(r["profile"] == "devops" for r in recent)
