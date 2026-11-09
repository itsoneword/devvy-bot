"""Fail-closed launch guards for native Codex background jobs."""

from __future__ import annotations

import argparse
import importlib.util
import subprocess
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest


def _bgjob():
    path = Path(__file__).parents[2] / "scripts" / "bgjob"
    loader = SourceFileLoader("bgjob_native_guard", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_native_launch_requires_resolved_sandbox_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bgjob = _bgjob()
    monkeypatch.setattr(
        bgjob,
        "_registry",
        lambda: {
            "confined": {"sandbox": True, "unrestricted": False},
            "default_off": {"sandbox": False, "unrestricted": False},
            "unrestricted": {"sandbox": True, "unrestricted": True},
        },
    )
    assert bgjob.confined_codex_profile("confined")
    assert not bgjob.confined_codex_profile("default_off")
    assert not bgjob.confined_codex_profile("unrestricted")


@pytest.mark.parametrize(
    "probe",
    [
        subprocess.CompletedProcess([], 1, "", "invalid route"),
        subprocess.CompletedProcess([], 0, "{}", ""),
    ],
)
def test_route_probe_error_stops_launch(
    monkeypatch: pytest.MonkeyPatch, probe: subprocess.CompletedProcess[str]
) -> None:
    bgjob = _bgjob()
    monkeypatch.setattr(bgjob.subprocess, "run", lambda *args, **kwargs: probe)
    assert (
        bgjob.job_harness(
            sys.executable,
            Path("/unused/agentjob"),
            argparse.Namespace(route=None, model=None),
        )
        == ""
    )


def test_failed_route_probe_generates_no_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bgjob = _bgjob()
    monkeypatch.setattr(bgjob, "JOBS_ROOT", tmp_path / "jobs")
    monkeypatch.setattr(bgjob, "load_profiles", lambda: {"general": str(tmp_path)})
    monkeypatch.setattr(bgjob, "live_jobs", lambda: [])
    monkeypatch.setattr(bgjob, "refuse_on_limit", lambda *a, **k: None)
    monkeypatch.setattr(bgjob, "job_harness", lambda *args: "")
    monkeypatch.setattr(bgjob.shutil, "which", lambda name: "/bin/sh")
    args = argparse.Namespace(
        prompt="probe",
        profile="general",
        name="probe",
        model=None,
        route=None,
        allow_dir=None,
        quiet=True,
    )
    with pytest.raises(SystemExit, match="job route probe failed"):
        bgjob.cmd_run(args)
    assert not list((tmp_path / "jobs").glob("*/run.sh"))


def test_native_preflight_checks_escape_from_memory_leaf() -> None:
    from src.model_router.job_codex import _PREFLIGHT_SCRIPT

    assert "for p in relative.parents" in _PREFLIGHT_SCRIPT
    assert "cg / relative / 'cgroup.procs'" not in _PREFLIGHT_SCRIPT
    assert "user manager socket reachable" in _PREFLIGHT_SCRIPT


def test_preflight_failure_survives_summary_and_reaches_jobs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bgjob = _bgjob()
    job_id = "20261102-000000-disposable-probe"
    job = tmp_path / job_id
    job.mkdir()
    monkeypatch.setattr(bgjob, "JOBS_ROOT", tmp_path)
    (job / "meta.json").write_text('{"id":"' + job_id + '","name":"probe"}')
    (job / "output.md").write_text("")
    (job / "stderr.log").write_text(
        "agentjob preflight failed: bwrap: No permissions to create a new namespace\n"
        "cleanup line one\ncleanup line two\ncleanup line three\ncleanup line four\n"
    )
    summary = bgjob.write_summary(job_id)
    assert "No permissions to create a new namespace" in summary
    assert summary.strip() == (job / "summary.md").read_text().strip()


def test_native_profile_rejects_root_write_grant(tmp_path: Path) -> None:
    from src.agent_backends.codex_sandbox import filesystem_policy

    with pytest.raises(ValueError):
        filesystem_policy(
            cwd=tmp_path, extra_dirs=(Path("/run"),), full_access=False, denied=()
        )
