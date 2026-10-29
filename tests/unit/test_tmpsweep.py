"""dv-1a02: scripts/tmpsweep -- the daily /tmp sweep (tmpfs = RAM).

The script is stdlib python with no .py suffix, loaded like scripts/bgjob in
test_bgjob.py. Contract these tests pin (docs/research/dv-1a02/analysis.md 2.1):

- ``sweep(root, *, now=None, max_idle_h=12, uid=None, proc_root="/proc",
  dry_run=False)`` sweeps the top-level entries of ``root`` and returns an
  object with ``freed_bytes`` (int) and ``removed`` (list of paths).
- ``usage_percent(root) -> float``: how full the filesystem of ``root`` is.
- ``send_notify(text) -> None``: the one ``agentctl notify --profile devops``
  line.
- ``main(argv) -> int`` with ``--root``, ``--max-idle-h``, ``--dry-run``,
  ``--notify``, ``--force``; state in ``$XDG_STATE_HOME/devvy/``.
"""

from __future__ import annotations

import importlib.util
import os
import socket
import subprocess
import sys
import time
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import SimpleNamespace

import pytest

_SCRIPTS = Path(__file__).parents[2] / "scripts"
_TMPSWEEP_PATH = _SCRIPTS / "tmpsweep"
_BGJOB_PATH = _SCRIPTS / "bgjob"

H = 3600


def _load(name: str, path: Path):
    loader = SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


@pytest.fixture
def tmpsweep():
    assert _TMPSWEEP_PATH.exists(), "scripts/tmpsweep does not exist yet"
    return _load("tmpsweep_under_test", _TMPSWEEP_PATH)


@pytest.fixture
def bgjob():
    return _load("bgjob_under_test_tmpsweep", _BGJOB_PATH)


@pytest.fixture
def root(tmp_path, monkeypatch) -> Path:
    r = tmp_path / "root"
    r.mkdir()
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    return r


def _age(path: Path, hours: float) -> None:
    """Set mtime (and atime) of path and everything under it to `hours` ago,
    children first so the parents' mtimes are not bumped afterwards."""
    t = time.time() - hours * H
    if path.is_dir() and not path.is_symlink():
        for dirpath, dirnames, filenames in os.walk(path, topdown=False):
            for n in filenames:
                os.utime(os.path.join(dirpath, n), (t, t), follow_symlinks=False)
            for n in dirnames:
                os.utime(os.path.join(dirpath, n), (t, t), follow_symlinks=False)
    os.utime(path, (t, t), follow_symlinks=False)


def _stale_dir(parent: Path, name: str, hours: float = 13) -> Path:
    d = parent / name
    (d / "sub").mkdir(parents=True)
    (d / "sub" / "a.db").write_bytes(b"x" * 8192)
    (d / "b.txt").write_text("old")
    _age(d, hours)
    return d


# --------------------------------------------------------------------------
# Approved scenarios
# --------------------------------------------------------------------------


def test_entry_untouched_over_12h_is_removed(tmpsweep, root):
    """Scenario 1: a dir and a file untouched for >12h are gone after the
    sweep, and the freed bytes are reported."""
    d = _stale_dir(root, "job-scratch")
    f = root / "tmpabc123.db"
    f.write_bytes(b"y" * 4096)
    _age(f, 13)

    result = tmpsweep.sweep(root)

    assert not d.exists()
    assert not f.exists()
    assert result.freed_bytes > 0
    assert {Path(p).name for p in result.removed} >= {"job-scratch", "tmpabc123.db"}


def test_recent_file_locks_sockets_and_codex_stay(tmpsweep, root, monkeypatch):
    """Scenario 2: a dir with one file touched 1h ago stays (the old files in
    it too); so do lock files, unix sockets and codex-* -- all idle >12h."""
    live = _stale_dir(root, "half-live")
    fresh = live / "sub" / "fresh.log"
    fresh.write_text("new")
    t = time.time() - 1 * H
    os.utime(fresh, (t, t))

    lock = root / "something.lock"
    lock.write_text("")
    _age(lock, 30)

    codex = _stale_dir(root, "codex-abc", hours=30)

    # AF_UNIX paths are capped at ~108 bytes: bind by a relative name.
    monkeypatch.chdir(root)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        srv.bind("s.sock")
        sock = root / "s.sock"
        _age(sock, 30)

        tmpsweep.sweep(root)

        assert live.exists() and (live / "b.txt").exists() and fresh.exists()
        assert lock.exists()
        assert sock.exists()
        assert codex.exists() and (codex / "b.txt").exists()
    finally:
        srv.close()


def test_dir_held_by_running_process_stays(tmpsweep, root):
    """Scenario 3: a dir a running process uses as cwd (a running job's
    scratch) stays, even though nothing in it changed for >12h; once the
    process is gone the same dir is swept."""
    busy = _stale_dir(root, "running-job")
    proc = subprocess.Popen(["sleep", "60"], cwd=busy / "sub")
    try:
        _age(busy, 13)  # Popen does not touch mtimes, but be explicit
        tmpsweep.sweep(root)
        assert busy.exists() and (busy / "sub" / "a.db").exists()
    finally:
        proc.kill()
        proc.wait()

    tmpsweep.sweep(root)
    assert not busy.exists()


class _BgjobCmd:
    @staticmethod
    def setup(bgjob, jobs: Path, monkeypatch) -> list:
        monkeypatch.setattr(bgjob, "JOBS_ROOT", jobs)
        monkeypatch.setattr(bgjob, "load_profiles", lambda: {"general": str(jobs)})
        monkeypatch.setattr(bgjob, "live_jobs", lambda: [])
        calls: list = []

        class R:
            returncode = 0
            stderr = ""
            stdout = ""

        monkeypatch.setattr(
            bgjob.subprocess, "run", lambda cmd, **kw: calls.append(cmd) or R()
        )
        monkeypatch.setattr(bgjob.subprocess, "Popen", lambda *a, **k: None)
        return calls

    @staticmethod
    def args(cmd: str):
        return type(
            "A",
            (),
            {
                "prompt": None,
                "cmd": cmd,
                "profile": "general",
                "name": "tmpcheck",
                "allow_dir": None,
                "model": "sonnet",
                "quiet": True,
            },
        )()


def test_job_mktemp_lands_in_job_dir_and_is_removed_after(bgjob, tmp_path, monkeypatch):
    """Scenario 4: a --cmd job's tempfile.mktemp() lands under its own job
    dir, and that tmp dir is gone after the job (trailing rm in run.sh, or an
    ExecStopPost on the unit that systemd runs after run.sh)."""
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    calls = _BgjobCmd.setup(bgjob, jobs, monkeypatch)
    cmd = f"{sys.executable} -c 'import tempfile;print(tempfile.mktemp())'"
    assert bgjob.cmd_run(_BgjobCmd.args(cmd)) == 0
    run_sh = next(jobs.glob("*/run.sh"))
    d = run_sh.parent

    monkeypatch.undo()
    env = dict(os.environ, BGJOB_ROOT=str(jobs), TMPDIR="/tmp")
    subprocess.run(["bash", str(run_sh)], env=env, timeout=60, check=False)

    assert (d / "exit_code").read_text().strip() == "0"
    printed = Path((d / "output.md").read_text().strip().splitlines()[-1])
    assert d.resolve() in printed.resolve().parents, printed

    tmp = d / "tmp"
    if tmp.exists():
        units = [c for c in calls if c and c[0] == "systemd-run"]
        assert units, calls
        stop_post = [a for a in units[-1] if "ExecStopPost" in a]
        assert stop_post and str(tmp) in " ".join(
            stop_post
        ), f"{tmp} left behind and no ExecStopPost removes it: {units[-1]}"


def test_devops_line_only_when_big_or_still_over(tmpsweep, root, monkeypatch):
    """Scenario 5: one devops line only when >500 MB was freed or /tmp is
    still >70% after the sweep; the still-over alert is not repeated within
    6h; a small sweep on a quiet /tmp says nothing."""
    sent: list[str] = []
    freed = {"bytes": 0}
    usage = {"pct": 20.0}
    monkeypatch.setattr(
        tmpsweep,
        "sweep",
        lambda *a, **k: SimpleNamespace(freed_bytes=freed["bytes"], removed=[]),
    )
    monkeypatch.setattr(tmpsweep, "usage_percent", lambda *a, **k: usage["pct"])
    monkeypatch.setattr(
        tmpsweep, "send_notify", lambda text, *a, **k: sent.append(text)
    )
    argv = ["--root", str(root), "--notify", "--force"]

    freed["bytes"] = 10 * 2**20
    assert tmpsweep.main(argv) == 0
    assert sent == []

    freed["bytes"] = 600 * 2**20
    assert tmpsweep.main(argv) == 0
    assert len(sent) == 1

    freed["bytes"] = 10 * 2**20
    usage["pct"] = 75.0
    assert tmpsweep.main(argv) == 0
    assert len(sent) == 2
    assert tmpsweep.main(argv) == 0  # still over, <6h later: no repeat
    assert len(sent) == 2


# --------------------------------------------------------------------------
# Added
# --------------------------------------------------------------------------


def test_containers_are_kept_and_their_children_swept(tmpsweep, root):
    """added: pytest-of-<user>, claude-<uid>/<slug>/ and claude_bot_files are
    containers -- the dir stays (the bot needs claude_bot_files), stale
    children go, fresh children stay."""
    user = os.environ.get("USER") or __import__("getpass").getuser()
    pyt = root / f"pytest-of-{user}"
    old_run = _stale_dir(pyt, "pytest-1")
    new_run = pyt / "pytest-2"
    new_run.mkdir()
    (new_run / "x").write_text("fresh")

    cl = root / f"claude-{os.getuid()}"
    old_sess = _stale_dir(cl / "proj-slug", "old-session")
    new_sess = cl / "proj-slug" / "new-session"
    new_sess.mkdir()
    (new_sess / "t").write_text("fresh")

    bot = root / "claude_bot_files"
    bot.mkdir()
    _age(bot, 48)

    tmpsweep.sweep(root)

    assert pyt.is_dir() and not old_run.exists() and new_run.exists()
    assert cl.is_dir() and not old_sess.exists() and new_sess.exists()
    assert bot.is_dir()


def test_quiet_tmp_and_recent_sweep_does_nothing(tmpsweep, root, monkeypatch):
    """added: the 15-min timer sweeps only when /tmp is >70% or the last
    sweep was >=23h ago; under 70% right after a sweep it leaves stale
    entries alone."""
    usage = {"pct": 50.0}
    monkeypatch.setattr(tmpsweep, "usage_percent", lambda *a, **k: usage["pct"])
    monkeypatch.setattr(tmpsweep, "send_notify", lambda *a, **k: None)
    assert tmpsweep.main(["--root", str(root), "--force"]) == 0  # records a sweep

    stale = _stale_dir(root, "stale")
    assert tmpsweep.main(["--root", str(root)]) == 0
    assert stale.exists()

    usage["pct"] = 75.0
    assert tmpsweep.main(["--root", str(root)]) == 0
    assert not stale.exists()


def test_agent_job_run_sh_sets_tmpdir_under_job_dir(bgjob, tmp_path, monkeypatch):
    """added: agent (prompt) jobs -- incl. every pump stage -- get TMPDIR and
    CLAUDE_CODE_TMPDIR inside their own job dir as well, not just --cmd jobs."""
    jobs, ws = tmp_path / "jobs", tmp_path / "ws"
    jobs.mkdir()
    ws.mkdir()
    monkeypatch.setattr(bgjob, "JOBS_ROOT", jobs)
    monkeypatch.setattr(bgjob, "load_profiles", lambda: {"general": str(ws)})
    monkeypatch.setattr(bgjob, "load_extra_dirs", lambda slug: [])
    monkeypatch.setattr(bgjob, "live_jobs", lambda: [])
    monkeypatch.setattr(bgjob, "_venv_python", lambda: sys.executable)
    monkeypatch.setattr(
        bgjob.shutil, "which", lambda n: "/usr/bin/bwrap" if n == "bwrap" else "/bin/sh"
    )

    class R:
        returncode = 0
        stderr = ""
        stdout = ""

    monkeypatch.setattr(bgjob.subprocess, "run", lambda cmd, **kw: R())
    args = type(
        "A",
        (),
        {
            "prompt": "do it",
            "profile": "general",
            "name": "t",
            "allow_dir": None,
            "model": "sonnet",
            "quiet": True,
        },
    )()
    assert bgjob.cmd_run(args) == 0
    run_sh = next(jobs.glob("*/run.sh"))
    text = run_sh.read_text()
    tmp = str(run_sh.parent / "tmp")
    assert "TMPDIR" in text and "CLAUDE_CODE_TMPDIR" in text, text
    for line in text.splitlines():
        if "export" in line and "TMPDIR" in line:
            assert tmp in line, line
            break
    else:
        raise AssertionError(f"no export of TMPDIR in run.sh:\n{text}")
