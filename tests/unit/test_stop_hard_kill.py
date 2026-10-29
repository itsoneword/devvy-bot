"""dv-b399 round 2: /stop ends the CLI's whole process tree when the polite
interrupt fails or is ignored; bgjobs (not descendants) are untouched."""

import asyncio
import os
import signal
import subprocess
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram.error import BadRequest, NetworkError

from src.bot.core import is_stale_callback_error
from src.claude import process_tree, sdk_integration
from src.claude.process_tree import cli_pid, descendants, terminate_tree
from src.claude.sdk_integration import ClaudeSDKManager, _ActiveRun
from src.config.settings import Settings


def _manager(tmp_path: Path) -> ClaudeSDKManager:
    return ClaudeSDKManager(
        Settings(
            telegram_bot_token="t:t",
            telegram_bot_username="b",
            approved_directory=tmp_path,
            claude_timeout_seconds=30,
            enable_mcp=False,
        )
    )


def _wait_for(cond, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


# --- process tree -----------------------------------------------------------


async def test_terminate_tree_ends_cli_shell_and_grandchild():
    """A CLI-like parent -> shell -> sleep chain dies, even if it ignores TERM."""
    script = (
        "trap '' TERM; sleep 120 & " "sh -c 'trap \"\" TERM; sleep 120 & wait' & wait"
    )
    proc = subprocess.Popen(["bash", "-c", script])
    try:
        assert _wait_for(lambda: len(descendants(proc.pid)) >= 3)
        tree = descendants(proc.pid) + [proc.pid]
        n = await terminate_tree(proc.pid, grace=0.3)
        assert n >= len(tree)
        proc.wait(timeout=5)
        assert _wait_for(lambda: not any(process_tree._alive(p) for p in tree))
    finally:
        if proc.poll() is None:
            proc.kill()


async def test_terminate_tree_spares_detached_processes():
    """A job moved out of the CLI's tree (systemd unit / double fork) is not
    a descendant and survives the stop."""
    marker = "sleep 119"
    launcher = subprocess.Popen(
        ["bash", "-c", f"(setsid {marker} >/dev/null 2>&1 &) ; sleep 118; true"]
    )
    detached: list = []
    try:
        assert _wait_for(lambda: len(descendants(launcher.pid)) >= 1)
        out = subprocess.run(
            ["pgrep", "-f", f"^{marker}$"], capture_output=True, text=True
        ).stdout.split()
        inside = set(descendants(launcher.pid))
        detached = [int(p) for p in out if int(p) not in inside]
        assert detached, "test setup: detached process not found"
        await terminate_tree(launcher.pid, grace=0.3)
        launcher.wait(timeout=5)
        assert any(process_tree._alive(p) for p in detached)
    finally:
        for p in detached:
            try:
                os.kill(p, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if launcher.poll() is None:
            launcher.kill()


async def test_terminate_tree_refuses_a_pid_that_is_not_our_child():
    assert await terminate_tree(os.getpid()) == 0
    assert await terminate_tree(1) == 0


def test_cli_pid_reads_sdk_transport_and_ignores_mocks():
    client = MagicMock()
    client._transport._process.pid = 4242
    assert cli_pid(client) == 4242
    assert cli_pid(MagicMock()) is None


# --- stop_turns escalation ---------------------------------------------------


def _run(m: ClaudeSDKManager, client) -> _ActiveRun:
    run = _ActiveRun(client=client, user_id=1, profile="gymbro")
    m._active_runs.setdefault((1, "gymbro"), []).append(run)
    return run


async def test_failed_interrupt_hard_stops_the_tree(tmp_path, monkeypatch):
    m = _manager(tmp_path)
    client = MagicMock()
    client.interrupt = AsyncMock(side_effect=RuntimeError("Control request timeout"))
    client._transport._process.pid = 777
    run = _run(m, client)
    terminated = AsyncMock(return_value=3)
    monkeypatch.setattr(sdk_integration, "terminate_tree", terminated)

    assert await m.stop_turns(1, "gymbro") == 1  # the turn IS ended
    terminated.assert_awaited_once_with(777)
    assert run.hard_stopped


@pytest.mark.timing
async def test_hanging_interrupt_is_cut_at_the_grace(tmp_path, monkeypatch):
    """The 2026-10-20 case: interrupt() never answers; stop must not wait 60 s."""
    m = _manager(tmp_path)
    monkeypatch.setattr(sdk_integration, "_INTERRUPT_GRACE_SECONDS", 0.2)

    async def hang() -> None:
        await asyncio.sleep(60)

    client = MagicMock()
    client.interrupt = hang
    client._transport._process.pid = 778
    run = _run(m, client)
    terminated = AsyncMock(return_value=2)
    monkeypatch.setattr(sdk_integration, "terminate_tree", terminated)

    t0 = time.monotonic()
    await m.stop_turns(1, "gymbro")
    assert time.monotonic() - t0 < 2
    terminated.assert_awaited_once_with(778)
    assert run.hard_stopped


async def test_acknowledged_interrupt_that_does_nothing_is_escalated(
    tmp_path, monkeypatch
):
    m = _manager(tmp_path)
    monkeypatch.setattr(sdk_integration, "_INTERRUPT_GRACE_SECONDS", 0.2)
    client = MagicMock()
    client.interrupt = AsyncMock()  # the CLI says ok, then keeps running
    client._transport._process.pid = 779
    run = _run(m, client)
    terminated = AsyncMock(return_value=2)
    monkeypatch.setattr(sdk_integration, "terminate_tree", terminated)

    await m.stop_turns(1, "gymbro")
    assert not run.hard_stopped  # the polite stop gets its grace first
    await asyncio.sleep(0.5)
    terminated.assert_awaited_once_with(779)
    assert run.hard_stopped


async def test_interrupt_that_works_is_not_hard_stopped(tmp_path, monkeypatch):
    m = _manager(tmp_path)
    monkeypatch.setattr(sdk_integration, "_INTERRUPT_GRACE_SECONDS", 0.2)
    client = MagicMock()
    client.interrupt = AsyncMock()
    client._transport._process.pid = 780
    run = _run(m, client)
    terminated = AsyncMock()
    monkeypatch.setattr(sdk_integration, "terminate_tree", terminated)

    await m.stop_turns(1, "gymbro")
    m._active_runs.pop((1, "gymbro"))  # the turn ended on its own
    await asyncio.sleep(0.5)
    terminated.assert_not_awaited()
    assert not run.hard_stopped


# --- stale callback is not a security event ---------------------------------


def test_stale_callback_errors_are_recognised():
    assert is_stale_callback_error(
        BadRequest(
            "Query is too old and response timeout expired or query id is invalid"
        )
    )
    assert not is_stale_callback_error(BadRequest("Message to edit not found"))
    assert not is_stale_callback_error(NetworkError("Query is too old"))
    assert not is_stale_callback_error(RuntimeError("Query is too old"))


async def test_global_handler_ignores_stale_callback():
    from src.bot.core import ClaudeCodeBot

    bot = ClaudeCodeBot.__new__(ClaudeCodeBot)
    audit = MagicMock(log_security_violation=AsyncMock())
    ctx = MagicMock()
    ctx.error = BadRequest("Query is too old and response timeout expired")
    ctx.bot_data = {"audit_logger": audit}
    update = MagicMock()
    update.effective_message.reply_text = AsyncMock()
    await bot._error_handler(update, ctx)
    audit.log_security_violation.assert_not_called()
    update.effective_message.reply_text.assert_not_called()

    ctx.error = RuntimeError("real bug")  # anything else is still audited
    await bot._error_handler(update, ctx)
    audit.log_security_violation.assert_awaited_once()
