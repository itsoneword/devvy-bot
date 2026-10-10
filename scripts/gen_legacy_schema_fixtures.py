#!/usr/bin/env python3
"""Generator for tests/fixtures/legacy_schema/*.json (dv-5e4f Phase 0.1).

The fixtures are the structural snapshot of the pre-Alembic schema, built by
the frozen legacy replay (src/storage/schema/legacy.py). That history no
longer changes, so neither should these files; this script stays only to
show (and re-check) where they came from. Schema changes go in a new Alembic
revision; tests/unit/test_storage/test_legacy_schema_fixtures.py compares a
fresh Alembic build against these structurally.

Usage:
    poetry run python scripts/gen_legacy_schema_fixtures.py
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from scripts.schema_snapshot import snapshot  # noqa: E402
from src.storage.schema.legacy import build_legacy  # noqa: E402

OUT_DIR = REPO / "tests" / "fixtures" / "legacy_schema"


async def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        for name in ("bot", "data", "llm_analytics", "channel_intel"):
            path = Path(tmp) / f"{name}.db"
            build_legacy(path, name)
            data = snapshot(path)
            out = OUT_DIR / f"{name}.json"
            import json

            out.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
            print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
