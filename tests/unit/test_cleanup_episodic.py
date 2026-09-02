"""Tests for scripts/cleanup_episodic.py plan()/apply() (dv-7517 step 7 follow-up).

No prior unit coverage existed for this script — prior sessions verified it
via manual dry-run against live data only. Covers close/flip/keep
classification and the --keep-full override (owner decision 2026-08-19:
exclude the recurring pizza recipe fact from the blanket >600-char flip).
"""

import sqlite3

import pytest

from scripts.cleanup_episodic import apply, plan
from src.storage.database import DatabaseManager
from src.storage.memory_repository import truncate_at_word

SCOPE = "coach#test"


@pytest.fixture
async def db_path(tmp_path):
    path = tmp_path / "mem.db"
    db = DatabaseManager(str(path))
    await db.initialize()
    await db.close()
    return str(path)


def _insert(conn, tier, content, label=None, kind="user_fact"):
    conn.execute(
        "INSERT INTO memory_facts (agent_id, tier, kind, label, content, "
        "status, valid_from) VALUES (?, ?, ?, ?, ?, 'active', datetime('now'))",
        (SCOPE, tier, kind, label, content),
    )
    conn.commit()
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


async def test_plan_classifies_close_flip_keep(db_path):
    conn = sqlite3.connect(db_path)
    episodic_id = _insert(
        conn, "general", "Съел завтрак 500 ккал 15.08, итого за день 1800 ккал"
    )
    oversized_id = _insert(conn, "general", "x" * 601)
    durable_id = _insert(conn, "general", "allergic to peanuts")
    pinned_id = _insert(conn, "pinned", "Съел завтрак 500 ккал 15.08")
    product_id = _insert(conn, "general", "[product-ref] oatmeal 55g")

    actions = plan(conn, SCOPE)

    assert [fid for fid, *_ in actions["close"]] == [episodic_id]
    assert [fid for fid, *_ in actions["flip"]] == [oversized_id]
    keep_ids = {fid for fid, *_ in actions["keep"]}
    assert keep_ids == {durable_id, pinned_id, product_id}


async def test_plan_ignores_non_user_fact_kind(db_path):
    conn = sqlite3.connect(db_path)
    _insert(conn, "general", "x" * 601, kind="conversation_result")

    actions = plan(conn, SCOPE)

    assert actions == {"close": [], "flip": [], "keep": []}


async def test_keep_full_excludes_id_from_flip(db_path):
    conn = sqlite3.connect(db_path)
    recipe_id = _insert(conn, "general", "pizza recipe: " + "x" * 601)
    other_oversized_id = _insert(conn, "general", "y" * 601)

    actions = plan(conn, SCOPE, keep_full={recipe_id})

    assert [fid for fid, *_ in actions["flip"]] == [other_oversized_id]
    keep_ids = {fid for fid, *_ in actions["keep"]}
    assert recipe_id in keep_ids
    reason = next(why for fid, _, why, _ in actions["keep"] if fid == recipe_id)
    assert reason == "--keep-full override"


async def test_apply_closes_and_flips_in_one_transaction(db_path):
    conn = sqlite3.connect(db_path)
    episodic_id = _insert(
        conn, "general", "Съел завтрак 500 ккал 15.08, итого за день 1800 ккал"
    )
    oversized_id = _insert(conn, "general", "x" * 601)

    actions = plan(conn, SCOPE)
    apply(conn, SCOPE, actions, marker="cleanup-episodic-test")

    row = conn.execute(
        "SELECT status, invalidated_by FROM memory_facts WHERE id=?", (episodic_id,)
    ).fetchone()
    assert row == ("superseded", "cleanup-episodic-test")

    row = conn.execute(
        "SELECT kind, label FROM memory_facts WHERE id=?", (oversized_id,)
    ).fetchone()
    assert row == ("conversation_result", truncate_at_word("x" * 601))


async def test_apply_raises_and_rolls_back_on_stale_row(db_path):
    conn = sqlite3.connect(db_path)
    episodic_id = _insert(
        conn, "general", "Съел завтрак 500 ккал 15.08, итого за день 1800 ккал"
    )
    actions = plan(conn, SCOPE)

    # Row is closed by someone else between plan() and apply().
    conn.execute(
        "UPDATE memory_facts SET status='superseded' WHERE id=?", (episodic_id,)
    )
    conn.commit()

    with pytest.raises(RuntimeError):
        apply(conn, SCOPE, actions, marker="cleanup-episodic-test")

    row = conn.execute(
        "SELECT status, invalidated_by FROM memory_facts WHERE id=?", (episodic_id,)
    ).fetchone()
    assert row == ("superseded", None)
