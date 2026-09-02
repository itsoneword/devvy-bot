"""Tests for the pending_notes one-shot injection queue."""

import pytest

from src.storage.database import DatabaseManager
from src.storage.pending_notes import PendingNoteRepository


@pytest.fixture
async def db(tmp_path):
    db = DatabaseManager(str(tmp_path / "notes.db"))
    await db.initialize()
    yield db
    await db.close()


@pytest.fixture
def repo(db):
    return PendingNoteRepository(db)


async def test_consume_returns_and_deletes(repo):
    await repo.add(42, "/ws/coach/users/42", "note one")
    await repo.add(42, "/ws/coach/users/42", "note two")

    notes = await repo.consume(42, "/ws/coach/users/42")
    assert notes == ["note one", "note two"]

    # Consumed exactly once.
    assert await repo.consume(42, "/ws/coach/users/42") == []


async def test_scoped_by_workspace_and_user(repo):
    await repo.add(42, "/ws/coach/users/42", "coach note")

    # A chef turn and another user must not see it.
    assert await repo.consume(42, "/ws/chef/users/42") == []
    assert await repo.consume(7, "/ws/coach/users/42") == []
    assert await repo.consume(42, "/ws/coach/users/42") == ["coach note"]


async def test_empty_scope_is_noop(repo):
    assert await repo.consume(42, "/nowhere") == []
