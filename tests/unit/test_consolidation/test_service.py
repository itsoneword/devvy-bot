"""Tests for the consolidation pass (dv-4c6f): report-only, owner-approved."""

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from src.consolidation.service import ConsolidationService
from src.storage.database import DatabaseManager
from src.storage.deviz_board import DevizBoard
from src.storage.memory_repository import MemoryRepository


@pytest.fixture
async def db(tmp_path):
    db = DatabaseManager(str(tmp_path / "mem.db"))
    await db.initialize()
    yield db
    await db.close()


@pytest.fixture
async def repo(db):
    return MemoryRepository(db)


@pytest.fixture
def board(tmp_path):
    return DevizBoard(tmp_path / "tasks")


@pytest.fixture
def service(repo, db, tmp_path, board):
    return ConsolidationService(
        memory_repo=repo,
        db=db,
        report_dir=tmp_path / "consolidation",
        board=board,
    )


async def test_run_reports_lexical_duplicates_without_writing(service, repo, db):
    a = await repo.add_fact("chef", "general", "likes spicy food")
    b = await repo.add_fact("chef", "general", "likes spicy foods!")
    await repo.add_fact("chef", "general", "allergic to walnuts")

    with patch(
        "src.consolidation.service.complete_text",
        AsyncMock(return_value="[]"),
    ):
        path = await service.run("chef")

    text = path.read_text()
    assert f"MERGE scope=chef survivor={b} close={a}" in text
    # Zero writes: all three facts still active, untouched.
    assert len(await repo.get_facts("chef")) == 3


async def test_run_includes_semantic_groups_from_model(service, repo):
    a = await repo.add_fact("chef", "general", "любит острое")
    b = await repo.add_fact("chef", "general", "likes spicy food")

    with patch(
        "src.consolidation.service.complete_text",
        AsyncMock(return_value=f"[[{a},{b}]]"),
    ):
        path = await service.run("chef")

    assert f"survivor={b} close={a}" in path.read_text()


async def test_semantic_failure_still_produces_report(service, repo):
    await repo.add_fact("chef", "general", "fact one entirely")
    await repo.add_fact("chef", "general", "different fact two")

    with patch(
        "src.consolidation.service.complete_text",
        AsyncMock(side_effect=RuntimeError("CLI down")),
    ):
        path = await service.run("chef")

    assert "No merge candidates" in path.read_text()


async def test_expiry_candidates_by_age(service, repo, db):
    fid = await repo.add_fact("chef", "general", "long dead fact")
    await repo.invalidate_fact(fid, "chef")
    old = (datetime.now(UTC) - timedelta(days=90)).isoformat()
    async with db.get_connection() as conn:
        await conn.execute(
            "UPDATE memory_facts SET invalid_at=? WHERE id=?", (old, fid)
        )
        await conn.commit()

    with patch(
        "src.consolidation.service.complete_text",
        AsyncMock(return_value="[]"),
    ):
        path = await service.run("chef")

    assert f"EXPIRE scope=chef id={fid}" in path.read_text()


async def test_recently_invalidated_not_expiry_candidate(service, repo):
    fid = await repo.add_fact("chef", "general", "freshly closed")
    await repo.invalidate_fact(fid, "chef")

    with patch(
        "src.consolidation.service.complete_text",
        AsyncMock(return_value="[]"),
    ):
        path = await service.run("chef")

    assert "EXPIRE" not in path.read_text()


async def test_request_run_debounces_and_records_board_task(service, board, repo):
    await repo.add_fact("devops", "general", "some fact")
    with patch(
        "src.consolidation.service.complete_text",
        AsyncMock(return_value="[]"),
    ):
        service.request_run("devops", "snapshot over budget", snapshot_chars=13000)
        service.request_run("devops", "snapshot over budget", snapshot_chars=13000)
        # Let the fire-and-forget task complete.
        await asyncio.sleep(0.2)

    tasks = await board.list_tasks(status="open")
    # Debounce: two requests, one board task.
    assert len(tasks) == 1
    assert tasks[0].title == "Consolidation needed: devops"
    assert tasks[0].created_by == "consolidation"
    assert tasks[0].assignee == "user"
    assert tasks[0].status == "todo"


async def test_repeat_trip_updates_the_open_task_instead_of_filing_another(
    service, board, repo
):
    """dv-1327: the debounce is 24h and in-memory, so a scope that stays over
    budget trips daily. Eight duplicate rows on the old board came from this."""
    await repo.add_fact("devops", "general", "some fact")
    with patch(
        "src.consolidation.service.complete_text",
        AsyncMock(return_value="[]"),
    ):
        service.request_run("devops", "over budget", snapshot_chars=13000)
        await asyncio.sleep(0.2)
        # Simulate the next day: debounce expired, still over budget.
        service._last_run.clear()
        service.request_run("devops", "over budget", snapshot_chars=14000)
        await asyncio.sleep(0.2)

    tasks = await board.list_tasks(status="open")
    assert len(tasks) == 1
    body = (board.tasks_dir / f"{tasks[0].id}.md").read_text()
    assert "snapshot_chars: 14000" in body
    assert "still over budget" in body


async def test_run_notifies_owner(repo, db, tmp_path):
    notify = AsyncMock()
    service = ConsolidationService(
        memory_repo=repo,
        db=db,
        notify_callback=notify,
        report_dir=tmp_path / "c",
    )
    await repo.add_fact("chef", "general", "likes spicy food")
    await repo.add_fact("chef", "general", "likes spicy foods!")

    with patch(
        "src.consolidation.service.complete_text",
        AsyncMock(return_value="[]"),
    ):
        await service.run("chef")

    notify.assert_awaited_once()
    assert "1 merge group(s)" in notify.await_args.args[0]
