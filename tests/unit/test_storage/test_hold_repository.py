"""Tests for user_holds storage (dv-8bb5)."""

from datetime import UTC, datetime, timedelta

import pytest

from src.storage.database import DatabaseManager
from src.storage.hold_repository import HoldStore, UserHoldRepository

USER_ID = 111
OTHER_ID = 222
ADMIN_ID = 999


@pytest.fixture
async def db(tmp_path):
    manager = DatabaseManager(str(tmp_path / "holds.db"))
    await manager.initialize()
    yield manager
    await manager.close()


@pytest.fixture
def repo(db):
    return UserHoldRepository(db)


@pytest.fixture
async def store(repo):
    s = HoldStore(repo)
    await s.load()
    return s


class TestUserHoldRepository:
    async def test_get_missing_hold_is_none(self, repo):
        assert await repo.get_hold(USER_ID) is None

    async def test_upsert_then_get(self, repo):
        until = datetime.now(UTC) + timedelta(minutes=10)
        await repo.upsert_hold(USER_ID, until, "watchdog", ADMIN_ID)
        hold = await repo.get_hold(USER_ID)
        assert hold.user_id == USER_ID
        assert hold.reason == "watchdog"
        assert hold.set_by == ADMIN_ID
        assert hold.set_at is not None

    async def test_upsert_overwrites_existing_hold(self, repo):
        first_until = datetime.now(UTC) + timedelta(minutes=10)
        await repo.upsert_hold(USER_ID, first_until, "watchdog", ADMIN_ID)
        second_until = datetime.now(UTC) + timedelta(days=1)
        await repo.upsert_hold(USER_ID, second_until, "watchdog", ADMIN_ID)

        hold = await repo.get_hold(USER_ID)
        # SQLite round-trips TIMESTAMP at second precision.
        assert abs((hold.held_until - second_until).total_seconds()) < 1

    async def test_delete_hold(self, repo):
        until = datetime.now(UTC) + timedelta(minutes=10)
        await repo.upsert_hold(USER_ID, until, None, ADMIN_ID)
        assert await repo.delete_hold(USER_ID) is True
        assert await repo.get_hold(USER_ID) is None

    async def test_delete_missing_hold_returns_false(self, repo):
        assert await repo.delete_hold(USER_ID) is False

    async def test_list_holds(self, repo):
        until = datetime.now(UTC) + timedelta(minutes=10)
        await repo.upsert_hold(USER_ID, until, None, ADMIN_ID)
        await repo.upsert_hold(OTHER_ID, until, None, ADMIN_ID)
        holds = await repo.list_holds()
        assert set(holds.keys()) == {USER_ID, OTHER_ID}


class TestHoldStore:
    async def test_no_hold_returns_none(self, store):
        assert store.held_until(USER_ID) is None

    async def test_set_hold_is_visible_immediately(self, store):
        until = await store.set_hold(
            USER_ID, timedelta(minutes=10), reason="watchdog", set_by=ADMIN_ID
        )
        assert store.held_until(USER_ID) == until

    async def test_set_hold_persists_to_db(self, store, repo):
        await store.set_hold(USER_ID, timedelta(minutes=10), set_by=ADMIN_ID)
        persisted = await repo.get_hold(USER_ID)
        assert persisted is not None

    async def test_expired_hold_reads_as_not_held(self, store):
        await store.set_hold(USER_ID, timedelta(seconds=-1), set_by=ADMIN_ID)
        assert store.held_until(USER_ID) is None

    async def test_clear_hold(self, store):
        await store.set_hold(USER_ID, timedelta(minutes=10), set_by=ADMIN_ID)
        assert store.held_until(USER_ID) is not None
        await store.clear_hold(USER_ID)
        assert store.held_until(USER_ID) is None

    async def test_load_seeds_snapshot_from_db(self, repo):
        until = datetime.now(UTC) + timedelta(minutes=10)
        await repo.upsert_hold(USER_ID, until, None, ADMIN_ID)

        store = HoldStore(repo)
        assert store.held_until(USER_ID) is None  # not loaded yet
        await store.load()
        assert store.held_until(USER_ID) is not None

    async def test_users_tracked_independently(self, store):
        await store.set_hold(USER_ID, timedelta(minutes=10), set_by=ADMIN_ID)
        assert store.held_until(USER_ID) is not None
        assert store.held_until(OTHER_ID) is None
