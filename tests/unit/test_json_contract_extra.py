"""JSON-contract details beyond the pinned phase-2 scenarios (dv-781f)."""

import json
from pathlib import Path
from typing import List

import pytest

from src.model_router import CallRequest, CallResult, ModelRouter
from src.model_router.json_contract import (
    InvalidJSON,
    canonical,
    native_schema,
    unsupported_keywords,
)
from src.model_router.routes import CallRoutes, load_call_routes


class _Fake:
    def __init__(self, *texts: str) -> None:
        self.texts = list(texts)
        self.calls: List[CallRequest] = []

    async def __call__(self, request: CallRequest) -> CallResult:
        self.calls.append(request)
        return CallResult(text=self.texts[min(len(self.calls), len(self.texts)) - 1])


def _repo_schemas():
    from src.evaluator.service import EXTRACTION_SCHEMA
    from src.model_router import llm_decide
    from src.model_router.types import Choice, DecideRequest, Noul, Score
    from src.security.ask_explain import SCHEMA

    decide = DecideRequest(
        purpose="screen",
        state="s",
        questions={
            "c": Choice("i", {"a": None, "b": None}),
            "s": Score("i", ("low", "high")),
            "n": Noul("i"),
        },
    )
    return {
        "ask_explain": SCHEMA,
        "extraction": EXTRACTION_SCHEMA,
        "decide": llm_decide.json_schema(decide),
    }


@pytest.mark.parametrize("name", ["ask_explain", "extraction", "decide"])
def test_every_repo_schema_is_inside_the_validator_subset(name):
    schema = _repo_schemas()[name]
    assert unsupported_keywords(schema) == []
    for harness in ("claude", "openai_codex", "openrouter"):
        assert unsupported_keywords(native_schema(schema, harness)) == []


def test_object_root_on_claude_is_sent_as_is():
    from src.security.ask_explain import SCHEMA

    assert native_schema(SCHEMA, "claude") == SCHEMA


@pytest.mark.parametrize(
    "text",
    [
        '  {"verdict": "ok", "why": "x"}\n',
        '```json\n{"why": "x", "verdict": "ok"}\n```',
        '```\n{"verdict": "ok", "why": "x", "note": null}\n```\n',
    ],
)
def test_fenced_reordered_or_null_padded_answers_are_one_canonical_text(text):
    schema = {
        "type": "object",
        "properties": {
            "verdict": {"type": "string"},
            "why": {"type": "string"},
            "note": {"type": "string"},
        },
        "required": ["verdict", "why"],
    }
    assert canonical(text, schema) == '{"verdict": "ok", "why": "x"}'


def test_validator_rejects_bool_as_integer_and_out_of_range():
    schema = {"type": "integer", "minimum": 0, "maximum": 2}
    for bad in ("true", "3", "-1", '"1"'):
        with pytest.raises(InvalidJSON):
            canonical(bad, schema)
    assert canonical("2", schema) == "2"


async def test_validate_route_keeps_items_to_the_caller():
    """Claude extraction is prompt-only JSON (``json: validate``): the
    prompt offers tier "core", which the strict schema has no room for; the
    op still reaches the evaluator's own lenient validator, no retry."""
    from src.evaluator.service import EXTRACTION_SCHEMA

    ops = [{"op": "add", "tier": "core", "content": "Is 41"}]
    fake = _Fake(json.dumps(ops))
    router = ModelRouter(plain_callers={"claude": fake})

    result = await router.complete(
        CallRequest(
            purpose="extraction",
            backend="claude",
            prompt="p",
            system_prompt="s",
            json_schema=EXTRACTION_SCHEMA,
        )
    )

    assert json.loads(result.text) == ops
    assert len(fake.calls) == 1 and fake.calls[0].json_schema is None


async def test_validate_route_still_retries_a_non_json_answer():
    from src.evaluator.service import EXTRACTION_SCHEMA

    fake = _Fake("I found nothing new.", "[]")
    router = ModelRouter(plain_callers={"claude": fake})

    result = await router.complete(
        CallRequest(
            purpose="extraction",
            backend="claude",
            prompt="p",
            system_prompt="s",
            json_schema=EXTRACTION_SCHEMA,
        )
    )

    assert result.text == "[]"
    # Prompt-only route: the retry tells the model the schema.
    assert '"type": "array"' in fake.calls[1].prompt


def test_json_mode_in_models_yaml(tmp_path: Path):
    path = tmp_path / "models.yaml"
    path.write_text(
        "purposes:\n  extraction: {json: {claude: native}}\n"
        "  consolidation: {json: {openrouter: validate}}\n",
        encoding="utf-8",
    )
    routes = load_call_routes(path)
    assert routes.resolve("extraction", backend="claude").json == "native"
    assert (
        routes.resolve("consolidation", harness="openrouter", model="m").json
        == "validate"
    )
    built_in = CallRoutes()
    assert built_in.resolve("extraction", backend="claude").json == "validate"
    assert built_in.resolve("extraction", backend="openai_codex").json == "native"
    assert built_in.resolve("ask_explain").json == "native"


@pytest.mark.parametrize(
    "body",
    [
        "purposes:\n  extraction: {json: {claude: strict}}\n",
        "purposes:\n  extraction: {json: {nope: native}}\n",
        "purposes:\n  extraction: {json: native}\n",
    ],
)
def test_bad_json_mode_is_rejected(tmp_path: Path, body: str):
    path = tmp_path / "models.yaml"
    path.write_text(body, encoding="utf-8")
    with pytest.raises(ValueError):
        load_call_routes(path)


async def test_openrouter_no_endpoints_for_parameters_is_resent(monkeypatch):
    import httpx

    from src.model_router import openrouter_plain

    monkeypatch.setattr(openrouter_plain, "openrouter_key", lambda: "sk")
    bodies = []
    answers = [
        (
            404,
            {
                "error": {
                    "message": "No endpoints found that can handle the "
                    "requested parameters."
                }
            },
        ),
        (200, {"choices": [{"message": {"content": "[1, 2]"}}], "usage": {}}),
    ]

    class _Client:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def post(self, url, **kw):
            bodies.append(kw["json"])
            status, payload = answers[len(bodies) - 1]
            return httpx.Response(
                status, json=payload, request=httpx.Request("POST", url)
            )

    monkeypatch.setattr(openrouter_plain.httpx, "AsyncClient", _Client)
    router = ModelRouter(
        plain_callers={"openrouter": openrouter_plain.OpenRouterPlainCaller()}
    )

    result = await router.complete(
        CallRequest(
            purpose="consolidation",
            harness="openrouter",
            model="qwen/q",
            prompt="p",
            system_prompt="s",
            json_schema={"type": "array", "items": {"type": "integer"}},
        )
    )

    assert result.text == "[1, 2]"
    assert len(bodies) == 2 and "response_format" not in bodies[1]
