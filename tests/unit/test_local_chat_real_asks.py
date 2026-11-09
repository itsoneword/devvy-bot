"""A local owner's deny travels through the real hooks for both backends."""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any, AsyncIterator
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from src.agent_bridge.policy_gate import PolicyGate
from src.agent_bridge.server import build_app
from src.agent_bridge.tokens import TokenRegistry
from src.api.server import create_api_app
from src.chat.local_turns import LocalChatTurns
from src.claude.confirmation import CALLBACK_PREFIX, ConfirmationManager
from src.claude.sdk_integration import _make_pre_tool_use_hook
from src.claude.tool_context import AgentServices, TurnContext
from src.events.bus import EventBus
from src.notifications.transport import LOCAL_CHAT_ID, LocalTransport
from src.security.policy import TurnPolicy
from src.storage.database import DatabaseManager
from src.storage.repositories import MessageRepository
from src.tui.client import PumpClient
from tests.unit.window_harness import (
    USER_ID,
    _make_repo,
    _mock_response,
    _real_message_saver,
    _seed_conversation,
)


class _Api:
    def __init__(self, client: TestClient) -> None:
        self.client = client

    def get(self, path: str, **params: object) -> dict:
        response = self.client.get(
            path, params=params, headers={"Authorization": "Bearer tui"}
        )
        assert response.status_code == 200
        return response.json()

    def post(self, path: str, json: dict) -> dict:
        response = self.client.post(
            path, json=json, headers={"Authorization": "Bearer tui"}
        )
        assert response.status_code == 200
        return response.json()


@pytest.mark.parametrize("backend", ["claude", "openai_codex"])
async def test_local_tui_denies_real_backend_tool_ask(
    tmp_path: Path, backend: str
) -> None:
    db = DatabaseManager(str(tmp_path / "chat.db"))
    await db.initialize()
    transport = LocalTransport(db)

    async def send(chat_id: int, text: str, token: str) -> int:
        return await transport.send(
            chat_id,
            text,
            buttons=[
                ("Approve", f"{CALLBACK_PREFIX}:{token}:y"),
                ("Deny", f"{CALLBACK_PREFIX}:{token}:n"),
            ],
        )

    manager = ConfirmationManager(send, chat_ids=[LOCAL_CHAT_ID], timeout_seconds=5)
    settings = MagicMock()
    settings.channel = "local"
    settings.local_tui_secret = "tui"
    settings.webhook_api_secret = "job"
    settings.development_mode = True
    local = TestClient(
        create_api_app(
            EventBus(), settings, db_manager=db, confirmation_manager=manager
        )
    )
    tui = PumpClient(api=_Api(local))
    tool = {"tool_name": "Bash", "tool_input": {"command": "git push origin main"}}
    cwd = tmp_path

    if backend == "claude":
        hook = _make_pre_tool_use_hook(
            cwd, full_access=True, confirmation_manager=manager
        )
        task = asyncio.create_task(hook(tool, "tool-1", None))
    else:
        tokens = TokenRegistry()
        bridge = build_app(AgentServices(event_bus=None, task_repo=None), tokens)
        bridge.state.policy_gate = PolicyGate(
            lambda ctx: TurnPolicy(working_directory=cwd, full_access=True),
            manager,
        )
        token = tokens.mint(TurnContext(profile_slug="devops", working_dir=cwd))

        async def codex_call() -> dict:
            def request() -> dict:
                with TestClient(bridge) as client:
                    response = client.post(
                        "/policy/check",
                        json=tool,
                        headers={"Authorization": f"Bearer {token}"},
                    )
                    assert response.status_code == 200
                    return response.json()

            return await asyncio.to_thread(request)

        task = asyncio.create_task(codex_call())

    try:
        for _ in range(100):
            asks = await asyncio.to_thread(tui.asks)
            if asks:
                break
            await asyncio.sleep(0.03)
        assert asks, "backend did not ask the local owner"
        assert await asyncio.to_thread(tui.deny, asks[0]) == {"matched": True}
        outcome = await asyncio.wait_for(task, 5)
        if backend == "claude":
            assert outcome["hookSpecificOutput"]["permissionDecision"] == "deny"
        else:
            assert outcome["decision"] == "deny"
        assert not tui.asks()  # the edited card no longer offers an action
    finally:
        await db.close()


@pytest.mark.parametrize("backend", ["claude", "openai_codex"])
async def test_local_chat_turn_waits_for_tui_deny_before_tool_runs(
    tmp_path: Path, backend: str
) -> None:
    repo = await _make_repo(eval_interval=0, refresh=2_000)
    await _seed_conversation(repo, USER_ID, str(tmp_path))
    db = repo._db
    transport = LocalTransport(db)

    async def send(chat_id: int, text: str, token: str) -> int:
        return await transport.send(
            chat_id,
            text,
            buttons=[
                ("Approve", f"{CALLBACK_PREFIX}:{token}:y"),
                ("Deny", f"{CALLBACK_PREFIX}:{token}:n"),
            ],
        )

    manager = ConfirmationManager(send, chat_ids=[LOCAL_CHAT_ID], timeout_seconds=5)
    settings = MagicMock()
    settings.channel = "local"
    settings.local_tui_secret = "tui"
    settings.webhook_api_secret = "job"
    settings.development_mode = True
    settings.agent_backend = backend
    settings.approved_directory = tmp_path
    client = TestClient(
        create_api_app(
            EventBus(), settings, db_manager=db, confirmation_manager=manager
        )
    )
    tui = PumpClient(api=_Api(client))
    tool = {"tool_name": "Bash", "tool_input": {"command": "git push origin main"}}
    executed = []

    async def run_turn(*args: object, **kwargs: object) -> object:
        if backend == "claude":
            hook = _make_pre_tool_use_hook(
                tmp_path, full_access=True, confirmation_manager=manager
            )
            result = await hook(tool, "tool-1", None)
            allowed = (
                result.get("hookSpecificOutput", {}).get("permissionDecision") != "deny"
            )
        else:
            gate = PolicyGate(
                lambda ctx: TurnPolicy(working_directory=tmp_path, full_access=True),
                manager,
            )
            result = await gate.check(
                "turn-1",
                TurnContext(profile_slug="devops", working_dir=tmp_path),
                tool,
            )
            allowed = result["decision"] == "allow"
        if allowed:
            executed.append(tool)
        response = _mock_response(context_tokens=100)
        response.session_id = "session-1"
        return response

    storage = MagicMock()
    storage.db_manager = db
    storage.memory_repository = None
    storage.messages = MessageRepository(db)
    storage.save_claude_interaction = _real_message_saver(repo)
    adapter = MagicMock()
    adapter.resumable_session_for = AsyncMock(return_value=None)
    turns = LocalChatTurns(
        settings,
        {
            "backends": {backend: adapter},
            "model_router": SimpleNamespace(run_turn=run_turn),
            "window_repo": repo,
            "storage": storage,
        },
    )
    stream = asyncio.create_task(
        _collect(turns.stream(user_id=USER_ID, profile="devops", text="push it"))
    )
    try:
        for _ in range(100):
            asks = await asyncio.to_thread(tui.asks)
            if asks:
                break
            await asyncio.sleep(0.03)
        assert asks
        assert await asyncio.to_thread(tui.deny, asks[0]) == {"matched": True}
        events = await asyncio.wait_for(stream, 5)
        assert any(event["type"] == "done" for event in events), events
        assert executed == []
        assert storage.save_claude_interaction.await_count == 1
    finally:
        await db.close()


async def _collect(stream: AsyncIterator[dict[str, Any]]) -> list[dict[str, Any]]:
    return [event async for event in stream]
