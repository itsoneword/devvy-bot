"""Codex resume failure falls back to a fresh, seeded thread (dv-e3f9).

The Claude facade retries on a fresh session with the stored tail when resume
fails and flags the response (`session_restarted`). Codex used to raise
`RuntimeError` on a dead thread id instead. These tests drive
`CodexCliIntegration.run_command` against a stand-in `codex` that fails the
way codex-cli 0.152.0 really fails on an unknown thread (exit 1, no stdout
events, one `thread/resume ... no rollout found` line on stderr) and
succeeds on the fresh run.
"""

import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import List

import pytest

from src.agent_backends.codex_cli import (
    CodexCliIntegration,
    CodexResumeError,
    CodexSessionStore,
    InMemoryCodexSessionStore,
    is_resume_failure,
)
from src.claude.sdk_integration import ClaudeResponse
from src.claude.session import ClaudeSession, SessionManager
from src.config.settings import Settings
from src.storage.facade import Storage
from src.storage.session_storage import SQLiteSessionStorage

USER_ID = 4242
DEAD_THREAD = "01a06921-833f-7a5e-9c1e-00000000dead"
NEW_THREAD = "01a06921-833f-7a5e-9c1e-0000000000ee"

# Verbatim from codex-cli 0.152.0 (2026-09-06 probe, docs/FINDINGS.md).
RESUME_STDERR = (
    "Error: thread/resume: thread/resume failed: no rollout found for "
    f"thread id {DEAD_THREAD} (code -32600)"
)


def _fake_codex(tmp_path: Path, *, fresh_fails: bool = False) -> Path:
    """A `codex` that dies like the real one on `resume`, succeeds fresh.

    Every invocation writes its argv (NUL-separated; prompts span lines) to
    `<tmp>/calls/call_<n>` so the test can inspect what each run was given.
    """
    calls = tmp_path / "calls"
    calls.mkdir(exist_ok=True)
    fresh_branch = (
        'echo \'{"type":"error","message":"401 unauthorized"}\'\n  exit 1\n'
        if fresh_fails
        else (
            f'echo \'{{"type":"thread.started","thread_id":"{NEW_THREAD}"}}\'\n'
            '  echo \'{"type":"item.completed","item":{"type":"agent_message",'
            '"text":"fresh answer"}}\'\n'
            '  echo \'{"type":"turn.completed","usage":{"input_tokens":50,'
            '"output_tokens":5}}\'\n'
            '  if [[ -n "$outfile" ]]; then echo -n "fresh answer" > "$outfile"; fi\n'
        )
    )
    script = tmp_path / "codex"
    script.write_text(
        "#!/usr/bin/env bash\n"
        f'calls="{calls}"\n'
        'n=$(ls "$calls" | wc -l)\n'
        'printf "%s\\0" "$@" > "$calls/call_$n"\n'
        'outfile=""\n'
        "resume=0\n"
        'args=("$@")\n'
        "for ((i=0; i<${#args[@]}; i++)); do\n"
        '  if [[ "${args[$i]}" == "-o" ]]; then outfile="${args[$((i+1))]}"; fi\n'
        '  if [[ "${args[$i]}" == "resume" ]]; then resume=1; fi\n'
        "done\n"
        "if [[ $resume == 1 ]]; then\n"
        f'  echo "{RESUME_STDERR}" >&2\n'
        "  exit 1\n"
        "else\n"
        f"  {fresh_branch}"
        "fi\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IRWXU)
    return script


def _calls(tmp_path: Path) -> List[List[str]]:
    calls = tmp_path / "calls"
    return [
        p.read_text().rstrip("\0").split("\0")
        for p in sorted(calls.iterdir(), key=lambda p: int(p.name.split("_")[1]))
    ]


@pytest.fixture
async def storage():
    with tempfile.TemporaryDirectory() as temp_dir:
        st = Storage(f"sqlite:///{Path(temp_dir) / 'bot.db'}")
        await st.initialize()
        yield st
        await st.close()


@pytest.fixture
def settings():
    return Settings(
        telegram_bot_token="x",
        telegram_bot_username="x",
        enable_mcp=False,
        mcp_config_path=None,
    )


async def _seed_history(storage: Storage, workdir: Path, turns: List[str]) -> None:
    """A sessions row for the dead thread plus one messages row per turn."""
    now = datetime.now(timezone.utc)
    await SQLiteSessionStorage(storage.db_manager).save_session(
        ClaudeSession(
            session_id=DEAD_THREAD,
            user_id=USER_ID,
            project_path=workdir,
            created_at=now,
            last_used=now,
        )
    )
    for i, text in enumerate(turns):
        await storage.save_claude_interaction(
            user_id=USER_ID,
            session_id=DEAD_THREAD,
            prompt=text,
            response=ClaudeResponse(
                content=f"reply {i}",
                session_id=DEAD_THREAD,
                cost=0.0,
                duration_ms=1,
                num_turns=1,
            ),
        )


async def _integration(
    settings, storage, tmp_path, codex, *, with_session_manager=True
):
    # The real bot.db-backed store when there is a DB (dv-ebde), else the
    # in-memory one. The DB store needs the users row _seed_history made.
    store = (
        CodexSessionStore(storage.db_manager)
        if storage is not None
        else InMemoryCodexSessionStore()
    )
    await store.record(USER_ID, tmp_path / "workspace", DEAD_THREAD, 1, 1)
    manager = (
        SessionManager(settings, SQLiteSessionStorage(storage.db_manager))
        if with_session_manager
        else None
    )
    return (
        CodexCliIntegration(
            config=settings,
            codex_bin=str(codex),
            session_store=store,
            session_manager=manager,
        ),
        store,
    )


def test_is_resume_failure_matches_the_real_stderr_only_when_resuming():
    assert is_resume_failure(DEAD_THREAD, RESUME_STDERR)
    assert not is_resume_failure(None, RESUME_STDERR)
    assert not is_resume_failure(DEAD_THREAD, "400 invalid_request_error")
    assert not is_resume_failure(DEAD_THREAD, "")


async def test_dead_thread_retries_fresh_seeded_and_flags_response(
    storage, settings, tmp_path
):
    workdir = tmp_path / "workspace"
    workdir.mkdir()
    await _seed_history(storage, workdir, ["first question", "second question"])
    integ, store = await _integration(
        settings, storage, tmp_path, _fake_codex(tmp_path)
    )

    resp = await integ.run_command(
        prompt="third question",
        working_directory=workdir,
        user_id=USER_ID,
        force_new=False,
    )

    # Same flag the Claude facade sets -> the orchestration footer shows
    # "session restarted (resume failed)" for Codex too.
    assert resp.session_restarted is True
    assert resp.content == "fresh answer"
    assert resp.session_id == NEW_THREAD

    calls = _calls(tmp_path)
    assert len(calls) == 2
    assert "resume" in calls[0] and DEAD_THREAD in calls[0]
    assert "resume" not in calls[1]
    # The retry prompt is the dead thread's stored tail + the user's message,
    # with the honest "could not be resumed" wording, not "rotated".
    retry_prompt = calls[1][-1]
    assert "could not be resumed" in retry_prompt
    assert "rotated" not in retry_prompt
    assert "first question" in retry_prompt and "reply 0" in retry_prompt
    assert "second question" in retry_prompt
    assert retry_prompt.rstrip().endswith("third question")
    # The dead thread was attempted with the plain prompt.
    assert "could not be resumed" not in calls[0][-1]

    # Store now points at the fresh thread; the dead id is gone.
    assert await store.thread_for(USER_ID, workdir) == NEW_THREAD
    # dv-ebde: the retry path writes the Codex columns on the new row, and the
    # dead thread keeps its row with the head flag dropped.
    async with storage.db_manager.get_connection() as conn:
        cur = await conn.execute(
            "SELECT session_id, backend, thread_head, thread_total_input, "
            "thread_total_output, cumulative_tokens FROM sessions "
            "WHERE session_id IN (?, ?) ORDER BY session_id",
            (DEAD_THREAD, NEW_THREAD),
        )
        rows = {r[0]: tuple(r[1:]) for r in await cur.fetchall()}
    assert rows[NEW_THREAD] == ("openai_codex", 1, 50, 5, 55)
    assert rows[DEAD_THREAD][:2] == ("openai_codex", 0)
    # Fresh thread's totals start from zero, so this turn's flow is the raw
    # turn.completed number, not a delta against the dead thread.
    assert resp.input_tokens == 50 and resp.output_tokens == 5
    # The dead thread's history rows survive (recall_conversation reads them).
    async with storage.db_manager.get_connection() as conn:
        cur = await conn.execute(
            "SELECT COUNT(*) FROM messages WHERE session_id = ?", (DEAD_THREAD,)
        )
        assert (await cur.fetchone())[0] == 2
    # And the fresh thread got its own sessions row for the FK.
    assert await storage.sessions.get_session(NEW_THREAD) is not None


async def test_no_session_manager_retries_unseeded(settings, tmp_path):
    """No history to read -> fresh retry with the plain prompt, still flagged."""
    workdir = tmp_path / "workspace"
    workdir.mkdir()
    integ, store = await _integration(
        settings, None, tmp_path, _fake_codex(tmp_path), with_session_manager=False
    )

    resp = await integ.run_command(
        prompt="hello", working_directory=workdir, user_id=USER_ID
    )

    assert resp.session_restarted is True
    calls = _calls(tmp_path)
    assert len(calls) == 2
    # dv-ca94: every turn now carries a <current_time> prefix ahead of the
    # (here unseeded, so otherwise unmodified) prompt.
    assert calls[1][-1].endswith("\n\nhello")
    assert await store.thread_for(USER_ID, workdir) == NEW_THREAD


async def test_non_resume_failure_on_a_resumed_thread_does_not_retry(
    settings, tmp_path
):
    """An API error on a live thread is NOT a dead thread: no retry, no wipe."""
    script = tmp_path / "codex"
    calls = tmp_path / "calls"
    calls.mkdir()
    script.write_text(
        "#!/usr/bin/env bash\n"
        f'printf "%s\\0" "$@" > "{calls}/call_$(ls "{calls}" | wc -l)"\n'
        f'echo \'{{"type":"thread.started","thread_id":"{DEAD_THREAD}"}}\'\n'
        'echo \'{"type":"error","message":"400 invalid_request_error"}\'\n'
        "exit 1\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IRWXU)
    workdir = tmp_path / "workspace"
    workdir.mkdir()
    integ, store = await _integration(
        settings, None, tmp_path, script, with_session_manager=False
    )

    with pytest.raises(RuntimeError) as excinfo:
        await integ.run_command(
            prompt="hello", working_directory=workdir, user_id=USER_ID
        )

    assert not isinstance(excinfo.value, CodexResumeError)
    assert "invalid_request_error" in str(excinfo.value)
    assert len(_calls(tmp_path)) == 1
    assert await store.thread_for(USER_ID, workdir) == DEAD_THREAD


async def test_fresh_retry_failure_propagates_without_looping(settings, tmp_path):
    workdir = tmp_path / "workspace"
    workdir.mkdir()
    integ, store = await _integration(
        settings,
        None,
        tmp_path,
        _fake_codex(tmp_path, fresh_fails=True),
        with_session_manager=False,
    )

    with pytest.raises(RuntimeError) as excinfo:
        await integ.run_command(
            prompt="hello", working_directory=workdir, user_id=USER_ID
        )

    assert "401 unauthorized" in str(excinfo.value)
    assert not isinstance(excinfo.value, CodexResumeError)
    assert len(_calls(tmp_path)) == 2
    # The dead id was dropped; nothing new was recorded for the failed retry.
    assert await store.thread_for(USER_ID, workdir) is None


async def test_explicit_dead_session_id_also_falls_back(settings, tmp_path):
    """A caller-supplied session_id takes the same path as the stored one."""
    workdir = tmp_path / "workspace"
    workdir.mkdir()
    integ, store = await _integration(
        settings, None, tmp_path, _fake_codex(tmp_path), with_session_manager=False
    )
    await store.clear(USER_ID, workdir)

    resp = await integ.run_command(
        prompt="hello",
        working_directory=workdir,
        user_id=USER_ID,
        session_id=DEAD_THREAD,
    )

    assert resp.session_restarted is True
    assert resp.session_id == NEW_THREAD
    calls = _calls(tmp_path)
    assert DEAD_THREAD in calls[0] and "resume" not in calls[1]
    assert await store.thread_for(USER_ID, workdir) == NEW_THREAD
