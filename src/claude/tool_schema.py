"""Explicit JSON Schema builder for SDK MCP tool declarations.

dv-9679 / dv-6f2a: ``@tool(name, description, {"a": str, "b": int})`` is the
SDK's dict shorthand, and ``_build_schema`` turns it into
``{"type": "object", "properties": {...}, "required": list(properties)}``
(``claude_agent_sdk/__init__.py``). **Every** declared key becomes required.

That leaves the model no way to express "I don't know this one". Faced with a
call it cannot make legally, it does not decline — it invents. The cascade is
on record twice in ``docs/FINDINGS.md`` (2026-08-19): three ``log_sleep``
calls in 5 ms — invalid JSON, then the correct minimal call, then surrender to
nine zeros, which then entered the averages as real measurements.

So tools declare their schema through :func:`tool_schema` instead, and
**required means "the handler rejects the call without it"** — read off the
handler's own guards, not off what is usually present. Keep the two in sync or
the model learns that the schema lies.

``tests/unit/test_claude/test_agent_tools_schemas.py`` enforces this
repo-wide: no ``@tool`` anywhere under ``src/`` may pass a bare dict.
"""

from __future__ import annotations

from typing import Any, Dict

_JSON_TYPES: Dict[type, str] = {
    str: "string",
    float: "number",
    int: "integer",
    bool: "boolean",
}


def tool_schema(props: Dict[str, type], *required: str) -> Dict[str, Any]:
    """Build a tool input schema where only ``required`` names are required.

    ``props`` maps parameter name to Python type; ``required`` names the
    subset the handler actually refuses to run without.

    The SDK passes a dict straight through only when it carries a string
    ``type`` and ``properties`` — which is exactly what this returns, so the
    ``required`` list below survives instead of being regenerated.
    """
    unknown = set(required) - set(props)
    if unknown:
        raise ValueError(f"required names undeclared params: {sorted(unknown)}")
    return {
        "type": "object",
        "properties": {k: {"type": _JSON_TYPES[v]} for k, v in props.items()},
        "required": list(required),
    }
