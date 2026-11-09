"""How a background job is started, watched and stopped (dv-fe4d).

Two launchers, one job dir contract (run.sh, exit_code, output.md):

* ``systemd`` (the Linux host, default when systemd runs and ``systemd-run``
  exists): a transient ``systemd-run --user`` unit, as bgjob always did. Its cgroup
  gives the hard memory cap and the ``oom-kill`` result in the journal.
* ``process`` (macOS, WSL2 without systemd, or ``DEVVY_LAUNCHER=process``):
  run.sh starts detached under the supervisor in this file. No cgroup, so the
  memory cap is SOFT: every :data:`SAMPLE_S` seconds the supervisor sums the
  RSS of the job's process group and, past the cap, kills the group
  (SIGTERM, :data:`KILL_GRACE_S`, SIGKILL). It can overshoot between
  samples. On exit it writes ``outcome.json``:
  ``{rc, signal, result: exit|memcap|signal, peak, peak_bytes}``.
  :func:`outcome` maps ``memcap`` to systemd's ``oom-kill`` so the pump's
  existing OOM branch reports it. A kill by the OS itself (jetsam, kernel
  OOM) reads ``result=signal``: we cannot tell it from any other SIGKILL and
  do not pretend to.

The job record says which launcher started it (``meta["launcher"]``,
absent = systemd), so a job is always watched by the launcher that started
it. Stdlib only; also run as ``python launcher.py supervise ...``.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

SYSTEMD = "systemd"
PROCESS = "process"

SUPERVISOR_PID = "supervisor.pid"
JOB_PGID = "job.pgid"
OUTCOME = "outcome.json"
SUPERVISOR_LOG = "supervisor.log"

SAMPLE_S = float(os.environ.get("BGJOB_SAMPLE_S", "5"))
KILL_GRACE_S = 10.0
_POLL_S = 0.5


# sd_booted(3): present only when systemd is the running init.
SYSTEMD_BOOTED = Path("/run/systemd/system")


def kind() -> str:
    """The launcher for a new job: ``DEVVY_LAUNCHER`` if set, else systemd
    when ``systemd-run`` exists AND systemd is the running init, else process.

    The binary alone is not enough: WSL2 without systemd and most containers
    ship it with no manager behind it, and every job would then fail at
    ``systemd-run``. The check is sd_booted's, not testrun's bus probe: an
    agent job's box hides the user bus on purpose (dv-5e2f), and a nested
    ``bgjob run`` there must keep failing at systemd-run, not quietly switch
    to the process launcher."""
    forced = os.environ.get("DEVVY_LAUNCHER", "").strip().lower()
    if forced in (SYSTEMD, PROCESS):
        return forced
    if shutil.which("systemd-run") and SYSTEMD_BOOTED.is_dir():
        return SYSTEMD
    return PROCESS


def is_process(meta: Dict[str, Any]) -> bool:
    """Was this job started by the process launcher?"""
    return meta.get("launcher") == PROCESS


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    stat = Path(f"/proc/{pid}/stat")
    if stat.exists():  # a zombie still answers kill(0)
        try:
            return stat.read_text().rsplit(")", 1)[1].split()[0] != "Z"
        except (OSError, IndexError):
            return True
    return True


def _read_int(path: Path) -> Optional[int]:
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return None


def active(job_dir: Path) -> bool:
    """A process-launched job is active while its supervisor lives and has
    not written the outcome."""
    if (job_dir / OUTCOME).exists():
        return False
    pid = _read_int(job_dir / SUPERVISOR_PID)
    return pid is not None and _pid_alive(pid)


def start(
    job_dir: Path,
    runner: Path,
    memory_max: Optional[int],
    env: Optional[Dict[str, str]] = None,
) -> None:
    """Start *runner* detached under the supervisor; returns once the job's
    process group exists (so a listing right after shows it RUNNING)."""
    cmd = [
        sys.executable,
        str(Path(__file__).resolve()),
        "supervise",
        "--dir",
        str(job_dir),
        "--memory-max",
        str(memory_max or 0),
        "--",
        "/bin/bash",
        str(runner),
    ]
    with open(job_dir / SUPERVISOR_LOG, "ab") as log:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
            env=env,
            close_fds=True,
        )
    # The first process forks the supervisor and exits once the job's pgid
    # is on disk: the supervisor is then nobody's child here (no zombie).
    rc = proc.wait(timeout=60)
    if rc != 0:
        raise RuntimeError(f"job supervisor failed to start (rc={rc})")


def cancel(job_dir: Path) -> None:
    """Stop a process-launched job: the supervisor kills the group and still
    writes the outcome; with no supervisor, the group is killed here."""
    pid = _read_int(job_dir / SUPERVISOR_PID)
    if pid is not None and _pid_alive(pid):
        try:
            os.kill(pid, signal.SIGTERM)
            return
        except ProcessLookupError:
            pass
    pgid = _read_int(job_dir / JOB_PGID)
    if pgid is not None:
        _signal_group(pgid, signal.SIGKILL)


def outcome(job_dir: Path) -> tuple[str, str]:
    """(result, peak) in unit_outcome's terms: ``oom-kill`` for a memcap
    kill, ``signal-N`` for any other signal, "" for a plain exit or unknown."""
    try:
        data = json.loads((job_dir / OUTCOME).read_text())
    except (OSError, ValueError):
        return "", ""
    peak = str(data.get("peak") or "")
    result = data.get("result")
    if result == "memcap":
        return "oom-kill", peak
    if result == "signal":
        return f"signal-{data.get('signal') or '?'}", peak
    return "", peak


def spawn_detached(argv: Sequence[str], env: Optional[Dict[str, str]] = None) -> None:
    """Fire-and-forget a helper (tick nudge, digest watcher) outside the
    caller's process group, as a transient unit would be."""
    subprocess.Popen(
        list(argv),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        env=env,
    )


# --------------------------------------------------------------- supervisor


def group_rss(pgid: int) -> int:
    """Summed RSS (bytes) of every process in *pgid* (``ps`` works on macOS
    and Linux alike)."""
    try:
        out = subprocess.run(
            ["ps", "-A", "-o", "pgid=,rss="],
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return 0
    total = 0
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] == str(pgid) and parts[1].isdigit():
            total += int(parts[1]) * 1024
    return total


def _signal_group(pgid: int, sig: int) -> None:
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def _human(n: int) -> str:
    for unit, size in (("G", 2**30), ("M", 2**20), ("K", 2**10)):
        if n >= size:
            return f"{n / size:.1f}{unit}".replace(".0", "")
    return f"{n}B"


def _kill_group(proc: "subprocess.Popen[bytes]", pgid: int) -> int:
    _signal_group(pgid, signal.SIGTERM)
    try:
        rc = proc.wait(timeout=KILL_GRACE_S)
    except subprocess.TimeoutExpired:
        _signal_group(pgid, signal.SIGKILL)
        rc = proc.wait()
    _signal_group(pgid, signal.SIGKILL)  # stragglers that ignored TERM
    return rc


def _watch(
    job_dir: Path,
    proc: "subprocess.Popen[bytes]",
    pgid: int,
    memory_max: int,
    is_cancelled: Any,
) -> Dict[str, Any]:
    peak = 0
    result = "exit"
    next_sample = 0.0
    while True:
        try:
            rc = proc.wait(timeout=_POLL_S)
            break
        except subprocess.TimeoutExpired:
            pass
        if is_cancelled():
            result = "signal"
            rc = _kill_group(proc, pgid)
            break
        now = time.monotonic()
        if now >= next_sample:
            next_sample = now + SAMPLE_S
            rss = group_rss(pgid)
            peak = max(peak, rss)
            if memory_max and rss > memory_max:
                print(
                    f"supervisor: group rss {_human(rss)} over cap "
                    f"{_human(memory_max)}; killing",
                    flush=True,
                )
                result = "memcap"
                rc = _kill_group(proc, pgid)
                break
    if result == "exit":
        # As systemd does at the end of a unit: nothing of the job outlives it.
        _signal_group(pgid, signal.SIGKILL)
        if rc < 0:
            result = "signal"
    record = {
        "rc": rc if rc >= 0 else None,
        "signal": -rc if rc < 0 else None,
        "result": result,
        "peak": _human(peak) if peak else "",
        "peak_bytes": peak,
    }
    tmp = job_dir / (OUTCOME + ".tmp")
    tmp.write_text(json.dumps(record))
    tmp.replace(job_dir / OUTCOME)
    return record


def _supervise_main(args: argparse.Namespace) -> int:
    job_dir = Path(args.dir)
    argv = list(args.argv)
    if argv and argv[0] == "--":
        argv = argv[1:]
    read_fd, write_fd = os.pipe()
    if os.fork():
        # First process: wait until the supervisor has the job started.
        os.close(write_fd)
        ok = os.read(read_fd, 1) == b"1"
        return 0 if ok else 1
    os.close(read_fd)
    os.setsid()
    (job_dir / SUPERVISOR_PID).write_text(str(os.getpid()))
    cancelled = False

    def on_term(_sig: int, _frame: Any) -> None:
        nonlocal cancelled
        cancelled = True

    signal.signal(signal.SIGTERM, on_term)
    try:
        proc = subprocess.Popen(argv, start_new_session=True)
    except OSError as exc:
        print(f"supervisor: cannot start {argv}: {exc}", flush=True)
        os.write(write_fd, b"0")
        os._exit(1)
    (job_dir / JOB_PGID).write_text(str(proc.pid))
    os.write(write_fd, b"1")
    os.close(write_fd)
    _watch(job_dir, proc, proc.pid, int(args.memory_max), lambda: cancelled)
    os._exit(0)


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="launcher")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("supervise", help="run a job under the soft-cap supervisor")
    p.add_argument("--dir", required=True)
    p.add_argument("--memory-max", default="0")
    p.add_argument("argv", nargs=argparse.REMAINDER)
    args = ap.parse_args(argv)
    return _supervise_main(args)


if __name__ == "__main__":
    sys.exit(main())
