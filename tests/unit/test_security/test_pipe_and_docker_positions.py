"""Commands produced for a shell pipe and Docker's actual subcommand."""

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
from src.security.policy import ALLOW, ASK, TurnPolicy, decide

CWD = Path("/home/user/project")
CASES = [
    (r"echo kill\ 1 | cat | sh", ASK),
    ("printf '%s' 'kill 1' | cat | sh", ASK),
    ("cat script.sh | sh", ASK),
    ("cat <<EOF | sh\necho safe\nEOF", ALLOW),
    ("echo -n 'kill 1' | sh", ASK),
    ("echo -e 'kill 1' | sh", ASK),
    ("echo -ne 'kill 1' | sh", ASK),
    ("echo -e 'ki\\154l 1' | sh", ASK),
    ("echo -n 'safe text' | sh", ALLOW),
    ("printf '%s' 'kill 1' | sh", ASK),
    ("printf 'safe %s' 'kill 1' | sh", ALLOW),
    ("printf 'kill 1' | sh", ASK),
    ("printf '%b' 'ki\\154l 1' | sh", ASK),
    ("docker rm c", ASK),
    ("docker image prune -f", ASK),
    ("docker --context local container rm c", ASK),
    ("docker run --rm alpine echo ok", ALLOW),
    ("docker ps --filter name=rm", ALLOW),
    ("docker run alpine echo rm", ALLOW),
    ("docker compose rm -f svc", ASK),
    ("docker compose -f dev.yml -p proj rm -f svc", ASK),
    ("docker-compose rm -f svc", ASK),
    ("docker plugin rm p", ASK),
    ("docker service rm web", ASK),
    ("docker secret rm s", ASK),
    ("docker compose run --rm svc echo ok", ALLOW),
    ("docker compose ps", ALLOW),
]


@pytest.mark.parametrize("command,expected", CASES)
def test_decide(command: str, expected: str) -> None:
    decision = decide("Bash", {"command": command}, TurnPolicy(CWD, full_access=True))
    assert decision.action == expected


@pytest.mark.parametrize("command,expected", CASES)
async def test_claude_hook(command: str, expected: str) -> None:
    manager = AsyncMock()
    manager.request.return_value = False
    hook = _make_pre_tool_use_hook(
        working_directory=CWD, full_access=True, confirmation_manager=manager
    )
    result = await hook(
        {"tool_name": "Bash", "tool_input": {"command": command}}, "t1", None
    )
    if expected == ASK:
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
        manager.request.assert_awaited_once()
    else:
        assert not result
        manager.request.assert_not_awaited()


@pytest.mark.parametrize("command,expected", CASES)
def test_codex_route(command: str, expected: str) -> None:
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
    assert response.json()["decision"] == ("deny" if expected == ASK else ALLOW)
    assert manager.request.await_count == (1 if expected == ASK else 0)
