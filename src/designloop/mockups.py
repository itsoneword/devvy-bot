"""Design step 0: mockups of the screen or element before any code.

3-4 images from the brief and the board's approved style spec, through the
model router like every model call:

1. one ``design_mockup_prompt`` call turns the brief into 3-4 subject
   sentences, each with the exact UI texts quoted (the brief is prose;
   picking the elements and texts needs a model);
2. each subject + the spec's deterministic look block
   (:meth:`StyleSpec.look_block`) + a background line is one
   ``image_output`` call: purpose ``design_mockup`` (free FLUX; its route
   falls back to the cheap OpenRouter image model when FLUX is down) or,
   only on a card labelled ``paidmockup``, ``design_mockup_paid``;
3. the critic picks the best mockup (:func:`pick_mockup`, one
   ``design_critic`` call), or the owner on a card labelled ``ownermockup``.

The images are run artefacts under ``<out>/mockups/``, never committed.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from src.model_router import CallRequest, ModelCallError, get_router

from .stylespec import StyleSpec, StyleSpecError, load_spec

PURPOSE = "design_mockup"
PAID_PURPOSE = "design_mockup_paid"
PROMPT_PURPOSE = "design_mockup_prompt"
PICK_PURPOSE = "design_critic"
PAID_LABEL = "paidmockup"
OWNER_LABEL = "ownermockup"
COUNT_MIN, COUNT_MAX, COUNT_DEFAULT = 3, 4, 4
# One paid mockup (POST_IMAGE_MODEL, Nano Banana 2.1), as measured.
PAID_IMAGE_USD = 0.037
# FLUX preset best: ~150 s typical, long tails seen up to ~15 min.
IMAGE_TIMEOUT = 900.0
PROMPT_TIMEOUT = 180.0
PICK_TIMEOUT = 600.0
DEFAULT_SIZE = (768, 1664)  # a phone screen; an element card is wider
ELEMENT_SIZE = (1024, 768)
MAX_SIDE = 1664
BACKGROUND = (
    "BG: plain soft neutral background, the UI centred, nothing else in the "
    "picture, no hands, no device frame."
)

SUBJECTS_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "required": ["subjects"],
    "additionalProperties": False,
    "properties": {
        "subjects": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["subject", "size"],
                "additionalProperties": False,
                "properties": {
                    "subject": {"type": "string"},
                    "size": {"type": "string"},
                },
            },
        }
    },
}

PROMPT_SYSTEM = """\
You write prompts for an image model that draws mobile UI mockups. The style
part is added by code; you write only the subject: what is on the screen (or
the one element), top to bottom, with every UI text in double quotes exactly
as it must appear. Few, short texts: an image model garbles long text. Each
subject is a different, sensible layout of the same brief. Size is
"<width>x<height>" in CSS px of the frame (a phone screen 390x844; an
isolated element its own box)."""

PICK_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "required": ["best", "reasons"],
    "additionalProperties": False,
    "properties": {
        "best": {"type": "integer", "minimum": 1},
        "reasons": {"type": "array", "items": {"type": "string"}},
    },
}

PICK_SYSTEM = """\
You are a senior product designer. Pick the mockup that best serves the
brief in the board's style: clear primary action, correct texts, the spec's
palette and shapes. Answer "best" (1-based) and one line of reason per
mockup, in order, each starting with its score as "N/10: "."""


class MockupError(RuntimeError):
    """Step 0 produced nothing usable; the run goes on without a mockup."""


@dataclass(frozen=True)
class MockupStep:
    """A configured step 0: the approved spec and how to draw."""

    spec: StyleSpec
    spec_path: Path
    count: int = COUNT_DEFAULT
    preset: str = "best"
    paid_max_usd: float = 0.0
    key_ref: Optional[str] = None

    @property
    def paid(self) -> bool:
        """The board allows paying for this run's mockups (owner)."""
        return (
            self.paid_max_usd > 0 and self.count * PAID_IMAGE_USD <= self.paid_max_usd
        )


def mockup_step(*, config: Any, repo: Optional[Path] = None) -> Optional[MockupStep]:
    """Step 0 for a board, or None (= no mockup call at all): no
    ``design.mockups`` block, no ``design.style_spec``, or (with ``repo``)
    a spec that is missing, invalid or still a draft."""
    mockups = getattr(config, "mockups", None)
    spec_name = getattr(config, "style_spec", None)
    if mockups is None or not spec_name or repo is None:
        return None
    path = spec_path(repo, spec_name)
    try:
        spec = load_spec(path)
    except (OSError, StyleSpecError):
        return None
    if not spec.approved:
        return None
    return MockupStep(
        spec=spec,
        spec_path=path,
        count=_clamp(getattr(mockups, "count", COUNT_DEFAULT)),
        preset=str(getattr(mockups, "preset", "best") or "best"),
        paid_max_usd=float(getattr(mockups, "paid_max_usd", 0.0) or 0.0),
        key_ref=getattr(mockups, "key", None) or None,
    )


def spec_path(repo: Path, name: str) -> Path:
    path = Path(name)
    return path if path.is_absolute() else Path(repo) / path


def spec_problem(repo: Path, name: Optional[str]) -> str:
    """What is wrong with a configured spec, "" when it is approved and valid
    (``pump2 design run`` / ``doctor``)."""
    if not name:
        return ""
    path = spec_path(repo, name)
    try:
        spec = load_spec(path)
    except FileNotFoundError:
        return f"style_spec {name}: missing"
    except StyleSpecError as exc:
        return f"style_spec {name}: invalid: {exc}"
    except OSError as exc:
        return f"style_spec {name}: unreadable: {exc}"
    if not spec.approved:
        return f"style_spec {name}: still a draft (pump2 design spec approve)"
    return ""


def _clamp(count: Any) -> int:
    try:
        n = int(count)
    except (TypeError, ValueError):
        n = COUNT_DEFAULT
    return max(COUNT_MIN, min(COUNT_MAX, n))


def ref_legend(count: int, note: str = "") -> str:
    """What the attached reference images are (dv-4032): image 1 is the
    current screen, the rest style references."""
    change = note.strip() or "what the subject above asks for"
    lines = [
        "# Attached images",
        f"Image 1 = the current screen: keep its layout and content, change "
        f"only {change}.",
    ]
    if count > 1:
        lines.append(
            f"Images 2..{count} = style references only: take their look, "
            "not their content."
        )
    return "\n".join(lines)


def mockup_prompt(
    subject: str, spec: StyleSpec, element: str = "", refs: int = 0, note: str = ""
) -> str:
    """Subject + the spec's look block (+ the element's spec line) + BG
    (+ the legend of ``refs`` attached reference images)."""
    parts = [subject.strip()]
    line = spec.component_line(element) if element else ""
    if line:
        parts.append(f"Component spec: {line}.")
    parts += [spec.look_block(), BACKGROUND]
    if refs:
        parts.append(ref_legend(refs, note))
    return "\n\n".join(parts)


_SIZE = re.compile(r"(\d{2,4})\s*[x×]\s*(\d{2,4})")


def image_size(size: str, fallback: Tuple[int, int]) -> Tuple[int, int]:
    """CSS px "390x844" -> pixels for the image model: scaled up so the long
    side is :data:`MAX_SIDE`, multiples of 16."""
    m = _SIZE.search(size or "")
    if not m:
        return fallback
    w, h = int(m.group(1)), int(m.group(2))
    if w <= 0 or h <= 0:
        return fallback
    scale = MAX_SIDE / max(w, h)
    return (max(256, int(w * scale) // 16 * 16), max(256, int(h * scale) // 16 * 16))


async def mockup_subjects(
    brief_text: str, spec: StyleSpec, count: int, element: str = ""
) -> List[Tuple[str, str]]:
    """(subject, size) x ``count`` from one ``design_mockup_prompt`` call;
    fewer answered = repeated in order."""
    what = (
        f"one isolated UI element, the {element!r} component"
        if element
        else "the whole screen"
    )
    components = "\n".join(f"- {spec.component_line(name)}" for name in spec.components)
    prompt = (
        f"# Brief\n{brief_text.strip()}\n\n"
        f"# Components of the board's style\n{components}\n\n"
        f"Write {count} subjects for mockups of {what}."
    )
    result = await get_router().complete(
        CallRequest(
            purpose=PROMPT_PURPOSE,
            prompt=prompt,
            system_prompt=PROMPT_SYSTEM,
            json_schema=SUBJECTS_SCHEMA,
            timeout_seconds=PROMPT_TIMEOUT,
        )
    )
    try:
        items = json.loads(result.text)["subjects"]
        pairs = [
            (str(i["subject"]).strip(), str(i.get("size") or ""))
            for i in items
            if str(i.get("subject") or "").strip()
        ]
    except (ValueError, KeyError, TypeError) as exc:
        raise ModelCallError(f"mockup subjects: bad answer: {exc}") from exc
    if not pairs:
        raise ModelCallError("mockup subjects: none answered")
    return [pairs[i % len(pairs)] for i in range(count)]


async def draw_image(
    prompt: str,
    size: Tuple[int, int],
    *,
    paid: bool = False,
    preset: str = "best",
    key_ref: Optional[str] = None,
    what: str = "image",
    job_id: Optional[str] = None,
    ref_images: Sequence[Path] = (),
) -> bytes:
    """One ``image_output`` call: ``design_mockup`` (free FLUX) or, only when
    ``paid``, ``design_mockup_paid``. The image bytes; ModelCallError when
    none came back. The one image call of step 0, the sprint and the element
    tool (dv-d971). ``job_id``: a FLUX job of an earlier failed call, fetched
    instead of drawn again when the server still has it (dv-c237).
    ``ref_images``: input pictures, in order (dv-4032); FLUX takes them only
    when its server advertises ``ref_images`` (dv-1c6a), else the call fails."""
    width, height = size
    result = await get_router().complete(
        CallRequest(
            purpose=PAID_PURPOSE if paid else PURPOSE,
            prompt=prompt,
            system_prompt="",
            image_output=True,
            image_width=width,
            image_height=height,
            image_preset=None if paid else preset,
            timeout_seconds=IMAGE_TIMEOUT,
            key_ref=key_ref,
            image_job_id=job_id,
            images=tuple(str(p) for p in ref_images),
        )
    )
    if not result.image:
        raise ModelCallError(f"{what}: no image")
    return result.image


def _no_paid_fallback(where: Path) -> None:
    """Free runs (sprint ``--free``, the element tool without ``--paid``):
    this process's route table = the repo's models.yaml with design_mockup's
    paid OpenRouter fallback removed (MODEL_ROUTES_PATH, written under
    ``where``), so a FLUX failure fails the image instead of spending money."""
    import os

    import yaml

    from src.model_router.routes import default_model_routes_path

    src = Path(os.environ.get("MODEL_ROUTES_PATH") or default_model_routes_path())
    data = (
        yaml.safe_load(src.read_text(encoding="utf-8")) if src.exists() else None
    ) or {}
    data.setdefault("purposes", {})[PURPOSE] = {
        "harness": "flux",
        "models": {"flux": "flux3-klein"},
        "fallback": None,
    }
    where.mkdir(parents=True, exist_ok=True)
    path = where / "models-free.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    os.environ["MODEL_ROUTES_PATH"] = str(path)


def _flux_up() -> bool:
    """FLUX's generator is up (``flux_plain.flux_health``, dv-c237): a free
    run stops when Roman's GPU is off instead of falling back to paid
    OpenRouter. The server alone answering is not enough: it queues jobs
    while the generator is offline."""
    from src.model_router.flux_plain import flux_health

    return flux_health()[0]


async def make_mockups(
    *,
    spec: StyleSpec,
    brief_text: str,
    out_dir: Path,
    count: int = COUNT_DEFAULT,
    paid: bool = False,
    preset: str = "best",
    element: str = "",
    key_ref: Optional[str] = None,
    ref_images: Sequence[Path] = (),
    note: str = "",
) -> List[Path]:
    """Draw ``count`` (3..4) mockups into ``out_dir`` as ``mockup-N.png``,
    concurrently. Free FLUX unless ``paid``. The images that came back, in
    order; :class:`MockupError` when none did. ``ref_images`` (dv-4032): the
    current screen first, then style references, attached to every image
    call; ``note`` names what may change on the current screen."""
    count = _clamp(count)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    subjects = await mockup_subjects(brief_text, spec, count, element)
    fallback = ELEMENT_SIZE if element else DEFAULT_SIZE

    async def draw(n: int, subject: str, size: str) -> Path:
        prompt = mockup_prompt(subject, spec, element, len(ref_images), note)
        image = await draw_image(
            prompt,
            image_size(size, fallback),
            paid=paid,
            preset=preset,
            key_ref=key_ref,
            what=f"mockup {n}",
            ref_images=ref_images,
        )
        path = out_dir / f"mockup-{n}.png"
        path.write_bytes(image)
        (out_dir / f"mockup-{n}.prompt.txt").write_text(prompt, encoding="utf-8")
        return path

    results = await asyncio.gather(
        *(draw(n, s, z) for n, (s, z) in enumerate(subjects, 1)),
        return_exceptions=True,
    )
    paths = [r for r in results if isinstance(r, Path)]
    if not paths:
        errors = "; ".join(
            str(r)[:160] for r in results if isinstance(r, BaseException)
        )
        raise MockupError(f"no mockup drawn: {errors}")
    return paths


async def pick_mockup(
    brief_text: str, mockups: Sequence[Path], spec: StyleSpec
) -> Tuple[int, List[str]]:
    """The critic's pick: (0-based index, one reason line per mockup)."""
    if len(mockups) == 1:
        return 0, ["the only mockup drawn"]
    legend = "\n".join(
        f"image {i}: mockup {i}: {p.name}" for i, p in enumerate(mockups, 1)
    )
    result = await get_router().complete(
        CallRequest(
            purpose=PICK_PURPOSE,
            prompt=(
                f"{brief_text.strip()}\n\n# Images (attached, in this order)\n"
                f"{legend}\n"
                + (
                    ""
                    if spec.text() in brief_text
                    else f"\n# Style spec\n{spec.text()}\n"
                )
            ),
            system_prompt=PICK_SYSTEM,
            json_schema=PICK_SCHEMA,
            images=tuple(str(Path(p).resolve()) for p in mockups),
            timeout_seconds=PICK_TIMEOUT,
        )
    )
    try:
        data = json.loads(result.text)
        best = int(data["best"])
        reasons = [str(r).strip() for r in data.get("reasons") or []]
    except (ValueError, KeyError, TypeError) as exc:
        raise ModelCallError(f"mockup pick: bad answer: {exc}") from exc
    if not 1 <= best <= len(mockups):
        raise ModelCallError(f"mockup pick: {best} is not 1..{len(mockups)}")
    return best - 1, reasons


def mockup_offer_text(
    title: str, mockups: Sequence[Path], reasons: Sequence[str]
) -> str:
    """The owner's mockup choice (card label ``ownermockup``)."""
    lines = [f"🖼 {title}: {len(mockups)} mockups (step 0), pick the one to build to"]
    for i, path in enumerate(mockups, 1):
        why = reasons[i - 1] if i - 1 < len(reasons) else ""
        lines.append(f"{i}. {path.name}" + (f" — {why}" if why else ""))
    choices = ", ".join(str(i) for i in range(1, len(mockups) + 1))
    lines.append(f"↩ reply with the number of the mockup: {choices}")
    return "\n".join(lines)
