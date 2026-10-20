#!/usr/bin/env python3
"""Write conservative OpenRouter prices into config/llm_pricing.json (dv-d2f6).

    poetry run python scripts/gen_openrouter_pricing.py z-ai/glm-5.3 [slug ...]

OpenRouter routes one model across many providers (GLM 5.3: 46 endpoints,
input $0.06-$2.80/Mtok), so the price of a call is unknown until its
generation record exists (~6 s later). Until then the cost cap is checked
against tokens x the rule written here: the MAX over the model's endpoints
for every field, so the lag can never let a turn past its cap. A cache write
has no OpenRouter price field and is billed as input, so it gets the max
input price. Free public GET, no key needed. Re-run when prices move.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Dict, List

PRICING = Path(__file__).resolve().parent.parent / "config" / "llm_pricing.json"
ENDPOINTS_URL = "https://openrouter.ai/api/v1/models/{slug}/endpoints"


def fetch_endpoints(slug: str) -> List[Dict[str, Any]]:
    with urllib.request.urlopen(ENDPOINTS_URL.format(slug=slug), timeout=30) as r:
        return list(json.load(r)["data"]["endpoints"])


def conservative_rule(slug: str, endpoints: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Max per-Mtok price over ``endpoints`` for each field of a pricing rule."""
    if not endpoints:
        raise ValueError(f"{slug}: no endpoints")

    def top(field: str) -> float:
        values = [float(e["pricing"].get(field) or 0) for e in endpoints]
        return round(max(values) * 1e6, 6)

    prompt = top("prompt")
    write = max(prompt, top("input_cache_write"))
    return {
        "pattern": slug,
        "input": prompt,
        "output": top("completion"),
        "cache_read": top("input_cache_read") or prompt,
        "cache_write_5m": write,
        "cache_write_1h": write,
        "source": (
            f"openrouter max over {len(endpoints)} endpoints, "
            f"{datetime.now(UTC):%Y-%m-%d} (scripts/gen_openrouter_pricing.py)"
        ),
    }


def upsert(data: Dict[str, Any], rule: Dict[str, Any]) -> None:
    """Replace the rule with the same pattern, else insert before the first
    wildcard rule (first match wins, so exact slugs go first)."""
    rules = data.setdefault("rules", [])
    for i, existing in enumerate(rules):
        if existing.get("pattern") == rule["pattern"]:
            rules[i] = rule
            return
    for i, existing in enumerate(rules):
        if "*" in str(existing.get("pattern")) and "/" in str(existing.get("pattern")):
            rules.insert(i, rule)
            return
    rules.append(rule)


def render(data: Dict[str, Any]) -> str:
    """One rule per line, the file's existing layout."""
    lines = ["{", f'  "default": {json.dumps(data["default"])},', '  "rules": [']
    rules = data.get("rules", [])
    for i, rule in enumerate(rules):
        lines.append(f"    {json.dumps(rule)}{',' if i < len(rules) - 1 else ''}")
    lines += ["  ]", "}", ""]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("slugs", nargs="+")
    ap.add_argument("--path", type=Path, default=PRICING)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    data = json.loads(args.path.read_text())
    for slug in args.slugs:
        rule = conservative_rule(slug, fetch_endpoints(slug))
        print(json.dumps(rule))
        upsert(data, rule)
    if not args.dry_run:
        args.path.write_text(render(data))
        print(f"wrote {args.path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
