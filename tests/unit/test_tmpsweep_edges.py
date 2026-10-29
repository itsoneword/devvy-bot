"""dv-1a02 edges beyond the pinned scenarios in test_tmpsweep.py: other
owners, symlinks, dry run, a dead notifier, the bash box's TMPDIR and the
agent job's tmp cleanup."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from tests.unit.test_tmpsweep import _age, _load, _stale_dir

_SCRIPTS = Path(__file__).parents[2] / "scripts"


@pytest.fixture
def tmpsweep():
    return _load("tmpsweep_edges", _SCRIPTS / "tmpsweep")


@pytest.fixture
def root(tmp_path, monkeypatch) -> Path:
    r = tmp_path / "root"
    r.mkdir()
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    return r


def test_other_owner_is_untouched(tmpsweep, root):
    d = _stale_dir(root, "theirs")
    res = tmpsweep.sweep(root, uid=os.getuid() + 1)
    assert d.exists() and res.removed == []


def test_symlink_is_removed_target_kept(tmpsweep, root, tmp_path):
    target = tmp_path / "outside"
    target.mkdir()
    (target / "keep.txt").write_text("x")
    _age(target, 30)
    link = root / "link"
    link.symlink_to(target)
    _age(link, 30)
    tmpsweep.sweep(root)
    assert not link.is_symlink() and (target / "keep.txt").exists()


def test_dry_run_removes_nothing(tmpsweep, root):
    d = _stale_dir(root, "old")
    res = tmpsweep.sweep(root, dry_run=True)
    assert d.exists() and res.freed_bytes > 0 and len(res.removed) == 1


def test_fully_stale_slug_goes_container_stays(tmpsweep, root):
    cl = root / f"claude-{os.getuid()}"
    slug = _stale_dir(cl, "proj-slug")
    _age(cl, 20)
    tmpsweep.sweep(root)
    assert cl.is_dir() and not slug.exists()


def test_notify_failure_does_not_fail_main(tmpsweep, root, monkeypatch):
    monkeypatch.setattr(tmpsweep, "AGENTCTL", Path("/nonexistent/agentctl"))
    monkeypatch.setattr(tmpsweep, "usage_percent", lambda *a, **k: 90.0)
    assert tmpsweep.main(["--root", str(root), "--notify", "--force"]) == 0


def test_report_line_buckets_tmp_dbs(tmpsweep, root):
    for i in range(3):
        f = root / f"tmp{i}abc.db"
        f.write_bytes(b"x" * 8192)
        _age(f, 13)
    res = tmpsweep.sweep(root)
    line = tmpsweep.report_line("/tmp", res, 12.0)
    assert "3 entries" in line and "tmp*.db" in line and "now 12%" in line


def test_bash_box_points_tmpdir_at_private_tmp(tmp_path, monkeypatch):
    from src.security.bash_box import build_bwrap_argv

    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    argv = build_bwrap_argv(write_dirs=[ws], bwrap_bin="/usr/bin/bwrap")
    i = argv.index("--setenv")
    assert argv[i : i + 3] == ["--setenv", "TMPDIR", "/tmp"]
    assert argv.index("--tmpfs") < i  # the box's own /tmp exists


def test_agent_job_removes_its_tmp_last(tmp_path, monkeypatch):
    bgjob = _load("bgjob_edges", _SCRIPTS / "bgjob")
    jobs, ws = tmp_path / "jobs", tmp_path / "ws"
    jobs.mkdir()
    ws.mkdir()
    monkeypatch.setattr(bgjob, "JOBS_ROOT", jobs)
    monkeypatch.setattr(bgjob, "load_profiles", lambda: {"general": str(ws)})
    monkeypatch.setattr(bgjob, "load_extra_dirs", lambda slug: [])
    monkeypatch.setattr(bgjob, "live_jobs", lambda: [])
    monkeypatch.setattr(bgjob, "_venv_python", lambda: sys.executable)
    monkeypatch.setattr(bgjob.shutil, "which", lambda n: "/usr/bin/bwrap")

    class R:
        returncode = 0
        stderr = ""
        stdout = ""

    monkeypatch.setattr(bgjob.subprocess, "run", lambda cmd, **kw: R())
    monkeypatch.setattr(bgjob.subprocess, "Popen", lambda *a, **k: None)
    args = type(
        "A",
        (),
        {
            "prompt": "do it",
            "profile": "general",
            "name": "t",
            "allow_dir": None,
            "model": "sonnet",
            "quiet": False,
        },
    )()
    assert bgjob.cmd_run(args) == 0
    run_sh = next(jobs.glob("*/run.sh"))
    lines = run_sh.read_text().strip().splitlines()
    assert lines[-1] == f"rm -rf {run_sh.parent / 'tmp'}"
    # the tmp dir is made before the box starts
    mk = next(i for i, ln in enumerate(lines) if ln.startswith("mkdir -m 700"))
    box = next(i for i, ln in enumerate(lines) if "bwrap" in ln)
    assert mk < box


# --------------------------------------------------------------------------
# dv-4b6f: docker volumes. The approved scenario was "daily tmpsweep prunes
# anonymous volumes; works when docker is missing". The owner answered
# 26.10 15:25 «Давай пока не будем автоматизировать удаления»: no automated
# Docker deletion for now. So the daily sweep must NOT prune or remove any
# docker volume, and must keep working when docker is absent.
# --------------------------------------------------------------------------


def _fake_docker(bin_dir: Path, log: Path) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    docker = bin_dir / "docker"
    docker.write_text(
        f'#!/bin/sh\necho "$@" >> "{log}"\necho "Total reclaimed space: 1GB"\n'
    )
    docker.chmod(0o755)


def _record_subprocess(tmpsweep, monkeypatch) -> list[list[str]]:
    calls: list[list[str]] = []
    real = tmpsweep.subprocess.run

    def run(cmd, *a, **k):
        calls.append(
            [str(c) for c in (cmd if isinstance(cmd, (list, tuple)) else [cmd])]
        )
        return real(cmd, *a, **k)

    monkeypatch.setattr(tmpsweep.subprocess, "run", run)
    return calls


def test_daily_sweep_does_not_delete_docker_volumes(
    tmpsweep, root, tmp_path, monkeypatch
):
    """dv-4b6f scenario 5, per the owner's answer: the daily (due) sweep runs,
    sweeps /tmp, and calls no `docker volume prune` / `docker volume rm` /
    `docker system prune` / `docker builder prune`."""
    log = tmp_path / "docker.log"
    _fake_docker(tmp_path / "bin", log)
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(tmpsweep, "usage_percent", lambda *a, **k: 10.0)
    monkeypatch.setattr(tmpsweep, "send_notify", lambda *a, **k: None)
    calls = _record_subprocess(tmpsweep, monkeypatch)
    stale = _stale_dir(root, "stale")

    assert tmpsweep.main(["--root", str(root), "--notify"]) == 0  # no state: due

    assert not stale.exists(), "the daily sweep did not run"
    docker_calls = [c for c in calls if c and Path(c[0]).name == "docker"]
    logged = log.read_text().splitlines() if log.exists() else []
    deleting = [
        " ".join(c[1:])
        for c in docker_calls
        if "prune" in c or ("rm" in c and "volume" in c)
    ] + [
        ln
        for ln in logged
        if "prune" in ln.split() or ln.split()[:2] == ["volume", "rm"]
    ]
    assert deleting == [], f"tmpsweep deletes docker resources: {deleting}"


def test_daily_sweep_works_without_docker(tmpsweep, root, tmp_path, monkeypatch):
    """dv-4b6f scenario 5: no docker binary on PATH -> the sweep still runs
    and exits 0."""
    empty = tmp_path / "nobin"
    empty.mkdir()
    py = Path(sys.executable).parent
    monkeypatch.setenv("PATH", f"{empty}{os.pathsep}{py}")
    monkeypatch.setattr(tmpsweep, "usage_percent", lambda *a, **k: 10.0)
    monkeypatch.setattr(tmpsweep, "send_notify", lambda *a, **k: None)
    stale = _stale_dir(root, "stale")

    assert tmpsweep.main(["--root", str(root)]) == 0
    assert not stale.exists()
