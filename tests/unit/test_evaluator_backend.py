import json

import pytest

from src.evaluator.service import EvaluatorService


@pytest.fixture
def service():
    # These tests exercise schema/unwrap/dispatch only; run() deps unused.
    return EvaluatorService(
        memory_repo=None,
        writer=None,
        token_tracker=None,
        db=None,
    )


def test_codex_output_schema_is_object_wrapping_array(service):
    # OpenAI structured outputs reject a top-level array schema; the root must
    # be an object. We wrap the op list under "extractions".
    schema = service._codex_output_schema()
    assert schema["type"] == "object"
    assert "extractions" in schema["properties"]
    # OpenAI strict structured outputs require additionalProperties:false on
    # every object node (verified against the live API).
    assert schema["additionalProperties"] is False
    items = schema["properties"]["extractions"]["items"]
    assert items["additionalProperties"] is False
    assert items["properties"]["op"]["enum"] == [
        "add",
        "supersede",
        "invalidate",
        "flag_conflict",
    ]
    # Nullable: the metadata-only ops (invalidate/flag_conflict) carry no tier.
    assert items["properties"]["tier"]["enum"] == ["pinned", "general", None]
    # OpenAI strict mode also requires EVERY property to be listed in
    # `required`; optionality is expressed by the "null" type member alone.
    # Listing only {op, tier, content} made the server answer 400
    # invalid_json_schema ("Missing 'label'") and every Codex evaluator run
    # died with `Codex CLI failed (exit 1):` and an empty stderr (dv-0c01).
    assert set(items["required"]) == set(items["properties"])


def test_unwrap_codex_output_extracts_array(service):
    # The object-wrapped Codex response is unwrapped back to the bare array
    # the validator expects.
    wrapped = json.dumps(
        {"extractions": [{"op": "add", "tier": "general", "content": "x"}]}
    )
    out = service._unwrap_codex_output(wrapped)
    parsed = json.loads(out)
    assert isinstance(parsed, list)
    assert parsed[0]["op"] == "add"


def test_unwrap_codex_output_passes_through_bare_array(service):
    bare = json.dumps([{"op": "add", "tier": "general", "content": "y"}])
    assert json.loads(service._unwrap_codex_output(bare)) == json.loads(bare)


async def test_invoke_cli_dispatches_to_codex(service, monkeypatch):
    calls = {}

    async def fake_codex(prompt):
        calls["codex"] = prompt
        return json.dumps([{"op": "add", "tier": "general", "content": "x"}])

    async def fake_claude(prompt):
        calls["claude"] = prompt
        return "[]"

    monkeypatch.setattr(service, "_invoke_codex_cli", fake_codex)
    monkeypatch.setattr(service, "_invoke_claude_cli", fake_claude)

    out = await service._invoke_cli("PROMPT", backend="openai_codex")
    assert "codex" in calls and "claude" not in calls
    assert json.loads(out)[0]["op"] == "add"


def test_codex_schema_every_op_field_is_nullable_except_op(service):
    """Strict mode forces every field into `required`, so every field that a
    metadata-only op does not carry MUST accept null — otherwise requiring
    them would make `invalidate`/`flag_conflict` unrepresentable."""
    items = service._codex_output_schema()["properties"]["extractions"]["items"]
    for name, spec in items["properties"].items():
        if name == "op":
            assert spec["type"] == "string"
            continue
        assert "null" in spec["type"], name


async def test_invoke_codex_cli_asks_the_router_with_schema_and_model(
    service, monkeypatch
):
    """Extraction on Codex is a plain router call: explicit model, the op
    schema, and the object wrapper stripped from the answer."""
    from unittest.mock import AsyncMock

    from src.model_router import CallResult, ModelRouter, set_router

    caller = AsyncMock(return_value=CallResult(text='{"extractions": []}'))
    set_router(ModelRouter(plain_callers={"openai_codex": caller}))
    try:
        result = await service._invoke_codex_cli("PROMPT")
    finally:
        set_router(None)

    request = caller.await_args.args[0]
    assert request.purpose == "extraction" and request.harness == "openai_codex"
    assert request.model == "openai-codex/gpt-6-sol"
    assert request.json_schema == service._codex_output_schema()
    assert json.loads(result) == []


async def test_invoke_claude_cli_asks_the_router(service):
    from unittest.mock import AsyncMock

    from src.model_router import CallResult, ModelRouter, set_router

    caller = AsyncMock(return_value=CallResult(text="[]"))
    set_router(ModelRouter(plain_callers={"claude": caller}))
    try:
        assert await service._invoke_claude_cli("PROMPT") == "[]"
        await service._invoke_reconcile_cli("PROMPT")
    finally:
        set_router(None)

    extraction, reconcile = (c.args[0] for c in caller.await_args_list)
    assert extraction.purpose == "extraction" and extraction.model == "sonnet"
    assert reconcile.purpose == "reconcile" and reconcile.model == "haiku"
    assert extraction.timeout_seconds == service._timeout
