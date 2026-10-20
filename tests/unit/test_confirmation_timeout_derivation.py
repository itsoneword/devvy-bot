"""One CONFIRMATION_TIMEOUT_SECONDS; every dependent timeout outlives it (dv-64dc)."""

import asyncio

from src.agent_bridge import codex_policy_hook
from src.claude.confirmation import ConfirmationManager
from src.config.settings import Settings
from src.utils.constants import CONFIRMATION_MARGIN_SECONDS


def _settings(tmp_path, **kw) -> Settings:  # type: ignore[no-untyped-def]
    return Settings(
        telegram_bot_token="t",
        telegram_bot_username="b",
        approved_directory=tmp_path,
        **kw,
    )


def test_default_is_600(tmp_path) -> None:  # type: ignore[no-untyped-def]
    assert _settings(tmp_path).confirmation_timeout_seconds == 600


def test_dependents_outlive_the_wait(tmp_path) -> None:  # type: ignore[no-untyped-def]
    s = _settings(
        tmp_path,
        confirmation_timeout_seconds=900,
        claude_timeout_seconds=300,
        agent_timeout_seconds=300,
    )
    assert s.confirmation_hook_timeout_seconds == 900 + CONFIRMATION_MARGIN_SECONDS
    assert s.claude_idle_limit_seconds > s.confirmation_hook_timeout_seconds
    assert s.agent_idle_limit_seconds > s.confirmation_hook_timeout_seconds


def test_larger_idle_settings_win(tmp_path) -> None:  # type: ignore[no-untyped-def]
    s = _settings(tmp_path, claude_timeout_seconds=5000, agent_timeout_seconds=5000)
    assert s.claude_idle_limit_seconds == 5000
    assert s.agent_idle_limit_seconds == 5000


def test_codex_hook_timeout_is_below_codex_kill(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("DEVVY_POLICY_TIMEOUT_SECONDS", "650")
    assert codex_policy_hook.request_timeout() == 650.0
    monkeypatch.delenv("DEVVY_POLICY_TIMEOUT_SECONDS")
    assert (
        codex_policy_hook.request_timeout() == codex_policy_hook.REQUEST_TIMEOUT_SECONDS
    )


def test_codex_args_use_derived_timeout(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from types import SimpleNamespace

    from src.agent_backends.codex_cli import CodexCliIntegration as CodexCliBackend

    b = object.__new__(CodexCliBackend)
    b.config = _settings(tmp_path, confirmation_timeout_seconds=600)
    b.agent_bridge = SimpleNamespace(policy_url="http://x")
    assert "timeout=660" in "".join(b._policy_hook_args())


async def test_late_tap_approves_after_a_long_wait() -> None:
    tokens: list = []

    async def send(chat_id, text, token):  # type: ignore[no-untyped-def]
        tokens.append(token)
        return 1

    mgr = ConfirmationManager(send_prompt=send, chat_ids=[1], timeout_seconds=2.0)
    task = asyncio.create_task(mgr.request("close position", "BTCUSDT"))
    await asyncio.sleep(1.0)
    assert not task.done()
    assert mgr.resolve(tokens[0], True)
    assert await asyncio.wait_for(task, 1) is True


async def test_no_tap_denies_at_the_configured_timeout() -> None:
    async def send(chat_id, text, token):  # type: ignore[no-untyped-def]
        return 1

    mgr = ConfirmationManager(send_prompt=send, chat_ids=[1], timeout_seconds=0.2)
    assert await mgr.request("x", "y") is False
