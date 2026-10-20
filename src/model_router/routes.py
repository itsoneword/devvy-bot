"""Which provider and model a profile's agent turn runs on (dv-d2f6 step 1).

Minimal slice of the route table in docs/analysis/openrouter-per-agent-config.md.
The harness is the program running the loop (always the Claude Code CLI here);
the provider is who serves and bills the model. A profile with no ``provider``
key resolves to the unchanged subscription route, so nothing moves until a
profile opts in.

Precedence for the model: a user's ``/model`` pick, only if it is in the
profile's ``allowed_models`` > the profile's ``model``. A pick outside the list
is ignored, never an error: the stored pick is per user, the allowed list is
per agent.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Mapping, Optional

from .types import CLAUDE_HARNESS

if TYPE_CHECKING:
    from src.profiles.registry import ProfileDefinition

PROVIDER_ANTHROPIC_SUB = "anthropic_sub"
PROVIDER_OPENROUTER = "openrouter"
VALID_PROVIDERS = frozenset({PROVIDER_OPENROUTER})

# OpenRouter's Anthropic Messages-compatible endpoint (their Claude Code guide).
OPENROUTER_ANTHROPIC_BASE_URL = "https://openrouter.ai/api"
OPENROUTER_KEY_SERVICE = "openrouter"
OPENROUTER_KEY_NAME = "OPENROUTER_API_KEY"

_PRICING_PATH = Path(__file__).resolve().parents[2] / "config" / "llm_pricing.json"


@dataclass(frozen=True)
class Route:
    harness: str
    provider: str
    model: Optional[str]

    @property
    def is_third_party(self) -> bool:
        """True when the CLI's own USD figure cannot be trusted."""
        return self.provider != PROVIDER_ANTHROPIC_SUB


def resolve_turn_route(
    profile: Optional["ProfileDefinition"], user_model: Optional[str]
) -> Route:
    """The route one chat turn on ``profile`` runs on.

    ``user_model`` is what the caller would pass today (a Claude tier or None).
    Subscription profiles get it back untouched.
    """
    provider = getattr(profile, "provider", None) if profile is not None else None
    if provider is None:
        return Route(CLAUDE_HARNESS, PROVIDER_ANTHROPIC_SUB, user_model)
    assert profile is not None
    allowed = profile.allowed_models
    if user_model and user_model in allowed:
        return Route(CLAUDE_HARNESS, provider, user_model)
    return Route(CLAUDE_HARNESS, provider, profile.model)


def user_pick_allowed(
    profile: Optional["ProfileDefinition"], pick: Optional[str]
) -> bool:
    """Whether ``/model`` may store ``pick`` while ``profile`` is active.

    A profile without ``allowed_models`` has no say (the backend policy decides).
    """
    if profile is None or not profile.allowed_models:
        return True
    return pick in profile.allowed_models


def openrouter_key() -> Optional[str]:
    """The OpenRouter key from the owner's key store (``/setkey openrouter``).

    The process env is scrubbed of secrets at startup, so the store is the
    source; the env is a fallback for dev runs.
    """
    from src.security.keystore import default_keystore

    store = default_keystore()
    if store is not None:
        value = store.env(OPENROUTER_KEY_SERVICE).get(OPENROUTER_KEY_NAME)
        if value:
            return value
    return os.environ.get(OPENROUTER_KEY_NAME) or None


def provider_env(route: Route, key: Optional[str]) -> Dict[str, str]:
    """Per-turn CLI env that points the Claude Code CLI at ``route``'s provider.

    {} for the subscription. For OpenRouter: base URL, the key as the auth
    token, an explicitly empty API key (so an inherited Anthropic key is not
    sent), and every tier alias mapped to the chosen slug so no side request
    falls back to an Anthropic model name the endpoint cannot serve.
    """
    if route.provider != PROVIDER_OPENROUTER:
        return {}
    if not key:
        raise RuntimeError(
            f"{OPENROUTER_KEY_NAME} is not in the key store: "
            f"/setkey {OPENROUTER_KEY_SERVICE} {OPENROUTER_KEY_NAME} <value>"
        )
    env = {
        "ANTHROPIC_BASE_URL": OPENROUTER_ANTHROPIC_BASE_URL,
        "ANTHROPIC_AUTH_TOKEN": key,
        "ANTHROPIC_API_KEY": "",
    }
    if route.model:
        for tier in ("OPUS", "SONNET", "HAIKU"):
            env[f"ANTHROPIC_DEFAULT_{tier}_MODEL"] = route.model
        env["CLAUDE_CODE_SUBAGENT_MODEL"] = route.model
    return env


def pricing_rule_for(
    model: Optional[str], pricing_path: Path = _PRICING_PATH
) -> Optional[str]:
    """The ``llm_pricing.json`` pattern that prices ``model``; None if only
    the Anthropic default would. The profile loader requires one for every
    provider route, so a cap on that route is never enforced on a guess."""
    from src.llm_analytics.pricing import PricingTable

    if not model:
        return None
    _, rule = PricingTable.load(pricing_path).match(model)
    return None if rule == "default" else rule


def cost_for(
    route: Route,
    *,
    model: Optional[str],
    usage: Mapping[str, Any],
    exact: Optional[float] = None,
    pricing_path: Path = _PRICING_PATH,
) -> Optional[float]:
    """USD for ONE model call on ``route``: the one cost function (dv-d2f6).

    ``exact`` (OpenRouter's ``total_cost`` for the call's generation) wins.
    Otherwise tokens x ``config/llm_pricing.json``: the subscription prices
    the call's own ``model`` at Anthropic list price (what the CLI's
    ``total_cost_usd`` does, 5m/1h cache writes split); a third-party route
    prices ``route.model`` at its conservative rule (max over the provider's
    endpoints, ``scripts/gen_openrouter_pricing.py``). None = no rule for a
    third-party model: unknown is not zero.

    ``usage`` is an Anthropic usage block (a stream-json ``assistant``
    event's ``message.usage``, or a ResultMessage's).
    """
    from src.llm_analytics.pricing import PricingTable

    if exact is not None:
        return float(exact)
    price_model = route.model if route.is_third_party else model
    table = PricingTable.load(pricing_path)
    rates, rule = table.match(price_model or "")
    if rule == "default" and route.is_third_party:
        return None
    split = usage.get("cache_creation") or {}
    return table.cost(
        rates,
        input_tokens=_int(usage.get("input_tokens")),
        output_tokens=_int(usage.get("output_tokens")),
        cache_read_tokens=_int(usage.get("cache_read_input_tokens")),
        cache_5m_tokens=_int(split.get("ephemeral_5m_input_tokens")),
        cache_1h_tokens=_int(split.get("ephemeral_1h_input_tokens")),
        cache_creation_tokens=_int(usage.get("cache_creation_input_tokens")),
    )


def cost_from_tokens(
    model: Optional[str],
    *,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
    pricing_path: Path = _PRICING_PATH,
) -> Optional[float]:
    """Turn-level token sums priced on a third-party ``model``'s rule.

    Thin wrapper over :func:`cost_for` for a turn whose per-call events were
    not seen; None when no rule matches the model.
    """
    if not model:
        return None
    return cost_for(
        Route(CLAUDE_HARNESS, PROVIDER_OPENROUTER, model),
        model=model,
        usage={
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_input_tokens": cache_read_tokens,
            "cache_creation_input_tokens": cache_creation_tokens,
        },
        pricing_path=pricing_path,
    )


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


__all__ = [
    "OPENROUTER_ANTHROPIC_BASE_URL",
    "PROVIDER_ANTHROPIC_SUB",
    "PROVIDER_OPENROUTER",
    "Route",
    "VALID_PROVIDERS",
    "cost_for",
    "cost_from_tokens",
    "openrouter_key",
    "pricing_rule_for",
    "provider_env",
    "resolve_turn_route",
    "user_pick_allowed",
]
