"""Tier-1 bot-quality signals mirrored from bot.db (dv-37e9).

Mechanical (no-LLM) daily counters, full refresh each scan cycle like
botdb.mirror_bot_db, source opened strictly read-only. Only per-day counts
per (profile, user) cross into the index DB — never prompt/response text.

(The sibling ``answer_scores`` table — Tier-2, dv-300d, written by
``scripts/score_answers.py`` rather than this module — is a deliberate,
narrower exception to that rule: it stores a capped ~300-char rationale per
score, because a quality score with no rationale is undiagnosable. That
table is local-only, same as this one; this module's own counters still
carry no prompt/response text.)

Signals:
- pinned_facts:        new memory_facts rows with tier='pinned' (each new
                       standing instruction is a user-unhappiness proxy).
- log_calls:           log_* tool calls (tool_usage), activity denominator.
- claimed_not_logged:  turns whose reply claims «записал/записано/logged»
                       with no log_* tool call in that turn.
- complaint_hits:      user messages matching a complaint pattern.
- user_messages:       all user turns (denominator for rates).
- recipe_turns:        turns whose response looks like a written-out recipe
                       (dv-5d5f Tier-1 mechanical check).
- recipe_turns_unsearched: of those, the ones with no WebSearch/WebFetch
                       call recorded against that message_id.
"""

import re
import sqlite3
from collections import defaultdict
from pathlib import Path

import structlog

logger = structlog.get_logger(__name__)

# Complaint patterns over USER messages. THE single place to extend/tune;
# matched case-insensitively, one hit per message regardless of match count.
COMPLAINT_PATTERNS: tuple[str, ...] = (
    r"я\s+(?:\S+\s+)?не\s+просил",  # «я не просил», «я тебя не просил»
    r"не\s+пиши\s+так",
    r"зачем\s+ты",
    r"убери",
    r"не\s+надо\s+было",
    r"я\s+же\s+(?:\S+\s+)?говорил",  # «я же говорил», «я же тебе говорила»
)

# Assistant replies that claim something was logged/recorded.
CLAIM_PATTERNS: tuple[str, ...] = (r"записал", r"записано", r"\blogged\b")

_COMPLAINT_RE = re.compile("|".join(COMPLAINT_PATTERNS), re.IGNORECASE)
_CLAIM_RE = re.compile("|".join(CLAIM_PATTERNS), re.IGNORECASE)

# log_* tools: bare names or MCP-qualified ones (mcp__agent__log_food, ...).
_LOG_TOOL_RE = re.compile(r"(?:^|__)log_")

# dv-5d5f Tier-1 mechanical check: a response "looks like a written-out
# recipe" if it has list structure (numbered or bulleted lines, >=2) AND
# names at least a couple of quantities with units — one stray number isn't
# a recipe, but ingredients almost always carry several unit-qualified
# amounts. Covers RU and EN unit spellings since chef's traffic is mixed.
_LIST_ITEM_RE = re.compile(r"(?m)^\s*(?:\d+[.)]|[-*])\s+\S")
_QUANTITY_UNIT_RE = re.compile(
    r"\d+[.,]?\d*\s*("
    r"г|гр|кг|мл|л|ст\.?\s*л|ч\.?\s*л|шт|стакан|"
    r"g|kg|ml|l|tbsp|tsp|cup|oz|lb"
    r")\b",
    re.IGNORECASE,
)
_WEB_SEARCH_TOOLS = frozenset({"WebSearch", "WebFetch"})

_METRICS = (
    "pinned_facts",
    "log_calls",
    "claimed_not_logged",
    "complaint_hits",
    "user_messages",
    "recipe_turns",
    "recipe_turns_unsearched",
)

_Key = tuple[str, str | None, int | None]  # (date, profile, user_id)


def is_log_tool(name: str) -> bool:
    return _LOG_TOOL_RE.search(name) is not None


def looks_like_recipe(response: str) -> bool:
    """True if ``response`` has the shape of a written-out recipe: a list
    (numbered or bulleted) plus at least two unit-qualified quantities.
    Heuristic, not a parser — see dv-5d5f's mechanical enforcement check."""
    if len(_LIST_ITEM_RE.findall(response)) < 2:
        return False
    return len(_QUANTITY_UNIT_RE.findall(response)) >= 2


def agent_parts(agent_id: str) -> tuple[str | None, int | None]:
    """Split a memory_facts agent_id like 'coach#u111111111' into
    (profile, user_id). Plain ids ('chef', 'devops') have no user."""
    profile, _, user_part = agent_id.partition("#")
    user_id: int | None = None
    if user_part:
        try:
            user_id = int(user_part.lstrip("u"))
        except ValueError:
            user_id = None
    return profile or None, user_id


def _date(ts: object) -> str | None:
    """Calendar date from an ISO-ish timestamp ('2026-08-22T…' or with space)."""
    if isinstance(ts, str) and len(ts) >= 10:
        return ts[:10]
    return None


def compute_quality_daily(src: sqlite3.Connection) -> list[tuple]:
    """Per-(date, profile, user) counters from an open bot.db connection.

    Returns rows shaped for quality_daily. Missing source tables (older
    bot.db) are tolerated: their counters simply stay zero.
    """
    # Imported here: indexer imports this module, so a top-level import of
    # indexer would be circular.
    from .indexer import _profile_from_cwd

    tables = {
        row[0]
        for row in src.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    counts: dict[_Key, dict[str, int]] = defaultdict(lambda: dict.fromkeys(_METRICS, 0))

    scope: dict[str, tuple[str | None, int | None]] = {}  # session -> (profile, user)
    if "sessions" in tables:
        for row in src.execute(
            "SELECT session_id, user_id, project_path FROM sessions"
        ):
            scope[row["session_id"]] = (
                _profile_from_cwd(row["project_path"]),
                row["user_id"],
            )

    logged_message_ids: set[int] = set()
    web_searched_message_ids: set[int] = set()
    if "tool_usage" in tables:
        for row in src.execute(
            "SELECT session_id, message_id, tool_name, timestamp FROM tool_usage"
        ):
            if row["tool_name"] in _WEB_SEARCH_TOOLS and row["message_id"] is not None:
                web_searched_message_ids.add(int(row["message_id"]))
            if not is_log_tool(row["tool_name"]):
                continue
            if row["message_id"] is not None:
                logged_message_ids.add(int(row["message_id"]))
            date = _date(row["timestamp"])
            if date is None:
                continue
            profile, user_id = scope.get(row["session_id"], (None, None))
            counts[(date, profile, user_id)]["log_calls"] += 1

    if "messages" in tables:
        for row in src.execute(
            "SELECT message_id, session_id, timestamp, prompt, response FROM messages"
        ):
            date = _date(row["timestamp"])
            if date is None:
                continue
            profile, user_id = scope.get(row["session_id"], (None, None))
            bucket = counts[(date, profile, user_id)]
            bucket["user_messages"] += 1
            prompt = row["prompt"] or ""
            if _COMPLAINT_RE.search(prompt):
                bucket["complaint_hits"] += 1
            response = row["response"] or ""
            if (
                _CLAIM_RE.search(response)
                and int(row["message_id"]) not in logged_message_ids
            ):
                bucket["claimed_not_logged"] += 1
            if looks_like_recipe(response):
                bucket["recipe_turns"] += 1
                if int(row["message_id"]) not in web_searched_message_ids:
                    bucket["recipe_turns_unsearched"] += 1

    if "memory_facts" in tables:
        for row in src.execute(
            "SELECT agent_id, created_at FROM memory_facts WHERE tier = 'pinned'"
        ):
            date = _date(row["created_at"])
            if date is None:
                continue
            profile, user_id = agent_parts(row["agent_id"])
            counts[(date, profile, user_id)]["pinned_facts"] += 1

    return [
        (date, profile, user_id, *(bucket[metric] for metric in _METRICS))
        for (date, profile, user_id), bucket in sorted(
            counts.items(),
            key=lambda item: (item[0][0], item[0][1] or "", item[0][2] or 0),
        )
    ]


def mirror_quality(conn: sqlite3.Connection, bot_db_path: Path) -> bool:
    """Full refresh of quality_daily from bot.db. False if bot.db is absent."""
    if not bot_db_path.exists():
        logger.warning("bot_db_missing", path=str(bot_db_path))
        return False
    src = sqlite3.connect(f"file:{bot_db_path}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    try:
        rows = compute_quality_daily(src)
    finally:
        src.close()
    with conn:
        conn.execute("DELETE FROM quality_daily")
        conn.executemany(
            "INSERT INTO quality_daily (date, profile, user_id, pinned_facts,"
            " log_calls, claimed_not_logged, complaint_hits, user_messages,"
            " recipe_turns, recipe_turns_unsearched)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
    logger.debug("quality_mirrored", rows=len(rows))
    return True
