"""FeatureRegistry builds exactly the shared features, in both security modes.

The classic-only features (git, quick_actions, session_export, conversation)
were deleted with classic mode; this pins that they cannot come back by
accident, and that denylist mode (APPROVED_DIRECTORY unset — the native
service) builds the same set as allowlist mode.
"""

from unittest.mock import MagicMock

from src.bot.features.registry import FeatureRegistry
from src.config.settings import Settings

_SHARED = {"file_handler", "image_handler", "voice_handler"}
_GONE = {"git", "quick_actions", "session_export", "conversation"}


def _settings(**overrides) -> Settings:
    values = dict(
        telegram_bot_token="test:token",
        telegram_bot_username="testbot",
        approved_directory=None,
        voice_provider="whisper",
    )
    values.update(overrides)
    return Settings(**values)


def _registry(settings: Settings) -> FeatureRegistry:
    return FeatureRegistry(config=settings, storage=MagicMock(), security=MagicMock())


def test_denylist_mode_builds_only_the_shared_features(tmp_path):
    registry = _registry(_settings())
    assert set(registry.features) == _SHARED


def test_allowlist_mode_adds_nothing_classic(tmp_path):
    registry = _registry(_settings(approved_directory=tmp_path))
    assert set(registry.features) == _SHARED
    assert not set(registry.features) & _GONE


def test_classic_feature_accessors_are_gone():
    for name in ("get_git_integration", "get_quick_actions", "get_session_export"):
        assert not hasattr(FeatureRegistry, name)
