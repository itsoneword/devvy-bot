#!/usr/bin/env python3
"""Structural schema snapshot for a SQLite DB -> JSON (dv-5e4f Phase 0.1).

Pure SQLite introspection -- no dependency on this repo's ORM/manager
classes, so it works on bot.db, data.db, llm_analytics.db and
channel_intel.db alike, and on any legacy-init or prod-backup copy of them.

Per table: PRAGMA table_xinfo (columns incl. position/hidden), index_list +
index_info (incl. partial/unique), foreign_key_list, and the normalized
CREATE SQL from sqlite_master (whitespace-collapsed so formatting-only edits
don't show up as drift). Also triggers, views, and FTS5 tables (including
their shadow tables, which show up as ordinary tables -- that's
deliberate: Phase 0.1 wants the full structural truth; Phase 1's Alembic
`include_object` is the thing that later excludes them).

Opens the DB read-only (`mode=ro` URI) so this is safe to point at a live
WAL-mode database without taking a write lock; for a consistent read of a
*hot* prod DB, back it up first (sqlite3 backup API) and snapshot the copy --
see scripts/golden_replay.py.

Usage:
    poetry run python scripts/schema_snapshot.py <db_path>
    poetry run python scripts/schema_snapshot.py <db_path> -o out.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from src.storage.schema.structure import snapshot as _snapshot  # noqa: E402


def snapshot(db_path: Path) -> Dict[str, Any]:
    """Return a structural snapshot of ``db_path`` as a JSON-able dict."""
    return _snapshot(db_path)


def main(argv: list) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("db_path", type=Path)
    parser.add_argument("-o", "--output", type=Path, default=None)
    args = parser.parse_args(argv)

    if not args.db_path.exists():
        print(f"no such file: {args.db_path}", file=sys.stderr)
        return 1

    data = snapshot(args.db_path)
    text = json.dumps(data, indent=2, sort_keys=True) + "\n"

    if args.output:
        args.output.write_text(text)
        print(f"wrote {args.output}")
    else:
        print(text, end="")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
