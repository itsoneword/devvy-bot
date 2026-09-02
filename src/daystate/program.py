"""Parse the weekly split out of the user's program doc.

Convention (owner decision 3, 2026-08-15): the first markdown table under
``## Недельный сплит`` is machine-readable. One row per day label, and a
label may list several days ("Вт, Чт, Сб" = rest). Anything unparseable
yields no mapping — the digest then omits the line rather than guessing,
because a wrong planned session is worse than a missing one.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional

HEADING = re.compile(r"^##\s+Недельный\s+сплит\s*$", re.IGNORECASE | re.MULTILINE)

# Monday-first, matching date.weekday().
WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]

_SEPARATOR = re.compile(r"^\|[\s:|-]+\|$")


def _split_row(line: str) -> List[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def parse_weekly_split(content: Optional[str]) -> Dict[str, str]:
    """Map weekday abbreviation -> planned session.

    Returns {} for missing content, a missing heading, or a malformed table.
    """
    if not content:
        return {}
    match = HEADING.search(content)
    if not match:
        return {}

    mapping: Dict[str, str] = {}
    started = False
    for raw in content[match.end() :].splitlines():
        line = raw.strip()
        if not line.startswith("|"):
            # Blank lines before the table are fine; any other prose after it
            # ends the table.
            if started or (line and not line.startswith("#")):
                if started:
                    break
            continue
        started = True
        if _SEPARATOR.match(line):
            continue
        cells = _split_row(line)
        if len(cells) < 2:
            continue
        day_cell, session = cells[0], cells[1]
        if not session or day_cell.lower().startswith("день"):
            continue
        for day in re.split(r"[,/]", day_cell):
            key = day.strip()[:2]
            if key in WEEKDAYS:
                mapping[key] = session
    return mapping


def planned_for(mapping: Dict[str, str], weekday_index: int) -> Optional[str]:
    """Planned session for a ``date.weekday()`` index, if the split has one."""
    if not mapping:
        return None
    return mapping.get(WEEKDAYS[weekday_index % 7])
