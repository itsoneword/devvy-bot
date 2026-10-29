"""The shared usage-limit record (dv-7ad9 phase 2).

Any job that ends on a usage limit records it (`quota.record(limit,
source)`); everyone that is about to start a model job asks
`quota.active(now)`. The record lives at `DataLayout.model_quota` (under the
data root, `<root>/workspaces/devops/model_quota.json`), so the tests point
`DEVVY_DATA_ROOT` at a tmp dir. It expires by itself: past `until` it is no
longer active, nobody has to delete it.
"""

from datetime import UTC, datetime, timedelta

import pytest

LIMIT = "You've hit your weekly limit · resets 9am (UTC)"
T0 = datetime(2026, 10, 26, 8, 15, tzinfo=UTC)
RESET = datetime(2026, 10, 26, 9, 0, tzinfo=UTC)


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setenv("DEVVY_DATA_ROOT", str(tmp_path))
    return tmp_path


def test_job_ending_on_the_limit_is_recorded_until_reset_and_expires_by_itself(
    root,
) -> None:
    """Scenario: any job ending on the limit records it until the reset; the
    record expires by itself."""
    from src.model_router import quota
    from src.utils.paths import data_layout

    limit = quota.parse_limit(LIMIT, T0)
    assert limit is not None
    quota.record(limit, "bgjob:merge-devvy")

    path = data_layout().model_quota
    assert str(path).startswith(str(root)), path
    assert path.exists()

    for now in (T0, RESET - timedelta(seconds=1)):
        got = quota.active(now)
        assert got is not None, now
        assert got.until == RESET
        assert "weekly limit" in got.message

    # Past the reset nobody has to clear it: it is simply no longer active.
    assert quota.active(RESET) is None
    assert quota.active(RESET + timedelta(minutes=5)) is None


def test_no_record_means_no_limit(root) -> None:
    """added: a fresh data root (no record file) is not exhausted."""
    from src.model_router import quota
    from src.utils.paths import data_layout

    assert not data_layout().model_quota.exists()
    assert quota.active(T0) is None


def test_later_hit_moves_until_and_an_unreadable_record_is_no_limit(root) -> None:
    """added: a second hit with a later reset replaces the record (until moves
    out); a corrupt record file never blocks jobs."""
    from src.model_router import quota
    from src.utils.paths import data_layout

    first = quota.parse_limit(LIMIT, T0)
    later = quota.parse_limit("You've hit your weekly limit · resets 2pm (UTC)", T0)
    assert first is not None and later is not None
    quota.record(first, "pump-dv-1")
    quota.record(later, "bgjob:x")
    got = quota.active(T0)
    assert got is not None
    assert got.until == datetime(2026, 10, 26, 14, 0, tzinfo=UTC)

    data_layout().model_quota.write_text("{not json")
    assert quota.active(T0) is None
