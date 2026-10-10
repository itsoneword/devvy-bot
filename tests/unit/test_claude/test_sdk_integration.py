"""Test Claude SDK integration."""

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    TextBlock,
    ThinkingBlock,
    ToolPermissionContext,
    UserMessage,
)
from claude_agent_sdk.types import StreamEvent

from src.claude.sdk_integration import (
    ClaudeResponse,
    ClaudeSDKManager,
    StreamUpdate,
    _make_can_use_tool_callback,
)
from src.config.settings import Settings


@pytest.fixture(autouse=True)
def _patch_parse_message():
    """Patch parse_message as identity so mocks can yield typed Message objects."""
    with patch("src.claude.sdk_integration.parse_message", side_effect=lambda x: x):
        yield


def _make_assistant_message(text="Test response"):
    """Create an AssistantMessage with proper structure for current SDK version."""
    return AssistantMessage(
        content=[TextBlock(text=text)],
        model="claude-sonnet-4-20250514",
    )


def _make_result_message(**kwargs):
    """Create a ResultMessage with sensible defaults."""
    defaults = {
        "subtype": "success",
        "duration_ms": 1000,
        "duration_api_ms": 800,
        "is_error": False,
        "num_turns": 1,
        "session_id": "test-session",
        "total_cost_usd": 0.05,
        "result": "Success",
    }
    defaults.update(kwargs)
    return ResultMessage(**defaults)


def _mock_client(*messages):
    """Create a mock ClaudeSDKClient that yields the given messages.

    Returns a factory function suitable for patching ClaudeSDKClient.
    Uses connect()/disconnect() pattern (not async context manager).
    """
    client = AsyncMock()
    client.connect = AsyncMock()
    client.disconnect = AsyncMock()
    client.query = AsyncMock()

    async def receive_raw_messages():
        for msg in messages:
            yield msg

    query_mock = AsyncMock()
    query_mock.receive_messages = receive_raw_messages
    client._query = query_mock

    return client


def _mock_client_factory(*messages, capture_options=None):
    """Create a factory that returns a mock client, optionally capturing options."""

    def factory(options):
        if capture_options is not None:
            capture_options.append(options)
        return _mock_client(*messages)

    return factory


class TestClaudeSDKManager:
    """Test Claude SDK manager."""

    @pytest.fixture
    def config(self, tmp_path):
        """Create test config without API key."""
        return Settings(
            telegram_bot_token="test:token",
            telegram_bot_username="testbot",
            approved_directory=tmp_path,
            claude_timeout_seconds=2,  # Short timeout for testing
            enable_mcp=False,
        )

    @pytest.fixture
    def sdk_manager(self, config):
        """Create SDK manager."""
        return ClaudeSDKManager(config)

    async def test_sdk_manager_initialization_with_api_key(self, tmp_path):
        """Test SDK manager initialization with API key."""
        from src.config.settings import Settings

        # Test with API key provided
        config_with_key = Settings(
            telegram_bot_token="test:token",
            telegram_bot_username="testbot",
            approved_directory=tmp_path,
            anthropic_api_key="test-api-key",
            claude_timeout_seconds=2,
        )

        # Store original env var
        original_api_key = os.environ.get("ANTHROPIC_API_KEY")

        try:
            ClaudeSDKManager(config_with_key)

            # Check that API key was set in environment
            assert os.environ.get("ANTHROPIC_API_KEY") == "test-api-key"

        finally:
            # Restore original env var
            if original_api_key:
                os.environ["ANTHROPIC_API_KEY"] = original_api_key
            elif "ANTHROPIC_API_KEY" in os.environ:
                del os.environ["ANTHROPIC_API_KEY"]

    async def test_sdk_manager_initialization_without_api_key(self, config):
        """Test SDK manager initialization without API key (uses CLI auth)."""
        # Store original env var
        original_api_key = os.environ.get("ANTHROPIC_API_KEY")

        try:
            # Remove any existing API key
            if "ANTHROPIC_API_KEY" in os.environ:
                del os.environ["ANTHROPIC_API_KEY"]

            ClaudeSDKManager(config)

            # Check that no API key was set (should use CLI auth)
            assert config.anthropic_api_key_str is None

        finally:
            # Restore original env var
            if original_api_key:
                os.environ["ANTHROPIC_API_KEY"] = original_api_key

    async def test_execute_command_success(self, sdk_manager):
        """Test successful command execution."""
        mock_factory = _mock_client_factory(
            _make_assistant_message("Test response"),
            _make_result_message(session_id="test-session", total_cost_usd=0.05),
        )

        with patch(
            "src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_factory
        ):
            response = await sdk_manager.execute_command(
                prompt="Test prompt",
                working_directory=Path("/test"),
                session_id="test-session",
            )

        # Verify response
        assert isinstance(response, ClaudeResponse)
        assert response.session_id == "test-session"
        assert response.duration_ms >= 0
        assert not response.is_error
        assert response.cost == 0.05

    async def test_execute_command_uses_result_content(self, sdk_manager):
        """Test that ResultMessage.result is used for content when available."""
        mock_factory = _mock_client_factory(
            _make_assistant_message("Assistant text"),
            _make_result_message(result="Final result from ResultMessage"),
        )

        with patch(
            "src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_factory
        ):
            response = await sdk_manager.execute_command(
                prompt="Test prompt",
                working_directory=Path("/test"),
            )

        assert response.content == "Final result from ResultMessage"

    async def test_execute_command_falls_back_to_messages(self, sdk_manager):
        """Test fallback to message extraction when result is None."""
        mock_factory = _mock_client_factory(
            _make_assistant_message("Extracted from messages"),
            _make_result_message(result=None),
        )

        with patch(
            "src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_factory
        ):
            response = await sdk_manager.execute_command(
                prompt="Test prompt",
                working_directory=Path("/test"),
            )

        assert response.content == "Extracted from messages"

    async def test_execute_command_with_streaming(self, sdk_manager):
        """Test command execution with streaming callback."""
        stream_updates = []

        async def stream_callback(update: StreamUpdate):
            stream_updates.append(update)

        mock_factory = _mock_client_factory(
            _make_assistant_message("Test response"),
            _make_result_message(),
        )

        with patch(
            "src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_factory
        ):
            await sdk_manager.execute_command(
                prompt="Test prompt",
                working_directory=Path("/test"),
                stream_callback=stream_callback,
            )

        # Verify streaming was called
        assert len(stream_updates) > 0
        assert any(update.type == "assistant" for update in stream_updates)

    async def test_execute_command_timeout(self, sdk_manager):
        """Test command execution timeout."""
        from src.claude.exceptions import ClaudeTimeoutError

        client = AsyncMock()
        client.connect = AsyncMock()
        client.disconnect = AsyncMock()
        client.query = AsyncMock()

        async def hanging_receive():
            await asyncio.sleep(5)  # Exceeds 2s timeout
            yield  # Never reached

        query_mock = AsyncMock()
        query_mock.receive_messages = hanging_receive
        client._query = query_mock

        with patch("src.claude.sdk_integration.ClaudeSDKClient", return_value=client):
            with pytest.raises(ClaudeTimeoutError):
                await sdk_manager.execute_command(
                    prompt="Test prompt",
                    working_directory=Path("/test"),
                )

    async def test_execute_command_passes_mcp_config(self, tmp_path):
        """Test that MCP config is passed to ClaudeAgentOptions when enabled."""
        # Create a valid MCP config file
        mcp_config_file = tmp_path / "mcp_config.json"
        mcp_config_file.write_text(
            '{"mcpServers": {"test-server": {"command": "echo", "args": ["hello"]}}}'
        )

        config = Settings(
            telegram_bot_token="test:token",
            telegram_bot_username="testbot",
            approved_directory=tmp_path,
            claude_timeout_seconds=2,
            enable_mcp=True,
            mcp_config_path=str(mcp_config_file),
        )

        manager = ClaudeSDKManager(config)

        captured_options = []
        mock_factory = _mock_client_factory(
            _make_assistant_message("Test response"),
            _make_result_message(total_cost_usd=0.01),
            capture_options=captured_options,
        )

        with patch(
            "src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_factory
        ):
            await manager.execute_command(
                prompt="Test prompt",
                working_directory=tmp_path,
            )

        # Verify MCP config was parsed and passed as dict to options
        assert len(captured_options) == 1
        assert captured_options[0].mcp_servers == {
            "test-server": {"command": "echo", "args": ["hello"]}
        }

    async def test_execute_command_no_mcp_when_disabled(self, sdk_manager):
        """Test that MCP config is NOT passed when MCP is disabled."""
        captured_options = []
        mock_factory = _mock_client_factory(
            _make_assistant_message("Test response"),
            _make_result_message(total_cost_usd=0.01),
            capture_options=captured_options,
        )

        with patch(
            "src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_factory
        ):
            await sdk_manager.execute_command(
                prompt="Test prompt",
                working_directory=Path("/test"),
            )

        # Verify MCP config was NOT set (should be empty default)
        assert len(captured_options) == 1
        assert captured_options[0].mcp_servers == {}

    async def test_execute_command_passes_resume_session(self, sdk_manager):
        """Test that session_id is passed as options.resume for continuation."""
        captured_options = []
        mock_factory = _mock_client_factory(
            _make_assistant_message("Test response"),
            _make_result_message(session_id="test-session"),
            capture_options=captured_options,
        )

        with patch(
            "src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_factory
        ):
            await sdk_manager.execute_command(
                prompt="Continue working",
                working_directory=Path("/test"),
                session_id="existing-session-id",
                continue_session=True,
            )

        assert len(captured_options) == 1
        assert captured_options[0].resume == "existing-session-id"

    async def test_execute_command_passes_max_budget_usd(self, sdk_manager, config):
        """Test that max_budget_usd is passed from config to ClaudeAgentOptions."""
        captured_options = []
        mock_factory = _mock_client_factory(
            _make_assistant_message("Test response"),
            _make_result_message(total_cost_usd=0.01),
            capture_options=captured_options,
        )

        with patch(
            "src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_factory
        ):
            await sdk_manager.execute_command(
                prompt="Test prompt",
                working_directory=Path("/test"),
            )

        assert len(captured_options) == 1
        assert captured_options[0].max_budget_usd == config.claude_max_cost_per_request

    async def test_execute_command_owner_role_has_no_budget_cap(self, sdk_manager):
        """role="owner" removes the per-request SDK budget cap entirely."""
        captured_options = []
        mock_factory = _mock_client_factory(
            _make_assistant_message("Test response"),
            _make_result_message(total_cost_usd=0.01),
            capture_options=captured_options,
        )

        with patch(
            "src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_factory
        ):
            await sdk_manager.execute_command(
                prompt="Test prompt",
                working_directory=Path("/test"),
                role="owner",
            )

        assert len(captured_options) == 1
        assert captured_options[0].max_budget_usd is None

    async def test_execute_command_no_resume_for_new_session(self, sdk_manager):
        """Test that resume is not set for new sessions."""
        captured_options = []
        mock_factory = _mock_client_factory(
            _make_assistant_message("Test response"),
            _make_result_message(session_id="new-session"),
            capture_options=captured_options,
        )

        with patch(
            "src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_factory
        ):
            await sdk_manager.execute_command(
                prompt="New prompt",
                working_directory=Path("/test"),
                session_id=None,
                continue_session=False,
            )

        assert len(captured_options) == 1
        assert (
            not hasattr(captured_options[0], "resume") or not captured_options[0].resume
        )


class TestClaudeSandboxSettings:
    """Test sandbox and system_prompt settings on ClaudeAgentOptions."""

    @pytest.fixture
    def config(self, tmp_path):
        """Create test config with sandbox enabled."""
        return Settings(
            telegram_bot_token="test:token",
            telegram_bot_username="testbot",
            approved_directory=tmp_path,
            claude_timeout_seconds=2,
            sandbox_enabled=True,
            sandbox_excluded_commands=["git", "npm"],
        )

    @pytest.fixture
    def sdk_manager(self, config):
        return ClaudeSDKManager(config)

    async def test_sandbox_settings_passed_to_options(self, sdk_manager, tmp_path):
        """Test that sandbox settings are set on ClaudeAgentOptions."""
        from src.security.denylist import ProtectedDirs

        secrets = tmp_path / "root" / "secrets"
        archive = tmp_path / "archive"
        db = tmp_path / "root" / "db"
        for d in (secrets, archive, db):
            d.mkdir(parents=True)
        dirs = ProtectedDirs(everyone=(secrets, archive), tenant=(db,))
        captured_options = []
        mock_factory = _mock_client_factory(
            _make_assistant_message("Test response"),
            _make_result_message(total_cost_usd=0.01),
            capture_options=captured_options,
        )

        with (
            patch(
                "src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_factory
            ),
            patch("src.security.denylist.protected_dirs", return_value=dirs),
        ):
            await sdk_manager.execute_command(
                prompt="Test prompt",
                working_directory=tmp_path,
            )

        assert len(captured_options) == 1
        opts = captured_options[0]
        # autoAllowBashIfSandboxed must stay False: when true the CLI
        # auto-approves bash for sandboxed profiles and `sqlite3 data/bot.db`
        # never reaches can_use_tool -- the dv-6dbc cross-user leak.
        assert opts.sandbox == {
            "enabled": True,
            "autoAllowBashIfSandboxed": False,
            "excludedCommands": ["git", "npm"],
            # dv-2c1c: bwrap hides the data-root areas from sandboxed bash.
            "filesystem": {"denyRead": [str(secrets), str(archive), str(db)]},
        }

    async def test_system_prompt_set_with_working_directory(
        self, sdk_manager, tmp_path
    ):
        """Test that system_prompt references the working directory."""
        captured_options = []
        mock_factory = _mock_client_factory(
            _make_assistant_message("Test response"),
            _make_result_message(total_cost_usd=0.01),
            capture_options=captured_options,
        )

        with patch(
            "src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_factory
        ):
            await sdk_manager.execute_command(
                prompt="Test prompt",
                working_directory=tmp_path,
            )

        assert len(captured_options) == 1
        opts = captured_options[0]
        assert str(tmp_path) in opts.system_prompt
        assert "relative paths" in opts.system_prompt.lower()

    async def test_system_prompt_carries_voice_runtime_note(
        self, sdk_manager, tmp_path
    ):
        """dv-38ef: the agent is told which STT provider it runs on."""
        sdk_manager.config.enable_voice_messages = True
        sdk_manager.config.voice_provider = "elevenlabs"
        sdk_manager.config.elevenlabs_api_key = "xi"
        sdk_manager.config.voice_language = None
        captured_options = []
        mock_factory = _mock_client_factory(
            _make_assistant_message("Test response"),
            _make_result_message(total_cost_usd=0.01),
            capture_options=captured_options,
        )

        with patch(
            "src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_factory
        ):
            await sdk_manager.execute_command(
                prompt="Test prompt",
                working_directory=tmp_path,
            )

        prompt = captured_options[0].system_prompt
        assert "<runtime>" in prompt
        assert "ElevenLabs Scribe (model scribe_v2)" in prompt
        assert "auto-detected" in prompt
        assert "Do not inspect the codebase" in prompt

    async def test_system_prompt_omits_voice_note_when_voice_disabled(
        self, sdk_manager, tmp_path
    ):
        """No runtime note when voice messages are off."""
        sdk_manager.config.enable_voice_messages = False
        captured_options = []
        mock_factory = _mock_client_factory(
            _make_assistant_message("Test response"),
            _make_result_message(total_cost_usd=0.01),
            capture_options=captured_options,
        )

        with patch(
            "src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_factory
        ):
            await sdk_manager.execute_command(
                prompt="Test prompt",
                working_directory=tmp_path,
            )

        assert "<runtime>" not in captured_options[0].system_prompt

    async def test_disallowed_tools_passed_to_options(self, tmp_path):
        """Test that disallowed_tools from config are passed to ClaudeAgentOptions."""
        config = Settings(
            telegram_bot_token="test:token",
            telegram_bot_username="testbot",
            approved_directory=tmp_path,
            claude_timeout_seconds=2,
            claude_disallowed_tools=["WebFetch", "WebSearch"],
        )
        manager = ClaudeSDKManager(config)

        captured_options = []
        mock_factory = _mock_client_factory(
            _make_assistant_message("Test response"),
            _make_result_message(total_cost_usd=0.01),
            capture_options=captured_options,
        )

        with patch(
            "src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_factory
        ):
            await manager.execute_command(
                prompt="Test prompt",
                working_directory=tmp_path,
            )

        assert len(captured_options) == 1
        assert captured_options[0].disallowed_tools == ["WebFetch", "WebSearch"]

    async def test_allowed_tools_passed_to_options(self, tmp_path):
        """Test that allowed_tools from config are passed to ClaudeAgentOptions."""
        config = Settings(
            telegram_bot_token="test:token",
            telegram_bot_username="testbot",
            approved_directory=tmp_path,
            claude_timeout_seconds=2,
            claude_allowed_tools=["Read", "Write", "Bash"],
        )
        manager = ClaudeSDKManager(config)

        captured_options = []
        mock_factory = _mock_client_factory(
            _make_assistant_message("Test response"),
            _make_result_message(total_cost_usd=0.01),
            capture_options=captured_options,
        )

        with patch(
            "src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_factory
        ):
            await manager.execute_command(
                prompt="Test prompt",
                working_directory=tmp_path,
            )

        assert len(captured_options) == 1
        # Every path-taking tool (Read included) is stripped from the
        # auto-approve list so it routes through can_use_tool, where the
        # path/denylist checks live. Unconditional now -- gating it on
        # full_access meant only the trusted profile was validated (dv-6dbc).
        assert captured_options[0].allowed_tools == []

    async def test_disable_tool_validation_sets_allowed_tools_none(self, tmp_path):
        """allowed_tools=None when DISABLE_TOOL_VALIDATION=true."""
        config = Settings(
            telegram_bot_token="test:token",
            telegram_bot_username="testbot",
            approved_directory=tmp_path,
            claude_timeout_seconds=2,
            disable_tool_validation=True,
            claude_allowed_tools=["Read", "Write", "Bash"],
            claude_disallowed_tools=["WebFetch"],
        )
        manager = ClaudeSDKManager(config)

        captured_options: list = []
        mock_factory = _mock_client_factory(
            _make_assistant_message("Test response"),
            _make_result_message(total_cost_usd=0.01),
            capture_options=captured_options,
        )

        with patch(
            "src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_factory
        ):
            await manager.execute_command(
                prompt="Test prompt",
                working_directory=tmp_path,
            )

        assert len(captured_options) == 1
        assert captured_options[0].allowed_tools is None
        assert captured_options[0].disallowed_tools is None

    async def test_tool_validation_enabled_passes_configured_tools(self, tmp_path):
        """allowed/disallowed_tools passed when DISABLE_TOOL_VALIDATION=false."""
        config = Settings(
            telegram_bot_token="test:token",
            telegram_bot_username="testbot",
            approved_directory=tmp_path,
            claude_timeout_seconds=2,
            disable_tool_validation=False,
            claude_allowed_tools=["Read", "Write"],
            claude_disallowed_tools=["WebFetch"],
        )
        manager = ClaudeSDKManager(config)

        captured_options: list = []
        mock_factory = _mock_client_factory(
            _make_assistant_message("Test response"),
            _make_result_message(total_cost_usd=0.01),
            capture_options=captured_options,
        )

        with patch(
            "src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_factory
        ):
            await manager.execute_command(
                prompt="Test prompt",
                working_directory=tmp_path,
            )

        assert len(captured_options) == 1
        # Both stripped to route through can_use_tool -- see dv-6dbc.
        assert captured_options[0].allowed_tools == []
        assert captured_options[0].disallowed_tools == ["WebFetch"]

    async def test_empty_cli_path_coerced_to_none(self, tmp_path):
        """Empty CLAUDE_CLI_PATH ('') is coerced to None so SDK auto-discovers the CLI."""
        config = Settings(
            telegram_bot_token="test:token",
            telegram_bot_username="testbot",
            approved_directory=tmp_path,
            claude_timeout_seconds=2,
            claude_cli_path="",
        )
        manager = ClaudeSDKManager(config)

        captured_options = []
        mock_factory = _mock_client_factory(
            _make_assistant_message("Test response"),
            _make_result_message(total_cost_usd=0.01),
            capture_options=captured_options,
        )

        with patch(
            "src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_factory
        ):
            await manager.execute_command(
                prompt="Test prompt",
                working_directory=tmp_path,
            )

        assert len(captured_options) == 1
        assert captured_options[0].cli_path is None

    async def test_sandbox_disabled_when_config_false(self, tmp_path):
        """Test sandbox is disabled when sandbox_enabled=False."""
        config = Settings(
            telegram_bot_token="test:token",
            telegram_bot_username="testbot",
            approved_directory=tmp_path,
            claude_timeout_seconds=2,
            sandbox_enabled=False,
        )
        manager = ClaudeSDKManager(config)

        captured_options = []
        mock_factory = _mock_client_factory(
            _make_assistant_message("Test response"),
            _make_result_message(total_cost_usd=0.01),
            capture_options=captured_options,
        )

        with patch(
            "src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_factory
        ):
            await manager.execute_command(
                prompt="Test prompt",
                working_directory=tmp_path,
            )

        assert len(captured_options) == 1
        assert captured_options[0].sandbox["enabled"] is False

    async def test_claude_model_passed_to_options(self, tmp_path):
        """Test that claude_model from config is passed to ClaudeAgentOptions."""
        config = Settings(
            telegram_bot_token="test:token",
            telegram_bot_username="testbot",
            approved_directory=tmp_path,
            claude_timeout_seconds=2,
            claude_model="claude-sonnet-4-20250514",
        )
        manager = ClaudeSDKManager(config)

        captured_options = []
        mock_factory = _mock_client_factory(
            _make_assistant_message("Test response"),
            _make_result_message(total_cost_usd=0.01),
            capture_options=captured_options,
        )

        with patch(
            "src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_factory
        ):
            await manager.execute_command(
                prompt="Test prompt",
                working_directory=tmp_path,
            )

        assert len(captured_options) == 1
        assert captured_options[0].model == "claude-sonnet-4-20250514"

    async def test_claude_model_none_when_unset(self, tmp_path):
        """Test that model is None when claude_model is not configured."""
        config = Settings(
            telegram_bot_token="test:token",
            telegram_bot_username="testbot",
            approved_directory=tmp_path,
            claude_timeout_seconds=2,
            # Explicit None so a CLAUDE_MODEL in the deployment .env
            # can't leak into this test.
            claude_model=None,
        )
        manager = ClaudeSDKManager(config)

        captured_options = []
        mock_factory = _mock_client_factory(
            _make_assistant_message("Test response"),
            _make_result_message(total_cost_usd=0.01),
            capture_options=captured_options,
        )

        with patch(
            "src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_factory
        ):
            await manager.execute_command(
                prompt="Test prompt",
                working_directory=tmp_path,
            )

        assert len(captured_options) == 1
        assert captured_options[0].model is None


class TestClaudeMCPErrors:
    """Test MCP-specific error handling."""

    @pytest.fixture
    def config(self, tmp_path):
        """Create test config."""
        return Settings(
            telegram_bot_token="test:token",
            telegram_bot_username="testbot",
            approved_directory=tmp_path,
            claude_timeout_seconds=2,
        )

    @pytest.fixture
    def sdk_manager(self, config):
        """Create SDK manager."""
        return ClaudeSDKManager(config)

    async def test_mcp_connection_error_raises_mcp_error(self, sdk_manager):
        """Test that MCP connection errors raise ClaudeMCPError."""
        from claude_agent_sdk import CLIConnectionError

        from src.claude.exceptions import ClaudeMCPError

        client = AsyncMock()
        client.connect = AsyncMock()
        client.disconnect = AsyncMock()
        client.query = AsyncMock(
            side_effect=CLIConnectionError("MCP server failed to start")
        )

        with patch("src.claude.sdk_integration.ClaudeSDKClient", return_value=client):
            with pytest.raises(ClaudeMCPError) as exc_info:
                await sdk_manager.execute_command(
                    prompt="Test prompt",
                    working_directory=Path("/test"),
                )

        assert "MCP server" in str(exc_info.value)

    async def test_mcp_process_error_raises_mcp_error(self, sdk_manager):
        """Test that MCP process errors raise ClaudeMCPError."""
        from claude_agent_sdk import ProcessError

        from src.claude.exceptions import ClaudeMCPError

        client = AsyncMock()
        client.connect = AsyncMock()
        client.disconnect = AsyncMock()
        client.query = AsyncMock(
            side_effect=ProcessError("Failed to start MCP server: connection refused")
        )

        with patch("src.claude.sdk_integration.ClaudeSDKClient", return_value=client):
            with pytest.raises(ClaudeMCPError) as exc_info:
                await sdk_manager.execute_command(
                    prompt="Test prompt",
                    working_directory=Path("/test"),
                )

        assert "MCP" in str(exc_info.value)


class TestCanUseToolCallback:
    """Test the _make_can_use_tool_callback factory and its behavior."""

    @pytest.fixture
    def approved_dir(self, tmp_path):
        return tmp_path

    @pytest.fixture
    def working_dir(self, tmp_path):
        return tmp_path / "project"

    @pytest.fixture
    def security_validator(self):
        """Create a mock SecurityValidator."""
        validator = MagicMock()
        validator.validate_path = MagicMock(return_value=(True, Path("/ok"), None))
        return validator

    @pytest.fixture
    def callback(self, security_validator, working_dir, approved_dir):
        return _make_can_use_tool_callback(
            security_validator=security_validator,
            working_directory=working_dir,
            approved_directory=approved_dir,
        )

    @pytest.fixture
    def context(self):
        return ToolPermissionContext()

    async def test_allows_safe_file_read(self, callback, context, security_validator):
        """File read with a valid path is allowed."""
        result = await callback("Read", {"file_path": "src/main.py"}, context)
        assert isinstance(result, PermissionResultAllow)
        security_validator.validate_path.assert_called_once()

    async def test_denies_invalid_file_path(
        self, callback, context, security_validator
    ):
        """File write with a path that fails validation is denied."""
        security_validator.validate_path.return_value = (
            False,
            None,
            "Path traversal detected",
        )
        result = await callback("Write", {"file_path": "../../etc/passwd"}, context)
        assert isinstance(result, PermissionResultDeny)
        assert "Path traversal" in result.message

    async def test_allows_bash_inside_boundary(
        self, callback, context, working_dir, approved_dir
    ):
        """Bash command targeting inside approved dir is allowed."""
        result = await callback(
            "Bash", {"command": f"mkdir -p {approved_dir}/subdir"}, context
        )
        assert isinstance(result, PermissionResultAllow)

    async def test_denies_bash_outside_boundary(self, callback, context):
        """Bash command targeting outside approved dir is denied."""
        result = await callback("Bash", {"command": "mkdir -p /tmp/evil"}, context)
        assert isinstance(result, PermissionResultDeny)
        assert "boundary violation" in result.message.lower()

    async def test_allows_unknown_tool(self, callback, context):
        """Tools not in file/bash sets are allowed through."""
        result = await callback("Grep", {"pattern": "foo"}, context)
        assert isinstance(result, PermissionResultAllow)

    async def test_allows_bash_read_only_command(self, callback, context):
        """Read-only bash commands pass through even with external paths."""
        result = await callback("Bash", {"command": "cat /etc/hosts"}, context)
        assert isinstance(result, PermissionResultAllow)

    async def test_file_tool_without_path_allowed(self, callback, context):
        """File tool call without a path key is allowed (no path to validate)."""
        result = await callback("Read", {"content": "something"}, context)
        assert isinstance(result, PermissionResultAllow)

    async def test_wired_into_sdk_manager(self, tmp_path):
        """SecurityValidator is wired into options.can_use_tool by execute_command."""
        validator = MagicMock()
        validator.validate_path = MagicMock(return_value=(True, tmp_path, None))

        config = Settings(
            telegram_bot_token="test:token",
            telegram_bot_username="testbot",
            approved_directory=tmp_path,
            claude_timeout_seconds=2,
        )
        manager = ClaudeSDKManager(config, security_validator=validator)

        captured_options = []
        mock_factory = _mock_client_factory(
            _make_assistant_message("ok"),
            _make_result_message(total_cost_usd=0.01),
            capture_options=captured_options,
        )

        with patch(
            "src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_factory
        ):
            await manager.execute_command(prompt="Test", working_directory=tmp_path)

        assert len(captured_options) == 1
        assert captured_options[0].can_use_tool is not None

    async def test_no_callback_without_security_validator(self, tmp_path):
        """Verify can_use_tool is None when no SecurityValidator is provided."""
        config = Settings(
            telegram_bot_token="test:token",
            telegram_bot_username="testbot",
            approved_directory=tmp_path,
            claude_timeout_seconds=2,
        )
        manager = ClaudeSDKManager(config)

        captured_options = []
        mock_factory = _mock_client_factory(
            _make_assistant_message("ok"),
            _make_result_message(total_cost_usd=0.01),
            capture_options=captured_options,
        )

        with patch(
            "src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_factory
        ):
            await manager.execute_command(prompt="Test", working_directory=tmp_path)

        assert len(captured_options) == 1
        assert captured_options[0].can_use_tool is None


class TestSessionIdFallback:
    """Test fallback session ID extraction from StreamEvent messages."""

    @pytest.fixture
    def config(self, tmp_path):
        return Settings(
            telegram_bot_token="test:token",
            telegram_bot_username="testbot",
            approved_directory=tmp_path,
            claude_timeout_seconds=2,
        )

    @pytest.fixture
    def sdk_manager(self, config):
        return ClaudeSDKManager(config)

    async def test_session_id_from_stream_event_fallback(self, sdk_manager):
        """Test that session_id is extracted from StreamEvent when ResultMessage has None."""
        stream_event = StreamEvent(
            uuid="evt-1",
            session_id="stream-session-123",
            event={"type": "content_block_delta"},
        )
        mock_factory = _mock_client_factory(
            stream_event,
            _make_assistant_message("Test response"),
            _make_result_message(session_id=None, result="Done"),
        )

        with patch(
            "src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_factory
        ):
            response = await sdk_manager.execute_command(
                prompt="Test prompt",
                working_directory=Path("/test"),
            )

        assert response.session_id == "stream-session-123"

    async def test_session_id_from_stream_event_empty_string(self, sdk_manager):
        """Test fallback triggers when ResultMessage session_id is empty string."""
        stream_event = StreamEvent(
            uuid="evt-1",
            session_id="stream-session-456",
            event={"type": "content_block_delta"},
        )
        mock_factory = _mock_client_factory(
            stream_event,
            _make_assistant_message("Test response"),
            _make_result_message(session_id="", result="Done"),
        )

        with patch(
            "src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_factory
        ):
            response = await sdk_manager.execute_command(
                prompt="Test prompt",
                working_directory=Path("/test"),
            )

        assert response.session_id == "stream-session-456"

    async def test_no_fallback_when_result_has_session_id(self, sdk_manager):
        """Test that ResultMessage session_id takes priority over StreamEvent."""
        stream_event = StreamEvent(
            uuid="evt-1",
            session_id="stream-session-999",
            event={"type": "content_block_delta"},
        )
        mock_factory = _mock_client_factory(
            stream_event,
            _make_assistant_message("Test response"),
            _make_result_message(session_id="result-session-abc", result="Done"),
        )

        with patch(
            "src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_factory
        ):
            response = await sdk_manager.execute_command(
                prompt="Test prompt",
                working_directory=Path("/test"),
            )

        # ResultMessage session_id should win
        assert response.session_id == "result-session-abc"

    async def test_fallback_skips_stream_events_without_session_id(self, sdk_manager):
        """Test that StreamEvents without session_id are skipped in fallback."""
        stream_event_no_id = StreamEvent(
            uuid="evt-1",
            session_id=None,
            event={"type": "content_block_start"},
        )
        stream_event_with_id = StreamEvent(
            uuid="evt-2",
            session_id="found-session",
            event={"type": "content_block_delta"},
        )
        mock_factory = _mock_client_factory(
            stream_event_no_id,
            stream_event_with_id,
            _make_assistant_message("Test response"),
            _make_result_message(session_id=None, result="Done"),
        )

        with patch(
            "src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_factory
        ):
            response = await sdk_manager.execute_command(
                prompt="Test prompt",
                working_directory=Path("/test"),
            )

        assert response.session_id == "found-session"

    async def test_no_session_id_anywhere_falls_back_to_input(self, sdk_manager):
        """Test that input session_id is used when neither ResultMessage nor StreamEvent provide one."""
        mock_factory = _mock_client_factory(
            _make_assistant_message("Test response"),
            _make_result_message(session_id=None, result="Done"),
        )

        with patch(
            "src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_factory
        ):
            response = await sdk_manager.execute_command(
                prompt="Test prompt",
                working_directory=Path("/test"),
                session_id="input-session-id",
            )

        # Should fall back to the input session_id
        assert response.session_id == "input-session-id"


class TestClaudeMdLoading:
    """CLAUDE.md must be left to the CLI, not injected into the system prompt.

    `setting_sources` includes "project", and the CLI loads {cwd}/CLAUDE.md for
    that source. The bot used to read the file and prepend it as well, putting
    the whole thing in context twice.
    """

    @pytest.fixture
    def config(self, tmp_path):
        return Settings(
            telegram_bot_token="test:token",
            telegram_bot_username="test_bot",
            approved_directory=str(tmp_path),
        )

    @pytest.fixture
    def sdk_manager(self, config):
        return ClaudeSDKManager(config)

    async def test_claude_md_not_injected_into_system_prompt(
        self, sdk_manager, tmp_path
    ):
        """CLAUDE.md stays out of system_prompt; the CLI loads it instead."""
        claude_md = tmp_path / "CLAUDE.md"
        claude_md.write_text("# Project Rules\nAlways use type hints.")

        captured: list = []
        mock_factory = _mock_client_factory(
            _make_assistant_message("ok"),
            _make_result_message(),
            capture_options=captured,
        )

        with patch(
            "src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_factory
        ):
            await sdk_manager.execute_command(prompt="test", working_directory=tmp_path)

        opts = captured[0]
        assert "# Project Rules" not in opts.system_prompt
        assert "Always use type hints." not in opts.system_prompt
        # ...but the setting source that makes the CLI load it must stay on.
        assert "project" in (opts.setting_sources or [])

    async def test_system_prompt_unchanged_without_claude_md(
        self, sdk_manager, tmp_path
    ):
        """System prompt is just the base when no CLAUDE.md exists."""
        captured: list = []
        mock_factory = _mock_client_factory(
            _make_assistant_message("ok"),
            _make_result_message(),
            capture_options=captured,
        )

        with patch(
            "src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_factory
        ):
            await sdk_manager.execute_command(prompt="test", working_directory=tmp_path)

        opts = captured[0]
        assert "Use relative paths." in opts.system_prompt
        assert "# Project Rules" not in opts.system_prompt

    async def test_setting_sources_includes_project(self, sdk_manager, tmp_path):
        """setting_sources=['project'] is passed to ClaudeAgentOptions."""
        captured: list = []
        mock_factory = _mock_client_factory(
            _make_assistant_message("ok"),
            _make_result_message(),
            capture_options=captured,
        )

        with patch(
            "src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_factory
        ):
            await sdk_manager.execute_command(prompt="test", working_directory=tmp_path)

        opts = captured[0]
        assert opts.setting_sources == ["project"]


class TestThinkingBlockStreaming:
    """ThinkingBlocks surface as reasoning text, never as a dataclass repr.

    Regression cover for dv-4b71: a message whose content held only thinking
    blocks used to fall through to a ``str(content)`` fallback and put
    ``[ThinkingBlock(thinking='', signature='CAIS...')]`` in the progress
    message.
    """

    @pytest.fixture
    def config(self, tmp_path):
        return Settings(
            telegram_bot_token="test:token",
            telegram_bot_username="testbot",
            approved_directory=tmp_path,
            claude_timeout_seconds=2,
            enable_mcp=False,
        )

    @pytest.fixture
    def sdk_manager(self, config):
        return ClaudeSDKManager(config)

    @staticmethod
    def _collector():
        updates: list = []

        async def callback(update: StreamUpdate) -> None:
            updates.append(update)

        return updates, callback

    @staticmethod
    def _assert_no_repr(updates) -> None:
        """No dataclass repr may appear in any content the user could see."""
        for upd in updates:
            text = upd.content or ""
            assert "ThinkingBlock(" not in text
            assert "TextBlock(" not in text
            assert "ToolUseBlock(" not in text
            assert "signature=" not in text
            assert not text.startswith("[")

    async def test_thinking_only_message_yields_thinking_update(self, sdk_manager):
        """Content of only ThinkingBlocks emits reasoning text, not a repr."""
        updates, callback = self._collector()
        message = AssistantMessage(
            content=[
                ThinkingBlock(thinking="First I check the log.", signature="sig-a"),
                ThinkingBlock(thinking="Then I restart it.", signature="sig-b"),
            ],
            model="claude-sonnet-4-20250514",
        )

        await sdk_manager._handle_stream_message(message, callback)

        assert [u.type for u in updates] == ["thinking"]
        assert updates[0].content == "First I check the log.\nThen I restart it."
        self._assert_no_repr(updates)

    async def test_redacted_thinking_block_emits_nothing(self, sdk_manager):
        """Redacted blocks (thinking='') produce no update at all."""
        updates, callback = self._collector()
        message = AssistantMessage(
            content=[
                ThinkingBlock(
                    thinking="",
                    signature="CAIS1wQKhwEIEBgCKkC53BJ2rTY1bGcWeduwBJ3OM",
                )
            ],
            model="claude-sonnet-4-20250514",
        )

        await sdk_manager._handle_stream_message(message, callback)

        assert updates == []

    async def test_whitespace_only_thinking_emits_nothing(self, sdk_manager):
        """Blocks holding only whitespace are treated as redacted."""
        updates, callback = self._collector()
        message = AssistantMessage(
            content=[ThinkingBlock(thinking="   \n\t ", signature="sig")],
            model="claude-sonnet-4-20250514",
        )

        await sdk_manager._handle_stream_message(message, callback)

        assert updates == []

    async def test_mixed_text_and_thinking(self, sdk_manager):
        """Thinking and assistant text are emitted as separate updates."""
        updates, callback = self._collector()
        message = AssistantMessage(
            content=[
                ThinkingBlock(thinking="The service is masked.", signature="sig"),
                TextBlock(text="Unmasking and restarting it now."),
            ],
            model="claude-sonnet-4-20250514",
        )

        await sdk_manager._handle_stream_message(message, callback)

        assert [u.type for u in updates] == ["thinking", "assistant"]
        assert updates[0].content == "The service is masked."
        assert updates[1].content == "Unmasking and restarting it now."
        self._assert_no_repr(updates)

    async def test_mixed_thinking_and_tool_use(self, sdk_manager):
        """Tool calls still ride on the assistant update alongside thinking."""
        from claude_agent_sdk import ToolUseBlock

        updates, callback = self._collector()
        message = AssistantMessage(
            content=[
                ThinkingBlock(thinking="Need the log.", signature="sig"),
                ToolUseBlock(id="t1", name="Bash", input={"command": "journalctl"}),
            ],
            model="claude-sonnet-4-20250514",
        )

        await sdk_manager._handle_stream_message(message, callback)

        assert [u.type for u in updates] == ["thinking", "assistant"]
        assert updates[1].tool_calls == [
            {"id": "t1", "name": "Bash", "input": {"command": "journalctl"}}
        ]
        assert updates[1].content is None
        self._assert_no_repr(updates)

    async def test_unknown_block_type_never_reaches_output(self, sdk_manager):
        """A future block with neither .text nor .thinking is dropped silently."""

        @dataclass
        class MysteryBlock:
            payload: str

        updates, callback = self._collector()
        message = AssistantMessage(
            content=[MysteryBlock(payload="secret")],
            model="claude-sonnet-4-20250514",
        )

        await sdk_manager._handle_stream_message(message, callback)

        assert updates == []

    async def test_non_list_content_still_falls_back_to_str(self, sdk_manager):
        """The str() fallback survives for genuinely non-list content."""
        updates, callback = self._collector()
        message = AssistantMessage(content="plain string payload", model="m")

        await sdk_manager._handle_stream_message(message, callback)

        assert [u.type for u in updates] == ["assistant"]
        assert updates[0].content == "plain string payload"

    async def test_user_message_block_list_is_not_stringified(self, sdk_manager):
        """UserMessage tool-result block lists never become a repr string."""
        from claude_agent_sdk import ToolResultBlock

        updates, callback = self._collector()
        message = UserMessage(
            content=[ToolResultBlock(tool_use_id="t1", content="done")],
            parent_tool_use_id=None,
        )

        await sdk_manager._handle_stream_message(message, callback)

        assert updates == []

    async def test_streaming_end_to_end_reports_thinking(self, sdk_manager, tmp_path):
        """A full execute_command run surfaces thinking through the callback."""
        updates, callback = self._collector()
        thinking_msg = AssistantMessage(
            content=[ThinkingBlock(thinking="Weighing options.", signature="sig")],
            model="claude-sonnet-4-20250514",
        )
        mock_factory = _mock_client_factory(
            thinking_msg,
            _make_assistant_message("Done."),
            _make_result_message(),
        )

        with patch(
            "src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_factory
        ):
            await sdk_manager.execute_command(
                prompt="Test prompt",
                working_directory=tmp_path,
                stream_callback=callback,
            )

        thinking = [u for u in updates if u.type == "thinking"]
        assert [u.content for u in thinking] == ["Weighing options."]
        self._assert_no_repr(updates)
