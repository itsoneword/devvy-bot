"""Per-call cost + our own per-request cap (dv-d2f6, docs/analysis/dv-d2f6-cost-caps.md).

Raw stream-json events go through the REAL ``parse_message`` here (no identity
patch): the meter reads the raw dict, the rest of the loop the parsed message,
exactly as in production. Event shapes are copied from CLI 2.1.302 probes.
"""

import asyncio
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.claude.sdk_integration import ClaudeResponse, ClaudeSDKManager
from src.config.settings import Settings
from src.model_router import ModelRouter
from src.model_router.routes import (
    PROVIDER_ANTHROPIC_SUB,
    PROVIDER_OPENROUTER,
    Route,
    cost_for,
)
from src.model_router.turn_cost import (
    COST_ESTIMATE,
    COST_EXACT,
    GenerationLookup,
    TurnCostMeter,
)
from src.model_router.types import CLAUDE_HARNESS
from src.profiles.registry import load_profile_registry
from src.storage.database import DatabaseManager
from src.storage.model_calls import ModelCallRepository

GLM = "z-ai/glm-5.3"
SUB = Route(CLAUDE_HARNESS, PROVIDER_ANTHROPIC_SUB, None)
OR = Route(CLAUDE_HARNESS, PROVIDER_OPENROUTER, GLM)

# A real subscription event (haiku, 2026-10-17) and the CLI's total for it.
HAIKU_USAGE = {
    "input_tokens": 2,
    "cache_creation_input_tokens": 13118,
    "cache_read_input_tokens": 11228,
    "cache_creation": {
        "ephemeral_5m_input_tokens": 0,
        "ephemeral_1h_input_tokens": 13118,
    },
    "output_tokens": 4,
}
HAIKU_CLI_TOTAL = 0.00273808


def _usage(inp: int = 1000, out: int = 100, cr: int = 0) -> Dict[str, Any]:
    return {
        "input_tokens": inp,
        "output_tokens": out,
        "cache_read_input_tokens": cr,
        "cache_creation_input_tokens": 0,
    }


def _assistant(
    request_id: Optional[str],
    *,
    usage: Optional[Dict[str, Any]] = None,
    tool: bool = False,
    text: str = "working",
    model: str = GLM,
    msg_id: str = "msg_1",
) -> Dict[str, Any]:
    content: List[Dict[str, Any]] = [{"type": "text", "text": text}]
    if tool:
        content = [{"type": "tool_use", "id": f"tu_{msg_id}", "name": "Read",
                    "input": {"file_path": "x"}}]  # fmt: skip
    raw: Dict[str, Any] = {
        "type": "assistant",
        "message": {
            "id": msg_id,
            "model": model,
            "role": "assistant",
            "content": content,
            "usage": usage or _usage(),
        },
        "parent_tool_use_id": None,
        "session_id": "sess",
    }
    if request_id is not None:
        raw["request_id"] = request_id
    return raw


_ZERO = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": None,
         "cache_creation_input_tokens": None}  # fmt: skip


def _or_call(gen: str, msg_id: str, *, billed: Optional[float], tool: bool,
             usage: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:  # fmt: skip
    """One OpenRouter call as CLI 2.1.302 streams it with partial messages:
    message_start, the assistant event(s) with ZERO usage, then the
    message_delta carrying the real usage and OpenRouter's ``usage.cost``."""
    delta_usage = dict(usage or _usage())
    if billed is not None:
        delta_usage["cost"] = billed
    base = {"session_id": "sess", "parent_tool_use_id": None, "uuid": "u"}
    return [
        {**base, "type": "stream_event", "event": {
            "type": "message_start",
            "message": {"id": msg_id, "model": GLM, "usage": dict(_ZERO)}}},
        _assistant(gen, usage=dict(_ZERO), tool=tool, msg_id=msg_id),
        {**base, "type": "stream_event", "event": {
            "type": "message_delta",
            "delta": {"stop_reason": "tool_use" if tool else "end_turn"},
            "usage": delta_usage}},
    ]  # fmt: skip


def _result(cost: float = 0.5, subtype: str = "success", result: Any = "done"):
    return {
        "type": "result",
        "subtype": subtype,
        "duration_ms": 10,
        "duration_api_ms": 8,
        "is_error": subtype != "success",
        "num_turns": 1,
        "session_id": "sess",
        "total_cost_usd": cost,
        "usage": _usage(),
        "result": result,
    }


def _client_factory(*raws: Dict[str, Any], clients: Optional[list] = None,
                    options: Optional[list] = None):  # fmt: skip

    def factory(opts: Any) -> Any:
        client = AsyncMock()

        async def receive():
            for raw in raws:
                yield raw

        client._query = MagicMock()
        client._query.receive_messages = receive
        if clients is not None:
            clients.append(client)
        if options is not None:
            options.append(opts)
        return client

    return factory


def _config(tmp_path: Path, cap: float = 5.0) -> Settings:
    return Settings(
        telegram_bot_token="t:t",
        telegram_bot_username="b",
        approved_directory=tmp_path,
        claude_timeout_seconds=5,
        claude_max_cost_per_request=cap,
        enable_mcp=False,
    )


def _or_manager(tmp_path: Path, cap: float = 0.2) -> ClaudeSDKManager:
    p = tmp_path / "profiles.yaml"
    p.write_text(f"""
profiles:
  - slug: devops
    name: DevOps
    working_dir: {tmp_path}/repo
    memory_dir: {tmp_path}/repo/memory
    persona: {tmp_path}/repo/agent.md
    default: true
    sandbox: false
  - slug: scratch
    name: Scratch
    working_dir: {tmp_path}/ws/scratch
    memory_dir: {tmp_path}/ws/scratch/memory
    persona: {tmp_path}/ws/scratch/persona.md
    provider: openrouter
    model: {GLM}
    max_cost_per_request: {cap}
""")
    (tmp_path / "ws" / "scratch").mkdir(parents=True)
    return ClaudeSDKManager(
        _config(tmp_path), profile_registry=load_profile_registry(p)
    )


class _Lookup:
    """Fake /generation: exact cost per id, None = lookup failed."""

    def __init__(self, costs: Dict[str, Optional[float]], delay: float = 0.0) -> None:
        self.costs = costs
        self.delay = delay
        self.calls: List[str] = []

    async def __call__(self, gen_id: str) -> Optional[float]:
        self.calls.append(gen_id)
        await asyncio.sleep(self.delay)
        return self.costs.get(gen_id)


async def _run_or(manager: ClaudeSDKManager, tmp_path: Path, lookup: _Lookup,
                  *raws: Dict[str, Any], clients: Optional[list] = None,
                  role: str = "owner") -> ClaudeResponse:  # fmt: skip
    with (
        patch(
            "src.claude.sdk_integration.ClaudeSDKClient",
            side_effect=_client_factory(*raws, clients=clients),
        ),
        patch("src.claude.sdk_integration.openrouter_key", return_value="sk-test"),
        patch.object(ClaudeSDKManager, "_generation_lookup", return_value=lookup),
    ):
        return await manager.execute_command(
            prompt="p", working_directory=tmp_path / "ws" / "scratch", role=role
        )


# --- the one cost function ---------------------------------------------------


def test_cost_for_subscription_matches_cli_total_with_1h_cache_split():
    """Per-call price == the CLI's total_cost_usd on the same call."""
    cost = cost_for(SUB, model="claude-haiku-5-5", usage=HAIKU_USAGE)
    assert cost == pytest.approx(HAIKU_CLI_TOTAL, rel=1e-9)


def test_cost_for_openrouter_prices_route_model_conservatively():
    cost = cost_for(OR, model="claude-sonnet-x", usage=_usage(1_000_000, 0))
    assert cost == pytest.approx(2.80)  # GLM max-endpoint input, not sonnet


def test_cost_for_exact_wins_and_unknown_third_party_is_none():
    assert cost_for(OR, model=None, usage=_usage(), exact=0.0042) == 0.0042
    unknown = Route(CLAUDE_HARNESS, PROVIDER_OPENROUTER, "acme/none")
    assert cost_for(unknown, model=None, usage=_usage()) is None


# --- meter -------------------------------------------------------------------


async def test_meter_dedupes_blocks_of_one_call_and_settles_to_exact():
    lookup = _Lookup({"gen-1": 0.001, "gen-2": 0.002})
    meter = TurnCostMeter(OR, cap=None, lookup=lookup)
    assert meter.observe(_assistant("gen-1", usage=_usage(out=10)))
    assert meter.observe(_assistant("gen-1", usage=_usage(out=100), tool=True))
    assert meter.observe(_assistant("gen-2", msg_id="msg_2"))
    assert not meter.observe({"type": "user", "message": {}})
    assert len(meter.calls) == 2
    estimate = meter.total
    assert estimate == pytest.approx(2 * cost_for(OR, model=GLM, usage=_usage()))
    settled = await meter.settle()
    assert settled.cost == pytest.approx(0.003)
    assert settled.source == COST_EXACT
    assert settled.reported == pytest.approx(estimate)
    assert lookup.calls == ["gen-1", "gen-2"]


async def test_meter_missing_request_id_is_counted_and_estimated():
    lookup = _Lookup({})
    meter = TurnCostMeter(OR, cap=None, lookup=lookup)
    meter.observe(_assistant(None))
    assert meter.missing_request_ids == 1
    assert meter.settle() is None  # nothing to look up
    assert meter.source == COST_ESTIMATE
    assert meter.total > 0
    assert lookup.calls == []


async def test_meter_never_looks_up_subscription_calls():
    lookup = _Lookup({})
    meter = TurnCostMeter(SUB, cap=1.0, lookup=lookup)
    meter.observe(_assistant("req_011", model="claude-haiku-5-5", usage=HAIKU_USAGE))
    assert meter.total == pytest.approx(HAIKU_CLI_TOTAL)
    assert meter.settle() is None and lookup.calls == []


async def test_generation_lookup_retries_404_then_reads_total_cost():
    responses = [MagicMock(status_code=404), MagicMock(status_code=200)]
    responses[1].json.return_value = {"data": {"total_cost": 0.0123}}
    client = MagicMock()
    client.get = AsyncMock(side_effect=responses)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    with patch("src.model_router.turn_cost.httpx.AsyncClient", return_value=client):
        cost = await GenerationLookup("k", delays=(0, 0, 0))("gen-x")
    assert cost == 0.0123
    assert client.get.await_count == 2


async def test_generation_lookup_gives_up_on_401_and_without_key():
    resp = MagicMock(status_code=401)
    client = MagicMock()
    client.get = AsyncMock(return_value=resp)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    with patch("src.model_router.turn_cost.httpx.AsyncClient", return_value=client):
        assert await GenerationLookup("k", delays=(0, 0, 0))("gen-x") is None
    assert client.get.await_count == 1
    assert await GenerationLookup(None)("gen-x") is None


# --- OpenRouter route through execute_command ---------------------------------


async def test_openrouter_turn_bills_estimate_then_settles_exact(tmp_path):
    manager = _or_manager(tmp_path)
    # The truth arrives after the turn returned (~6 s live).
    lookup = _Lookup({"gen-a": 0.0011, "gen-b": 0.0022}, delay=0.2)
    response = await _run_or(
        manager, tmp_path, lookup,
        _assistant("gen-a", tool=True),
        _assistant("gen-b", msg_id="msg_2", text="done"),
        _result(cost=0.9),  # the CLI's Anthropic-table figure: ignored
    )  # fmt: skip
    per_call = cost_for(OR, model=GLM, usage=_usage())
    assert response.cost == pytest.approx(2 * per_call)
    assert response.cost_source == COST_ESTIMATE
    assert response.api_calls == 2 and response.request_ids_missing == 0
    assert response.error_type is None
    settled = await response.cost_settlement
    assert settled.cost == pytest.approx(0.0033) and settled.source == COST_EXACT


async def test_openrouter_lookup_failure_keeps_estimate(tmp_path):
    manager = _or_manager(tmp_path)
    response = await _run_or(
        manager, tmp_path, _Lookup({"gen-a": None}, delay=0.2),
        _assistant("gen-a"), _result(),
    )  # fmt: skip
    settled = await response.cost_settlement
    assert settled.source == COST_ESTIMATE
    assert settled.cost == pytest.approx(response.cost)
    assert response.error_type is None


async def test_openrouter_cap_interrupts_on_our_price_not_the_clis(tmp_path):
    """Cap $0.005: the 2nd tool call crosses it -> our interrupt, max_budget."""
    manager = _or_manager(tmp_path, cap=0.005)
    per_call = cost_for(OR, model=GLM, usage=_usage())
    assert per_call < 0.005 < 2 * per_call  # the fixture's premise
    clients: list = []
    response = await _run_or(
        manager, tmp_path, _Lookup({}),
        _assistant("gen-a", tool=True),
        _assistant("gen-b", tool=True, msg_id="msg_2"),
        _result(subtype="error_during_execution", result=None),
        clients=clients,
    )  # fmt: skip
    clients[0].interrupt.assert_awaited_once()
    assert response.error_type == "max_budget"


async def test_openrouter_stream_cost_is_charged_at_once(tmp_path):
    """Real OR shape: assistant usage is zero; the delta's usage.cost is the
    charge, then /generation confirms it in the background."""
    manager = _or_manager(tmp_path)
    lookup = _Lookup({"gen-a": 0.0101, "gen-b": 0.0202}, delay=0.2)
    options: list = []
    with (
        patch(
            "src.claude.sdk_integration.ClaudeSDKClient",
            side_effect=_client_factory(
                *_or_call("gen-a", "msg_a", billed=0.01, tool=True),
                *_or_call("gen-b", "msg_b", billed=0.02, tool=False),
                _result(cost=0.9),
                options=options,
            ),
        ),
        patch("src.claude.sdk_integration.openrouter_key", return_value="k"),
        patch.object(ClaudeSDKManager, "_generation_lookup", return_value=lookup),
    ):
        response = await manager.execute_command(
            prompt="p", working_directory=tmp_path / "ws" / "scratch", role="owner"
        )
    assert options[0].include_partial_messages is True  # needed for the delta
    assert response.cost == pytest.approx(0.03)
    assert response.cost_source == COST_EXACT
    assert response.api_calls == 2 and response.request_ids_missing == 0
    assert response.content == "done"
    settled = await response.cost_settlement
    assert settled.cost == pytest.approx(0.0303)
    assert lookup.calls == ["gen-a", "gen-b"]


async def test_openrouter_cap_fires_on_the_delta_of_a_tool_call(tmp_path):
    manager = _or_manager(tmp_path, cap=0.05)
    clients: list = []
    response = await _run_or(
        manager, tmp_path, _Lookup({}),
        *_or_call("gen-a", "msg_a", billed=0.03, tool=True),
        *_or_call("gen-b", "msg_b", billed=0.03, tool=True),
        _result(subtype="error_during_execution", result=None),
        clients=clients,
    )  # fmt: skip
    clients[0].interrupt.assert_awaited_once()
    assert response.error_type == "max_budget"
    assert response.cost == pytest.approx(0.06)


async def test_openrouter_under_cap_ignores_cli_cost(tmp_path):
    """The CLI's figure (15x high on GLM) used to trip its own cap."""
    manager = _or_manager(tmp_path, cap=0.2)
    clients: list = []
    response = await _run_or(
        manager, tmp_path, _Lookup({}),
        _assistant("gen-a", tool=True), _result(cost=0.25),
        clients=clients,
    )  # fmt: skip
    clients[0].interrupt.assert_not_awaited()
    assert response.error_type is None


# --- subscription parity -------------------------------------------------------


async def _run_sub(tmp_path: Path, *raws: Dict[str, Any], role: Optional[str],
                   cap: float = 5.0):  # fmt: skip
    manager = ClaudeSDKManager(_config(tmp_path, cap=cap))
    clients: list = []
    options: list = []
    lookup_factory = MagicMock()
    with (
        patch(
            "src.claude.sdk_integration.ClaudeSDKClient",
            side_effect=_client_factory(*raws, clients=clients, options=options),
        ),
        patch.object(ClaudeSDKManager, "_generation_lookup", lookup_factory),
    ):
        response = await manager.execute_command(
            prompt="p", working_directory=tmp_path, role=role
        )
    lookup_factory.assert_not_called()
    return response, clients[0], options[0]


async def test_subscription_cost_is_still_the_clis_and_unchanged(tmp_path):
    response, client, opts = await _run_sub(
        tmp_path,
        _assistant("req_1", model="claude-haiku-5-5", usage=HAIKU_USAGE),
        _result(cost=HAIKU_CLI_TOTAL),
        role="guest",
    )
    assert response.cost == HAIKU_CLI_TOTAL
    assert response.cost_source is None and response.cost_settlement is None
    assert response.provider == PROVIDER_ANTHROPIC_SUB and response.model is None
    assert response.api_calls == 1 and response.request_ids_missing == 0
    assert opts.max_budget_usd is None
    assert opts.include_partial_messages is False  # unchanged: no callback
    client.interrupt.assert_not_awaited()


async def test_subscription_cap_is_enforced_by_our_loop(tmp_path):
    """Same outcome as the CLI cap had: stopped, labelled max_budget."""
    big = {**HAIKU_USAGE, "output_tokens": 20_000_000}  # $10 at haiku-5-5 rates
    response, client, _ = await _run_sub(
        tmp_path,
        _assistant("req_1", model="claude-haiku-5-5", usage=big, tool=True),
        _result(subtype="error_during_execution", result=None),
        role="guest",
    )
    client.interrupt.assert_awaited_once()
    assert response.error_type == "max_budget"


async def test_subscription_owner_stays_uncapped(tmp_path):
    big = {**HAIKU_USAGE, "output_tokens": 20_000_000}
    response, client, _ = await _run_sub(
        tmp_path,
        _assistant("req_1", model="claude-haiku-5-5", usage=big, tool=True),
        _result(),
        role="owner",
    )
    client.interrupt.assert_not_awaited()
    assert response.error_type is None


async def test_final_answer_over_cap_is_delivered_not_cut(tmp_path):
    big = {**HAIKU_USAGE, "output_tokens": 20_000_000}
    response, client, _ = await _run_sub(
        tmp_path,
        _assistant("req_1", model="claude-haiku-5-5", usage=big, text="answer"),
        _result(result="answer"),
        role="guest",
    )
    client.interrupt.assert_not_awaited()
    assert response.error_type is None and response.content == "answer"


# --- sinks: model_calls row, ledgers, limiter -----------------------------------


@pytest.fixture
async def db(tmp_path):
    manager = DatabaseManager(f"sqlite:///{tmp_path / 'b.db'}")
    await manager.initialize()
    yield manager
    await manager.close()


def _backend(response: ClaudeResponse, backend_id: str = "claude") -> MagicMock:
    backend = MagicMock()
    backend.backend_id = backend_id
    backend.default_model.return_value = "sonnet"
    backend.run_command = AsyncMock(return_value=response)
    return backend


async def _settled_task(cost: float, source: str, reported: float):
    from src.model_router.turn_cost import Settlement

    return Settlement(cost=cost, source=source, reported=reported)


async def _drain() -> None:
    """Let the background true-up (aiosqlite thread hops) finish."""
    for _ in range(20):
        await asyncio.sleep(0.02)


async def test_router_row_swaps_estimate_for_exact(db):
    settle = asyncio.ensure_future(_settled_task(0.004, COST_EXACT, 0.02))
    response = ClaudeResponse(
        content="hi", session_id="s", cost=0.02, duration_ms=1, num_turns=1,
        provider=PROVIDER_OPENROUTER, model=GLM, cost_source=COST_ESTIMATE,
        cost_settlement=settle,
    )  # fmt: skip
    router = ModelRouter(backends={"claude": _backend(response)},
                         recorder=ModelCallRepository(db))  # fmt: skip
    await router.run_turn("claude", purpose="chat", prompt="x")
    await settle
    await _drain()
    async with db.get_connection() as conn:
        cur = await conn.execute("SELECT cost_usd, cost_source FROM model_calls")
        assert tuple(await cur.fetchone()) == (0.004, COST_EXACT)


@pytest.mark.parametrize("backend_id,cost", [("claude", 0.05), ("openai_codex", None)])
async def test_router_subscription_and_codex_rows_unchanged(db, backend_id, cost):
    response = ClaudeResponse(content="hi", session_id="s", cost=cost,
                              duration_ms=1, num_turns=1)  # fmt: skip
    router = ModelRouter(backends={backend_id: _backend(response, backend_id)},
                         recorder=ModelCallRepository(db))  # fmt: skip
    await router.run_turn(backend_id, purpose="chat", prompt="x")
    await _drain()
    async with db.get_connection() as conn:
        cur = await conn.execute("SELECT cost_usd, cost_source FROM model_calls")
        assert tuple(await cur.fetchone()) == (cost, None)


async def test_storage_ledgers_swap_estimate_for_exact(tmp_path):
    from src.storage.facade import Storage

    storage = Storage(f"sqlite:///{tmp_path / 'c.db'}")
    await storage.initialize()
    try:
        await storage.get_or_create_user(7, "u")
        await storage.create_session(7, "/p", "s")
        settle = asyncio.ensure_future(_settled_task(0.004, COST_EXACT, 0.02))
        response = ClaudeResponse(
            content="hi", session_id="s", cost=0.02, duration_ms=1, num_turns=1,
            cost_source=COST_ESTIMATE, cost_settlement=settle,
        )  # fmt: skip
        mid = await storage.save_claude_interaction(7, "s", "p", response)
        await settle
        await _drain()
        async with storage.db_manager.get_connection() as conn:
            cur = await conn.execute(
                "SELECT cost FROM messages WHERE message_id = ?", (mid,)
            )
            assert (await cur.fetchone())[0] == pytest.approx(0.004)
            cur = await conn.execute(
                "SELECT daily_cost, request_count FROM cost_tracking WHERE user_id=7"
            )
            daily, count = await cur.fetchone()
            assert daily == pytest.approx(0.004) and count == 1
            cur = await conn.execute("SELECT total_cost FROM users WHERE user_id=7")
            assert (await cur.fetchone())[0] == pytest.approx(0.004)
    finally:
        await storage.close()


async def test_rate_limiter_adjust_cost_floors_at_zero():
    from src.security.rate_limiter import RateLimiter

    limiter = RateLimiter(_config(Path("/tmp")))
    await limiter.record_cost(1, 0.02)
    await limiter.adjust_cost(1, -0.016)
    assert limiter.cost_tracker[1] == pytest.approx(0.004)
    await limiter.adjust_cost(1, -1.0)
    assert limiter.cost_tracker[1] == 0.0


# --- daily reconciliation -----------------------------------------------------


async def test_reconcile_alerts_on_drift_and_stays_quiet_when_close():
    from src.model_router.or_reconcile import OpenRouterReconciler

    calls = MagicMock()
    calls.provider_cost_on = AsyncMock(return_value=(0.10, 12))
    notes: List[str] = []

    async def notify(text: str) -> None:
        notes.append(text)

    async def info(key: str) -> dict:
        return {"usage_daily": theirs, "usage": 1.0, "limit": 50, "limit_reset": None}

    rec = OpenRouterReconciler(calls, notify, key_fn=lambda: "k", key_info=info)
    theirs = 0.1003
    r = await rec.run()
    assert r is not None and not r.alarming and notes == []
    theirs = 0.25
    r = await rec.run()
    assert r is not None and r.alarming and "0.2500" in notes[0]
    calls.provider_cost_on.assert_awaited_with("openrouter", r.day)


async def test_reconcile_idle_without_key():
    from src.model_router.or_reconcile import OpenRouterReconciler

    calls = MagicMock()
    calls.provider_cost_on = AsyncMock()
    rec = OpenRouterReconciler(calls, None, key_fn=lambda: None)
    assert await rec.run() is None
    calls.provider_cost_on.assert_not_awaited()
