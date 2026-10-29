"""Host and container metrics sampler for the ops console server board (dv-e005).

One run = one row set into Postgres ``observability.host_metrics``, then the
rows older than ``RETENTION_DAYS`` are deleted. ``deploy/host-metrics.timer``
runs it every minute as a oneshot unit, outside the bot process:

    python -m src.observability.host_metrics

Host numbers come from ``/proc`` and ``statvfs`` (no psutil), per-container
memory/CPU from ``docker stats``. Memory and disk are bytes, CPU is percent;
the mount or container a row is about is in ``labels``.

Connection from the environment (``~/.config/grafana.env`` through the unit's
``EnvironmentFile``): ``OBS_PG_HOST`` (127.0.0.1), ``OBS_PG_PORT`` (5433),
``OBS_PG_DB`` (observability), user ``obs_writer``, ``OBS_WRITER_PASSWORD``.
The role can INSERT and run the retention DELETE, nothing else
(``deploy/grafana/sql/observability.sql``).

Failure modes: DB unreachable -> one log line, exit 1. Docker unavailable ->
a warning, the host rows are still written. Nothing here is imported by the
bot; it only shares the venv.
"""

import json
import os
import socket
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import structlog

logger = structlog.get_logger()

RETENTION_DAYS = 90
MOUNTS = ("/", "/tmp")
CPU_SAMPLE_S = 1.0
DOCKER_TIMEOUT_S = 20.0
CONNECT_TIMEOUT_S = 5

# (metric, labels, value)
Row = Tuple[str, Dict[str, str], float]

_UNITS = {
    "b": 1,
    "kb": 1000,
    "mb": 1000**2,
    "gb": 1000**3,
    "tb": 1000**4,
    "kib": 1024,
    "mib": 1024**2,
    "gib": 1024**3,
    "tib": 1024**4,
}


# --- host ----------------------------------------------------------------------


def parse_meminfo(text: str) -> List[Row]:
    """MemTotal / MemAvailable / swap used, in bytes."""
    kb: Dict[str, int] = {}
    for line in text.splitlines():
        name, _, rest = line.partition(":")
        parts = rest.split()
        if parts and parts[0].isdigit():
            kb[name.strip()] = int(parts[0])
    rows: List[Row] = []
    if "MemTotal" in kb:
        rows.append(("mem_total_bytes", {}, kb["MemTotal"] * 1024.0))
    if "MemAvailable" in kb:
        rows.append(("mem_available_bytes", {}, kb["MemAvailable"] * 1024.0))
    if "SwapTotal" in kb and "SwapFree" in kb:
        used = kb["SwapTotal"] - kb["SwapFree"]
        rows.append(("swap_used_bytes", {}, used * 1024.0))
    return rows


def cpu_times(text: str) -> Tuple[int, int]:
    """(busy, total) jiffies from the aggregate ``cpu`` line of /proc/stat."""
    for line in text.splitlines():
        if line.startswith("cpu "):
            fields = [int(x) for x in line.split()[1:9]]
            idle = fields[3] + fields[4]  # idle + iowait
            total = sum(fields)
            return total - idle, total
    raise ValueError("no aggregate cpu line in /proc/stat")


def cpu_percent(before: Tuple[int, int], after: Tuple[int, int]) -> float:
    """Busy share of all CPUs between two samples, 0-100."""
    busy = after[0] - before[0]
    total = after[1] - before[1]
    if total <= 0:
        return 0.0
    return max(0.0, min(100.0, 100.0 * busy / total))


def disk_rows(mounts: Tuple[str, ...] = MOUNTS) -> List[Row]:
    rows: List[Row] = []
    for mount in mounts:
        try:
            st = os.statvfs(mount)
        except OSError as e:
            logger.warning("statvfs failed", mount=mount, error=str(e))
            continue
        labels = {"mount": mount}
        used = (st.f_blocks - st.f_bfree) * st.f_frsize
        rows.append(("disk_used_bytes", labels, float(used)))
        rows.append(("disk_size_bytes", labels, float(st.f_blocks * st.f_frsize)))
    return rows


def load_rows(text: str) -> List[Row]:
    return [("load1", {}, float(text.split()[0]))]


# --- containers ----------------------------------------------------------------


def parse_size(text: str) -> Optional[float]:
    """'35.2MiB' / '1.5GB' / '0B' -> bytes; None if unparseable."""
    text = text.strip()
    num = text.rstrip("BbiKkMmGgTt")
    unit = text[len(num) :].lower()
    try:
        return float(num) * _UNITS[unit]
    except (ValueError, KeyError):
        return None


def parse_docker_stats(output: str) -> List[Row]:
    """``docker stats --no-stream --format '{{json .}}'`` lines -> rows."""
    rows: List[Row] = []
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except ValueError:
            continue
        name = str(item.get("Name") or item.get("Container") or "")
        if not name:
            continue
        labels = {"container": name}
        mem = parse_size(str(item.get("MemUsage", "")).split("/")[0])
        if mem is not None:
            rows.append(("container_mem_bytes", labels, mem))
        cpu = str(item.get("CPUPerc", "")).strip().rstrip("%")
        try:
            rows.append(("container_cpu_percent", labels, float(cpu)))
        except ValueError:
            pass
    return rows


def _start_docker_stats() -> Optional["subprocess.Popen[str]"]:
    try:
        return subprocess.Popen(
            ["docker", "stats", "--no-stream", "--format", "{{json .}}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except OSError as e:
        logger.warning("docker stats unavailable", error=str(e))
        return None


def _finish_docker_stats(proc: Optional["subprocess.Popen[str]"]) -> List[Row]:
    if proc is None:
        return []
    try:
        out, err = proc.communicate(timeout=DOCKER_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        logger.warning("docker stats timed out", timeout_s=DOCKER_TIMEOUT_S)
        return []
    if proc.returncode != 0:
        logger.warning(
            "docker stats failed", code=proc.returncode, error=err.strip()[:200]
        )
        return []
    return parse_docker_stats(out)


# --- one sample ----------------------------------------------------------------


def sample() -> List[Row]:
    """Every metric of this run. docker stats (~2 s) runs while CPU is sampled."""
    docker = _start_docker_stats()
    stat = Path("/proc/stat")
    before = cpu_times(stat.read_text())
    time.sleep(CPU_SAMPLE_S)
    after = cpu_times(stat.read_text())
    rows: List[Row] = [("cpu_percent", {}, cpu_percent(before, after))]
    rows += load_rows(Path("/proc/loadavg").read_text())
    rows += parse_meminfo(Path("/proc/meminfo").read_text())
    rows += disk_rows()
    rows += _finish_docker_stats(docker)
    return rows


def write(conn: Any, ts: datetime, host: str, rows: List[Row]) -> int:
    """Insert the row set and apply retention in one transaction; returns the
    number of rows deleted."""
    from psycopg.types.json import Jsonb

    with conn.transaction(), conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO host_metrics (ts, host, metric, labels, value)"
            " VALUES (%s, %s, %s, %s, %s)",
            [
                (ts, host, metric, Jsonb(labels), value)
                for metric, labels, value in rows
            ],
        )
        cur.execute(
            "DELETE FROM host_metrics WHERE ts < now() - make_interval(days => %s)",
            (RETENTION_DAYS,),
        )
        return int(cur.rowcount or 0)


def _connect_args(env: Dict[str, str]) -> Dict[str, Any]:
    return {
        "host": env.get("OBS_PG_HOST") or "127.0.0.1",
        "port": int(env.get("OBS_PG_PORT") or 5433),
        "dbname": env.get("OBS_PG_DB") or "observability",
        "user": "obs_writer",
        "password": env.get("OBS_WRITER_PASSWORD", ""),
        "connect_timeout": CONNECT_TIMEOUT_S,
        "application_name": "host-metrics",
    }


def main() -> int:
    args = _connect_args(dict(os.environ))
    if not args["password"]:
        logger.error("OBS_WRITER_PASSWORD not set; observability DB not written")
        return 2
    try:
        import psycopg
    except ImportError as e:
        logger.error("Postgres driver psycopg missing", error=str(e))
        return 2

    ts = datetime.now(UTC)
    rows = sample()
    target = f"{args['host']}:{args['port']}/{args['dbname']}"
    try:
        with psycopg.connect(**args) as conn:
            deleted = write(conn, ts, socket.gethostname(), rows)
    except psycopg.Error as e:
        logger.error(
            "observability DB write failed (connect/database)",
            target=target,
            error=" ".join(str(e).split())[:300],
        )
        return 1
    logger.info("host metrics written", rows=len(rows), deleted=deleted)
    return 0


if __name__ == "__main__":
    sys.exit(main())
