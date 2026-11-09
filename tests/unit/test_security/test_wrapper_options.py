"""Wrapper option values must not hide the command the shell executes."""

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from starlette.testclient import TestClient

from src.agent_bridge import POLICY_PATH
from src.agent_bridge.policy_gate import PolicyGate
from src.agent_bridge.server import build_app
from src.agent_bridge.tokens import TokenRegistry
from src.claude.sdk_integration import _make_pre_tool_use_hook
from src.claude.tool_context import AgentServices, TurnContext
from src.security.policy import ASK, TurnPolicy, decide

CWD = Path("/home/user/project")

COMMANDS = [
    ("sudo -u root rm -rf /home/user/data", "file deletion"),
    ("sudo -u root systemctl restart nginx", "service control"),
    ("env -u X systemctl restart nginx", "service control"),
    ("nice -n 5 docker rm c", "docker resource removal"),
    ("sudo --user=root -- systemctl restart nginx", "service control"),
    ("env --unset=X systemctl restart nginx", "service control"),
    ("env -S 'systemctl restart nginx'", "service control"),
    ("nice --adjustment=5 docker rm c", "docker resource removal"),
]


@pytest.mark.parametrize("command,reason", COMMANDS)
def test_decide_wrapped_command_asks(command: str, reason: str) -> None:
    decision = decide("Bash", {"command": command}, TurnPolicy(CWD, full_access=True))
    assert decision.action == ASK
    assert reason in decision.reason


@pytest.mark.parametrize(
    "command",
    [
        "env -u X grep 'systemctl restart' docs/a.md",
        "sudo -u root grep 'docker rm c' docs/a.md",
    ],
)
def test_wrapper_does_not_execute_quoted_search_text(command: str) -> None:
    decision = decide("Bash", {"command": command}, TurnPolicy(CWD, full_access=True))
    assert decision.action != ASK


@pytest.mark.parametrize("command,reason", COMMANDS)
async def test_claude_hook_wrapped_command_asks(command: str, reason: str) -> None:
    manager = AsyncMock()
    manager.request.return_value = False
    hook = _make_pre_tool_use_hook(
        working_directory=CWD, full_access=True, confirmation_manager=manager
    )
    result = await hook(
        {"tool_name": "Bash", "tool_input": {"command": command}}, "t1", None
    )
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    manager.request.assert_awaited_once()


@pytest.mark.parametrize("command,reason", COMMANDS)
def test_codex_route_wrapped_command_asks(command: str, reason: str) -> None:
    manager = AsyncMock()
    manager.request.return_value = False
    tokens = TokenRegistry()
    app = build_app(AgentServices(event_bus=None, task_repo=None), tokens)
    app.state.policy_gate = PolicyGate(
        lambda ctx: TurnPolicy(ctx.working_dir, full_access=True), manager
    )
    token = tokens.mint(TurnContext(profile_slug="devops", working_dir=CWD))
    with TestClient(app) as client:
        response = client.post(
            POLICY_PATH,
            json={"tool_name": "Bash", "tool_input": {"command": command}},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 200
    assert response.json()["decision"] == "deny"
    manager.request.assert_awaited_once()
