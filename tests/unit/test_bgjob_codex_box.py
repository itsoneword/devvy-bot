"""dv-d63d phase 3: the bgjob box of a Codex job.

bgjob resolves the job route before it builds the bwrap box (``agentjob
--print-route``, analysis "Codex jobs"): a Codex job also needs ``~/.codex``
writable (sessions, rollouts, auth refresh); a Claude job does not get it.
A pump stage (``--cwd <worktree>``) writes only its job dir, its worktree
and the CLI homes -- never the live checkout.
"""

from __future__ import annotations

import importlib.util
import re
import shutil
import subprocess
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path
from typing import Any, List, Tuple

import pytest

_REPO = Path(__file__).parents[2]
_BGJOB_PATH = _REPO / "scripts" / "bgjob"

CODEX_ROUTE = (
    '{"harness": "openai_codex", "provider": "openai", '
    '"model": "openai-codex/gpt-6-sol"}'
)
CLAUDE_ROUTE = '{"harness": "claude", "provider": "anthropic_sub", "model": "sonnet"}'


@pytest.fixture
def bgjob():
    loader = SourceFileLoader("bgjob_codex_under_test", str(_BGJOB_PATH))
    spec = importlib.util.spec_from_loader("bgjob_codex_under_test", loader)
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def _launch(
    bgjob: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    route_json: str,
    *argv: str,
    profile: str = "general",
    extra_dirs: List[str] | None = None,
    quiet: bool = True,
) -> Tuple[str, Path, List[Any]]:
    """`bgjob run <argv>` through the real parser; nothing launched. The
    route probe answers ``route_json``. Returns (agentjob line, job dir,
    every subprocess argv)."""
    jobs, ws = tmp_path / "jobs", tmp_path / "ws"
    jobs.mkdir()
    ws.mkdir(exist_ok=True)
    real_bwrap = shutil.which("bwrap") or "/usr/bin/bwrap"
    monkeypatch.setattr(bgjob, "JOBS_ROOT", jobs)
    monkeypatch.setattr(bgjob, "load_profiles", lambda: {profile: str(ws)})
    monkeypatch.setattr(bgjob, "load_extra_dirs", lambda slug: extra_dirs or [])
    monkeypatch.setattr(bgjob, "live_jobs", lambda: [])
    monkeypatch.setattr(bgjob, "refuse_on_limit", lambda *a, **k: None)
    monkeypatch.setattr(bgjob, "find_duplicate", lambda *a: None)
    monkeypatch.setattr(bgjob, "_venv_python", lambda: sys.executable)
    monkeypatch.setattr(
        bgjob.shutil, "which", lambda n: real_bwrap if n == "bwrap" else "/bin/sh"
    )
    calls: List[Any] = []

    def _answer(cmd: Any) -> str:
        flat = cmd if isinstance(cmd, str) else " ".join(map(str, cmd))
        return route_json if "--print-route" in flat else ""

    def fake_run(cmd: Any, **kw: Any) -> Any:
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, _answer(cmd), "")

    def fake_check_output(cmd: Any, **kw: Any) -> Any:
        calls.append(cmd)
        out = _answer(cmd)
        text = kw.get("text") or kw.get("universal_newlines") or kw.get("encoding")
        return out if text else out.encode()

    monkeypatch.setattr(bgjob.subprocess, "run", fake_run)
    monkeypatch.setattr(bgjob.subprocess, "check_output", fake_check_output)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "bgjob",
            "run",
            "do it",
            "--profile",
            profile,
            *(["--quiet"] if quiet else []),
            *argv,
        ],
    )
    assert bgjob.main() == 0
    run_sh = next(jobs.glob("*/run.sh"))
    line = next(
        ln
        for ln in run_sh.read_text().splitlines()
        if "agentjob" in ln and "--prompt-file" in ln
    )
    return line, run_sh.parent, calls


@pytest.mark.parametrize("route", [CLAUDE_ROUTE, CODEX_ROUTE])
def test_owner_user_cwd_keeps_configured_extra_dir_writable_in_job_box(
    bgjob, tmp_path, monkeypatch, route
):
    user_cwd = tmp_path / "ws" / "users" / "1001"
    user_cwd.mkdir(parents=True)
    extra = tmp_path / "obsidian"
    extra.mkdir()
    line, _, _ = _launch(
        bgjob,
        tmp_path,
        monkeypatch,
        route,
        "--cwd",
        str(user_cwd),
        extra_dirs=[str(extra)],
    )
    if "openai_codex" in route:
        # Confined Codex runs natively (dv-a8e4): no bwrap binds, the dir
        # reaches the permission profile as --allow-dir.
        assert "bwrap" not in line
        assert f"--allow-dir {extra}" in line
    else:
        assert str(extra) in _binds(line)


def test_pump_worktree_cwd_cannot_inherit_profile_extra_dir(
    bgjob, tmp_path, monkeypatch
):
    pump = tmp_path / "pump-worktrees" / "dv-5a35"
    pump.mkdir(parents=True)
    extra = tmp_path / "obsidian"
    extra.mkdir()
    line, _, _ = _launch(
        bgjob,
        tmp_path,
        monkeypatch,
        CODEX_ROUTE,
        "--cwd",
        str(pump),
        extra_dirs=[str(extra)],
    )
    assert str(extra) not in _binds(line)


def _binds(line: str) -> List[str]:
    """The rw binds of the bwrap box on the agentjob line."""
    return [m.group(1).strip("'\"") for m in re.finditer(r"--bind (\S+) \1", line)]


CODEX_HOME = str(Path.home() / ".codex")
CLAUDE_HOME = str(Path.home() / ".claude")


def test_codex_job_box_makes_the_codex_home_writable(
    bgjob: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Confined Codex uses its native sandbox; the host process can reach its home."""
    line, jobdir, calls = _launch(bgjob, tmp_path, monkeypatch, CODEX_ROUTE)

    assert any(
        "--print-route" in (c if isinstance(c, str) else " ".join(map(str, c)))
        for c in calls
    ), "bgjob must resolve the job route before building the box"
    assert "bwrap" not in line
    assert "--native-preflight" in line
    assert CODEX_HOME not in _binds(line)
    assert "$HOME/.npm-global/bin" in (jobdir / "run.sh").read_text()


def test_claude_job_box_does_not_get_the_codex_home(
    bgjob: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Guard: a Claude job keeps today's write set (no ~/.codex)."""
    line, _jobdir, _calls = _launch(bgjob, tmp_path, monkeypatch, CLAUDE_ROUTE)

    binds = _binds(line)
    assert CLAUDE_HOME in binds
    assert CODEX_HOME not in binds


def test_codex_pump_stage_box_writes_only_its_worktree(
    bgjob: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A confined Codex pump stage uses its worktree as the native write root."""
    worktree = tmp_path / "pump-worktrees" / "dv-x"
    worktree.mkdir(parents=True)

    line, jobdir, _calls = _launch(
        bgjob,
        tmp_path,
        monkeypatch,
        CODEX_ROUTE,
        "--cwd",
        str(worktree),
        "--route",
        "pump_implement",
    )

    assert "--route pump_implement" in line
    assert "bwrap" not in line
    assert "--native-preflight" in line
    assert f"--cwd '{worktree.resolve()}'" in line
    assert "--allow-dir" not in line
    assert str(tmp_path / "ws") not in line
    assert str(_REPO) not in _binds(line)


def test_real_gymbro_profile_selects_native_preflight_and_model_turn(
    bgjob: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Use the deployed profile definition, but launch only into disposable space."""
    from src.profiles.registry import load_profile_registry

    config = _REPO / "config" / "profiles.yaml"
    if not config.exists():
        pytest.skip("host GymBro profile config is unavailable")
    gymbro = load_profile_registry(config, validate_paths=False).get_by_slug("gymbro")
    assert gymbro is not None
    assert gymbro.sandbox is not False and not gymbro.unrestricted
    assert gymbro.extra_dirs

    line, jobdir, _ = _launch(
        bgjob, tmp_path, monkeypatch, CODEX_ROUTE, profile=gymbro.slug
    )
    script = (jobdir / "run.sh").read_text().replace(str(tmp_path), "/TMP")
    assert "bwrap" not in line, "named-profile Codex must create its native namespace"
    assert "preflight" in script.lower()
    assert script.index("preflight") < script.index("--prompt-file")
    assert "--profile gymbro" in line
    assert str(gymbro.working_dir) not in line


def test_allow_dir_is_passed_to_codex_and_cwd_excludes_profile_workspace(
    bgjob: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    allowed = tmp_path / "explicit"
    allowed.mkdir()
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    profile_extra = tmp_path / "profile-extra"
    profile_extra.mkdir()
    line, _jobdir, _ = _launch(
        bgjob,
        tmp_path,
        monkeypatch,
        CODEX_ROUTE,
        "--cwd",
        str(worktree),
        "--allow-dir",
        str(allowed),
        extra_dirs=[str(profile_extra)],
    )
    assert "bwrap" not in line
    assert str(allowed.resolve()) in line, "native permission profile needs --allow-dir"
    assert str(profile_extra) not in line
    assert str(tmp_path / "ws") not in line


def test_failed_preflight_reports_cause_without_starting_model(
    bgjob: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    line, jobdir, _ = _launch(bgjob, tmp_path, monkeypatch, CODEX_ROUTE, quiet=False)
    script = (jobdir / "run.sh").read_text().replace(str(tmp_path), "/TMP")
    assert "preflight" in script.lower()
    assert script.index("preflight") < script.index("--prompt-file")
    assert "exit_code" in script
    assert "notify" in script or "summary.md" in script
    assert "stderr.log" in script
    assert (
        "stderr.log" in script[script.index("rc=$?") :]
    ), "a failed preflight must put its stderr cause in the Jobs result"


def test_policy_and_job_wrapper_survive_native_codex_launch(
    bgjob: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pre_log = tmp_path / "pre.log"
    line, jobdir, calls = _launch(
        bgjob,
        tmp_path,
        monkeypatch,
        CODEX_ROUTE,
        "--pre-cmd",
        "true",
        "--pre-log",
        str(pre_log),
        quiet=False,
    )
    script = (jobdir / "run.sh").read_text().replace(str(tmp_path), "/TMP")
    assert "bwrap" not in line
    assert "bwrap" in script[: script.index("--prompt-file")]
    assert "memory.max" in script
    assert "exit_code" in script
    assert "notify" in script or "summary.md" in script
    assert "timeout" in script
    unit = next(c for c in calls if isinstance(c, list) and c[0] == "systemd-run")
    assert any(str(a).startswith("--property=MemoryMax=") for a in unit)
    assert "--property=Delegate=yes" in unit
    assert "--property=NoNewPrivileges=yes" in unit
    from src.agent_backends.codex_cli import policy_hook_args
    from src.model_router.job import JobSpec
    from src.model_router.job_codex import codex_job_argv
    from src.model_router.routes import Route

    spec = JobSpec(
        purpose="background_job",
        name="disposable-gymbro-probe",
        prompt="probe",
        model="openai-codex/gpt-6-sol",
        route=Route("openai_codex", "openai", "openai-codex/gpt-6-sol"),
        working_directory=tmp_path,
        full_access=False,
        unrestricted=False,
        profile="gymbro",
    )
    argv = codex_job_argv(spec, "codex", tmp_path / "out.md", None)
    assert 'default_permissions="devvy_confined"' in argv
    assert any(a.startswith("hooks.PreToolUse=") for a in argv)
    assert policy_hook_args(3720)[0] in argv
