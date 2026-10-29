"""More of the usage-limit classifier (dv-7ad9 phase 1): the CLI's other
reset spellings, Codex's wording, and which stderr lines count."""

from datetime import UTC, datetime, timedelta

import pytest

from src.model_router.quota import find_limit, parse_limit, quota_line

NOW = datetime(2026, 10, 26, 8, 15, tzinfo=UTC)


@pytest.mark.parametrize(
    "text, until",
    [
        (
            "You've hit your session limit · resets 2:30pm",
            datetime(2026, 10, 26, 14, 30),
        ),
        (
            "You've hit your weekly limit · resets 7am (UTC)",
            datetime(2026, 10, 27, 7, 0),
        ),
        # Moscow is UTC+3: 9am there is 06:00 UTC on the 27th.
        (
            "You've hit your Opus weekly limit · resets Oct 27, 9am (Europe/Moscow)",
            datetime(2026, 10, 27, 6, 0),
        ),
        (
            "You've hit your session limit · resets in 2h 30m",
            datetime(2026, 10, 26, 10, 45),
        ),
        (
            "You've hit your monthly spend limit · resets 12am (UTC)",
            datetime(2026, 10, 27, 0, 0),
        ),
        (
            "You've hit your usage limit. Upgrade to Pro or try again at 3:15 PM.",
            datetime(2026, 10, 26, 15, 15),
        ),
        # Unreadable / unknown zone -> UTC; no time at all -> +1h.
        (
            "You've hit your weekly limit · resets 9am (Mars/Base)",
            datetime(2026, 10, 26, 9, 0),
        ),
        ("You've hit your weekly limit · resets soon", datetime(2026, 10, 26, 9, 15)),
    ],
)
def test_reset_spellings(text: str, until: datetime) -> None:
    limit = parse_limit(text, NOW)
    assert limit is not None
    assert limit.until == until.replace(tzinfo=UTC)


def test_absurd_reset_falls_back_to_one_hour() -> None:
    limit = parse_limit("You've hit your weekly limit · resets in 40 days", NOW)
    assert limit is not None and limit.until == NOW + timedelta(hours=1)


def test_kind_is_read() -> None:
    limit = parse_limit("You've hit your team's shared budget · resets 9am (UTC)", NOW)
    assert limit is not None and limit.kind == "team's shared budget"


def test_find_limit_reads_only_agentjob_lines() -> None:
    raw = "You've hit your weekly limit · resets 9am (UTC)"
    # Quoted in some other output: not a hit.
    assert find_limit(f"grep: {raw}\nsomething else\n", NOW) is None
    legacy = find_limit(f"noise\nagentjob failed: {raw}\n", NOW)
    assert legacy is not None and legacy.until == datetime(2026, 10, 26, 9, tzinfo=UTC)
    # The quota line wins and carries its own until (no re-parse at reap time).
    line = quota_line(legacy)
    later = NOW + timedelta(hours=2)
    got = find_limit(f"agentjob failed: {raw}\n{line}\n", later)
    assert got is not None and got.until == legacy.until
