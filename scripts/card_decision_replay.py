#!/usr/bin/env python3
"""Replay of recent memory rows through the dv-3e28 card routing (read-only).

For each scope, every ``memory_facts`` row recorded in the last ``--days``
days is routed as the writer would route it today: a repo agent's card ref
whose card exists on a board goes to the card (off-prompt); a
``docs/DECISIONS.md`` ref stays a pointer when verified, else flagged; any
other row stays what it is. Stored content and source_ref are replayed as
recorded; the extractor model is not re-run.

Read-only: ``bot.db`` is opened ``mode=ro`` and cards are only checked for
existence. The one file written is ``--out``.

    poetry run python scripts/card_decision_replay.py \\
        --out docs/research/dv-3e28/replay.md
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.evaluator.decision_refs import (  # noqa: E402
    DecisionRefChecker,
    card_ref_id,
    is_decision_ref,
)
from src.storage.memory_repository import split_ref  # noqa: E402
from src.utils.boards import (  # noqa: E402
    BOARDS_FILE_DEFAULT,
    agent_repos,
    all_tasks_dirs,
)
from src.utils.paths import data_layout  # noqa: E402


def _parse(val: object) -> Optional[datetime]:
    if not isinstance(val, str) or not val:
        return None
    try:
        dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _boards_with(cid: str, boards: Path) -> List[str]:
    return [
        name
        for name, d in all_tasks_dirs(boards).items()
        if (d / f"{cid}.md").is_file() or (d / "archive" / f"{cid}.md").is_file()
    ]


def route(
    checker: DecisionRefChecker,
    boards: Path,
    scope: str,
    kind: str,
    source_ref: Optional[str],
    content: str,
) -> Tuple[str, str]:
    """(outcome, reason): outcome is card / fact / pointer / flagged / other."""
    ref = split_ref(source_ref)[0] or ""
    if checker.handles(scope) and is_decision_ref(ref):
        parsed = card_ref_id(ref)
        if parsed is not None:
            found = checker.resolve_card(scope, ref)
            if found is not None:
                return "card", f"{found[0]}:{parsed[1]}"
            on = _boards_with(parsed[1], boards)
            if not on:
                return "flagged", f"{ref}: no such card on any board"
            return "flagged", f"{ref}: ambiguous, on {', '.join(on)}"
        if checker.verify(scope, ref, content):
            return "pointer", ref
        return "flagged", f"{ref}: not in the repo"
    if kind == "user_fact":
        return "fact", ""
    if kind == "conversation_result":
        return "pointer", ref
    return "other", kind


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", type=Path, default=data_layout().bot_db)
    ap.add_argument("--boards", type=Path, default=BOARDS_FILE_DEFAULT)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--scopes", default="devops,gymbro")
    args = ap.parse_args(argv)

    scopes = [s.strip() for s in args.scopes.split(",") if s.strip()]
    checker = DecisionRefChecker(agent_repos(args.boards), boards_path=args.boards)
    cutoff = datetime.now(UTC) - timedelta(days=args.days)

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT id, agent_id, kind, tier, content, source_ref, recorded_at "
            "FROM memory_facts ORDER BY id"
        ).fetchall()
    finally:
        conn.close()

    lines = [
        f"# dv-3e28 card-decision replay ({datetime.now(UTC):%Y-%m-%d %H:%M} UTC)",
        "",
        f"Rows recorded in the last {args.days} days, routed as the writer "
        "routes them now (content and source_ref as stored; no model re-run).",
        "",
    ]
    total_flagged_existing = 0
    for scope in scopes:
        counts: Counter[str] = Counter()
        flagged: List[Tuple[int, str, str]] = []
        for fid, agent, kind, _tier, content, ref, rec in rows:
            if agent.split("#", 1)[0] != scope:
                continue
            when = _parse(rec)
            if when is None or when < cutoff:
                continue
            outcome, reason = route(checker, args.boards, scope, kind, ref, content)
            counts[outcome] += 1
            if outcome == "flagged":
                flagged.append((fid, reason, content))
                cid = card_ref_id(split_ref(ref)[0] or "")
                if cid and _boards_with(cid[1], args.boards):
                    total_flagged_existing += 1
        lines += [
            f"## {scope}",
            "",
            f"- rows: {sum(counts.values())}",
            f"- routed to a card (off-prompt): {counts['card']}",
            f"- still injected as a fact: {counts['fact']}",
            f"- still injected as a pointer: {counts['pointer']}",
            f"- still flagged: {counts['flagged']}",
            f"- other kinds (not injected): {counts['other']}",
            "",
        ]
        if flagged:
            lines.append("Flagged rows:")
            lines.append("")
            for fid, reason, content in flagged:
                text = content.replace("\n", " ")[:140]
                lines.append(f"- #{fid} ({reason}): {text}")
            lines.append("")
    lines += [
        f"Flagged rows whose card exists on a board: {total_flagged_existing}",
        "",
    ]
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("\n".join(lines), encoding="utf-8")
    print(args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
