"""dv-a5aa C2: the design critic runs on Claude opus (no fallback) and the
Claude plain path takes images through the router."""

from __future__ import annotations

import base64
from typing import Any, List, Optional

import pytest

from src.model_router import CallRequest, CallResult, ModelCallError, ModelRouter
from src.model_router.harness import HarnessVersions
from src.storage.database import DatabaseManager
from src.storage.model_calls import ModelCallRepository

PNG = b"\x89PNG\r\n\x1a\nxx"


class _Versions(HarnessVersions):
    async def get(self, harness: str) -> str:
        return f"{harness}-9.9"


class _Fake:
    def __init__(self, raises: Optional[BaseException] = None) -> None:
        self.raises = raises
        self.calls: List[CallRequest] = []

    async def __call__(self, request: CallRequest) -> CallResult:
        self.calls.append(request)
        if self.raises is not None:
            raise self.raises
        return CallResult(text="ok")


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


def test_C2_design_critic_resolves_to_claude_opus_with_no_fallback(tmp_path):
    from src.model_router.routes import CallRoutes, load_call_routes

    route = CallRoutes().resolve("design_critic")
    assert route.harness == "claude" and route.model == "opus"
    assert route.fallback is None

    yaml_path = tmp_path / "models.yaml"
    yaml_path.write_text(
        "default:\n  harness: openai_codex\n  models:\n    claude: sonnet\n"
        "    openai_codex: openai-codex/gpt-6-sol\n"
        "    openrouter: x-ai/grok-4\n  fallback:\n"
        "    harness: openrouter\n",
        encoding="utf-8",
    )
    route = load_call_routes(yaml_path).resolve("design_critic")
    assert route.harness == "claude" and route.model == "opus"
    assert route.fallback is None


async def test_C2_failed_critic_call_is_one_claude_error_row_and_no_other_adapter(
    db,
):
    fakes = {
        "claude": _Fake(ModelCallError("claude down")),
        "openai_codex": _Fake(),
        "openrouter": _Fake(),
    }
    router = ModelRouter(
        plain_callers=fakes, recorder=ModelCallRepository(db), versions=_Versions()
    )
    with pytest.raises(ModelCallError, match="claude down"):
        await router.complete(
            CallRequest(purpose="design_critic", prompt="p", system_prompt="s")
        )
    assert len(fakes["claude"].calls) == 1
    assert not fakes["openai_codex"].calls and not fakes["openrouter"].calls
    (row,) = await _rows(db)
    assert row["purpose"] == "design_critic"
    assert row["harness"] == "claude" and row["outcome"] == "error"


class _FakeClient:
    instances: List["_FakeClient"] = []

    def __init__(self, options: Any) -> None:
        self.sent: List[Any] = []
        _FakeClient.instances.append(self)

    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        return None

    async def query(self, prompt: Any) -> None:
        if isinstance(prompt, str):
            self.sent.append(prompt)
        else:
            self.sent.extend([m async for m in prompt])

    async def receive_response(self):
        from claude_agent_sdk import ResultMessage

        yield ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id="s",
            total_cost_usd=0.0,
            usage={"input_tokens": 1, "output_tokens": 1},
            result="done",
        )


@pytest.fixture
def fake_client(monkeypatch):
    from src.model_router import claude_plain

    _FakeClient.instances = []
    monkeypatch.setattr(claude_plain, "ClaudeSDKClient", _FakeClient)
    return _FakeClient


async def test_C2_claude_plain_sends_images_in_order_before_the_prompt(
    tmp_path, fake_client
):
    from src.model_router.claude_plain import ClaudePlainCaller

    a, b = tmp_path / "a.png", tmp_path / "b.jpg"
    a.write_bytes(PNG)
    b.write_bytes(b"\xff\xd8\xffjpg")
    result = await ClaudePlainCaller()(
        CallRequest(
            purpose="design_critic",
            prompt="the brief",
            system_prompt="s",
            images=(str(a), str(b)),
        )
    )
    assert result.text == "done"
    (client,) = fake_client.instances
    (message,) = client.sent
    assert message["type"] == "user"
    content = message["message"]["content"]
    assert [c["type"] for c in content] == ["image", "image", "text"]
    assert content[2]["text"] == "the brief"
    assert content[0]["source"]["media_type"] == "image/png"
    assert content[1]["source"]["media_type"] == "image/jpeg"
    assert base64.b64decode(content[0]["source"]["data"]) == PNG


async def test_C2_claude_plain_without_images_keeps_a_plain_string_query(fake_client):
    from src.model_router.claude_plain import ClaudePlainCaller

    await ClaudePlainCaller()(
        CallRequest(purpose="screen", prompt="hello", system_prompt="s")
    )
    assert fake_client.instances[0].sent == ["hello"]


@pytest.mark.parametrize("kind", ["missing", "not_image"])
async def test_C2_claude_plain_bad_image_raises_before_any_call(
    tmp_path, fake_client, kind
):
    from src.model_router.claude_plain import ClaudePlainCaller

    path = tmp_path / ("nope.png" if kind == "missing" else "notes.txt")
    if kind == "not_image":
        path.write_text("hi")
    with pytest.raises(ModelCallError):
        await ClaudePlainCaller()(
            CallRequest(
                purpose="design_critic",
                prompt="p",
                system_prompt="s",
                images=(str(path),),
            )
        )
    assert not fake_client.instances or not fake_client.instances[0].sent
