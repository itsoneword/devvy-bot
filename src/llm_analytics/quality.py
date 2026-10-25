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
- log_calls:           log_* tool calls (tool_calls), activity denominator.
- claimed_not_logged:  turns whose reply claims «записал/записано/logged»
                       with no log_* tool call in that turn.
- complaint_hits:      user messages matching a complaint pattern.
- user_messages:       all user turns (denominator for rates).
- recipe_turns:        turns whose response looks like a written-out recipe
                       (dv-5d5f Tier-1 mechanical check).
- recipe_turns_unsearched: of those, the ones with no WebSearch/WebFetch
                       call recorded against that message_id.
- verification_calls:  WebSearch/WebFetch/Agent/Task calls (dv-9f41), so
                       calls-per-turn = verification_calls / user_messages.
- verified_turns:      turns with >=1 such call.
- deep_research_turns: turns with more than CHEAP_CHECK_MAX such calls — the
                       turn-1817 tail (17 WebSearch for one chest session).
- program_turns:       turns whose response prescribes a training programme
                       (sets x reps / weights / %1RM / RPE in a list).
- program_turns_unchecked: of those, no Web*/subagent call AND no read of the
                       user's own data — the dv-7a3c tail. Both tails are
                       failures; the band between them is the target.
"""

import re
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Sequence

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

# dv-9f41: a training prescription. List structure plus at least one
# load-bearing number — set x rep scheme, a weight with a unit, %1RM or RPE.
# Physiology prose with citations has none of these and is not load-bearing.
_SET_REP_RE = re.compile(r"\b\d+\s*[x×х]\s*\d+")
_WEIGHT_RE = re.compile(r"\b\d+(?:[.,]\d+)?\s*(?:кг|kg|lbs?|фунт\w*)(?!\w)", re.I)
_PERCENT_1RM_RE = re.compile(r"\d+\s*%\s*(?:от\s*)?(?:1\s*RM|1ПМ|ПМ)", re.I)
_RPE_RE = re.compile(r"\b(?:RPE|RIR)\s*\d+", re.I)
_PROGRAM_NUMBER_RES = (_SET_REP_RE, _WEIGHT_RE, _PERCENT_1RM_RE, _RPE_RE)
# dv-9f41 band edge: 1-3 search-class calls is the cheap check; above that a
# turn is doing research. Same number as VERIFICATION_BUDGET_CONFIRM_MAX's
# default, kept a constant so historical counts don't move with config.
CHEAP_CHECK_MAX = 3

# Search-class tools: each call pulls external content into the turn. A
# foreground subagent is one row in tool_calls but weighs 2 against the live
# budget: one spawn routinely fans out into many nested Web* calls (turn
# 1817: Agent x1 -> WebSearch x17 + WebFetch x2).
SUBAGENT_TOOLS = frozenset({"Agent", "Task"})
SUBAGENT_WEIGHT = 2
# Local data reads that ground a training prescription in the user's own
# history. The load-bearing claim in turn 1817 was "are these sets/weights
# defensible for THIS user" — lift_history answers that for free.
_DATA_READ_RE = re.compile(
    r"(?:^|__)(?:lift_history|day_summary|week_summary|sleep_last)$"
)

_METRICS = (
    "pinned_facts",
    "log_calls",
    "claimed_not_logged",
    "complaint_hits",
    "user_messages",
    "recipe_turns",
    "recipe_turns_unsearched",
    "verification_calls",
    "verified_turns",
    "deep_research_turns",
    "program_turns",
    "program_turns_unchecked",
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


def tool_weight(tool_name: str) -> int:
    """Budget units one call costs (0 = not search-class)."""
    if tool_name in _WEB_SEARCH_TOOLS:
        return 1
    if tool_name in SUBAGENT_TOOLS:
        return SUBAGENT_WEIGHT
    return 0


def verification_calls(tool_names: Sequence[str]) -> int:
    """Unweighted count of search-class calls (Web* + subagent spawns)."""
    return sum(1 for name in tool_names if tool_weight(name))


def is_grounded(tool_names: Sequence[str]) -> bool:
    """True if the turn checked anything: a web search/fetch, a research
    subagent, or a read of the user's own logged data."""
    return any(tool_weight(name) or _DATA_READ_RE.search(name) for name in tool_names)


def looks_like_training_program(response: str) -> bool:
    """True if ``response`` prescribes a workout: a list (>=2 items) plus at
    least one set x rep, weight, %1RM or RPE figure. Heuristic, the coach-side
    twin of ``looks_like_recipe`` (dv-9f41)."""
    if len(_LIST_ITEM_RE.findall(response)) < 2:
        return False
    return any(rx.search(response) for rx in _PROGRAM_NUMBER_RES)


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
    tools_by_message: dict[int, list[str]] = defaultdict(list)
    if "tool_calls" in tables:
        for row in src.execute(
            "SELECT session_id, message_id, tool_name, started_at AS timestamp "
            "FROM tool_calls WHERE parent_tool_use_id IS NULL"
        ):
            if row["message_id"] is not None:
                tools_by_message[int(row["message_id"])].append(row["tool_name"])
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
            turn_tools = tools_by_message.get(int(row["message_id"]), [])
            calls = verification_calls(turn_tools)
            bucket["verification_calls"] += calls
            if calls:
                bucket["verified_turns"] += 1
            if calls > CHEAP_CHECK_MAX:
                bucket["deep_research_turns"] += 1
            if looks_like_training_program(response):
                bucket["program_turns"] += 1
                if not is_grounded(turn_tools):
                    bucket["program_turns_unchecked"] += 1

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
            " recipe_turns, recipe_turns_unsearched, verification_calls,"
            " verified_turns, deep_research_turns, program_turns,"
            " program_turns_unchecked)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
    logger.debug("quality_mirrored", rows=len(rows))
    return True
