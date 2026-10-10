"""Tests for the /schedules command's authorization (dv-8c19).

Before this, `/schedules` ran an unfiltered `SELECT * FROM scheduled_jobs`
and showed every job to any caller — same bug class as the unfiltered
`schedule_list` agent tool, just a second, unguarded path onto the same
table. It must apply the identical rule (`job_visible_to`).
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.bot.orchestrator import MessageOrchestrator
from src.config import create_test_config
from src.storage.database import DatabaseManager


@pytest.fixture
async def db(tmp_path):
    db = DatabaseManager(str(tmp_path / "sched.db"))
    await db.initialize()
    yield db
    await db.close()


async def _insert_job(
    db: DatabaseManager,
    job_id: str,
    created_by: int,
    target_chat_ids: str,
    name: str = "job",
) -> None:
    async with db.get_connection() as conn:
        await conn.execute(
            "INSERT INTO scheduled_jobs (job_id, job_name, cron_expression, "
            "prompt, target_chat_ids, working_directory, created_by, "
            "is_active) VALUES (?, ?, '0 9 * * *', 'p', ?, '/tmp', ?, 1)",
            (job_id, name, target_chat_ids, created_by),
        )
        await conn.commit()


class FakeStorage:
    def __init__(self, db_manager: DatabaseManager) -> None:
        self.db_manager = db_manager

    async def log_bot_event(self, *args, **kwargs) -> None:  # noqa: ANN002
        pass


def _update(user_id: int, chat_id: int):  # noqa: ANN201
    update = MagicMock()
    update.effective_user.id = user_id
    update.effective_chat.id = chat_id
    update.message = MagicMock()
    update.message.reply_text = AsyncMock()
    return update


def _context(storage, role: str = "guest"):  # noqa: ANN201
    context = MagicMock()
    context.bot_data = {"storage": storage, "profile_registry": None}
    context.user_data = {"role": role}
    return context


def _orchestrator(tmp_path):  # noqa: ANN201
    settings = create_test_config(
        approved_directory=str(tmp_path), agentic_mode=True, enable_scheduler=True
    )
    deps = {
        "claude_integration": MagicMock(),
        "storage": MagicMock(),
        "security_validator": MagicMock(),
        "rate_limiter": MagicMock(),
        "audit_logger": MagicMock(),
    }
    return MessageOrchestrator(settings, deps)


class TestSchedulesCommandAuthorization:
    async def test_lists_only_own_and_targeted_jobs(self, db, tmp_path) -> None:
        await _insert_job(db, "mine", created_by=111, target_chat_ids="111")
        await _insert_job(db, "theirs", created_by=222, target_chat_ids="222")
        await _insert_job(db, "for-me", created_by=222, target_chat_ids="111,222")
        storage = FakeStorage(db)
        orchestrator = _orchestrator(tmp_path)
        update = _update(user_id=111, chat_id=111)
        context = _context(storage, role="guest")

        await orchestrator.agentic_schedules(update, context)

        text = update.message.reply_text.call_args[0][0]
        assert "mine" in text
        assert "for-me" in text
        assert "theirs" not in text

    async def test_owner_sees_legacy_rows_not_other_owners_jobs(
        self, db, tmp_path
    ) -> None:
        await _insert_job(db, "mine", created_by=111, target_chat_ids="111")
        await _insert_job(db, "theirs", created_by=222, target_chat_ids="222")
        await _insert_job(db, "legacy", created_by=0, target_chat_ids="")
        storage = FakeStorage(db)
        orchestrator = _orchestrator(tmp_path)
        update = _update(user_id=111, chat_id=111)
        context = _context(storage, role="owner")

        await orchestrator.agentic_schedules(update, context)

        text = update.message.reply_text.call_args[0][0]
        assert "mine" in text
        assert "legacy" in text
        assert "theirs" not in text

    async def test_no_visible_jobs_reports_empty(self, db, tmp_path) -> None:
        await _insert_job(db, "theirs", created_by=222, target_chat_ids="222")
        storage = FakeStorage(db)
        orchestrator = _orchestrator(tmp_path)
        update = _update(user_id=111, chat_id=111)
        context = _context(storage, role="guest")

        await orchestrator.agentic_schedules(update, context)

        text = update.message.reply_text.call_args[0][0]
        assert text == "No scheduled jobs."
