"""ConfirmationManager: per-call timeout and source line (dv-ef90)."""

import asyncio
from unittest.mock import AsyncMock

from src.claude.confirmation import ConfirmationManager


async def test_per_call_timeout_and_source_appear_in_the_prompt() -> None:
    send = AsyncMock(return_value=11)
    manager = ConfirmationManager(send, chat_ids=[7], timeout_seconds=0.01)

    task = asyncio.ensure_future(
        manager.request("rm outside tmp", "rm x", timeout_seconds=5, source="pump-1")
    )
    await asyncio.sleep(0.05)  # longer than the default; the call's own applies
    assert not task.done()
    chat_id, text, token = send.await_args.args
    assert chat_id == 7 and "pump-1" in text and "in 5s" in text
    manager.resolve(token, True)
    assert await task is True


async def test_default_timeout_still_denies() -> None:
    send = AsyncMock(return_value=11)
    manager = ConfirmationManager(send, chat_ids=[7], timeout_seconds=0.01)
    assert await manager.request("r", "p") is False
    assert "in 0s" in send.await_args.args[1]
