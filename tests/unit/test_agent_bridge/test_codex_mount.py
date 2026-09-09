"""The Codex side of the bridge: the mount, and the token's life.

A token is minted when ``run_command`` starts, exists ONLY in that child's
environment, and is revoked when the call returns — including when the turn
raises.
"""

import json
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pytest

from src.agent_backends.codex_cli import CodexCliIntegration, CodexSessionStore
from src.agent_bridge import TOKEN_ENV_VAR
from src.agent_bridge.tokens import TokenRegistry
from src.claude.tool_context import TurnContext
from src.config.settings import Settings
from src.profiles.registry import ProfileDefinition, ProfileRegistry


def _real_registry(working_dir: Path) -> ProfileRegistry:
    """A real registry: agent-id resolution is the thing under test."""
    return ProfileRegistry(
        [
            ProfileDefinition(
                slug="coach",
                name="coach",
                working_dir=working_dir,
                memory_dir=working_dir / "memory",
                persona=working_dir / "persona.md",
                default=True,
                user_data_tools=frozenset({"logs"}),
            )
        ]
    )


@dataclass
class _FakeProfile:
    working_dir: Path
    mcp: Optional[Path] = None
    slug: str = "coach"


class _FakeRegistry:
    def __init__(self, profiles):
        self._profiles = profiles

    def get_by_working_dir(self, path: Path):
        for profile in self._profiles:
            if profile.working_dir == path:
                return profile
        return None

    def get_by_slug(self, slug: str):
        for profile in self._profiles:
            if profile.slug == slug:
                return profile
        return None


class FakeBridge:
    """Records the tokens it hands out and the ones it takes back."""

    url = "http://127.0.0.1:8770/mcp"

    def __init__(self) -> None:
        self.tokens = TokenRegistry()
        self.minted: list = []
        self.revoked: list = []
        self.contexts: list = []

    def mint(self, context: TurnContext) -> str:
        self.contexts.append(context)
        token = self.tokens.mint(context)
        self.minted.append(token)
        return token

    def revoke(self, token) -> None:
        self.revoked.append(token)
        self.tokens.revoke(token)


def _env_dumping_codex(tmp_path: Path) -> Path:
    """A stand-in `codex` that writes its own environment to a file."""
    script = tmp_path / "codex"
    dump = tmp_path / "child_env.txt"
    script.write_text(
        "#!/usr/bin/env bash\n"
        f'env > "{dump}"\n'
        'outfile=""\n'
        'args=("$@")\n'
        "for ((i=0; i<${#args[@]}; i++)); do\n"
        '  if [[ "${args[$i]}" == "-o" ]]; then outfile="${args[$((i+1))]}"; fi\n'
        "done\n"
        'echo \'{"type":"thread.started","thread_id":"th-xyz"}\'\n'
        'echo \'{"type":"turn.completed","usage":{"input_tokens":1,"output_tokens":1}}\''
        "\n"
        'if [[ -n "$outfile" ]]; then echo -n ok > "$outfile"; fi\n'
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IRWXU)
    return script


@pytest.fixture
def settings():
    # enable_mcp is pinned explicitly: Settings reads the developer's real
    # .env, where ENABLE_MCP=true, and the stdio seam would then be mounted
    # from the real config/mcp.json in tests that are not about it.
    return Settings(
        telegram_bot_token="x",
        telegram_bot_username="x",
        enable_mcp=False,
        mcp_config_path=None,
    )


class TestMount:
    def test_bridge_is_mounted_on_every_profile(self, tmp_path, settings) -> None:
        integ = CodexCliIntegration(
            config=settings,
            session_store=CodexSessionStore(tmp_path / "s.json"),
            profile_registry=_FakeRegistry([_FakeProfile(working_dir=tmp_path)]),
            agent_bridge=FakeBridge(),
        )
        argv = integ._build_argv(
            model="openai-codex/gpt-5.4-mini",
            cwd=tmp_path,
            out_file=tmp_path / "o.txt",
            resume_id=None,
        )
        assert 'mcp_servers.agent.url="http://127.0.0.1:8770/mcp"' in argv
        assert f'mcp_servers.agent.bearer_token_env_var="{TOKEN_ENV_VAR}"' in argv

    def test_bridge_is_added_to_the_profile_seam_not_instead_of_it(
        self, tmp_path, settings
    ) -> None:
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
        integ = CodexCliIntegration(
            config=settings,
            session_store=CodexSessionStore(tmp_path / "s.json"),
            profile_registry=_FakeRegistry(
                [_FakeProfile(working_dir=channel_dir, mcp=seam)]
            ),
            agent_bridge=FakeBridge(),
        )
        argv = integ._build_argv(
            model="openai-codex/gpt-5.4-mini",
            cwd=channel_dir,
            out_file=tmp_path / "o.txt",
            resume_id=None,
        )
        assert 'mcp_servers.agent.url="http://127.0.0.1:8770/mcp"' in argv
        assert 'mcp_servers.channel_intel.url="http://127.0.0.1:8765/mcp"' in argv

    def test_no_bridge_no_mount(self, tmp_path, settings) -> None:
        integ = CodexCliIntegration(
            config=settings,
            session_store=CodexSessionStore(tmp_path / "s.json"),
            profile_registry=_FakeRegistry([_FakeProfile(working_dir=tmp_path)]),
        )
        argv = integ._build_argv(
            model="openai-codex/gpt-5.4-mini",
            cwd=tmp_path,
            out_file=tmp_path / "o.txt",
            resume_id=None,
        )
        assert not any("mcp_servers" in arg for arg in argv)


class TestStdioMcpMount:
    """`send_image_to_user` is a stdio MCP server, not a bridge tool.

    The Claude path mounts it from ``config/mcp.json`` through
    ``ClaudeAgentOptions.mcp_servers``; Codex gets the same server through
    ``-c mcp_servers.<name>.command/args/env``. One implementation, one
    transport per backend — verified live 2026-09-05 (docs/FINDINGS.md).
    """

    def _settings(self, tmp_path: Path, servers: dict):
        config = tmp_path / "mcp.json"
        config.write_text(json.dumps({"mcpServers": servers}))
        return Settings(
            telegram_bot_token="x",
            telegram_bot_username="x",
            enable_mcp=True,
            mcp_config_path=config,
        )

    def _integration(self, tmp_path: Path, settings) -> CodexCliIntegration:
        return CodexCliIntegration(
            config=settings,
            session_store=CodexSessionStore(tmp_path / "s.json"),
            profile_registry=_FakeRegistry([_FakeProfile(working_dir=tmp_path)]),
        )

    def test_the_telegram_server_is_mounted_with_command_args_and_roots(
        self, tmp_path
    ) -> None:
        settings = self._settings(
            tmp_path,
            {"telegram": {"command": "/usr/bin/python3", "args": ["/srv/tg.py"]}},
        )
        argv = self._integration(tmp_path, settings)._build_argv(
            model="m", cwd=tmp_path, out_file=tmp_path / "o.txt", resume_id=None
        )
        assert 'mcp_servers.telegram.command="/usr/bin/python3"' in argv
        assert 'mcp_servers.telegram.args=["/srv/tg.py"]' in argv
        roots = next(
            arg
            for arg in argv
            if arg.startswith("mcp_servers.telegram.env.TELEGRAM_IMAGE_ROOTS=")
        )
        # The turn's workspace must be deliverable, or the tool refuses every
        # chart the agent just drew.
        assert str(tmp_path) in roots

    def test_image_roots_follow_the_turns_working_dir(self, tmp_path) -> None:
        settings = self._settings(
            tmp_path, {"telegram": {"command": "python", "args": ["tg.py"]}}
        )
        other = tmp_path / "channel"
        other.mkdir()
        argv = self._integration(tmp_path, settings)._build_argv(
            model="m", cwd=other, out_file=tmp_path / "o.txt", resume_id=None
        )
        roots = next(
            arg
            for arg in argv
            if arg.startswith("mcp_servers.telegram.env.TELEGRAM_IMAGE_ROOTS=")
        )
        assert str(other) in roots

    def test_http_entries_are_left_to_the_profile_seam(self, tmp_path) -> None:
        """Mounting one server twice under two names doubles its tool list."""
        settings = self._settings(
            tmp_path, {"remote": {"type": "http", "url": "http://127.0.0.1:9/mcp"}}
        )
        argv = self._integration(tmp_path, settings)._build_argv(
            model="m", cwd=tmp_path, out_file=tmp_path / "o.txt", resume_id=None
        )
        assert not any("mcp_servers.remote" in arg for arg in argv)

    def test_mcp_disabled_mounts_nothing(self, tmp_path, settings) -> None:
        integ = self._integration(tmp_path, settings)
        assert integ._stdio_mcp_overrides(tmp_path) == []

    def test_an_unreadable_config_is_survivable(self, tmp_path) -> None:
        settings = self._settings(tmp_path, {"telegram": {"command": "python"}})
        (tmp_path / "mcp.json").write_text("{not json")
        assert (
            self._integration(tmp_path, settings)._stdio_mcp_overrides(tmp_path) == []
        )


class TestTokenLifecycle:
    def _integration(self, tmp_path, settings, bridge, binary=None):
        return CodexCliIntegration(
            config=settings,
            session_store=CodexSessionStore(tmp_path / "s.json"),
            codex_bin=str(binary or _env_dumping_codex(tmp_path)),
            profile_registry=_real_registry(tmp_path),
            agent_bridge=bridge,
        )

    async def test_token_reaches_the_child_env_and_dies_with_the_turn(
        self, tmp_path, settings
    ) -> None:
        bridge = FakeBridge()
        integ = self._integration(tmp_path, settings, bridge)

        await integ.run_command("hi", tmp_path, user_id=111, role="owner")

        (token,) = bridge.minted
        child_env = (tmp_path / "child_env.txt").read_text()
        assert f"{TOKEN_ENV_VAR}={token}" in child_env
        # revoked on return: the same token is now worthless
        assert bridge.revoked == [token]
        assert bridge.tokens.resolve(token) is None

    async def test_token_is_revoked_even_when_the_turn_fails(
        self, tmp_path, settings
    ) -> None:
        failing = tmp_path / "codex_fail"
        failing.write_text("#!/usr/bin/env bash\nexit 1\n")
        failing.chmod(failing.stat().st_mode | stat.S_IEXEC | stat.S_IRWXU)
        bridge = FakeBridge()
        integ = self._integration(tmp_path, settings, bridge, binary=failing)

        with pytest.raises(RuntimeError):
            await integ.run_command("hi", tmp_path, user_id=111)

        assert bridge.revoked == bridge.minted
        assert len(bridge.tokens) == 0

    async def test_each_turn_gets_its_own_token(self, tmp_path, settings) -> None:
        bridge = FakeBridge()
        integ = self._integration(tmp_path, settings, bridge)

        await integ.run_command("one", tmp_path, user_id=111)
        await integ.run_command("two", tmp_path, user_id=222)

        assert len(set(bridge.minted)) == 2
        assert len(bridge.tokens) == 0

    async def test_the_token_never_appears_on_the_command_line(
        self, tmp_path, settings
    ) -> None:
        """/proc/<pid>/cmdline is world-readable; the environment is not."""
        bridge = FakeBridge()
        integ = self._integration(tmp_path, settings, bridge)

        await integ.run_command("hi", tmp_path, user_id=111)

        (token,) = bridge.minted
        argv = integ._build_argv(
            model="m", cwd=tmp_path, out_file=tmp_path / "o.txt", resume_id=None
        )
        assert not any(token in arg for arg in argv)

    async def test_turn_context_carries_the_resolved_scopes(
        self, tmp_path, settings
    ) -> None:
        bridge = FakeBridge()
        integ = self._integration(tmp_path, settings, bridge)

        await integ.run_command(
            "hi",
            tmp_path,
            user_id=111,
            requester_chat_id=555,
            family_id=7,
            role="owner",
        )

        (ctx,) = bridge.contexts
        assert ctx.user_id == 111
        assert ctx.requester_chat_id == 555
        assert ctx.role == "owner"
        assert ctx.working_dir == tmp_path
        assert ctx.memory_agent_id and ctx.family_agent_id
        assert ctx.family_agent_id.startswith("f7:")
