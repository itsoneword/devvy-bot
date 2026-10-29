"""One JSON contract for every plain-call route (dv-781f phase 2).

``CallRequest.json_schema`` is the caller's real contract: any root type,
optional keys allowed. Adapters disagree on what they accept, so the router
translates here and nowhere else:

- before the adapter, :func:`native_schema`: a non-object root is wrapped
  under one key (every structured-output API wants an object root), and for
  OpenAI-style strict mode (Codex, OpenRouter) every object is closed and
  every key required, an optional key becoming nullable (the dv-0c01 rule);
- after the adapter, :func:`canonical`: strip a ``` fence, parse, unwrap,
  drop a null optional key (Claude omits it, strict mode sends null: the
  same answer must be the same text), validate, dump.

A route in ``validate`` mode (``json:`` in ``models.yaml``; built-in for
Claude extraction) sends no schema and keeps the caller's prompt-only JSON.
Its answer is parsed, unwrapped and root-checked here; the item contract
stays with the caller's own lenient validator (the extraction prompt offers
tier "core", which the strict schema has no room for).

The validator covers the subset the repo's schemas use: ``type`` (unions,
``null``), ``properties``, ``required``, ``enum``,
``additionalProperties: false``, ``items``, ``minimum`` / ``maximum``.
"""

from __future__ import annotations

import copy
import json
from typing import Any, Dict, List, Optional

from .types import CODEX_HARNESS, OPENROUTER_HARNESS, ModelCallError

JSON_NATIVE = "native"
JSON_VALIDATE = "validate"
JSON_MODES = frozenset({JSON_NATIVE, JSON_VALIDATE})

# Harnesses whose structured output is OpenAI strict mode.
STRICT_HARNESSES = frozenset({CODEX_HARNESS, OPENROUTER_HARNESS})

# The key a non-object root is wrapped under.
WRAP_KEY = "value"

SCHEMA_KEYWORDS = frozenset(
    {
        "type",
        "properties",
        "required",
        "enum",
        "additionalProperties",
        "items",
        "minimum",
        "maximum",
    }
)


class InvalidJSON(ModelCallError):
    """The answer is not JSON for the caller's schema. Retried once."""


def is_wrapped(schema: Dict[str, Any]) -> bool:
    return schema.get("type") != "object"


def native_schema(schema: Dict[str, Any], harness: str) -> Dict[str, Any]:
    """The schema ``harness``'s adapter receives for ``schema``.

    Never mutates ``schema``.
    """
    body = copy.deepcopy(schema)
    if harness in STRICT_HARNESSES:
        body = _strict(body)
    if not is_wrapped(schema):
        return body
    return {
        "type": "object",
        "properties": {WRAP_KEY: body},
        "required": [WRAP_KEY],
        "additionalProperties": False,
    }


def _types(node: Dict[str, Any]) -> List[str]:
    value = node.get("type")
    if value is None:
        return []
    return list(value) if isinstance(value, list) else [value]


def _nullable(node: Dict[str, Any]) -> Dict[str, Any]:
    types = _types(node)
    if not types:
        return {"anyOf": [node, {"type": "null"}]}
    if "null" not in types:
        node["type"] = types + ["null"]
    if "enum" in node and None not in node["enum"]:
        node["enum"] = list(node["enum"]) + [None]
    return node


def _strict(node: Any) -> Any:
    """OpenAI strict mode: every object closed, every key required; a key
    that was optional is nullable instead."""
    if not isinstance(node, dict):
        return node
    if "object" in _types(node):
        properties = node.get("properties") or {}
        required = set(node.get("required") or [])
        strict_props: Dict[str, Any] = {}
        for key, sub in properties.items():
            sub = _strict(sub)
            strict_props[key] = sub if key in required else _nullable(sub)
        node["properties"] = strict_props
        node["required"] = list(strict_props)
        node["additionalProperties"] = False
    if "items" in node:
        node["items"] = _strict(node["items"])
    return node


def canonical(text: str, schema: Dict[str, Any], *, validate: bool = True) -> str:
    """The answer as canonical JSON text for ``schema``; :class:`InvalidJSON`
    when it is not. ``validate=False`` checks the root type only."""
    value = parse(text)
    if is_wrapped(schema) and isinstance(value, dict) and set(value) == {WRAP_KEY}:
        value = value[WRAP_KEY]
    value = _drop_null_optionals(value, schema)
    if validate:
        errors = errors_for(value, schema)
    else:
        errors = errors_for(value, {"type": schema["type"]}) if "type" in schema else []
    if errors:
        raise InvalidJSON(f"invalid json: {errors[0]}")
    return json.dumps(value, ensure_ascii=False)


def parse(text: str) -> Any:
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        first, _, rest = cleaned.partition("\n")
        cleaned = rest.rsplit("```", 1)[0].strip() if rest else ""
    try:
        return json.loads(cleaned)
    except ValueError as exc:
        raise InvalidJSON(f"invalid json: not JSON ({exc}): {cleaned[:120]!r}")


def _drop_null_optionals(value: Any, schema: Dict[str, Any]) -> Any:
    """Drop null optional keys and order keys as the schema lists them."""
    if isinstance(value, dict) and isinstance(schema.get("properties"), dict):
        properties = schema["properties"]
        required = set(schema.get("required") or [])
        out: Dict[str, Any] = {}
        ordered = [k for k in properties if k in value] + [
            k for k in value if k not in properties
        ]
        for key in ordered:
            item = value[key]
            if item is None and key not in required:
                continue
            sub = properties.get(key)
            out[key] = _drop_null_optionals(item, sub) if sub else item
        return out
    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        return [_drop_null_optionals(v, schema["items"]) for v in value]
    return value


_TYPE_CHECKS = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
}


def errors_for(value: Any, schema: Dict[str, Any], path: str = "$") -> List[str]:
    """Where ``value`` breaks ``schema`` (empty = valid)."""
    if "anyOf" in schema:
        if any(not errors_for(value, s, path) for s in schema["anyOf"]):
            return []
        return [f"{path}: matches none of anyOf"]
    types = _types(schema)
    if types and not any(_TYPE_CHECKS[t](value) for t in types if t in _TYPE_CHECKS):
        return [f"{path}: expected {'|'.join(types)}, got {type(value).__name__}"]
    if "enum" in schema and value not in schema["enum"]:
        return [f"{path}: {value!r} not one of {schema['enum']}"]
    errors: List[str] = []
    if isinstance(value, dict):
        properties = schema.get("properties") or {}
        for key in schema.get("required") or []:
            if key not in value:
                errors.append(f"{path}: missing {key!r}")
        if schema.get("additionalProperties") is False:
            for key in value:
                if key not in properties:
                    errors.append(f"{path}: unexpected key {key!r}")
        for key, sub in properties.items():
            if key in value:
                errors += errors_for(value[key], sub, f"{path}.{key}")
    elif isinstance(value, list) and isinstance(schema.get("items"), dict):
        for i, item in enumerate(value):
            errors += errors_for(item, schema["items"], f"{path}[{i}]")
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: {value} < {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}: {value} > {schema['maximum']}")
    return errors


def unsupported_keywords(schema: Any, path: str = "$") -> List[str]:
    """Schema keywords outside the subset :func:`errors_for` checks."""
    found: List[str] = []
    if isinstance(schema, dict):
        for key, sub in schema.items():
            if key not in SCHEMA_KEYWORDS and key != "anyOf":
                found.append(f"{path}.{key}")
            if key == "properties" and isinstance(sub, dict):
                for name, prop in sub.items():
                    found += unsupported_keywords(prop, f"{path}.{name}")
            elif key == "items":
                found += unsupported_keywords(sub, f"{path}[]")
            elif key == "anyOf" and isinstance(sub, list):
                for i, s in enumerate(sub):
                    found += unsupported_keywords(s, f"{path}.anyOf{i}")
    return found


def retry_prompt(prompt: str, error: str, schema: Optional[Dict[str, Any]]) -> str:
    """The JSON retry: the same prompt, plus why the last answer failed."""
    tail = (
        f"\n\nYour previous reply was not valid JSON for the required schema "
        f"({error[:200]}). Reply with JSON only"
    )
    if schema is not None:
        tail += f", matching this JSON Schema: {json.dumps(schema)}"
    return prompt + tail + "."
