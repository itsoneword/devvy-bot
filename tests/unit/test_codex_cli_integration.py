import json
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pytest

from src.agent_backends.codex_cli import CodexCliIntegration, InMemoryCodexSessionStore
from src.claude.prompt_composer import PromptComposer
from src.config.settings import Settings


@dataclass
class _FakeProfile:
    working_dir: Path
    mcp: Optional[Path] = None


class _FakeRegistry:
    def __init__(self, profiles):
        self._profiles = profiles

    def get_by_working_dir(self, path: Path):
        for p in self._profiles:
            if p.working_dir == path:
                return p
        return None


def _fake_codex(tmp_path: Path) -> Path:
    """A stand-in `codex` that emits canned JSONL and writes the -o file."""
    script = tmp_path / "codex"
    script.write_text(
        "#!/usr/bin/env bash\n"
        "set -e\n"
        'outfile=""\n'
        'args=("$@")\n'
        "for ((i=0; i<${#args[@]}; i++)); do\n"
        '  if [[ "${args[$i]}" == "-o" ]]; then outfile="${args[$((i+1))]}"; fi\n'
        "done\n"
        'echo \'{"type":"thread.started","thread_id":"th-xyz"}\'\n'
        'echo \'{"type":"item.started","item":{"type":"command_execution","command":"echo hi","status":"in_progress"}}\'\n'
        'echo \'{"type":"item.completed","item":{"type":"agent_message","text":"the answer"}}\'\n'
        'echo \'{"type":"turn.completed","usage":{"input_tokens":11,"output_tokens":4}}\'\n'
        'if [[ -n "$outfile" ]]; then echo -n \'the answer\' > "$outfile"; fi\n'
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IRWXU)
    return script


@pytest.fixture
def settings():
    # enable_mcp pinned: Settings reads the developer's real .env, where
    # ENABLE_MCP=true, and the stdio seam (config/mcp.json ->
    # send_image_to_user) would then be mounted in tests that are about the
    # profile seam. Its own coverage lives in test_agent_bridge/.
    return Settings(
        telegram_bot_token="x",
        telegram_bot_username="x",
        enable_mcp=False,
        mcp_config_path=None,
    )


async def test_run_command_streams_tools_and_returns_answer(tmp_path, settings):
    fake = _fake_codex(tmp_path)
    store = InMemoryCodexSessionStore()
    integ = CodexCliIntegration(
        config=settings, codex_bin=str(fake), session_store=store
    )

    seen = []

    async def on_stream(u):
        if u.tool_calls:
            seen.append(u.tool_calls[0]["name"])

    resp = await integ.run_command(
        prompt="hello",
        working_directory=tmp_path,
        user_id=7,
        session_id=None,
        on_stream=on_stream,
        force_new=True,
    )

    assert resp.content == "the answer"
    assert resp.session_id == "th-xyz"
    assert resp.input_tokens == 11 and resp.output_tokens == 4
    assert "Bash" in seen  # tool activity streamed
    assert await store.thread_for(7, tmp_path) == "th-xyz"  # session persisted


def test_argv_caps_foreground_subagents_at_two(tmp_path, settings):
    integ = CodexCliIntegration(
        config=settings,
        session_store=InMemoryCodexSessionStore(),
    )
    argv = integ._build_argv(
        model="openai-codex/gpt-5.6-sol",
        cwd=tmp_path,
        out_file=tmp_path / "out.txt",
        resume_id=None,
    )
    assert "agents.max_concurrent_threads_per_session=2" in argv


async def test_run_command_resumes_from_store(tmp_path, settings):
    fake = _fake_codex(tmp_path)
    store = InMemoryCodexSessionStore()
    await store.record(7, tmp_path, "th-prev", 1, 1)
    captured = {}
    mem = tmp_path / "mem"
    mem.mkdir()
    agent_md = tmp_path / "agent.md"
    agent_md.write_text("A deliberately large frozen persona.")
    integ = CodexCliIntegration(
        config=settings,
        codex_bin=str(fake),
        session_store=store,
        prompt_composer=PromptComposer(memory_path=mem, agent_md_path=agent_md),
    )
    orig = integ._build_argv
    integ._build_argv = lambda **kw: captured.setdefault("argv", orig(**kw))  # type: ignore

    await integ.run_command(
        prompt="again",
        working_directory=tmp_path,
        user_id=7,
        session_id=None,
        on_stream=None,
        force_new=False,
    )
    assert "resume" in captured["argv"] and "th-prev" in captured["argv"]
    # The persona already lives in th-prev's first message.  A resume must
    # append only the new user text (plus the per-turn <current_time> line,
    # dv-ca94 -- cheap and legitimately different every turn, unlike the
    # static persona bundle), or every turn permanently duplicates the
    # frozen bundle in the thread history.
    assert captured["argv"][-1].endswith("\n\nagain")
    assert "<current_time>" in captured["argv"][-1]
    assert "large frozen persona" not in captured["argv"][-1]


def _m_flag(argv):
    return argv[argv.index("-m") + 1]


async def test_run_command_model_kwarg_drives_the_m_argv(tmp_path, settings):
    """The model comes in on `run_command(model=)`, exactly as it does for
    Claude — not from a per-user dict on the integration (dv-4207): that was
    per-process, lost on restart, and raced when two chats shared a user."""
    fake = _fake_codex(tmp_path)
    integ = CodexCliIntegration(
        config=settings,
        codex_bin=str(fake),
        session_store=InMemoryCodexSessionStore(),
    )
    captured = []
    orig = integ._build_argv
    integ._build_argv = lambda **kw: (captured.append(orig(**kw)) or captured[-1])  # type: ignore

    await integ.run_command(
        prompt="hi",
        working_directory=tmp_path,
        user_id=7,
        force_new=True,
        model="openai-codex/gpt-5.5",
    )
    # Same user, different chat, a different pick: nothing on the object
    # remembers the previous call.
    await integ.run_command(
        prompt="hi",
        working_directory=tmp_path,
        user_id=7,
        force_new=True,
        model="openai-codex/gpt-5.4-mini",
    )

    assert [_m_flag(a) for a in captured] == ["gpt-5.5", "gpt-5.4-mini"]
    assert not hasattr(integ, "set_user_model")
    assert not hasattr(integ, "_models_by_user")


async def test_run_command_without_model_uses_the_config_default(tmp_path, settings):
    fake = _fake_codex(tmp_path)
    integ = CodexCliIntegration(
        config=settings,
        codex_bin=str(fake),
        session_store=InMemoryCodexSessionStore(),
    )
    captured = {}
    orig = integ._build_argv
    integ._build_argv = lambda **kw: captured.setdefault("argv", orig(**kw))  # type: ignore

    await integ.run_command(
        prompt="hi", working_directory=tmp_path, user_id=7, force_new=True
    )

    expected = settings.agent_model or "openai-codex/gpt-5.6-sol"
    assert _m_flag(captured["argv"]) == expected.split("/", 1)[-1]


async def test_run_command_does_not_overwrite_agents_md(tmp_path, settings):
    """Bot persona must be injected via the prompt, never by clobbering the
    project's human-authored AGENTS.md (root AGENTS.md is dev-only)."""
    fake = _fake_codex(tmp_path)
    work = tmp_path / "repo"
    work.mkdir()
    human = "# Human project instructions\nDo not clobber."
    (work / "AGENTS.md").write_text(human)

    mem = tmp_path / "mem"
    mem.mkdir()
    agent_md = tmp_path / "agent.md"
    agent_md.write_text("I am Atlas, the bot persona.")
    composer = PromptComposer(memory_path=mem, agent_md_path=agent_md)

    store = InMemoryCodexSessionStore()
    integ = CodexCliIntegration(
        config=settings,
        codex_bin=str(fake),
        session_store=store,
        prompt_composer=composer,
    )
    captured = {}
    orig = integ._build_argv
    integ._build_argv = lambda **kw: captured.setdefault("argv", orig(**kw))  # type: ignore

    await integ.run_command(
        prompt="user question",
        working_directory=work,
        user_id=7,
        session_id=None,
        on_stream=None,
        force_new=True,
    )

    # AGENTS.md left exactly as the human wrote it
    assert (work / "AGENTS.md").read_text() == human
    # persona is prepended to the prompt argument instead
    final_prompt = captured["argv"][-1]
    assert "Atlas, the bot persona" in final_prompt
    assert "user question" in final_prompt


def test_mcp_overrides_added_for_profile_with_seam(tmp_path, settings):
    seam = tmp_path / "channel-intel.json"
    seam.write_text(
        json.dumps(
            {
                "type": "http",
                "url": "http://127.0.0.1:8765/mcp",
                "auth_env": "CHANNEL_INTEL_TOKEN",
            }
        )
    )
    channel_dir = tmp_path / "channel"
    registry = _FakeRegistry([_FakeProfile(working_dir=channel_dir, mcp=seam)])
    integ = CodexCliIntegration(
        config=settings,
        session_store=InMemoryCodexSessionStore(),
        profile_registry=registry,
    )
    argv = integ._build_argv(
        model="openai-codex/gpt-5.4-mini",
        cwd=channel_dir,
        out_file=tmp_path / "o.txt",
        resume_id=None,
    )
    assert 'mcp_servers.channel_intel.url="http://127.0.0.1:8765/mcp"' in argv
    assert (
        'mcp_servers.channel_intel.bearer_token_env_var="CHANNEL_INTEL_TOKEN"' in argv
    )


def test_no_mcp_overrides_for_profile_without_seam(tmp_path, settings):
    devops_dir = tmp_path / "devops"
    registry = _FakeRegistry([_FakeProfile(working_dir=devops_dir, mcp=None)])
    integ = CodexCliIntegration(
        config=settings,
        session_store=InMemoryCodexSessionStore(),
        profile_registry=registry,
    )
    argv = integ._build_argv(
        model="openai-codex/gpt-5.4-mini",
        cwd=devops_dir,
        out_file=tmp_path / "o.txt",
        resume_id=None,
    )
    assert not any("mcp_servers" in a for a in argv)


def _failing_codex(tmp_path: Path) -> Path:
    """A `codex` that fails the way the real one does: the reason on STDOUT
    as an `error`/`turn.failed` event, and an empty stderr."""
    script = tmp_path / "codex_fail"
    script.write_text(
        "#!/usr/bin/env bash\n"
        'echo \'{"type":"thread.started","thread_id":"th-1"}\'\n'
        'echo \'{"type":"error","message":"400 invalid_request_error"}\'\n'
        'echo \'{"type":"turn.failed","error":'
        '{"message":"400 invalid_request_error"}}\'\n'
        "exit 1\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IRWXU)
    return script


async def test_failed_turn_reports_the_reason_from_stdout(tmp_path, settings):
    """Regression: the stderr drainer consumes the whole stream, so the old
    `await proc.stderr.read()` in the failure path always came back empty and
    the operator saw `Codex CLI failed (exit 1): ` with nothing after it."""
    integ = CodexCliIntegration(
        config=settings,
        codex_bin=str(_failing_codex(tmp_path)),
        session_store=InMemoryCodexSessionStore(),
    )

    with pytest.raises(RuntimeError) as excinfo:
        await integ.run_command(
            prompt="hi", working_directory=tmp_path, user_id=1, force_new=True
        )

    message = str(excinfo.value)
    assert "exit 1" in message
    assert "invalid_request_error" in message
    assert message.count("invalid_request_error") == 1  # turn.failed dedup


class TestCurrentTimezone:
    """dv-ca94: the host runs UTC, so the Codex CLI's own
    ``environment_context`` (and any builtin clock tool) needs TZ set
    explicitly rather than inherited."""

    def test_child_env_sets_tz_from_config(self, tmp_path, settings):
        integ = CodexCliIntegration(
            config=settings,
            codex_bin="codex",
            session_store=InMemoryCodexSessionStore(),
        )

        env = integ._child_env(tmp_path)

        assert env["TZ"] == settings.user_timezone == "Europe/Lisbon"

    async def test_run_command_prepends_current_time_line(self, tmp_path, settings):
        """The subprocess actually receives a <current_time> line ahead of
        the prompt -- verified via the fake codex echoing argv back."""
        script = tmp_path / "codex_echo"
        script.write_text(
            "#!/usr/bin/env bash\n"
            "set -e\n"
            'outfile=""\n'
            'args=("$@")\n'
            "for ((i=0; i<${#args[@]}; i++)); do\n"
            '  if [[ "${args[$i]}" == "-o" ]]; then outfile="${args[$((i+1))]}"; fi\n'
            "done\n"
            'prompt="${args[-1]}"\n'
            'echo \'{"type":"thread.started","thread_id":"th-time"}\'\n'
            'echo "{\\"type\\":\\"item.completed\\",\\"item\\":{\\"type\\":\\"agent_message\\",\\"text\\":\\"$prompt\\"}}"\n'
            'echo \'{"type":"turn.completed","usage":{"input_tokens":1,"output_tokens":1}}\'\n'
            'if [[ -n "$outfile" ]]; then printf "%s" "$prompt" > "$outfile"; fi\n'
        )
        script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IRWXU)
        integ = CodexCliIntegration(
            config=settings,
            codex_bin=str(script),
            session_store=InMemoryCodexSessionStore(),
        )

        response = await integ.run_command(
            prompt="hi", working_directory=tmp_path, user_id=1, force_new=True
        )

        assert "<current_time>" in response.content
        assert "Europe/Lisbon" in response.content
