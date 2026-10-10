"""Alembic schema runner (dv-5e4f Phase 1): fresh build, legacy bridge,
idempotency, locking, backups, post-baseline revisions, FTS trigger guard."""

from __future__ import annotations

import shutil
import sqlite3
import threading
from pathlib import Path
from typing import Dict, List

import pytest
from alembic import command

from src.storage.schema import migrate
from src.storage.schema.legacy import LEGACY_HEAD, build_legacy, schema_version
from src.storage.schema.migrate import (
    BASELINE,
    DB_NAMES,
    SchemaMismatchError,
    alembic_config,
    ensure_schema,
    head_revision,
)
from src.storage.schema.structure import diff, snapshot

# FTS5 sync triggers per DB. A batch_alter_table on a base table rebuilds it
# and silently drops these; any revision that does so must recreate them.
EXPECTED_TRIGGERS = {
    "bot": {
        "memory_facts_ad",
        "memory_facts_ai",
        "memory_facts_au",
        "messages_fts_ad",
        "messages_fts_ai",
        "messages_fts_au",
    },
    "channel_intel": {
        "places_ad",
        "places_ai",
        "places_au",
        "place_images_ad",
        "place_images_ai",
        "place_images_au",
        "place_references_ad",
        "place_references_ai",
        "place_references_au",
    },
}


def _triggers(path: Path) -> set:
    conn = sqlite3.connect(path)
    try:
        return {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")
        }
    finally:
        conn.close()


def _alembic_rev(path: Path) -> List[str]:
    conn = sqlite3.connect(path)
    try:
        return [r[0] for r in conn.execute("SELECT version_num FROM alembic_version")]
    finally:
        conn.close()


def _backups(path: Path) -> List[str]:
    d = path.parent / "backups"
    return sorted(p.name for p in d.iterdir()) if d.exists() else []


def _row_counts(path: Path) -> Dict[str, int]:
    conn = sqlite3.connect(path)
    try:
        names = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' AND name NOT LIKE '%_fts%'"
            )
        ]
        return {
            n: conn.execute(f'SELECT COUNT(*) FROM "{n}"').fetchone()[0] for n in names
        }
    finally:
        conn.close()


# --- fresh ------------------------------------------------------------------


@pytest.mark.parametrize("name", DB_NAMES)
def test_fresh_alembic_equals_legacy_replay(tmp_path, name):
    """Fresh install and a bridged pre-Alembic install end up identical, and
    the baseline itself equals the legacy replay (so stamping is sound)."""
    fresh, old = tmp_path / "fresh" / f"{name}.db", tmp_path / "old" / f"{name}.db"
    old.parent.mkdir()
    assert ensure_schema(name, fresh) == "created"
    build_legacy(old, name)
    assert diff(migrate.baseline_snapshot(name), snapshot(old), "baseline") == []
    assert ensure_schema(name, old) == "bridged"
    assert diff(snapshot(fresh), snapshot(old), "fresh", "bridged") == []
    assert _alembic_rev(fresh) == _alembic_rev(old) == [head_revision(name)]


@pytest.mark.parametrize("name", DB_NAMES)
def test_second_run_is_a_noop_without_backup(tmp_path, name):
    path = tmp_path / f"{name}.db"
    ensure_schema(name, path)
    before = snapshot(path)
    assert ensure_schema(name, path) == "current"
    assert snapshot(path) == before
    assert _backups(path) == []


@pytest.mark.parametrize("name", DB_NAMES)
def test_alembic_check_is_clean(tmp_path, name):
    """Models == baseline revisions: autogenerate finds nothing to do."""
    path = tmp_path / f"{name}.db"
    ensure_schema(name, path)
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        conn.execute("BEGIN")
        migrate._run_alembic(conn, name, command.check)  # raises on any diff
        conn.execute("ROLLBACK")
    finally:
        conn.close()


@pytest.mark.parametrize("name", ["bot", "channel_intel"])
def test_fts_trigger_guard(tmp_path, name):
    """Trigger-count guard: head must keep every FTS sync trigger (6 + 9 =
    15). A future batch revision that rebuilds an FTS-backed table without
    recreating its triggers fails here."""
    path = tmp_path / f"{name}.db"
    ensure_schema(name, path)
    assert _triggers(path) == EXPECTED_TRIGGERS[name]


def test_trigger_guard_total_is_15():
    assert sum(len(v) for v in EXPECTED_TRIGGERS.values()) == 15


# --- legacy bridge ----------------------------------------------------------


def test_bridge_from_bot_v20_keeps_rows_and_stamps(tmp_path):
    path = tmp_path / "bot.db"
    build_legacy(path, "bot", upto=20)
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO users (user_id, telegram_username) VALUES (1, 'a')")
    conn.execute(
        "INSERT INTO sessions (session_id, user_id, project_path) VALUES ('s', 1, '/p')"
    )
    conn.execute(
        "INSERT INTO messages (session_id, user_id, prompt, response) "
        "VALUES ('s', 1, 'findable prompt', 'r')"
    )
    conn.commit()
    conn.close()
    counts = _row_counts(path)

    assert ensure_schema("bot", path) == "bridged"

    assert _alembic_rev(path) == [head_revision("bot")]
    conn = sqlite3.connect(path)
    assert schema_version(conn) == LEGACY_HEAD["bot"]  # frozen, old code no-ops
    hit = conn.execute(
        "SELECT COUNT(*) FROM messages_fts WHERE messages_fts MATCH 'findable'"
    ).fetchone()[0]
    conn.close()
    assert hit == 1
    after = _row_counts(path)
    assert {k: after[k] for k in counts if k != "schema_version"} == {
        k: v for k, v in counts.items() if k != "schema_version"
    }
    assert diff(snapshot(path), migrate.baseline_snapshot("bot")) == []
    assert len(_backups(path)) == 1 and _backups(path)[0].startswith("bot.")
    assert ensure_schema("bot", path) == "current"
    assert len(_backups(path)) == 1


@pytest.mark.parametrize(
    "name,upto", [("bot", 25), ("bot", 1), ("data", 1), ("data", 4)]
)
def test_bridge_from_any_versioned_legacy(tmp_path, name, upto):
    path = tmp_path / f"{name}.db"
    build_legacy(path, name, upto=upto)
    assert ensure_schema(name, path) == "bridged"
    assert _alembic_rev(path) == [head_revision(name)]


def test_bridge_llm_analytics_adds_sniffed_columns(tmp_path):
    """An old llm_analytics.db (quality_daily before dv-37e9) is caught up by
    the legacy column sniff, then stamped."""
    path = tmp_path / "llm_analytics.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE quality_daily (date TEXT NOT NULL, profile TEXT, "
        "user_id INTEGER, pinned_facts INTEGER NOT NULL DEFAULT 0, "
        "log_calls INTEGER NOT NULL DEFAULT 0, claimed_not_logged INTEGER NOT "
        "NULL DEFAULT 0, complaint_hits INTEGER NOT NULL DEFAULT 0, "
        "user_messages INTEGER NOT NULL DEFAULT 0)"
    )
    conn.execute("INSERT INTO quality_daily (date) VALUES ('2026-01-01')")
    conn.commit()
    conn.close()
    assert ensure_schema("llm_analytics", path) == "bridged"
    conn = sqlite3.connect(path)
    row = conn.execute(
        "SELECT date, recipe_turns, recipe_turns_unsearched FROM quality_daily"
    ).fetchone()
    conn.close()
    assert row == ("2026-01-01", 0, 0)


def test_bridge_channel_intel(tmp_path):
    path = tmp_path / "channel_intel.db"
    build_legacy(path, "channel_intel")
    assert ensure_schema("channel_intel", path) == "bridged"
    assert _triggers(path) == EXPECTED_TRIGGERS["channel_intel"]


def test_drifted_legacy_db_is_refused_and_left_untouched(tmp_path):
    """Verification gate: a legacy DB that doesn't match the baseline after
    replay is rolled back whole (no stamp, no partial legacy steps)."""
    path = tmp_path / "data.db"
    build_legacy(path, "data", upto=3)
    conn = sqlite3.connect(path)
    conn.execute("ALTER TABLE products ADD COLUMN local_hack TEXT")
    conn.commit()
    conn.close()
    before = snapshot(path)
    with pytest.raises(SchemaMismatchError, match="local_hack"):
        ensure_schema("data", path)
    assert snapshot(path) == before  # v4 not applied, no alembic_version
    assert len(_backups(path)) == 1  # the pre-attempt backup is kept


def test_versioned_db_without_any_version_table_is_refused(tmp_path):
    path = tmp_path / "bot.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE users (user_id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()
    with pytest.raises(SchemaMismatchError):
        ensure_schema("bot", path)


# --- concurrency + backups --------------------------------------------------


@pytest.mark.parametrize("legacy", [False, True])
def test_two_concurrent_starts_migrate_once(tmp_path, legacy):
    path = tmp_path / "bot.db"
    if legacy:
        build_legacy(path, "bot", upto=25)
    results: List[str] = []
    errors: List[BaseException] = []
    barrier = threading.Barrier(2)

    def run() -> None:
        try:
            barrier.wait()
            results.append(ensure_schema("bot", path))
        except BaseException as e:  # pragma: no cover - reported below
            errors.append(e)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert sorted(results) == sorted(["bridged" if legacy else "created", "current"])
    assert len(_backups(path)) == (1 if legacy else 0)


def test_backups_keep_last_three(tmp_path):
    path = tmp_path / "bot.db"
    build_legacy(path, "bot")
    for i in range(5):
        migrate.backup(path, "bot", f"r{i}")
    names = _backups(path)
    assert len(names) == 3
    assert [n.rsplit(".pre-", 1)[1] for n in names] == ["r2.db", "r3.db", "r4.db"]
    # The copy is a complete, readable DB.
    conn = sqlite3.connect(path.parent / "backups" / names[-1])
    assert schema_version(conn) == LEGACY_HEAD["bot"]
    conn.close()


# --- post-baseline revision (throwaway, test-only) ---------------------------

_ADD_COLUMN_REV = """
from alembic import op
import sqlalchemy as sa

revision = "9001"
down_revision = "{down}"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("messages", sa.Column("test_flag", sa.INTEGER(), nullable=True))


def downgrade():
    op.drop_column("messages", "test_flag")
"""

_BATCH_REBUILD_REV = """
from alembic import op
import sqlalchemy as sa

revision = "9001"
down_revision = "{down}"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("{table}", recreate="always") as b:
        b.add_column(sa.Column("test_flag", sa.INTEGER(), nullable=True))


def downgrade():
    pass
"""


@pytest.fixture
def scratch_migrations(tmp_path, monkeypatch):
    """A copy of migrations/ the test can add a throwaway revision to."""
    root = tmp_path / "migrations"
    shutil.copytree(
        migrate.MIGRATIONS_DIR, root, ignore=shutil.ignore_patterns("__pycache__")
    )
    monkeypatch.setattr(migrate, "MIGRATIONS_DIR", root)

    def add(template: str, **kw: str) -> None:
        (root / "bot" / "versions" / "9001_test.py").write_text(
            template.format(down=BASELINE["bot"], **kw)
        )

    return add


def _columns(path: Path, table: str) -> List[str]:
    conn = sqlite3.connect(path)
    try:
        return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
    finally:
        conn.close()


def test_new_revision_applies_to_stamped_and_fresh_db(tmp_path, scratch_migrations):
    stamped = tmp_path / "stamped" / "bot.db"
    stamped.parent.mkdir()
    build_legacy(stamped, "bot", upto=25)
    assert ensure_schema("bot", stamped) == "bridged"
    conn = sqlite3.connect(stamped)
    conn.execute("INSERT INTO users (user_id) VALUES (1)")
    conn.commit()
    conn.close()

    scratch_migrations(_ADD_COLUMN_REV)
    assert head_revision("bot") == "9001"

    assert ensure_schema("bot", stamped) == "upgraded"
    assert _alembic_rev(stamped) == ["9001"]
    assert _columns(stamped, "messages")[-1] == "test_flag"
    assert _row_counts(stamped)["users"] == 1
    assert _triggers(stamped) == EXPECTED_TRIGGERS["bot"]
    assert [b for b in _backups(stamped) if b.endswith(".pre-9001.db")]

    fresh = tmp_path / "fresh" / "bot.db"
    assert ensure_schema("bot", fresh) == "created"
    assert diff(snapshot(fresh), snapshot(stamped)) == []

    # Rollback procedure (docs/database-migrations.md): downgrade one step.
    conn = sqlite3.connect(stamped, isolation_level=None)
    conn.execute("BEGIN IMMEDIATE")
    migrate._run_alembic(conn, "bot", command.downgrade, BASELINE["bot"])
    conn.execute("COMMIT")
    conn.close()
    assert "test_flag" not in _columns(stamped, "messages")
    assert _alembic_rev(stamped) == [BASELINE["bot"]]
    assert diff(snapshot(stamped), migrate.baseline_snapshot("bot")) == []


def test_batch_rebuild_of_fts_table_drops_triggers(tmp_path, scratch_migrations):
    """Why the trigger guard exists: render_as_batch's copy-and-swap on an
    FTS-backed table (memory_facts) silently loses its sync triggers unless
    the revision recreates them."""
    scratch_migrations(_BATCH_REBUILD_REV, table="memory_facts")
    path = tmp_path / "bot.db"
    ensure_schema("bot", path)
    assert "test_flag" in _columns(path, "memory_facts")
    assert not {"memory_facts_ai", "memory_facts_ad", "memory_facts_au"} & _triggers(
        path
    )


def test_batch_rebuild_of_view_referenced_table_fails_and_rolls_back(
    tmp_path, scratch_migrations
):
    """messages/users/sessions back the daily_stats/user_stats views: SQLite
    refuses the batch rename while a view references the table, so such a
    revision must drop + recreate the views (and messages' FTS triggers).
    The whole upgrade rolls back -- the DB stays at the old head."""
    path = tmp_path / "bot.db"
    ensure_schema("bot", path)
    before = snapshot(path)
    scratch_migrations(_BATCH_REBUILD_REV, table="messages")
    with pytest.raises(Exception, match="daily_stats"):
        ensure_schema("bot", path)
    assert snapshot(path) == before
    assert _alembic_rev(path) == [BASELINE["bot"]]


def test_alembic_config_rejects_unknown_db():
    with pytest.raises(ValueError):
        alembic_config("nope")


# --- scripts/schema_diff.py ---------------------------------------------------


def test_schema_diff_cli(tmp_path, capsys):
    from scripts.schema_diff import main

    assert main(["fresh", "legacy", "--db", "data"]) == 0
    drifted = tmp_path / "data.db"
    build_legacy(drifted, "data")
    conn = sqlite3.connect(drifted)
    conn.execute("ALTER TABLE products ADD COLUMN local_hack TEXT")
    conn.commit()
    conn.close()
    assert main([str(drifted), "fresh", "--db", "data"]) == 1
    assert "local_hack" in capsys.readouterr().out
    assert main(["--dir", str(tmp_path)]) == 1  # data differs; others missing
