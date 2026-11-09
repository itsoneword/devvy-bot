"""dv-fe4d phase 1: ``channel`` -- Telegram token only required for telegram."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.config import load_config
from src.config.settings import Settings


@pytest.fixture(autouse=True)
def _no_telegram_env(monkeypatch):
    for name in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_BOT_USERNAME", "CHANNEL"):
        monkeypatch.delenv(name, raising=False)


def test_local_channel_loads_without_token_or_username() -> None:
    s = Settings(_env_file=None, channel="local")
    assert s.channel == "local"
    assert s.telegram_bot_token is None
    assert s.telegram_bot_username is None
    assert s.notification_chat_ids == [1]


def test_default_channel_is_telegram_and_still_requires_token() -> None:
    with pytest.raises(ValidationError) as exc:
        Settings(_env_file=None)
    text = str(exc.value).lower()
    assert "telegram_bot_token" in text and "telegram_bot_username" in text
    ok = Settings(_env_file=None, telegram_bot_token="t:k", telegram_bot_username="b")
    assert ok.channel == "telegram"


def test_local_channel_keeps_explicit_notification_chat_ids() -> None:
    s = Settings(_env_file=None, channel="local", notification_chat_ids=[42])
    assert s.notification_chat_ids == [42]


def test_load_config_for_agentjob_with_channel_local_and_no_token(
    tmp_path, monkeypatch
) -> None:
    """agentjob's loader path (load_config on a config file) works with no
    token: one loader, no placeholder."""
    cfg = tmp_path / "config.env"
    cfg.write_text("CHANNEL=local\n")
    monkeypatch.chdir(tmp_path)
    config = load_config(config_file=cfg)
    assert config.channel == "local"
    assert config.telegram_bot_token is None
