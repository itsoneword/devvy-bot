"""The design critic: Claude opus judges variant renders through the model
router.

Purpose ``design_critic`` is pinned to Claude opus with no fallback
(routes.py; it replaced an earlier Codex pin, the owner's call). It sees
only the brief, the renders, the current screen and the owner's reference
images, as images; no code and no conversation, so it cannot be talked into
``ready``.

The answer is fixed JSON: six criteria, each ``ok`` or ``fix: <what>``, a
1-10 score and a verdict ``ready`` | ``revise``. A criterion that is neither
is refused (:class:`ModelCallError`): a vague "great!" must never reach the
owner as a verdict.
"""

from __future__ import annotations

import dataclasses
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence, Tuple, Union

from src.model_router import CallRequest, ModelCallError, get_router

from .brief import Brief

PURPOSE = "design_critic"
TIMEOUT_SECONDS = 600.0

# Six criteria, with the owner's later additions folded in: grouping into hierarchy, laconism into density, structural
# parity of sibling screens into consistency.
CRITERIA: Mapping[str, str] = {
    "hierarchy": (
        "one clear primary action; related elements grouped into a few "
        "blocks; the eye goes where the user's goal is"
    ),
    "spacing_rhythm": "a consistent spacing scale (the brief's tokens), no cramped "
    "or stray gaps",
    "alignment": "shared edges and columns; nothing off by a few pixels",
    "density": (
        "laconic: rare features stay quiet (text, a row, behind a tap), no "
        "element repeats another's job, nothing stuffed in a row"
    ),
    "consistency": (
        "same structure and components as the app's other screens; sibling "
        "screens (e.g. two views of the same object) share one structure"
    ),
    "reference_match": (
        "as good as the reference (the current screen, and the named "
        "reference apps when given) at what they do well"
    ),
}

# With the board's owner references / verdicts in the brief, the
# current screen is only the "before", not the bar.
OWNER_REFERENCE_MATCH = (
    "judged against the owner's references and his verdicts in the brief: "
    "the traits he likes are present, the traits he rejected are absent; the "
    "current screen is only the before"
)
OWNER_REFERENCE = "owner reference"
# With the board's UI kit: the owner's approved components come first.
UI_KIT = "UI kit"
KIT_REFERENCE_MATCH = (
    "judged first against the UI kit images: the render's components "
    "(buttons, chips, tabs, cards, badges) look like the kit's; then against "
    "the owner's references and his verdicts in the brief: the traits he likes "
    "are present, the traits he rejected are absent; the current screen is "
    "only the before"
)

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
        # A scored critic, so its verdicts can be compared with
        # the owner's picks over time.
        "score": {"type": "integer", "minimum": 1, "maximum": 10},
        "verdict": {"enum": list(VERDICTS)},
    },
}

_VALUE = re.compile(r"^(ok|fix:\s*\S.*)$", re.IGNORECASE | re.DOTALL)

SYSTEM_PROMPT = """\
You are a senior product designer reviewing mobile screens of an
app. You judge only what the images show against the brief. You are
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


# Mockup mode: the render is scored against the chosen mockup and
# the board's style spec on eight named dimensions, 1-10 each with a one-line
# evidence note naming the element. They replace ``reference_match``; the
# other five criteria stay ok/fix. Overall = the lowest dimension; the
# verdict is computed here, never the model's.
DIMENSIONS: Mapping[str, str] = {
    "palette": "colours and their roles match the spec's palette (hex roles)",
    "typography": "font family, weights and sizes per role match the spec",
    "shape_radius": "corner radii and border widths/colours match the spec",
    "depth": "shadows (offset, blur, opacity) and glass (blur, tint, "
    "highlight) match the spec",
    "spacing_density": "paddings and gaps sit on the spec's spacing scale; "
    "density like the mockup",
    "component_fidelity": "each component (button, chip, card, tab bar, ...) "
    "matches the spec's component description and state",
    "layout_fidelity": "structure, order and proportions follow the target " "mockup",
    "text_correctness": "every UI text is correct and legible per the brief "
    "(garbled text in the mockup is not the render's defect)",
}
MOCKUP_CRITERIA: Mapping[str, str] = {
    k: v for k, v in CRITERIA.items() if k != "reference_match"
}
THRESHOLD = 7
TARGET_MOCKUP = "target mockup"

MOCKUP_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "required": ["criteria", "dimensions"],
    "additionalProperties": False,
    "properties": {
        "criteria": {
            "type": "object",
            "required": list(MOCKUP_CRITERIA),
            "additionalProperties": False,
            "properties": {name: {"type": "string"} for name in MOCKUP_CRITERIA},
        },
        "dimensions": {
            "type": "object",
            "required": list(DIMENSIONS),
            "additionalProperties": False,
            "properties": {
                name: {
                    "type": "object",
                    "required": ["score", "evidence"],
                    "additionalProperties": False,
                    "properties": {
                        "score": {"type": "integer", "minimum": 1, "maximum": 10},
                        "evidence": {"type": "string"},
                    },
                }
                for name in DIMENSIONS
            },
        },
    },
}

MOCKUP_SYSTEM_PROMPT = """\
You are a senior product designer checking that a mobile screen built in code
matches its target mockup and the board's style spec. You judge only what the
images show. You are strict.

Answer the JSON schema. For each criterion write exactly "ok" or
"fix: <what>", naming the element and the change. For each dimension give an
integer score 1-10 (10 = indistinguishable from the spec and the mockup) and
one line of evidence that names the element you looked at and what differs
or matches, in web-design terms ("primary button fill #5F8F6A vs spec
#7A9E7E", "card radius ~8px, spec 20px"). Never a general wish. Seed data
(titles, names, photos, dates and their format) that differs from the mockup
is not a build defect and does not lower a score: initials in place of a
photo and a map without tiles are normal on test data. Blank space below the
content is not a layout gap."""

# Block remarks (dv-696e): every difference per block, so the builder gets
# all of them in one round, not one fix per criterion. ``kind`` = build (the
# code must change) | data (seed content: title, photos, date format).
REMARK_KINDS = ("build", "data")
REMARK_FIELDS = ("block", "element", "mockup", "render", "change", "kind")
REMARKS_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "required": ["remarks"],
    "additionalProperties": False,
    "properties": {
        "remarks": {
            "type": "array",
            "items": {
                "type": "object",
                "required": list(REMARK_FIELDS),
                "additionalProperties": False,
                "properties": {
                    **{k: {"type": "string"} for k in REMARK_FIELDS[:-1]},
                    "kind": {"enum": list(REMARK_KINDS)},
                },
            },
        }
    },
}

REMARKS_SYSTEM_PROMPT = """\
You are a senior product designer comparing a mobile screen built in code
with its target mockup, block by block. You judge only what the images show.

List every difference you see in the block under review, one remark each:
block = the block id given (or a short name of the block), element = the
element inside it, mockup = what the mockup shows, render = what the render
shows, change = the exact CSS property and value or the UI kit component to
use ("gap 12px -> 8px", "use ProfileCard"). kind = "data" when the
difference is seed content (title, names, photos, date values or format;
initials in place of a photo, a map without tiles) the code does not decide,
else "build". Leave out what matches. Do not stop at
the biggest differences: all of them."""


@dataclass(frozen=True)
class Remark:
    """One difference in one block: ``kind`` = build | data."""

    block: str
    element: str
    mockup: str
    render: str
    change: str
    kind: str = "build"

    @property
    def line(self) -> str:
        return (
            f"{self.block} / {self.element}: mockup = {self.mockup}; "
            f"render = {self.render}; change = {self.change}"
        )


@dataclass(frozen=True)
class Remarks:
    """One remarks call: what it was shown and what it answered."""

    remarks: Tuple[Remark, ...]
    raw: str
    images: Tuple[str, ...]


@dataclass(frozen=True)
class Critique:
    """The critic's verdict on one round of renders.

    ``dimensions`` (mockup mode only): name -> (score, evidence);
    ``threshold``: the score a dimension needs to count as met."""

    verdict: str
    criteria: Dict[str, str]
    score: int
    raw: str = field(repr=False, default="")
    dimensions: Mapping[str, Tuple[int, str]] = field(default_factory=dict)
    threshold: int = 0
    # Block remarks (mockup mode): they go to the builder, never into the
    # verdict or the score.
    remarks: Tuple[Remark, ...] = ()
    # the images the critic call was shown, in order (for critic.json).
    images: Tuple[str, ...] = field(default=(), compare=False, repr=False)

    @property
    def build_remarks(self) -> Tuple[Remark, ...]:
        return tuple(r for r in self.remarks if r.kind == "build")

    @property
    def fixes(self) -> Dict[str, str]:
        """criterion -> what to fix, for every criterion that is not ok."""
        return {k: v for k, v in self.criteria.items() if not _is_ok(v)}

    @property
    def low_dimensions(self) -> Dict[str, Tuple[int, str]]:
        """The dimensions below the threshold, in :data:`DIMENSIONS` order."""
        return {k: v for k, v in self.dimensions.items() if v[0] < self.threshold}


async def critique(
    brief: Brief,
    renders: Sequence[Union[str, Path]],
    references: Sequence[str] = (),
    *,
    reference_images: Sequence[Union[str, Path]] = (),
    kit_images: Sequence[Union[str, Path]] = (),
    point_by_point: bool = False,
) -> Critique:
    """One ``design_critic`` call: the renders, next to the current screen
    (``brief.refs``), judged against the brief.

    ``references`` are reference apps in words ("Strava feed: ...");
    ``reference_images`` screenshots of them, when the board has any;
    ``kit_images`` the board's UI kit, attached before them.
    """
    owner_led = brief.owner_led or bool(kit_images)
    role = OWNER_REFERENCE if owner_led else "reference app"
    images, legend = _images(renders, brief.refs, reference_images, role, kit_images)
    prompt = _prompt(brief, legend, references, point_by_point, bool(kit_images))
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
    return dataclasses.replace(parse(result.text), images=images)


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
    fixes as last round (case and spacing ignored); in mockup mode the same
    dimensions are low with the same scores and evidence."""
    if not (current.fixes or current.low_dimensions):
        return False
    return _norm(previous.fixes) == _norm(current.fixes) and _low(previous) == _low(
        current
    )


def _low(critique: Critique) -> Dict[str, str]:
    return _norm({k: f"{n} {ev}" for k, (n, ev) in critique.low_dimensions.items()})


def parse_mockup(text: str, threshold: int = THRESHOLD) -> Critique:
    """A mockup-mode answer as a :class:`Critique`: every dimension present,
    an integer 1-10 and an evidence line of at least two words; overall score
    = the lowest dimension; ``ready`` only when it is >= ``threshold`` and
    every criterion is ok. Anything else raises :class:`ModelCallError`."""
    try:
        data = json.loads(text)
        raw_criteria = data["criteria"]
        raw_dims = data["dimensions"]
    except (ValueError, KeyError, TypeError) as exc:
        raise ModelCallError(f"design critic: not the mockup schema: {exc}") from exc
    if not isinstance(raw_criteria, dict) or not isinstance(raw_dims, dict):
        raise ModelCallError("design critic: criteria / dimensions must be objects")
    criteria = {k: str(v).strip() for k, v in raw_criteria.items()}
    bad = {k: v for k, v in criteria.items() if not _VALUE.match(v)}
    if bad:
        raise ModelCallError(
            "design critic: criteria must be 'ok' or 'fix: <what>', got "
            + "; ".join(f"{k}={v!r}" for k, v in sorted(bad.items()))
        )
    missing = [d for d in DIMENSIONS if d not in raw_dims]
    if missing:
        raise ModelCallError("design critic: dimensions missing: " + ", ".join(missing))
    dims: Dict[str, Tuple[int, str]] = {}
    for name in DIMENSIONS:
        item = raw_dims[name]
        score = item.get("score") if isinstance(item, dict) else None
        evidence = " ".join(str((item or {}).get("evidence") or "").split())
        if (
            isinstance(score, bool)
            or not isinstance(score, int)
            or not 1 <= score <= 10
        ):
            raise ModelCallError(
                f"design critic: {name} score must be an integer 1-10, got {score!r}"
            )
        if len(evidence.split()) < 2:
            raise ModelCallError(
                f"design critic: {name} evidence must name an element, got "
                f"{evidence!r}"
            )
        dims[name] = (score, evidence)
    lowest = min(n for n, _ in dims.values())
    ok = lowest >= threshold and all(_is_ok(v) for v in criteria.values())
    return Critique(
        verdict="ready" if ok else "revise",
        criteria=criteria,
        score=lowest,
        raw=text,
        dimensions=dims,
        threshold=threshold,
    )


async def critique_mockup(
    brief: Brief,
    renders: Sequence[Union[str, Path]],
    *,
    mockup: Union[str, Path],
    spec_text: str,
    threshold: int = THRESHOLD,
    kit_images: Sequence[Union[str, Path]] = (),
) -> Critique:
    """One ``design_critic`` call in mockup mode: the renders against the
    chosen mockup (attached as "target mockup") and the style spec text,
    scored on :data:`DIMENSIONS`."""
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
    add(mockup, TARGET_MOCKUP)
    for path in brief.refs:
        add(path, "current screen (before)")
    for path in kit_images:
        add(path, UI_KIT)
    criteria = "\n".join(f"- {k}: {v}" for k, v in MOCKUP_CRITERIA.items())
    dimensions = "\n".join(f"- {k}: {v}" for k, v in DIMENSIONS.items())
    # The brief carries the approved spec as its token section; then once.
    spec_part = (
        ""
        if getattr(brief, "spec", "").strip()
        else "# Style spec (the board's approved spec, web-design terms)\n"
        + spec_text.strip()
    )
    prompt = "\n\n".join(
        part
        for part in [
            brief.text,
            "# Images (attached, in this order)\n" + "\n".join(legend),
            spec_part,
            "# Criteria\n" + criteria,
            "# Dimensions (score each 1-10 against the target mockup and the "
            "style spec; evidence names the element)\n" + dimensions,
        ]
        if part
    )
    result = await get_router().complete(
        CallRequest(
            purpose=PURPOSE,
            prompt=prompt + "\n",
            system_prompt=MOCKUP_SYSTEM_PROMPT,
            json_schema=MOCKUP_SCHEMA,
            images=tuple(images),
            timeout_seconds=TIMEOUT_SECONDS,
        )
    )
    return dataclasses.replace(
        parse_mockup(result.text, threshold=threshold), images=tuple(images)
    )


def parse_remarks(text: str, block: str = "") -> Tuple[Remark, ...]:
    """A remarks answer as :class:`Remark` s; ``block`` (a DOM block id) fills
    a remark that names none. A wrong kind or an empty field raises
    :class:`ModelCallError`."""
    try:
        items = json.loads(text)["remarks"]
    except (ValueError, KeyError, TypeError) as exc:
        raise ModelCallError(f"design critic: not the remarks schema: {exc}") from exc
    if not isinstance(items, list):
        raise ModelCallError("design critic: remarks must be a list")
    out: List[Remark] = []
    for item in items:
        if not isinstance(item, dict):
            raise ModelCallError(f"design critic: remark is not an object: {item!r}")
        values = {k: " ".join(str(item.get(k) or "").split()) for k in REMARK_FIELDS}
        values["block"] = values["block"] or block
        values["kind"] = values["kind"].lower()
        if values["kind"] not in REMARK_KINDS:
            raise ModelCallError(
                f"design critic: remark kind must be build|data, got {item!r}"
            )
        empty = [k for k in ("block", "element", "change") if not values[k]]
        if empty:
            raise ModelCallError(
                f"design critic: remark without {', '.join(empty)}: {item!r}"
            )
        out.append(Remark(**values))
    return tuple(out)


async def critique_remarks(
    brief: Brief,
    renders: Sequence[Union[str, Path]],
    *,
    mockup: Union[str, Path],
    spec_text: str,
    block: str = "",
    kit_images: Sequence[Union[str, Path]] = (),
) -> Remarks:
    """One ``design_critic`` call for block remarks: ``renders`` are one
    block's crop (``block`` = its DOM id) or, without a sidecar, the whole
    renders (the model names the blocks); the mockup rides along whole."""
    images: List[str] = []
    legend: List[str] = []
    for path, role in (
        *((p, f"render of block {block}" if block else "render") for p in renders),
        (mockup, f"{TARGET_MOCKUP} (whole screen; find the block in it)"),
        *((p, UI_KIT) for p in kit_images),
    ):
        resolved = str(Path(path).resolve())
        if resolved not in images:
            images.append(resolved)
            legend.append(f"image {len(images)}: {role}: {Path(path).name}")
    scope = (
        f"# Block under review: {block}\nCompare only this block of the render "
        "with the same block of the mockup."
        if block
        else "# Blocks\nGo through the screen top to bottom, block by block; "
        "name each block."
    )
    prompt = "\n\n".join(
        [
            brief.text,
            "# Images (attached, in this order)\n" + "\n".join(legend),
            "# Style spec (the board's approved spec, web-design terms)\n"
            + spec_text.strip(),
            scope,
        ]
    )
    result = await get_router().complete(
        CallRequest(
            purpose=PURPOSE,
            prompt=prompt + "\n",
            system_prompt=REMARKS_SYSTEM_PROMPT,
            json_schema=REMARKS_SCHEMA,
            images=tuple(images),
            timeout_seconds=TIMEOUT_SECONDS,
        )
    )
    return Remarks(parse_remarks(result.text, block), result.text, tuple(images))


def _is_ok(value: str) -> bool:
    return value.strip().lower() == "ok"


def _norm(fixes: Mapping[str, str]) -> Dict[str, str]:
    return {k: " ".join(v.lower().split()) for k, v in fixes.items()}


def _images(
    renders: Sequence[Union[str, Path]],
    refs: Sequence[Path],
    reference_images: Sequence[Union[str, Path]],
    reference_role: str = "reference app",
    kit_images: Sequence[Union[str, Path]] = (),
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
    for path in kit_images:
        add(path, UI_KIT)
    for path in reference_images:
        add(path, reference_role)
    return tuple(images), legend


def _prompt(
    brief: Brief,
    legend: Sequence[str],
    references: Sequence[str],
    point_by_point: bool,
    kit: bool = False,
) -> str:
    def what_of(name: str, what: str) -> str:
        if name != "reference_match":
            return what
        if kit:
            return KIT_REFERENCE_MATCH
        return OWNER_REFERENCE_MATCH if brief.owner_led else what

    criteria = "\n".join(
        f"- {name}: {what_of(name, what)}" for name, what in CRITERIA.items()
    )
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
