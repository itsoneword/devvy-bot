"""The router's typed ``decide`` call: Jev first, LLM fallback (dv-7e11)."""

import json
from typing import Any, Dict, List
from unittest.mock import AsyncMock

import httpx
import pytest

from src.model_router import (
    CallResult,
    Choice,
    DecideRequest,
    ModelCallError,
    ModelRouter,
    Noul,
    Score,
    typesafe_decide,
)
from src.model_router.harness import HarnessVersions
from src.model_router.typesafe_decide import TypesafeDecider
from src.storage.database import DatabaseManager
from src.storage.model_calls import ModelCallRepository

QUESTIONS = {
    "rel": Choice(
        instructions="How does the new fact relate to the known one?",
        criteria={"new": "unrelated", "duplicate": "same", "contradicts": None},
    ),
    "imp": Score(instructions="How important?", levels=("low", "mid", "high")),
    "dur": Noul(instructions="Is it durable?"),
}

JEV_REPLY = {
    "model": "jev-1.13.0",
    "answers": {
        "rel": {
            "type": "choice",
            "choice": "contradicts",
            "confidence": 0.9,
            "probabilities": {"new": 0.05, "duplicate": 0.05, "contradicts": 0.9},
        },
        "imp": {
            "type": "score",
            "score": 1.8,
            "confidence": 0.7,
            "legend": {"0": "low", "1": "mid", "2": "high"},
            "probabilities": {"0": 0.01, "1": 0.18, "2": 0.81},
        },
        "dur": {"type": "noul", "noul": 0.7},
    },
    "usage": {"input_tokens": 1_000_000, "output_tokens": 72},
}


class _Versions(HarnessVersions):
    async def get(self, harness: str) -> None:
        return None


def _request(**overrides: Any) -> DecideRequest:
    fields: Dict[str, Any] = dict(
        purpose="reconcile",
        state={"known": "lives in Lisbon", "new": "moved to Porto"},
        questions=QUESTIONS,
    )
    fields.update(overrides)
    return DecideRequest(**fields)


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


def _mock_http(monkeypatch, handler) -> List[httpx.Request]:
    seen: List[httpx.Request] = []
    real = httpx.AsyncClient

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    def client(**kwargs: Any) -> httpx.AsyncClient:
        return real(transport=httpx.MockTransport(wrapped), **kwargs)

    monkeypatch.setattr(typesafe_decide.httpx, "AsyncClient", client)
    return seen


def _fallback_caller(reply: Dict[str, Any]) -> AsyncMock:
    return AsyncMock(
        return_value=CallResult(
            text=json.dumps(reply), model="claude-haiku-x", input_tokens=50
        )
    )


def _router(db: DatabaseManager, claude: AsyncMock) -> ModelRouter:
    return ModelRouter(
        plain_callers={"claude": claude},
        recorder=ModelCallRepository(db),
        versions=_Versions(),
        decider=TypesafeDecider(),
    )


async def test_jev_request_and_answers_are_mapped(db, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "k-123")
    seen = _mock_http(monkeypatch, lambda r: httpx.Response(200, json=JEV_REPLY))
    claude = _fallback_caller({})

    result = await _router(db, claude).decide(_request())

    (sent,) = seen
    assert str(sent.url) == "https://api.typesafe.ai/v1/systemone"
    assert sent.headers["Authorization"] == "Bearer k-123"
    body = json.loads(sent.content)
    assert body["model"] == "jev-latest"
    assert body["state"] == {"known": "lives in Lisbon", "new": "moved to Porto"}
    assert body["questions"] == {
        "rel": {
            "type": "choice",
            "instructions": "How does the new fact relate to the known one?",
            "criteria": {"new": "unrelated", "duplicate": "same", "contradicts": None},
        },
        "imp": {
            "type": "score",
            "instructions": "How important?",
            "criteria": ["low", "mid", "high"],
        },
        "dur": {"type": "noul", "instructions": "Is it durable?"},
    }

    assert result.harness == "typesafe" and result.model == "jev-1.13.0"
    assert result.fallback_reason is None
    rel, imp, dur = (result.answers[k] for k in ("rel", "imp", "dur"))
    assert rel.kind == "choice" and rel.value == "contradicts"
    assert rel.confidence == 0.9 and rel.probabilities["contradicts"] == 0.9
    assert imp.kind == "score" and imp.value == 1.8 and imp.probabilities["2"] == 0.81
    assert dur.kind == "noul" and dur.value == 0.7 and dur.confidence is None
    claude.assert_not_awaited()

    (row,) = await _rows(db)
    assert row["kind"] == "decide" and row["purpose"] == "reconcile"
    assert row["harness"] == "typesafe" and row["provider"] == "typesafe"
    assert row["model"] == "jev-1.13.0" and row["outcome"] == "ok"
    assert row["input_tokens"] == 1_000_000 and row["output_tokens"] == 72
    assert row["cost_usd"] == pytest.approx(0.042)


async def test_missing_key_falls_back_to_llm_without_calling_jev(db, monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    seen = _mock_http(monkeypatch, lambda r: httpx.Response(200, json=JEV_REPLY))
    claude = _fallback_caller({"rel": "duplicate", "imp": 2, "dur": False})

    result = await _router(db, claude).decide(_request())

    assert seen == []
    assert result.harness == "claude" and result.model == "claude-haiku-x"
    assert "TYPESAFE_API_KEY" in (result.fallback_reason or "")
    assert result.answers["rel"].value == "duplicate"
    assert result.answers["imp"].value == 2.0
    assert result.answers["dur"].value == 0.0
    assert all(
        a.confidence is None and a.probabilities is None
        for a in result.answers.values()
    )

    call = claude.await_args.args[0]
    assert call.purpose == "reconcile" and call.model == "haiku"
    assert call.json_schema["properties"]["rel"]["enum"] == [
        "new",
        "duplicate",
        "contradicts",
    ]
    assert call.json_schema["properties"]["imp"]["maximum"] == 2
    assert call.json_schema["properties"]["dur"] == {"type": "boolean"}
    assert "Lisbon" in call.prompt and "2: high" in call.prompt

    (row,) = await _rows(db)  # only the LLM leg; no Jev attempt
    assert row["kind"] == "plain" and row["harness"] == "claude"


async def test_jev_http_error_is_recorded_then_falls_back(db, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "k")
    _mock_http(monkeypatch, lambda r: httpx.Response(529, text="overloaded"))
    claude = _fallback_caller({"rel": "new", "imp": 0, "dur": True})

    result = await _router(db, claude).decide(_request())

    assert result.harness == "claude" and "529" in (result.fallback_reason or "")
    assert result.answers["dur"].value == 1.0
    jev, llm = await _rows(db)
    assert jev["harness"] == "typesafe" and jev["outcome"] == "error"
    assert jev["kind"] == "decide" and "529" in jev["error"]
    assert llm["harness"] == "claude" and llm["outcome"] == "ok"


async def test_jev_timeout_is_recorded_as_timeout(db, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "k")

    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    _mock_http(monkeypatch, slow)
    claude = _fallback_caller({"rel": "new", "imp": 0, "dur": True})

    result = await _router(db, claude).decide(_request())

    assert result.harness == "claude"
    jev, _ = await _rows(db)
    assert jev["outcome"] == "timeout"


async def test_jev_answer_outside_the_options_falls_back(db, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "k")
    bad = json.loads(json.dumps(JEV_REPLY))
    bad["answers"]["rel"]["choice"] = "maybe"
    _mock_http(monkeypatch, lambda r: httpx.Response(200, json=bad))
    claude = _fallback_caller({"rel": "new", "imp": 0, "dur": True})

    result = await _router(db, claude).decide(_request())

    assert result.harness == "claude"


async def test_bad_llm_reply_raises(db, monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    claude = _fallback_caller({"rel": "sideways", "imp": 0, "dur": True})

    with pytest.raises(ModelCallError):
        await _router(db, claude).decide(_request())


async def test_unknown_purpose_and_no_questions_are_refused(db):
    router = _router(db, _fallback_caller({}))
    with pytest.raises(ValueError):
        await router.decide(_request(purpose="misc"))
    with pytest.raises(ValueError):
        await router.decide(_request(questions={}))


async def test_no_fallback_model_raises_instead_of_calling_llm(db, monkeypatch):
    """fallback_model=None (the reconcile gate): Jev down -> ModelCallError,
    no LLM leg."""
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    claude = _fallback_caller({})
    with pytest.raises(ModelCallError, match="no fallback"):
        await _router(db, claude).decide(_request(fallback_model=None))
    claude.assert_not_called()


@pytest.fixture
def configured_key():
    """Register a Settings-held key the way main.py does; always cleared."""
    typesafe_decide.configure_typesafe_key("settings-key")
    yield
    typesafe_decide.configure_typesafe_key(None)


async def test_decide_uses_the_settings_key_without_the_env_var(
    db, monkeypatch, configured_key
):
    """dv-e690: the bot scrubs secrets from os.environ; Jev must still answer."""
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    seen = _mock_http(monkeypatch, lambda r: httpx.Response(200, json=JEV_REPLY))
    claude = _fallback_caller({})

    result = await _router(db, claude).decide(_request())

    assert result.harness == "typesafe"
    assert result.fallback_reason is None
    assert seen[0].headers["Authorization"] == "Bearer settings-key"
    claude.assert_not_called()


async def test_env_var_overrides_the_settings_key(db, monkeypatch, configured_key):
    monkeypatch.setenv("TYPESAFE_API_KEY", "env-key")
    seen = _mock_http(monkeypatch, lambda r: httpx.Response(200, json=JEV_REPLY))

    await _router(db, _fallback_caller({})).decide(_request())

    assert seen[0].headers["Authorization"] == "Bearer env-key"


def test_no_key_anywhere_means_none(monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    typesafe_decide.configure_typesafe_key(None)
    assert typesafe_decide.typesafe_key() is None


def test_settings_field_feeds_the_adapter(monkeypatch):
    """The Settings field is what main.py registers; it is a SecretStr."""
    from src.config.settings import Settings

    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    s = Settings(
        telegram_bot_token="123:abc",
        telegram_bot_username="b",
        typesafe_api_key="from-settings",
        _env_file=None,
    )
    assert s.typesafe_api_key.get_secret_value() == "from-settings"
    assert "from-settings" in s.secret_values()


async def test_noul_with_mapping_instructions_and_criteria_is_sent(db, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "k")
    seen = _mock_http(
        monkeypatch,
        lambda r: httpx.Response(
            200, json={"answers": {"q": {"type": "noul", "noul": 0.4}}, "usage": {}}
        ),
    )
    question = Noul(
        instructions={"saved_fact": "x", "question": "y?"},
        criteria={"true": "t", "false": "f"},
    )
    await _router(db, _fallback_caller({})).decide(
        _request(questions={"q": question}, fallback_model=None)
    )
    assert json.loads(seen[0].content)["questions"]["q"] == {
        "type": "noul",
        "instructions": {"saved_fact": "x", "question": "y?"},
        "criteria": {"true": "t", "false": "f"},
    }
