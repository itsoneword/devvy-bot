"""Usage-limit classifier (dv-7ad9).

A Claude turn that runs out of the subscription ends in an error result
whose text is the CLI's limit line, e.g. ``You've hit your weekly limit ·
resets 9am (UTC)``. That is not a failure of the work: nothing helps until the
reset. :func:`parse_limit` turns such a line into a :class:`Limit` with an
aware UTC ``until``; any other text is ``None``.

agentjob exits :data:`QUOTA_EXIT` on a limit and prints :func:`quota_line` on
stderr, so whoever reaps the job (the pump) reads ``until`` back with
:func:`parse_quota_line` instead of parsing the CLI's wording a second time.

The CLI's templates (2.1.310 binary): ``You've hit your ${kind}`` with kind
"session limit" (five_hour), "weekly limit" (seven_day), an Opus-only weekly
limit, "monthly spend limit", "team's shared budget"; suffix ``· resets
<time>``, the time as ``9am (UTC)``, ``2:30pm``, ``Oct 27, 9am (Europe/X)``
or ``resets in …``. Codex says "You've hit your usage limit … try again at
<time>"; no codex binary here to verify, so that one is best effort.

The shared record (phase 2): whoever reaps a job that ended on a limit
(``bgjob summarize``, after every job) calls :func:`record`; whoever is about to
start a model job asks :func:`active`. The record is one small JSON file
(``DataLayout.model_quota``) and expires by itself: past ``until`` it is no
longer active, nobody deletes it.

The same file also holds the owner's job switch (dv-d63d, ``job_route`` key,
see :func:`set_job_route`) and the latest Claude subscription usage (dv-55ad,
``claude_usage`` key, see :func:`record_usage`). Each writer rewrites only its
own keys, locked and atomic (:func:`update_record`).
"""

from __future__ import annotations

import fcntl
import json
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import structlog

from .types import CLAUDE_HARNESS, CODEX_HARNESS, OPENROUTER_HARNESS

logger = structlog.get_logger()

# agentjob's exit code for "ended on a usage limit" (0 ok, 1 failed,
# 3 incomplete: job.py).
QUOTA_EXIT = 4
# No reset time in the message, or one that cannot be read: try again then.
FALLBACK = timedelta(hours=1)
# Longest believable pause; anything later is a misparse.
MAX_PAUSE = timedelta(days=8)

_LIMIT_RE = re.compile(
    r"\b(?:you'?ve|you have)\s+(?:hit|reached)\s+your\s+"
    r"(?P<kind>[^·.\n]{0,40}?\b(?:limit|budget))\b",
    re.IGNORECASE,
)
_RESET_RE = re.compile(
    r"\b(?:resets|try again)\s+(?:(?P<prep>at|in)\s+)?(?P<spec>[^\n]+)",
    re.IGNORECASE,
)
_DURATION_RE = re.compile(
    r"(\d+)\s*(d|days?|h|hrs?|hours?|m|mins?|minutes?)\b", re.IGNORECASE
)
_MONTHS = {
    m: i
    for i, m in enumerate(
        [
            "jan",
            "feb",
            "mar",
            "apr",
            "may",
            "jun",
            "jul",
            "aug",
            "sep",
            "oct",
            "nov",
            "dec",
        ],
        start=1,
    )
}
_DATE_RE = re.compile(
    r"\b(?P<mon>[A-Za-z]{3})[a-z]*\.?\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?,?"
    r"(?:\s+(?P<year>\d{4}),?)?",
)
_CLOCK_RE = re.compile(
    r"\b(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ampm>[ap]\.?m\.?)?(?![\w:])",
    re.IGNORECASE,
)
_TZ_RE = re.compile(r"\(([^()]+)\)")
_FAILED_PREFIX = "agentjob failed: "
_QUOTA_LINE_RE = re.compile(r"^agentjob quota: (?P<msg>.*) · until (?P<until>\S+)\s*$")


@dataclass(frozen=True)
class Limit:
    kind: str  # "weekly limit", "session limit", "usage limit", ...
    until: datetime  # aware UTC: when to try again
    message: str  # the limit text as the harness wrote it


def parse_limit(text: str, now: Optional[datetime] = None) -> Optional[Limit]:
    """The usage limit `text` reports, or None when it is some other error."""
    match = _LIMIT_RE.search(text or "")
    if not match:
        return None
    now = (now or datetime.now(UTC)).astimezone(UTC)
    line = text[match.start() :].splitlines()[0].strip()
    until = _reset_time(line, now)
    if until is None or until <= now or until > now + MAX_PAUSE:
        until = now + FALLBACK
    return Limit(
        kind=" ".join(match.group("kind").split()).lower(),
        until=until,
        message=line[:300],
    )


def quota_line(limit: Limit) -> str:
    """The stderr line agentjob prints on a limit hit."""
    until = limit.until.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"agentjob quota: {limit.message} · until {until}"


def parse_quota_line(text: str) -> Optional[Limit]:
    """The Limit a :func:`quota_line` carries, or None for any other line."""
    match = _QUOTA_LINE_RE.search((text or "").strip())
    if not match:
        return None
    try:
        until = datetime.fromisoformat(match.group("until").replace("Z", "+00:00"))
    except ValueError:
        return None
    if until.tzinfo is None:
        until = until.replace(tzinfo=UTC)
    msg = match.group("msg")
    kind = _LIMIT_RE.search(msg)
    return Limit(
        kind=" ".join(kind.group("kind").split()).lower() if kind else "limit",
        until=until.astimezone(UTC),
        message=msg,
    )


def find_limit(text: str, now: Optional[datetime] = None) -> Optional[Limit]:
    """A job's stderr: its quota line if it has one, else agentjob's own
    `agentjob failed: <limit text>` line (a job started before agentjob knew
    QUOTA_EXIT). Only agentjob's lines count: a limit message quoted in some
    other output is not a limit hit. None otherwise."""
    lines = (text or "").splitlines()
    for line in reversed(lines):
        limit = parse_quota_line(line)
        if limit is not None:
            return limit
    for line in reversed(lines):
        if line.startswith(_FAILED_PREFIX):
            limit = parse_limit(line[len(_FAILED_PREFIX) :], now)
            if limit is not None:
                return limit
    return None


def record(limit: Limit, source: str) -> None:
    """Record a limit hit for every later job. A hit with an earlier reset
    never shortens a record that is still active (a session limit inside a
    weekly one). Best effort: an unwritable data root records nothing."""
    current = active(datetime.now(UTC))
    if current is not None and current.until >= limit.until:
        return
    body = {
        "kind": limit.kind,
        "until": limit.until.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "message": limit.message,
        "source": source,
        "recorded": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    try:
        update_record(lambda rec: rec.update(body))
    except OSError:
        pass


def active(now: Optional[datetime] = None) -> Optional[Limit]:
    """The recorded limit if its reset is still ahead of `now`, else None.
    No record, an expired one or an unreadable one: None (never blocks)."""
    from src.utils.paths import data_layout

    now = (now or datetime.now(UTC)).astimezone(UTC)
    try:
        body = json.loads(data_layout().model_quota.read_text(encoding="utf-8"))
        until = datetime.fromisoformat(str(body["until"]).replace("Z", "+00:00"))
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if until.tzinfo is None:
        until = until.replace(tzinfo=UTC)
    until = until.astimezone(UTC)
    if until <= now:
        return None
    return Limit(
        kind=str(body.get("kind") or "limit"),
        until=until,
        message=str(body.get("message") or ""),
    )


def _reset_time(line: str, now: datetime) -> Optional[datetime]:
    match = _RESET_RE.search(line)
    if not match:
        return None
    spec = match.group("spec").strip().rstrip(".")
    if (match.group("prep") or "").lower() == "in":
        return _after(spec, now)
    tz = _zone(spec)
    local_now = now.astimezone(tz)
    date = _DATE_RE.search(spec)
    rest = spec[date.end() :] if date and date.group("mon").lower() in _MONTHS else spec
    clock = _CLOCK_RE.search(_TZ_RE.sub(" ", rest))
    if not clock or not (clock.group("ampm") or clock.group("m")):
        return None
    hour, minute = int(clock.group("h")), int(clock.group("m") or 0)
    ampm = (clock.group("ampm") or "").lower().replace(".", "")
    if ampm:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if ampm == "pm" else 0)
    if hour > 23 or minute > 59:
        return None
    if date and date.group("mon").lower() in _MONTHS:
        month, day = _MONTHS[date.group("mon").lower()], int(date.group("day"))
        year = int(date.group("year") or local_now.year)
        try:
            at = datetime(year, month, day, hour, minute, tzinfo=tz)
            if at <= local_now and not date.group("year"):
                at = at.replace(year=year + 1)
        except ValueError:
            return None
        return at.astimezone(UTC)
    at = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if at <= local_now:
        at += timedelta(days=1)
    return at.astimezone(UTC)


def _after(spec: str, now: datetime) -> Optional[datetime]:
    total = timedelta()
    for count, unit in _DURATION_RE.findall(spec):
        n, u = int(count), unit.lower()[0]
        total += {"d": timedelta(days=n), "h": timedelta(hours=n)}.get(
            u, timedelta(minutes=n)
        )
    return now + total if total else None


def _zone(spec: str) -> tzinfo:
    match = _TZ_RE.search(spec)
    name = match.group(1).strip() if match else ""
    if not name or name.upper() in ("UTC", "GMT", "Z"):
        return UTC
    try:
        return ZoneInfo(name)
    except Exception:
        return UTC


# --- the owner's job switch (dv-d63d) ---

JOB_ROUTE_KEY = "job_route"
# The harnesses the switch can send jobs to (routes.JOB_HARNESS_ROUTES).
JOB_SWITCH_HARNESSES = (CLAUDE_HARNESS, CODEX_HARNESS, OPENROUTER_HARNESS)


def _path() -> Path:
    from src.utils.paths import data_layout

    return data_layout().model_quota


def read_record() -> Dict[str, Any]:
    """The whole record; {} when absent, unreadable or not a JSON object."""
    try:
        body = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return body if isinstance(body, dict) else {}


def update_record(change: Callable[[Dict[str, Any]], None]) -> None:
    """Apply ``change`` to the record in place and write it back, locked and
    atomic. ``change`` touches only its caller's keys; the rest is kept."""
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_name(path.name + ".lock")
    with open(lock, "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        body = read_record()
        change(body)
        tmp = path.with_name(f".{path.name}.{os.getpid()}")
        tmp.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)


def job_route_override() -> Optional[str]:
    """The harness the owner switched every job to, or None (the table).

    A garbled or unknown value is ignored with a warning: the table wins.
    """
    value = read_record().get(JOB_ROUTE_KEY)
    if value is None:
        return None
    harness = value.get("harness") if isinstance(value, dict) else None
    if harness not in JOB_SWITCH_HARNESSES:
        logger.warning("job switch ignored: unknown value", job_route=value)
        return None
    return str(harness)


def job_route_info() -> Optional[Dict[str, Any]]:
    """The switch entry (harness, set_at, by) when one is on, else None."""
    if job_route_override() is None:
        return None
    return dict(read_record()[JOB_ROUTE_KEY])


def set_job_route(harness: Optional[str], by: str) -> None:
    """Move every job to ``harness``; None clears the switch (the table).

    Only the owner's tap in /menu → 🧠 Модели → «Фоновые задачи на» calls
    this (``switch_jobs``). Clearing an absent switch
    writes nothing.
    """
    if harness is not None and harness not in JOB_SWITCH_HARNESSES:
        raise ValueError(
            f"no job harness {harness!r}; one of {list(JOB_SWITCH_HARNESSES)}"
        )
    if harness is None and JOB_ROUTE_KEY not in read_record():
        return

    def change(body: Dict[str, Any]) -> None:
        if harness is None:
            body.pop(JOB_ROUTE_KEY, None)
        else:
            body[JOB_ROUTE_KEY] = {
                "harness": harness,
                "set_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "by": by,
            }

    update_record(change)
    logger.info("job switch set", harness=harness, by=by)


# --- live Claude subscription usage (dv-55ad) ---
#
# The CLI sends a ``rate_limit_event`` on every turn, also while the status is
# "allowed" (probe 2026-10-26, CLI 2.1.310); its ``unifiedWindows`` carry the
# utilization and reset of each window. Every Claude call site keeps the last
# one of its stream and records it once, after the stream: the chat turn and
# plain calls with :func:`record_usage_soon` (a worker thread, never inside the
# reader loop), agentjob (read-only box) by printing :func:`usage_line`, which
# ``bgjob summarize`` reads back with :func:`find_usage`. /limits renders it with
# :func:`claude_limits_text`; no model call.

USAGE_KEY = "claude_usage"
# Older than this, the panel says the numbers may have moved since.
USAGE_STALE = timedelta(hours=1)
_USAGE_PREFIX = "agentjob usage: "
_WINDOW_LABELS = {
    "five_hour": "5 ч",
    "seven_day": "7 дн.",
    "seven_day_opus": "7 дн. Opus",
    "seven_day_sonnet": "7 дн. Sonnet",
    "seven_day_overage_included": "7 дн. (с overage)",
}


@dataclass(frozen=True)
class UsageWindow:
    utilization: Optional[float]  # 0..1
    resets_at: Optional[datetime]  # aware UTC
    status: str


@dataclass(frozen=True)
class ClaudeUsage:
    windows: Dict[str, UsageWindow]  # in the CLI's order
    at: datetime  # aware UTC: when the event was seen
    source: str


def _stamp(at: datetime) -> str:
    return at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _from_stamp(value: Any) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    try:
        at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (at if at.tzinfo else at.replace(tzinfo=UTC)).astimezone(UTC)


def _epoch_to_utc(value: Any) -> Optional[datetime]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(value, UTC)
    except (OverflowError, OSError, ValueError):
        return None


def usage_from_info(info: Any) -> Optional[Dict[str, Any]]:
    """The stored windows of a raw ``rate_limit_info``; None when it carries
    no non-empty ``unifiedWindows`` (a third-party route, an older CLI)."""
    windows = info.get("unifiedWindows") if isinstance(info, dict) else None
    if not isinstance(windows, dict):
        return None
    out: Dict[str, Any] = {}
    for key, win in windows.items():
        if not isinstance(win, dict):
            continue
        util = win.get("utilization")
        resets = _epoch_to_utc(win.get("resetsAt"))
        out[str(key)] = {
            "utilization": (
                float(util)
                if isinstance(util, (int, float)) and not isinstance(util, bool)
                else None
            ),
            "resets_at": _stamp(resets) if resets else None,
            "status": str(win.get("status") or ""),
        }
    return out or None


def record_usage(
    info: Any, source: str, observed_at: Optional[datetime] = None
) -> None:
    """Record the usage a raw ``rate_limit_info`` reports, seen at
    ``observed_at``. A snapshot older than the stored one is dropped (a late
    reaper must not roll the numbers back). Best effort, like :func:`record`."""
    windows = usage_from_info(info)
    if windows is None:
        return
    at = (observed_at or datetime.now(UTC)).astimezone(UTC)

    def change(body: Dict[str, Any]) -> None:
        current = body.get(USAGE_KEY)
        stored = _from_stamp(current.get("at")) if isinstance(current, dict) else None
        if stored is not None and stored > at.replace(microsecond=0):
            return
        body[USAGE_KEY] = {
            "at": _stamp(at),
            "source": source,
            "status": str(info.get("status") or ""),
            "windows": windows,
        }

    try:
        update_record(change)
    except OSError:
        logger.warning("claude usage not recorded", source=source, exc_info=True)


def record_usage_soon(
    info: Any, source: str, observed_at: Optional[datetime] = None
) -> None:
    """Bot process: :func:`record_usage` on a worker thread, not awaited, so
    a contended lock stalls neither the stream reader nor the event loop.
    Nothing to record (no windows): no thread at all."""
    if usage_from_info(info) is None:
        return
    import asyncio

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        record_usage(info, source, observed_at)
        return
    loop.run_in_executor(None, record_usage, info, source, observed_at)


def read_usage() -> Optional[ClaudeUsage]:
    """The recorded usage snapshot, or None (none yet, or unreadable)."""
    body = read_record().get(USAGE_KEY)
    if not isinstance(body, dict) or not isinstance(body.get("windows"), dict):
        return None
    at = _from_stamp(body.get("at"))
    if at is None:
        return None
    windows: Dict[str, UsageWindow] = {}
    for key, win in body["windows"].items():
        if not isinstance(win, dict):
            continue
        util = win.get("utilization")
        windows[str(key)] = UsageWindow(
            utilization=float(util) if isinstance(util, (int, float)) else None,
            resets_at=_from_stamp(win.get("resets_at")),
            status=str(win.get("status") or ""),
        )
    if not windows:
        return None
    return ClaudeUsage(windows=windows, at=at, source=str(body.get("source") or ""))


def usage_line(info: Any, observed_at: Optional[datetime] = None) -> Optional[str]:
    """The stderr line agentjob prints for ``bgjob summarize`` (its box cannot
    write the record); None when the info has no windows."""
    if usage_from_info(info) is None:
        return None
    at = (observed_at or datetime.now(UTC)).astimezone(UTC)
    body = {"at": _stamp(at), "info": info}
    return _USAGE_PREFIX + json.dumps(body, ensure_ascii=False, separators=(",", ":"))


def find_usage(text: str) -> Optional[Tuple[Dict[str, Any], datetime]]:
    """The last :func:`usage_line` in a job's stderr: (info, observed_at)."""
    for line in reversed((text or "").splitlines()):
        if not line.startswith(_USAGE_PREFIX):
            continue
        try:
            body = json.loads(line[len(_USAGE_PREFIX) :])
        except ValueError:
            continue
        at = _from_stamp(body.get("at")) if isinstance(body, dict) else None
        if at is not None and isinstance(body.get("info"), dict):
            return body["info"], at
    return None


def _clock(at: datetime, now: datetime) -> str:
    fmt = "%H:%M" if at.date() == now.date() else "%d.%m %H:%M"
    return at.astimezone(UTC).strftime(fmt)


def _age(delta: timedelta) -> str:
    minutes = max(0, int(delta.total_seconds() // 60))
    if minutes < 1:
        return "только что"
    if minutes < 60:
        return f"{minutes} мин назад"
    hours, minutes = divmod(minutes, 60)
    if hours < 48:
        return f"{hours} ч {minutes} мин назад" if minutes else f"{hours} ч назад"
    return f"{hours // 24} дн. назад"


def format_claude_usage(
    usage: Optional[ClaudeUsage],
    limit: Optional[Limit],
    now: Optional[datetime] = None,
) -> str:
    """The Claude block of /limits (plain text): % and reset per window, when
    the numbers were seen, and an active limit hit. Pure."""
    now = (now or datetime.now(UTC)).astimezone(UTC)
    lines: List[str] = []
    if usage is None:
        lines.append(
            "Claude: данных об использовании ещё нет "
            "(появятся после первого хода Claude)"
        )
    else:
        at = usage.at.astimezone(UTC)
        lines.append(f"📊 Claude (на {_clock(at, now)} UTC, {_age(now - at)})")
        for key, win in usage.windows.items():
            label = _WINDOW_LABELS.get(key, key)
            if win.resets_at is not None and win.resets_at <= now:
                lines.append(
                    f"{label}: окно сброшено в {_clock(win.resets_at, now)} UTC, "
                    "свежих данных нет"
                )
                continue
            pct = (
                f"использовано {round(win.utilization * 100)}%"
                if win.utilization is not None
                else "использовано ?"
            )
            reset = (
                f" · сброс {_clock(win.resets_at, now)} UTC"
                if win.resets_at is not None
                else ""
            )
            lines.append(f"{label}: {pct}{reset}")
        if now - at > USAGE_STALE:
            lines.append("(данные устарели: обновятся со следующим ходом Claude)")
    if limit is not None:
        until = _clock(limit.until, now)
        lines.append(
            f"⛔ Claude: лимит кончился, до {until} UTC\n{limit.message}".strip()
        )
    return "\n".join(lines)


def claude_limits_text(now: Optional[datetime] = None) -> str:
    """The Claude block of /limits from the shared record. No bot context and
    no model call: the panel is the outage view (dv-6104 reuses it)."""
    return format_claude_usage(read_usage(), active(now), now)


__all__ = [
    "Limit",
    "QUOTA_EXIT",
    "active",
    "find_limit",
    "parse_limit",
    "parse_quota_line",
    "quota_line",
    "record",
    "JOB_ROUTE_KEY",
    "JOB_SWITCH_HARNESSES",
    "job_route_info",
    "job_route_override",
    "read_record",
    "set_job_route",
    "update_record",
    "ClaudeUsage",
    "USAGE_KEY",
    "UsageWindow",
    "claude_limits_text",
    "find_usage",
    "format_claude_usage",
    "read_usage",
    "record_usage",
    "record_usage_soon",
    "usage_from_info",
    "usage_line",
]
