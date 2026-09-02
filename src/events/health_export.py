"""Parser for the 'health' webhook provider (Apple Health via the
Health Auto Export iOS app, https://github.com/Lybron/health-auto-export).

The app POSTs a JSON envelope shaped like::

    {"data": {"metrics": [
        {"name": "Sleep Analysis", "units": "min", "data": [
            {"date": "2026-07-28", "totalSleep": 450, "asleep": 430,
             "core": 200, "deep": 100, "rem": 130,
             "sleepStart": "2026-07-27 22:30:00 +0100",
             "sleepEnd": "2026-07-28 06:30:00 +0100"}
        ]},
        {"name": "Heart Rate Variability", "units": "ms", "data": [
            {"date": "2026-07-28 08:15:00 +0100", "qty": 45}
        ]},
        {"name": "Blood Pressure", "units": "mmHg", "data": [
            {"date": "2026-07-28 08:00:00 +0100", "systolic": 118, "diastolic": 76}
        ]},
    ]}}

Only a handful of metrics matter for the recovery log; everything else in
the payload (workouts, ECG, mood, cycle tracking, ...) is ignored rather
than dumped into the agent prompt. We deliberately hand the AGENT a short
factual statement of the numbers -- not a pre-formatted markdown row --
and let it fold them into recovery_log.md using its own (date-anchored)
logging rules, the same way a manually-reported day is handled.
"""

from typing import Any, Dict, List, Optional

import structlog

logger = structlog.get_logger()

# Metric "name" values this parser understands, normalized (lowercase,
# spaces collapsed) -> short label used in the generated summary line.
_KNOWN_METRICS = {
    "sleep analysis": "Sleep",
    "heart rate variability": "HRV",
    "resting heart rate": "Resting HR",
    "blood pressure": "Blood pressure",
}


def _normalize(name: str) -> str:
    return " ".join(name.strip().lower().split())


def _latest(entries: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Health Auto Export sends entries oldest-first; the last one is the
    most recent sample/day for that metric in this payload."""
    return entries[-1] if entries else None


def _format_sleep(entry: Dict[str, Any]) -> Optional[str]:
    total = entry.get("asleep") or entry.get("totalSleep")
    if total is None:
        return None
    hours = round(float(total) / 60, 1)
    start = entry.get("sleepStart", "")
    end = entry.get("sleepEnd", "")
    window = f" ({start} → {end})" if start and end else ""
    parts = [f"{hours}h asleep{window}"]
    for key, label in (("core", "core"), ("deep", "deep"), ("rem", "REM")):
        val = entry.get(key)
        if val is not None:
            parts.append(f"{round(float(val) / 60, 1)}h {label}")
    return "Sleep: " + ", ".join(parts) + f" (date: {entry.get('date', '?')})"


def _format_hrv(entry: Dict[str, Any]) -> Optional[str]:
    qty = entry.get("qty")
    if qty is None:
        return None
    return f"HRV: {qty}ms (at {entry.get('date', '?')})"


def _format_resting_hr(entry: Dict[str, Any]) -> Optional[str]:
    qty = entry.get("qty")
    if qty is None:
        return None
    return f"Resting HR: {qty}bpm (at {entry.get('date', '?')})"


def _format_blood_pressure(entry: Dict[str, Any]) -> Optional[str]:
    systolic = entry.get("systolic")
    diastolic = entry.get("diastolic")
    if systolic is None or diastolic is None:
        return None
    return f"Blood pressure: {systolic}/{diastolic} mmHg (at {entry.get('date', '?')})"


_FORMATTERS = {
    "Sleep": _format_sleep,
    "HRV": _format_hrv,
    "Resting HR": _format_resting_hr,
    "Blood pressure": _format_blood_pressure,
}


def summarize_health_export(payload: Dict[str, Any]) -> Optional[str]:
    """Extract known metrics from a Health Auto Export payload.

    Returns a short multi-line factual summary (one line per recognized
    metric, most recent sample only), or None if nothing recognized was
    found -- callers should skip triggering the agent entirely in that
    case rather than spend a turn on an empty/irrelevant export.
    """
    metrics = payload.get("data", {}).get("metrics", [])
    if not isinstance(metrics, list):
        return None

    lines: List[str] = []
    for metric in metrics:
        if not isinstance(metric, dict):
            continue
        label = _KNOWN_METRICS.get(_normalize(str(metric.get("name", ""))))
        if not label:
            continue
        entries = metric.get("data")
        if not isinstance(entries, list):
            continue
        latest = _latest(entries)
        if not isinstance(latest, dict):
            continue
        formatter = _FORMATTERS[label]
        try:
            line = formatter(latest)
        except (TypeError, ValueError) as exc:
            logger.warning(
                "Failed to format health metric", metric=label, error=str(exc)
            )
            continue
        if line:
            lines.append(line)

    if not lines:
        return None
    return "\n".join(lines)


def build_health_export_prompt(summary: str) -> str:
    """Wrap the parsed summary into an explicit, low-ambiguity agent prompt.

    Explicitly instructs the agent to resolve dates from its own facts
    (each line already carries an explicit date/timestamp from the device,
    not "today"/"yesterday") and to update the existing day's row rather
    than create a duplicate -- this is an automated import, so it must not
    silently overwrite a day someone already filled in by hand.
    """
    return (
        "[Automated Apple Health import]\n"
        f"{summary}\n\n"
        "Fold these into recovery_log.md using the exact date on each line "
        "above (not 'today') -- fill in only the empty fields for that "
        "date's row if it already exists; don't overwrite anything the "
        "user already reported by hand, and don't create a duplicate row. "
        "If a date isn't in the log yet, add a new row with just these "
        "fields filled and the rest left blank/'?'."
    )
