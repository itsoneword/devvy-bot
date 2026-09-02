"""Tests for the shared agent task board."""

from datetime import UTC, datetime, timedelta

import pytest

from src.storage.database import DatabaseManager
from src.storage.task_repository import TaskRepository


@pytest.fixture
async def repo(tmp_path):
    db = DatabaseManager(str(tmp_path / "tasks.db"))
    await db.initialize()
    yield TaskRepository(db)
    await db.close()


async def test_add_and_list_open(repo):
    task_id = await repo.add_task("Buy bands", created_by="coach")
    tasks = await repo.list_tasks()
    assert [t.id for t in tasks] == [task_id]
    assert tasks[0].title == "Buy bands"
    assert tasks[0].created_by == "coach"
    assert tasks[0].status == "open"


async def test_done_removes_from_open_list(repo):
    task_id = await repo.add_task("Stretch")
    assert await repo.set_status(task_id, "done")
    assert await repo.list_tasks(status="open") == []
    done = await repo.list_tasks(status="done")
    assert [t.id for t in done] == [task_id]


async def test_set_status_unknown_id_returns_false(repo):
    assert not await repo.set_status(999, "done")


async def test_due_tasks_sort_before_undated(repo):
    late = await repo.add_task("no due date")
    soon = await repo.add_task(
        "due soon", due_at=datetime.now(UTC) + timedelta(hours=1)
    )
    tasks = await repo.list_tasks()
    assert [t.id for t in tasks] == [soon, late]
    assert tasks[0].due_at is not None


async def test_list_all_statuses(repo):
    a = await repo.add_task("a")
    b = await repo.add_task("b")
    await repo.set_status(a, "done")
    all_tasks = await repo.list_tasks(status=None)
    assert {t.id for t in all_tasks} == {a, b}
