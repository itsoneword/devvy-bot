import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.bot.orchestration.formatting import format_usage_table
from src.profiles.registry import ProfileDefinition, ProfileRegistry
from src.storage.database import DatabaseManager
from src.token.tracker import TokenTracker


@pytest.fixture
async def db():
    manager = DatabaseManager(tempfile.mktemp(suffix=".db"))
    await manager.initialize()
    return manager


async def _seed(db, user_id, session_id, project_path, turns):
    """Insert a sessions row + N session_tracking rows at given timestamps.

    turns: list of (timestamp_str, input_tokens, output_tokens, cost).
    """
    async with db.get_connection() as conn:
        await conn.execute(
            "INSERT OR IGNORE INTO users (user_id, telegram_username) VALUES (?, ?)",
            (user_id, f"user{user_id}"),
        )
        await conn.execute(
            "INSERT OR IGNORE INTO sessions (session_id, user_id, project_path) "
            "VALUES (?, ?, ?)",
            (session_id, user_id, project_path),
        )
        for ts, inp, out, cost in turns:
            await conn.execute(
                "INSERT INTO session_tracking "
                "(session_id, user_id, input_tokens, output_tokens, "
                "cumulative_tokens, cost, timestamp) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (session_id, user_id, inp, out, inp + out, cost, ts),
            )
        await conn.commit()


async def test_get_usage_by_project_aggregates_and_filters_by_since(db):
    tracker = TokenTracker(db)
    now = datetime.now(UTC)
    fmt = lambda dt: dt.strftime("%Y-%m-%d %H:%M:%S")  # noqa: E731

    await _seed(
        db,
        user_id=1,
        session_id="s-coach",
        project_path="/ws/coach",
        turns=[
            (fmt(now - timedelta(hours=1)), 10, 100, 0.05),  # within 24h
            (fmt(now - timedelta(days=10)), 20, 200, 0.10),  # within 30d, not 24h
            (fmt(now - timedelta(days=40)), 30, 300, 0.15),  # older than 30d
        ],
    )
    await _seed(
        db,
        user_id=1,
        session_id="s-chef",
        project_path="/ws/chef",
        turns=[(fmt(now - timedelta(hours=2)), 5, 50, 0.02)],
    )
    # Different user must never leak in.
    await _seed(
        db,
        user_id=2,
        session_id="s-other-user",
        project_path="/ws/coach",
        turns=[(fmt(now - timedelta(hours=1)), 999, 999, 9.99)],
    )

    all_time = await tracker.get_usage_by_project(1)
    # 660 == sum of user 1's coach turns only; if user 2's 1998-token turn
    # leaked in (wrong WHERE user_id filter), this would be 2658 instead.
    assert all_time["/ws/coach"]["tokens"] == 10 + 100 + 20 + 200 + 30 + 300 == 660
    assert all_time["/ws/coach"]["turns"] == 3
    assert all_time["/ws/chef"]["tokens"] == 55

    last_24h = await tracker.get_usage_by_project(1, since=now - timedelta(hours=24))
    assert last_24h["/ws/coach"]["tokens"] == 110  # only the 1h-ago turn
    assert "/ws/chef" in last_24h

    last_30d = await tracker.get_usage_by_project(1, since=now - timedelta(days=30))
    assert last_30d["/ws/coach"]["tokens"] == 110 + 220  # 1h-ago + 10d-ago, not 40d


async def test_get_usage_by_project_empty_for_unknown_user(db):
    tracker = TokenTracker(db)
    result = await tracker.get_usage_by_project(999)
    assert result == {}


def _registry():
    return ProfileRegistry(
        [
            ProfileDefinition(
                "devops",
                "DevOps",
                Path("/ws/devops"),
                Path("/ws/devops/m"),
                Path("/ws/devops/p.md"),
                default=True,
            ),
            ProfileDefinition(
                "coach",
                "Coach",
                Path("/ws/coach"),
                Path("/ws/coach/m"),
                Path("/ws/coach/p.md"),
            ),
        ]
    )


def test_format_usage_table_renders_known_and_missing_profiles():
    reg = _registry()
    usage_24h = {"/ws/coach": {"tokens": 110, "cost": 0.05, "turns": 1}}
    usage_30d = {
        "/ws/coach": {"tokens": 330, "cost": 0.15, "turns": 2},
        "/ws/devops": {"tokens": 5000, "cost": 1.23, "turns": 10},
    }
    text = format_usage_table(reg, usage_24h, usage_30d)
    assert text.startswith("<b>Usage by profile:</b>")
    assert "<pre>" in text and "</pre>" in text
    assert "coach" in text and "devops" in text
    assert "110" in text  # 24h tokens for coach
    assert "330" in text  # 30d tokens for coach
    assert "5000" in text  # 30d tokens for devops (no 24h activity)


def test_format_usage_table_zero_fills_profiles_with_no_activity():
    reg = _registry()
    text = format_usage_table(reg, {}, {})
    assert "coach" in text and "devops" in text
    assert "0.00" in text  # cost columns zero-filled, not crashing on missing keys
