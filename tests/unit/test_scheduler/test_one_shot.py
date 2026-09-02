"""Tests for one-shot (@at:) scheduled jobs."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from src.scheduler.scheduler import JobScheduler
from src.storage.database import DatabaseManager


@pytest.fixture
async def db(tmp_path):
    db = DatabaseManager(str(tmp_path / "sched.db"))
    await db.initialize()
    yield db
    await db.close()


@pytest.fixture
async def scheduler(db):
    bus = AsyncMock()
    sched = JobScheduler(
        event_bus=bus,
        db_manager=db,
        default_working_directory=Path("/tmp"),
    )
    yield sched


async def test_requires_exactly_one_of_cron_or_run_at(scheduler):
    with pytest.raises(ValueError):
        await scheduler.add_job("j", None, "p")
    with pytest.raises(ValueError):
        await scheduler.add_job(
            "j", "0 7 * * *", "p", run_at=datetime.now(UTC) + timedelta(hours=1)
        )


async def test_one_shot_persisted_with_at_marker(scheduler, db):
    run_at = datetime.now(UTC) + timedelta(hours=1)
    job_id = await scheduler.add_job("remind", None, "stretch", run_at=run_at)

    jobs = await scheduler.list_jobs()
    assert len(jobs) == 1
    assert jobs[0]["job_id"] == job_id
    assert jobs[0]["cron_expression"] == f"@at:{run_at.isoformat()}"


async def test_one_shot_deactivated_after_firing(scheduler):
    run_at = datetime.now(UTC) + timedelta(hours=1)
    job_id = await scheduler.add_job("remind", None, "stretch", run_at=run_at)

    await scheduler._fire_event(
        job_name="remind",
        prompt="stretch",
        working_directory="/tmp",
        target_chat_ids=[],
        skill_name=None,
        job_db_id=job_id,
        one_shot=True,
    )

    assert await scheduler.list_jobs() == []
    scheduler.event_bus.publish.assert_awaited_once()


async def test_expired_one_shot_dropped_on_load(scheduler, db):
    past = datetime.now(UTC) - timedelta(hours=1)
    async with db.get_connection() as conn:
        await conn.execute(
            "INSERT INTO scheduled_jobs (job_id, job_name, cron_expression, "
            "prompt, working_directory) VALUES (?, ?, ?, ?, ?)",
            ("old", "expired", f"@at:{past.isoformat()}", "p", "/tmp"),
        )
        await conn.commit()

    await scheduler._load_jobs_from_db()

    assert await scheduler.list_jobs() == []


async def test_cron_job_still_loads(scheduler, db):
    await scheduler.add_job("daily", "0 7 * * *", "check in")
    jobs = await scheduler.list_jobs()
    assert jobs[0]["cron_expression"] == "0 7 * * *"


async def test_delivery_mode_persisted_and_fired(scheduler, db):
    run_at = datetime.now(UTC) + timedelta(hours=1)
    job_id = await scheduler.add_job(
        "remind", None, "Как плечо?", run_at=run_at, delivery="message"
    )

    jobs = await scheduler.list_jobs()
    assert jobs[0]["delivery"] == "message"

    aps_job = scheduler._scheduler.get_job(job_id)
    assert aps_job.kwargs["delivery"] == "message"

    await scheduler._fire_event(
        job_name="remind",
        prompt="Как плечо?",
        working_directory="/tmp",
        target_chat_ids=[],
        skill_name=None,
        job_db_id=job_id,
        one_shot=True,
        delivery="message",
    )
    event = scheduler.event_bus.publish.await_args.args[0]
    assert event.delivery == "message"


async def test_delivery_defaults_to_agent(scheduler, db):
    await scheduler.add_job("daily", "0 7 * * *", "check in")
    jobs = await scheduler.list_jobs()
    assert jobs[0]["delivery"] == "agent"

    await scheduler._load_jobs_from_db()
    aps_job = scheduler._scheduler.get_job(jobs[0]["job_id"])
    assert aps_job.kwargs["delivery"] == "agent"


async def test_invalid_delivery_rejected(scheduler):
    with pytest.raises(ValueError):
        await scheduler.add_job(
            "j",
            None,
            "p",
            run_at=datetime.now(UTC) + timedelta(hours=1),
            delivery="carrier-pigeon",
        )


async def test_created_by_persisted_and_fired(scheduler, db):
    """dv-3e91: created_by must survive a live fire, not just the DB row —
    ScheduledEvent had no field for it at all, so handle_scheduled could
    never resolve a job's real owner even when the caller passed it."""
    run_at = datetime.now(UTC) + timedelta(hours=1)
    job_id = await scheduler.add_job(
        "remind", None, "stretch", run_at=run_at, created_by=222222222
    )

    jobs = await scheduler.list_jobs()
    assert jobs[0]["created_by"] == 222222222

    aps_job = scheduler._scheduler.get_job(job_id)
    assert aps_job.kwargs["created_by"] == 222222222

    await scheduler._fire_event(
        job_name="remind",
        prompt="stretch",
        working_directory="/tmp",
        target_chat_ids=[],
        skill_name=None,
        job_db_id=job_id,
        one_shot=True,
        created_by=222222222,
    )
    event = scheduler.event_bus.publish.await_args.args[0]
    assert event.created_by == 222222222


async def test_created_by_defaults_to_zero(scheduler, db):
    await scheduler.add_job("daily", "0 7 * * *", "check in")
    jobs = await scheduler.list_jobs()
    assert jobs[0]["created_by"] == 0


async def test_created_by_survives_reload_from_db(scheduler, db):
    """A restart must re-register jobs with created_by intact, not just a
    fresh add_job call — _load_jobs_from_db is a second, independent path
    into the same APScheduler kwargs."""
    job_id = await scheduler.add_job(
        "daily", "0 7 * * *", "check in", created_by=222222222
    )

    await scheduler._load_jobs_from_db()
    aps_job = scheduler._scheduler.get_job(job_id)
    assert aps_job.kwargs["created_by"] == 222222222
