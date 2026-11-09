"""A plain Codex call uses the same provider limit as Codex turns and jobs."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from src.model_router import CallRequest, CallResult, ModelCallError, ModelRouter, quota
from src.model_router.codex_plain import CodexPlainCaller

LIMIT_TEXT = (
    "You’ve hit your usage limit. Upgrade to Pro (https://chatgpt.com/explore/pro), "
    "visit https://chatgpt.com/codex/settings/usage to purchase more credits "
    "or try again at 10:24 PM."
)


class FailedProcess:
    returncode = 1

    def __init__(self, stdout: bytes, stderr: bytes = b"") -> None:
        self.stdout = stdout
        self.stderr = stderr

    async def communicate(self, input: bytes) -> tuple[bytes, bytes]:  # noqa: A002
        return self.stdout, self.stderr


def request(harness: str = "openai_codex") -> CallRequest:
    return CallRequest(
        purpose="extraction",
        prompt="extract",
        system_prompt="",
        harness=harness,
        model="openai-codex/gpt-6-sol" if harness == "openai_codex" else "sonnet",
    )


async def test_plain_codex_limit_records_hit_and_blocks_relaunch_but_not_claude(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("DEVVY_DATA_ROOT", str(tmp_path))
    now = datetime.now(UTC)
    event = {"type": "error", "message": LIMIT_TEXT}
    tail = {"type": "turn.failed", "error": {"message": "turn failed"}}
    calls = AsyncMock(
        return_value=FailedProcess(
            "\n".join(json.dumps(item) for item in (event, tail)).encode(),
            stderr=b"codex exited with status 1",
        )
    )
    monkeypatch.setattr(asyncio, "create_subprocess_exec", calls)
    claude = AsyncMock(return_value=CallResult(text="Claude answer"))
    router = ModelRouter(
        plain_callers={"openai_codex": CodexPlainCaller(), "claude": claude}
    )

    with pytest.raises(ModelCallError, match="OpenAI usage limit"):
        await router.complete(request())
    recorded = quota.active(provider="openai")
    assert recorded is not None and recorded.provider == "openai"
    assert now < recorded.until <= now + timedelta(days=1)
    assert quota.active(provider="anthropic") is None

    with pytest.raises(ModelCallError, match="OpenAI usage limit"):
        await router.complete(request())
    calls.assert_awaited_once()

    answer = await router.complete(request("claude"))
    assert answer.text == "Claude answer"
    claude.assert_awaited_once()


async def test_plain_codex_limit_in_stderr_is_recorded(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("DEVVY_DATA_ROOT", str(tmp_path))
    calls = AsyncMock(return_value=FailedProcess(b"", LIMIT_TEXT.encode()))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", calls)

    with pytest.raises(ModelCallError, match="OpenAI usage limit"):
        await CodexPlainCaller()(request())
    assert quota.active(provider="openai") is not None


async def test_plain_codex_ordinary_error_does_not_record_limit(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("DEVVY_DATA_ROOT", str(tmp_path))
    calls = AsyncMock(return_value=FailedProcess(b"", b"invalid schema"))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", calls)

    with pytest.raises(ModelCallError, match="invalid schema"):
        await CodexPlainCaller()(request())
    assert quota.active(provider="openai") is None
