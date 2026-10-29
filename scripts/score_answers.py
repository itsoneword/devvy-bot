#!/usr/bin/env python3
"""Tier-2 offline answer-scoring batch judge (dv-300d).

Scores individual bot.db turns against the fixed rubric in
``src.llm_analytics.rubric`` using an LLM judge, and writes the result to
the ``answer_scores`` table in the llm_analytics index DB. This is an
**offline batch job** — it never runs in the live turn path, so it adds no
latency or cost to a user's message (that's exactly why the "verifier
agent" rejected in dv-5d5f is not this).

bot.db is opened strictly read-only and scanned via a streaming cursor, not
loaded into memory — the host has ~3.8G RAM.

Idempotent on ``(message_id, judge_model, rubric_version)``: re-running a
backfill skips turns already scored under the same judge+rubric, and
bumping ``RUBRIC_VERSION`` re-scores everything deliberately (old and new
versions are never averaged together).

Modes (mutually exclusive):
  --backfill --since YYYY-MM-DD [--profile X]   bulk-score history
  --recent                                       last --recent-days days,
                                                  for a nightly cron
  --message-id N                                 one turn, for rubric
                                                  debugging

Usage:
  poetry run python scripts/score_answers.py --provider claude_cli \\
      --message-id 1720 --dry-run
  poetry run python scripts/score_answers.py --provider claude_cli \\
      --backfill --since 2026-01-01 --profile chef --limit 50
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone


def _load_repo_env() -> None:
    """Make the repo's EnvironmentFile visible to a shell-launched run.

    The bot process gets these via systemd ``EnvironmentFile=``, but a manual
    ``python3 scripts/score_answers.py`` does not, and the judge key would
    look absent. Only fills variables that are not already set, and only the
    ones this script needs — never echoes a value.
    """
    path = Path(__file__).resolve().parent.parent / ".env"
    if not path.exists():
        return
    # PROFILES_CONFIG_PATH: answer scores carry the registry's agent slug.
    wanted = ("OPENROUTER_API_KEY", "SCORER_MODEL", "PROFILES_CONFIG_PATH")
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            if key in wanted and key not in os.environ:
                os.environ[key] = value.strip().strip("'\"")
    except OSError:
        return


from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import structlog  # noqa: E402

from src.llm_analytics import db as index_db  # noqa: E402
from src.llm_analytics.attribution import agent_for_path  # noqa: E402
from src.llm_analytics.judges import JudgeProvider, build_judge  # noqa: E402
from src.llm_analytics.quality import looks_like_recipe  # noqa: E402
from src.llm_analytics.rubric import (  # noqa: E402
    RUBRIC_VERSION,
    BatchTurn,
    ContextTurn,
    ParsedScore,
    build_batch_judge_prompt,
    build_judge_prompt,
    parse_batch_judge_output,
    parse_judge_output,
)
from src.utils.paths import data_layout  # noqa: E402

logger = structlog.get_logger(__name__)

DEFAULT_MIN_RESPONSE_CHARS = 200
DEFAULT_CONTEXT_TURNS = 2
_JUDGE_PARSE_ATTEMPTS = 3
DEFAULT_RECENT_DAYS = 2
# Per-message char caps on what goes into the judge prompt: bounds cost and
# keeps a single pathological turn from blowing the prompt up, while giving
# the judge enough to actually assess grounding/calibration.
_CONTEXT_TURN_CHAR_CAP = 1500
_CURRENT_TURN_CHAR_CAP = 6000


def _truncate(text: str, cap: int) -> str:
    text = text or ""
    return text if len(text) <= cap else text[:cap].rstrip() + " …[truncated]"


def _load_session_scope(
    conn: sqlite3.Connection,
) -> dict[str, tuple[Optional[str], Optional[int]]]:
    """session_id -> (profile, user_id). Sessions table is small; loaded
    fully, unlike messages which is streamed."""
    scope: dict[str, tuple[Optional[str], Optional[int]]] = {}
    for row in conn.execute("SELECT session_id, user_id, project_path FROM sessions"):
        scope[row["session_id"]] = (
            agent_for_path(row["project_path"]),
            row["user_id"],
        )
    return scope


def _select_candidates(
    conn: sqlite3.Connection,
    scope: dict[str, tuple[Optional[str], Optional[int]]],
    args: argparse.Namespace,
) -> sqlite3.Cursor:
    """A streaming cursor over candidate `messages` rows — never fetchall'd."""
    cols = "message_id, session_id, user_id, timestamp, prompt, response"
    if args.message_id is not None:
        return conn.execute(
            f"SELECT {cols} FROM messages WHERE message_id = ?", (args.message_id,)
        )

    if args.recent:
        since = (
            datetime.now(timezone.utc) - timedelta(days=args.recent_days)
        ).strftime("%Y-%m-%d")
    else:
        since = args.since

    query = f"SELECT {cols} FROM messages WHERE timestamp >= ?"
    params: list = [since]
    if args.profile:
        session_ids = [
            sid for sid, (profile, _uid) in scope.items() if profile == args.profile
        ]
        if not session_ids:
            return conn.execute(f"SELECT {cols} FROM messages WHERE 0")
        placeholders = ",".join("?" * len(session_ids))
        query += f" AND session_id IN ({placeholders})"
        params.extend(session_ids)
    query += " ORDER BY timestamp"
    return conn.execute(query, params)


def _fetch_context_turns(
    conn: sqlite3.Connection, session_id: str, before_ts: str, n: int
) -> list[ContextTurn]:
    rows = conn.execute(
        "SELECT prompt, response FROM messages"
        " WHERE session_id = ? AND timestamp < ?"
        " ORDER BY timestamp DESC LIMIT ?",
        (session_id, before_ts, n),
    ).fetchall()
    turns: list[ContextTurn] = []
    for row in reversed(rows):
        turns.append(
            ContextTurn("user", _truncate(row["prompt"] or "", _CONTEXT_TURN_CHAR_CAP))
        )
        turns.append(
            ContextTurn(
                "assistant", _truncate(row["response"] or "", _CONTEXT_TURN_CHAR_CAP)
            )
        )
    return turns


def _fetch_tool_calls(conn: sqlite3.Connection, message_id: int) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT tool FROM tool_calls WHERE message_id = ?", (message_id,)
    ).fetchall()
    return [row["tool"] for row in rows]


def _already_scored(
    conn: sqlite3.Connection, message_id: int, judge_model: str, rubric_version: int
) -> bool:
    row = conn.execute(
        "SELECT 1 FROM answer_scores"
        " WHERE message_id = ? AND judge_model = ? AND rubric_version = ?",
        (message_id, judge_model, rubric_version),
    ).fetchone()
    return row is not None


async def _score_one(
    judge: JudgeProvider,
    judge_model: str,
    profile: Optional[str],
    user_id: Optional[int],
    row: sqlite3.Row,
    bot_conn: sqlite3.Connection,
    index_conn: sqlite3.Connection,
    context_turns: int,
) -> None:
    context = _fetch_context_turns(
        bot_conn, row["session_id"], row["timestamp"], context_turns
    )
    tool_calls = _fetch_tool_calls(bot_conn, row["message_id"])
    prompt = build_judge_prompt(
        profile,
        _truncate(row["prompt"] or "", _CURRENT_TURN_CHAR_CAP),
        _truncate(row["response"] or "", _CURRENT_TURN_CHAR_CAP),
        context,
        tool_calls,
    )
    # The judge occasionally emits structurally broken JSON — observed
    # 2026-09-19: opus dropped exactly one closing brace, twice in a row on
    # msg 1704, at a different offset each time. Repairing it locally would
    # have to guess *where* the brace belongs (append-at-EOF silently nests
    # summary/flags inside `criteria` and scores an empty summary), so we
    # re-ask instead: a fresh sample never fabricates content, and strict
    # validation still runs on whatever comes back.
    parsed = None
    last_error: Optional[Exception] = None
    for _attempt in range(_JUDGE_PARSE_ATTEMPTS):
        raw = await judge.complete(prompt)
        try:
            parsed = parse_judge_output(raw, profile)
            break
        except ValueError as e:
            last_error = e
            logger.warning(
                "judge_output_unparseable",
                message_id=row["message_id"],
                error=str(e),
            )
    if parsed is None:
        raise RuntimeError(f"judge output unparseable after retries: {last_error}")

    _store_score(index_conn, row, profile, user_id, judge_model, parsed)


def _store_score(
    index_conn: sqlite3.Connection,
    row: sqlite3.Row,
    profile: Optional[str],
    user_id: Optional[int],
    judge_model: str,
    parsed: ParsedScore,
) -> None:
    index_conn.execute(
        "INSERT OR IGNORE INTO answer_scores"
        " (message_id, profile, user_id, ts, judge_model, rubric_version,"
        "  scores_json, total, rationale, flags, scored_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            row["message_id"],
            profile,
            user_id,
            row["timestamp"],
            judge_model,
            RUBRIC_VERSION,
            json.dumps(parsed.scores, ensure_ascii=False),
            parsed.total,
            parsed.summary,
            json.dumps(parsed.flags, ensure_ascii=False) if parsed.flags else None,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    index_conn.commit()


async def _score_batch(
    judge: JudgeProvider,
    judge_model: str,
    profile: Optional[str],
    pending: list[tuple[sqlite3.Row, Optional[int]]],
    bot_conn: sqlite3.Connection,
    index_conn: sqlite3.Connection,
    context_turns: int,
) -> tuple[int, list[int]]:
    """Score a batch in one judge call. Returns (stored, ids_not_returned)."""
    turns = []
    for row, _uid in pending:
        turns.append(
            BatchTurn(
                message_id=row["message_id"],
                prompt=_truncate(row["prompt"] or "", _CURRENT_TURN_CHAR_CAP),
                response=_truncate(row["response"] or "", _CURRENT_TURN_CHAR_CAP),
                context_turns=_fetch_context_turns(
                    bot_conn, row["session_id"], row["timestamp"], context_turns
                ),
                tool_calls=_fetch_tool_calls(bot_conn, row["message_id"]),
            )
        )
    ids = [t.message_id for t in turns]
    prompt = build_batch_judge_prompt(profile, turns)

    results: dict[int, object] = {}
    for _attempt in range(_JUDGE_PARSE_ATTEMPTS):
        raw = await judge.complete(prompt)
        try:
            results = dict(parse_batch_judge_output(raw, profile, ids))
        except ValueError as e:
            logger.warning("batch_output_unparseable", ids=ids, error=str(e))
            continue
        if results:
            break

    stored = 0
    for row, uid in pending:
        parsed = results.get(row["message_id"])
        if parsed is None:
            continue
        _store_score(index_conn, row, profile, uid, judge_model, parsed)
        stored += 1
    missing = [i for i in ids if i not in results]
    return stored, missing


async def run(args: argparse.Namespace) -> dict[str, object]:
    judge = build_judge(args.provider, model=args.model)
    judge_model = await judge.resolve()
    if args.batch_size > 1:
        # A batched judge sees the other turns; a solo judge does not. Same
        # rubric, different instrument — so it gets its own label and the two
        # populations stay separable (and separately re-runnable).
        judge_model = f"{judge_model}+b{args.batch_size}"
    if args.label_suffix:
        judge_model = f"{judge_model}{args.label_suffix}"

    bot_conn = sqlite3.connect(f"file:{args.bot_db}?mode=ro", uri=True)
    bot_conn.row_factory = sqlite3.Row
    index_conn = index_db.connect(args.index_db)
    index_db.init_db(index_conn)

    scope = _load_session_scope(bot_conn)

    scored = 0
    skipped_short = 0
    skipped_not_recipe = 0
    skipped_duplicate = 0
    skipped_profile = 0
    errors = 0
    total_cost_usd = 0.0

    batch: list[tuple[sqlite3.Row, Optional[int]]] = []
    batch_profile: Optional[str] = None

    async def _flush(
        prof: Optional[str], pending: list[tuple[sqlite3.Row, Optional[int]]]
    ) -> tuple[int, list[int], float]:
        try:
            stored, missing = await _score_batch(
                judge,
                judge_model,
                prof,
                pending,
                bot_conn,
                index_conn,
                args.context_turns,
            )
        except Exception as e:  # noqa: BLE001 — one bad batch must not kill the run
            logger.warning(
                "batch_failed",
                ids=[r["message_id"] for r, _ in pending],
                error=str(e),
            )
            return 0, [r["message_id"] for r, _ in pending], 0.0
        if missing:
            logger.warning("batch_incomplete", missing=missing)
        logger.info(
            "batch_scored", stored=stored, of=len(pending), judge_model=judge_model
        )
        return stored, missing, getattr(judge, "last_cost_usd", None) or 0.0

    cursor = _select_candidates(bot_conn, scope, args)
    for row in cursor:
        if scored >= args.limit:
            break

        response = row["response"] or ""
        if len(response) < args.min_response_chars:
            skipped_short += 1
            continue

        if args.recipes_only and not looks_like_recipe(response):
            skipped_not_recipe += 1
            continue

        profile, scope_user_id = scope.get(row["session_id"], (None, None))
        user_id = scope_user_id if scope_user_id is not None else row["user_id"]
        if args.profile and profile != args.profile:
            skipped_profile += 1
            continue

        if _already_scored(index_conn, row["message_id"], judge_model, RUBRIC_VERSION):
            skipped_duplicate += 1
            continue

        if args.dry_run:
            logger.info(
                "would_score",
                message_id=row["message_id"],
                profile=profile,
                ts=row["timestamp"],
            )
            scored += 1
            continue

        if args.batch_size > 1:
            # Batches never straddle profiles: the criteria set is per-profile,
            # so one prompt can only carry turns that share it.
            if batch and batch_profile != profile:
                stored, missing, cost = await _flush(batch_profile, batch)
                scored += stored
                errors += len(missing)
                total_cost_usd += cost
                batch = []
            batch_profile = profile
            batch.append((row, user_id))
            if len(batch) >= args.batch_size:
                stored, missing, cost = await _flush(batch_profile, batch)
                scored += stored
                errors += len(missing)
                total_cost_usd += cost
                batch = []
            continue

        try:
            await _score_one(
                judge,
                judge_model,
                profile,
                user_id,
                row,
                bot_conn,
                index_conn,
                args.context_turns,
            )
        except Exception as e:  # noqa: BLE001 — one bad turn must not kill the batch
            errors += 1
            logger.warning("score_failed", message_id=row["message_id"], error=str(e))
            continue

        cost = getattr(judge, "last_cost_usd", None)
        if cost:
            total_cost_usd += cost
        scored += 1
        logger.info(
            "scored",
            message_id=row["message_id"],
            profile=profile,
            judge_model=judge_model,
        )

    if batch:
        stored, missing, cost = await _flush(batch_profile, batch)
        scored += stored
        errors += len(missing)
        total_cost_usd += cost

    bot_conn.close()
    index_conn.close()
    return {
        "scored": scored,
        "skipped_short": skipped_short,
        "skipped_not_recipe": skipped_not_recipe,
        "skipped_duplicate": skipped_duplicate,
        "skipped_profile": skipped_profile,
        "errors": errors,
        "total_cost_usd": total_cost_usd,
        "judge_model": judge_model,
        "rubric_version": RUBRIC_VERSION,
    }


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--backfill", action="store_true", help="bulk-score history")
    mode.add_argument(
        "--recent", action="store_true", help="score the last --recent-days days"
    )
    mode.add_argument("--message-id", type=int, help="score exactly one turn")

    parser.add_argument("--since", help="YYYY-MM-DD, required with --backfill")
    parser.add_argument("--profile", help="restrict to one profile (e.g. chef)")
    parser.add_argument(
        "--provider", choices=("openrouter", "claude_cli"), default="openrouter"
    )
    parser.add_argument(
        "--model", help="override the judge model for the chosen provider"
    )
    parser.add_argument("--limit", type=int, default=200, required=False)
    parser.add_argument("--dry-run", action="store_true", required=False)
    parser.add_argument("--context-turns", type=int, default=DEFAULT_CONTEXT_TURNS)
    parser.add_argument("--recent-days", type=int, default=DEFAULT_RECENT_DAYS)
    parser.add_argument(
        "--min-response-chars", type=int, default=DEFAULT_MIN_RESPONSE_CHARS
    )
    parser.add_argument(
        "--recipes-only",
        action="store_true",
        help=(
            "score only turns that look like a written-out recipe, using the"
            " same mechanical detector as the Tier-1 quality metric"
        ),
    )
    parser.add_argument(
        "--label-suffix",
        default="",
        help=(
            "append to the stored judge_model label. Used to re-score an"
            " already-scored population under a second label — the only way to"
            " measure the judge's own run-to-run variance, which is the"
            " baseline any other scoring change has to beat"
        ),
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=1,
        help=(
            "score N turns per judge call (default 1 = one call per turn)."
            " Batched scores are stored under a '+bN' judge_model suffix so"
            " they are never averaged together with solo scores"
        ),
    )
    parser.add_argument("--bot-db", default=str(data_layout().bot_db))
    parser.add_argument("--index-db", default=str(data_layout().llm_analytics_db))

    args = parser.parse_args(argv)
    if args.backfill and not args.since:
        parser.error("--backfill requires --since YYYY-MM-DD")
    return args


def main(argv: Optional[list[str]] = None) -> int:
    _load_repo_env()
    args = parse_args(argv)
    stats = asyncio.run(run(args))
    print(json.dumps(stats, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
