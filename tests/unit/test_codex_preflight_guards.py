"""Guards for the host-side Codex sandbox probe."""

from pathlib import Path
from subprocess import CompletedProcess

import pytest

from src.model_router import job_codex
from src.model_router.job import JobSpec
from src.model_router.routes import Route


def _spec(tmp_path: Path) -> JobSpec:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    extras = (tmp_path / "first", tmp_path / "second")
    for root in extras:
        root.mkdir()
    job = tmp_path / "job"
    job.mkdir()
    return JobSpec(
        purpose="background_job",
        name="disposable-preflight",
        prompt="probe",
        model="openai-codex/gpt-6-sol",
        route=Route("openai_codex", "openai", "openai-codex/gpt-6-sol"),
        working_directory=workspace,
        full_access=False,
        unrestricted=False,
        profile="gymbro",
        extra_dirs=extras,
        session_file=job / "session_id",
    )


def test_preflight_checks_each_explicit_write_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _spec(tmp_path)
    seen = []

    def fake_run(argv, **kwargs):
        seen.append(argv)
        return CompletedProcess(argv, 0, "native Codex sandbox preflight ok\n", "")

    monkeypatch.setattr(job_codex.subprocess, "run", fake_run)
    job_codex.preflight_codex_job(spec, "codex")
    assert len(seen) == 1
    assert all(str(root) in seen[0] for root in spec.extra_dirs)
    assert "for extra in extras:" in job_codex._PREFLIGHT_SCRIPT


def test_preflight_requires_probe_success_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        job_codex.subprocess,
        "run",
        lambda argv, **kwargs: CompletedProcess(argv, 0, "", ""),
    )
    with pytest.raises(RuntimeError, match="no success marker"):
        job_codex.preflight_codex_job(_spec(tmp_path), "codex")
