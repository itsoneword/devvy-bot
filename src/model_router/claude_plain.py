"""Plain Claude calls through the Agent SDK (dv-11a5).

Replaces the print-mode CLI subprocess copies. The harness is stripped the same
way: no tools, no project or user settings (no CLAUDE.md, no skills), no MCP
servers, no slash commands. These callers send a prompt and read text back.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any, List, Optional, Tuple

from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKClient,
    RateLimitEvent,
    ResultMessage,
    SystemMessage,
)

from . import quota
from .harness import CLAUDE_ACCOUNT_ENV, claude_program
from .types import (
    CLAUDE_HARNESS,
    CallRequest,
    CallResult,
    ModelCallError,
    ModelCallTimeout,
    refuse_images,
)


def lean_options(
    request: CallRequest, cli_path: Optional[str] = None
) -> ClaudeAgentOptions:
    """The one lean option set for a tool-less call."""
    options = ClaudeAgentOptions(
        model=request.model,
        system_prompt=request.system_prompt,
        tools=[],
        setting_sources=[],
        strict_mcp_config=True,
        permission_mode="default",
        cli_path=claude_program(cli_path),
        env=dict(CLAUDE_ACCOUNT_ENV),
        extra_args={"disable-slash-commands": None},
    )
    if request.json_schema is not None:
        options.output_format = {
            "type": "json_schema",
            "schema": request.json_schema,
        }
    return options


class ClaudePlainCaller:
    """Runs one :class:`CallRequest` on the Claude CLI and returns its text."""

    def __init__(self, cli_path: Optional[str] = None) -> None:
        self._cli_path = cli_path

    async def __call__(self, request: CallRequest) -> CallResult:
        refuse_images(request, CLAUDE_HARNESS)
        client = ClaudeSDKClient(lean_options(request, self._cli_path))
        messages: List[Any] = []

        # Connect and disconnect stay in one task: the SDK's task group must
        # be left by the task that entered it. A timeout cancels this task and
        # the ``finally`` stops the CLI child.
        async def _run() -> None:
            # dv-55ad: the last usage event, recorded once after the stream
            # (also on timeout or error), never inside the loop.
            latest: Optional[Tuple[Any, datetime]] = None
            try:
                await client.connect()
                await client.query(request.prompt)
                async for message in client.receive_response():
                    messages.append(message)
                    if isinstance(message, RateLimitEvent):
                        latest = (message.rate_limit_info.raw, datetime.now(UTC))
            finally:
                if latest is not None:
                    quota.record_usage_soon(latest[0], "claude-call", latest[1])
                await client.disconnect()

        task = asyncio.ensure_future(_run())
        try:
            done, _ = await asyncio.wait({task}, timeout=request.timeout_seconds)
            if not done:
                raise ModelCallTimeout(
                    f"timeout after {request.timeout_seconds:g}s (claude)"
                )
            task.result()
        except ModelCallError:
            raise
        except Exception as exc:
            raise ModelCallError(f"Claude call failed: {exc}") from exc
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

        return _result_from(messages)


def _result_from(messages: List[Any]) -> CallResult:
    version: Optional[str] = None
    model: Optional[str] = None
    final: Optional[ResultMessage] = None
    for message in messages:
        if isinstance(message, SystemMessage) and message.subtype == "init":
            version = message.data.get("claude_code_version") or version
            model = message.data.get("model") or model
        elif isinstance(message, ResultMessage):
            final = message
    if final is None:
        raise ModelCallError("Claude call failed: no result message")
    if final.is_error:
        reason = str(final.result or final.subtype or "unknown error")
        raise ModelCallError(f"Claude call failed: {reason[:800]}")

    structured = getattr(final, "structured_output", None)
    text = json.dumps(structured) if structured is not None else (final.result or "")
    usage = final.usage or {}
    return CallResult(
        text=str(text),
        model=model,
        harness_version=version,
        input_tokens=int(usage.get("input_tokens") or 0),
        output_tokens=int(usage.get("output_tokens") or 0),
        cache_read_tokens=int(usage.get("cache_read_input_tokens") or 0),
        cache_creation_tokens=int(usage.get("cache_creation_input_tokens") or 0),
        cost_usd=final.total_cost_usd,
        session_id=final.session_id,
    )
