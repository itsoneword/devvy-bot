"""Pricing table: fnmatch rules, first-match-wins, default fallback.

Costs are estimates only (host runs on subscription OAuth, no real billing).
Unknown models never fail indexing — they price at 'default' and the UI can
flag pricing_rule='default'.
"""

import hashlib
import json
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Rates:
    """USD per Mtok."""

    input: float
    output: float
    cache_read: float
    cache_write_5m: float
    cache_write_1h: float


def _rates(raw: dict[str, Any]) -> Rates:
    return Rates(
        input=float(raw["input"]),
        output=float(raw["output"]),
        cache_read=float(raw["cache_read"]),
        cache_write_5m=float(raw["cache_write_5m"]),
        cache_write_1h=float(raw["cache_write_1h"]),
    )


class PricingTable:
    def __init__(
        self, default: Rates, rules: list[tuple[str, Rates]], config_hash: str
    ) -> None:
        self._default = default
        self._rules = rules
        self.config_hash = config_hash

    @classmethod
    def load(cls, path: Path) -> "PricingTable":
        raw = path.read_bytes()
        data = json.loads(raw)
        default = _rates(data["default"])
        rules = [(str(r["pattern"]), _rates(r)) for r in data.get("rules", [])]
        return cls(default, rules, hashlib.sha256(raw).hexdigest())

    def match(self, model: str) -> tuple[Rates, str]:
        """Return (rates, rule) where rule is the matched pattern or 'default'."""
        for pattern, rates in self._rules:
            if fnmatchcase(model, pattern):
                return rates, pattern
        return self._default, "default"

    @staticmethod
    def cost(
        rates: Rates,
        *,
        input_tokens: int,
        output_tokens: int,
        cache_read_tokens: int,
        cache_5m_tokens: int,
        cache_1h_tokens: int,
        cache_creation_tokens: int,
    ) -> float:
        """Estimated USD. Cache-creation not covered by the 5m/1h split is
        billed at the 5m rate per the plan."""
        remainder = cache_creation_tokens - cache_5m_tokens - cache_1h_tokens
        if remainder < 0:
            remainder = 0
        return (
            input_tokens * rates.input
            + output_tokens * rates.output
            + cache_read_tokens * rates.cache_read
            + (cache_5m_tokens + remainder) * rates.cache_write_5m
            + cache_1h_tokens * rates.cache_write_1h
        ) / 1e6
