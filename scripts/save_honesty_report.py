#!/usr/bin/env python3
"""Offline FP measurement + baseline reproduction for the save-honesty check.

Runs the EXACT production detector (imported from
``src.bot.orchestration.save_honesty``) over historical ``messages`` rows
joined with ``tool_usage`` — same code online and offline, or the measured
false-positive rate says nothing about production (dv-7783).

Modes:
  (default)            all-profiles claim dump for FP labelling: every
                       response with a claim hit, its family/excerpt and
                       whether a write-ish tool satisfied it.
  --corpus food-coach  reproduce the FINDINGS 2026-08-04 baseline: coach
                       workspace, user 222222222, 2026-07-28..2026-08-04,
                       denominator = food-mention turns, numerator =
                       claim+no-write. Gate: ~64% (+/-5pp agreed tolerance).
  --negative-control   assert zero hits on recall-style responses
                       («помню…», "I remember you…") lacking claim verbs.

Read-only. The prompt side is scanned only to strip the legacy reply
envelope and test the trigger on the user part — claims are detected on
``response`` text exclusively.

Usage: poetry run python scripts/save_honesty_report.py [--db data/bot.db]
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.bot.orchestration.save_honesty import (  # noqa: E402
    check_turn,
    detect_write_claim,
)
from src.bot.orchestration.windowing import (  # noqa: E402
    mentions_remember_trigger,
)
from src.utils.paths import data_layout  # noqa: E402

# Legacy reply envelope: "[Replying to <label>:\n<quoted>]\n\n<user text>".
_LEGACY_ENVELOPE = re.compile(r"^\[Replying to [^\]]*\]\s*", re.DOTALL)

# Food-mention lexicon for the baseline denominator (ru-centric — the
# measured corpus is a Russian-speaking coach user). Checked in with the
# script so the 64% stays recomputable.
FOOD_LEXICON = re.compile(
    r"обед|завтрак|ужин|перекус|съел|поел|позавтракал|пообедал|поужинал"
    r"|каша|гречк|куриц|творог|яйц|салат|суп|котлет|рыб|мяс|овощ|фрукт"
    r"|калори|белк|углевод|порци|грамм|\bел[аи]?\b|\bем\b|скушал|кушал"
    r"|breakfast|lunch|dinner|snack|\bate\b|\beaten\b|calorie|protein",
    re.IGNORECASE,
)


def user_part(prompt: str) -> str:
    return _LEGACY_ENVELOPE.sub("", prompt or "")


def load_turns(conn: sqlite3.Connection, where: str, params: tuple) -> list[dict]:
    rows = conn.execute(
        f"""
        SELECT m.message_id, m.user_id, m.prompt, m.response, m.timestamp,
               s.project_path
        FROM messages m LEFT JOIN sessions s ON s.session_id = m.session_id
        WHERE m.response IS NOT NULL {where}
        ORDER BY m.message_id
        """,
        params,
    ).fetchall()
    turns = []
    for mid, uid, prompt, response, ts, path in rows:
        tools = [
            r[0]
            for r in conn.execute(
                "SELECT tool_name FROM tool_usage WHERE message_id=?", (mid,)
            )
        ]
        turns.append(
            {
                "message_id": mid,
                "user_id": uid,
                "prompt": prompt or "",
                "response": response,
                "timestamp": ts,
                "project_path": path or "",
                "tools": tools,
            }
        )
    return turns


def claim_dump(conn: sqlite3.Connection) -> None:
    turns = load_turns(conn, "", ())
    hits = []
    for t in turns:
        res = check_turn(t["response"], t["tools"])
        if res.claim is not None:
            hits.append((t, res))
    print(f"# Claim hits: {len(hits)} of {len(turns)} turns\n")
    fam = Counter(res.claim.family for _, res in hits)
    sat = Counter(res.satisfied_by or "NONE" for _, res in hits)
    print(f"by family: {dict(fam)}")
    print(f"satisfied_by: {dict(sat)}\n")
    for t, res in hits:
        scope = t["project_path"].rsplit("/", 1)[-1] or "?"
        flag = "MISMATCH" if res.mismatch_kind else "ok"
        excerpt = " ".join(res.claim.excerpt.split())
        print(
            f"[{flag:8}] msg={t['message_id']} {scope} u{t['user_id']} "
            f"{str(t['timestamp'])[:16]} family={res.claim.family} "
            f"satisfied={res.satisfied_by}"
        )
        print(f"    …{excerpt}…")
    print(
        "\nLabel each hit: genuine durable-write claim? Mislabels / hits = FP "
        "rate; bound <5% (D11)."
    )


def corpus_food_coach(conn: sqlite3.Connection) -> None:
    turns = load_turns(
        conn,
        "AND s.project_path LIKE '%workspaces/coach%' AND m.user_id=? "
        "AND m.timestamp >= '2026-07-28' AND m.timestamp < '2026-08-05'",
        (222222222,),
    )
    denom = [t for t in turns if FOOD_LEXICON.search(user_part(t["prompt"]))]
    numer = []
    for t in denom:
        res = check_turn(t["response"], t["tools"])
        if res.claim is not None and res.satisfied_by is None:
            numer.append((t, res))
    print(f"coach food corpus: {len(turns)} turns, {len(denom)} food-mention")
    if denom:
        print(
            f"claim+no-write: {len(numer)}/{len(denom)} "
            f"= {len(numer) / len(denom):.0%}  (FINDINGS baseline: 34/53 = 64%)"
        )
    for t, res in numer:
        excerpt = " ".join(res.claim.excerpt.split())[:140]
        print(f"  msg={t['message_id']} {str(t['timestamp'])[:16]}: …{excerpt}…")


def negative_control(conn: sqlite3.Connection) -> None:
    turns = load_turns(conn, "", ())
    recall_like = [
        t
        for t in turns
        if re.search(r"\bпомню\b|I remember you", t["response"], re.IGNORECASE)
    ]
    bad = []
    for t in recall_like:
        claim = detect_write_claim(t["response"])
        # A response may legitimately contain BOTH recall and a real claim;
        # only flag when the matched text itself is a recall verb.
        if claim and re.fullmatch(
            r"помню|I remember you", claim.matched_text, re.IGNORECASE
        ):
            bad.append((t, claim))
    print(
        f"negative control: {len(recall_like)} recall-style responses, "
        f"{len(bad)} false hits"
    )
    for t, claim in bad:
        print(f"  msg={t['message_id']}: {claim.matched_text!r}")
    if bad:
        sys.exit(1)


def trigger_scan(conn: sqlite3.Connection) -> None:
    """How often the trigger fires on the envelope but not the user part —
    the msg-766 class this ships a structural fix for."""
    turns = load_turns(conn, "", ())
    envelope_only = [
        t
        for t in turns
        if mentions_remember_trigger(t["prompt"])
        and not mentions_remember_trigger(user_part(t["prompt"]))
    ]
    print(f"trigger fired on envelope-only (historical): {len(envelope_only)}")
    for t in envelope_only:
        print(f"  msg={t['message_id']} {str(t['timestamp'])[:16]}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(data_layout().bot_db))
    parser.add_argument("--corpus", choices=["food-coach"])
    parser.add_argument("--negative-control", action="store_true")
    parser.add_argument("--trigger-scan", action="store_true")
    args = parser.parse_args()

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    if args.corpus == "food-coach":
        corpus_food_coach(conn)
    elif args.negative_control:
        negative_control(conn)
    elif args.trigger_scan:
        trigger_scan(conn)
    else:
        claim_dump(conn)


if __name__ == "__main__":
    main()
