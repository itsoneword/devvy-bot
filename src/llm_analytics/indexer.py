"""Incremental transcript indexer.

Sync by design: invoked from the CLI now; the future server (milestone 3)
runs scan_once() in asyncio.to_thread with its own sqlite3 connection.

CLI: poetry run python -m src.llm_analytics.indexer --once [--stats] [--session ID]
"""

import argparse
import json
import os
import sqlite3
import sys
import time
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

import structlog

from .botdb import mirror_bot_db
from .config import AnalyticsConfig
from .db import connect, init_db
from .models import AgentSpawnInput, ParsedEntry, ScanStats
from .parser import estimate_tokens, is_seeded_text, normalize_ts, parse_entry
from .pricing import PricingTable
from .quality import mirror_quality

logger = structlog.get_logger(__name__)

SCHEMA_VERSION = "1"
# Bump when the parser starts recording something new about existing
# transcripts: every still-present file is then re-read once from byte 0
# (all writes are idempotent), so history gains the new fields.
PARSER_VERSION = "2"  # 2: tool input/result sizes + context_items (dv-aeca)

# Derived-event thresholds — educated guesses from two verified examples
# (plan risk #2). Tunable HERE only; the UI keeps "inferred" labels.
CACHE_EXPIRY_MIN_PREV_CONTEXT = 5_000
CACHE_EXPIRY_CREATION_RATIO = 0.5
PARTIAL_HIT_CREATION_RATIO = 0.25
CONTEXT_CLIFF_DROP_RATIO = 0.4
CONTEXT_CLIFF_MIN_DROP_TOKENS = 20_000


@dataclass(frozen=True)
class _FileTarget:
    path: Path
    kind: str  # 'session' | 'subagent'
    session_id: str
    agent_id: str | None


def _now_iso() -> str:
    return normalize_ts(datetime.now(UTC).isoformat())


def _profile_from_cwd(cwd: str | None) -> str | None:
    if not cwd:
        return None
    # Sessions may record a symlinked cwd (~/chef-workspace -> workspaces/chef);
    # resolve so aliases collapse into one profile.
    with suppress(OSError):
        if os.path.exists(cwd):
            cwd = os.path.realpath(cwd)
    parts = PurePosixPath(cwd).parts
    if "workspaces" in parts:
        idx = parts.index("workspaces")
        if idx + 1 < len(parts):
            return parts[idx + 1]
    return parts[-1] if parts else None


class Indexer:
    def __init__(
        self,
        conn: sqlite3.Connection,
        config: AnalyticsConfig,
        pricing: PricingTable,
    ) -> None:
        self._conn = conn
        self._config = config
        self._pricing = pricing

    # ------------------------------------------------------------------
    # Scan cycle
    # ------------------------------------------------------------------

    def scan_once(self, only_session: str | None = None) -> ScanStats:
        started = time.monotonic()
        stats = ScanStats()
        repriced_sessions = self._maybe_reprice()
        self._maybe_reparse(only_session)
        targets = self._discover(only_session)
        stats.files_seen = len(targets)
        self._mark_missing(targets, only_session)
        touched_scopes: set[tuple[str, str | None]] = set()
        agent_inputs: dict[str, AgentSpawnInput] = {}
        for target in targets:
            if self._scan_file(target, agent_inputs, stats):
                touched_scopes.add((target.session_id, target.agent_id))
        mirrored = mirror_bot_db(self._conn, self._config.bot_db_path)
        mirror_quality(self._conn, self._config.bot_db_path)
        self._apply_agent_inputs(agent_inputs)
        touched_sessions = {sid for sid, _ in touched_scopes} | repriced_sessions
        for session_id in sorted(touched_sessions):
            self._rollup_session(session_id)
        for session_id, agent_id in sorted(
            touched_scopes, key=lambda scope: (scope[0], scope[1] or "")
        ):
            self._derive_events(session_id, agent_id)
        if mirrored:
            # Bot turns refresh touches sessions outside touched_scopes.
            self._conn.execute(
                "UPDATE sessions SET turn_count = (SELECT COUNT(*) FROM turns t"
                " WHERE t.session_id = sessions.session_id)"
            )
        self._set_meta("schema_version", SCHEMA_VERSION)
        self._set_meta("pricing_config_hash", self._pricing.config_hash)
        self._set_meta("last_scan_at", _now_iso())
        self._conn.commit()
        stats.sessions_touched = len(touched_sessions)
        stats.elapsed_s = round(time.monotonic() - started, 3)
        logger.info(
            "scan_complete",
            files_seen=stats.files_seen,
            files_scanned=stats.files_scanned,
            lines=stats.lines_consumed,
            api_calls=stats.api_calls_inserted,
            sessions=stats.sessions_touched,
            elapsed_s=stats.elapsed_s,
        )
        return stats

    # ------------------------------------------------------------------
    # Discovery / bookkeeping
    # ------------------------------------------------------------------

    def _discover(self, only_session: str | None) -> list[_FileTarget]:
        targets: list[_FileTarget] = []
        root = self._config.projects_dir
        for path in sorted(root.glob("*/*.jsonl")):
            session_id = path.stem
            if only_session and session_id != only_session:
                continue
            targets.append(_FileTarget(path, "session", session_id, None))
        # Two subagent layouts exist: <session>/subagents/agent-*.jsonl and
        # <session>/subagents/workflows/wf_*/agent-*.jsonl. Both globs match
        # agent-* only, so wf_*/journal.jsonl stays ignored.
        subagent_globs = (
            ("*/*/subagents/agent-*.jsonl", 1),
            ("*/*/subagents/workflows/wf_*/agent-*.jsonl", 3),
        )
        for pattern, session_depth in subagent_globs:
            for path in sorted(root.glob(pattern)):
                session_id = path.parents[session_depth].name
                if only_session and session_id != only_session:
                    continue
                agent_id = path.stem.removeprefix("agent-")
                targets.append(_FileTarget(path, "subagent", session_id, agent_id))
        return targets

    def _mark_missing(
        self, targets: list[_FileTarget], only_session: str | None
    ) -> None:
        present = {str(t.path) for t in targets}
        sql = "SELECT path FROM files WHERE missing_since IS NULL"
        params: tuple[str, ...] = ()
        if only_session:
            sql += " AND session_id = ?"
            params = (only_session,)
        known = [row["path"] for row in self._conn.execute(sql, params)]
        now = _now_iso()
        for path in known:
            if path not in present:
                self._conn.execute(
                    "UPDATE files SET missing_since = ? WHERE path = ?", (now, path)
                )

    # ------------------------------------------------------------------
    # File scanning
    # ------------------------------------------------------------------

    def _scan_file(
        self,
        target: _FileTarget,
        agent_inputs: dict[str, AgentSpawnInput],
        stats: ScanStats,
    ) -> bool:
        try:
            stat = target.path.stat()
        except FileNotFoundError:
            return False
        row = self._conn.execute(
            "SELECT id, byte_offset, mtime, size FROM files WHERE path = ?",
            (str(target.path),),
        ).fetchone()
        if row is None:
            cursor = self._conn.execute(
                "INSERT INTO files (path, kind, session_id, agent_id) VALUES"
                " (?,?,?,?)",
                (str(target.path), target.kind, target.session_id, target.agent_id),
            )
            file_id = int(cursor.lastrowid or 0)
            offset = 0
        else:
            file_id = int(row["id"])
            offset = int(row["byte_offset"])
            if stat.st_mtime == row["mtime"] and stat.st_size == row["size"]:
                stats.files_skipped += 1
                return False
            if stat.st_size < offset:
                # Truncation/rewrite defense: wipe this scope, rescan from 0.
                logger.warning(
                    "file_truncated",
                    path=str(target.path),
                    size=stat.st_size,
                    offset=offset,
                )
                self._wipe_scope(target)
                stats.files_truncated += 1
                offset = 0
        if target.kind == "subagent":
            self._upsert_subagent_stub(target.session_id, target.agent_id or "")
        # Row must exist before the bot.db mirror runs this cycle, or the
        # user_id/in_bot_db backfill skips sessions first seen in this scan.
        self._conn.execute(
            "INSERT OR IGNORE INTO sessions (session_id) VALUES (?)",
            (target.session_id,),
        )

        meta_pending = target.kind == "session"
        first_user_pending = offset == 0 and target.kind == "session"
        with target.path.open("rb") as handle:
            handle.seek(offset)
            while True:
                line = handle.readline()
                if not line or not line.endswith(b"\n"):
                    break  # partial line: leave for the next scan
                line_offset = offset
                offset += len(line)
                text = line.decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                try:
                    entry = json.loads(text)
                except json.JSONDecodeError:
                    logger.warning(
                        "bad_json_line", path=str(target.path), offset=line_offset
                    )
                    continue
                if not isinstance(entry, dict):
                    continue
                stats.lines_consumed += 1
                if meta_pending and entry.get("cwd"):
                    self._upsert_session_meta(target.session_id, entry)
                    meta_pending = False
                parsed = parse_entry(entry, kind=target.kind)
                if parsed is None:
                    continue
                if first_user_pending and parsed.user_text is not None:
                    first_user_pending = False
                    if is_seeded_text(parsed.user_text):
                        self._conn.execute(
                            "INSERT INTO sessions (session_id, seeded_start)"
                            " VALUES (?, 1) ON CONFLICT(session_id)"
                            " DO UPDATE SET seeded_start = 1",
                            (target.session_id,),
                        )
                self._apply_parsed(target, file_id, line_offset, parsed, agent_inputs)
                if parsed.api_call is not None:
                    stats.api_calls_inserted += 1  # attempted; PK may no-op
        self._conn.execute(
            "UPDATE files SET byte_offset = ?, mtime = ?, size = ?,"
            " last_scanned_at = ?, missing_since = NULL WHERE id = ?",
            (offset, stat.st_mtime, stat.st_size, _now_iso(), file_id),
        )
        self._conn.commit()
        stats.files_scanned += 1
        return True

    def _apply_parsed(
        self,
        target: _FileTarget,
        file_id: int,
        line_offset: int,
        parsed: ParsedEntry,
        agent_inputs: dict[str, AgentSpawnInput],
    ) -> None:
        call = parsed.api_call
        if call is not None:
            rates, rule = self._pricing.match(call.model)
            cost = self._pricing.cost(
                rates,
                input_tokens=call.input_tokens,
                output_tokens=call.output_tokens,
                cache_read_tokens=call.cache_read_tokens,
                cache_5m_tokens=call.cache_5m_tokens,
                cache_1h_tokens=call.cache_1h_tokens,
                cache_creation_tokens=call.cache_creation_tokens,
            )
            # Dedupe: usage repeats identically across a requestId's streamed
            # entries; the PK no-op also absorbs scan-boundary straddle.
            self._conn.execute(
                "INSERT OR IGNORE INTO api_calls (request_id, session_id,"
                " agent_id, ts, model, input_tokens, output_tokens,"
                " cache_read_tokens, cache_creation_tokens, cache_5m_tokens,"
                " cache_1h_tokens, est_cost_usd, pricing_rule, entry_uuid,"
                " file_id, entry_offset) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    call.request_id,
                    target.session_id,
                    target.agent_id,
                    call.ts,
                    call.model,
                    call.input_tokens,
                    call.output_tokens,
                    call.cache_read_tokens,
                    call.cache_creation_tokens,
                    call.cache_5m_tokens,
                    call.cache_1h_tokens,
                    cost,
                    rule,
                    call.entry_uuid,
                    file_id,
                    line_offset,
                ),
            )
        for tool_use in parsed.tool_uses:
            self._conn.execute(
                "INSERT INTO tool_calls (tool_use_id, session_id,"
                " agent_id, name, ts, request_id, assistant_uuid, input_summary,"
                " input_chars) VALUES (?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(tool_use_id) DO UPDATE SET"
                " input_chars = excluded.input_chars",
                (
                    tool_use.tool_use_id,
                    target.session_id,
                    target.agent_id,
                    tool_use.name,
                    tool_use.ts,
                    tool_use.request_id,
                    tool_use.assistant_uuid,
                    tool_use.input_summary,
                    tool_use.input_chars,
                ),
            )
            if tool_use.agent_input is not None:
                agent_inputs[tool_use.tool_use_id] = tool_use.agent_input
        for tool_result in parsed.tool_results:
            self._conn.execute(
                "UPDATE tool_calls SET result_ts = ?, is_error = ?,"
                " duration_ms = CAST(ROUND((julianday(?) - julianday(ts))"
                " * 86400000.0) AS INTEGER), result_chars = ?,"
                " result_images = ?, result_tokens_est = ?"
                " WHERE tool_use_id = ?",
                (
                    tool_result.result_ts,
                    tool_result.is_error,
                    tool_result.result_ts,
                    tool_result.result_chars,
                    tool_result.result_images,
                    estimate_tokens(
                        tool_result.result_chars, tool_result.result_images
                    ),
                    tool_result.tool_use_id,
                ),
            )
        for seq, item in enumerate(parsed.context_items):
            self._conn.execute(
                "INSERT OR IGNORE INTO context_items (session_id, agent_id,"
                " file_id, entry_offset, seq, ts, kind, name, chars, request_id)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    target.session_id,
                    target.agent_id,
                    file_id,
                    line_offset,
                    seq,
                    item.ts,
                    item.kind,
                    item.name,
                    item.chars,
                    item.request_id,
                ),
            )
        for link in parsed.spawn_links:
            self._conn.execute(
                "INSERT INTO subagent_runs (agent_id, session_id,"
                " spawn_tool_use_id, description) VALUES (?,?,?,?)"
                " ON CONFLICT(agent_id) DO UPDATE SET"
                " spawn_tool_use_id ="
                "   COALESCE(subagent_runs.spawn_tool_use_id,"
                "            excluded.spawn_tool_use_id),"
                " description ="
                "   COALESCE(subagent_runs.description, excluded.description)",
                (link.agent_id, target.session_id, link.tool_use_id, link.description),
            )
        if parsed.turn is not None and target.kind == "session":
            self._conn.execute(
                "INSERT OR IGNORE INTO turns (session_id, ts, source,"
                " prompt_excerpt) VALUES (?,?,'transcript',?)",
                (target.session_id, parsed.turn.ts, parsed.turn.excerpt),
            )

    def _upsert_session_meta(self, session_id: str, entry: dict[str, Any]) -> None:
        cwd = entry.get("cwd") if isinstance(entry.get("cwd"), str) else None
        entrypoint = (
            entry.get("entrypoint")
            if isinstance(entry.get("entrypoint"), str)
            else None
        )
        version = (
            entry.get("version") if isinstance(entry.get("version"), str) else None
        )
        git_branch = (
            entry.get("gitBranch") if isinstance(entry.get("gitBranch"), str) else None
        )
        self._conn.execute(
            "INSERT INTO sessions (session_id, cwd, profile, entrypoint,"
            " git_branch, cli_version) VALUES (?,?,?,?,?,?)"
            " ON CONFLICT(session_id) DO UPDATE SET"
            " cwd = COALESCE(sessions.cwd, excluded.cwd),"
            " profile = COALESCE(sessions.profile, excluded.profile),"
            " entrypoint = COALESCE(sessions.entrypoint, excluded.entrypoint),"
            " git_branch = COALESCE(sessions.git_branch, excluded.git_branch),"
            " cli_version = COALESCE(sessions.cli_version, excluded.cli_version)",
            (session_id, cwd, _profile_from_cwd(cwd), entrypoint, git_branch, version),
        )

    def _upsert_subagent_stub(self, session_id: str, agent_id: str) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO subagent_runs (agent_id, session_id)"
            " VALUES (?,?)",
            (agent_id, session_id),
        )

    def _wipe_scope(self, target: _FileTarget) -> None:
        params = (target.session_id, target.agent_id)
        self._conn.execute(
            "DELETE FROM api_calls WHERE session_id = ? AND agent_id IS ?", params
        )
        self._conn.execute(
            "DELETE FROM tool_calls WHERE session_id = ? AND agent_id IS ?", params
        )
        self._conn.execute(
            "DELETE FROM derived_events WHERE session_id = ? AND agent_id IS ?",
            params,
        )
        self._conn.execute(
            "DELETE FROM context_items WHERE session_id = ? AND agent_id IS ?",
            params,
        )
        if target.kind == "session":
            # Transcript turns come from this file; stale rows would survive
            # a rewrite otherwise. Bot turns are refreshed from bot.db anyway.
            self._conn.execute(
                "DELETE FROM turns WHERE session_id = ? AND source = 'transcript'",
                (target.session_id,),
            )
        self._conn.execute(
            "UPDATE files SET byte_offset = 0 WHERE path = ?", (str(target.path),)
        )

    # ------------------------------------------------------------------
    # Post-pass
    # ------------------------------------------------------------------

    def _apply_agent_inputs(self, agent_inputs: dict[str, AgentSpawnInput]) -> None:
        for tool_use_id, spawn in agent_inputs.items():
            self._conn.execute(
                "UPDATE subagent_runs SET"
                " description = COALESCE(?, description),"
                " subagent_type = COALESCE(?, subagent_type),"
                " model_override = COALESCE(?, model_override)"
                " WHERE spawn_tool_use_id = ?",
                (spawn.description, spawn.subagent_type, spawn.model, tool_use_id),
            )

    def _rollup_session(self, session_id: str) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO sessions (session_id) VALUES (?)", (session_id,)
        )
        self._conn.execute(
            """
            UPDATE sessions SET
              api_call_count = (SELECT COUNT(*) FROM api_calls a
                                WHERE a.session_id = :sid),
              input_tokens = COALESCE((SELECT SUM(a.input_tokens)
                  FROM api_calls a WHERE a.session_id = :sid), 0),
              output_tokens = COALESCE((SELECT SUM(a.output_tokens)
                  FROM api_calls a WHERE a.session_id = :sid), 0),
              cache_read_tokens = COALESCE((SELECT SUM(a.cache_read_tokens)
                  FROM api_calls a WHERE a.session_id = :sid), 0),
              cache_creation_tokens = COALESCE((SELECT SUM(a.cache_creation_tokens)
                  FROM api_calls a WHERE a.session_id = :sid), 0),
              est_cost_usd = COALESCE((SELECT SUM(a.est_cost_usd)
                  FROM api_calls a WHERE a.session_id = :sid), 0),
              first_ts = (SELECT MIN(a.ts) FROM api_calls a
                          WHERE a.session_id = :sid),
              last_ts = (SELECT MAX(a.ts) FROM api_calls a
                         WHERE a.session_id = :sid),
              tool_call_count = (SELECT COUNT(*) FROM tool_calls t
                                 WHERE t.session_id = :sid),
              subagent_count = (SELECT COUNT(*) FROM subagent_runs r
                                WHERE r.session_id = :sid),
              turn_count = (SELECT COUNT(*) FROM turns u
                            WHERE u.session_id = :sid),
              first_call_context = (SELECT a.input_tokens + a.cache_read_tokens
                                        + a.cache_creation_tokens
                  FROM api_calls a WHERE a.session_id = :sid
                    AND a.agent_id IS NULL ORDER BY a.ts LIMIT 1),
              bot_formula_tokens = (SELECT SUM(a.input_tokens + a.output_tokens)
                  FROM api_calls a WHERE a.session_id = :sid)
            WHERE session_id = :sid
            """,
            {"sid": session_id},
        )
        self._conn.execute(
            """
            UPDATE subagent_runs SET
              api_call_count = (SELECT COUNT(*) FROM api_calls a
                  WHERE a.session_id = :sid
                    AND a.agent_id = subagent_runs.agent_id),
              input_tokens = COALESCE((SELECT SUM(a.input_tokens) FROM api_calls a
                  WHERE a.session_id = :sid
                    AND a.agent_id = subagent_runs.agent_id), 0),
              output_tokens = COALESCE((SELECT SUM(a.output_tokens)
                  FROM api_calls a WHERE a.session_id = :sid
                    AND a.agent_id = subagent_runs.agent_id), 0),
              cache_read_tokens = COALESCE((SELECT SUM(a.cache_read_tokens)
                  FROM api_calls a WHERE a.session_id = :sid
                    AND a.agent_id = subagent_runs.agent_id), 0),
              cache_creation_tokens = COALESCE((SELECT SUM(a.cache_creation_tokens)
                  FROM api_calls a WHERE a.session_id = :sid
                    AND a.agent_id = subagent_runs.agent_id), 0),
              est_cost_usd = COALESCE((SELECT SUM(a.est_cost_usd)
                  FROM api_calls a WHERE a.session_id = :sid
                    AND a.agent_id = subagent_runs.agent_id), 0),
              started_ts = (SELECT MIN(a.ts) FROM api_calls a
                  WHERE a.session_id = :sid
                    AND a.agent_id = subagent_runs.agent_id),
              ended_ts = (SELECT MAX(a.ts) FROM api_calls a
                  WHERE a.session_id = :sid
                    AND a.agent_id = subagent_runs.agent_id)
            WHERE session_id = :sid
            """,
            {"sid": session_id},
        )

    def _derive_events(self, session_id: str, agent_id: str | None) -> None:
        rows = self._conn.execute(
            "SELECT ts, input_tokens, cache_read_tokens, cache_creation_tokens"
            " FROM api_calls WHERE session_id = ? AND agent_id IS ?"
            " ORDER BY ts",
            (session_id, agent_id),
        ).fetchall()
        self._conn.execute(
            "DELETE FROM derived_events WHERE session_id = ? AND agent_id IS ?",
            (session_id, agent_id),
        )
        prev_context: int | None = None
        prev_ts: datetime | None = None
        events: list[tuple[str, str, str]] = []
        for row in rows:
            cache_read = int(row["cache_read_tokens"])
            cache_creation = int(row["cache_creation_tokens"])
            context = int(row["input_tokens"]) + cache_read + cache_creation
            cur_ts = datetime.fromisoformat(row["ts"])
            gap_s: float | None = None
            if prev_ts is not None:
                gap_s = round((cur_ts - prev_ts).total_seconds(), 1)
            detail = json.dumps(
                {
                    "gap_s": gap_s,
                    "prev_context": prev_context,
                    "cur_cache_read": cache_read,
                    "cur_cache_creation": cache_creation,
                }
            )
            is_expiry = (
                prev_context is not None
                and prev_context > CACHE_EXPIRY_MIN_PREV_CONTEXT
                and cache_read == 0
                and cache_creation >= CACHE_EXPIRY_CREATION_RATIO * prev_context
            )
            if is_expiry:
                events.append((row["ts"], "cache_expiry", detail))
            if (
                cache_read > 0
                and cache_creation >= PARTIAL_HIT_CREATION_RATIO * context
            ):
                events.append((row["ts"], "partial_cache_hit", detail))
            if (
                not is_expiry
                and prev_context is not None
                and prev_context - context > CONTEXT_CLIFF_MIN_DROP_TOKENS
                and context < (1 - CONTEXT_CLIFF_DROP_RATIO) * prev_context
            ):
                events.append((row["ts"], "context_cliff", detail))
            prev_context = context
            prev_ts = cur_ts
        self._conn.executemany(
            "INSERT INTO derived_events (session_id, agent_id, ts, kind, detail)"
            " VALUES (?,?,?,?,?)",
            [(session_id, agent_id, ts, kind, detail) for ts, kind, detail in events],
        )

    # ------------------------------------------------------------------
    # Pricing refresh / meta
    # ------------------------------------------------------------------

    def _maybe_reprice(self) -> set[str]:
        stored = self._get_meta("pricing_config_hash")
        if stored is None or stored == self._pricing.config_hash:
            return set()
        logger.info("pricing_changed_recomputing_costs")
        touched: set[str] = set()
        rows = self._conn.execute(
            "SELECT request_id, session_id, model, input_tokens, output_tokens,"
            " cache_read_tokens, cache_creation_tokens, cache_5m_tokens,"
            " cache_1h_tokens FROM api_calls"
        ).fetchall()
        for row in rows:
            rates, rule = self._pricing.match(row["model"])
            cost = self._pricing.cost(
                rates,
                input_tokens=row["input_tokens"],
                output_tokens=row["output_tokens"],
                cache_read_tokens=row["cache_read_tokens"],
                cache_5m_tokens=row["cache_5m_tokens"],
                cache_1h_tokens=row["cache_1h_tokens"],
                cache_creation_tokens=row["cache_creation_tokens"],
            )
            self._conn.execute(
                "UPDATE api_calls SET est_cost_usd = ?, pricing_rule = ?"
                " WHERE request_id = ?",
                (cost, rule, row["request_id"]),
            )
            touched.add(row["session_id"])
        return touched

    def _maybe_reparse(self, only_session: str | None) -> None:
        """Re-read every still-present transcript once after a parser upgrade.
        Rows of vanished files stay as they were (their fields stay NULL)."""
        stored = self._get_meta("parser_version")
        if stored == PARSER_VERSION:
            return
        if only_session is not None:
            return  # a partial run must not consume the one-shot full re-read
        logger.info("parser_changed_reindexing", old=stored, new=PARSER_VERSION)
        self._conn.execute(
            "UPDATE files SET byte_offset = 0, mtime = 0, size = 0"
            " WHERE missing_since IS NULL"
        )
        self._set_meta("parser_version", PARSER_VERSION)
        self._conn.commit()

    def _get_meta(self, key: str) -> str | None:
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key = ?", (key,)
        ).fetchone()
        return str(row["value"]) if row is not None else None

    def _set_meta(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO meta (key, value) VALUES (?,?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------


def _print_stats(conn: sqlite3.Connection, stats: ScanStats) -> None:
    def one(sql: str) -> Any:
        return conn.execute(sql).fetchone()[0]

    print(f"scan: {stats.elapsed_s}s wall")
    print(
        f"files seen={stats.files_seen} scanned={stats.files_scanned}"
        f" skipped={stats.files_skipped} truncated={stats.files_truncated}"
    )
    print(
        f"lines consumed={stats.lines_consumed}"
        f" api_call inserts attempted={stats.api_calls_inserted}"
        f" sessions touched={stats.sessions_touched}"
    )
    print("-- index db --")
    print(
        "files:",
        one("SELECT COUNT(*) FROM files WHERE kind='session'"),
        "session +",
        one("SELECT COUNT(*) FROM files WHERE kind='subagent'"),
        "subagent",
    )
    print("sessions:", one("SELECT COUNT(*) FROM sessions"))
    print("api_calls:", one("SELECT COUNT(*) FROM api_calls"))
    print("tool_calls:", one("SELECT COUNT(*) FROM tool_calls"))
    print("subagent_runs:", one("SELECT COUNT(*) FROM subagent_runs"))
    print(
        "turns:",
        one("SELECT COUNT(*) FROM turns WHERE source='bot'"),
        "bot +",
        one("SELECT COUNT(*) FROM turns WHERE source='transcript'"),
        "transcript",
    )
    for kind, count in conn.execute(
        "SELECT kind, COUNT(*) FROM derived_events GROUP BY kind ORDER BY kind"
    ):
        print(f"derived_events[{kind}]:", count)
    print(
        "est cost total: $",
        round(one("SELECT SUM(est_cost_usd) FROM api_calls") or 0, 2),
    )


def _build_arg_parser() -> argparse.ArgumentParser:
    arg_parser = argparse.ArgumentParser(
        prog="python -m src.llm_analytics.indexer",
        description="Index Claude Code transcripts into data/llm_analytics.db",
    )
    arg_parser.add_argument(
        "--once", action="store_true", help="run one scan cycle and exit"
    )
    arg_parser.add_argument(
        "--stats", action="store_true", help="print index stats after the scan"
    )
    arg_parser.add_argument(
        "--session", metavar="ID", default=None, help="only index this session id"
    )
    return arg_parser


def main(argv: list[str] | None = None) -> int:
    arg_parser = _build_arg_parser()
    args = arg_parser.parse_args(argv)
    if not args.once:
        arg_parser.error("--once is required (continuous mode ships with the server)")
    config = AnalyticsConfig.from_env()
    config.db_path.parent.mkdir(parents=True, exist_ok=True)
    pricing = PricingTable.load(config.pricing_path)
    conn = connect(config.db_path)
    try:
        init_db(conn)
        indexer = Indexer(conn, config, pricing)
        stats = indexer.scan_once(only_session=args.session)
        if args.stats:
            _print_stats(conn, stats)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
