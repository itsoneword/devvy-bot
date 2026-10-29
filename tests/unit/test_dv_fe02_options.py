"""dv-fe02 phase 3: what bot chats and jobs load from outside their project.

Owner answers 3.1 and 3.2 (2026-10-25, "as proposed"):

3.1  Bot chats and jobs no longer load the owner's ``~/.claude/CLAUDE.md`` nor
     the data root's parent ``CLAUDE.md`` (``~/Devvybot/CLAUDE.md``). The CLI's
     upward CLAUDE.md walk finds both from any cwd under ``$HOME`` even with
     ``setting_sources=["project"]`` (analysis H1a: 3335 + 653 + 94 = 4082
     chars measured on every chat). Fix: the CLI's ``claudeMdExcludes``
     setting, passed through the SDK ``settings`` option in BOTH existing
     builders (chat ``ClaudeSDKManager`` and ``job_options``). The paths are
     derived when the options are built, from ``$HOME`` and the data root
     (``DEVVY_DATA_ROOT``), never hardcoded, so another install or a test sees
     its own paths.

3.2  Pump stages load the ``project`` setting source only (no user plugins,
     skills, deviz SessionStart hook); bgjob / server-monitor jobs keep
     ``user`` + ``project``. One options builder: ``JobSpec.setting_sources``
     (default ``("user", "project")``) -> ``job_options``; plumbed through
     ``scripts/agentjob --setting-sources a,b`` and
     ``scripts/bgjob run --setting-sources`` (argparse dest
     ``setting_sources``); taskpump passes ``project`` (tested in
     ``tests/unit/test_scripts/test_dv_fe02_phase3.py``).

The effective ``--settings`` value is read the way the SDK builds it for the
CLI (``SubprocessCLITransport._build_settings_value``, which merges the chat's
``sandbox`` block in), so a JSON string and a settings-file path both count.
"""

from __future__ import annotations

import json
import sys
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from claude_agent_sdk._internal.transport.subprocess_cli import (
    SubprocessCLITransport,
)

from src.model_router.job import BotLink, JobSpec, job_options

REPO = Path(__file__).resolve().parents[2]


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """A home with the live layout: ~/.claude/CLAUDE.md, ~/Devvybot/CLAUDE.md,
    data root ~/Devvybot/data, a chat workspace under it."""
    home = tmp_path / "home"
    install = home / "Devvybot"
    data = install / "data"
    workspace = data / "workspaces" / "chef"
    for d in (home / ".claude", workspace):
        d.mkdir(parents=True, exist_ok=True)
    (home / ".claude" / "CLAUDE.md").write_text("Commit proactively\n")
    (install / "CLAUDE.md").write_text("The code lives in `devvy_bot/`.\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("DEVVY_DATA_ROOT", str(data))
    return {
        "home": home,
        "global": home / ".claude" / "CLAUDE.md",
        "install": install / "CLAUDE.md",
        "data": data,
        "workspace": workspace,
    }


def _effective_settings(options: Any) -> dict[str, Any]:
    """The --settings object the SDK hands the CLI for these options."""
    transport = SubprocessCLITransport(prompt="", options=options)
    value = transport._build_settings_value()
    assert value, "no --settings passed to the CLI (claudeMdExcludes missing)"
    value = value.strip()
    if value.startswith("{"):
        data = json.loads(value)
    else:
        data = json.loads(Path(value).read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def _excludes(options: Any) -> list[str]:
    excludes = _effective_settings(options).get("claudeMdExcludes")
    assert isinstance(excludes, list), f"claudeMdExcludes not a list: {excludes!r}"
    return [str(e) for e in excludes]


def _spec(cwd: Path, **extra: Any) -> JobSpec:
    return JobSpec(
        purpose="background_job",
        name="pump-dv-1-implement",
        prompt="do it",
        model="opus",
        working_directory=cwd,
        full_access=True,
        unrestricted=False,
        profile="devops",
        cli_path="/bin/claude",
        **extra,
    )


def _link() -> BotLink:
    return BotLink("http://127.0.0.1:1", "s")


# --------------------------------------------------------------------------
# Scenario: bot chats and jobs no longer load ~/.claude/CLAUDE.md or
# ~/Devvybot/CLAUDE.md
# --------------------------------------------------------------------------


def test_job_options_exclude_global_and_install_claude_md(
    fake_home: dict[str, Path],
) -> None:
    """Every job (bgjob default AND pump project-only) passes claudeMdExcludes
    with the owner's global file and the install dir's CLAUDE.md."""
    for spec in (
        _spec(fake_home["workspace"]),
        _spec(fake_home["workspace"], setting_sources=("project",)),
    ):
        excludes = _excludes(job_options(spec, _link()))
        assert str(fake_home["global"]) in excludes, excludes
        assert str(fake_home["install"]) in excludes, excludes


async def test_chat_options_exclude_global_and_install_claude_md(
    fake_home: dict[str, Path], tmp_path: Path
) -> None:
    """The chat builder passes the same excludes, and the merged --settings
    still carries the sandbox block (native sandbox off, dv-98f2)."""
    from src.claude.sdk_integration import ClaudeSDKManager
    from src.config.settings import Settings
    from src.security.denylist import ProtectedDirs, protected_dirs

    sys.path.insert(0, str(REPO / "tests" / "unit" / "test_claude"))
    from test_sdk_integration import (  # type: ignore[import-not-found]
        _make_assistant_message,
        _make_result_message,
        _mock_client_factory,
    )

    config = Settings(
        telegram_bot_token="test:token",
        telegram_bot_username="testbot",
        approved_directory=tmp_path,
        claude_timeout_seconds=2,
        enable_mcp=False,
    )
    manager = ClaudeSDKManager(config)
    captured: list[Any] = []
    factory = _mock_client_factory(
        _make_assistant_message("ok"),
        _make_result_message(total_cost_usd=0.01),
        capture_options=captured,
    )
    protected_dirs.cache_clear()
    try:
        with (
            patch("src.claude.sdk_integration.ClaudeSDKClient", side_effect=factory),
            # The mock yields typed messages; parse them as themselves.
            patch("src.claude.sdk_integration.parse_message", side_effect=lambda x: x),
            patch(
                "src.security.denylist.protected_dirs",
                return_value=ProtectedDirs(everyone=(), tenant=()),
            ),
        ):
            await manager.execute_command(
                prompt="hi", working_directory=fake_home["workspace"]
            )
    finally:
        protected_dirs.cache_clear()

    assert len(captured) == 1
    options = captured[0]
    assert options.setting_sources == ["project"]  # unchanged
    excludes = _excludes(options)
    assert str(fake_home["global"]) in excludes, excludes
    assert str(fake_home["install"]) in excludes, excludes
    settings = _effective_settings(options)
    assert settings.get("sandbox", {}).get("enabled") is False


def test_job_excludes_never_hide_the_working_dirs_own_claude_md(
    fake_home: dict[str, Path], tmp_path: Path
) -> None:
    """added: the exclude list names only the two outside files; a project's
    own CLAUDE.md (devops repo, pump worktree, gymbro repo) still loads."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "CLAUDE.md").write_text("project rules\n")
    excludes = _excludes(job_options(_spec(repo), _link()))
    assert str(repo / "CLAUDE.md") not in excludes
    assert not any(e in ("**/CLAUDE.md", "*/CLAUDE.md", "CLAUDE.md") for e in excludes)


# --------------------------------------------------------------------------
# Scenario: pump stage options use project settings only; bgjob options
# unchanged
# --------------------------------------------------------------------------


def test_job_options_setting_sources_follow_the_spec_default_unchanged(
    tmp_path: Path,
) -> None:
    default = job_options(_spec(tmp_path), _link())
    assert default.setting_sources == ["user", "project"]  # bgjob, monitor

    pump = job_options(_spec(tmp_path, setting_sources=("project",)), _link())
    assert pump.setting_sources == ["project"]
    # Same builder: everything else identical.
    assert pump.permission_mode == default.permission_mode == "bypassPermissions"
    assert pump.env == default.env
    assert len(pump.hooks["PreToolUse"]) == len(default.hooks["PreToolUse"]) == 1


def _load_agentjob() -> Any:
    import structlog

    saved = structlog.get_config()
    loader = SourceFileLoader("agentjob_under_test", str(REPO / "scripts" / "agentjob"))
    spec = spec_from_loader(loader.name, loader)
    assert spec is not None
    module = module_from_spec(spec)
    try:
        loader.exec_module(module)
    finally:
        # agentjob configures structlog at import; keep the suite's config.
        structlog.configure(**saved)
    return module


def _agentjob_spec(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *extra: str
) -> JobSpec:
    """Run scripts/agentjob's main() with fakes; return the JobSpec it built."""
    agentjob = _load_agentjob()
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("do it")
    cwd = tmp_path / "wt"
    cwd.mkdir()
    profile = type(
        "P",
        (),
        {
            "slug": "devops",
            "sandbox": False,
            "unrestricted": False,
            "working_dir": cwd,
        },
    )()
    registry = type("R", (), {"get_by_slug": lambda self, slug: profile})()
    config = type(
        "C",
        (),
        {
            "profiles_config_path": tmp_path / "profiles.yaml",
            "sandbox_enabled": True,
            "devvy_data_root": None,
            "claude_cli_path": None,
        },
    )()
    monkeypatch.setattr("src.config.loader.load_config", lambda **kw: config)
    monkeypatch.setattr("src.profiles.load_profile_registry", lambda *a, **kw: registry)
    seen: list[JobSpec] = []

    async def fake_run_job(spec: JobSpec, link: Any) -> int:
        seen.append(spec)
        return 0

    monkeypatch.setattr(agentjob, "run_job", fake_run_job)
    monkeypatch.setattr(agentjob, "bot_link_from_env", lambda repo: None)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "agentjob",
            "--profile",
            "devops",
            "--prompt-file",
            str(prompt),
            "--cwd",
            str(cwd),
            *extra,
        ],
    )
    assert agentjob.main() == 0
    assert len(seen) == 1
    return seen[0]


def test_agentjob_setting_sources_flag_reaches_the_job_spec(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pump = _agentjob_spec(monkeypatch, tmp_path, "--setting-sources", "project")
    assert list(pump.setting_sources) == ["project"]


def test_agentjob_without_the_flag_keeps_user_and_project(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    default = _agentjob_spec(monkeypatch, tmp_path)
    assert list(default.setting_sources) == ["user", "project"]


def _run_sh(bgjob: Any, tmp_path: Path, monkeypatch: Any, **extra: Any) -> str:
    """bgjob cmd_run writes run.sh (no systemd); return its agentjob line."""
    monkeypatch.setattr(bgjob, "JOBS_ROOT", tmp_path)
    monkeypatch.setattr(bgjob, "load_profiles", lambda: {"general": str(tmp_path)})
    monkeypatch.setattr(bgjob, "live_jobs", lambda: [])
    monkeypatch.setattr(bgjob.shutil, "which", lambda name: "/bin/sh")

    class R:
        returncode = 0
        stderr = ""
        stdout = ""

    monkeypatch.setattr(bgjob.subprocess, "run", lambda cmd, **kw: R())
    fields = {
        "prompt": "do it",
        "profile": "general",
        "name": "t",
        "allow_dir": None,
        "model": "sonnet",
        **extra,
    }
    bgjob.cmd_run(type("A", (), fields)())
    text = next(tmp_path.glob("*/run.sh")).read_text()
    lines = [ln for ln in text.splitlines() if "scripts/agentjob" in ln]
    assert len(lines) == 1, text
    return lines[0]


@pytest.fixture
def bgjob() -> Any:
    loader = SourceFileLoader("bgjob_dv_fe02", str(REPO / "scripts" / "bgjob"))
    spec = spec_from_loader(loader.name, loader)
    assert spec is not None
    module = module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_bgjob_run_passes_setting_sources_to_agentjob(
    bgjob: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    line = _run_sh(bgjob, tmp_path, monkeypatch, setting_sources="project")
    assert "--setting-sources project" in line


def test_bgjob_run_default_keeps_user_and_project(
    bgjob: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """bgjob unchanged: without the flag, agentjob gets no project-only
    override (no flag, or an explicit user,project)."""
    line = _run_sh(bgjob, tmp_path, monkeypatch)
    if "--setting-sources" in line:
        assert "--setting-sources user,project" in line, line
