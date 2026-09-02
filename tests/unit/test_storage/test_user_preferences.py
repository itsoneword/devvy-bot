import pytest

from src.storage.database import DatabaseManager
from src.storage.repositories import UserPreferencesRepository


@pytest.fixture
async def db(tmp_path):
    mgr = DatabaseManager(str(tmp_path / "test.db"))
    await mgr.initialize()
    return mgr


async def test_user_preferences_table_exists(db):
    async with db.get_connection() as conn:
        cursor = await conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='user_preferences'"
        )
        row = await cursor.fetchone()
    assert row is not None


@pytest.fixture
async def prefs(db):
    return UserPreferencesRepository(db)


async def test_get_unknown_user_returns_none(prefs):
    assert await prefs.get(999) is None


async def test_set_then_get_roundtrip(prefs):
    await prefs.set(
        1, active_profile="channel", agent_backend="claude", agent_model="claude"
    )
    got = await prefs.get(1)
    assert got == {
        "active_profile": "channel",
        "agent_backend": "claude",
        "agent_model": "claude",
        "pinned_switch_message_id": None,
    }


async def test_partial_update_does_not_clobber(prefs):
    await prefs.set(1, agent_backend="openai_codex", agent_model="openai-codex/gpt-5.5")
    await prefs.set(1, active_profile="general")  # only profile this time
    got = await prefs.get(1)
    assert got == {
        "active_profile": "general",
        "agent_backend": "openai_codex",
        "agent_model": "openai-codex/gpt-5.5",
        "pinned_switch_message_id": None,
    }


async def test_pinned_switch_message_id_roundtrip(prefs):
    await prefs.set(1, active_profile="devops", pinned_switch_message_id=42)
    got = await prefs.get(1)
    assert got["pinned_switch_message_id"] == 42
    # Switching again updates just the pinned id, leaving the rest untouched.
    await prefs.set(1, pinned_switch_message_id=99)
    got = await prefs.get(1)
    assert got["active_profile"] == "devops"
    assert got["pinned_switch_message_id"] == 99


async def test_set_with_no_fields_is_noop(prefs):
    await prefs.set(1, active_profile="devops")
    await prefs.set(1)  # nothing to update
    got = await prefs.get(1)
    assert got["active_profile"] == "devops"


async def test_facade_exposes_preferences(tmp_path):
    from src.storage.facade import Storage

    store = Storage(str(tmp_path / "facade.db"))
    await store.db_manager.initialize()
    await store.preferences.set(5, active_profile="channel")
    assert (await store.preferences.get(5))["active_profile"] == "channel"
