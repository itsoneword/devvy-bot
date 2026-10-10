"""Unit tests for the Tier-2 judge providers (dv-300d)."""

from unittest.mock import AsyncMock

import httpx
import pytest

from src.llm_analytics.judges import (
    ClaudeCliJudge,
    OpenRouterJudge,
    build_judge,
)
from src.model_router import CallResult, ModelCallError, ModelRouter, set_router


@pytest.fixture(autouse=True)
def _openrouter_key(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")


@pytest.fixture
def plain_caller():
    """A router whose plain-call adapters are one recording fake."""
    caller = AsyncMock(return_value=CallResult(text="[]", cost_usd=0.01))
    set_router(ModelRouter(plain_callers={"claude": caller, "openrouter": caller}))
    yield caller
    set_router(None)


class TestClaudeCliJudge:
    async def test_asks_the_router_and_keeps_the_cost(self, plain_caller) -> None:
        judge = ClaudeCliJudge(model="sonnet", timeout_seconds=7)
        result = await judge.complete("prompt")

        request = plain_caller.await_args.args[0]
        assert request.purpose == "scorer" and request.harness == "claude"
        assert request.model == "sonnet" and request.timeout_seconds == 7
        assert request.prompt == "prompt"
        assert result == "[]"
        assert judge.last_cost_usd == 0.01

    async def test_a_failed_call_raises(self, plain_caller) -> None:
        plain_caller.side_effect = ModelCallError("boom")
        with pytest.raises(RuntimeError, match="boom"):
            await ClaudeCliJudge().complete("prompt")

    async def test_resolve_returns_model_name_without_a_call(self) -> None:
        judge = ClaudeCliJudge(model="opus")
        assert await judge.resolve() == "claude-cli:opus"


class TestBuildJudge:
    def test_claude_cli_default_model_from_env(self, monkeypatch) -> None:
        monkeypatch.delenv("SCORER_CLI_MODEL", raising=False)
        judge = build_judge("claude_cli")
        assert isinstance(judge, ClaudeCliJudge)

    def test_unknown_provider_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown"):
            build_judge("nonexistent")

    def test_openrouter_requires_api_key(self, monkeypatch) -> None:
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
            build_judge("openrouter")


_MODELS_PAYLOAD = {
    "data": [
        {
            "id": "qwen/qwen3-235b-a22b-instruct",
            "pricing": {"prompt": "0.0000002", "completion": "0.0000008"},
        },
        {
            "id": "qwen/qwen2.5-7b-instruct",
            "pricing": {"prompt": "0.00000005", "completion": "0.0000001"},
        },
        {
            "id": "qwen/qwen2.5-vl-72b-instruct",
            "pricing": {"prompt": "0.0000003", "completion": "0.0000009"},
        },
        {"id": "qwen/qwen3-32b:free", "pricing": {"prompt": "0", "completion": "0"}},
        {
            "id": "openai/gpt-5",
            "pricing": {"prompt": "0.000003", "completion": "0.000012"},
        },
    ]
}


class TestOpenRouterJudge:
    async def test_pick_best_value_qwen_excludes_vl_and_free_prefers_largest(
        self, monkeypatch
    ) -> None:
        async def fake_get(self, url, *args, **kwargs):
            assert "models" in url
            return httpx.Response(
                200, json=_MODELS_PAYLOAD, request=httpx.Request("GET", url)
            )

        monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

        judge = OpenRouterJudge()
        model = await judge.pick_best_value_qwen()
        # qwen3-235b is the largest non-vl, non-free candidate.
        assert model == "qwen/qwen3-235b-a22b-instruct"

    async def test_explicit_model_skips_resolution(self, monkeypatch) -> None:
        async def fail_get(self, *args, **kwargs):
            raise AssertionError("should not hit the network when model is explicit")

        monkeypatch.setattr(httpx.AsyncClient, "get", fail_get)
        judge = OpenRouterJudge(model="qwen/qwen3-30b-a3b-instruct")
        assert await judge.resolve() == "qwen/qwen3-30b-a3b-instruct"

    async def test_complete_posts_and_parses_content(self, monkeypatch) -> None:
        captured = {}

        async def fake_post(self, url, *args, **kwargs):
            captured["url"] = url
            captured["json"] = kwargs.get("json")
            captured["headers"] = kwargs.get("headers")
            body = {
                "choices": [{"message": {"content": '{"criteria": {}}'}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 50},
            }
            return httpx.Response(200, json=body, request=httpx.Request("POST", url))

        monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
        monkeypatch.setenv("OPENROUTER_API_KEY", "secret-key")
        set_router(None)  # the default router, with the real OpenRouter adapter

        judge = OpenRouterJudge(model="qwen/qwen3-30b-a3b-instruct")
        judge._prompt_price, judge._completion_price = 0.001, 0.002
        result = await judge.complete("judge this")

        assert result == '{"criteria": {}}'
        assert captured["headers"]["Authorization"] == "Bearer secret-key"
        assert captured["json"]["model"] == "qwen/qwen3-30b-a3b-instruct"
        assert captured["json"]["temperature"] == 0
        assert captured["json"]["messages"][0]["role"] == "system"
        assert judge.last_cost_usd == pytest.approx(100 * 0.001 + 50 * 0.002)

    async def test_no_qwen_models_raises(self, monkeypatch) -> None:
        async def fake_get(self, url, *args, **kwargs):
            return httpx.Response(
                200, json={"data": []}, request=httpx.Request("GET", url)
            )

        monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
        judge = OpenRouterJudge()
        with pytest.raises(RuntimeError, match="no Qwen"):
            await judge.pick_best_value_qwen()
