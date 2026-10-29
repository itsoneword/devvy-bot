#!/usr/bin/env python3
"""Apply owner-ticked actions from a consolidation report (dv-4c6f, dv-cf07).

The stale-fact sweep (src/consolidation/service.py) closes superseded facts
itself and lists each closure as an ``UNDO`` line; this script executes
exactly the ``- [x]``-annotated lines of a ``report-*.md`` from
``DataLayout.consolidation_dir``
(``<root>/workspaces/devops/reports/consolidation/``, or ``data/consolidation/``
in the legacy layout). A bare ``--report`` file name is looked up there:

    - [x] UNDO scope=<scope> id=<id>
    - [x] MERGE scope=<scope> survivor=<id> close=<id>[,<id>...]
    - [x] EXPIRE scope=<scope> id=<id>

MERGE and EXPIRE are no longer generated (dv-cf07) but still accepted for
hand-written reports.

Semantics:
  - UNDO: reopen a row the sweep (or a MERGE) closed: status='active',
    invalid_at / superseded_by / invalidated_by cleared. Only rows with
    invalidated_by 'sweep' or 'consolidation' -- an owner's memory_forget
    stays forgotten (same rule as MemoryRepository.reopen_fact).
  - MERGE: the survivor row (one of the originals, verbatim — never
    model-composed) stays untouched; every ``close`` row is closed
    metadata-only: status='superseded', invalid_at=now,
    superseded_by=<survivor>, invalidated_by='consolidation'.
  - EXPIRE: status='expired' (invalid_at stays as it was). Never DELETE —
    expired rows stay reachable via recall(include_history).

Backup-first via the sqlite3 backup API (the sqlite3 CLI binary is absent on
this host) unless --no-backup. Single transaction: any error rolls back all.

Recommended: run against a copy first, diff, then against live (D17 allows
live apply without a service stop — repository writes are single
transactions).
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.utils.paths import data_layout  # noqa: E402

_ACTION_RE = re.compile(
    r"^\s*-\s*\[x\]\s+(UNDO|MERGE|EXPIRE)\s+(.+?)\s*$", re.IGNORECASE | re.MULTILINE
)


def _parse_kv(rest: str) -> dict[str, str]:
    return dict(pair.split("=", 1) for pair in rest.split() if "=" in pair)


def apply_report(conn: sqlite3.Connection, report_path: Path) -> None:
    text = report_path.read_text(encoding="utf-8")
    actions = _ACTION_RE.findall(text)
    if not actions:
        print("No approved ([x]) actions found; nothing to do.")
        return

    now = dt.datetime.now(dt.UTC).isoformat()
    cur = conn.cursor()
    cur.execute("BEGIN")
    try:
        for verb, rest in actions:
            kv = _parse_kv(rest)
            scope = kv["scope"]
            if verb.upper() == "UNDO":
                fid = int(kv["id"])
                changed = cur.execute(
                    "UPDATE memory_facts SET status='active', invalid_at=NULL, "
                    "superseded_by=NULL, invalidated_by=NULL, updated_at=? "
                    "WHERE id=? AND agent_id=? AND invalid_at IS NOT NULL "
                    "AND invalidated_by IN ('sweep', 'consolidation')",
                    (now, fid, scope),
                ).rowcount
                if not changed:
                    raise RuntimeError(
                        f"id {fid} in scope {scope} is not a sweep/consolidation "
                        "closure"
                    )
                print(f"  UNDO {scope}: {fid} reopened")
            elif verb.upper() == "MERGE":
                survivor = int(kv["survivor"])
                closing = [int(i) for i in kv["close"].split(",") if i]
                row = cur.execute(
                    "SELECT status FROM memory_facts WHERE id=? AND agent_id=?",
                    (survivor, scope),
                ).fetchone()
                if not row or row[0] != "active":
                    raise RuntimeError(
                        f"survivor {survivor} not active in scope {scope}"
                    )
                for fid in closing:
                    changed = cur.execute(
                        "UPDATE memory_facts SET status='superseded', "
                        "invalid_at=?, superseded_by=?, "
                        "invalidated_by='consolidation', updated_at=? "
                        "WHERE id=? AND agent_id=? AND status='active'",
                        (now, survivor, now, fid, scope),
                    ).rowcount
                    if not changed:
                        raise RuntimeError(f"id {fid} not active in scope {scope}")
                print(f"  MERGE {scope}: {closing} -> survivor {survivor}")
            else:  # EXPIRE
                fid = int(kv["id"])
                changed = cur.execute(
                    "UPDATE memory_facts SET status='expired', updated_at=? "
                    "WHERE id=? AND agent_id=? AND invalid_at IS NOT NULL",
                    (now, fid, scope),
                ).rowcount
                if not changed:
                    raise RuntimeError(
                        f"id {fid} in scope {scope} is not an invalid row"
                    )
                print(f"  EXPIRE {scope}: {fid}")
        conn.commit()
        print(f"Committed {len(actions)} action(s).")
    except Exception:
        conn.rollback()
        print("Rolled back — nothing was applied.")
        raise


def backup(db_path: str) -> Path:
    backups = data_layout().db_dir / "backups"
    backups.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%d-%H%M%S")
    dest = backups / f"bot-{stamp}.db"
    src = sqlite3.connect(db_path)
    dst = sqlite3.connect(dest)
    with dst:
        src.backup(dst)
    src.close()
    dst.close()
    print(f"Backup written: {dest}")
    return dest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--db", default=str(data_layout().bot_db))
    parser.add_argument("--no-backup", action="store_true")
    args = parser.parse_args()

    if not args.report.exists() and args.report.name == str(args.report):
        args.report = data_layout().consolidation_dir / args.report
    if not args.report.exists():
        sys.exit(f"report not found: {args.report}")
    if not args.no_backup:
        backup(args.db)
    conn = sqlite3.connect(args.db)
    apply_report(conn, args.report)


if __name__ == "__main__":
    main()
