"""Crontab day-of-week numbering must be translated for APScheduler.

crontab: 0=Sun..6=Sat (7=Sun). APScheduler: 0=Mon..6=Sun.
`CronTrigger.from_crontab` passes digits through untranslated, so every
numeric-DOW job fired a day late until `build_cron_trigger` was introduced.
"""

from datetime import UTC, datetime, timedelta

import pytest

from src.scheduler.scheduler import _translate_dow_field, build_cron_trigger


def _next_fires(expression: str, count: int = 3) -> list[datetime]:
    trigger = build_cron_trigger(expression)
    out: list[datetime] = []
    cursor = datetime(2026, 8, 10, 0, 0, tzinfo=UTC)  # a Monday
    for _ in range(count):
        fire = trigger.get_next_fire_time(None, cursor)
        out.append(fire)
        cursor = fire + timedelta(seconds=1)
    return out


@pytest.mark.parametrize(
    "expression,weekday",
    [
        ("0 19 * * 0", "Sunday"),
        ("0 19 * * 1", "Monday"),
        ("0 19 * * 4", "Thursday"),  # the friday-weighin-reminder regression
        ("0 19 * * 6", "Saturday"),
        ("0 19 * * 7", "Sunday"),
        ("0 19 * * thu", "Thursday"),  # names already correct, must not shift
    ],
)
def test_numeric_dow_matches_crontab_semantics(expression: str, weekday: str) -> None:
    for fire in _next_fires(expression):
        assert fire.strftime("%A") == weekday
        assert (fire.hour, fire.minute) == (19, 0)


def test_dow_list() -> None:
    days = {f.strftime("%A") for f in _next_fires("0 17 * * 2,5", count=4)}
    assert days == {"Tuesday", "Friday"}


def test_dow_range() -> None:
    days = {f.strftime("%A") for f in _next_fires("0 9 * * 1-5", count=10)}
    assert days == {"Monday", "Tuesday", "Wednesday", "Thursday", "Friday"}


def test_dow_range_wrapping_week_boundary() -> None:
    # crontab 5-1 = Fri, Sat, Sun, Mon
    days = {f.strftime("%A") for f in _next_fires("0 9 * * 5-1", count=8)}
    assert days == {"Friday", "Saturday", "Sunday", "Monday"}


def test_wildcard_and_other_fields_untouched() -> None:
    assert _translate_dow_field("*") == "*"
    trigger = build_cron_trigger("30 6 1 * *")
    fire = trigger.get_next_fire_time(None, datetime(2026, 8, 10, tzinfo=UTC))
    assert (fire.day, fire.hour, fire.minute) == (1, 6, 30)


def test_step_translation() -> None:
    # crontab 1/2 = Mon, Wed, Fri -> APScheduler 0/2
    assert _translate_dow_field("1/2") == "0/2"
