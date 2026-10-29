"""quota.py's Claude usage snapshot helpers (dv-55ad), beyond the panel
scenarios of test_limits_claude_usage.py."""

from datetime import UTC, datetime, timedelta

from src.model_router import quota
from tests.unit.test_limits_panel import root  # noqa: F401  (autouse fixture)
from tests.unit.test_model_router_job import _multi_turn_job

# The 2026-10-26 probe (CLI 2.1.310), verbatim apart from the ids.
PROBE = {
    "status": "allowed",
    "resetsAt": 1793060400,
    "rateLimitType": "five_hour",
    "overageStatus": "rejected",
    "overageDisabledReason": "org_level_disabled",
    "isUsingOverage": False,
    "unifiedWindows": {
        "five_hour": {"utilization": 0.22, "resetsAt": 1793060400, "status": "allowed"},
        "seven_day": {"utilization": 0.14, "resetsAt": 1793610000, "status": "allowed"},
    },
}


def test_usage_from_the_probe_payload():
    windows = quota.usage_from_info(PROBE)
    assert list(windows) == ["five_hour", "seven_day"]
    assert windows["five_hour"]["utilization"] == 0.22
    assert windows["seven_day"]["resets_at"] == "2026-11-02T09:00:00Z"
    assert quota.usage_from_info({"status": "allowed"}) is None
    assert quota.usage_from_info({"unifiedWindows": {}}) is None
    assert quota.usage_from_info(None) is None


def test_usage_line_round_trip_takes_the_last_line():
    at = datetime(2026, 10, 27, 9, 0, tzinfo=UTC)
    first = quota.usage_line({**PROBE, "status": "old"}, at - timedelta(minutes=5))
    last = quota.usage_line(PROBE, at)
    got = quota.find_usage(f"noise\n{first}\nagentjob failed: x\n{last}\n")
    assert got == (PROBE, at)
    assert quota.usage_line({"status": "allowed"}) is None
    assert quota.find_usage("agentjob usage: {not json") is None


def test_unknown_window_key_is_shown_raw_and_limit_next_to_the_percent():
    now = datetime(2026, 10, 27, 9, 0, tzinfo=UTC)
    info = {
        "unifiedWindows": {
            "five_hour": {
                "utilization": 0.5,
                "resetsAt": int((now + timedelta(hours=1)).timestamp()),
            },
            "monthly_x": {
                "utilization": 0.01,
                "resetsAt": int((now + timedelta(days=3)).timestamp()),
            },
        }
    }
    quota.record_usage(info, "claude-turn", now - timedelta(minutes=5))
    limit = quota.Limit("session limit", now + timedelta(minutes=40), "You've hit it")
    text = quota.format_claude_usage(quota.read_usage(), limit, now)
    assert "на 08:55 UTC, 5 мин назад" in text, text
    assert "5 ч: использовано 50% · сброс 10:00 UTC" in text, text
    assert "monthly_x: использовано 1% · сброс 30.10 09:00 UTC" in text, text
    assert "лимит кончился, до 09:40 UTC" in text, text
    assert "устар" not in text


def test_unwritable_record_is_best_effort(monkeypatch):
    def boom(_change):
        raise OSError("read-only")

    monkeypatch.setattr(quota, "update_record", boom)
    quota.record_usage(PROBE, "claude-turn")  # no raise
    assert quota.read_usage() is None


async def test_agentjob_prints_the_usage_line_on_an_error_exit(monkeypatch, capsys):
    from claude_agent_sdk._internal.message_parser import parse_message

    event = parse_message(
        {
            "type": "rate_limit_event",
            "rate_limit_info": PROBE,
            "uuid": "u",
            "session_id": "s",
        }
    )
    run, _, _, _ = _multi_turn_job(monkeypatch, [[event]])  # no result: error
    assert await run() == 1
    err = capsys.readouterr().err
    assert "agentjob failed" in err
    got = quota.find_usage(err)
    assert got is not None and got[0] == PROBE
