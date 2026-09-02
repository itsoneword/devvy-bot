from pathlib import Path

from src.config.settings import Settings


def test_profiles_config_path_defaults_none(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "x")
    monkeypatch.setenv("TELEGRAM_BOT_USERNAME", "y")
    # _env_file=None so the repo's real .env (which sets PROFILES_CONFIG_PATH)
    # doesn't leak in; we're asserting the field default.
    s = Settings(_env_file=None)
    assert s.profiles_config_path is None


def test_profiles_config_path_from_env(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "x")
    monkeypatch.setenv("TELEGRAM_BOT_USERNAME", "y")
    monkeypatch.setenv("PROFILES_CONFIG_PATH", "/tmp/profiles.yaml")
    s = Settings(_env_file=None)
    assert s.profiles_config_path == Path("/tmp/profiles.yaml")
