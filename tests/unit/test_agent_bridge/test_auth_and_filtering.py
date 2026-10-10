"""Bridge auth (401 paths) and per-context tool filtering.

The filtering assertions mirror ``build_agent_mcp_server``'s mount rules on
purpose: the same context must see the same tools on both transports, so
these are the SDK server's rules restated at the HTTP boundary.
"""

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from starlette.testclient import TestClient

from src import agent_bridge
from src.agent_bridge.server import (
    NOT_EXPORTED,
    AgentBridgeMcp,
    bearer_token,
    build_app,
    exported_tool_names,
)
from src.agent_bridge.tokens import TokenRegistry
from src.claude import agent_tools
from src.claude.agent_tools import ALL_TOOL_SPECS
from src.claude.tool_context import AgentServices, ToolSpec, TurnContext
from src.profiles.registry import ProfileDefinition, ProfileRegistry

USER_A = 111

# The SDK server's mount rules, by the scope each group keys off. These are
# the EXPECTATIONS the filtering tests check the bridge against — written out
# by hand on purpose, so a change to a predicate in ``agent_tools`` shows up
# here as a failure rather than being re-derived and waved through.
MEMORY_TOOLS = frozenset(
    {"remember", "recall", "memory_list", "memory_forget", "memory_correct"}
)
DATA_TOOLS = frozenset(
    {
        "day_summary",
        "sleep_last",
        "lift_history",
        "week_summary",
        "product_find",
        "portion_find",
        "log_food",
        "food_edit",
        "train_edit",
        "activity_edit",
        "log_delete",
        "log_sleep",
        "sleep_edit",
        "log_train",
        "log_activity",
        "product_save",
        "product_delete",
        "portion_save",
        "portion_delete",
    }
)
PLATFORM_TOOLS = frozenset(
    {
        "notify",
        "schedule_add",
        "schedule_list",
        "schedule_remove",
        "task_add",
        "task_list",
        "task_done",
        "get_weather",
    }
)
JOB_TOOLS = frozenset({"job_start", "job_list", "job_output", "jobs_refresh_pin"})


def _profile(slug: str, groups: frozenset) -> ProfileDefinition:
    return ProfileDefinition(
        slug=slug,
        name=slug,
        working_dir=Path(f"/tmp/{slug}"),
        memory_dir=Path(f"/tmp/{slug}/memory"),
        persona=Path(f"/tmp/{slug}/persona.md"),
        default=(slug == "devops"),
        user_data_tools=groups,
    )


@pytest.fixture
def registry() -> ProfileRegistry:
    return ProfileRegistry(
        [
            _profile("devops", frozenset()),
            _profile("coach", frozenset({"logs", "products", "portions"})),
        ]
    )


@pytest.fixture
def services(registry: ProfileRegistry) -> AgentServices:
    """Everything wired: any tool missing from a listing is missing by RULE."""
    return AgentServices(
        event_bus=AsyncMock(),
        task_repo=AsyncMock(),
        profile_registry=registry,
        memory_writer=AsyncMock(),
        memory_repo=AsyncMock(),
        message_repo=AsyncMock(),
        audit_memory_writer=AsyncMock(),
        user_data=AsyncMock(),
        items=AsyncMock(),
    )


def _ctx(**kwargs: Any) -> TurnContext:
    base = dict(
        profile_slug="coach",
        working_dir=Path("/tmp/coach"),
        memory_agent_id="coach#u111",
        user_id=USER_A,
        role="owner",
    )
    base.update(kwargs)
    return TurnContext(**base)  # type: ignore[arg-type]


class TestBearerParsing:
    @pytest.mark.parametrize(
        "header",
        [None, "", "Basic abc", "Bearer", "Bearer   ", "token abc"],
    )
    def test_rejects_non_bearer_headers(self, header) -> None:
        assert bearer_token(header) is None

    def test_accepts_a_bearer_header(self) -> None:
        assert bearer_token("Bearer abc123") == "abc123"
        assert bearer_token("bearer abc123") == "abc123"


class TestAuth:
    """Every unauthenticated shape must be 401 — never a default context."""

    @pytest.fixture
    def tokens(self) -> TokenRegistry:
        return TokenRegistry()

    @pytest.fixture
    def client(self, services: AgentServices, tokens: TokenRegistry):
        # `with` runs the app lifespan, which is what starts the MCP session
        # manager's task group.
        with TestClient(build_app(services, tokens)) as client:
            yield client

    @pytest.mark.parametrize(
        "headers",
        [
            {},
            {"Authorization": "Bearer wrong-token"},
            {"Authorization": "Basic hunter2"},
            {"Authorization": "Bearer "},
        ],
    )
    def test_unauthenticated_requests_are_401(self, client, headers) -> None:
        response = client.post("/mcp", json={}, headers=headers)
        assert response.status_code == 401
        assert response.json() == {"error": "unauthorized"}

    def test_401_body_never_echoes_the_token(self, client) -> None:
        secret = "s3cret-token-value"
        response = client.post(
            "/mcp", json={}, headers={"Authorization": f"Bearer {secret}"}
        )
        assert response.status_code == 401
        assert secret not in response.text

    def test_a_live_token_gets_past_auth(self, client, tokens) -> None:
        token = tokens.mint(_ctx())
        response = client.post(
            "/mcp", json={}, headers={"Authorization": f"Bearer {token}"}
        )
        # Past the gate: whatever the MCP layer says about a bare POST, it is
        # not "unauthorized".
        assert response.status_code != 401

    def test_revoked_token_is_401_again(self, client, tokens) -> None:
        token = tokens.mint(_ctx())
        tokens.revoke(token)
        response = client.post(
            "/mcp", json={}, headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 401

    def test_expired_token_is_401(self, services) -> None:
        clock = type("C", (), {"now": 0.0})()
        tokens = TokenRegistry(ttl_seconds=10, clock=lambda: clock.now)
        with TestClient(build_app(services, tokens)) as client:
            token = tokens.mint(_ctx())
            clock.now = 11.0
            response = client.post(
                "/mcp", json={}, headers={"Authorization": f"Bearer {token}"}
            )
        assert response.status_code == 401


class TestToolFiltering:
    """The listing a context gets is the SDK server's mount rules, restated."""

    @pytest.fixture
    def bridge(self, services: AgentServices) -> AgentBridgeMcp:
        return AgentBridgeMcp(services, TokenRegistry())

    def _names(self, bridge: AgentBridgeMcp, ctx: TurnContext) -> set:
        return {spec.name for spec in bridge.specs_for(ctx)}

    def test_owner_on_a_data_profile_sees_every_exported_tool(self, bridge) -> None:
        # `coach` declares logs/products/portions but not `export`, so
        # export_my_data is not visible; hireli_* need a client + `hireli: true`
        # (tests/unit/test_hireli_tools.py covers their gating); exchange_*
        # likewise (tests/unit/test_exchange_tools.py).
        opt_in = {
            n for n in exported_tool_names() if n.startswith(("hireli_", "exchange_"))
        }
        assert (
            self._names(bridge, _ctx())
            == set(exported_tool_names()) - {"export_my_data"} - opt_in
        )

    def test_no_memory_scope_means_no_memory_tools(self, bridge) -> None:
        names = self._names(bridge, _ctx(memory_agent_id=None))
        assert not (names & MEMORY_TOOLS)
        # recall_conversation is scoped by user_id, not by memory scope
        assert "recall_conversation" in names

    def test_no_user_id_means_no_data_and_no_conversation_search(self, bridge) -> None:
        names = self._names(bridge, _ctx(user_id=None))
        assert not (names & DATA_TOOLS)
        assert "recall_conversation" not in names
        assert "remember" in names  # memory scope is still resolved

    def test_profile_without_data_groups_gets_no_data_tools(self, bridge) -> None:
        names = self._names(
            bridge,
            _ctx(profile_slug="devops", memory_agent_id="devops"),
        )
        assert not (names & DATA_TOOLS)
        assert MEMORY_TOOLS | {"recall_conversation"} <= names

    def test_per_user_profile_suffix_still_resolves_capabilities(self, bridge) -> None:
        """'coach#u111' must read its groups off the base 'coach' profile."""
        names = self._names(bridge, _ctx(profile_slug="coach#u111"))
        assert DATA_TOOLS <= names

    def test_role_is_the_only_difference_between_owner_and_guest(self, bridge) -> None:
        """Jobs are owner-only; nothing else keys off the role."""
        owner = self._names(bridge, _ctx(role="owner"))
        guest = self._names(bridge, _ctx(role="guest"))
        assert owner - guest == set(JOB_TOOLS)
        assert not (guest & JOB_TOOLS)

    def test_platform_tools_need_no_scope_at_all(self, bridge) -> None:
        """Nothing here depends on memory or data scope."""
        bare = self._names(bridge, _ctx(memory_agent_id=None, user_id=None, role=None))
        assert PLATFORM_TOOLS <= bare


def _spec(name: str) -> ToolSpec:
    async def handler(services: AgentServices, ctx: TurnContext, args: Any) -> Any:
        return {"content": [{"type": "text", "text": f"{name} ran"}]}

    return ToolSpec(
        name=name,
        description=f"test tool {name}",
        schema={"type": "object", "properties": {}},
        handler=handler,
    )


class TestExposure:
    """The bridge serves what the SDK serves, minus a declared denylist.

    Before dv-afda the bridge exported a hand-maintained allowlist, and a
    tool added to ``agent_tools.py`` silently never reached Codex. Now the
    default is "everything ``available_tool_specs`` returns" and the two
    transports diverge only by ``NOT_EXPORTED`` — each entry a real spec name
    with a one-line reason.
    """

    @pytest.fixture
    def tokens(self) -> TokenRegistry:
        return TokenRegistry()

    def _names(self, bridge: AgentBridgeMcp, ctx: TurnContext) -> set:
        return {spec.name for spec in bridge.specs_for(ctx)}

    def test_a_newly_added_spec_is_exposed_by_default(
        self, services: AgentServices, tokens: TokenRegistry, monkeypatch: Any
    ) -> None:
        """Add a spec to the shared list and touch nothing in the bridge."""
        monkeypatch.setattr(
            agent_tools, "ALL_TOOL_SPECS", [*ALL_TOOL_SPECS, _spec("brand_new_tool")]
        )
        bridge = AgentBridgeMcp(services, tokens)
        assert "brand_new_tool" in self._names(bridge, _ctx())
        assert "brand_new_tool" in exported_tool_names()

    async def test_a_denylisted_spec_is_neither_listed_nor_callable(
        self, services: AgentServices, tokens: TokenRegistry, monkeypatch: Any
    ) -> None:
        """Off, not hidden: the model can name a tool it was never shown."""
        ctx = _ctx()
        bridge = AgentBridgeMcp(services, tokens, denied={"remember"})
        monkeypatch.setattr(bridge, "resolve_context", lambda: ctx)

        listed = {tool.name for tool in await bridge.list_tools()}
        assert "remember" not in listed
        assert "recall" in listed  # only the named tool is withheld

        out = await bridge.call_tool("remember", {"content": "x"})
        assert out.isError
        assert "not available in this session" in out.content[0].text  # type: ignore[union-attr]
        services.memory_writer.apply_ops.assert_not_called()

    def test_the_denylist_is_the_only_difference_from_the_sdk_list(self) -> None:
        every = {spec.name for spec in ALL_TOOL_SPECS}
        assert every - set(exported_tool_names()) == set(NOT_EXPORTED)
        assert set(NOT_EXPORTED) <= every, "an entry names a spec that no longer exists"

    def test_every_denylist_entry_carries_a_reason(self) -> None:
        for name, reason in NOT_EXPORTED.items():
            assert isinstance(reason, str) and reason.strip(), name

    def test_the_package_exports_no_allowlist(self) -> None:
        """Waves were a roll-out switch; nothing may grow a new one quietly."""
        assert not [n for n in dir(agent_bridge) if n.startswith("WAVE_")]
        assert not hasattr(agent_bridge, "EXPORTED_TOOLS")
