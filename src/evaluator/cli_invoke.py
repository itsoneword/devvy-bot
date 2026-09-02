"""Shared lean-context Claude CLI invocation for offline extraction jobs.

Factored out of EvaluatorService so other offline consumers (the
consolidation pass, dv-4c6f) can make one-shot JSON calls without importing
the service. The harness is stripped: no project/user settings (no CLAUDE.md,
no `.claude/skills`), no MCP servers, no tools — these callers read a prompt
and emit JSON, and re-sending the harness on every run is pure cost.
"""

import asyncio
import json
from typing import List

DEFAULT_SYSTEM_PROMPT = (
    "You extract structured memory ops from conversations. Follow the output "
    "format in the user message exactly and reply with JSON only."
)


def lean_cli_args(model: str, system_prompt: str) -> List[str]:
    """Argv for a lean, tool-less, settings-less JSON extraction subprocess."""
    return [
        "--model",
        model,
        "--system-prompt",
        system_prompt,
        "--tools",
        "",
        "--setting-sources",
        "",
        "--strict-mcp-config",
        "--disable-slash-commands",
        "--output-format",
        "json",
        "-p",
    ]


async def invoke_claude_cli(
    prompt: str,
    model: str,
    system_prompt: str = DEFAULT_SYSTEM_PROMPT,
    timeout: float = 120,
) -> str:
    """Invoke the Claude CLI for JSON extraction; returns the result text.

    The prompt is piped via stdin rather than passed as a CLI argument:
    extraction prompts can embed whole conversation windows and exceed the OS
    ARG_MAX limit (shared with the process environment). Stdin has no such cap.
    """
    process = await asyncio.create_subprocess_exec(
        "claude",
        *lean_cli_args(model, system_prompt),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(
        process.communicate(input=prompt.encode("utf-8")), timeout=timeout
    )

    if process.returncode != 0:
        error_msg = stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(
            f"Claude CLI failed (exit {process.returncode}): {error_msg}"
        )

    raw = stdout.decode("utf-8").strip()
    # --output-format json wraps in {"result": ...}; unwrap to the text.
    try:
        wrapper = json.loads(raw)
        if isinstance(wrapper, dict) and "result" in wrapper:
            return str(wrapper["result"])
    except (json.JSONDecodeError, TypeError):
        pass
    return raw
