"""Native Codex job preflight fails before a model turn on a bad sandbox."""

import asyncio
import os
from dataclasses import replace
from pathlib import Path
from subprocess import CompletedProcess

import pytest

from src.agent_backends.codex_sandbox import filesystem_policy
from src.model_router.job import BotLink, JobSpec, run_job
from src.model_router.job_codex import preflight_codex_job
from src.model_router.routes import Route


def _spec(tmp_path: Path) -> JobSpec:
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    extra = tmp_path / "allowed"
    extra.mkdir()
    job = tmp_path / "job"
    job.mkdir()
    return JobSpec(
        purpose="background_job",
        name="disposable-gymbro-probe",
        prompt="probe",
        model="openai-codex/gpt-6-sol",
        route=Route("openai_codex", "openai", "openai-codex/gpt-6-sol"),
        working_directory=cwd,
        full_access=False,
        unrestricted=False,
        profile="gymbro",
        extra_dirs=(extra,),
        session_file=job / "session_id",
    )


def test_preflight_uses_native_profile_and_disposable_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.model_router import job_codex

    spec = _spec(tmp_path)
    seen = []

    def fake_run(argv, **kwargs):
        seen.append((argv, kwargs))
        return CompletedProcess(argv, 0, "native Codex sandbox preflight ok", "")

    monkeypatch.setattr(job_codex.subprocess, "run", fake_run)
    preflight_codex_job(spec, "codex")
    assert len(seen) == 1
    argv, kwargs = seen[0]
    assert argv[:2] == ["codex", "sandbox"]
    assert argv[argv.index("-P") + 1] == "devvy_confined"
    assert str(spec.working_directory) in argv
    assert str(spec.extra_dirs[0]) in argv
    assert "/run/user" in " ".join(argv)
    assert kwargs["timeout"] <= 15
    assert "DBUS_SESSION_BUS_ADDRESS" not in kwargs["env"]
    assert "XDG_RUNTIME_DIR" not in kwargs["env"]
    assert not list(spec.session_file.parent.glob("codex-preflight-*"))


def test_failed_preflight_reports_namespace_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.model_router import job_codex

    monkeypatch.setattr(
        job_codex.subprocess,
        "run",
        lambda argv, **kwargs: CompletedProcess(
            argv, 1, "", "bwrap: No permissions to create a new namespace\n"
        ),
    )
    with pytest.raises(RuntimeError, match="No permissions to create a new namespace"):
        preflight_codex_job(_spec(tmp_path), "codex")


@pytest.mark.parametrize("root", [Path("/run/user/1000"), Path("/run")])
def test_bus_root_cannot_be_authorized(root: Path, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="/run/user"):
        filesystem_policy(
            cwd=tmp_path,
            extra_dirs=(root,),
            full_access=False,
            denied=(),
        )


def test_real_gymbro_host_turn_in_disposable_workspace(tmp_path: Path) -> None:
    """Run only on the host; the pump's outer box cannot nest namespaces."""
    if os.environ.get("DEVVY_NATIVE_CODEX_HOST_PROBE") != "1":
        pytest.skip("requires host-side Codex namespace and one authorized model turn")
    from src.profiles.registry import load_profile_registry

    config = Path(__file__).parents[2] / "config" / "profiles.yaml"
    gymbro = load_profile_registry(config, validate_paths=False).get_by_slug("gymbro")
    assert gymbro is not None and gymbro.sandbox is not False
    assert not gymbro.unrestricted and gymbro.extra_dirs
    spec = _spec(tmp_path)
    assert spec.working_directory != gymbro.working_dir
    assert all(root not in gymbro.extra_dirs for root in spec.extra_dirs)
    preflight_codex_job(spec, "codex")
    spec = replace(
        spec,
        prompt=(
            "Write the single word ready to probe-result.txt in the current "
            "workspace, then reply done. Do not access other directories."
        ),
    )
    assert asyncio.run(run_job(spec, BotLink("http://127.0.0.1:1", None))) == 0
    assert (spec.working_directory / "probe-result.txt").read_text().strip() == "ready"
