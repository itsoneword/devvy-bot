"""dv-fe4d phase 1: the portable job launcher behind scripts/bgjob.

Linux keeps the systemd unit by default; ``DEVVY_LAUNCHER=process`` runs the
same job detached under a small supervisor (no systemd, no cgroup).
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import time
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).parents[2] / "scripts"


def _load(name: str):
    loader = SourceFileLoader(f"{name}_launcher_test", str(_SCRIPTS / name))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def _args(**kw) -> argparse.Namespace:
    base = dict(
        prompt=None,
        cmd=None,
        profile="general",
        name="t",
        allow_dir=None,
        model="sonnet",
        quiet=True,
        force=True,
        cwd=None,
        task=None,
        queue=False,
        batch=None,
        route=None,
        pre_cmd=None,
        pre_log=None,
        setting_sources=None,
    )
    base.update(kw)
    return argparse.Namespace(**base)


@pytest.fixture
def jobs(tmp_path, monkeypatch):
    root = tmp_path / "jobs"
    root.mkdir()
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setenv("BGJOB_ROOT", str(root))
    monkeypatch.setenv("DEVVY_LAUNCHER", "process")
    monkeypatch.setenv("BGJOB_MAX_CONCURRENT", "5")
    return root, ws


def _bgjob(monkeypatch, ws):
    mod = _load("bgjob")
    monkeypatch.setattr(mod, "load_profiles", lambda: {"general": str(ws)})
    return mod


def _one_job(root: Path) -> str:
    dirs = [p for p in root.iterdir() if p.is_dir()]
    assert len(dirs) == 1, dirs
    return dirs[0].name


def _wait_for(pred, timeout: float = 20.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.2)
    return False


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:  # a zombie still answers kill(0)
        stat = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
        return stat != "Z"
    except OSError:
        return True


def _listing(bgjob, capsys) -> str:
    capsys.readouterr()
    bgjob.cmd_list(argparse.Namespace(compact=True, limit=None))
    return capsys.readouterr().out


def test_linux_default_still_starts_a_systemd_unit_in_bwrap(
    tmp_path, monkeypatch
) -> None:
    """DEVVY_LAUNCHER unset on a systemd host: the same systemd-run argv and a
    bwrap-boxed run.sh as before the launcher seam existed."""
    monkeypatch.delenv("DEVVY_LAUNCHER", raising=False)
    monkeypatch.setenv("BGJOB_ROOT", str(tmp_path))
    bgjob = _load("bgjob")
    monkeypatch.setattr(bgjob, "load_profiles", lambda: {"general": str(tmp_path)})
    # Not the host's profiles.yaml: its dump runs through the stubbed run below.
    monkeypatch.setattr(bgjob, "_registry", lambda: {})
    monkeypatch.setattr(bgjob, "live_jobs", lambda: [])
    monkeypatch.setattr(bgjob.shutil, "which", lambda name: f"/usr/bin/{name}")
    seen: list = []

    class R:
        returncode = 0
        stderr = ""

        def __init__(self, stdout: str) -> None:
            self.stdout = stdout

    # The route probe must answer: an empty answer aborts the launch (dv-a8e4).
    route = '{"harness": "claude", "provider": "anthropic_sub", "model": "sonnet"}'

    monkeypatch.setattr(
        bgjob.subprocess,
        "run",
        lambda cmd, **kw: seen.append(cmd)
        or R(route if "--print-route" in cmd else "active"),
    )
    bgjob.cmd_run(_args(prompt="do it"))

    started = [c for c in seen if c and c[0] == "systemd-run"]
    assert len(started) == 1
    assert any(a.startswith("--unit=bgjob-") for a in started[0])
    run_sh = next(tmp_path.glob("*/run.sh")).read_text()
    assert "--ro-bind / /" in run_sh and "bwrap" in run_sh


def test_process_launcher_runs_detached_and_saves_rc(jobs, monkeypatch, capsys):
    root, ws = jobs
    bgjob = _bgjob(monkeypatch, ws)
    marker = ws / "marker"
    bgjob.cmd_run(
        _args(cmd=f"echo from-job; sleep 2; echo done > {marker}; exit 3", cwd=str(ws))
    )
    jid = _one_job(root)
    d = root / jid

    # Detached: cmd_run returned while the command is still sleeping.
    assert not (d / "exit_code").exists()
    assert "RUNNING" in _listing(bgjob, capsys)

    assert _wait_for(lambda: (d / "exit_code").exists())
    assert (d / "exit_code").read_text().strip() == "3"
    assert marker.exists()
    assert _wait_for(lambda: "failed(rc=3)" in _listing(bgjob, capsys))

    capsys.readouterr()
    bgjob.cmd_output(argparse.Namespace(id=jid, tail=5, full=False))
    assert "from-job" in capsys.readouterr().out


def test_process_launcher_cancel_kills_the_process_group(jobs, monkeypatch, capsys):
    root, ws = jobs
    bgjob = _bgjob(monkeypatch, ws)
    child = ws / "child.pid"
    bgjob.cmd_run(_args(cmd=f"sleep 300 & echo $! > {child}; wait", cwd=str(ws)))
    jid = _one_job(root)
    assert _wait_for(lambda: child.exists() and child.read_text().strip())
    pid = int(child.read_text())
    assert _alive(pid)

    bgjob.cmd_cancel(argparse.Namespace(id=jid))
    assert _wait_for(lambda: not _alive(pid), timeout=15)
    assert "RUNNING" not in _listing(bgjob, capsys)


def test_job_over_memory_cap_is_reaped_as_oom(jobs, monkeypatch):
    """The supervisor kills the group over BGJOB_MEMORY_MAX and records
    ``outcome.json`` result=memcap (no exit_code)."""
    root, ws = jobs
    monkeypatch.setenv("BGJOB_MEMORY_MAX", "60M")
    bgjob = _bgjob(monkeypatch, ws)
    hog = 'python3 -c "b=bytearray(300*1024*1024); import time; time.sleep(120)"'
    bgjob.cmd_run(_args(cmd=hog, cwd=str(ws)))
    jid = _one_job(root)
    d = root / jid

    # soft cap: the supervisor samples every few seconds
    assert _wait_for(lambda: (d / "outcome.json").exists(), timeout=40)
    outcome = json.loads((d / "outcome.json").read_text())
    assert outcome["result"] == "memcap"
    assert not (d / "exit_code").exists()


def test_plain_exit_is_not_reported_as_oom(jobs, monkeypatch) -> None:
    """added: a normal fast job leaves outcome.json result=exit, no memcap."""
    root, ws = jobs
    bgjob = _bgjob(monkeypatch, ws)
    bgjob.cmd_run(_args(cmd="exit 0", cwd=str(ws)))
    jid = _one_job(root)
    d = root / jid
    assert _wait_for(lambda: (d / "outcome.json").exists())
    assert json.loads((d / "outcome.json").read_text())["result"] == "exit"
