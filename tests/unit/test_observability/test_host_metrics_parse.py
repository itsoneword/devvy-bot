"""Parsers of the host-metrics sampler (dv-e005 phase 3).

The end-to-end contract (real Postgres, real /proc, real docker) is in
tests/unit/test_host_metrics.py; this pins the parsing on fixed inputs, so a
unit mix-up (KiB vs kB, MiB vs MB) or a missing iowait shows as a number.
"""

import json
from datetime import UTC, datetime
from typing import Any, List

import pytest

from src.observability import host_metrics as hm

MEMINFO = """MemTotal:       11000000 kB
MemFree:          500000 kB
MemAvailable:    1100000 kB
SwapTotal:       2000000 kB
SwapFree:        1500000 kB
HugePages_Total:       0
"""


def test_meminfo_in_bytes() -> None:
    rows = {m: v for m, _, v in hm.parse_meminfo(MEMINFO)}
    assert rows == {
        "mem_total_bytes": 11000000 * 1024,
        "mem_available_bytes": 1100000 * 1024,
        "swap_used_bytes": 500000 * 1024,
    }


def test_cpu_percent_counts_iowait_as_idle() -> None:
    before = hm.cpu_times("cpu  100 0 100 700 100 0 0 0 0 0\ncpu0 1 2 3 4\n")
    after = hm.cpu_times("cpu  150 0 150 850 150 0 0 0 0 0\n")
    # busy +100, idle+iowait +200 -> 33.3 %
    assert hm.cpu_percent(before, after) == pytest.approx(100 / 3)
    assert hm.cpu_percent(after, after) == 0.0


def test_cpu_times_without_aggregate_line_raises() -> None:
    with pytest.raises(ValueError):
        hm.cpu_times("cpu0 1 2 3 4 5 6 7 8\n")


@pytest.mark.parametrize(
    "text,expected",
    [
        ("35.5MiB", 35.5 * 1024**2),
        ("1.5GiB", 1.5 * 1024**3),
        ("512KiB", 512 * 1024),
        ("0B", 0.0),
        ("2GB", 2e9),
        ("12kB", 12000.0),
        (" 7MB ", 7e6),
        ("--", None),
        ("", None),
    ],
)
def test_parse_size(text: str, expected: Any) -> None:
    assert hm.parse_size(text) == expected


def test_docker_stats_lines_to_rows() -> None:
    lines = [
        {"Name": "gymbro-db", "MemUsage": "40MiB / 256MiB", "CPUPerc": "1.25%"},
        {"Name": "grafana", "MemUsage": "--", "CPUPerc": "--"},
        {"Container": "abc", "MemUsage": "1GiB / 2GiB", "CPUPerc": "150.00%"},
    ]
    out = "\n".join(json.dumps(x) for x in lines) + "\nnot json\n\n"
    rows = hm.parse_docker_stats(out)
    assert rows == [
        ("container_mem_bytes", {"container": "gymbro-db"}, 40 * 1024**2),
        ("container_cpu_percent", {"container": "gymbro-db"}, 1.25),
        ("container_mem_bytes", {"container": "abc"}, float(1024**3)),
        ("container_cpu_percent", {"container": "abc"}, 150.0),
    ]


def test_disk_rows_label_the_mount_and_skip_missing(tmp_path: Any) -> None:
    rows = hm.disk_rows((str(tmp_path), "/no/such/mount"))
    assert {m for m, _, _ in rows} == {"disk_used_bytes", "disk_size_bytes"}
    assert all(labels == {"mount": str(tmp_path)} for _, labels, _ in rows)


def test_connect_args_defaults_and_fixed_user() -> None:
    args = hm._connect_args({"OBS_WRITER_PASSWORD": "pw", "PGUSER": "gymbro"})
    assert (args["host"], args["port"], args["dbname"]) == (
        "127.0.0.1",
        5433,
        "observability",
    )
    assert args["user"] == "obs_writer"
    assert args["connect_timeout"] <= 10


def test_no_password_is_an_error_before_any_sample(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OBS_WRITER_PASSWORD", raising=False)
    monkeypatch.setattr(hm, "sample", lambda: pytest.fail("sampled"))
    assert hm.main() == 2


class _Cursor:
    def __init__(self, log: List[Any]) -> None:
        self.log = log
        self.rowcount = 3

    def __enter__(self) -> "_Cursor":
        return self

    def __exit__(self, *a: Any) -> None:
        pass

    def executemany(self, sql: str, params: List[Any]) -> None:
        self.log.append(("many", sql, params))

    def execute(self, sql: str, params: Any) -> None:
        self.log.append(("one", sql, params))


class _Conn:
    def __init__(self) -> None:
        self.log: List[Any] = []

    def transaction(self) -> "_Cursor":
        return _Cursor(self.log)

    def cursor(self) -> _Cursor:
        return _Cursor(self.log)


def test_write_inserts_the_row_set_then_applies_90_day_retention() -> None:
    pytest.importorskip("psycopg")
    conn = _Conn()
    ts = datetime(2026, 10, 26, tzinfo=UTC)
    deleted = hm.write(conn, ts, "h", [("cpu_percent", {}, 5.0)])
    assert deleted == 3
    kind, sql, params = conn.log[0]
    assert kind == "many" and sql.startswith("INSERT INTO host_metrics")
    assert [p[:3] for p in params] == [(ts, "h", "cpu_percent")]
    kind, sql, params = conn.log[1]
    assert kind == "one" and sql.startswith("DELETE FROM host_metrics WHERE ts <")
    assert params == (hm.RETENTION_DAYS,) and hm.RETENTION_DAYS == 90
