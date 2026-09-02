"""Tests for optional APPROVED_DIRECTORY in settings."""

from pathlib import Path

from src.config.settings import Settings


class TestOptionalApprovedDirectory:
    """APPROVED_DIRECTORY should be optional when running in native/denylist mode."""

    def test_stale_agentic_mode_key_is_ignored(self) -> None:
        """A leftover AGENTIC_MODE in .env must not break startup (D6).

        The field was deleted with classic mode; ``extra="ignore"`` swallows
        it and the bot runs conversationally, which is the correct outcome.
        """
        settings = Settings(
            telegram_bot_token="test:token",
            telegram_bot_username="testbot",
            approved_directory=None,
            agentic_mode=False,
        )
        assert not hasattr(settings, "agentic_mode")

    def test_settings_created_without_approved_directory(self) -> None:
        """Settings should accept missing APPROVED_DIRECTORY."""
        settings = Settings(
            telegram_bot_token="test:token",
            telegram_bot_username="testbot",
            approved_directory=None,
        )
        assert settings.approved_directory is None

    def test_settings_with_approved_directory_still_works(self, tmp_path: Path) -> None:
        """Backward compatibility: APPROVED_DIRECTORY still works when set."""
        settings = Settings(
            telegram_bot_token="test:token",
            telegram_bot_username="testbot",
            approved_directory=tmp_path,
        )
        assert settings.approved_directory == tmp_path

    def test_denylist_mode_flag(self) -> None:
        """When no APPROVED_DIRECTORY, we're in denylist mode."""
        settings = Settings(
            telegram_bot_token="test:token",
            telegram_bot_username="testbot",
            approved_directory=None,
        )
        assert settings.is_denylist_mode is True

    def test_allowlist_mode_flag(self, tmp_path: Path) -> None:
        """When APPROVED_DIRECTORY set, we're in allowlist mode."""
        settings = Settings(
            telegram_bot_token="test:token",
            telegram_bot_username="testbot",
            approved_directory=tmp_path,
        )
        assert settings.is_denylist_mode is False
