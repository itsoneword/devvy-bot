"""Background jobs on the router (dv-ef90): one agent turn in its own process.

`scripts/bgjob` (and taskpump through it) and the server monitor used to run
the CLI in print mode with every permission bypassed and no checks. They run
outside the bot process, so the bot's in-process hook cannot reach them. This
module gives them the same turn through the Agent SDK with:

* the one tool-call policy (``src/security/policy.py``), evaluated here, so
  allow/deny holds even when the bot is down;
* an ``ask`` sent to the bot's internal API, which shows the owner the usual
  Telegram button and waits up to :data:`ASK_TIMEOUT_SECONDS`; no bot, no
  answer, or a connection error all mean deny;
* a ``model_calls`` row written through the bot (the job's sandbox cannot
  write the database itself).

``scripts/agentjob`` is the command-line entry point.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import structlog
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    ResultMessage,
    SystemMessage,
    TextBlock,
)

from src.security import policy

from .harness import CLAUDE_ACCOUNT_ENV, claude_program
from .types import CLAUDE_HARNESS, PURPOSES

logger = structlog.get_logger()

# Owner decision 2026-10-04: an unattended run that hits an ask rule waits for
# the owner's button up to one hour, then is denied.
ASK_TIMEOUT_SECONDS = 3600.0
# How long the hook may run; the SDK kills it otherwise.
_HOOK_TIMEOUT_SECONDS = int(ASK_TIMEOUT_SECONDS) + 120
# Any Task-tool subagents a job spawns stay on sonnet whatever the job's model.
SUBAGENT_MODEL = "sonnet"

ASK_PATH = "/internal/policy/ask"
MODEL_CALLS_PATH = "/internal/model_calls"


@dataclass(frozen=True)
class JobSpec:
    purpose: str
    name: str
    prompt: str
    model: str
    working_directory: Path
    full_access: bool
    unrestricted: bool
    profile: Optional[str] = None
    cli_path: Optional[str] = None


class BotLink:
    """The job's line to the bot: the internal API with the shared secret."""

    def __init__(self, base_url: str, secret: Optional[str]) -> None:
        self._base = base_url.rstrip("/")
        self._secret = secret

    def post(self, path: str, body: Dict[str, Any], timeout: float) -> Dict[str, Any]:
        if not self._secret:
            raise RuntimeError("no bot API secret available")
        request = urllib.request.Request(
            self._base + path,
            data=json.dumps(body).encode(),
            headers={
                "Authorization": f"Bearer {self._secret}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            data = json.loads(resp.read().decode() or "{}")
        return data if isinstance(data, dict) else {}

    async def ask(self, reason: str, preview: str, source: str) -> bool:
        """Owner's answer to an ask rule; False on any failure."""
        try:
            answer = await asyncio.to_thread(
                self.post,
                ASK_PATH,
                {"reason": reason, "preview": preview, "source": source},
                ASK_TIMEOUT_SECONDS + 60,
            )
        except (urllib.error.URLError, OSError, ValueError, RuntimeError) as exc:
            logger.warning("ask could not reach the bot -> denied", error=str(exc))
            return False
        return bool(answer.get("approved"))

    async def record(self, row: Dict[str, Any]) -> bool:
        try:
            await asyncio.to_thread(self.post, MODEL_CALLS_PATH, row, 15)
        except (urllib.error.URLError, OSError, ValueError, RuntimeError) as exc:
            logger.warning("model call not recorded", error=str(exc))
            return False
        return True


def policy_hook(turn: policy.TurnPolicy, link: BotLink, source: str) -> Any:
    """The job's PreToolUse hook: same rules as chat, asks through the bot."""

    def deny(reason: str) -> Dict[str, Any]:
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }

    async def pre_tool_use(
        input_data: Dict[str, Any], tool_use_id: Optional[str], context: Any
    ) -> Dict[str, Any]:
        tool_name = str(input_data.get("tool_name") or "")
        tool_input = input_data.get("tool_input") or {}
        decision = policy.decide(tool_name, tool_input, turn)
        if decision.action == policy.DENY:
            logger.warning(
                "PreToolUse hook denied tool call",
                backend="job",
                tool_name=tool_name,
                reason=decision.reason,
            )
            return deny(decision.reason)
        if decision.action == policy.ASK:
            approved = await link.ask(decision.reason, decision.preview, source)
            if not approved:
                logger.warning(
                    "Destructive op denied by confirmation gate",
                    backend="job",
                    tool_name=tool_name,
                    reason=decision.reason,
                )
                return deny(f"Denied by owner (confirmation gate): {decision.reason}")
        return {}

    return pre_tool_use


def job_options(spec: JobSpec, link: BotLink) -> ClaudeAgentOptions:
    turn = policy.TurnPolicy(
        working_directory=spec.working_directory,
        full_access=spec.full_access,
        unrestricted=spec.unrestricted,
    )
    return ClaudeAgentOptions(
        model=spec.model,
        cwd=str(spec.working_directory),
        cli_path=claude_program(spec.cli_path),
        # The user's and the project's settings (CLAUDE.md, skills, MCP servers
        # of the working dir), but NOT `.claude/settings.local.json`: that file
        # is the owner's interactive session's permission cache. In the devops
        # dir it says `defaultMode: dontAsk` with an ask list, and the CLI
        # applies it even under bypassPermissions, so a job's `rm` was denied
        # right after the owner approved it (2026-10-06; print mode had the
        # same problem).
        setting_sources=["user", "project"],
        # The job has nobody at a prompt; the hook is the gate.
        permission_mode="bypassPermissions",
        env={**CLAUDE_ACCOUNT_ENV, "CLAUDE_CODE_SUBAGENT_MODEL": SUBAGENT_MODEL},
        hooks={
            "PreToolUse": [
                HookMatcher(
                    hooks=[policy_hook(turn, link, source=spec.name)],
                    timeout=_HOOK_TIMEOUT_SECONDS,
                )
            ]
        },
        stderr=lambda line: print(line, file=sys.stderr, flush=True),
    )


async def run_job(spec: JobSpec, link: BotLink, out: Any = None) -> int:
    """Run the job; print its answer to ``out`` (stdout); return an exit code.

    Also writes the ``model_calls`` row through the bot.
    """
    if spec.purpose not in PURPOSES:
        raise ValueError(f"unknown model call purpose: {spec.purpose!r}")
    out = out if out is not None else sys.stdout
    client = ClaudeSDKClient(job_options(spec, link))
    started = time.monotonic()
    version: Optional[str] = None
    model: Optional[str] = spec.model
    final: Optional[ResultMessage] = None
    texts: List[str] = []
    error: Optional[str] = None
    try:
        await client.connect()
        await client.query(spec.prompt)
        async for message in client.receive_response():
            if isinstance(message, SystemMessage) and message.subtype == "init":
                version = message.data.get("claude_code_version") or version
                model = message.data.get("model") or model
            elif isinstance(message, AssistantMessage):
                texts = [
                    block.text
                    for block in message.content
                    if isinstance(block, TextBlock) and block.text
                ]
            elif isinstance(message, ResultMessage):
                final = message
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"[:500]
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass

    if final is not None and not final.is_error:
        answer = final.result if final.result is not None else "\n".join(texts)
        print(answer, file=out, flush=True)
        outcome, code = "ok", 0
    else:
        reason = error or (
            str(final.result or final.subtype) if final is not None else "no result"
        )
        print(f"agentjob failed: {reason}", file=sys.stderr, flush=True)
        outcome, code, error = "error", 1, reason[:500]

    usage = (final.usage if final is not None else None) or {}
    await link.record(
        {
            "purpose": spec.purpose,
            "kind": "turn",
            "harness": CLAUDE_HARNESS,
            "harness_version": version,
            "model": model,
            "attended": False,
            "profile": spec.profile,
            "session_id": final.session_id if final is not None else None,
            "input_tokens": int(usage.get("input_tokens") or 0),
            "output_tokens": int(usage.get("output_tokens") or 0),
            "cache_read_tokens": int(usage.get("cache_read_input_tokens") or 0),
            "cache_creation_tokens": int(usage.get("cache_creation_input_tokens") or 0),
            "cost_usd": final.total_cost_usd if final is not None else None,
            "duration_ms": int((time.monotonic() - started) * 1000),
            "outcome": outcome,
            "error": error,
        }
    )
    return code


def bot_link_from_env(repo_root: Path) -> BotLink:
    """The bot's internal API as the scripts know it: host/port from the
    config env, secret from the secrets file (never logged)."""
    from src.utils.paths import read_bot_secret

    dotenv = repo_root / ".env"
    env = _dotenv_values(dotenv)
    host = (
        os.environ.get("API_SERVER_HOST") or env.get("API_SERVER_HOST") or "127.0.0.1"
    )
    if host == "0.0.0.0":
        host = "127.0.0.1"
    port = os.environ.get("API_SERVER_PORT") or env.get("API_SERVER_PORT") or "8080"
    secret = read_bot_secret("WEBHOOK_API_SECRET", dotenv=dotenv)
    return BotLink(f"http://{host}:{port}", secret)


def _dotenv_values(path: Path) -> Dict[str, str]:
    values: Dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, val = line.partition("=")
            values[key.strip()] = val.strip().strip("\"'")
    return values
