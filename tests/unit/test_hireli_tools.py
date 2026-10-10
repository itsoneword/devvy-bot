"""Hireli proxy tools (dv-9f02): mount gating and the outgoing request shape."""

import json
from pathlib import Path
from typing import Any, Dict, List

import httpx
import pytest

from src.claude.agent_tools import ALL_TOOL_SPECS, available_tool_specs
from src.claude.hireli_tools import HIRELI_SPECS, HireliClient
from src.claude.tool_context import AgentServices, TurnContext
from src.profiles.registry import ProfileDefinition, ProfileRegistry

KEY = "svc-key-secret"
URL = "http://127.0.0.1:8001/mcp"


def _profile(slug: str, hireli: bool) -> ProfileDefinition:
    return ProfileDefinition(
        slug=slug,
        name=slug,
        working_dir=Path(f"/tmp/{slug}"),
        memory_dir=Path(f"/tmp/{slug}/memory"),
        persona=Path(f"/tmp/{slug}/persona.md"),
        default=slug == "on",
        hireli=hireli,
    )


def _registry() -> ProfileRegistry:
    return ProfileRegistry([_profile("on", True), _profile("off", False)])


def _client(seen: List[httpx.Request], reply: Dict[str, Any]) -> HireliClient:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=reply)

    return HireliClient(URL, KEY, transport=httpx.MockTransport(handler))


def _hireli_names(services: AgentServices, ctx: TurnContext) -> set:
    return {
        s.name
        for s in available_tool_specs(services, ctx)
        if s.name.startswith("hireli_")
    }


ALL_NAMES = {s.name for s in HIRELI_SPECS}


def test_specs_registered_on_both_transports() -> None:
    assert ALL_NAMES <= {s.name for s in ALL_TOOL_SPECS}
    assert "hireli_link_telegram" in ALL_NAMES and len(ALL_NAMES) == 15


@pytest.mark.parametrize(
    "has_client,slug,user_id,expected",
    [
        (True, "on", 42, True),
        (True, "on#u42", 42, True),  # per-user suffix resolves to base profile
        (True, "off", 42, False),  # profile not opted in
        (False, "on", 42, False),  # no key configured
        (True, "on", None, False),  # webhook/scheduler turn: no sender
    ],
)
def test_mount_gating(
    has_client: bool, slug: str, user_id: Any, expected: bool
) -> None:
    services = AgentServices(
        event_bus=None,
        task_repo=None,
        profile_registry=_registry(),
        hireli=_client([], {}) if has_client else None,
    )
    ctx = TurnContext(profile_slug=slug, user_id=user_id)
    assert (_hireli_names(services, ctx) == ALL_NAMES) is expected


async def test_call_sends_key_and_sender_id_not_model_input() -> None:
    seen: List[httpx.Request] = []
    reply = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"content": [{"type": "text", "text": '{"linked":false}'}]},
    }
    services = AgentServices(
        event_bus=None,
        task_repo=None,
        profile_registry=_registry(),
        hireli=_client(seen, reply),
    )
    ctx = TurnContext(profile_slug="on", user_id=42)
    spec = next(s for s in HIRELI_SPECS if s.name == "hireli_list_applications")
    # A model trying to smuggle an identity is just an argument Hireli ignores.
    out = await spec.handler(services, ctx, {"status": "applied"})

    assert out == {"content": [{"type": "text", "text": '{"linked":false}'}]}
    req = seen[0]
    assert req.headers["authorization"] == f"Bearer {KEY}"
    assert req.headers["x-telegram-user-id"] == "42"
    body = json.loads(req.content)
    assert body["method"] == "tools/call"
    assert body["params"] == {
        "name": "list_applications",
        "arguments": {"status": "applied"},
    }


async def test_tool_error_passes_through_as_error() -> None:
    reply = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "content": [{"type": "text", "text": "not linked"}],
            "isError": True,
        },
    }
    client = _client([], reply)
    out = await client.call("list_roles", {}, 42)
    assert out["isError"] is True and out["content"][0]["text"] == "not linked"


async def test_http_error_and_unreachable_do_not_leak_key() -> None:
    def denied(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "invalid token"})

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    for handler in (denied, down):
        client = HireliClient(URL, KEY, transport=httpx.MockTransport(handler))
        out = await client.call("whoami", {}, 42)
        assert out["isError"] is True
        assert KEY not in json.dumps(out)
