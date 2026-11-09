"""Compatibility at the provider boundary of the shared quota record."""

from datetime import UTC, datetime, timedelta


def test_old_marker_is_claude_and_new_marker_keeps_openai() -> None:
    from src.model_router.quota import Limit, parse_quota_line, quota_line

    until = datetime(2026, 11, 2, 22, 24, tzinfo=UTC)
    old = parse_quota_line(
        "agentjob quota: You've hit your weekly limit · until " "2026-11-02T22:24:00Z"
    )
    assert old is not None and old.provider == "anthropic" and old.until == until
    codex = Limit("usage limit", until, "You've hit your usage limit", "openai")
    assert parse_quota_line(quota_line(codex)) == codex


def test_claude_route_provider_alias_reads_legacy_record(tmp_path, monkeypatch) -> None:
    from src.model_router import quota

    monkeypatch.setenv("DEVVY_DATA_ROOT", str(tmp_path))
    until = datetime.now(UTC) + timedelta(hours=2)
    quota.record(quota.Limit("weekly limit", until, "Claude limit"), "test")
    assert quota.active(provider="anthropic_sub") == quota.active(provider="anthropic")
    assert quota.active(provider="openai") is None
