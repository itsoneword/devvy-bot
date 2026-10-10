"""Codex thread bookkeeping on the sessions row (dv-ebde / dv-2a61)."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from src.agent_backends.codex_cli import InMemoryCodexSessionStore
from src.storage.codex_sessions import (
    CODEX_BACKEND,
    CodexSessionStore,
    import_codex_sessions_json,
)
from src.storage.database import DatabaseManager
from src.storage.schema.legacy import build_legacy
from src.storage.schema.migrate import head_revision


async def _add_user(db: DatabaseManager, user_id: int) -> None:
    async with db.get_connection() as conn:
        await conn.execute(
            "INSERT OR IGNORE INTO users (user_id, is_allowed) VALUES (?, TRUE)",
            (user_id,),
        )
        await conn.commit()


@pytest.fixture
async def db(tmp_path):
    manager = DatabaseManager(f"sqlite:///{tmp_path / 'bot.db'}")
    await manager.initialize()
    for uid in (7, 8, 99):
        await _add_user(manager, uid)
    yield manager
    await manager.close()


@pytest.fixture(params=["sqlite", "memory"])
async def store(request, db):
    if request.param == "sqlite":
        return CodexSessionStore(db)
    return InMemoryCodexSessionStore()


# --- store behaviour (both implementations) ---------------------------------


async def test_record_and_lookup_by_user_and_dir(store):
    await store.record(
        user_id=7,
        working_dir=Path("/home/me/proj"),
        thread_id="th-1",
        input_tokens=10,
        output_tokens=5,
    )
    assert await store.thread_for(7, Path("/home/me/proj")) == "th-1"
    assert await store.thread_for(7, Path("/other")) is None
    assert await store.thread_for(99, Path("/home/me/proj")) is None


async def test_record_updates_cumulative_tokens_and_totals(store):
    await store.record(7, Path("/p"), "th-1", 10, 5, thread_total_input=100)
    await store.record(7, Path("/p"), "th-1", 3, 2, thread_total_output=40)
    info = await store.info_for(7, Path("/p"))
    assert info["cumulative_tokens"] == 20
    assert info["session_id"] == "th-1"
    # A None total keeps the previous value.
    assert await store.thread_totals(7, Path("/p"), "th-1") == (100, 40)
    assert await store.thread_totals(7, Path("/p"), "th-other") == (0, 0)


async def test_newest_thread_is_the_head(store):
    await store.record(7, Path("/p"), "th-old", 1, 1)
    await store.record(7, Path("/p"), "th-new", 1, 1)
    assert await store.thread_for(7, Path("/p")) == "th-new"


async def test_clear_drops_the_head_only_for_that_scope(store):
    await store.record(7, Path("/p"), "th-1", 1, 1)
    await store.record(7, Path("/q"), "th-2", 1, 1)
    await store.clear(7, Path("/p"))
    assert await store.thread_for(7, Path("/p")) is None
    assert await store.thread_for(7, Path("/q")) == "th-2"
    await store.record(7, Path("/p"), "th-3", 1, 1)
    assert await store.thread_for(7, Path("/p")) == "th-3"


async def test_sessions_for_user_returns_all_dirs(store):
    await store.record(7, Path("/a"), "th-a", 1, 1)
    await store.record(7, Path("/b"), "th-b", 1, 1)
    await store.record(8, Path("/a"), "th-c", 1, 1)
    dirs = {Path(s["working_dir"]) for s in await store.sessions_for_user(7)}
    assert dirs == {Path("/a"), Path("/b")}


# --- sqlite specifics -------------------------------------------------------


async def test_persists_across_instances_and_keeps_the_row(db):
    await CodexSessionStore(db).record(7, Path("/p"), "th-1", 1, 1)
    assert await CodexSessionStore(db).thread_for(7, Path("/p")) == "th-1"
    await CodexSessionStore(db).clear(7, Path("/p"))
    async with db.get_connection() as conn:
        cur = await conn.execute(
            "SELECT is_active, thread_head, backend FROM sessions "
            "WHERE session_id = 'th-1'"
        )
        row = await cur.fetchone()
    # Only the pointer goes; the row stays active and intact (FK parent of
    # the thread's messages), exactly like the JSON pop left it.
    assert (bool(row[0]), row[1], row[2]) == (True, 0, CODEX_BACKEND)


async def test_head_ignores_is_active_like_the_json_did(db):
    """A Claude-side dir-cap retirement (is_active=FALSE) never touched the
    JSON pointer, so it must not break the Codex lookup either."""
    store = CodexSessionStore(db)
    await store.record(7, Path("/p"), "th-1", 1, 1)
    async with db.get_connection() as conn:
        await conn.execute(
            "UPDATE sessions SET is_active = FALSE, deactivated_reason = 'dir_cap:5'"
        )
        await conn.commit()
    assert await store.thread_for(7, Path("/p")) == "th-1"


async def test_record_moves_the_single_head(db):
    store = CodexSessionStore(db)
    await store.record(7, Path("/p"), "th-1", 1, 1)
    await store.record(7, Path("/p"), "th-2", 1, 1)
    await store.record(7, Path("/p"), "th-1", 1, 1)  # explicit resume of th-1
    assert await store.thread_for(7, Path("/p")) == "th-1"
    async with db.get_connection() as conn:
        cur = await conn.execute("SELECT COUNT(*) FROM sessions WHERE thread_head = 1")
        assert (await cur.fetchone())[0] == 1


async def test_record_tags_an_existing_claude_style_row(db):
    """_persist_session writes the row first; record() adds the Codex columns
    without touching the SessionManager-owned ones."""
    now = datetime.now(UTC)
    async with db.get_connection() as conn:
        await conn.execute(
            "INSERT INTO sessions (session_id, user_id, project_path, created_at, "
            "last_used, total_turns, message_count) VALUES (?, ?, ?, ?, ?, 3, 3)",
            ("th-1", 7, "/p", now, now),
        )
        await conn.commit()
    store = CodexSessionStore(db)
    await store.record(7, Path("/p"), "th-1", 4, 1, 40, 10)
    async with db.get_connection() as conn:
        cur = await conn.execute(
            "SELECT backend, message_count, cumulative_tokens, thread_total_input "
            "FROM sessions WHERE session_id = 'th-1'"
        )
        assert tuple(await cur.fetchone()) == (CODEX_BACKEND, 3, 5, 40)


async def test_claude_rows_are_never_a_codex_head(db):
    now = datetime.now(UTC)
    async with db.get_connection() as conn:
        await conn.execute(
            "INSERT INTO sessions (session_id, user_id, project_path, created_at, "
            "last_used) VALUES ('claude-1', 7, '/p', ?, ?)",
            (now, now),
        )
        await conn.commit()
    assert await CodexSessionStore(db).thread_for(7, Path("/p")) is None


# --- migration --------------------------------------------------------------


async def test_migration_22_is_additive_on_a_populated_v21_db(tmp_path):
    path = tmp_path / "live.db"
    build_legacy(path, "bot", upto=21)  # pre-Alembic DB at legacy v21
    old = DatabaseManager(f"sqlite:///{path}")  # pool only, no initialize
    await _add_user(old, 7)
    now = datetime.now(UTC)
    async with old.get_connection() as conn:
        await conn.execute(
            "INSERT INTO sessions (session_id, user_id, project_path, created_at, "
            "last_used, message_count) VALUES ('old-1', 7, '/p', ?, ?, 12)",
            (now, now),
        )
        await conn.commit()
    await old.close()

    new = DatabaseManager(f"sqlite:///{path}")  # bridges 22..26 + stamps
    await new.initialize()
    try:
        async with new.get_connection() as conn:
            cur = await conn.execute(
                "SELECT message_count, backend, thread_total_input, "
                "thread_total_output, cumulative_tokens FROM sessions "
                "WHERE session_id = 'old-1'"
            )
            assert tuple(await cur.fetchone()) == (12, None, 0, 0, 0)
            cur = await conn.execute("SELECT MAX(version) FROM schema_version")
            assert (await cur.fetchone())[0] == 26  # legacy chain completed
            cur = await conn.execute("SELECT version_num FROM alembic_version")
            assert (await cur.fetchone())[0] == head_revision("bot")
            cur = await conn.execute("SELECT COUNT(*) FROM pinned_messages")
            assert (await cur.fetchone())[0] == 0
        # Existing rows still load through the ORM model.
        from src.storage.repositories import SessionRepository

        row = await SessionRepository(new).get_session("old-1")
        assert row is not None and row.backend is None
    finally:
        await new.close()


# --- JSON import ------------------------------------------------------------


def _write_json(path: Path, entries: dict) -> Path:
    path.write_text(json.dumps(entries, indent=2))
    return path


def _entry(sid, uid, wd, cum=0, tin=0, tout=0):
    return {
        "session_id": sid,
        "user_id": uid,
        "working_dir": wd,
        "cumulative_tokens": cum,
        "thread_total_input": tin,
        "thread_total_output": tout,
        "updated_at": "2026-09-20T10:00:00+00:00",
    }


async def test_import_tags_existing_rows_and_inserts_missing(db, tmp_path):
    now = datetime.now(UTC)
    async with db.get_connection() as conn:
        # Row _persist_session already wrote for the live head (dv-0c01).
        await conn.execute(
            "INSERT INTO sessions (session_id, user_id, project_path, created_at, "
            "last_used) VALUES ('th-a', 7, '/a', ?, ?)",
            (now, now),
        )
        await conn.commit()
    src = _write_json(
        tmp_path / "codex_sessions.json",
        {
            "7::/a": _entry("th-a", 7, "/a", cum=500, tin=300, tout=20),
            "7::/b": _entry("th-b", 7, "/b", cum=90, tin=80, tout=5),
            "12345::/c": _entry("th-c", 12345, "/c"),  # no users row
            "junk": {"user_id": 7},
        },
    )
    before = src.read_bytes()

    counts = await import_codex_sessions_json(db, src)
    assert counts == {
        "tagged": 1,
        "inserted": 1,
        "already": 0,
        "skipped": 2,
        "moved": 0,
    }

    store = CodexSessionStore(db)
    assert await store.thread_for(7, Path("/a")) == "th-a"
    assert await store.thread_for(7, Path("/b")) == "th-b"
    assert await store.thread_totals(7, Path("/a"), "th-a") == (300, 20)
    assert (await store.info_for(7, Path("/b")))["cumulative_tokens"] == 90
    # Read-only: the file is untouched and still there.
    assert src.read_bytes() == before


async def test_import_is_idempotent_and_never_overwrites_newer_state(db, tmp_path):
    src = _write_json(
        tmp_path / "codex_sessions.json",
        {"7::/a": _entry("th-a", 7, "/a", cum=10, tin=10, tout=1)},
    )
    await import_codex_sessions_json(db, src)
    store = CodexSessionStore(db)
    # The live bot moves on: another turn on th-a, then a resume failure.
    await store.record(7, Path("/a"), "th-a", 5, 0, thread_total_input=15)
    await store.clear(7, Path("/a"))

    counts = await import_codex_sessions_json(db, src)  # next startup
    assert counts == {
        "tagged": 0,
        "inserted": 0,
        "already": 1,
        "skipped": 0,
        "moved": 0,
    }
    assert await store.thread_for(7, Path("/a")) is None  # not resurrected
    async with db.get_connection() as conn:
        cur = await conn.execute(
            "SELECT thread_total_input, cumulative_tokens FROM sessions "
            "WHERE session_id = 'th-a'"
        )
        assert tuple(await cur.fetchone()) == (15, 15)


async def test_import_missing_or_corrupt_file_is_a_noop(db, tmp_path):
    empty = {"tagged": 0, "inserted": 0, "already": 0, "skipped": 0, "moved": 0}
    assert await import_codex_sessions_json(db, tmp_path / "absent.json") == empty
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert await import_codex_sessions_json(db, bad) == empty
    assert bad.read_text() == "{not json"


# --- owner requirement: continuity survives the move ----------------------

OWNER = 10000001  # stands in for the owner id (the real one must not ship)
LIVE_SHAPED = {
    # Head whose sessions row exists (every thread since dv-0c01).
    f"{OWNER}::/home/u/Devvybot/devvy_bot": {
        "cumulative_tokens": 1_234_567,
        "session_id": "01a0c070-0000-7000-8000-000000000001",
        "working_dir": "/home/u/Devvybot/devvy_bot",
        "user_id": OWNER,
        "updated_at": "2026-09-25T18:01:02.123456+00:00",
        "thread_total_input": 987_654,
        "thread_total_output": 12_345,
    },
    # Pre-dv-0c01 thread: no sessions row, and a stale working_dir field --
    # the 2026-06 workspace migration rewrote the KEY only (live data).
    f"{OWNER}::/home/u/Devvybot/workspaces/channel": {
        "cumulative_tokens": 0,
        "session_id": "019e89fa-0000-7000-8000-000000000002",
        "working_dir": "/home/u/channel-workspace",
        "user_id": OWNER,
        "updated_at": "2026-06-02T20:16:29.055045+00:00",
    },
    f"{OWNER}::/home/u/Devvybot/workspaces/coach/users/{OWNER}": {
        "cumulative_tokens": 42,
        "session_id": "01a06926-0000-7000-8000-000000000003",
        "working_dir": f"/home/u/Devvybot/workspaces/coach/users/{OWNER}",
        "user_id": OWNER,
        "updated_at": "2026-09-20T09:00:00+00:00",
        "thread_total_input": 40,
        "thread_total_output": 2,
    },
}


def _json_era_thread_for(raw: dict, user_id: int, working_dir: Path):
    """Exactly what the removed JSON CodexSessionStore.thread_for did."""
    entry = raw.get(f"{user_id}::{Path(working_dir)}")
    return entry.get("session_id") if entry else None


async def test_live_shaped_json_keeps_every_thread_and_counter(db, tmp_path):
    await _add_user(db, OWNER)
    now = datetime.now(UTC)
    async with db.get_connection() as conn:
        for key in (
            f"{OWNER}::/home/u/Devvybot/devvy_bot",
            f"{OWNER}::/home/u/Devvybot/workspaces/coach/users/{OWNER}",
        ):
            e = LIVE_SHAPED[key]
            await conn.execute(
                "INSERT INTO sessions (session_id, user_id, project_path, "
                "created_at, last_used, message_count) VALUES (?, ?, ?, ?, ?, 9)",
                (e["session_id"], OWNER, e["working_dir"], now, now),
            )
        await conn.commit()
    src = _write_json(tmp_path / "codex_sessions.json", LIVE_SHAPED)
    before = src.read_bytes()

    first = await import_codex_sessions_json(db, src)
    assert (first["tagged"], first["inserted"], first["skipped"]) == (2, 1, 0)

    store = CodexSessionStore(db)
    for key, e in LIVE_SHAPED.items():
        uid, _, wd = key.partition("::")
        expected = _json_era_thread_for(LIVE_SHAPED, int(uid), Path(wd))
        assert expected == e["session_id"]
        assert await store.thread_for(int(uid), Path(wd)) == expected, key
        info = await store.info_for(int(uid), Path(wd))
        assert info["cumulative_tokens"] == e["cumulative_tokens"], key
        assert await store.thread_totals(int(uid), Path(wd), expected) == (
            e.get("thread_total_input", 0),
            e.get("thread_total_output", 0),
        ), key

    # The created row is invisible to the Claude facade's resume lookup
    # (active rows only), so Claude never tries to resume a Codex thread id.
    async with db.get_connection() as conn:
        cur = await conn.execute(
            "SELECT is_active FROM sessions WHERE session_id = ?",
            (
                LIVE_SHAPED[f"{OWNER}::/home/u/Devvybot/workspaces/channel"][
                    "session_id"
                ],
            ),
        )
        assert not (await cur.fetchone())[0]

    second = await import_codex_sessions_json(db, src)
    assert second == {
        "tagged": 0,
        "inserted": 0,
        "already": 3,
        "skipped": 0,
        "moved": 0,
    }
    for key, e in LIVE_SHAPED.items():
        uid, _, wd = key.partition("::")
        assert await store.thread_for(int(uid), Path(wd)) == e["session_id"]
    assert src.read_bytes() == before


async def test_import_never_steals_a_scope_from_a_moved_row(db, tmp_path):
    """A row whose project_path differs from the key's dir is tagged but not
    made a head: a head there would hijack another scope's lookup."""
    now = datetime.now(UTC)
    async with db.get_connection() as conn:
        await conn.execute(
            "INSERT INTO sessions (session_id, user_id, project_path, created_at, "
            "last_used) VALUES ('th-m', 7, '/elsewhere', ?, ?)",
            (now, now),
        )
        await conn.commit()
    src = _write_json(tmp_path / "c.json", {"7::/a": _entry("th-m", 7, "/a")})
    counts = await import_codex_sessions_json(db, src)
    assert (counts["tagged"], counts["moved"]) == (1, 1)
    store = CodexSessionStore(db)
    assert await store.thread_for(7, Path("/elsewhere")) is None
    assert await store.thread_for(7, Path("/a")) is None
