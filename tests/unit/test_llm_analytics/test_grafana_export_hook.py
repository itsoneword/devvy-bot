"""The Grafana export inside the mirror cycle never breaks the scan and only
writes where the installer created the dir (dv-e005)."""

import dataclasses
import sqlite3
from pathlib import Path

import pytest

from src.llm_analytics import indexer as indexer_mod
from src.llm_analytics.config import AnalyticsConfig
from src.llm_analytics.db import connect, init_db
from src.llm_analytics.indexer import Indexer
from src.llm_analytics.pricing import PricingTable
from src.storage.schema.migrate import migrate_connection


def _indexer(tmp_path: Path, export: Path | None) -> tuple[Indexer, sqlite3.Connection]:
    bot_db = tmp_path / "bot.db"
    src = sqlite3.connect(bot_db)
    migrate_connection("bot", src)
    src.close()
    pricing_path = tmp_path / "pricing.json"
    pricing_path.write_text(
        '{"default": {"input": 5.0, "output": 25.0, "cache_read": 0.5,'
        ' "cache_write_5m": 6.25, "cache_write_1h": 10.0}, "rules": []}'
    )
    (tmp_path / "projects").mkdir()
    config = AnalyticsConfig(
        host="127.0.0.1",
        port=0,
        db_path=tmp_path / "a.db",
        projects_dir=tmp_path / "projects",
        bot_db_path=bot_db,
        pricing_path=pricing_path,
        scan_interval=30,
        grafana_export_path=export,
    )
    conn = connect(config.db_path)
    init_db(conn)
    return Indexer(conn, config, PricingTable.load(pricing_path)), conn


def _meta(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        "SELECT value FROM meta WHERE key = 'grafana_export_at'"
    ).fetchone()
    return None if row is None else str(row[0])


def test_default_config_has_export_off() -> None:
    fields = {f.name: f for f in dataclasses.fields(AnalyticsConfig)}
    assert fields["grafana_export_path"].default is None


def test_missing_export_dir_writes_nothing_and_retries(tmp_path: Path) -> None:
    out = tmp_path / "grafana" / "devvy.db"
    indexer, conn = _indexer(tmp_path, out)
    indexer.scan_once()
    assert not out.parent.exists()
    assert _meta(conn) is None  # not throttled: the next cycle tries again
    out.parent.mkdir()
    indexer.scan_once()
    assert out.exists()
    assert _meta(conn) is not None


def test_export_failure_does_not_break_the_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "grafana" / "devvy.db"
    out.parent.mkdir()

    def boom(bot_db: Path, out_path: Path) -> bool:
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(indexer_mod, "export_usage", boom)
    indexer, conn = _indexer(tmp_path, out)
    indexer.scan_once()
    assert not out.exists()
    assert _meta(conn) is None
    last_scan = conn.execute(
        "SELECT value FROM meta WHERE key = 'last_scan_at'"
    ).fetchone()
    assert last_scan is not None


def test_failed_export_leaves_no_tmp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.llm_analytics import grafana_export

    out = tmp_path / "grafana" / "devvy.db"
    out.parent.mkdir()
    indexer, _ = _indexer(tmp_path, out)

    def fail_replace(src: object, dst: object) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(grafana_export.os, "replace", fail_replace)
    with pytest.raises(OSError):
        grafana_export.export_usage(tmp_path / "bot.db", out)
    assert list(out.parent.iterdir()) == []
