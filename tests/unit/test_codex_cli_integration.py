import json
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pytest

from src.agent_backends.codex_cli import CodexCliIntegration, CodexSessionStore
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
    return Settings(telegram_bot_token="x", telegram_bot_username="x")


async def test_run_command_streams_tools_and_returns_answer(tmp_path, settings):
    fake = _fake_codex(tmp_path)
    store = CodexSessionStore(tmp_path / "s.json")
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
    assert store.thread_for(7, tmp_path) == "th-xyz"  # session persisted


async def test_run_command_resumes_from_store(tmp_path, settings):
    fake = _fake_codex(tmp_path)
    store = CodexSessionStore(tmp_path / "s.json")
    store.record(7, tmp_path, "th-prev", 1, 1)
    captured = {}
    integ = CodexCliIntegration(
        config=settings, codex_bin=str(fake), session_store=store
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

    store = CodexSessionStore(tmp_path / "s.json")
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
        session_store=CodexSessionStore(tmp_path / "s.json"),
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
        session_store=CodexSessionStore(tmp_path / "s.json"),
        profile_registry=registry,
    )
    argv = integ._build_argv(
        model="openai-codex/gpt-5.4-mini",
        cwd=devops_dir,
        out_file=tmp_path / "o.txt",
        resume_id=None,
    )
    assert not any("mcp_servers" in a for a in argv)
