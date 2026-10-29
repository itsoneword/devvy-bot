"""Calibrate the design critic on current screens (dv-9133, first step).

Before the loop trusts the critic, it critiques three current gymbro screens
(their reference PNGs, dark + light) against well-known apps; the owner has
no favourites yet, so industry references stand in. The report keeps the raw
critic output per screen, so a reader can judge whether the critique is
specific and actionable. If it is not, rerun with ``point_by_point``: the
critic then compares the screen with each reference pattern in turn.

Run after merge from the devops chat (owner answer 2.1: a build job cannot
write ``~/.codex``): ``scripts/designloop_calibrate``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from src.model_router import ModelCallError

from .brief import build_brief, state_refs
from .critic import CRITERIA, Critique, critique


@dataclass(frozen=True)
class Target:
    screen_id: str
    state: str
    references: Tuple[str, ...]


# Ticket 26.10: Strava (feed + map), Meetup / Partiful (event discovery and
# the event page), Airbnb (map + list). The feed has a map and a list view.
TARGETS: Tuple[Target, ...] = (
    Target(
        "feed",
        "main",
        (
            "Strava feed: activity cards with the author line on top, a map "
            "thumbnail, a stats row of three numbers, kudos/comment actions",
            "Airbnb map + list: one list of cards, a map toggle, filters as "
            "quiet chips above the list",
        ),
    ),
    Target(
        "event_page",
        "other",
        (
            "Meetup event page: title, date/time and place up top, host line, "
            "attendee avatars, one sticky Attend button",
            "Partiful event page: a bold header card, RSVP as the single "
            "primary action, guest list and comments below",
        ),
    ),
    Target(
        "profile_tab",
        "main",
        (
            "Strava athlete profile: avatar, name and a short stats row on "
            "top, recent activities below, settings out of the way",
            "Airbnb profile: identity card first, confirmed details as a quiet "
            "list, rare actions as text rows",
        ),
    ),
)

CHANGE_REQUEST = (
    "Calibration run, no variant: judge the current screen as it is, as if it "
    "were proposed for shipping, against the reference apps below."
)


@dataclass(frozen=True)
class Outcome:
    target: Target
    images: Tuple[Path, ...]
    critique: Optional[Critique] = None
    error: Optional[str] = None


async def calibrate(
    repo: Path,
    report: Path,
    *,
    targets: Sequence[Target] = TARGETS,
    point_by_point: bool = False,
) -> List[Outcome]:
    """Critique each target screen once and write ``report``.

    A failed call is written as such and the run goes on: the other screens'
    output is paid for and worth keeping."""
    outcomes: List[Outcome] = []
    for target in targets:
        brief = build_brief(repo, target.screen_id, CHANGE_REQUEST)
        renders = state_refs(repo, target.screen_id, target.state)
        if not renders:
            outcomes.append(
                Outcome(target, (), error=f"no refs for state {target.state!r}")
            )
            continue
        try:
            result = await critique(
                brief, renders, target.references, point_by_point=point_by_point
            )
        except ModelCallError as exc:
            outcomes.append(Outcome(target, renders, error=str(exc)))
            continue
        outcomes.append(Outcome(target, renders, critique=result))
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(render_report(outcomes, point_by_point), encoding="utf-8")
    return outcomes


def render_report(outcomes: Sequence[Outcome], point_by_point: bool) -> str:
    mode = "point-by-point" if point_by_point else "standard"
    lines = [
        "# dv-9133 — design critic calibration",
        "",
        f"Run {datetime.now(UTC):%Y-%m-%d %H:%M} UTC, prompt mode: {mode}, "
        "route: design_critic (Codex, no fallback).",
        f"Criteria: {', '.join(CRITERIA)}. Each is `ok` or `fix: <what>`.",
        "",
        "Specific and actionable? A fix should name the element and the change.",
        "If most fixes are general wishes, rerun with `--point-by-point`.",
        "",
    ]
    for outcome in outcomes:
        target = outcome.target
        lines += [f"## {target.screen_id} / {target.state}", ""]
        lines += [f"- image: {p.parent.name}/{p.name}" for p in outcome.images]
        lines += [f"- reference: {ref}" for ref in target.references]
        lines.append("")
        if outcome.critique is None:
            lines += [f"**Error:** {outcome.error}", ""]
            continue
        found = outcome.critique
        lines += [
            f"Verdict **{found.verdict}**, score {found.score}/10, "
            f"{len(found.fixes)} of {len(found.criteria)} criteria to fix.",
            "",
            "Raw critic output:",
            "",
            "```json",
            json.dumps(json.loads(found.raw), ensure_ascii=False, indent=2),
            "```",
            "",
        ]
    return "\n".join(lines)
