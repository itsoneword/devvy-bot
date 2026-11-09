"""dv-86f4 phase 1: a planned restart is gated by a boot check.

``/restart`` and the ``restart_bot`` tool run ``system.preflight()`` (a
subprocess; ``None`` = ok, else a short reason) and refuse when it fails.
``/restart force`` skips it. The same check is the CLI
``scripts/check_install.py --preflight`` (exit 0/1, first line names the
problem) and never migrates any of the four DBs.
"""

import hashlib
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.bot.orchestration.commands import system
from src.storage.schema import migrate
from src.utils.turn_locks import TurnLocks

REPO = Path(__file__).resolve().parents[2]
REASON = (
    "Profile 'gymbro': extra_dirs entry is not a directory: /nonexistent/gymbro-staging"
)


# --- bot side (preflight faked) -------------------------------------------------


@pytest.fixture
def kills(monkeypatch) -> List[tuple]:
    calls: List[tuple] = []
    monkeypatch.setattr(system.os, "kill", lambda pid, sig: calls.append((pid, sig)))
    return calls


def _update() -> MagicMock:
    update = MagicMock()
    update.effective_user.id = 123
    update.message.reply_text = AsyncMock()
    return update


def _context(args: Optional[list] = None) -> MagicMock:
    context = MagicMock()
    context.bot_data = {"audit_logger": None}
    context.args = args if args is not None else []
    return context


def _replies(update: MagicMock) -> str:
    return "\n".join(str(c) for c in update.message.reply_text.call_args_list)


async def test_restart_with_valid_config_restarts(monkeypatch, kills):
    pre = AsyncMock(return_value=None)
    monkeypatch.setattr(system, "preflight", pre, raising=False)
    update = _update()

    await system.restart_command(update, _context())

    pre.assert_awaited_once()
    assert len(kills) == 1
    assert "Restarting" in _replies(update)


async def test_restart_refused_when_check_fails_names_profile_and_path(
    monkeypatch, kills
):
    monkeypatch.setattr(
        system, "preflight", AsyncMock(return_value=REASON), raising=False
    )
    update = _update()

    await system.restart_command(update, _context())

    assert kills == []
    text = _replies(update)
    assert "gymbro" in text and "/nonexistent/gymbro-staging" in text
    assert "Restarting" not in text


async def test_restart_bot_refused_when_check_fails(monkeypatch, kills):
    monkeypatch.setattr(
        system, "preflight", AsyncMock(return_value=REASON), raising=False
    )
    monkeypatch.setattr(system, "RESTART_POLL_S", 0.01, raising=False)
    monkeypatch.setattr(system, "RESTART_GRACE_S", 0.01, raising=False)
    monkeypatch.setattr(system, "_pending", None)

    out = await system.request_restart(TurnLocks(), reason="apply config", user_id=1)

    assert out not in ("scheduled", "already scheduled")
    assert "gymbro" in out and "/nonexistent/gymbro-staging" in out
    import asyncio

    await asyncio.sleep(0.2)
    assert kills == []


async def test_restart_force_skips_the_check(monkeypatch, kills):
    pre = AsyncMock(return_value=REASON)
    monkeypatch.setattr(system, "preflight", pre, raising=False)
    update = _update()

    await system.restart_command(update, _context(["force"]))

    pre.assert_not_awaited()
    assert len(kills) == 1


# --- CLI: scripts/check_install.py --preflight ----------------------------------


def _write_profiles(tmp: Path, extra: Optional[Path]) -> Path:
    ws = tmp / "ws"
    ws.mkdir(exist_ok=True)
    gym = f"    extra_dirs: [{extra}]\n" if extra else ""
    cfg = tmp / "profiles.yaml"
    cfg.write_text(
        "profiles:\n"
        "  - slug: devops\n    name: Devops\n    default: true\n"
        f"    working_dir: {ws}/devops\n"
        f"    memory_dir: {ws}/devops/memory\n"
        f"    persona: {ws}/devops/persona.md\n"
        f"    persona_template: {REPO}/data/templates/agent.md\n"
        "    sandbox: false\n    unrestricted: true\n    shared: true\n"
        "  - slug: gymbro\n    name: Gym\n"
        f"    working_dir: {ws}/gymbro\n"
        f"    memory_dir: {ws}/gymbro/memory\n"
        f"    persona: {ws}/gymbro/persona.md\n"
        f"    persona_template: {REPO}/data/templates/agent.md\n" + gym
    )
    return cfg


def _run_preflight(tmp: Path, profiles: Path) -> subprocess.CompletedProcess:
    root = tmp / "root"
    env_file = tmp / "test.env"
    env_file.write_text(
        "TELEGRAM_BOT_TOKEN=123456:TESTTOKEN\n"
        "TELEGRAM_BOT_USERNAME=testbot\n"
        "ALLOWED_USERS=1\n"
        f"PROFILES_CONFIG_PATH={profiles}\n"
        f"DEVVY_DATA_ROOT={root}\n"
    )
    env: Dict[str, str] = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("TELEGRAM_", "DEVVY_", "PROFILES_", "DATABASE_"))
        and k not in ("ALLOWED_USERS",)
    }
    env["DEVVY_DATA_ROOT"] = str(root)
    return subprocess.run(
        [
            sys.executable,
            str(REPO / "scripts" / "check_install.py"),
            "--preflight",
            "--env-file",
            str(env_file),
        ],
        capture_output=True,
        text=True,
        cwd=str(tmp),
        env=env,
        timeout=120,
    )


def _make_dbs(tmp: Path) -> Dict[str, Path]:
    db_dir = tmp / "root" / "db"
    db_dir.mkdir(parents=True, exist_ok=True)
    paths = {name: db_dir / f"{name}.db" for name in migrate.DB_NAMES}
    for name, path in paths.items():
        migrate.ensure_schema(name, path)
    return paths


def _fingerprint(paths: Dict[str, Path]) -> Dict[str, tuple]:
    return {
        n: (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
        for n, p in paths.items()
    }


def test_preflight_cli_fails_on_missing_extra_dir(tmp_path):
    missing = tmp_path / "gymbro-staging"
    res = _run_preflight(tmp_path, _write_profiles(tmp_path, missing))

    assert res.returncode == 1, res.stdout + res.stderr
    first = res.stdout.strip().splitlines()[0]
    assert "gymbro" in first and str(missing) in first


def test_preflight_cli_passes_on_valid_config_and_leaves_dbs_untouched(tmp_path):
    extra = tmp_path / "gymbro-staging"
    extra.mkdir()
    dbs = _make_dbs(tmp_path)
    before = _fingerprint(dbs)

    res = _run_preflight(tmp_path, _write_profiles(tmp_path, extra))

    assert res.returncode == 0, res.stdout + res.stderr
    assert _fingerprint(dbs) == before


def test_preflight_cli_fails_on_db_newer_than_code_and_does_not_migrate(tmp_path):
    extra = tmp_path / "gymbro-staging"
    extra.mkdir()
    dbs = _make_dbs(tmp_path)
    conn = sqlite3.connect(dbs["llm_analytics"])
    conn.execute("UPDATE alembic_version SET version_num = '9999'")
    conn.commit()
    conn.close()
    before = _fingerprint(dbs)

    res = _run_preflight(tmp_path, _write_profiles(tmp_path, extra))

    assert res.returncode == 1, res.stdout + res.stderr
    assert "llm_analytics" in res.stdout
    assert _fingerprint(dbs) == before


def test_preflight_cli_does_not_create_missing_dbs(tmp_path):
    extra = tmp_path / "gymbro-staging"
    extra.mkdir()

    _run_preflight(tmp_path, _write_profiles(tmp_path, extra))

    db_dir = tmp_path / "root" / "db"
    assert not db_dir.exists() or not list(db_dir.glob("*.db"))
