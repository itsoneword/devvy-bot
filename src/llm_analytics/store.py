"""Read-side query facade over the analytics index DB.

aiosqlite, channel_intel Store shape: async open()/close(), one method per
endpoint's data need. Strictly read-only (PRAGMA query_only) — the indexer
writes on its own sync sqlite3 connection in a worker thread.

Reconciliation flags and chain annotations are computed here at query time
(plan: "tunable"), with the tolerances kept as module constants.
"""

import json
import os
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

import aiosqlite

from .indexer import _profile_from_cwd

# Chain annotations. These are the thresholds the bot actually applies, read
# from the same env vars its Settings use (window_eval_interval_tokens /
# window_refresh_threshold_tokens) so the chart's reference lines follow the
# deployment. Defaults match src/config/settings.py. The former hardcoded
# 50k/150k were the pre-dv-9f31 numbers, counted against uncached input only —
# they drew lines the bot has not used since 2026-08-04.
EVAL_THRESHOLD = int(os.getenv("WINDOW_EVAL_INTERVAL_TOKENS", "120000"))
REFRESH_THRESHOLD = int(os.getenv("WINDOW_REFRESH_THRESHOLD_TOKENS", "220000"))

# Reconciliation tolerances (plan: tunable, query-time).
TURN_COUNT_TOLERANCE = 2
COST_ABS_TOLERANCE_USD = 0.05
COST_REL_TOLERANCE = 0.10
TIME_SKEW_MAX_S = 300.0

TIMELINE_ROW_CAP = 10_000

# Entrypoints that are expected to have no bot.db row: interactive CLI runs
# and agent-SDK CLI runs ('sdk-cli', 134+ sessions) are not bot traffic.
NON_BOT_ENTRYPOINTS = ("cli", "sdk-cli")

_SESSION_SORT_COLUMNS = frozenset(
    {
        "session_id",
        "first_ts",
        "last_ts",
        "api_call_count",
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_creation_tokens",
        "est_cost_usd",
        "tool_call_count",
        "subagent_count",
        "turn_count",
    }
)


def _add_user_cond(
    conds: list[str], params: list[Any], user_id: int | str | None
) -> None:
    """'local' selects sessions with no bot user (interactive/CLI on this machine)."""
    if user_id == "local":
        conds.append("s.user_id IS NULL")
    elif user_id is not None:
        conds.append("s.user_id = ?")
        params.append(user_id)


def _norm_bound(value: str, *, end: bool) -> str:
    """Widen a date-only bound to a full-day ISO bound (index ts are ISO-Z)."""
    if len(value) == 10:
        return value + ("T23:59:59.999Z" if end else "T00:00:00.000Z")
    return value


def _parse_dt(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


def _json_detail(raw: Any) -> Any:
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw
    return raw


class Store:
    """Async read-only queries over the index DB, one method per endpoint."""

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    @classmethod
    async def open(cls, path: str | Path) -> "Store":
        conn = await aiosqlite.connect(str(path))
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA query_only=ON")
        return cls(conn)

    async def close(self) -> None:
        await self._conn.close()

    # ------------------------------------------------------------------
    # Low-level helpers
    # ------------------------------------------------------------------

    async def _all(self, sql: str, params: Sequence[Any] = ()) -> list[aiosqlite.Row]:
        cursor = await self._conn.execute(sql, tuple(params))
        try:
            rows = await cursor.fetchall()
        finally:
            await cursor.close()
        return list(rows)

    async def _row(self, sql: str, params: Sequence[Any] = ()) -> aiosqlite.Row | None:
        cursor = await self._conn.execute(sql, tuple(params))
        try:
            row = await cursor.fetchone()
        finally:
            await cursor.close()
        return row

    async def _scalar(self, sql: str, params: Sequence[Any] = ()) -> Any:
        row = await self._row(sql, params)
        return row[0] if row is not None else None

    # ------------------------------------------------------------------
    # api/health
    # ------------------------------------------------------------------

    async def health(self) -> dict[str, Any]:
        return {
            "status": "ok",
            "last_scan_at": await self._scalar(
                "SELECT value FROM meta WHERE key = 'last_scan_at'"
            ),
            "files": await self._scalar("SELECT COUNT(*) FROM files"),
            "sessions": await self._scalar("SELECT COUNT(*) FROM sessions"),
            "api_calls": await self._scalar("SELECT COUNT(*) FROM api_calls"),
        }

    # ------------------------------------------------------------------
    # api/filters
    # ------------------------------------------------------------------

    async def filters(
        self, *, from_: str | None = None, to: str | None = None
    ) -> dict[str, Any]:
        # Options are scoped to sessions active in the requested range so the
        # dropdowns never offer a choice that yields an empty view.
        range_conds: list[str] = []
        range_params: list[Any] = []
        if from_:
            range_conds.append("last_ts >= ?")
            range_params.append(_norm_bound(from_, end=False))
        if to:
            range_conds.append("first_ts <= ?")
            range_params.append(_norm_bound(to, end=True))
        in_range = (" AND " + " AND ".join(range_conds)) if range_conds else ""
        users: list[int | str] = [
            row[0]
            for row in await self._all(
                "SELECT DISTINCT user_id FROM sessions"
                f" WHERE user_id IS NOT NULL{in_range} ORDER BY user_id",
                range_params,
            )
        ]
        local = await self._scalar(
            "SELECT COUNT(*) FROM sessions"
            f" WHERE user_id IS NULL AND api_call_count > 0{in_range}",
            range_params,
        )
        if local:
            users.append("local")
        # Only profiles with indexed traffic — cwd-derived one-offs and
        # transcript-less bot sessions would otherwise clutter the dropdown.
        profiles = [
            row[0]
            for row in await self._all(
                "SELECT profile FROM sessions"
                f" WHERE profile IS NOT NULL{in_range} GROUP BY profile"
                " HAVING SUM(api_call_count) > 0 ORDER BY profile",
                range_params,
            )
        ]
        models = [
            row[0]
            for row in await self._all(
                "SELECT DISTINCT model FROM api_calls ORDER BY model"
            )
        ]
        entrypoints = [
            row[0]
            for row in await self._all(
                "SELECT DISTINCT entrypoint FROM sessions"
                " WHERE entrypoint IS NOT NULL ORDER BY entrypoint"
            )
        ]
        span = await self._row("SELECT MIN(ts) AS f, MAX(ts) AS t FROM api_calls")
        return {
            "users": users,
            "profiles": profiles,
            "models": models,
            "entrypoints": entrypoints,
            "date_range": {
                "from": span["f"] if span else None,
                "to": span["t"] if span else None,
            },
        }

    # ------------------------------------------------------------------
    # api/overview
    # ------------------------------------------------------------------

    async def overview(
        self,
        *,
        from_: str | None = None,
        to: str | None = None,
        user_id: int | str | None = None,
        profile: str | None = None,
    ) -> dict[str, Any]:
        conds: list[str] = []
        params: list[Any] = []
        if from_:
            conds.append("a.ts >= ?")
            params.append(_norm_bound(from_, end=False))
        if to:
            conds.append("a.ts <= ?")
            params.append(_norm_bound(to, end=True))
        _add_user_cond(conds, params, user_id)
        if profile:
            conds.append("s.profile = ?")
            params.append(profile)
        where = f" WHERE {' AND '.join(conds)}" if conds else ""
        base = f"FROM api_calls a JOIN sessions s ON s.session_id = a.session_id{where}"
        agg = (
            "COUNT(*) AS api_calls,"
            " COALESCE(SUM(a.input_tokens), 0) AS input_tokens,"
            " COALESCE(SUM(a.output_tokens), 0) AS output_tokens,"
            " COALESCE(SUM(a.cache_read_tokens), 0) AS cache_read_tokens,"
            " COALESCE(SUM(a.cache_creation_tokens), 0) AS cache_creation_tokens,"
            " COALESCE(SUM(a.est_cost_usd), 0) AS est_cost_usd"
        )
        totals_row = await self._row(
            f"SELECT {agg}, COUNT(DISTINCT a.session_id) AS sessions {base}", params
        )
        totals = dict(totals_row) if totals_row is not None else {}
        tool_conds = [c.replace("a.ts", "t.ts") for c in conds]
        tool_where = f" WHERE {' AND '.join(tool_conds)}" if tool_conds else ""
        totals["tool_calls"] = await self._scalar(
            "SELECT COUNT(*) FROM tool_calls t"
            f" JOIN sessions s ON s.session_id = t.session_id{tool_where}",
            params,
        )
        by_model = [
            dict(row)
            for row in await self._all(
                f"SELECT a.model, {agg} {base} GROUP BY a.model"
                " ORDER BY est_cost_usd DESC",
                params,
            )
        ]
        by_profile = [
            dict(row)
            for row in await self._all(
                f"SELECT s.profile, {agg} {base} GROUP BY s.profile"
                " ORDER BY est_cost_usd DESC",
                params,
            )
        ]
        daily = [
            dict(row)
            for row in await self._all(
                f"SELECT substr(a.ts, 1, 10) AS date, {agg} {base}"
                " GROUP BY date ORDER BY date",
                params,
            )
        ]
        return {
            "totals": totals,
            "by_model": by_model,
            "by_profile": by_profile,
            "daily": daily,
        }

    # ------------------------------------------------------------------
    # api/quality (Tier-1 bot-quality signals, dv-37e9)
    # ------------------------------------------------------------------

    async def quality(
        self,
        *,
        from_: str | None = None,
        to: str | None = None,
        user_id: int | str | None = None,
        profile: str | None = None,
    ) -> dict[str, Any]:
        conds: list[str] = []
        params: list[Any] = []
        if from_:
            conds.append("q.date >= ?")
            params.append(from_[:10])
        if to:
            conds.append("q.date <= ?")
            params.append(to[:10])
        if user_id == "local":
            conds.append("q.user_id IS NULL")
        elif user_id is not None:
            conds.append("q.user_id = ?")
            params.append(user_id)
        if profile:
            conds.append("q.profile = ?")
            params.append(profile)
        where = f" WHERE {' AND '.join(conds)}" if conds else ""
        agg = (
            "COALESCE(SUM(q.pinned_facts), 0) AS pinned_facts,"
            " COALESCE(SUM(q.log_calls), 0) AS log_calls,"
            " COALESCE(SUM(q.claimed_not_logged), 0) AS claimed_not_logged,"
            " COALESCE(SUM(q.complaint_hits), 0) AS complaint_hits,"
            " COALESCE(SUM(q.user_messages), 0) AS user_messages"
        )
        totals_row = await self._row(
            f"SELECT {agg} FROM quality_daily q{where}", params
        )
        daily = [
            dict(row)
            for row in await self._all(
                f"SELECT q.date, {agg} FROM quality_daily q{where}"
                " GROUP BY q.date ORDER BY q.date",
                params,
            )
        ]
        by_agent = [
            dict(row)
            for row in await self._all(
                f"SELECT q.profile, q.user_id, {agg} FROM quality_daily q{where}"
                " GROUP BY q.profile, q.user_id"
                " ORDER BY pinned_facts + claimed_not_logged + complaint_hits DESC,"
                " q.profile, q.user_id",
                params,
            )
        ]
        return {
            "totals": dict(totals_row) if totals_row is not None else {},
            "daily": daily,
            "by_agent": by_agent,
        }

    # ------------------------------------------------------------------
    # api/sessions (the only paginated endpoint)
    # ------------------------------------------------------------------

    async def sessions_page(
        self,
        *,
        from_: str | None = None,
        to: str | None = None,
        user_id: int | str | None = None,
        profile: str | None = None,
        entrypoint: str | None = None,
        sort: str = "last_ts",
        order: str = "desc",
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        if sort not in _SESSION_SORT_COLUMNS:
            raise ValueError(f"invalid sort column: {sort!r}")
        if order not in ("asc", "desc"):
            raise ValueError(f"invalid order: {order!r}")
        conds: list[str] = []
        params: list[Any] = []
        if from_:
            conds.append("s.last_ts >= ?")
            params.append(_norm_bound(from_, end=False))
        if to:
            conds.append("s.first_ts <= ?")
            params.append(_norm_bound(to, end=True))
        _add_user_cond(conds, params, user_id)
        if profile:
            conds.append("s.profile = ?")
            params.append(profile)
        if entrypoint:
            conds.append("s.entrypoint = ?")
            params.append(entrypoint)
        where = f" WHERE {' AND '.join(conds)}" if conds else ""
        total = await self._scalar(f"SELECT COUNT(*) FROM sessions s{where}", params)
        rows = await self._all(
            f"SELECT s.* FROM sessions s{where}"
            f" ORDER BY s.{sort} {order.upper()}, s.session_id LIMIT ? OFFSET ?",
            [*params, limit, offset],
        )
        return {
            "total": total,
            "limit": limit,
            "offset": offset,
            "sessions": [dict(row) for row in rows],
        }

    # ------------------------------------------------------------------
    # api/sessions/{id}
    # ------------------------------------------------------------------

    async def session_detail(self, session_id: str) -> dict[str, Any] | None:
        session = await self._row(
            "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
        )
        if session is None:
            return None
        turns = await self._all(
            "SELECT * FROM turns WHERE session_id = ? ORDER BY ts", (session_id,)
        )
        subagents = await self._all(
            "SELECT * FROM subagent_runs WHERE session_id = ?"
            " ORDER BY started_ts, agent_id",
            (session_id,),
        )
        events = await self._all(
            "SELECT * FROM derived_events WHERE session_id = ? ORDER BY ts",
            (session_id,),
        )
        file_row = await self._row(
            "SELECT missing_since FROM files WHERE session_id = ?"
            " AND kind = 'session'",
            (session_id,),
        )
        missing = file_row is None or file_row["missing_since"] is not None
        return {
            "session": dict(session),
            "turns": [dict(row) for row in turns],
            "subagents": [dict(row) for row in subagents],
            "events": [
                {**dict(row), "detail": _json_detail(row["detail"])} for row in events
            ],
            "missing_transcript": missing,
        }

    # ------------------------------------------------------------------
    # api/sessions/{id}/timeline
    # ------------------------------------------------------------------

    async def session_timeline(
        self, session_id: str, agent_id: str = ""
    ) -> dict[str, Any] | None:
        exists = await self._scalar(
            "SELECT 1 FROM sessions WHERE session_id = ?", (session_id,)
        )
        if exists is None:
            return None
        if agent_id == "*":
            agent_cond = ""
            params: list[Any] = [session_id]
        elif agent_id == "":
            agent_cond = " AND agent_id IS NULL"
            params = [session_id]
        else:
            agent_cond = " AND agent_id = ?"
            params = [session_id, agent_id]
        call_rows = await self._all(
            "SELECT * FROM api_calls WHERE session_id = ?"
            f"{agent_cond} ORDER BY ts, request_id LIMIT ?",
            [*params, TIMELINE_ROW_CAP + 1],
        )
        truncated = len(call_rows) > TIMELINE_ROW_CAP
        call_rows = call_rows[:TIMELINE_ROW_CAP]
        # Server-computed cumulatives, per lane (agent_id) in ts order.
        lane_cum: dict[str | None, tuple[int, float]] = {}
        calls: list[dict[str, Any]] = []
        for row in call_rows:
            call = dict(row)
            call["context_provided"] = (
                call["input_tokens"]
                + call["cache_read_tokens"]
                + call["cache_creation_tokens"]
            )
            cum_out, cum_cost = lane_cum.get(call["agent_id"], (0, 0.0))
            cum_out += int(call["output_tokens"])
            cum_cost += float(call["est_cost_usd"])
            lane_cum[call["agent_id"]] = (cum_out, cum_cost)
            call["cum_output_tokens"] = cum_out
            call["cum_est_cost_usd"] = round(cum_cost, 6)
            calls.append(call)
        tools = await self._all(
            "SELECT * FROM tool_calls WHERE session_id = ?"
            f"{agent_cond} ORDER BY ts, tool_use_id LIMIT ?",
            [*params, TIMELINE_ROW_CAP],
        )
        events = await self._all(
            "SELECT * FROM derived_events WHERE session_id = ?"
            f"{agent_cond} ORDER BY ts LIMIT ?",
            [*params, TIMELINE_ROW_CAP],
        )
        turns = await self._all(
            "SELECT * FROM turns WHERE session_id = ? ORDER BY ts LIMIT ?",
            (session_id, TIMELINE_ROW_CAP),
        )
        subagents = await self._all(
            "SELECT * FROM subagent_runs WHERE session_id = ?"
            " ORDER BY started_ts, agent_id",
            (session_id,),
        )
        return {
            "session_id": session_id,
            "agent_id": agent_id,
            "calls": calls,
            "tools": [dict(row) for row in tools],
            "events": [
                {**dict(row), "detail": _json_detail(row["detail"])} for row in events
            ],
            "turns": [dict(row) for row in turns],
            "subagents": [dict(row) for row in subagents],
            "truncated": truncated,
        }

    # ------------------------------------------------------------------
    # api/calls/{request_id} (+ raw location)
    # ------------------------------------------------------------------

    async def get_call(self, request_id: str) -> dict[str, Any] | None:
        row = await self._row(
            "SELECT a.*, f.path AS file_path, f.missing_since AS file_missing_since"
            " FROM api_calls a JOIN files f ON f.id = a.file_id"
            " WHERE a.request_id = ?",
            (request_id,),
        )
        if row is None:
            return None
        call = dict(row)
        call["context_provided"] = (
            call["input_tokens"]
            + call["cache_read_tokens"]
            + call["cache_creation_tokens"]
        )
        return call

    async def call_location(self, request_id: str) -> tuple[str, int] | None:
        """(file path, byte offset of the line) for api/calls/{id}/raw."""
        row = await self._row(
            "SELECT f.path, a.entry_offset FROM api_calls a"
            " JOIN files f ON f.id = a.file_id WHERE a.request_id = ?",
            (request_id,),
        )
        if row is None:
            return None
        return str(row["path"]), int(row["entry_offset"])

    # ------------------------------------------------------------------
    # api/reconciliation
    # ------------------------------------------------------------------

    async def reconciliation(
        self, *, user_id: int | None = None, profile: str | None = None
    ) -> dict[str, Any]:
        bot_rows = await self._all("SELECT * FROM bot_sessions ORDER BY created_at")
        index_sessions = {
            row["session_id"]: row
            for row in await self._all(
                "SELECT session_id, api_call_count, output_tokens, est_cost_usd,"
                " first_ts, last_ts, entrypoint, profile FROM sessions"
            )
        }
        present = {
            row["session_id"]
            for row in await self._all(
                "SELECT session_id FROM files WHERE kind = 'session'"
                " AND missing_since IS NULL"
            )
        }
        transcript_turns = {
            row["session_id"]: row["n"]
            for row in await self._all(
                "SELECT session_id, COUNT(*) AS n FROM turns"
                " WHERE source = 'transcript' GROUP BY session_id"
            )
        }
        message_rows = {
            row["session_id"]: row["n"]
            for row in await self._all(
                "SELECT session_id, COUNT(*) AS n FROM bot_messages"
                " GROUP BY session_id"
            )
        }
        oldest_transcript = await self._scalar(
            "SELECT MIN(s.first_ts) FROM sessions s JOIN files f"
            " ON f.session_id = s.session_id AND f.kind = 'session'"
            " WHERE f.missing_since IS NULL AND s.first_ts IS NOT NULL"
        )
        rows: list[dict[str, Any]] = []
        for bot in bot_rows:
            bot_profile = _profile_from_cwd(bot["project_path"])
            if user_id is not None and bot["user_id"] != user_id:
                continue
            if profile and bot_profile != profile:
                continue
            session_id = bot["session_id"]
            disk = index_sessions.get(session_id)
            is_present = session_id in present
            flags = self._reconciliation_flags(
                bot=bot,
                disk=disk,
                is_present=is_present,
                bot_message_rows=message_rows.get(session_id, 0),
                oldest_transcript=oldest_transcript,
            )
            rows.append(
                {
                    "session_id": session_id,
                    "user_id": bot["user_id"],
                    "project_path": bot["project_path"],
                    "profile": bot_profile,
                    "bot": {
                        "message_count": bot["message_count"],
                        "total_cost": bot["total_cost"],
                        "total_turns": bot["total_turns"],
                        "created_at": bot["created_at"],
                        "last_used": bot["last_used"],
                    },
                    "disk": {
                        "present": is_present,
                        "api_calls": disk["api_call_count"] if disk else None,
                        "user_turns": transcript_turns.get(session_id, 0),
                        "output_tokens": disk["output_tokens"] if disk else None,
                        "est_cost": disk["est_cost_usd"] if disk else None,
                        "first_ts": disk["first_ts"] if disk else None,
                        "last_ts": disk["last_ts"] if disk else None,
                    },
                    "flags": flags,
                }
            )
        orphans = await self._orphans(profile)
        missing_count = sum(
            1
            for row in rows
            if "missing_transcript" in row["flags"]
            or "missing_transcript_retention" in row["flags"]
        )
        summary = {
            "bot_sessions": len(rows),
            "with_transcript": sum(1 for row in rows if row["disk"]["present"]),
            "missing_transcript": missing_count,
            "orphan_transcripts": len(orphans),
            "flagged": sum(1 for row in rows if row["flags"]),
            "bot_cost_total": round(
                sum(row["bot"]["total_cost"] or 0.0 for row in rows), 4
            ),
            "disk_est_cost_total": round(
                sum(row["disk"]["est_cost"] or 0.0 for row in rows), 4
            ),
        }
        return {"summary": summary, "rows": rows, "orphans": orphans}

    @staticmethod
    def _reconciliation_flags(
        *,
        bot: aiosqlite.Row,
        disk: aiosqlite.Row | None,
        is_present: bool,
        bot_message_rows: int,
        oldest_transcript: str | None,
    ) -> list[str]:
        flags: list[str] = []
        # sdk-py transcript user turns are deliberately not indexed (bot turns
        # come from bot.db), so the turn check compares the bot's per-session
        # message_count counter against its actual mirrored message rows.
        message_count = bot["message_count"] or 0
        if abs(message_count - bot_message_rows) > TURN_COUNT_TOLERANCE:
            flags.append("turn_count_mismatch")
        if not is_present:
            created_at = bot["created_at"]
            if (
                oldest_transcript
                and created_at
                and str(created_at) < str(oldest_transcript)
            ):
                flags.append("missing_transcript_retention")
            else:
                flags.append("missing_transcript")
            return flags
        bot_cost = float(bot["total_cost"] or 0.0)
        est_cost = float(disk["est_cost_usd"]) if disk is not None else 0.0
        if abs(bot_cost - est_cost) > max(
            COST_ABS_TOLERANCE_USD, COST_REL_TOLERANCE * abs(bot_cost)
        ):
            flags.append("cost_mismatch")
        if disk is not None and disk["first_ts"] and bot["created_at"]:
            first_dt = _parse_dt(str(disk["first_ts"]))
            created_dt = _parse_dt(str(bot["created_at"]))
            if first_dt is not None and created_dt is not None:
                skew = abs((first_dt - created_dt).total_seconds())
                if skew > TIME_SKEW_MAX_S:
                    flags.append("time_skew")
        return flags

    async def _orphans(self, profile: str | None) -> list[dict[str, Any]]:
        """sdk-py transcripts with no bot_sessions row. 'cli' and 'sdk-cli'
        are expected-non-bot entrypoints and excluded by construction."""
        sql = (
            "SELECT session_id, cwd, profile, entrypoint, first_ts, last_ts,"
            " api_call_count, est_cost_usd FROM sessions"
            " WHERE entrypoint = 'sdk-py' AND in_bot_db = 0"
        )
        params: list[Any] = []
        if profile:
            sql += " AND profile = ?"
            params.append(profile)
        sql += " ORDER BY first_ts"
        return [dict(row) for row in await self._all(sql, params)]

    # ------------------------------------------------------------------
    # api/chains
    # ------------------------------------------------------------------

    async def chains(self) -> dict[str, Any]:
        window_state = {
            (row["user_id"], row["working_dir"]): dict(row)
            for row in await self._all("SELECT * FROM bot_windows")
        }
        bot_sessions = await self._all("SELECT * FROM bot_sessions ORDER BY created_at")
        keys = sorted(
            set(window_state)
            | {(row["user_id"], row["project_path"]) for row in bot_sessions}
        )
        index_sessions = {
            row["session_id"]: row
            for row in await self._all(
                "SELECT session_id, first_call_context, bot_formula_tokens,"
                " seeded_start, api_call_count, est_cost_usd, first_ts, last_ts"
                " FROM sessions"
            )
        }
        messages_by_session: dict[str, list[aiosqlite.Row]] = defaultdict(list)
        for row in await self._all(
            "SELECT session_id, ts, window_id, cost FROM bot_messages ORDER BY ts"
        ):
            messages_by_session[row["session_id"]].append(row)
        chains: list[dict[str, Any]] = []
        for chain_user, working_dir in keys:
            chain_sessions = [
                row
                for row in bot_sessions
                if row["user_id"] == chain_user and row["project_path"] == working_dir
            ]
            chains.append(
                self._build_chain(
                    chain_user,
                    working_dir,
                    chain_sessions,
                    window_state.get((chain_user, working_dir)),
                    index_sessions,
                    messages_by_session,
                )
            )
        return {"chains": chains}

    @staticmethod
    def _build_chain(
        user_id: int,
        working_dir: str,
        chain_sessions: list[aiosqlite.Row],
        current_window: dict[str, Any] | None,
        index_sessions: dict[str, aiosqlite.Row],
        messages_by_session: dict[str, list[aiosqlite.Row]],
    ) -> dict[str, Any]:
        all_messages = sorted(
            (
                message
                for row in chain_sessions
                for message in messages_by_session.get(row["session_id"], [])
            ),
            key=lambda m: str(m["ts"]),
        )
        # NULL window_id → window 0 ("pre-window era", plan risk #3).
        windows_agg: dict[int, dict[str, Any]] = {}
        rotations = 0
        prev_wid: int | None = None
        for message in all_messages:
            wid = int(message["window_id"] or 0)
            if prev_wid is not None and wid > prev_wid:
                rotations += 1
            prev_wid = wid
            agg = windows_agg.setdefault(
                wid,
                {
                    "window_id": wid,
                    "message_count": 0,
                    "first_ts": message["ts"],
                    "last_ts": message["ts"],
                    "total_cost": 0.0,
                },
            )
            agg["message_count"] += 1
            agg["last_ts"] = message["ts"]
            agg["total_cost"] = round(
                agg["total_cost"] + float(message["cost"] or 0.0), 6
            )
        session_windows: list[tuple[int, int] | None] = []
        for row in chain_sessions:
            wids = [
                int(m["window_id"] or 0)
                for m in messages_by_session.get(row["session_id"], [])
            ]
            session_windows.append((min(wids), max(wids)) if wids else None)
        sessions_out: list[dict[str, Any]] = []
        for i, row in enumerate(chain_sessions):
            rotated_into: str | None = None
            span = session_windows[i]
            if span is not None and i + 1 < len(chain_sessions):
                next_span = session_windows[i + 1]
                if next_span is not None and next_span[0] > span[1]:
                    rotated_into = chain_sessions[i + 1]["session_id"]
            index_row = index_sessions.get(row["session_id"])
            sessions_out.append(
                {
                    "session_id": row["session_id"],
                    "created_at": row["created_at"],
                    "last_used": row["last_used"],
                    "message_count": row["message_count"],
                    "total_cost": row["total_cost"],
                    "min_window": span[0] if span else None,
                    "max_window": span[1] if span else None,
                    "rotated_into": rotated_into,
                    "in_index": index_row is not None,
                    "first_call_context": (
                        index_row["first_call_context"] if index_row else None
                    ),
                    "bot_formula_tokens": (
                        index_row["bot_formula_tokens"] if index_row else None
                    ),
                    "seeded_start": (
                        bool(index_row["seeded_start"]) if index_row else None
                    ),
                    "api_call_count": (
                        index_row["api_call_count"] if index_row else None
                    ),
                    "est_cost_usd": index_row["est_cost_usd"] if index_row else None,
                }
            )
        return {
            "user_id": user_id,
            "working_dir": working_dir,
            "current_window": current_window,
            "windows": [windows_agg[wid] for wid in sorted(windows_agg)],
            "sessions": sessions_out,
            "rotations_observed": rotations,
        }

    # ------------------------------------------------------------------
    # api/chains/detail
    # ------------------------------------------------------------------

    async def chain_detail(
        self,
        *,
        user_id: int,
        working_dir: str,
        since: str | None = None,
        until: str | None = None,
    ) -> dict[str, Any] | None:
        chain_sessions = await self._all(
            "SELECT * FROM bot_sessions WHERE user_id = ? AND project_path = ?"
            " ORDER BY created_at",
            (user_id, working_dir),
        )
        if not chain_sessions:
            return None
        session_ids = [row["session_id"] for row in chain_sessions]
        qmarks = ",".join("?" * len(session_ids))
        messages_by_session: dict[str, list[aiosqlite.Row]] = defaultdict(list)
        for row in await self._all(
            "SELECT session_id, ts, window_id FROM bot_messages"
            f" WHERE session_id IN ({qmarks}) ORDER BY ts",
            session_ids,
        ):
            messages_by_session[row["session_id"]].append(row)
        expiries = {
            (row["session_id"], row["ts"])
            for row in await self._all(
                "SELECT session_id, ts FROM derived_events"
                f" WHERE kind = 'cache_expiry' AND session_id IN ({qmarks})"
                " AND agent_id IS NULL",
                session_ids,
            )
        }
        calls_by_session: dict[str, list[aiosqlite.Row]] = defaultdict(list)
        for row in await self._all(
            "SELECT session_id, ts, input_tokens, output_tokens,"
            " cache_read_tokens, cache_creation_tokens FROM api_calls"
            f" WHERE session_id IN ({qmarks}) AND agent_id IS NULL ORDER BY ts",
            session_ids,
        ):
            calls_by_session[row["session_id"]].append(row)
        # Replay the bot's window counter: SUM(input+output), cache excluded,
        # reset only at session boundaries that correspond to an observed
        # window_id increment between consecutive sessions.
        series: list[dict[str, Any]] = []
        counter = 0
        prev_max_window: int | None = None
        for session_row in chain_sessions:
            session_id = session_row["session_id"]
            messages = messages_by_session.get(session_id, [])
            window_ids = [int(m["window_id"] or 0) for m in messages]
            min_window = min(window_ids) if window_ids else None
            if (
                prev_max_window is not None
                and min_window is not None
                and min_window > prev_max_window
            ):
                counter = 0
            pointer = 0
            current_window = min_window
            first_of_session = True
            for call in calls_by_session.get(session_id, []):
                while pointer < len(messages) and str(messages[pointer]["ts"]) <= str(
                    call["ts"]
                ):
                    current_window = int(messages[pointer]["window_id"] or 0)
                    pointer += 1
                counter += int(call["input_tokens"]) + int(call["output_tokens"])
                series.append(
                    {
                        "ts": call["ts"],
                        "session_id": session_id,
                        "window_id": current_window,
                        "context_provided": (
                            int(call["input_tokens"])
                            + int(call["cache_read_tokens"])
                            + int(call["cache_creation_tokens"])
                        ),
                        "bot_counter_cum": counter,
                        "first_of_session": first_of_session,
                        "cache_expiry": (session_id, call["ts"]) in expiries,
                    }
                )
                first_of_session = False
            if window_ids:
                prev_max_window = max(window_ids)
        if since:
            lo = _norm_bound(since, end=False)
            series = [point for point in series if str(point["ts"]) >= lo]
        if until:
            hi = _norm_bound(until, end=True)
            series = [point for point in series if str(point["ts"]) <= hi]
        window_row = await self._row(
            "SELECT cumulative_tokens FROM bot_windows"
            " WHERE user_id = ? AND working_dir = ?",
            (user_id, working_dir),
        )
        return {
            "user_id": user_id,
            "working_dir": working_dir,
            "series": series,
            "annotations": {
                "eval_threshold": EVAL_THRESHOLD,
                "refresh_threshold": REFRESH_THRESHOLD,
                "db_current_counter": (
                    window_row["cumulative_tokens"] if window_row else None
                ),
            },
        }
