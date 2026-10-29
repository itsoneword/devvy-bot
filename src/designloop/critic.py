"""The design critic: Codex judges variant renders through the model router
(dv-9133).

The generator is Claude, so the critic is another family: purpose
``design_critic`` is pinned to Codex with no fallback (routes.py). It sees
only the brief, the renders and the current screen, as images; no code and
no conversation, so it cannot be talked into ``ready``.

The answer is fixed JSON: six criteria, each ``ok`` or ``fix: <what>``, a
1-10 score and a verdict ``ready`` | ``revise``. A criterion that is neither
is refused (:class:`ModelCallError`): a vague "great!" must never reach the
owner as a verdict.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence, Tuple, Union

from src.model_router import CallRequest, ModelCallError, get_router

from .brief import Brief

PURPOSE = "design_critic"
TIMEOUT_SECONDS = 600.0

# The ticket's six criteria (26.10) with the owner's 26.10/27.10 additions
# folded in: grouping into hierarchy, laconism into density, structural
# parity of sibling screens into consistency.
CRITERIA: Mapping[str, str] = {
    "hierarchy": (
        "one clear primary action; related elements grouped into a few "
        "blocks; the eye goes where the user's goal is"
    ),
    "spacing_rhythm": "a consistent spacing scale (the §1 tokens), no cramped "
    "or stray gaps",
    "alignment": "shared edges and columns; nothing off by a few pixels",
    "density": (
        "laconic: rare features stay quiet (text, a row, behind a tap), no "
        "element repeats another's job, nothing stuffed in a row"
    ),
    "consistency": (
        "same structure and components as the app's other screens; sibling "
        "screens (own / other profile) share one structure"
    ),
    "reference_match": (
        "as good as the reference (the current screen, and the named "
        "reference apps when given) at what they do well"
    ),
}

VERDICTS = ("ready", "revise")

CRITIC_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "required": ["criteria", "score", "verdict"],
    "additionalProperties": False,
    "properties": {
        "criteria": {
            "type": "object",
            "required": list(CRITERIA),
            "additionalProperties": False,
            "properties": {name: {"type": "string"} for name in CRITERIA},
        },
        # Owner 27.10: a scored critic, so its verdicts can be compared with
        # the owner's picks over time.
        "score": {"type": "integer", "minimum": 1, "maximum": 10},
        "verdict": {"enum": list(VERDICTS)},
    },
}

_VALUE = re.compile(r"^(ok|fix:\s*\S.*)$", re.IGNORECASE | re.DOTALL)

SYSTEM_PROMPT = """\
You are a senior product designer reviewing mobile screens of a Telegram
Mini App. You judge only what the images show against the brief. You are
strict: "ready" means you would ship it as is.

Answer the JSON schema. For each criterion write exactly "ok" or
"fix: <what>". A fix names the element and the change ("fix: the bell chip
and the bell button say the same thing; drop the chip"), never a general
wish ("improve spacing"). Verdict "ready" only when every criterion is ok.
Score 1-10: how close the screen is to shippable."""

POINT_BY_POINT = """\
Compare point by point: for each pattern of each reference app, say whether
the screen has it, where, and what differs. Put each difference in the
criterion it belongs to."""


@dataclass(frozen=True)
class Critique:
    """The critic's verdict on one round of renders."""

    verdict: str
    criteria: Dict[str, str]
    score: int
    raw: str = field(repr=False, default="")

    @property
    def fixes(self) -> Dict[str, str]:
        """criterion -> what to fix, for every criterion that is not ok."""
        return {k: v for k, v in self.criteria.items() if not _is_ok(v)}


async def critique(
    brief: Brief,
    renders: Sequence[Union[str, Path]],
    references: Sequence[str] = (),
    *,
    reference_images: Sequence[Union[str, Path]] = (),
    point_by_point: bool = False,
) -> Critique:
    """One ``design_critic`` call: the renders, next to the current screen
    (``brief.refs``), judged against the brief.

    ``references`` are reference apps in words ("Strava feed: ...");
    ``reference_images`` screenshots of them, when the board has any.
    """
    images, legend = _images(renders, brief.refs, reference_images)
    prompt = _prompt(brief, legend, references, point_by_point)
    result = await get_router().complete(
        CallRequest(
            purpose=PURPOSE,
            prompt=prompt,
            system_prompt=SYSTEM_PROMPT,
            json_schema=CRITIC_SCHEMA,
            images=images,
            timeout_seconds=TIMEOUT_SECONDS,
        )
    )
    return parse(result.text)


def parse(text: str) -> Critique:
    """The router's canonical JSON as a :class:`Critique`; a criterion that
    is not ``ok`` / ``fix: <what>`` raises :class:`ModelCallError`."""
    data = json.loads(text)
    criteria = {k: str(v).strip() for k, v in data["criteria"].items()}
    bad = {k: v for k, v in criteria.items() if not _VALUE.match(v)}
    if bad:
        raise ModelCallError(
            "design critic: criteria must be 'ok' or 'fix: <what>', got "
            + "; ".join(f"{k}={v!r}" for k, v in sorted(bad.items()))
        )
    return Critique(
        verdict=str(data["verdict"]),
        criteria=criteria,
        score=int(data.get("score") or 0),
        raw=text,
    )


def repeated(previous: Critique, current: Critique) -> bool:
    """The stop rule's "issues repeat": the same criteria ask for the same
    fixes as last round (case and spacing ignored)."""
    return bool(current.fixes) and _norm(previous.fixes) == _norm(current.fixes)


def _is_ok(value: str) -> bool:
    return value.strip().lower() == "ok"


def _norm(fixes: Mapping[str, str]) -> Dict[str, str]:
    return {k: " ".join(v.lower().split()) for k, v in fixes.items()}


def _images(
    renders: Sequence[Union[str, Path]],
    refs: Sequence[Path],
    reference_images: Sequence[Union[str, Path]],
) -> Tuple[Tuple[str, ...], List[str]]:
    """The images in attach order and one legend line each. The current
    screen always rides along; a path already attached is not sent twice."""
    images: List[str] = []
    legend: List[str] = []

    def add(path: Union[str, Path], role: str) -> None:
        resolved = str(Path(path).resolve())
        if resolved in images:
            return
        images.append(resolved)
        name = Path(path)
        legend.append(f"image {len(images)}: {role}: {name.parent.name}/{name.name}")

    for path in renders:
        add(path, "under review")
    for path in refs:
        add(path, "current screen (before)")
    for path in reference_images:
        add(path, "reference app")
    return tuple(images), legend


def _prompt(
    brief: Brief,
    legend: Sequence[str],
    references: Sequence[str],
    point_by_point: bool,
) -> str:
    criteria = "\n".join(f"- {name}: {what}" for name, what in CRITERIA.items())
    parts = [
        brief.text,
        "# Images (attached, in this order)\n" + "\n".join(legend),
    ]
    if references:
        parts.append("# Reference apps\n" + "\n".join(f"- {ref}" for ref in references))
    parts.append("# Criteria\n" + criteria)
    if point_by_point and references:
        parts.append(POINT_BY_POINT)
    return "\n\n".join(parts) + "\n"
