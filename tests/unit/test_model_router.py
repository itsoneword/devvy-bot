"""The model router records every call it makes (dv-11a5)."""

from pathlib import Path
from typing import Any, List
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.claude.sdk_integration import ClaudeResponse
from src.model_router import (
    CallRequest,
    CallResult,
    ModelCallError,
    ModelCallTimeout,
    ModelRouter,
    complete_text,
    set_router,
)
from src.model_router.claude_plain import lean_options
from src.model_router.harness import CLAUDE_ACCOUNT_ENV, HarnessVersions
from src.storage.database import DatabaseManager
from src.storage.model_calls import ModelCallRepository


class _Versions(HarnessVersions):
    async def get(self, harness: str) -> str:
        return f"{harness}-9.9"


def _backend(backend_id: str = "claude", **response: Any) -> MagicMock:
    backend = MagicMock()
    backend.backend_id = backend_id
    backend.default_model.return_value = "sonnet"
    fields = dict(content="hi", session_id="s1", cost=0.5, duration_ms=10, num_turns=1)
    fields.update(response)
    backend.run_command = AsyncMock(return_value=ClaudeResponse(**fields))
    return backend


def _request(**overrides: Any) -> CallRequest:
    fields = dict(purpose="consolidation", prompt="p", model="haiku", system_prompt="s")
    fields.update(overrides)
    return CallRequest(**fields)


@pytest.fixture
async def db(tmp_path):
    manager = DatabaseManager(f"sqlite:///{tmp_path / 'test.db'}")
    await manager.initialize()
    yield manager
    await manager.close()


async def _rows(db: DatabaseManager) -> List[dict]:
    async with db.get_connection() as conn:
        cur = await conn.execute("SELECT * FROM model_calls ORDER BY id")
        return [dict(r) for r in await cur.fetchall()]


def _router(db: DatabaseManager, **kwargs: Any) -> ModelRouter:
    return ModelRouter(recorder=ModelCallRepository(db), versions=_Versions(), **kwargs)


async def test_turn_passes_kwargs_through_and_records(db):
    backend = _backend(input_tokens=7, output_tokens=3)
    router = _router(
        db, backends={"claude": backend}, profile_resolver=lambda p: "devops"
    )

    response = await router.run_turn(
        "claude",
        purpose="chat",
        prompt="hello",
        working_directory=Path("/w"),
        user_id=42,
        model="opus",
    )

    assert response.content == "hi"
    backend.run_command.assert_awaited_once_with(
        prompt="hello", working_directory=Path("/w"), user_id=42, model="opus"
    )
    (row,) = await _rows(db)
    assert row["purpose"] == "chat" and row["kind"] == "turn"
    assert row["harness"] == "claude" and row["harness_version"] == "claude-9.9"
    assert row["provider"] == "anthropic" and row["model"] == "opus"
    assert row["profile"] == "devops" and row["user_id"] == 42
    assert row["session_id"] == "s1" and row["cost_usd"] == 0.5
    assert row["input_tokens"] == 7 and row["output_tokens"] == 3
    assert row["outcome"] == "ok" and row["attended"] == 1


async def test_turn_accepts_a_backend_instance_and_uses_its_default_model(db):
    backend = _backend("openai_codex", cost=None)
    router = _router(db)

    await router.run_turn(backend, purpose="chat", prompt="x", user_id=1)

    (row,) = await _rows(db)
    assert row["harness"] == "openai_codex" and row["provider"] == "openai"
    assert row["model"] == "sonnet" and row["cost_usd"] is None


async def test_failed_turn_is_recorded_and_reraised(db):
    backend = _backend()
    backend.run_command = AsyncMock(side_effect=RuntimeError("boom"))
    router = _router(db, backends={"claude": backend})

    with pytest.raises(RuntimeError, match="boom"):
        await router.run_turn("claude", purpose="scheduled", attended=False, prompt="x")

    (row,) = await _rows(db)
    assert row["outcome"] == "error" and "boom" in row["error"]
    assert row["attended"] == 0


async def test_plain_call_records_what_the_adapter_reports(db):
    caller = AsyncMock(
        return_value=CallResult(
            text="[]", model="claude-haiku-x", harness_version="2.0", cost_usd=0.01
        )
    )
    router = _router(db, plain_callers={"claude": caller})

    result = await router.complete(_request())

    assert result.text == "[]"
    (row,) = await _rows(db)
    assert row["kind"] == "plain" and row["purpose"] == "consolidation"
    assert row["model"] == "claude-haiku-x" and row["harness_version"] == "2.0"
    assert row["outcome"] == "ok" and row["attended"] == 0


async def test_plain_call_timeout_is_recorded_as_timeout(db):
    caller = AsyncMock(side_effect=ModelCallTimeout("timeout after 1s (claude)"))
    router = _router(db, plain_callers={"claude": caller})

    with pytest.raises(ModelCallTimeout):
        await router.complete(_request())

    (row,) = await _rows(db)
    assert row["outcome"] == "timeout" and row["model"] == "haiku"


async def test_unknown_purpose_and_missing_adapter_are_refused(db):
    router = _router(db, plain_callers={})
    with pytest.raises(ValueError):
        await router.complete(_request(purpose="misc"))
    with pytest.raises(ModelCallError):
        await router.complete(_request())
    with pytest.raises(ValueError):
        await router.run_turn(_backend(), purpose="misc", prompt="x")


async def test_a_failed_record_write_does_not_fail_the_call():
    recorder = MagicMock()
    recorder.record = AsyncMock(side_effect=RuntimeError("db down"))
    router = ModelRouter(recorder=recorder, versions=_Versions())

    response = await router.run_turn(_backend(), purpose="chat", prompt="x")

    assert response.content == "hi"


async def test_complete_text_uses_the_registered_router():
    caller = AsyncMock(return_value=CallResult(text="ok"))
    set_router(ModelRouter(plain_callers={"claude": caller}))
    try:
        text = await complete_text(
            "p", purpose="fact_quality", model="haiku", system_prompt="s", timeout=5
        )
    finally:
        set_router(None)

    assert text == "ok"
    request = caller.await_args.args[0]
    assert request.purpose == "fact_quality" and request.timeout_seconds == 5


def test_lean_options_strip_the_harness_and_set_account_separation():
    options = lean_options(_request(), cli_path="/bin/claude")

    assert options.tools == [] and options.setting_sources == []
    assert options.strict_mcp_config is True
    assert options.permission_mode == "default"
    assert options.cli_path == "/bin/claude"
    assert options.extra_args == {"disable-slash-commands": None}
    assert options.env == CLAUDE_ACCOUNT_ENV
    assert options.env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"
    assert options.model == "haiku" and options.system_prompt == "s"


# --- dv-9133: an image call on a harness that cannot take images -------------


def test_call_request_carries_images_and_defaults_to_empty():
    assert _request().images == ()
    assert _request(images=("a.png", "b.png")).images == ("a.png", "b.png")


async def test_claude_plain_call_with_images_fails_loudly_and_is_recorded(
    db, monkeypatch, tmp_path
):
    """Silently dropping the images would let a critic judge renders it never
    saw: the Claude adapter refuses, before it starts the CLI."""
    from src.model_router import claude_plain

    def no_client(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the CLI must not start for an image call")

    monkeypatch.setattr(claude_plain, "ClaudeSDKClient", no_client)
    image = tmp_path / "shot.png"
    image.write_bytes(b"\x89PNG")
    router = _router(db, plain_callers={"claude": claude_plain.ClaudePlainCaller()})

    with pytest.raises(ModelCallError, match="(?i)image"):
        await router.complete(_request(images=(str(image),)))

    (row,) = await _rows(db)
    assert row["kind"] == "plain" and row["harness"] == "claude"
    assert row["outcome"] == "error" and "image" in row["error"].lower()


async def test_openrouter_plain_call_with_images_fails_loudly_and_is_recorded(
    db, monkeypatch, tmp_path
):
    from src.model_router import openrouter_plain

    class _NoHttp:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            raise AssertionError("no HTTP request for an image call")

    monkeypatch.setattr(openrouter_plain, "openrouter_key", lambda: "sk-test")
    monkeypatch.setattr(openrouter_plain.httpx, "AsyncClient", _NoHttp)
    image = tmp_path / "shot.png"
    image.write_bytes(b"\x89PNG")
    router = _router(
        db, plain_callers={"openrouter": openrouter_plain.OpenRouterPlainCaller()}
    )

    with pytest.raises(ModelCallError, match="(?i)image"):
        await router.complete(
            _request(harness="openrouter", model="z-ai/glm-5.3", images=(str(image),))
        )

    (row,) = await _rows(db)
    assert row["harness"] == "openrouter"
    assert row["outcome"] == "error" and "image" in row["error"].lower()
