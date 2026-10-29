"""Same JSON contract on every plain-call route (dv-781f phase 2).

``CallRequest.json_schema`` is the caller's real contract (any root type,
optional keys allowed). The router + ``src/model_router/json_contract.py``
own everything route-specific: wrapping a non-object root, strictifying for
OpenAI-style strict mode, unwrapping, dropping null optional keys,
validating, and one JSON retry. Callers (the evaluator first) pass a schema
and a purpose and get the same canonical text from claude, codex and
openrouter.

Contract pinned here (names from docs/research/dv-781f/analysis.md
"JSON contract (Phase 2)" and "Rows, retries, timeouts"):
- ``src.evaluator.service.EXTRACTION_SCHEMA``: the evaluator's one array-root
  op schema, route-agnostic.
- ``src.model_router.json_contract.native_schema(schema, harness)``: the
  schema an adapter receives in native mode.
- ``CallRequest.deadline_seconds``: bounds the whole attempt chain.
- An invalid-JSON answer's row: ``outcome="error"``, ``error`` starting
  ``"invalid json"``, tokens kept; at most 4 adapter calls per request.
- Built-in extraction on claude is ``json: validate``: no ``output_format``,
  today's call, the router still validates.

The wrapper's property name is not pinned: the fakes read it from the
schema the adapter receives.
"""

import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from src.model_router import (
    CallRequest,
    CallResult,
    ModelCallError,
    ModelRouter,
    complete_text,
    set_router,
)
from src.model_router.harness import HarnessVersions
from src.storage.database import DatabaseManager
from src.storage.model_calls import ModelCallRepository

CODEX_MODEL = "openai-codex/gpt-6-sol"

# A small object-root contract, ask_explain-shaped: one optional key.
VERDICT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["ok", "caution", "danger"]},
        "why": {"type": "string"},
        "note": {"type": "string"},
    },
    "required": ["verdict", "why"],
}
GOOD_VERDICT = {"verdict": "caution", "why": "deletes a file"}


class _Versions(HarnessVersions):
    async def get(self, harness: str) -> str:
        return f"{harness}-9.9"


class _Scripted:
    """A plain-call adapter that answers from a script, one text per call.

    A callable entry gets the request and returns the text, so a fake can
    build a wrapped answer from the schema it was actually sent.
    """

    def __init__(self, *answers: Any, sleep: float = 0.0) -> None:
        self.answers = list(answers)
        self.sleep = sleep
        self.calls: List[CallRequest] = []

    async def __call__(self, request: CallRequest) -> CallResult:
        self.calls.append(request)
        if self.sleep:
            import asyncio

            await asyncio.sleep(self.sleep)
        answer = self.answers[min(len(self.calls), len(self.answers)) - 1]
        if callable(answer):
            answer = answer(request)
        if isinstance(answer, BaseException):
            raise answer
        return CallResult(text=answer, input_tokens=11, output_tokens=7)


def _routes(tmp_path: Path, body: str):
    from src.model_router.routes import load_call_routes

    path = tmp_path / "models.yaml"
    path.write_text(body, encoding="utf-8")
    return load_call_routes(path)


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


def _wrapper_key(schema: Dict[str, Any]) -> str:
    """The single property a non-object root was wrapped under."""
    assert schema["type"] == "object", schema
    assert schema.get("additionalProperties") is False, schema
    (key,) = schema["required"]
    assert set(schema["properties"]) == {key}
    return key


def _assert_strict(node: Any) -> None:
    """OpenAI strict mode: every object closed and every key required."""
    if isinstance(node, dict):
        types = node.get("type")
        if types == "object" or (isinstance(types, list) and "object" in types):
            assert node.get("additionalProperties") is False, node
            assert set(node.get("required", [])) == set(node.get("properties", {}))
        for value in node.values():
            _assert_strict(value)
    elif isinstance(node, list):
        for value in node:
            _assert_strict(value)


# --- scenario 1: array-root evaluator schema, identical JSON on 3 routes -----

# What the model means, as Claude says it (absent optional keys).
OPS = [
    {"op": "add", "tier": "general", "content": "Likes green tea", "label": "tea"},
    {"op": "invalidate", "ids": [3, 4]},
    {
        "op": "supersede",
        "tier": "pinned",
        "content": "Lives in Riga",
        "supersedes_id": 9,
    },
]
_OP_KEYS = [
    "op",
    "tier",
    "content",
    "label",
    "supersedes_id",
    "kind",
    "source_ref",
    "target_id",
    "ids",
    "scope",
    "revokes",
    "conflicts_with_id",
]


def _with_nulls(op: Dict[str, Any]) -> Dict[str, Any]:
    """The same op as a strict-mode model says it: every key, nulls."""
    return {k: op.get(k) for k in _OP_KEYS}


def _claude_answer(request: CallRequest) -> str:
    # Prompt-instructed JSON, fenced, the way Claude extraction answers today.
    return "```json\n" + json.dumps(OPS, indent=2) + "\n```\n"


def _wrapped_answer(request: CallRequest) -> str:
    assert request.json_schema is not None, "native route got no schema"
    key = _wrapper_key(request.json_schema)
    return json.dumps({key: [_with_nulls(op) for op in OPS]})


_EXTRACTION_ON_OPENROUTER = """
purposes:
  extraction:
    harness: openrouter
    models:
      openrouter: z-ai/glm-5.3
"""


async def test_array_root_extraction_schema_gives_identical_json_on_every_route(
    tmp_path,
):
    """The evaluator passes one array-root schema; claude (bare fenced array,
    absent keys), codex and openrouter (wrapped, explicit nulls) answers
    come back as byte-identical canonical text."""
    from src.evaluator.service import EvaluatorService

    service = EvaluatorService(
        memory_repo=None, writer=None, token_tracker=None, db=None
    )
    texts: Dict[str, str] = {}
    seen: Dict[str, CallRequest] = {}
    cases = [
        ("claude", "claude", None, _claude_answer),
        ("openai_codex", "openai_codex", None, _wrapped_answer),
        ("openrouter", "claude", _EXTRACTION_ON_OPENROUTER, _wrapped_answer),
    ]
    for harness, backend, body, answer in cases:
        fake = _Scripted(answer)
        kwargs: Dict[str, Any] = {"plain_callers": {harness: fake}}
        if body:
            kwargs["call_routes"] = _routes(tmp_path, body)
        set_router(ModelRouter(**kwargs))
        try:
            texts[harness] = await service._invoke_cli("PROMPT", backend=backend)
        finally:
            set_router(None)
        (seen[harness],) = fake.calls

    assert texts["claude"] == texts["openai_codex"] == texts["openrouter"]
    assert json.loads(texts["claude"]) == OPS
    # Both native routes got an object root in strict form.
    for harness in ("openai_codex", "openrouter"):
        schema = seen[harness].json_schema
        assert schema is not None and schema["type"] == "object", harness
        _assert_strict(schema)


async def test_array_root_schema_through_the_router_directly():
    """Same contract for any caller, not only the evaluator: an array-root
    schema on codex comes back as the bare array."""
    from src.evaluator.service import EXTRACTION_SCHEMA

    assert EXTRACTION_SCHEMA["type"] == "array"
    fake = _Scripted(_wrapped_answer)
    router = ModelRouter(plain_callers={"openai_codex": fake})

    result = await router.complete(
        CallRequest(
            purpose="extraction",
            backend="openai_codex",
            prompt="p",
            system_prompt="s",
            json_schema=EXTRACTION_SCHEMA,
        )
    )

    assert json.loads(result.text) == OPS


# --- scenario 2: invalid then valid = one retry, 2 rows ----------------------


async def test_invalid_json_then_valid_retries_once_with_two_rows(db):
    fake = _Scripted("Sure! The verdict is caution.", json.dumps(GOOD_VERDICT))
    router = ModelRouter(
        plain_callers={"claude": fake},
        recorder=ModelCallRepository(db),
        versions=_Versions(),
    )

    result = await router.complete(
        CallRequest(
            purpose="consolidation",
            prompt="ORIGINAL PROMPT",
            system_prompt="s",
            json_schema=VERDICT_SCHEMA,
        )
    )

    assert json.loads(result.text) == GOOD_VERDICT
    first, retry = fake.calls
    # The retry is on the same route and still carries the original prompt,
    # with a nudge to answer JSON only.
    assert (retry.harness, retry.model) == (first.harness, first.model)
    assert "ORIGINAL PROMPT" in retry.prompt and retry.prompt != first.prompt
    rows = await _rows(db)
    assert [r["outcome"] for r in rows] == ["error", "ok"]
    assert rows[0]["error"].startswith("invalid json")
    # The bad answer's tokens were spent: they are recorded.
    assert rows[0]["input_tokens"] == 11 and rows[0]["output_tokens"] == 7
    assert all(r["purpose"] == "consolidation" for r in rows)


async def test_schema_violation_counts_as_invalid_json(db):
    """Parseable JSON that breaks the schema (bad enum, missing required) is
    retried like unparseable text."""
    bad = json.dumps({"verdict": "maybe"})
    fake = _Scripted(bad, json.dumps(GOOD_VERDICT))
    router = ModelRouter(
        plain_callers={"claude": fake},
        recorder=ModelCallRepository(db),
        versions=_Versions(),
    )

    result = await router.complete(
        CallRequest(
            purpose="consolidation",
            prompt="p",
            system_prompt="s",
            json_schema=VERDICT_SCHEMA,
        )
    )

    assert json.loads(result.text) == GOOD_VERDICT
    assert len(fake.calls) == 2
    assert [r["outcome"] for r in await _rows(db)] == ["error", "ok"]


# --- scenario 3: invalid twice = ModelCallError; max 4 calls; deadline -------

_FALLBACK = """
purposes:
  consolidation:
    fallback:
      harness: openai_codex
"""


async def test_invalid_json_twice_raises_model_call_error(db):
    fake = _Scripted("not json", "still not json", json.dumps(GOOD_VERDICT))
    router = ModelRouter(
        plain_callers={"claude": fake},
        recorder=ModelCallRepository(db),
        versions=_Versions(),
    )

    with pytest.raises(ModelCallError):
        await router.complete(
            CallRequest(
                purpose="consolidation",
                prompt="p",
                system_prompt="s",
                json_schema=VERDICT_SCHEMA,
            )
        )

    assert len(fake.calls) == 2
    rows = await _rows(db)
    assert [r["outcome"] for r in rows] == ["error", "error"]
    assert all(r["error"].startswith("invalid json") for r in rows)


async def test_invalid_json_with_fallback_is_at_most_four_calls(tmp_path, db):
    """primary, JSON retry, fallback, JSON retry on fallback: 4, never 5."""
    claude = _Scripted("nope")
    codex = _Scripted("nope")
    router = ModelRouter(
        plain_callers={"claude": claude, "openai_codex": codex},
        call_routes=_routes(tmp_path, _FALLBACK),
        recorder=ModelCallRepository(db),
        versions=_Versions(),
    )

    with pytest.raises(ModelCallError):
        await router.complete(
            CallRequest(
                purpose="consolidation",
                prompt="p",
                system_prompt="s",
                json_schema=VERDICT_SCHEMA,
            )
        )

    assert len(claude.calls) == 2 and len(codex.calls) == 2
    rows = await _rows(db)
    assert [r["harness"] for r in rows] == [
        "claude",
        "claude",
        "openai_codex",
        "openai_codex",
    ]
    assert all(r["outcome"] == "error" for r in rows)


async def test_invalid_json_then_fallback_valid_returns_the_fallback_answer(
    tmp_path, db
):
    claude = _Scripted("nope")
    codex = _Scripted(json.dumps(GOOD_VERDICT))
    router = ModelRouter(
        plain_callers={"claude": claude, "openai_codex": codex},
        call_routes=_routes(tmp_path, _FALLBACK),
        recorder=ModelCallRepository(db),
        versions=_Versions(),
    )

    result = await router.complete(
        CallRequest(
            purpose="consolidation",
            prompt="p",
            system_prompt="s",
            json_schema=VERDICT_SCHEMA,
        )
    )

    assert json.loads(result.text) == GOOD_VERDICT
    assert len(claude.calls) == 2 and len(codex.calls) == 1
    assert [r["outcome"] for r in await _rows(db)] == ["error", "error", "ok"]


async def test_deadline_bounds_the_whole_chain(tmp_path):
    """``deadline_seconds`` bounds the chain: each attempt's timeout is at
    most what is left, and no retry/fallback starts with < 5 s left."""
    claude = _Scripted("nope", sleep=1.5)
    codex = _Scripted(json.dumps(GOOD_VERDICT))
    router = ModelRouter(
        plain_callers={"claude": claude, "openai_codex": codex},
        call_routes=_routes(tmp_path, _FALLBACK),
    )

    started = time.monotonic()
    with pytest.raises(ModelCallError):
        await router.complete(
            CallRequest(
                purpose="consolidation",
                prompt="p",
                system_prompt="s",
                json_schema=VERDICT_SCHEMA,
                timeout_seconds=30,
                deadline_seconds=7,
            )
        )
    elapsed = time.monotonic() - started

    # t=0: primary (7 s left); t=1.5: JSON retry (5.5 s left);
    # t=3.0: 4 s left < 5 s, so the fallback never starts.
    assert len(claude.calls) == 2
    assert not codex.calls
    first, retry = claude.calls
    assert first.timeout_seconds <= 7
    assert retry.timeout_seconds <= 5.6
    assert elapsed < 7


async def test_no_deadline_keeps_the_per_call_timeout():
    fake = _Scripted(json.dumps(GOOD_VERDICT))
    router = ModelRouter(plain_callers={"claude": fake})

    await router.complete(
        CallRequest(
            purpose="consolidation",
            prompt="p",
            system_prompt="s",
            json_schema=VERDICT_SCHEMA,
            timeout_seconds=42,
        )
    )

    assert fake.calls[0].timeout_seconds == 42


# --- scenario 4: OpenRouter rejects response_format --------------------------


def _openrouter(monkeypatch, responses: List[Any]):
    """Patch the OpenRouter adapter's httpx client; return the sent bodies.

    ``responses``: (status, json) per post, in order.
    """
    import httpx

    import src.security.keystore as keystore
    from src.model_router import openrouter_plain

    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    store = SimpleNamespace(
        env=lambda service: (
            {"OPENROUTER_API_KEY": "sk-or-store"} if service == "openrouter" else {}
        )
    )
    monkeypatch.setattr(keystore, "default_keystore", lambda: store)
    bodies: List[Dict[str, Any]] = []

    class _Client:
        def __init__(self, *a: Any, **kw: Any) -> None:
            pass

        async def __aenter__(self) -> "_Client":
            return self

        async def __aexit__(self, *a: Any) -> None:
            return None

        async def post(self, url: str, **kw: Any) -> httpx.Response:
            bodies.append(json.loads(json.dumps(kw["json"])))
            status, payload = responses[min(len(bodies), len(responses)) - 1]
            return httpx.Response(
                status, json=payload, request=httpx.Request("POST", url)
            )

    monkeypatch.setattr(openrouter_plain.httpx, "AsyncClient", _Client)
    return bodies


def _completion(content: str) -> Dict[str, Any]:
    return {
        "model": "z-ai/glm-5.3",
        "choices": [{"message": {"content": content}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 10},
    }


_REJECTED = (
    400,
    {
        "error": {
            "code": 400,
            "message": "Provider returned error: response_format "
            "json_schema is not supported for this model",
        }
    },
)


def _or_router(db=None) -> ModelRouter:
    from src.model_router.openrouter_plain import OpenRouterPlainCaller

    kwargs: Dict[str, Any] = {"plain_callers": {"openrouter": OpenRouterPlainCaller()}}
    if db is not None:
        kwargs.update(recorder=ModelCallRepository(db), versions=_Versions())
    return ModelRouter(**kwargs)


def _or_request(**kw: Any) -> CallRequest:
    return CallRequest(
        purpose="consolidation",
        harness="openrouter",
        model="z-ai/glm-5.3",
        prompt="p",
        system_prompt="s",
        json_schema=VERDICT_SCHEMA,
        **kw,
    )


async def test_openrouter_sends_strict_response_format(monkeypatch):
    bodies = _openrouter(monkeypatch, [(200, _completion(json.dumps(GOOD_VERDICT)))])

    result = await _or_router().complete(_or_request())

    assert json.loads(result.text) == GOOD_VERDICT
    (body,) = bodies
    fmt = body["response_format"]
    assert fmt["type"] == "json_schema"
    assert fmt["json_schema"]["strict"] is True
    assert fmt["json_schema"].get("name")
    _assert_strict(fmt["json_schema"]["schema"])


async def test_openrouter_rejecting_response_format_resends_once_without_it(
    monkeypatch, db
):
    """400 naming response_format: resent once without it, inside one adapter
    call (one row); the answer is still validated and canonicalised."""
    sloppy = "```json\n" + json.dumps({**GOOD_VERDICT, "note": None}) + "\n```"
    bodies = _openrouter(monkeypatch, [_REJECTED, (200, _completion(sloppy))])

    result = await _or_router(db).complete(_or_request())

    assert len(bodies) == 2
    assert "response_format" in bodies[0]
    assert "response_format" not in bodies[1]
    # Without response_format the model is told the schema in the messages.
    assert "verdict" in json.dumps(bodies[1]["messages"])
    # Fence stripped, null optional key dropped: the canonical answer.
    assert json.loads(result.text) == GOOD_VERDICT
    (row,) = await _rows(db)
    assert row["outcome"] == "ok" and row["harness"] == "openrouter"


async def test_openrouter_resent_answer_is_still_validated(monkeypatch, db):
    """The schema is enforced by the router after the resend: an answer that
    breaks it is retried once, then raises."""
    bad = _completion(json.dumps({"verdict": "maybe"}))
    _openrouter(monkeypatch, [_REJECTED, (200, bad)])

    with pytest.raises(ModelCallError):
        await _or_router(db).complete(_or_request())

    rows = await _rows(db)
    assert [r["outcome"] for r in rows] == ["error", "error"]
    assert all(r["error"].startswith("invalid json") for r in rows)


async def test_openrouter_other_400_is_not_resent(monkeypatch):
    """Only a rejection of the structured-output parameter is resent; any
    other client error is a plain failure."""
    bodies = _openrouter(
        monkeypatch,
        [(400, {"error": {"code": 400, "message": "context length exceeded"}})],
    )

    with pytest.raises(ModelCallError):
        await _or_router().complete(_or_request())

    assert len(bodies) == 1


# --- native_schema: wrap + strictify (dv-0c01 regression) --------------------

# Today's hand-written Codex op item (service.py ``_codex_output_schema``
# before phase 2): the strict form the generic rule must still produce.
_TODAYS_CODEX_ITEM_PROPERTIES = {
    "op": {
        "type": "string",
        "enum": ["add", "supersede", "invalidate", "flag_conflict"],
    },
    "tier": {"type": ["string", "null"], "enum": ["pinned", "general", None]},
    "content": {"type": ["string", "null"]},
    "label": {"type": ["string", "null"]},
    "supersedes_id": {"type": ["integer", "null"]},
    "kind": {
        "type": ["string", "null"],
        "enum": ["user_fact", "conversation_result", None],
    },
    "source_ref": {"type": ["string", "null"]},
    "target_id": {"type": ["integer", "null"]},
    "ids": {"type": ["array", "null"], "items": {"type": "integer"}},
    "scope": {"type": ["string", "null"], "enum": ["private", "family", None]},
    "revokes": {"type": ["boolean", "null"]},
    "conflicts_with_id": {"type": ["integer", "null"]},
}


def _norm(node: Any) -> Any:
    """Order-insensitive view of a schema: type unions, enums, required."""
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            if k in ("type", "enum", "required") and isinstance(v, list):
                out[k] = sorted(v, key=repr)
            else:
                out[k] = _norm(v)
        return out
    if isinstance(node, list):
        return [_norm(v) for v in node]
    return node


@pytest.mark.parametrize("harness", ["openai_codex", "openrouter"])
def test_strict_extraction_schema_matches_todays_codex_shape(harness):
    from src.evaluator.service import EXTRACTION_SCHEMA
    from src.model_router.json_contract import native_schema

    strict = native_schema(EXTRACTION_SCHEMA, harness)

    key = _wrapper_key(strict)
    items = strict["properties"][key]["items"]
    assert items["additionalProperties"] is False
    assert set(items["required"]) == set(_TODAYS_CODEX_ITEM_PROPERTIES)
    assert _norm(items["properties"]) == _norm(_TODAYS_CODEX_ITEM_PROPERTIES)


def test_native_schema_wraps_for_claude_without_strictifying():
    from src.evaluator.service import EXTRACTION_SCHEMA
    from src.model_router.json_contract import native_schema

    wrapped = native_schema(EXTRACTION_SCHEMA, "claude")

    key = _wrapper_key(wrapped)
    # Claude keeps the caller's optional keys optional.
    assert wrapped["properties"][key] == EXTRACTION_SCHEMA


def test_native_schema_leaves_the_callers_schema_untouched():
    from src.evaluator.service import EXTRACTION_SCHEMA
    from src.model_router.json_contract import native_schema

    before = json.dumps(EXTRACTION_SCHEMA, sort_keys=True)
    native_schema(EXTRACTION_SCHEMA, "openai_codex")
    native_schema(VERDICT_SCHEMA, "openrouter")
    assert json.dumps(EXTRACTION_SCHEMA, sort_keys=True) == before


# --- added: a schema-less plain call is not touched --------------------------


async def test_schemaless_call_is_passed_through_untouched():
    """No json_schema = prose (consolidation, onboarding): no parsing, no
    retry, the text exactly as the model wrote it."""
    prose = "```\nNot JSON at all.\n```"
    fake = _Scripted(prose)
    set_router(ModelRouter(plain_callers={"claude": fake}))
    try:
        text = await complete_text("p", purpose="consolidation", system_prompt="s")
    finally:
        set_router(None)

    assert text == prose
    assert len(fake.calls) == 1
    assert fake.calls[0].json_schema is None


# --- added: an outage is not a JSON problem ----------------------------------


async def test_adapter_error_is_not_json_retried(tmp_path):
    """The JSON retry is for invalid answers only; an error goes straight to
    the fallback (no second call on the failing route)."""
    claude = _Scripted(ModelCallError("claude exited 1"))
    codex = _Scripted(json.dumps(GOOD_VERDICT))
    router = ModelRouter(
        plain_callers={"claude": claude, "openai_codex": codex},
        call_routes=_routes(tmp_path, _FALLBACK),
    )

    result = await router.complete(
        CallRequest(
            purpose="consolidation",
            prompt="p",
            system_prompt="s",
            json_schema=VERDICT_SCHEMA,
        )
    )

    assert json.loads(result.text) == GOOD_VERDICT
    assert len(claude.calls) == 1 and len(codex.calls) == 1
