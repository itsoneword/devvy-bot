"""Usage-limit classifier (dv-7ad9 phase 1): `parse_limit(text, now)` turns
the CLI's limit message into a `Limit` with an aware UTC `until`; anything
that is not a limit message is None."""

from datetime import UTC, datetime, timedelta

LIMIT = "You've hit your weekly limit · resets 9am (UTC)"


def test_reset_clock_time_is_the_next_occurrence_and_other_errors_are_none() -> None:
    """added: 9am (UTC) at 08:15 is today 09:00, at 09:30 tomorrow 09:00; a
    reset-less limit is now + 1h; a non-limit error is no limit; the stderr
    quota line round-trips; the exit code is 4."""
    from src.model_router.quota import (
        QUOTA_EXIT,
        parse_limit,
        parse_quota_line,
        quota_line,
    )

    early = datetime(2026, 10, 26, 8, 15, tzinfo=UTC)
    late = datetime(2026, 10, 26, 9, 30, tzinfo=UTC)

    a = parse_limit(LIMIT, early)
    assert a is not None and a.until == datetime(2026, 10, 26, 9, 0, tzinfo=UTC)
    b = parse_limit(LIMIT, late)
    assert b is not None and b.until == datetime(2026, 10, 27, 9, 0, tzinfo=UTC)
    c = parse_limit("You've hit your weekly limit", early)
    assert c is not None and c.until == early + timedelta(hours=1)

    assert parse_limit("API overloaded", early) is None
    assert parse_limit("error_during_execution", early) is None

    back = parse_quota_line(quota_line(a))
    assert back is not None and back.until == a.until
    assert QUOTA_EXIT == 4
