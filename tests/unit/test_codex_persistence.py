"""A Codex turn must persist like a Claude turn (dv-0c01).

Live 2026-09-03: every Codex turn logged "Failed to log interaction: FOREIGN
KEY constraint failed". The Codex thread id was used as `messages.session_id`
but only ever stored in the JSON session store, never in the `sessions` table
that `messages` references. These tests run against a real SQLite file with
`PRAGMA foreign_keys = ON` (the production DatabaseManager sets it), so the
constraint is genuinely enforced rather than mocked away.
"""

import json
import shlex
import stat
import tempfile
from pathlib import Path

import pytest

from src.agent_backends.codex_cli import CodexCliIntegration, InMemoryCodexSessionStore
from src.claude.session import SessionManager
from src.config.settings import Settings
from src.storage.facade import Storage
from src.storage.session_storage import SQLiteSessionStorage
from tests.helpers.traced import traced

USER_ID = 4242
THREAD_ID = "01a06921-833f-7a5e-9c1e-000000000001"


def _fake_codex(tmp_path: Path) -> Path:
    """Stand-in `codex` emitting the JSONL a real turn emits."""
    script = tmp_path / "codex"
    script.write_text(
        "#!/usr/bin/env bash\n"
        "set -e\n"
        'outfile=""\n'
        'args=("$@")\n'
        "for ((i=0; i<${#args[@]}; i++)); do\n"
        '  if [[ "${args[$i]}" == "-o" ]]; then outfile="${args[$((i+1))]}"; fi\n'
        "done\n"
        f'echo \'{{"type":"thread.started","thread_id":"{THREAD_ID}"}}\'\n'
        'echo \'{"type":"item.started","item":{"type":"command_execution",'
        '"command":"ls"}}\'\n'
        'echo \'{"type":"item.completed","item":{"type":"agent_message",'
        '"text":"done"}}\'\n'
        'echo \'{"type":"turn.completed","usage":{"input_tokens":1200,'
        '"output_tokens":300}}\'\n'
        'if [[ -n "$outfile" ]]; then echo -n "done" > "$outfile"; fi\n'
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IRWXU)
    return script


@pytest.fixture
async def storage():
    with tempfile.TemporaryDirectory() as temp_dir:
        st = Storage(f"sqlite:///{Path(temp_dir) / 'bot.db'}")
        await st.initialize()
        yield st
        await st.close()


@pytest.fixture
def settings():
    return Settings(telegram_bot_token="x", telegram_bot_username="x")


def _integration(settings, storage, tmp_path, *, with_session_manager=True):
    manager = (
        SessionManager(settings, SQLiteSessionStorage(storage.db_manager))
        if with_session_manager
        else None
    )
    return CodexCliIntegration(
        config=settings,
        codex_bin=str(_fake_codex(tmp_path)),
        session_store=InMemoryCodexSessionStore(),
        session_manager=manager,
    )


async def _fk_enabled(storage) -> bool:
    async with storage.db_manager.get_connection() as conn:
        cur = await conn.execute("PRAGMA foreign_keys")
        return bool((await cur.fetchone())[0])


async def test_fixture_actually_enforces_foreign_keys(storage):
    """Guard the guard: a fixture with FKs off would pass everything."""
    assert await _fk_enabled(storage)


async def test_codex_turn_writes_session_message_and_cost_rows(
    storage, settings, tmp_path
):
    workdir = tmp_path / "workspace"
    workdir.mkdir()
    integ = _integration(settings, storage, tmp_path)

    response = await traced(
        storage,
        lambda: integ.run_command(
            prompt="привет",
            working_directory=workdir,
            user_id=USER_ID,
            force_new=True,
        ),
    )
    assert response.session_id == THREAD_ID

    # The sessions row must already exist when run_command returns — the
    # orchestrator calls save_claude_interaction immediately afterwards.
    session = await storage.sessions.get_session(THREAD_ID)
    assert session is not None
    assert session.user_id == USER_ID
    assert session.project_path == str(workdir)

    # This is the call that raised "FOREIGN KEY constraint failed".
    message_id = await storage.save_claude_interaction(
        user_id=USER_ID,
        session_id=response.session_id,
        prompt="привет",
        response=response,
        window_id=17,
    )
    assert message_id > 0

    async with storage.db_manager.get_connection() as conn:
        cur = await conn.execute(
            "SELECT session_id, window_id FROM messages WHERE message_id = ?",
            (message_id,),
        )
        row = await cur.fetchone()
        assert row[0] == THREAD_ID
        assert row[1] == 17

        cur = await conn.execute(
            "SELECT tool_name FROM tool_calls WHERE message_id = ?", (message_id,)
        )
        assert [r[0] for r in await cur.fetchall()] == ["Bash"]

        cur = await conn.execute(
            "SELECT request_count FROM cost_tracking WHERE user_id = ?", (USER_ID,)
        )
        assert (await cur.fetchone())[0] == 1


async def test_session_row_accumulates_across_turns(storage, settings, tmp_path):
    """A resumed thread updates its row instead of duplicating or resetting it."""
    workdir = tmp_path / "workspace"
    workdir.mkdir()
    integ = _integration(settings, storage, tmp_path)

    for _ in range(3):
        await integ.run_command(
            prompt="ещё", working_directory=workdir, user_id=USER_ID, force_new=False
        )

    async with storage.db_manager.get_connection() as conn:
        cur = await conn.execute("SELECT COUNT(*) FROM sessions")
        assert (await cur.fetchone())[0] == 1
    session = await storage.sessions.get_session(THREAD_ID)
    assert session.message_count == 3
    assert session.total_turns == 3


async def test_user_row_is_created_for_an_unknown_user(storage, settings, tmp_path):
    """`sessions.user_id` is itself an FK; the Claude path auto-creates it."""
    async with storage.db_manager.get_connection() as conn:
        cur = await conn.execute("SELECT COUNT(*) FROM users")
        assert (await cur.fetchone())[0] == 0

    integ = _integration(settings, storage, tmp_path)
    await integ.run_command(
        prompt="hi", working_directory=tmp_path, user_id=USER_ID, force_new=True
    )

    user = await storage.users.get_user(USER_ID)
    assert user is not None


async def test_turn_survives_a_broken_session_manager(storage, settings, tmp_path):
    """Losing the history row must never cost the user their answer."""

    integ = _integration(settings, storage, tmp_path)

    class _Broken:
        class storage:  # noqa: N801 - mimics SessionManager.storage
            @staticmethod
            async def load_session(*_a, **_kw):
                raise RuntimeError("db is down")

    integ.session_manager = _Broken()
    response = await integ.run_command(
        prompt="hi", working_directory=tmp_path, user_id=USER_ID, force_new=True
    )
    assert response.content == "done"


async def test_no_session_manager_keeps_old_behaviour(storage, settings, tmp_path):
    """Constructed without a session manager (tests, tooling): no rows, no crash."""
    integ = _integration(settings, storage, tmp_path, with_session_manager=False)
    response = await integ.run_command(
        prompt="hi", working_directory=tmp_path, user_id=USER_ID, force_new=True
    )
    assert response.session_id == THREAD_ID
    assert await storage.sessions.get_session(THREAD_ID) is None


def _fake_codex_failed_mcp(tmp_path: Path) -> Path:
    """A turn with one failed MCP call and one successful shell command."""
    script = tmp_path / "codex_failed_mcp"
    events = [
        {"type": "thread.started", "thread_id": THREAD_ID},
        {
            "type": "item.started",
            "item": {
                "id": "item_1",
                "type": "mcp_tool_call",
                "server": "agent",
                "tool": "week_summary",
                "arguments": {},
                "status": "in_progress",
            },
        },
        {
            "type": "item.completed",
            "item": {
                "id": "item_1",
                "type": "mcp_tool_call",
                "server": "agent",
                "tool": "week_summary",
                "arguments": {},
                "result": None,
                "status": "failed",
                "error": {
                    "message": "MCP tool call requires approval, but approval policy is never"
                },
            },
        },
        {
            "type": "item.started",
            "item": {
                "id": "item_2",
                "type": "command_execution",
                "command": "ls",
                "status": "in_progress",
            },
        },
        {
            "type": "item.completed",
            "item": {
                "id": "item_2",
                "type": "command_execution",
                "command": "ls",
                "exit_code": 0,
                "status": "completed",
            },
        },
        {"type": "item.completed", "item": {"type": "agent_message", "text": "done"}},
        {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 3}},
    ]
    body = "".join(f"echo {shlex.quote(json.dumps(e))}\n" for e in events)
    script.write_text(
        "#!/usr/bin/env bash\n"
        "set -e\n"
        'outfile=""\n'
        'args=("$@")\n'
        "for ((i=0; i<${#args[@]}; i++)); do\n"
        '  if [[ "${args[$i]}" == "-o" ]]; then outfile="${args[$((i+1))]}"; fi\n'
        "done\n"
        + body
        + 'if [[ -n "$outfile" ]]; then echo -n "done" > "$outfile"; fi\n'
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IRWXU)
    return script


async def test_failed_mcp_call_is_stored_as_failure(storage, settings, tmp_path):
    """dv-c7d1: a failed MCP item was stored success=1 (only item.started
    was read)."""
    workdir = tmp_path / "workspace"
    workdir.mkdir()
    integ = CodexCliIntegration(
        config=settings,
        codex_bin=str(_fake_codex_failed_mcp(tmp_path)),
        session_store=InMemoryCodexSessionStore(),
        session_manager=SessionManager(
            settings, SQLiteSessionStorage(storage.db_manager)
        ),
    )
    response = await traced(
        storage,
        lambda: integ.run_command(
            prompt="week", working_directory=workdir, user_id=USER_ID, force_new=True
        ),
    )
    assert not response.is_error
    message_id = await storage.save_claude_interaction(
        user_id=USER_ID,
        session_id=response.session_id,
        prompt="week",
        response=response,
        window_id=1,
    )
    async with storage.db_manager.get_connection() as conn:
        cur = await conn.execute(
            "SELECT tool_name, ok, error FROM tool_calls "
            "WHERE message_id = ? ORDER BY id",
            (message_id,),
        )
        rows = [tuple(r) for r in await cur.fetchall()]
    assert rows == [
        (
            "week_summary",
            0,
            "MCP tool call requires approval, but approval policy is never",
        ),
        ("Bash", 1, None),
    ]
