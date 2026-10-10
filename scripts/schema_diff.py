#!/usr/bin/env python3
"""Structural schema diff for the bot's SQLite DBs (dv-5e4f Phase 1).

Compares two schemas column by column (position, name, declared type,
NOT NULL, default, PK), plus AUTOINCREMENT, CHECKs, FKs, indexes, and the
SQL of FTS5 tables, triggers and views. CREATE TABLE text is not compared
verbatim (a table grown by ALTER and the same table created in one go are
equal). ``schema_version`` / ``alembic_version`` are ignored.

Each side is a DB file, a snapshot JSON (tests/fixtures/legacy_schema/*.json),
or one of two references built in a temp dir:

    fresh   -- a new DB built by Alembic (what a fresh install gets)
    legacy  -- the frozen pre-Alembic replay (src/storage/schema/legacy.py)

DB files are opened read-only. For a live DB, diff a backup copy instead
(sqlite3 backup API) to get a consistent read.

Usage:
    poetry run python scripts/schema_diff.py data/db/bot.db fresh --db bot
    poetry run python scripts/schema_diff.py a.db b.db
    poetry run python scripts/schema_diff.py fresh legacy --db channel_intel
    poetry run python scripts/schema_diff.py --dir /path/to/copies   # all 4 vs fresh

Exit 0 = structurally equal, 1 = differences (printed), 2 = usage error.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from src.storage.schema.legacy import build_legacy  # noqa: E402
from src.storage.schema.migrate import DB_NAMES, ensure_schema  # noqa: E402
from src.storage.schema.structure import diff, snapshot  # noqa: E402


def load(side: str, db_name: Optional[str], tmp: Path) -> Dict[str, Any]:
    if side in ("fresh", "legacy"):
        if db_name not in DB_NAMES:
            raise SystemExit(f"'{side}' needs --db one of {', '.join(DB_NAMES)}")
        path = tmp / side / f"{db_name}.db"
        path.parent.mkdir(parents=True, exist_ok=True)
        if side == "fresh":
            ensure_schema(db_name, path)
        else:
            build_legacy(path, db_name)
        return snapshot(path)
    p = Path(side)
    if not p.exists():
        raise SystemExit(f"no such file: {p}")
    if p.suffix == ".json":
        data: Dict[str, Any] = json.loads(p.read_text())
        return data
    return snapshot(p)


def compare(left: str, right: str, db_name: Optional[str]) -> int:
    with tempfile.TemporaryDirectory() as td:
        a = load(left, db_name, Path(td))
        b = load(right, db_name, Path(td))
    problems = diff(a, b, left, right)
    label = f"[{db_name}] " if db_name else ""
    if problems:
        print(f"{label}{left} vs {right}: {len(problems)} difference(s)")
        for line in problems:
            print(f"  {line}")
        return 1
    print(f"{label}{left} vs {right}: structurally equal")
    return 0


def main(argv: list) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("left", nargs="?")
    parser.add_argument("right", nargs="?")
    parser.add_argument("--db", choices=DB_NAMES, help="which DB (for fresh/legacy)")
    parser.add_argument(
        "--dir", type=Path, help="compare <dir>/<db>.db against fresh, all 4 DBs"
    )
    args = parser.parse_args(argv)

    if args.dir:
        rc = 0
        for name in DB_NAMES:
            path = args.dir / f"{name}.db"
            if not path.exists():
                print(f"[{name}] {path}: missing, skipped")
                continue
            rc |= compare(str(path), "fresh", name)
        return rc
    if not (args.left and args.right):
        parser.print_usage()
        return 2
    return compare(args.left, args.right, args.db)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
