#!/usr/bin/env python3
"""Compare the classic extraction with the Jev screen's shadow run (dv-7e11).

Reads ``memory_extractions`` rows that carry ``op_stats.screen`` (written by
``MEMORY_SCREEN_MODE=shadow``) over a date range, read-only, and prints:

* run outcome: how many rows, chunks ok / Jev failed / Sonnet failed;
* fact selection: facts sent vs all active facts (count and chars);
* recall: of the existing facts the classic run acted on (supersede targets,
  invalidations, duplicate/contradicts verdict targets), how many were in the
  shadow's sent set; the misses are the stale-fact risk;
* op agreement: classic adds/supersedes matched to the shadow's by text
  similarity, plus ops only one side proposed;
* cost: Jev + shadow Sonnet vs the classic Sonnet runs (``model_calls``).

Usage:
    poetry run python scripts/screen_shadow_report.py --since 2026-10-18
    poetry run python scripts/screen_shadow_report.py --since 2026-10-18 \
        --until 2026-11-01 --agent devops -v

Reading it: ``recall`` is the headline (target >= 95%). ``only classic`` ops
are what the targeted path would have missed or phrased differently; ``only
screen`` ops are extra adds (often duplicates the writer would have dropped)
or updates the classic reconciler missed. Both lists print with -v.
"""

from __future__ import annotations

import argparse
import difflib
import json
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.utils.paths import data_layout  # noqa: E402

MATCH_RATIO = 0.6
CONTENT_OPS = ("add", "supersede")


def load_rows(
    db: sqlite3.Connection, since: str, until: str, agent: Optional[str]
) -> List[Dict[str, Any]]:
    rows = []
    for rid, ts, status, insights, targets, stats in db.execute(
        "SELECT id, timestamp, validation_status, parsed_insights, target_files, "
        "op_stats FROM memory_extractions "
        "WHERE op_stats LIKE '%\"screen\"%' AND date(timestamp) >= ? "
        "AND date(timestamp) <= ? ORDER BY id",
        (since, until),
    ):
        try:
            op_stats = json.loads(stats)
            if "screen" not in op_stats:
                continue
            old_ops = json.loads(insights) if insights else []
            scopes = json.loads(targets) if targets else []
        except (TypeError, ValueError):
            continue
        if agent and agent not in scopes:
            continue
        rows.append(
            {
                "id": rid,
                "ts": ts,
                "scopes": scopes,
                "old_ops": old_ops,
                "op_stats": op_stats,
                "screen": op_stats["screen"],
            }
        )
    return rows


def classic_refs(row: Dict[str, Any]) -> Set[int]:
    """Existing fact ids the classic run acted on."""
    refs: Set[int] = set()
    for op in row["old_ops"]:
        for key in ("supersedes_id", "target_id"):
            if isinstance(op.get(key), int):
                refs.add(op[key])
    for verdict in row["op_stats"].get("verdicts") or []:
        if verdict.get("verdict") in ("duplicate", "contradicts") and isinstance(
            verdict.get("existing_id"), int
        ):
            refs.add(verdict["existing_id"])
    return refs


def ok_chunks(row: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [c for c in row["screen"].get("chunks", []) if c.get("status") == "ok"]


def sent_ids(row: Dict[str, Any]) -> Set[int]:
    return {s["id"] for c in ok_chunks(row) for s in c.get("selected", [])}


def new_ops(row: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [op for c in ok_chunks(row) for op in c.get("ops", [])]


def match_ops(
    old: Sequence[Dict[str, Any]], new: Sequence[Dict[str, Any]]
) -> Tuple[List[Tuple[Dict, Dict]], List[Dict], List[Dict]]:
    """Greedy best-first pairing of content ops by text similarity."""
    old_c = [o for o in old if o.get("op") in CONTENT_OPS]
    new_c = [o for o in new if o.get("op") in CONTENT_OPS]
    pairs = sorted(
        (
            (
                difflib.SequenceMatcher(
                    None, str(a.get("content", "")), str(b.get("content", ""))
                ).ratio(),
                i,
                j,
            )
            for i, a in enumerate(old_c)
            for j, b in enumerate(new_c)
        ),
        reverse=True,
    )
    used_old: Set[int] = set()
    used_new: Set[int] = set()
    matched: List[Tuple[Dict, Dict]] = []
    for ratio, i, j in pairs:
        if ratio < MATCH_RATIO:
            break
        if i in used_old or j in used_new:
            continue
        used_old.add(i)
        used_new.add(j)
        matched.append((old_c[i], new_c[j]))
    only_old = [o for i, o in enumerate(old_c) if i not in used_old]
    only_new = [o for j, o in enumerate(new_c) if j not in used_new]
    return matched, only_old, only_new


def call_costs(
    db: sqlite3.Connection, since: str, until: str
) -> Dict[str, Tuple[int, float]]:
    out: Dict[str, Tuple[int, float]] = {}
    try:
        cursor = db.execute(
            "SELECT purpose, COUNT(*), COALESCE(SUM(cost_usd), 0) FROM model_calls "
            "WHERE purpose IN ('extraction','reconcile','screen','screen_extraction') "
            "AND date(ts) >= ? AND date(ts) <= ? GROUP BY purpose",
            (since, until),
        )
        out = {p: (n, float(c)) for p, n, c in cursor}
    except sqlite3.Error:
        pass
    return out


def _pct(a: float, b: float) -> str:
    return f"{100 * a / b:.0f}%" if b else "n/a"


def report(
    rows: List[Dict[str, Any]],
    costs: Dict[str, Tuple[int, float]],
    verbose: bool,
    out: Any = sys.stdout,
) -> None:
    def p(text: str = "") -> None:
        print(text, file=out)

    if not rows:
        p("no rows with op_stats.screen in that range")
        return

    chunk_status: Dict[str, int] = {}
    run_errors = 0
    refs_total = refs_hit = 0
    misses: List[Tuple[int, int]] = []
    sent_n = total_n = sent_c = total_c = 0
    capped = 0
    matched_n = only_old_n = only_new_n = 0
    same_target = diff_target = 0
    gate_hits: Dict[str, int] = {}
    ok_chunk_n = 0
    detail: List[str] = []
    for row in rows:
        screen = row["screen"]
        if screen.get("status") != "ok":
            run_errors += 1
            continue
        for chunk in screen.get("chunks", []):
            chunk_status[chunk["status"]] = chunk_status.get(chunk["status"], 0) + 1
        for chunk in ok_chunks(row):
            ok_chunk_n += 1
            f = chunk["facts"]
            sent_n += f["sent"]
            total_n += f["total"]
            sent_c += f["sent_chars"]
            total_c += f["total_chars"]
            capped += 1 if f["capped"] else 0
            for qid, score in chunk.get("gate", {}).items():
                gate_hits[qid] = gate_hits.get(qid, 0) + (1 if score >= 0.5 else 0)
        if not ok_chunks(row):
            continue
        refs = classic_refs(row)
        sent = sent_ids(row)
        refs_total += len(refs)
        refs_hit += len(refs & sent)
        for fid in sorted(refs - sent):
            misses.append((row["id"], fid))
        matched, only_old, only_new = match_ops(row["old_ops"], new_ops(row))
        matched_n += len(matched)
        only_old_n += len(only_old)
        only_new_n += len(only_new)
        for a, b in matched:
            if a.get("supersedes_id") == b.get("supersedes_id"):
                same_target += 1
            else:
                diff_target += 1
        if verbose:
            detail.append(
                f"- row {row['id']} {row['ts']} {','.join(row['scopes'])}: "
                f"classic {len(row['old_ops'])} ops, screen {len(new_ops(row))}; "
                f"refs {sorted(refs)} sent {len(sent)}"
            )
            for o in only_old:
                detail.append(f"    only classic: {_short(o)}")
            for o in only_new:
                detail.append(f"    only screen:  {_short(o)}")

    p(f"rows with a screen record: {len(rows)} (run-level errors: {run_errors})")
    p(f"chunks: {dict(sorted(chunk_status.items()))}")
    p()
    p("fact selection (ok chunks)")
    p(f"  facts sent / active: {sent_n} / {total_n} ({_pct(sent_n, total_n)})")
    p(f"  chars sent / active: {sent_c} / {total_c} ({_pct(sent_c, total_c)})")
    p(f"  chunks where the cap cut real hits: {capped} of {ok_chunk_n}")
    p()
    p("recall of facts the classic run acted on")
    p(f"  in the sent set: {refs_hit} / {refs_total} ({_pct(refs_hit, refs_total)})")
    if misses:
        p("  missed (row, fact id): " + ", ".join(f"{r}:{f}" for r, f in misses[:30]))
    p()
    p("op agreement (add/supersede, text similarity >= %.1f)" % MATCH_RATIO)
    p(
        f"  matched {matched_n} (same supersede target {same_target}, "
        f"different {diff_target}); only classic {only_old_n}; only screen {only_new_n}"
    )
    p()
    p("content gate questions >= 0.5 (logged, never gating)")
    p("  " + ", ".join(f"{q} {n}/{ok_chunk_n}" for q, n in sorted(gate_hits.items())))
    p()
    p("cost in range (model_calls)")
    for purpose in ("extraction", "reconcile", "screen_extraction", "screen"):
        n, usd = costs.get(purpose, (0, 0.0))
        p(f"  {purpose:18} {n:4} calls  ${usd:.4f}")
    if verbose:
        p()
        p("\n".join(detail))


def _short(op: Dict[str, Any]) -> str:
    return f"{op.get('op')} {str(op.get('content', ''))[:100]!r}" + (
        f" supersedes {op['supersedes_id']}" if op.get("supersedes_id") else ""
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    today = date.today()
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--since", default=str(today - timedelta(days=14)))
    parser.add_argument("--until", default=str(today))
    parser.add_argument("--agent", help="only runs that targeted this scope id")
    parser.add_argument("--db", help="bot.db path (default: the live one)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    db_path = args.db or str(data_layout(legacy_dir=REPO_ROOT / "data").bot_db)
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    rows = load_rows(db, args.since, args.until, args.agent)
    report(rows, call_costs(db, args.since, args.until), args.verbose)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
