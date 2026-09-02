#!/usr/bin/env python3
"""One-time-able sweep closing episodic user_facts in a memory scope (dv-7517
step 7; generalizes the manual 2026-08-14 procedure that produced
invalidated_by='cleanup-episodic-2026-08-14').

Classification is the SAME deterministic guard the evaluator pipeline now
runs (src/evaluator/episodic_guard.py), so the sweep and the inflow filter
can never disagree. Run it AFTER the guard is live, or the swept scope
refills.

Actions proposed:
  - CLOSE: active general user_facts classified episodic -> metadata-only
    close (status='superseded', invalid_at=now, invalidated_by=<marker>).
    Never DELETE; rows stay reachable via recall(include_history).
  - FLIP: active user_facts over MAX_INJECTABLE_FACT_CHARS (600) -> kind
    flipped to 'conversation_result' IN PLACE (owner decision 2026-08-15 on
    the oversized-analysis fact): injected from then on as a one-line
    pointer, full text intact, history intact. Label = first content line
    when the row has none.
  - Product-reference rows are ALWAYS preserved (dv-5f3a seeds the products
    table from them; they are closed only after that seeding).
  - Pinned rows are never touched.

Dry-run by default (read-only connection): prints every proposed CLOSE and
FLIP **and every preserved row** for owner review. --apply performs the
writes in a single transaction, backup-first (sqlite3 backup API — the CLI
binary is absent on this host), refusing to overwrite an existing backup.

Rollback: restore the backup file. No service restart needed; effects appear
at the next window roll — verify via the bundle_chars log line
(facts_count/total_chars) after rolling the window.
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.evaluator.episodic_guard import classify  # noqa: E402
from src.evaluator.writer import MAX_INJECTABLE_FACT_CHARS  # noqa: E402
from src.storage.memory_repository import truncate_at_word  # noqa: E402

_PRODUCT_REF = re.compile(r"(?i)\[product-ref\]|product\ reference")

# ~230 rendered chars per fact line measured in the 2026-08-14 audit; used
# only for the before/after chars estimate printed at the end.
_RENDER_OVERHEAD = 11  # "[dd.mm.yy] " prefix


def plan(
    conn: sqlite3.Connection, scope: str, keep_full: set[int] | None = None
) -> dict:
    keep_full = keep_full or set()
    rows = conn.execute(
        "SELECT id, tier, kind, label, content, valid_from FROM memory_facts "
        "WHERE agent_id=? AND status='active' AND invalid_at IS NULL "
        "ORDER BY id",
        (scope,),
    ).fetchall()

    close, flip, keep = [], [], []
    for fid, tier, kind, label, content, valid_from in rows:
        if kind != "user_fact":
            continue
        if tier == "pinned":
            keep.append((fid, valid_from, "pinned — never touched", content))
            continue
        if _PRODUCT_REF.search(content):
            keep.append((fid, valid_from, "product-ref (dv-5f3a seed)", content))
            continue
        reason = classify(content, tier=tier)
        if reason is not None:
            close.append((fid, valid_from, reason, content))
        elif len(content) > MAX_INJECTABLE_FACT_CHARS:
            if fid in keep_full:
                keep.append((fid, valid_from, "--keep-full override", content))
            else:
                flip.append((fid, valid_from, label, content))
        else:
            keep.append((fid, valid_from, "durable", content))
    return {"close": close, "flip": flip, "keep": keep}


def report(actions: dict, scope: str) -> None:
    def line(fid, date, tag, content, width=160):
        date = (date or "?")[:10]
        flat = " / ".join(content.splitlines())
        print(f"  {fid:>5} | {date} | {tag} | {flat[:width]}")

    print(f"== PROPOSED CLOSURES ({len(actions['close'])}) — scope {scope} ==")
    for fid, date, reason, content in actions["close"]:
        line(fid, date, reason, content)
    print(
        f"\n== PROPOSED KIND-FLIPS to conversation_result ({len(actions['flip'])}) =="
    )
    for fid, date, label, content in actions["flip"]:
        line(fid, date, f"{len(content)} chars, label={label!r}", content)
    print(f"\n== PRESERVED ({len(actions['keep'])}) ==")
    for fid, date, why, content in actions["keep"]:
        line(fid, date, why, content, width=120)

    kept_chars = (
        sum(len(c) + _RENDER_OVERHEAD for _, _, _, c in actions["keep"])
        + len(actions["flip"]) * 130
    )  # flipped rows render as ~130-char pointers
    removed_chars = sum(len(c) + _RENDER_OVERHEAD for _, _, _, c in actions["close"])
    removed_chars += sum(len(c) - 130 for _, _, _, c in actions["flip"])
    print(
        f"\nSummary: {len(actions['close'])} close, {len(actions['flip'])} flip, "
        f"{len(actions['keep'])} keep. Rendered facts estimate: "
        f"~{kept_chars + removed_chars} -> ~{kept_chars} chars."
    )


def backup(db_path: str, dest: Path) -> None:
    if dest.exists():
        sys.exit(f"refusing to overwrite existing backup: {dest}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(db_path)
    dst = sqlite3.connect(dest)
    with dst:
        src.backup(dst)
    src.close()
    dst.close()
    print(f"Backup written: {dest}")


def apply(conn: sqlite3.Connection, scope: str, actions: dict, marker: str) -> None:
    now = dt.datetime.now(dt.UTC).isoformat()
    cur = conn.cursor()
    cur.execute("BEGIN")
    try:
        for fid, _, _, _ in actions["close"]:
            changed = cur.execute(
                "UPDATE memory_facts SET status='superseded', invalid_at=?, "
                "invalidated_by=?, updated_at=? "
                "WHERE id=? AND agent_id=? AND status='active'",
                (now, marker, now, fid, scope),
            ).rowcount
            if not changed:
                raise RuntimeError(f"id {fid} not active in scope {scope}")
        for fid, _, label, content in actions["flip"]:
            new_label = label or truncate_at_word(content.strip().splitlines()[0])
            changed = cur.execute(
                "UPDATE memory_facts SET kind='conversation_result', label=?, "
                "updated_at=? WHERE id=? AND agent_id=? AND status='active' "
                "AND kind='user_fact'",
                (new_label, now, fid, scope),
            ).rowcount
            if not changed:
                raise RuntimeError(f"id {fid} not an active user_fact in {scope}")
        conn.commit()
        print(
            f"Committed: {len(actions['close'])} closed ({marker}), "
            f"{len(actions['flip'])} flipped."
        )
    except Exception:
        conn.rollback()
        print("Rolled back — nothing was applied.")
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scope", default="coach#u111111111")
    parser.add_argument("--db", default="data/bot.db")
    parser.add_argument("--apply", action="store_true", help="write (default: dry-run)")
    parser.add_argument(
        "--no-flip",
        action="store_true",
        help="closures only; leave oversized user_facts as they are",
    )
    parser.add_argument(
        "--backup-path",
        type=Path,
        default=None,
        help="backup destination (default ../backups/bot.db.pre-episodic-<date>)",
    )
    parser.add_argument(
        "--keep-full",
        type=int,
        nargs="+",
        default=[],
        metavar="ID",
        help="fact ids to exclude from the >600-char kind-flip (kept as full facts)",
    )
    args = parser.parse_args()
    keep_full = set(args.keep_full)

    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%d")
    marker = f"cleanup-episodic-{dt.datetime.now(dt.UTC).strftime('%Y-%m-%d')}"

    if not args.apply:
        conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
        actions = plan(conn, args.scope, keep_full)
        if args.no_flip:
            actions["flip"] = []
        report(actions, args.scope)
        print("\nDry-run only. Re-run with --apply to write (backup-first).")
        return

    dest = args.backup_path or Path(
        Path(args.db).resolve().parent.parent.parent,
        "backups",
        f"bot.db.pre-episodic-{stamp}",
    )
    backup(args.db, dest)
    conn = sqlite3.connect(args.db)
    actions = plan(conn, args.scope, keep_full)
    if args.no_flip:
        actions["flip"] = []
    report(actions, args.scope)
    apply(conn, args.scope, actions, marker)


if __name__ == "__main__":
    main()
