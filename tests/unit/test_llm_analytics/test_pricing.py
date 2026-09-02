"""Pricing matcher and cost-formula tests."""

import json
from pathlib import Path

import pytest

from src.llm_analytics.pricing import PricingTable, Rates

CONFIG = {
    "default": {
        "input": 5.0,
        "output": 25.0,
        "cache_read": 0.50,
        "cache_write_5m": 6.25,
        "cache_write_1h": 10.0,
    },
    "rules": [
        {
            "pattern": "claude-fable-5*",
            "input": 10.0,
            "output": 50.0,
            "cache_read": 1.00,
            "cache_write_5m": 12.50,
            "cache_write_1h": 20.0,
        },
        {
            "pattern": "claude-opus-*",
            "input": 5.0,
            "output": 25.0,
            "cache_read": 0.50,
            "cache_write_5m": 6.25,
            "cache_write_1h": 10.0,
        },
    ],
}


@pytest.fixture()
def table(tmp_path: Path) -> PricingTable:
    path = tmp_path / "pricing.json"
    path.write_text(json.dumps(CONFIG))
    return PricingTable.load(path)


class TestMatch:
    def test_first_match_wins(self, table: PricingTable) -> None:
        rates, rule = table.match("claude-fable-5")
        assert rule == "claude-fable-5*"
        assert rates.input == 10.0

    def test_wildcard_suffix(self, table: PricingTable) -> None:
        _, rule = table.match("claude-opus-4-1-20250805")
        assert rule == "claude-opus-*"

    def test_unknown_model_falls_to_default(self, table: PricingTable) -> None:
        rates, rule = table.match("gpt-oss-120b")
        assert rule == "default"
        assert rates.output == 25.0

    def test_hash_changes_with_content(self, tmp_path: Path) -> None:
        p1 = tmp_path / "a.json"
        p2 = tmp_path / "b.json"
        p1.write_text(json.dumps(CONFIG))
        changed = dict(CONFIG)
        changed["default"] = dict(CONFIG["default"], input=6.0)  # type: ignore[call-overload,arg-type]
        p2.write_text(json.dumps(changed))
        assert PricingTable.load(p1).config_hash != PricingTable.load(p2).config_hash


class TestCost:
    def test_basic_formula(self) -> None:
        rates = Rates(
            input=10.0,
            output=50.0,
            cache_read=1.0,
            cache_write_5m=12.5,
            cache_write_1h=20.0,
        )
        cost = PricingTable.cost(
            rates,
            input_tokens=1_000_000,
            output_tokens=1_000_000,
            cache_read_tokens=1_000_000,
            cache_5m_tokens=1_000_000,
            cache_1h_tokens=1_000_000,
            cache_creation_tokens=2_000_000,
        )
        assert cost == pytest.approx(10.0 + 50.0 + 1.0 + 12.5 + 20.0)

    def test_uncovered_cache_creation_billed_at_5m(self) -> None:
        rates = Rates(
            input=0.0,
            output=0.0,
            cache_read=0.0,
            cache_write_5m=10.0,
            cache_write_1h=0.0,
        )
        cost = PricingTable.cost(
            rates,
            input_tokens=0,
            output_tokens=0,
            cache_read_tokens=0,
            cache_5m_tokens=100_000,
            cache_1h_tokens=0,
            cache_creation_tokens=300_000,  # 200k remainder -> 5m rate
        )
        assert cost == pytest.approx(3.0)
