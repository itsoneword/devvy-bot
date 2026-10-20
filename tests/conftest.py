"""Pytest configuration and fixtures."""

import os

import pytest

# The `core` subset (dv-5206): fast tests of the mechanics a feature can break
# silently — window roll, evaluator, items, policy parity, export drift. `make
# test-core` runs it before every restart. One list here instead of a
# `pytestmark` in each file. Entries are node-id prefixes relative to the repo
# root; keep the whole subset under ~60 s (measure with `make test-core`).
# Env-sensitive tests (tests/unit/test_config.py) stay out on purpose.
CORE_TESTS = (
    # window roll
    "tests/unit/test_core_window_cycle.py::",
    "tests/unit/test_orchestrator_window_accounting.py::",
    "tests/unit/test_window_repository.py::",
    "tests/unit/test_window_transitions.py::",
    "tests/unit/test_window_seed.py::",
    "tests/unit/test_over_threshold_guard.py::",
    # evaluator
    "tests/unit/test_evaluator/",
    "tests/unit/test_evaluator_backend.py::",
    # items
    "tests/unit/test_items/",
    # one rule set / one call path
    "tests/unit/test_security/test_policy_parity.py::",
    "tests/unit/test_no_stray_model_calls.py::",
    # export drift: caught at restart, not at release
    "tests/unit/test_scripts/test_export_public.py::"
    "test_every_tracked_file_is_classified",
    "tests/unit/test_scripts/test_export_public.py::"
    "test_no_shipped_file_carries_personal_data",
)


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    for item in items:
        if item.nodeid.startswith(CORE_TESTS):
            item.add_marker(pytest.mark.core)


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
    _scrub_live_config()


def _scrub_live_config() -> None:
    """Keep the live bot's configuration out of the suite (dv-5206).

    Run from the bot's own shell (devops profile, bgjob), the process env
    carries live values such as WINDOW_REFRESH_THRESHOLD_TOKENS or
    PROFILES_CONFIG_PATH, and the checkout's dotenv file sits next to the
    tests; tests that assert defaults then fail only on the host. Done at
    configure time, not per test, because some modules read os.environ at
    import (llm_analytics/store.py). Drops every env var ``Settings`` reads
    and stops it loading the dotenv file; tests that need a value pass it
    explicitly or ``monkeypatch.setenv`` it.
    """
    from src.config.settings import Settings

    for name in Settings.model_fields:
        os.environ.pop(name.upper(), None)
    Settings.model_config["env_file"] = None


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
