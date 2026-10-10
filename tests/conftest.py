"""Pytest configuration and fixtures."""

import os

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--update-golden",
        action="store_true",
        default=False,
        help="dv-5e4f: rewrite storage golden-master fixtures instead of "
        "failing on mismatch (equivalent to DEVVY_UPDATE_GOLDEN=1).",
    )


def pytest_configure(config: pytest.Config) -> None:
    if config.getoption("--update-golden"):
        os.environ["DEVVY_UPDATE_GOLDEN"] = "1"


@pytest.fixture(autouse=True)
def _legacy_data_root(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the legacy data layout (dv-c66a).

    Without this, a DEVVY_DATA_ROOT in the developer's shell or the checkout's
    .env would silently re-root every DB/profile path the suite resolves.
    Empty = unset; tests that exercise the root override it themselves.
    """
    monkeypatch.setenv("DEVVY_DATA_ROOT", "")


@pytest.fixture(autouse=True)
def _codex_rules_to_tmp(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """Codex turns write a money-guard rules file into $CODEX_HOME/rules
    (dv-4fc2). Keep the suite out of the real ~/.codex."""
    from src.agent_backends import codex_cli, codex_execpolicy

    home = tmp_path_factory.mktemp("codex_home")
    monkeypatch.setattr(
        codex_cli,
        "ensure_money_guard_rules",
        lambda _home, tools_root=None: codex_execpolicy.ensure_rules(home),
    )


@pytest.fixture
def sample_user_id():
    """Sample Telegram user ID for testing."""
    return 123456789


@pytest.fixture
def sample_config():
    """Sample configuration for testing."""
    return {
        "telegram_bot_token": "test_token",
        "telegram_bot_username": "test_bot",
        "approved_directory": "/tmp/test_projects",
        "allowed_users": [123456789],
    }
