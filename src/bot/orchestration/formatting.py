"""Presentation helpers for orchestrator output: Telegram HTML rendering,
secret redaction, tool icons, and text condensing/truncation.

Extracted verbatim from src/bot/orchestrator.py (behavior-preserving move).
"""

import re
from typing import Any, Dict, List, Optional

from ...claude.exceptions import (
    ClaudeError,
    ClaudeMCPError,
    ClaudeParsingError,
    ClaudeProcessError,
    ClaudeSessionError,
    ClaudeTimeoutError,
)
from ...profiles.registry import ProfileRegistry
from ...storage.user_repository import BotUser, Family
from ..utils.html_format import escape_html


def image_save_extension(processed_image: Any) -> str:
    """Pick the file extension for a downloaded Telegram photo.

    Must be the actual image format (jpeg/png/...), never
    ``processed_image.image_type`` — that's a semantic category
    ("screenshot", "diagram", "ui_mockup", ...) used only for prompt
    selection, and ``_detect_image_type`` is currently a stub that always
    returns "screenshot". Using it as the extension produced files like
    "tg_image_xxx.screenshot", which neither Claude's Read tool nor Codex's
    ``-i`` recognize as an image by extension — the backend then reports a
    perfectly valid JPEG as an unreadable binary file.
    """
    image_format = (processed_image.metadata or {}).get("format", "jpeg")
    if image_format == "unknown":
        image_format = "jpeg"  # Telegram photos are JPEG; safe fallback
    return image_format


def format_sessions_summary(
    registry: ProfileRegistry, last_by_dir: dict, active: str
) -> str:
    """Render a /sessions summary as Telegram HTML.

    last_by_dir: {working_dir: info|None}. Dynamic values are HTML-escaped so
    Telegram never rejects the message over stray metacharacters.
    """
    lines = ["<b>Sessions by profile:</b>"]
    for p in registry.profiles:
        marker = "→ " if p.slug == active else "  "
        slug = escape_html(p.slug)
        info = last_by_dir.get(p.working_dir)
        if not info:
            lines.append(f"{marker}{slug}: no session yet")
        else:
            toks = info.get("context_tokens", info.get("cumulative_tokens", 0))
            ts = escape_html(str(info.get("updated_at", "?")))
            backend = escape_html(str(info.get("backend", "claude")))
            lines.append(f"{marker}{slug}: {toks} tokens ({backend}), last {ts}")
    return "\n".join(lines)


def format_usage_table(
    registry: ProfileRegistry,
    usage_24h: Dict[str, Dict[str, float]],
    usage_30d: Dict[str, Dict[str, float]],
) -> str:
    """Render a per-profile token/cost usage table for 24h and 30d windows.

    ``usage_24h``/``usage_30d``: {project_path: {"tokens", "cost", "turns"}}
    as returned by TokenTracker.get_usage_by_project, keyed by the exact
    working_dir string of each profile. Telegram has no table markup, so
    this renders as a monospace <pre> block with aligned columns.
    """
    rows = []
    for p in registry.profiles:
        key = str(p.working_dir)
        d24 = usage_24h.get(key, {})
        d30 = usage_30d.get(key, {})
        rows.append(
            (
                p.slug,
                int(d24.get("tokens", 0)),
                float(d24.get("cost", 0.0)),
                int(d30.get("tokens", 0)),
                float(d30.get("cost", 0.0)),
            )
        )

    header = f"{'profile':<10}{'24h tok':>10}{'24h $':>8}{'30d tok':>10}{'30d $':>8}"
    lines = [header, "-" * len(header)]
    for slug, tok24, cost24, tok30, cost30 in rows:
        lines.append(f"{slug:<10}{tok24:>10}{cost24:>8.2f}{tok30:>10}{cost30:>8.2f}")
    table = "\n".join(lines)
    return f"<b>Usage by profile:</b>\n<pre>{escape_html(table)}</pre>"


def format_diag(profile: str, events: list) -> str:
    """Summarize recent lifecycle events for a profile as Telegram HTML."""
    counts: dict = {}
    mem_chars = []
    for e in events:
        counts[e["event"]] = counts.get(e["event"], 0) + 1
        if e["event"] == "memory_loaded":
            c = e.get("detail", {}).get("chars")
            if isinstance(c, (int, float)):
                mem_chars.append(c)
    lines = [f"<b>Diag — {escape_html(profile)}</b> (last {len(events)} events)"]
    for k in (
        "resume",
        "fresh",
        "memory_loaded",
        "evaluator",
        "refresh",
        "orphan_recap",
    ):
        if k in counts:
            lines.append(f"  {k}: {counts[k]}")
    if mem_chars:
        avg = sum(mem_chars) / len(mem_chars)
        lines.append(f"  avg memory chars: {avg:.0f}")
    if len(lines) == 1:
        lines.append("  no events recorded yet")
    return "\n".join(lines)


# Telegram ids are positive integers; anything above this bound is implausible.
_MAX_TELEGRAM_ID = 10**15


def parse_telegram_id(args: List[str]) -> Optional[int]:
    """Return a plausible Telegram user id from command args, else None."""
    if len(args) != 1:
        return None
    try:
        value = int(args[0])
    except ValueError:
        return None
    if 0 < value < _MAX_TELEGRAM_ID:
        return value
    return None


def format_family(family: Family, members: List[BotUser], cap: int) -> str:
    """Render the /family manage view as Telegram HTML."""
    created = family.created_at.strftime("%Y-%m-%d") if family.created_at else "?"
    lines = [
        f"<b>Your family</b> — {len(members)}/{cap} members",
        f"Created: {escape_html(created)}",
    ]
    for m in members:
        joined = m.created_at.strftime("%Y-%m-%d") if m.created_at else "?"
        lines.append(
            f"  <code>{m.telegram_id}</code> — {escape_html(m.role)}, "
            f"joined {escape_html(joined)}"
        )
    if not members:
        lines.append("  (no members)")
    return "\n".join(lines)


# Patterns that look like secrets/credentials in CLI arguments
_SECRET_PATTERNS: List[re.Pattern[str]] = [
    # API keys / tokens (sk-ant-..., sk-..., ghp_..., gho_..., github_pat_..., xoxb-...)
    re.compile(
        r"(sk-ant-api\d*-[A-Za-z0-9_-]{10})[A-Za-z0-9_-]*"
        r"|(sk-[A-Za-z0-9_-]{20})[A-Za-z0-9_-]*"
        r"|(ghp_[A-Za-z0-9]{5})[A-Za-z0-9]*"
        r"|(gho_[A-Za-z0-9]{5})[A-Za-z0-9]*"
        r"|(github_pat_[A-Za-z0-9_]{5})[A-Za-z0-9_]*"
        r"|(xoxb-[A-Za-z0-9]{5})[A-Za-z0-9-]*"
    ),
    # AWS access keys
    re.compile(r"(AKIA[0-9A-Z]{4})[0-9A-Z]{12}"),
    # Generic long hex/base64 tokens after common flags/env patterns
    re.compile(
        r"((?:--token|--secret|--password|--api-key|--apikey|--auth)"
        r"[= ]+)['\"]?[A-Za-z0-9+/_.:-]{8,}['\"]?"
    ),
    # Inline env assignments like KEY=value
    re.compile(
        r"((?:TOKEN|SECRET|PASSWORD|API_KEY|APIKEY|AUTH_TOKEN|PRIVATE_KEY"
        r"|ACCESS_KEY|CLIENT_SECRET|WEBHOOK_SECRET)"
        r"=)['\"]?[^\s'\"]{8,}['\"]?"
    ),
    # Bearer / Basic auth headers
    re.compile(r"(Bearer )[A-Za-z0-9+/_.:-]{8,}" r"|(Basic )[A-Za-z0-9+/=]{8,}"),
    # Connection strings with credentials  user:pass@host
    re.compile(r"://([^:]+:)[^@]{4,}(@)"),
]


def _redact_secrets(text: str) -> str:
    """Replace likely secrets/credentials with redacted placeholders."""
    result = text
    for pattern in _SECRET_PATTERNS:
        result = pattern.sub(
            lambda m: next((g + "***" for g in m.groups() if g is not None), "***"),
            result,
        )
    return result


# Tool name -> friendly emoji mapping for verbose output
_TOOL_ICONS: Dict[str, str] = {
    "Read": "\U0001f4d6",
    "Write": "\u270f\ufe0f",
    "Edit": "\u270f\ufe0f",
    "MultiEdit": "\u270f\ufe0f",
    "Bash": "\U0001f4bb",
    "Glob": "\U0001f50d",
    "Grep": "\U0001f50d",
    "LS": "\U0001f4c2",
    "Task": "\U0001f9e0",
    "TaskOutput": "\U0001f9e0",
    "WebFetch": "\U0001f310",
    "WebSearch": "\U0001f310",
    "NotebookRead": "\U0001f4d3",
    "NotebookEdit": "\U0001f4d3",
    "TodoRead": "\u2611\ufe0f",
    "TodoWrite": "\u2611\ufe0f",
}


def _tool_icon(name: str) -> str:
    """Return emoji for a tool, with a default wrench."""
    return _TOOL_ICONS.get(name, "\U0001f527")


def _condense(text: str) -> str:
    """Collapse all whitespace runs into single spaces."""
    return " ".join(text.split())


def _truncate(text: str, limit: int) -> str:
    """Return *text* clipped to *limit* chars with an ellipsis when clipped."""
    if len(text) <= limit:
        return text
    return text[: max(limit - 1, 0)].rstrip() + "…"


def _format_error_message(error: Exception | str) -> str:
    """Format error messages for user-friendly display.

    Accepts an exception object (preferred) or a string for backward
    compatibility.  When an exception is provided, the error type is used
    to produce a specific, actionable message.
    """
    # Normalise: keep both the object and a string representation.
    if isinstance(error, str):
        error_str = error
        error_obj: Exception | None = None
    else:
        error_str = str(error)
        error_obj = error

    # --- Dispatch on exception type first (most specific) ---

    if isinstance(error_obj, ClaudeTimeoutError):
        return (
            "⏰ <b>Request Timeout</b>\n\n"
            f"{escape_html(error_str)}\n\n"
            "<b>What you can do:</b>\n"
            "• Try breaking your request into smaller parts\n"
            "• Avoid asking for very large file operations in one go\n"
            "• Try again — transient slowdowns happen"
        )

    if isinstance(error_obj, ClaudeMCPError):
        server_hint = ""
        if error_obj.server_name:
            server_hint = f" (<code>{escape_html(error_obj.server_name)}</code>)"
        return (
            f"🔌 <b>MCP Server Error</b>{server_hint}\n\n"
            f"{escape_html(error_str)}\n\n"
            "<b>What you can do:</b>\n"
            "• Check that the MCP server is running and reachable\n"
            "• Verify <code>MCP_CONFIG_PATH</code> points to a valid config\n"
            "• Ask the administrator to check MCP server logs"
        )

    if isinstance(error_obj, ClaudeParsingError):
        if "buffer size" in error_str.lower():
            # A single tool result (usually an image read back) blew past the
            # SDK's stdout message limit. Retrying the same prompt reproduces
            # it, so say what to change instead.
            return (
                "📄 <b>Response Too Large</b>\n\n"
                "A single tool result was too big for the agent transport "
                "(usually reading an image or a huge file back into the "
                "conversation).\n\n"
                "<b>What you can do:</b>\n"
                "• Ask for the file to be sent, not read back\n"
                "• Work on a smaller slice of the file\n"
                "• Use /new if the session keeps failing"
            )
        return (
            "📄 <b>Response Parsing Error</b>\n\n"
            f"Claude returned a response that could not be parsed:\n"
            f"<code>{escape_html(error_str[:300])}</code>\n\n"
            "<b>What you can do:</b>\n"
            "• Try your request again\n"
            "• Rephrase your prompt if the problem persists"
        )

    if isinstance(error_obj, ClaudeSessionError):
        return (
            "🔄 <b>Session Error</b>\n\n"
            f"{escape_html(error_str)}\n\n"
            "<b>What you can do:</b>\n"
            "• Use /new to start a fresh session\n"
            "• Try your request again\n"
            "• Use /status to check your current session"
        )

    if isinstance(error_obj, ClaudeProcessError):
        return _format_process_error(error_str)

    # Any future ClaudeError subtypes not explicitly handled above —
    # preserve their existing message as-is rather than downgrading
    # to a generic "process error".
    if isinstance(error_obj, ClaudeError):
        safe_error = escape_html(error_str)
        if len(safe_error) > 500:
            safe_error = safe_error[:500] + "..."
        return (
            f"❌ <b>Claude Error</b>\n\n"
            f"{safe_error}\n\n"
            f"Try again or use /new to start a fresh session."
        )

    # --- Fall back to keyword matching (for string-only callers) --------
    # These patterns match the known error prefixes produced by
    # sdk_integration.py and facade.py, NOT arbitrary user content.

    error_lower = error_str.lower()

    if "usage limit reached" in error_lower or "usage limit" in error_lower:
        return error_str  # Already user-friendly

    if "tool not allowed" in error_lower:
        return error_str  # Already formatted by facade.py

    if "no conversation found" in error_lower:
        return (
            "🔄 <b>Session Not Found</b>\n\n"
            "The previous Claude session could not be found or has expired.\n\n"
            "<b>What you can do:</b>\n"
            "• Use /new to start a fresh session\n"
            "• Try your request again\n"
            "• Use /status to check your current session"
        )

    if "rate limit" in error_lower:
        return (
            "⏱️ <b>Rate Limit Reached</b>\n\n"
            "Too many requests in a short time period.\n\n"
            "<b>What you can do:</b>\n"
            "• Wait a moment before trying again\n"
            "• Use simpler requests\n"
            "• Check your current usage with /status"
        )

    if (
        "timed out after" in error_lower
        or "claude sdk timed out" in error_lower
        or "produced no output for" in error_lower
    ):
        return (
            "⏰ <b>Request Timeout</b>\n\n"
            f"{escape_html(error_str)}\n\n"
            "The agent went silent rather than simply taking a long time — "
            "work already written to disk survives, the reply does not.\n\n"
            "<b>What you can do:</b>\n"
            "• Ask what state the work is in before redoing it\n"
            "• Try again — transient stalls happen"
        )

    if "overloaded" in error_lower:
        return (
            "🏗️ <b>Claude is Overloaded</b>\n\n"
            "The Claude API is currently experiencing high demand.\n\n"
            "<b>What you can do:</b>\n"
            "• Wait a moment and try again\n"
            "• Shorter prompts may succeed more easily"
        )

    if "invalid api key" in error_lower or "authentication_error" in error_lower:
        return (
            "🔑 <b>API Authentication Error</b>\n\n"
            "The API key used to connect to Claude is invalid or expired.\n\n"
            "<b>What you can do:</b>\n"
            "• Ask the administrator to verify the "
            "<code>ANTHROPIC_API_KEY</code> setting\n"
            "• Check that the API key has not been revoked"
        )

    # Match known SDK prefixes: "Failed to connect to Claude: ..."
    # and "MCP server connection failed: ..."
    if error_lower.startswith("failed to connect to claude"):
        return (
            "🌐 <b>Connection Error</b>\n\n"
            f"Could not connect to Claude:\n"
            f"<code>{escape_html(error_str[:300])}</code>\n\n"
            "<b>What you can do:</b>\n"
            "• Check your network / firewall settings\n"
            "• Verify the Claude CLI is installed and accessible\n"
            "• Try again in a moment"
        )

    # Match known SDK prefix: "Claude Code not found. ..."
    if error_lower.startswith("claude code not found"):
        return (
            "🔍 <b>Claude CLI Not Found</b>\n\n"
            f"{escape_html(error_str)}\n\n"
            "<b>What you can do:</b>\n"
            "• Ensure Claude Code is installed: "
            "<code>npm install -g @anthropic-ai/claude-code</code>\n"
            "• Set the <code>CLAUDE_CLI_PATH</code> environment variable"
        )

    # Match known SDK prefixes: "MCP server error: ..." and
    # "MCP server connection failed: ..."
    if error_lower.startswith("mcp server"):
        return (
            "🔌 <b>MCP Server Error</b>\n\n"
            f"{escape_html(error_str)}\n\n"
            "<b>What you can do:</b>\n"
            "• Check that the MCP server is running\n"
            "• Verify MCP configuration\n"
            "• Ask the administrator to check MCP server logs"
        )

    # --- No match — show the raw error as-is ---
    safe_error = escape_html(error_str)
    if len(safe_error) > 500:
        safe_error = safe_error[:500] + "..."

    return f"❌ {safe_error}"


def _format_process_error(error_str: str) -> str:
    """Format a Claude process/SDK error with the actual details."""
    safe_error = escape_html(error_str)
    if len(safe_error) > 500:
        safe_error = safe_error[:500] + "..."

    return (
        f"❌ <b>Claude Process Error</b>\n\n"
        f"{safe_error}\n\n"
        "<b>What you can do:</b>\n"
        "• Try your request again\n"
        "• Use /new to start a fresh session if the problem persists\n"
        "• Check /status for current session state"
    )
