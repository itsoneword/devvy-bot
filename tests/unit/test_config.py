"""Test configuration loading and validation."""

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import ValidationError

import src.config.loader as config_loader
from src.config import Settings, create_test_config, load_config
from src.config.features import FeatureFlags
from src.exceptions import ConfigurationError


@pytest.fixture(autouse=True)
def _restore_environ():
    """Snapshot/restore os.environ so a test's raw env writes can't leak into
    later tests (some tests here set os.environ directly without monkeypatch)."""
    snapshot = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(snapshot)


def test_settings_validation_required_fields(monkeypatch):
    """Test that missing required fields raise validation errors."""
    # Clear any environment variables that might provide defaults
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_USERNAME", raising=False)
    monkeypatch.delenv("APPROVED_DIRECTORY", raising=False)

    with pytest.raises(ValidationError) as exc_info:
        Settings(_env_file=None)

    errors = exc_info.value.errors()
    required_fields = {error["loc"][0] for error in errors}
    assert "telegram_bot_token" in required_fields
    assert "telegram_bot_username" in required_fields
    # approved_directory is now optional (denylist mode)


def test_settings_with_valid_data(tmp_path):
    """Test settings creation with valid data."""
    test_dir = tmp_path / "projects"
    test_dir.mkdir()

    settings = Settings(
        telegram_bot_token="test_token",
        telegram_bot_username="test_bot",
        approved_directory=str(test_dir),
    )

    assert settings.telegram_token_str == "test_token"
    assert settings.telegram_bot_username == "test_bot"
    assert settings.approved_directory == test_dir


def test_allowed_users_parsing():
    """Test parsing of comma-separated user IDs."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        settings = Settings(
            telegram_bot_token="test_token",
            telegram_bot_username="test_bot",
            approved_directory=tmp_dir,
            allowed_users="123,456,789",
        )

        assert settings.allowed_users == [123, 456, 789]


def test_allowed_users_parsing_with_spaces():
    """Test parsing with spaces around user IDs."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        settings = Settings(
            telegram_bot_token="test_token",
            telegram_bot_username="test_bot",
            approved_directory=tmp_dir,
            allowed_users="123, 456 , 789",
        )

        assert settings.allowed_users == [123, 456, 789]


def test_security_relaxation_settings_defaults_and_overrides():
    """Security relaxation settings should default to False and be configurable."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        defaults = Settings(
            telegram_bot_token="test_token",
            telegram_bot_username="test_bot",
            approved_directory=tmp_dir,
        )
        assert defaults.disable_security_patterns is False
        assert defaults.disable_tool_validation is False

        overridden = Settings(
            telegram_bot_token="test_token",
            telegram_bot_username="test_bot",
            approved_directory=tmp_dir,
            disable_security_patterns=True,
            disable_tool_validation=True,
        )
        assert overridden.disable_security_patterns is True
        assert overridden.disable_tool_validation is True


def test_approved_directory_validation_nonexistent():
    """Test validation fails for non-existent directory."""
    with pytest.raises(ValidationError) as exc_info:
        Settings(
            telegram_bot_token="test_token",
            telegram_bot_username="test_bot",
            approved_directory="/nonexistent/directory",
        )

    assert "does not exist" in str(exc_info.value)


def test_approved_directory_validation_not_directory(tmp_path):
    """Test validation fails when path is not a directory."""
    test_file = tmp_path / "not_a_dir.txt"
    test_file.write_text("test")

    with pytest.raises(ValidationError) as exc_info:
        Settings(
            telegram_bot_token="test_token",
            telegram_bot_username="test_bot",
            approved_directory=str(test_file),
        )

    assert "not a directory" in str(exc_info.value)


def test_auth_token_validation():
    """Test auth token secret validation."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        # Should fail when token auth enabled but no secret
        with pytest.raises(ValidationError) as exc_info:
            Settings(
                telegram_bot_token="test_token",
                telegram_bot_username="test_bot",
                approved_directory=tmp_dir,
                enable_token_auth=True,
            )

        assert "auth_token_secret required" in str(exc_info.value)

        # Should succeed when both enabled and secret provided
        settings = Settings(
            telegram_bot_token="test_token",
            telegram_bot_username="test_bot",
            approved_directory=tmp_dir,
            enable_token_auth=True,
            auth_token_secret="secret123",
        )

        assert settings.enable_token_auth is True
        assert settings.auth_secret_str == "secret123"


def test_mcp_config_validation(tmp_path, monkeypatch):
    """Test MCP configuration validation."""
    test_dir = tmp_path / "projects"
    test_dir.mkdir()

    # Clear any MCP-related environment variables
    monkeypatch.delenv("ENABLE_MCP", raising=False)
    monkeypatch.delenv("MCP_CONFIG_PATH", raising=False)

    # Should fail when MCP enabled but no config path
    with pytest.raises(ValidationError) as exc_info:
        Settings(
            telegram_bot_token="test_token",
            telegram_bot_username="test_bot",
            approved_directory=str(test_dir),
            enable_mcp=True,
            mcp_config_path=None,
        )

    assert "mcp_config_path required" in str(exc_info.value)

    # Should fail when config file doesn't exist
    with pytest.raises(ValidationError) as exc_info:
        Settings(
            telegram_bot_token="test_token",
            telegram_bot_username="test_bot",
            approved_directory=str(test_dir),
            enable_mcp=True,
            mcp_config_path="/nonexistent/config.json",
        )

    assert "does not exist" in str(exc_info.value)

    # Should fail when config file is not valid JSON
    bad_json_file = tmp_path / "bad.json"
    bad_json_file.write_text("not json at all")

    with pytest.raises(ValidationError) as exc_info:
        Settings(
            telegram_bot_token="test_token",
            telegram_bot_username="test_bot",
            approved_directory=str(test_dir),
            enable_mcp=True,
            mcp_config_path=str(bad_json_file),
        )

    assert "not valid JSON" in str(exc_info.value)

    # Should fail when config file is missing mcpServers key
    no_servers_file = tmp_path / "no_servers.json"
    no_servers_file.write_text('{"test": true}')

    with pytest.raises(ValidationError) as exc_info:
        Settings(
            telegram_bot_token="test_token",
            telegram_bot_username="test_bot",
            approved_directory=str(test_dir),
            enable_mcp=True,
            mcp_config_path=str(no_servers_file),
        )

    assert "mcpServers" in str(exc_info.value)

    # Should fail when mcpServers is empty
    empty_servers_file = tmp_path / "empty_servers.json"
    empty_servers_file.write_text('{"mcpServers": {}}')

    with pytest.raises(ValidationError) as exc_info:
        Settings(
            telegram_bot_token="test_token",
            telegram_bot_username="test_bot",
            approved_directory=str(test_dir),
            enable_mcp=True,
            mcp_config_path=str(empty_servers_file),
        )

    assert "at least one server" in str(exc_info.value)

    # Should succeed with valid MCP config
    config_file = tmp_path / "mcp_config.json"
    config_file.write_text(
        '{"mcpServers": {"my-server": '
        '{"command": "npx", "args": ["-y", "my-mcp-server"]}}}'
    )

    settings = Settings(
        telegram_bot_token="test_token",
        telegram_bot_username="test_bot",
        approved_directory=str(test_dir),
        enable_mcp=True,
        mcp_config_path=str(config_file),
    )

    assert settings.enable_mcp is True
    assert settings.mcp_config_path == config_file


def test_log_level_validation():
    """Test log level validation."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        # Should fail with invalid log level
        with pytest.raises(ValidationError) as exc_info:
            Settings(
                telegram_bot_token="test_token",
                telegram_bot_username="test_bot",
                approved_directory=tmp_dir,
                log_level="INVALID",
            )

        assert "must be one of" in str(exc_info.value)

        # Should succeed with valid log level
        settings = Settings(
            telegram_bot_token="test_token",
            telegram_bot_username="test_bot",
            approved_directory=tmp_dir,
            log_level="debug",  # Should be converted to uppercase
        )

        assert settings.log_level == "DEBUG"


def test_project_threads_validation_requires_chat_id_in_group_mode(tmp_path):
    """Group thread mode requires project_threads_chat_id."""
    project_dir = tmp_path / "projects"
    project_dir.mkdir()
    app_dir = project_dir / "app"
    app_dir.mkdir()
    config_file = tmp_path / "projects.yaml"
    config_file.write_text(
        "projects:\n" "  - slug: app\n" "    name: App\n" "    path: app\n",
        encoding="utf-8",
    )

    with pytest.raises(ValidationError) as exc_info:
        Settings(
            telegram_bot_token="test_token",
            telegram_bot_username="test_bot",
            approved_directory=str(project_dir),
            enable_project_threads=True,
            project_threads_mode="group",
            projects_config_path=str(config_file),
        )

    assert "project_threads_chat_id required" in str(exc_info.value)


def test_project_threads_validation_requires_projects_config(tmp_path):
    """Thread mode requires projects_config_path."""
    project_dir = tmp_path / "projects"
    project_dir.mkdir()

    with pytest.raises(ValidationError) as exc_info:
        Settings(
            telegram_bot_token="test_token",
            telegram_bot_username="test_bot",
            approved_directory=str(project_dir),
            enable_project_threads=True,
            project_threads_chat_id=-1001234567890,
            projects_config_path=None,
        )

    assert "projects_config_path required" in str(exc_info.value)


def test_project_threads_validation_blank_projects_config_path_fails(tmp_path):
    """Blank projects_config_path should be treated as missing."""
    project_dir = tmp_path / "projects"
    project_dir.mkdir()

    with pytest.raises(ValidationError) as exc_info:
        Settings(
            telegram_bot_token="test_token",
            telegram_bot_username="test_bot",
            approved_directory=str(project_dir),
            enable_project_threads=True,
            project_threads_mode="private",
            projects_config_path="",
        )

    assert "projects_config_path required" in str(exc_info.value)


def test_project_threads_validation_private_mode_no_chat_id(tmp_path):
    """Private thread mode does not require project_threads_chat_id."""
    project_dir = tmp_path / "projects"
    project_dir.mkdir()
    app_dir = project_dir / "app"
    app_dir.mkdir()
    config_file = tmp_path / "projects.yaml"
    config_file.write_text(
        "projects:\n" "  - slug: app\n" "    name: App\n" "    path: app\n",
        encoding="utf-8",
    )

    settings = Settings(
        telegram_bot_token="test_token",
        telegram_bot_username="test_bot",
        approved_directory=str(project_dir),
        enable_project_threads=True,
        project_threads_mode="private",
        projects_config_path=str(config_file),
    )

    assert settings.project_threads_mode == "private"
    assert settings.project_threads_chat_id is None


def test_project_threads_validation_private_mode_empty_chat_id(tmp_path):
    """Private mode accepts blank project_threads_chat_id from env/.env."""
    project_dir = tmp_path / "projects"
    project_dir.mkdir()
    app_dir = project_dir / "app"
    app_dir.mkdir()
    config_file = tmp_path / "projects.yaml"
    config_file.write_text(
        "projects:\n" "  - slug: app\n" "    name: App\n" "    path: app\n",
        encoding="utf-8",
    )

    settings = Settings(
        telegram_bot_token="test_token",
        telegram_bot_username="test_bot",
        approved_directory=str(project_dir),
        enable_project_threads=True,
        project_threads_mode="private",
        project_threads_chat_id="",
        projects_config_path=str(config_file),
    )

    assert settings.project_threads_mode == "private"
    assert settings.project_threads_chat_id is None


def test_project_threads_validation_group_mode_empty_chat_id_fails(tmp_path):
    """Group mode rejects blank project_threads_chat_id."""
    project_dir = tmp_path / "projects"
    project_dir.mkdir()
    app_dir = project_dir / "app"
    app_dir.mkdir()
    config_file = tmp_path / "projects.yaml"
    config_file.write_text(
        "projects:\n" "  - slug: app\n" "    name: App\n" "    path: app\n",
        encoding="utf-8",
    )

    with pytest.raises(ValidationError) as exc_info:
        Settings(
            telegram_bot_token="test_token",
            telegram_bot_username="test_bot",
            approved_directory=str(project_dir),
            enable_project_threads=True,
            project_threads_mode="group",
            project_threads_chat_id="",
            projects_config_path=str(config_file),
        )

    assert "project_threads_chat_id required" in str(exc_info.value)


def test_project_threads_sync_action_interval_validation(tmp_path):
    """Thread sync action interval should accept non-negative values only."""
    project_dir = tmp_path / "projects"
    project_dir.mkdir()

    settings = Settings(
        telegram_bot_token="test_token",
        telegram_bot_username="test_bot",
        approved_directory=str(project_dir),
        project_threads_sync_action_interval_seconds=0,
    )
    assert settings.project_threads_sync_action_interval_seconds == 0

    settings = Settings(
        telegram_bot_token="test_token",
        telegram_bot_username="test_bot",
        approved_directory=str(project_dir),
        project_threads_sync_action_interval_seconds="1.1",
    )
    assert settings.project_threads_sync_action_interval_seconds == pytest.approx(1.1)

    with pytest.raises(ValidationError):
        Settings(
            telegram_bot_token="test_token",
            telegram_bot_username="test_bot",
            approved_directory=str(project_dir),
            project_threads_sync_action_interval_seconds=-0.1,
        )


def test_project_threads_validation_invalid_mode(tmp_path):
    """Invalid project thread mode should fail validation."""
    project_dir = tmp_path / "projects"
    project_dir.mkdir()

    with pytest.raises(ValidationError) as exc_info:
        Settings(
            telegram_bot_token="test_token",
            telegram_bot_username="test_bot",
            approved_directory=str(project_dir),
            enable_project_threads=True,
            project_threads_mode="invalid",
        )

    assert "project_threads_mode must be one of" in str(exc_info.value)


def test_voice_provider_validation_and_normalization(tmp_path):
    """VOICE_PROVIDER accepts only mistral/openai and normalizes casing."""
    project_dir = tmp_path / "projects"
    project_dir.mkdir()

    settings = Settings(
        telegram_bot_token="test_token",
        telegram_bot_username="test_bot",
        approved_directory=str(project_dir),
        voice_provider="OPENAI",
    )

    assert settings.voice_provider == "openai"
    assert settings.voice_provider_api_key_env == "OPENAI_API_KEY"
    assert settings.voice_provider_display_name == "OpenAI Whisper"

    with pytest.raises(ValidationError) as exc_info:
        Settings(
            telegram_bot_token="test_token",
            telegram_bot_username="test_bot",
            approved_directory=str(project_dir),
            voice_provider="google",
        )

    assert "voice_provider must be one of" in str(exc_info.value)


def test_voice_vocabulary_hints_default_and_env_parsing(tmp_path):
    """voice_vocabulary_hints defaults to the model-name glossary and accepts CSV.

    ``_env_file=None`` everywhere: the dev box's real .env carries a custom list.
    """
    project_dir = tmp_path / "projects"
    project_dir.mkdir()

    default_settings = Settings(
        _env_file=None,
        telegram_bot_token="test_token",
        telegram_bot_username="test_bot",
        approved_directory=str(project_dir),
    )
    assert default_settings.voice_vocabulary_hints == [
        "Claude",
        "Anthropic",
        "Sonnet",
        "Opus",
        "Haiku",
    ]

    custom_settings = Settings(
        _env_file=None,
        telegram_bot_token="test_token",
        telegram_bot_username="test_bot",
        approved_directory=str(project_dir),
        voice_vocabulary_hints="Kubernetes, Terraform ,,Voxtral",
    )
    assert custom_settings.voice_vocabulary_hints == [
        "Kubernetes",
        "Terraform",
        "Voxtral",
    ]

    empty_settings = Settings(
        _env_file=None,
        telegram_bot_token="test_token",
        telegram_bot_username="test_bot",
        approved_directory=str(project_dir),
        voice_vocabulary_hints="",
    )
    assert empty_settings.voice_vocabulary_hints == []


def test_voice_max_file_size_configuration(tmp_path):
    """Voice max file size should be configurable and validated."""
    project_dir = tmp_path / "projects"
    project_dir.mkdir()

    settings = Settings(
        telegram_bot_token="test_token",
        telegram_bot_username="test_bot",
        approved_directory=str(project_dir),
        voice_max_file_size_mb=32,
    )

    assert settings.voice_max_file_size_mb == 32
    assert settings.voice_max_file_size_bytes == 32 * 1024 * 1024

    with pytest.raises(ValidationError):
        Settings(
            telegram_bot_token="test_token",
            telegram_bot_username="test_bot",
            approved_directory=str(project_dir),
            voice_max_file_size_mb=0,
        )


def test_computed_properties(tmp_path):
    """Test computed properties."""
    test_dir = tmp_path / "projects"
    test_dir.mkdir()

    # Test production mode detection
    dev_settings = Settings(
        telegram_bot_token="test_token",
        telegram_bot_username="test_bot",
        approved_directory=str(test_dir),
        debug=True,
    )
    assert dev_settings.is_production is False

    prod_settings = Settings(
        telegram_bot_token="test_token",
        telegram_bot_username="test_bot",
        approved_directory=str(test_dir),
        debug=False,
        development_mode=False,
    )
    assert prod_settings.is_production is True

    # Test database path extraction
    sqlite_settings = Settings(
        telegram_bot_token="test_token",
        telegram_bot_username="test_bot",
        approved_directory=str(test_dir),
        database_url="sqlite:///data/bot.db",
    )
    assert sqlite_settings.database_path == Path("data/bot.db").resolve()


def test_feature_flags():
    """Test feature flag system."""
    # Create test MCP config file with valid structure before creating settings
    mcp_config = (
        '{"mcpServers": {"test-server": {"command": "echo", "args": ["hello"]}}}'
    )
    Path("/tmp/test_mcp.json").write_text(mcp_config)

    settings = create_test_config(
        enable_mcp=True,
        mcp_config_path="/tmp/test_mcp.json",
        enable_file_uploads=False,
        enable_token_auth=True,
        auth_token_secret="secret",
    )

    features = FeatureFlags(settings)

    assert features.mcp_enabled is True
    assert features.file_uploads_enabled is False
    assert features.token_auth_enabled is True

    enabled_features = features.get_enabled_features()
    assert "mcp" in enabled_features
    assert "file_uploads" not in enabled_features
    assert "token_auth" in enabled_features

    # Test generic feature check
    assert features.is_feature_enabled("mcp") is True
    assert features.is_feature_enabled("nonexistent") is False

    # Cleanup test file
    Path("/tmp/test_mcp.json").unlink(missing_ok=True)


def test_environment_loading():
    """Test environment-specific configuration loading."""
    # Test development environment
    with tempfile.TemporaryDirectory() as tmp_dir:
        os.environ["TELEGRAM_BOT_TOKEN"] = "test_token"
        os.environ["TELEGRAM_BOT_USERNAME"] = "test_bot"
        os.environ["APPROVED_DIRECTORY"] = tmp_dir

        try:
            config = load_config(env="development")
            assert config.debug is True
            assert config.development_mode is True
            assert config.log_level == "DEBUG"

            config = load_config(env="production")
            assert config.debug is False
            assert config.development_mode is False
            assert config.log_level == "INFO"

        finally:
            # Clean up environment
            for key in [
                "TELEGRAM_BOT_TOKEN",
                "TELEGRAM_BOT_USERNAME",
                "APPROVED_DIRECTORY",
            ]:
                os.environ.pop(key, None)


def test_load_config_does_not_log_api_keys(tmp_path):
    """Startup/error logs should not include raw provider API keys."""
    secrets = {
        "ANTHROPIC_API_KEY": "sk-ant-api03-fake-anthropic-token-value",
        "MISTRAL_API_KEY": "mistral-sensitive-token-value-123",
        "OPENAI_API_KEY": "sk-sensitive-openai-token-value-456",
    }

    os.environ["TELEGRAM_BOT_TOKEN"] = "test_token"
    os.environ["TELEGRAM_BOT_USERNAME"] = "test_bot"
    os.environ["APPROVED_DIRECTORY"] = str(tmp_path)
    for key, value in secrets.items():
        os.environ[key] = value

    try:
        with patch.object(config_loader, "logger") as mock_logger:
            load_config(env="development", config_file=tmp_path / "missing.env")
            logged_text = " ".join(str(call) for call in mock_logger.mock_calls)

        for value in secrets.values():
            assert value not in logged_text
    finally:
        for key in [
            "TELEGRAM_BOT_TOKEN",
            "TELEGRAM_BOT_USERNAME",
            "APPROVED_DIRECTORY",
            "ANTHROPIC_API_KEY",
            "MISTRAL_API_KEY",
            "OPENAI_API_KEY",
        ]:
            os.environ.pop(key, None)


def test_create_test_config():
    """Test test configuration creation."""
    config = create_test_config()

    assert config.telegram_token_str == "test_token_123"
    assert config.telegram_bot_username == "test_bot"
    assert str(config.approved_directory).endswith("test_projects")
    assert config.debug is True
    assert config.database_url == "sqlite:///:memory:"

    # Test with overrides
    config = create_test_config(
        log_level="ERROR",
        claude_max_turns=5,
    )

    assert config.log_level == "ERROR"
    assert config.claude_max_turns == 5


class TestSoulMemoryConfig:
    """Tests for soul/memory configuration settings and feature flag."""

    def test_memory_path_default(self, tmp_path):
        """Settings accepts MEMORY_PATH env var, defaults to Path("data/memory")."""
        project_dir = tmp_path / "projects"
        project_dir.mkdir()

        settings = Settings(
            telegram_bot_token="test_token",
            telegram_bot_username="test_bot",
            approved_directory=str(project_dir),
            _env_file=None,
        )

        assert settings.memory_path == Path("data/memory")

    def test_soul_md_path_default(self, tmp_path):
        """Settings accepts SOUL_MD_PATH env var, defaults to Path("data/soul.md")."""
        project_dir = tmp_path / "projects"
        project_dir.mkdir()

        settings = Settings(
            telegram_bot_token="test_token",
            telegram_bot_username="test_bot",
            approved_directory=str(project_dir),
            _env_file=None,
        )

        assert settings.soul_md_path == Path("data/soul.md")

    def test_activity_log_max_lines_default(self, tmp_path):
        """Settings accepts ACTIVITY_LOG_MAX_LINES, defaults to 200."""
        project_dir = tmp_path / "projects"
        project_dir.mkdir()

        settings = Settings(
            telegram_bot_token="test_token",
            telegram_bot_username="test_bot",
            approved_directory=str(project_dir),
            _env_file=None,
        )

        assert settings.activity_log_max_lines == 200

    def test_activity_log_max_lines_rejects_below_50(self, tmp_path):
        """ACTIVITY_LOG_MAX_LINES rejects values below 50."""
        project_dir = tmp_path / "projects"
        project_dir.mkdir()

        with pytest.raises(ValidationError):
            Settings(
                telegram_bot_token="test_token",
                telegram_bot_username="test_bot",
                approved_directory=str(project_dir),
                activity_log_max_lines=49,
                _env_file=None,
            )

    def test_enable_soul_memory_default_false(self, tmp_path):
        """Settings accepts ENABLE_SOUL_MEMORY, defaults to False."""
        project_dir = tmp_path / "projects"
        project_dir.mkdir()

        settings = Settings(
            telegram_bot_token="test_token",
            telegram_bot_username="test_bot",
            approved_directory=str(project_dir),
            _env_file=None,
        )

        assert settings.enable_soul_memory is False

    def test_relative_memory_path_resolves_against_approved_directory(self, tmp_path):
        """When enabled, relative memory_path resolves against approved_directory."""
        project_dir = tmp_path / "projects"
        project_dir.mkdir()

        settings = Settings(
            telegram_bot_token="test_token",
            telegram_bot_username="test_bot",
            approved_directory=str(project_dir),
            enable_soul_memory=True,
            memory_path="data/memory",
            _env_file=None,
        )

        assert settings.memory_path == project_dir / "data" / "memory"

    def test_relative_soul_md_path_resolves_against_approved_directory(self, tmp_path):
        """When enabled, relative soul_md_path resolves against approved_directory."""
        project_dir = tmp_path / "projects"
        project_dir.mkdir()

        settings = Settings(
            telegram_bot_token="test_token",
            telegram_bot_username="test_bot",
            approved_directory=str(project_dir),
            enable_soul_memory=True,
            soul_md_path="data/soul.md",
            _env_file=None,
        )

        assert settings.soul_md_path == project_dir / "data" / "soul.md"

    def test_paths_outside_approved_directory_rejected(self, tmp_path):
        """When enabled, paths outside approved_directory raise ValidationError."""
        project_dir = tmp_path / "projects"
        project_dir.mkdir()

        with pytest.raises(ValidationError) as exc_info:
            Settings(
                telegram_bot_token="test_token",
                telegram_bot_username="test_bot",
                approved_directory=str(project_dir),
                enable_soul_memory=True,
                memory_path="/etc/passwd",
                _env_file=None,
            )

        assert (
            "outside approved directory" in str(exc_info.value).lower()
            or "not relative" in str(exc_info.value).lower()
            or "boundary" in str(exc_info.value).lower()
        )

    def test_paths_not_validated_when_disabled(self, tmp_path):
        """When disabled, paths are NOT validated (no error for defaults)."""
        project_dir = tmp_path / "projects"
        project_dir.mkdir()

        # Should NOT raise even though defaults are relative and directory doesn't exist
        settings = Settings(
            telegram_bot_token="test_token",
            telegram_bot_username="test_bot",
            approved_directory=str(project_dir),
            enable_soul_memory=False,
            _env_file=None,
        )

        # Paths should remain as-is (not resolved)
        assert settings.memory_path == Path("data/memory")
        assert settings.soul_md_path == Path("data/soul.md")

    def test_feature_flags_soul_memory_enabled(self, tmp_path):
        """FeatureFlags.soul_memory_enabled returns settings.enable_soul_memory."""
        project_dir = tmp_path / "projects"
        project_dir.mkdir()

        settings = Settings(
            telegram_bot_token="test_token",
            telegram_bot_username="test_bot",
            approved_directory=str(project_dir),
            enable_soul_memory=True,
            _env_file=None,
        )
        features = FeatureFlags(settings)
        assert features.soul_memory_enabled is True

        settings_off = Settings(
            telegram_bot_token="test_token",
            telegram_bot_username="test_bot",
            approved_directory=str(project_dir),
            enable_soul_memory=False,
            _env_file=None,
        )
        features_off = FeatureFlags(settings_off)
        assert features_off.soul_memory_enabled is False

    def test_feature_flags_is_feature_enabled_soul_memory(self, tmp_path):
        """FeatureFlags.is_feature_enabled("soul_memory") works."""
        project_dir = tmp_path / "projects"
        project_dir.mkdir()

        settings = Settings(
            telegram_bot_token="test_token",
            telegram_bot_username="test_bot",
            approved_directory=str(project_dir),
            enable_soul_memory=True,
            _env_file=None,
        )
        features = FeatureFlags(settings)
        assert features.is_feature_enabled("soul_memory") is True

    def test_feature_flags_get_enabled_features_includes_soul_memory(self, tmp_path):
        """get_enabled_features() includes soul_memory when enabled."""
        project_dir = tmp_path / "projects"
        project_dir.mkdir()

        settings = Settings(
            telegram_bot_token="test_token",
            telegram_bot_username="test_bot",
            approved_directory=str(project_dir),
            enable_soul_memory=True,
            _env_file=None,
        )
        features = FeatureFlags(settings)
        enabled = features.get_enabled_features()
        assert "soul_memory" in enabled

        settings_off = Settings(
            telegram_bot_token="test_token",
            telegram_bot_username="test_bot",
            approved_directory=str(project_dir),
            enable_soul_memory=False,
            _env_file=None,
        )
        features_off = FeatureFlags(settings_off)
        enabled_off = features_off.get_enabled_features()
        assert "soul_memory" not in enabled_off


def test_configuration_error_handling():
    """Test configuration error handling."""
    # Test with invalid directory permissions (simulate by using a file)
    with tempfile.NamedTemporaryFile() as tmp_file:
        os.environ["TELEGRAM_BOT_TOKEN"] = "test_token"
        os.environ["TELEGRAM_BOT_USERNAME"] = "test_bot"
        os.environ["APPROVED_DIRECTORY"] = tmp_file.name  # File instead of directory

        try:
            with pytest.raises(ConfigurationError):
                load_config()
        finally:
            for key in [
                "TELEGRAM_BOT_TOKEN",
                "TELEGRAM_BOT_USERNAME",
                "APPROVED_DIRECTORY",
            ]:
                os.environ.pop(key, None)


def test_voice_vocabulary_hints_csv_from_env(tmp_path, monkeypatch):
    """The env var is a plain CSV string and must load without a JSON decode error."""
    monkeypatch.setenv("VOICE_VOCABULARY_HINTS", "Claude, Anthropic,Devvybot")
    settings = Settings(
        _env_file=None,
        telegram_bot_token="test_token",
        telegram_bot_username="test_bot",
        approved_directory=tmp_path,
    )
    assert settings.voice_vocabulary_hints == ["Claude", "Anthropic", "Devvybot"]


def test_voice_provider_elevenlabs_defaults(tmp_path):
    """ElevenLabs provider resolves its model, key env var and display name."""
    settings = Settings(
        _env_file=None,
        telegram_bot_token="test_token",
        telegram_bot_username="test_bot",
        approved_directory=tmp_path,
        voice_provider="elevenlabs",
        elevenlabs_api_key="xi-secret",
    )
    assert settings.voice_provider == "elevenlabs"
    assert settings.resolved_voice_model == "scribe_v2"
    assert settings.voice_provider_api_key_env == "ELEVENLABS_API_KEY"
    assert settings.voice_provider_display_name == "ElevenLabs Scribe"
    assert settings.elevenlabs_api_key_str == "xi-secret"
    assert settings.voice_language is None


def test_voice_messages_enabled_gate_for_elevenlabs(tmp_path):
    """ElevenLabs voice is on only when its API key is present."""
    base = dict(
        _env_file=None,
        telegram_bot_token="test_token",
        telegram_bot_username="test_bot",
        approved_directory=tmp_path,
        enable_voice_messages=True,
        voice_provider="elevenlabs",
    )
    assert FeatureFlags(Settings(**base)).voice_messages_enabled is False
    assert (
        FeatureFlags(Settings(**base, elevenlabs_api_key="xi")).voice_messages_enabled
        is True
    )
