"""dv-86f4 phase 1, beyond the scenario file ``test_restart_preflight.py``:
the subprocess runner, the read-only DB revision check, the re-check after a
deferred restart drains, the force button and its owner gate, and the
``restart_bot`` tool's refusal shape.
"""

import json
import sqlite3
import stat
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import List
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram.ext import CallbackQueryHandler

from src.bot.orchestration.commands import system
from src.bot.orchestrator import MessageOrchestrator
from src.claude import agent_tools
from src.config import create_test_config
from src.storage.schema import migrate
from src.utils.turn_locks import TurnLocks

REASON = "✘ profiles: Profile 'gymbro': extra_dirs entry is not a directory: /x"


@pytest.fixture
def kills(monkeypatch) -> List[tuple]:
    calls: List[tuple] = []
    monkeypatch.setattr(system.os, "kill", lambda pid, sig: calls.append((pid, sig)))
    return calls


# --- preflight(): the subprocess ---------------------------------------------


def _fake_check(tmp_path: Path, body: str) -> Path:
    script = tmp_path / "check.py"
    script.write_text("import os, sys, time\n" + body)
    return script


async def test_preflight_ok_is_none(monkeypatch, tmp_path):
    monkeypatch.setattr(
        system, "CHECK_SCRIPT", _fake_check(tmp_path, "print('ok')\nsys.exit(0)\n")
    )
    assert await system.preflight() is None


async def test_preflight_failure_is_the_first_stdout_line(monkeypatch, tmp_path):
    body = f"print({REASON!r})\nprint('  more detail')\nsys.exit(1)\n"
    monkeypatch.setattr(system, "CHECK_SCRIPT", _fake_check(tmp_path, body))
    assert await system.preflight() == REASON


async def test_preflight_timeout_counts_as_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(
        system, "CHECK_SCRIPT", _fake_check(tmp_path, "time.sleep(30)\n")
    )
    monkeypatch.setattr(system, "PREFLIGHT_TIMEOUT_S", 0.5)
    reason = await system.preflight()
    assert reason and "timed out" in reason


async def test_preflight_does_not_inherit_the_bots_environ(monkeypatch, tmp_path):
    """The child reads the env file itself; the bot's os.environ holds values
    from the last boot and would mask the edit being checked."""
    monkeypatch.setenv("TELEGRAM_BOT_USERNAME", "stale_from_last_boot")
    monkeypatch.setenv("DEVVY_DATA_ROOT", str(tmp_path))
    body = (
        "print(','.join(sorted(os.environ)))\n"
        "sys.exit(0 if 'TELEGRAM_BOT_USERNAME' not in os.environ "
        "and os.environ.get('DEVVY_DATA_ROOT') else 1)\n"
    )
    monkeypatch.setattr(system, "CHECK_SCRIPT", _fake_check(tmp_path, body))
    assert await system.preflight() is None


def test_preflight_runs_the_one_cli_entry_point():
    assert system.CHECK_SCRIPT.name == "check_install.py"
    assert system.CHECK_SCRIPT.exists()
    assert system.CHECK_SCRIPT.parent.parent == system.REPO_ROOT


# --- migrate.check_revision ---------------------------------------------------


def test_check_revision_missing_file_is_ok_and_not_created(tmp_path):
    path = tmp_path / "bot.db"
    assert migrate.check_revision("bot", path) is None
    assert not path.exists()


def test_check_revision_without_alembic_table_is_ok(tmp_path):
    path = tmp_path / "data.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE schema_version (version INTEGER)")
    conn.commit()
    conn.close()
    assert migrate.check_revision("data", path) is None


@pytest.mark.parametrize("db_name", migrate.DB_NAMES)
def test_check_revision_at_head_and_at_an_older_known_rev(tmp_path, db_name):
    path = tmp_path / f"{db_name}.db"
    migrate.ensure_schema(db_name, path)
    assert migrate.check_revision(db_name, path) is None

    conn = sqlite3.connect(path)
    conn.execute(
        "UPDATE alembic_version SET version_num = ?", (migrate.BASELINE[db_name],)
    )
    conn.commit()
    conn.close()
    assert migrate.check_revision(db_name, path) is None


def test_check_revision_unknown_rev_names_the_db(tmp_path):
    path = tmp_path / "channel_intel.db"
    migrate.ensure_schema("channel_intel", path)
    conn = sqlite3.connect(path)
    conn.execute("UPDATE alembic_version SET version_num = '9999'")
    conn.commit()
    conn.close()

    problem = migrate.check_revision("channel_intel", path)

    assert problem is not None
    assert "channel_intel" in problem and "9999" in problem
    assert migrate.head_revision("channel_intel") in problem


def test_check_revision_in_a_read_only_dir(tmp_path):
    """The live data dir may be read-only to whoever runs the check: mode=ro
    cannot create the WAL side files there, the immutable retry can."""
    db_dir = tmp_path / "db"
    db_dir.mkdir()
    path = db_dir / "bot.db"
    migrate.ensure_schema("bot", path)
    for side in db_dir.glob("bot.db-*"):
        side.unlink()
    db_dir.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        assert migrate.check_revision("bot", path) is None
    finally:
        db_dir.chmod(stat.S_IRWXU)


# --- deferred restart: re-check after the drain --------------------------------


async def test_drained_restart_rechecks_and_tells_the_owner(monkeypatch, kills):
    """Config may change during the (up to 10 min) drain: the check runs again
    before the SIGTERM, and a refusal then goes to the owner."""
    monkeypatch.setattr(
        system, "preflight", AsyncMock(side_effect=[None, REASON]), raising=False
    )
    monkeypatch.setattr(system, "RESTART_POLL_S", 0.01)
    monkeypatch.setattr(system, "RESTART_GRACE_S", 0.01)
    monkeypatch.setattr(system, "_pending", None)
    notify = AsyncMock()

    out = await system.request_restart(
        TurnLocks(), reason="apply config", user_id=1, notify=notify
    )
    assert out == "scheduled"
    assert system._pending is not None
    await system._pending

    assert kills == []
    notify.assert_awaited_once()
    assert "gymbro" in notify.await_args.args[0]


async def test_drained_restart_with_a_clean_recheck_fires(monkeypatch, kills):
    pre = AsyncMock(return_value=None)
    monkeypatch.setattr(system, "preflight", pre, raising=False)
    monkeypatch.setattr(system, "RESTART_POLL_S", 0.01)
    monkeypatch.setattr(system, "RESTART_GRACE_S", 0.01)
    monkeypatch.setattr(system, "_pending", None)

    await system.request_restart(TurnLocks(), reason="x", user_id=1)
    assert system._pending is not None
    await system._pending

    assert pre.await_count == 2
    assert len(kills) == 1


# --- restart_bot tool -------------------------------------------------------------


async def test_restart_bot_tool_returns_refused_with_why():
    services = SimpleNamespace(
        request_restart=AsyncMock(return_value=f"refused: {REASON}")
    )
    ctx = SimpleNamespace(role="owner", profile_slug="devops", user_id=1)

    result = await agent_tools.restart_bot(services, ctx, {"reason": "apply"})

    body = json.loads(result["content"][0]["text"])
    assert body["status"] == "refused"
    assert body["why"] == REASON


# --- the force button ------------------------------------------------------------


def _force_update(user_id: int) -> MagicMock:
    update = MagicMock()
    update.effective_user.id = user_id
    update.callback_query.data = system.FORCE_CALLBACK
    update.callback_query.answer = AsyncMock()
    update.callback_query.edit_message_text = AsyncMock()
    update.effective_message.reply_text = AsyncMock()
    return update


async def test_force_button_restarts_without_the_check(monkeypatch, kills):
    pre = AsyncMock(return_value=REASON)
    monkeypatch.setattr(system, "preflight", pre)
    update = _force_update(1)
    context = MagicMock()
    context.bot_data = {"audit_logger": None}

    await system.restart_force_callback(update, context)

    pre.assert_not_awaited()
    assert len(kills) == 1


def test_refusal_screen_has_reason_and_force_button():
    text, markup = system.refusal("a <b> & c")
    assert "a &lt;b&gt; &amp; c" in text
    [[button]] = markup.inline_keyboard
    assert button.callback_data == system.FORCE_CALLBACK


async def test_force_button_is_owner_only(monkeypatch, kills, tmp_path):
    settings = create_test_config(approved_directory=str(tmp_path))
    orchestrator = MessageOrchestrator(settings, {"storage": MagicMock()})
    app = MagicMock()
    orchestrator.register_handlers(app)
    [handler] = [
        c.args[0]
        for c in app.add_handler.call_args_list
        if isinstance(c.args[0], CallbackQueryHandler)
        and c.args[0].pattern.match(system.FORCE_CALLBACK)
    ]

    update = _force_update(424242)
    context = MagicMock()
    context.bot_data = {}
    context.user_data = {"role": "guest", "_display_name_checked": True}
    await handler.callback(update, context)

    assert kills == []
    assert "owner-only" in str(update.effective_message.reply_text.call_args)


def test_cli_entry_point_is_importable_without_side_effects():
    """scripts/check_install.py is a thin wrapper over src.boot.check."""
    sys.path.insert(0, str(system.REPO_ROOT / "scripts"))
    try:
        import check_install  # type: ignore[import-not-found]
    finally:
        sys.path.pop(0)
    from src.boot import check

    assert check_install.verify is check.verify
