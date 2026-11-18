"""The design critic: Claude opus (dv-a5aa; Codex before, dv-9133), through
the model router, with the renders as images.

Contract (docs/research/dv-9133/analysis.md, phase 2 step 4):
``src.designloop.critic.critique(brief, renders, references=()) -> Critique``
(async) does one ``get_router().complete(CallRequest(purpose="design_critic",
images=..., json_schema=...))``. The schema is fixed: ``criteria`` (6
criteria, each ``ok`` | ``fix: <what>``) and ``verdict`` ``ready`` |
``revise``. The critic always sees the current screen (``brief.refs``) next to
the variant renders. ``Critique.verdict`` / ``Critique.criteria``.
"""

import inspect
import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

from src.model_router import (
    CallRequest,
    CallResult,
    ModelCallError,
    ModelRouter,
    set_router,
)
from src.model_router.claude_plain import ClaudePlainCaller
from src.model_router.harness import HarnessVersions
from src.storage.database import DatabaseManager
from src.storage.model_calls import ModelCallRepository
from tests.unit.test_designloop.helpers import FEED_FEATURES, conforming


class _Versions(HarnessVersions):
    async def get(self, harness: str) -> str:
        return f"{harness}-9.9"


class _NeverCodex:
    def __init__(self) -> None:
        self.calls: List[CallRequest] = []

    async def __call__(self, request: CallRequest) -> CallResult:
        self.calls.append(request)
        raise AssertionError("the design critic never runs on Codex")


@pytest.fixture
async def db(tmp_path):
    manager = DatabaseManager(f"sqlite:///{tmp_path / 'test.db'}")
    await manager.initialize()
    yield manager
    await manager.close()


@pytest.fixture(autouse=True)
def _reset_router():
    yield
    set_router(None)


async def _rows(db: DatabaseManager) -> List[dict]:
    async with db.get_connection() as conn:
        cur = await conn.execute("SELECT * FROM model_calls ORDER BY id")
        return [dict(r) for r in await cur.fetchall()]


async def _maybe(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


def _renders(tmp_path: Path) -> List[Path]:
    out = []
    for variant in ("v1", "v2"):
        for theme in ("dark", "light"):
            path = tmp_path / ".screens" / "design" / variant / f"main-{theme}.png"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"\x89PNG")
            out.append(path)
    return out


async def test_critic_sends_renders_to_claude_as_images_and_gets_the_fixed_json(
    app_repo, tmp_path, monkeypatch, db
):
    """End to end down to the SDK client: every render and every
    current-screen reference is an image block before the brief, the answer
    is parsed into 6 criteria + a verdict, one row is recorded."""
    from claude_agent_sdk import ResultMessage

    from src.designloop.brief import build_brief
    from src.designloop.critic import critique
    from src.model_router import claude_plain

    captured: Dict[str, Any] = {}

    class _Client:
        def __init__(self, options: Any) -> None:
            captured["options"] = options

        async def connect(self) -> None:
            return None

        async def disconnect(self) -> None:
            return None

        async def query(self, prompt: Any) -> None:
            captured["messages"] = [m async for m in prompt]

        async def receive_response(self) -> Any:
            schema = captured["options"].output_format["schema"]
            captured["schema"] = schema
            answer = conforming(schema, "the filter row crowds the first card")
            yield ResultMessage(
                subtype="success",
                duration_ms=1,
                duration_api_ms=1,
                is_error=False,
                num_turns=1,
                session_id="s",
                usage={"input_tokens": 1, "output_tokens": 1},
                result=json.dumps(answer),
                structured_output=answer,
            )

    monkeypatch.setattr(claude_plain, "ClaudeSDKClient", _Client)
    codex = _NeverCodex()
    set_router(
        ModelRouter(
            plain_callers={"claude": ClaudePlainCaller(), "openai_codex": codex},
            recorder=ModelCallRepository(db),
            versions=_Versions(),
        )
    )
    brief = build_brief(app_repo, "feed", "Make the feed calmer.")
    renders = _renders(tmp_path)

    result = await _maybe(critique(brief, renders))

    assert captured["options"].model == "opus"
    (message,) = captured["messages"]
    content = message["message"]["content"]
    images = [c for c in content if c["type"] == "image"]
    assert len(images) == len(renders) + len(brief.refs)
    assert content[-1]["type"] == "text"
    prompt = content[-1]["text"]
    for feature in FEED_FEATURES:
        assert feature in prompt, feature

    # The fixed schema: 6 criteria + verdict ready|revise.
    schema = captured["schema"]
    criteria = schema["properties"]["criteria"]["properties"]
    assert len(criteria) == 6
    assert set(schema["properties"]["verdict"]["enum"]) == {"ready", "revise"}

    assert result.verdict == "revise"
    assert set(result.criteria) == set(criteria)
    fixes = [v for v in result.criteria.values() if v != "ok"]
    assert len(fixes) == 1 and fixes[0].startswith("fix:")
    assert "filter row crowds the first card" in fixes[0]

    assert not codex.calls
    (row,) = await _rows(db)
    assert row["purpose"] == "design_critic" and row["harness"] == "claude"
    assert row["outcome"] == "ok"


async def test_critic_refuses_a_criterion_that_is_neither_ok_nor_fix(
    app_repo, tmp_path
):
    """`great!` is not `ok` or `fix: <what>`: the critique is not accepted as
    is (a vague verdict would reach the owner), and Codex never answers
    instead."""
    from src.designloop.brief import build_brief
    from src.designloop.critic import critique

    calls: List[CallRequest] = []

    async def claude(request: CallRequest) -> CallResult:
        calls.append(request)
        answer = conforming(request.json_schema or {}, "x")
        answer["criteria"] = {k: "great!" for k in answer["criteria"]}
        return CallResult(text=json.dumps(answer))

    codex = _NeverCodex()
    set_router(
        ModelRouter(
            plain_callers={"claude": claude, "openai_codex": codex},
            versions=_Versions(),
        )
    )
    brief = build_brief(app_repo, "feed", "Make the feed calmer.")

    with pytest.raises((ModelCallError, ValueError)):
        await _maybe(critique(brief, _renders(tmp_path)))

    assert calls and all(r.purpose == "design_critic" for r in calls)
    assert all(r.images for r in calls)
    assert not codex.calls
