"""ask_explain rendering and the manager's timeout rewrite (dv-af70)."""

from __future__ import annotations

import asyncio
from typing import List, Optional, Tuple

from src.claude.confirmation import ConfirmationManager
from src.security.ask_explain import render


def test_render_caps_and_flattens_fields() -> None:
    block = render(
        {
            "what": "строка\nвторая " + "я" * 500,
            "changes": "ничего",
            "sends": "никуда",
            "verdict": "safe",
            "injection_suspected": False,
        }
    )
    what = block.splitlines()[1]
    assert what.startswith("Что делает: строка вторая")
    assert len(what) < 230 and what.endswith("…")
    assert block.endswith("Вердикт: ✅ безопасно")


async def test_timeout_rewrite_keeps_the_explanation() -> None:
    sent: List[str] = []
    edits: List[str] = []
    updates: List[Tuple[int, int, str, str]] = []

    async def send(chat_id: int, text: str, token: str) -> int:
        sent.append(text)
        return 1

    async def edit(chat_id: int, message_id: int, text: str) -> None:
        edits.append(text)

    async def update(chat_id: int, message_id: int, text: str, token: str) -> None:
        updates.append((chat_id, message_id, text, token))

    async def explain(reason: str, preview: str) -> Optional[str]:
        return "🔎 пояснение"

    manager = ConfirmationManager(
        send_prompt=send,
        chat_ids=[1],
        edit_prompt=edit,
        explain=explain,
        update_prompt=update,
    )
    approved = await asyncio.wait_for(
        manager.request(
            "process termination (kill/pkill)", "kill 1", timeout_seconds=0.2
        ),
        timeout=3,
    )
    assert approved is False
    assert len(updates) == 1 and "🔎 пояснение" in updates[0][2]
    assert len(edits) == 1 and "🔎 пояснение" in edits[0] and "timed out" in edits[0]


async def test_no_update_callback_means_no_explainer_call() -> None:
    calls: List[str] = []

    async def send(chat_id: int, text: str, token: str) -> int:
        return 1

    async def explain(reason: str, preview: str) -> Optional[str]:
        calls.append(reason)
        return "x"

    manager = ConfirmationManager(send_prompt=send, chat_ids=[1], explain=explain)
    assert await manager.request("r", "p", timeout_seconds=0.05) is False
    assert calls == []
