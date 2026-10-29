"""The evaluator's model calls hold no route knowledge (dv-781f phase 2).

Before phase 2 the evaluator wrapped its op schema for Codex
(``_codex_output_schema``) and unwrapped the answer
(``_unwrap_codex_output``). Wrapping, strictifying and unwrapping now live in
``src/model_router/json_contract.py``; the evaluator passes one array-root
``EXTRACTION_SCHEMA`` + a purpose (+ the chat backend as a hint) through
``_invoke_cli`` and gets a bare JSON array back on any route. The dv-0c01
strict-shape regression moved to ``tests/unit/test_json_contract.py``.
"""

import json
import re
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from src.evaluator.service import EXTRACTION_SYSTEM_PROMPT, EvaluatorService
from src.model_router import CallResult, ModelRouter, set_router

REPO = Path(__file__).resolve().parents[2]
SERVICE = REPO / "src" / "evaluator" / "service.py"


@pytest.fixture
def service():
    # These tests exercise the model-call path only; run() deps unused.
    return EvaluatorService(
        memory_repo=None,
        writer=None,
        token_tracker=None,
        db=None,
    )


def test_evaluator_has_no_codex_specific_code_left():
    source = SERVICE.read_text(encoding="utf-8")
    for needle in (
        "openai_codex",
        "CODEX_HARNESS",
        "_codex_output_schema",
        "_unwrap_codex_output",
        "_invoke_codex_cli",
    ):
        assert needle not in source, needle
    # No branch on a harness/backend name either.
    assert not re.search(r"backend\s*==\s*['\"]", source)


def test_extraction_schema_is_one_route_agnostic_array_schema():
    from src.evaluator.service import EXTRACTION_SCHEMA

    assert EXTRACTION_SCHEMA["type"] == "array"
    item = EXTRACTION_SCHEMA["items"]
    assert item["type"] == "object"
    # The caller's real contract: only ``op`` is required; the strict-mode
    # "every key required + null" form is the router's business.
    assert item["required"] == ["op"]
    assert item["properties"]["op"]["enum"] == [
        "add",
        "supersede",
        "invalidate",
        "flag_conflict",
    ]
    # The screen path's fields are part of the contract (dv-7e11).
    assert {"revokes", "conflicts_with_id", "ids", "scope"} <= set(item["properties"])


async def test_claude_extraction_call_unchanged(service):
    """Claude extraction is today's call: Sonnet, the extraction system
    prompt, the prompt as is, no output_format (``json: validate``)."""
    caller = AsyncMock(return_value=CallResult(text="[]"))
    set_router(ModelRouter(plain_callers={"claude": caller}))
    try:
        out = await service._invoke_cli("PROMPT", backend="claude")
        await service._invoke_cli(
            "PROMPT2", backend="claude", purpose="screen_extraction"
        )
        await service._invoke_reconcile_cli("RPROMPT")
    finally:
        set_router(None)

    assert json.loads(out) == []
    extraction, screen, reconcile = (c.args[0] for c in caller.await_args_list)
    assert extraction.purpose == "extraction"
    assert (extraction.harness, extraction.model) == ("claude", "sonnet")
    assert extraction.prompt == "PROMPT"
    assert extraction.system_prompt == EXTRACTION_SYSTEM_PROMPT
    assert extraction.json_schema is None
    assert extraction.timeout_seconds == service._timeout
    assert screen.purpose == "screen_extraction" and screen.json_schema is None
    assert reconcile.purpose == "reconcile" and reconcile.model == "haiku"


async def test_codex_extraction_gets_the_strict_schema_and_a_bare_array(service):
    """On Codex the router (not the evaluator) sends a strict object-root
    schema and unwraps the answer back to the bare array."""
    seen = []

    async def codex(request):
        seen.append(request)
        schema = request.json_schema
        (key,) = schema["required"]
        return CallResult(
            text=json.dumps(
                {key: [{"op": "add", "tier": "general", "content": "x", "label": None}]}
            )
        )

    set_router(ModelRouter(plain_callers={"openai_codex": codex}))
    try:
        out = await service._invoke_cli("PROMPT", backend="openai_codex")
    finally:
        set_router(None)

    (request,) = seen
    assert request.purpose == "extraction"
    assert request.model == "openai-codex/gpt-6-sol"
    assert request.json_schema["type"] == "object"
    assert request.json_schema["additionalProperties"] is False
    assert json.loads(out) == [{"op": "add", "tier": "general", "content": "x"}]
