"""Element generation (dv-d971): one image per entry of a prompts file.

    python scripts/design_elements.py <board> <prompts.yaml> --out DIR \
        [--groups a,b] [--paid] [--within ROOT] [--refs-within ROOT ...]

The prompts file is YAML (JSON is valid YAML): a list of
``{group, name, prompt, size?: "WxH", refs?: [image, ...]}``. ``group`` and
``name`` are plain slugs; ``size`` is pixels as given (rounded down to
multiples of 16, each side 256..1664), none =
:data:`~src.designloop.mockups.ELEMENT_SIZE`. The prompt is drawn verbatim:
element prompts carry their own look and background.

``refs`` (dv-1c6a): 1-4 reference images (png/jpeg/webp/gif, at most 8 MB
each), relative to the prompts file's folder or absolute, each resolving
(symlinks followed) inside a ``--refs-within`` root; none given = refs are
refused. They go to the model with the prompt, in order. ``--paid`` takes
them; a free run stops before the first draw while the FLUX server does not
advertise ``ref_images`` (``flux_plain.flux_takes_images``).

Images are drawn one at a time (one GPU) through
:func:`src.designloop.mockups.draw_image`, the one image call of the design
loop, into ``DIR/<group>/<name>.png`` (+ ``.prompt.txt``, one ``log.jsonl``
line each). An existing image is skipped, so the same command resumes; a
failure whose FLUX job id was logged fetches that job then (dv-c237). Exit 1
when an element is still missing. Five
failures in a row stop the run (exit 1). Free FLUX with no paid fallback
unless ``--paid`` (``design_mockup_paid``, the board's key). Then one review
PDF (``DIR/elements-review.pdf``: a numbered grid per group, rendered by the
board's ``design.python``) and its cover PNG, printed as ``image: <path>``
lines for the job's ping.

Needs only a board with a ``design`` block (its python and FLUX preset), not
an approved style spec. Started from a board agent's chat by
``job_start(design="elements")``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import yaml

from src.model_router.flux_plain import flux_health, flux_takes_images
from src.model_router.types import IMAGE_MEDIA_TYPES
from src.pump2.state import load_boards

from . import assetmap, mockups
from .mockups import _SIZE, ELEMENT_SIZE, MAX_SIDE, _flux_up, _no_paid_fallback
from .review_pdf import ReviewError
from .review_pdf import item as review_item
from .review_pdf import render_with
from .steps import _parse_mockups

MAX_FAILURES_IN_A_ROW = 5
MIN_SIDE = 256
SHEET_COLUMNS = 4
MAX_REFS = 4
MAX_REF_BYTES = 8 * 1024 * 1024
SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def default_boards() -> Path:
    from src.pump2.cli import DEFAULT_BOARDS

    return DEFAULT_BOARDS


def default_root() -> Path:
    return Path(__file__).resolve().parents[2]


def is_slug(value: Any) -> bool:
    """A plain file-name part: no ``/``, no ``..``, no leading dot or dash."""
    return isinstance(value, str) and bool(SLUG.match(value))


@dataclass(frozen=True)
class Element:
    group: str
    name: str
    prompt: str
    size: Tuple[int, int]
    refs: Tuple[Path, ...] = ()


def element_size(size: Any) -> Tuple[int, int]:
    """ "WxH" pixels -> multiples of 16, each side 256..1664."""
    m = _SIZE.search(str(size or ""))
    if not m:
        return ELEMENT_SIZE
    return tuple(  # type: ignore[return-value]
        max(MIN_SIDE, min(MAX_SIDE, int(v) // 16 * 16)) for v in m.groups()
    )


def _inside(path: Path, roots: Sequence[Path]) -> bool:
    return any(path == r or r in path.parents for r in roots)


def load_refs(
    raw: Any, base: Path, roots: Sequence[Path], where: str
) -> Tuple[Path, ...]:
    """An entry's ``refs``: resolved image paths (dv-1c6a). ValueError
    starting with ``where`` for a bad list, a missing / non-image / too big
    file, or one resolving outside ``roots`` (the script runs unboxed and the
    file leaves the machine)."""
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_REFS:
        raise ValueError(f"{where}: refs must be a list of 1-{MAX_REFS} image paths")
    resolved = [r.expanduser().resolve() for r in roots]
    out: List[Path] = []
    for ref in raw:
        if not isinstance(ref, str) or not ref.strip():
            raise ValueError(f"{where}: ref {ref!r} is not a path")
        path = Path(ref.strip()).expanduser()
        if not path.is_absolute():
            path = base / path
        path = path.resolve()
        if not resolved or not _inside(path, resolved):
            raise ValueError(f"{where}: ref {ref} is outside the allowed folders")
        if path.suffix.lower() not in IMAGE_MEDIA_TYPES:
            raise ValueError(f"{where}: ref {ref} is not an image")
        if not path.is_file():
            raise ValueError(f"{where}: ref {ref} is not a file")
        if path.stat().st_size > MAX_REF_BYTES:
            raise ValueError(f"{where}: ref {ref} is over 8 MB")
        out.append(path)
    return tuple(out)


def load_prompts(path: Path, roots: Sequence[Path] = ()) -> List[Element]:
    """The prompts file; ValueError naming the first bad entry. ``roots``:
    the folders an entry's ``refs`` must resolve inside (none = no refs)."""
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, list) or not data:
        raise ValueError(f"{path}: expected a non-empty list of entries")
    out: List[Element] = []
    seen: set = set()
    for i, item in enumerate(data, 1):
        if not isinstance(item, dict):
            raise ValueError(f"entry {i}: not a mapping")
        group, name, prompt = item.get("group"), item.get("name"), item.get("prompt")
        for key, value in (("group", group), ("name", name)):
            if not is_slug(value):
                raise ValueError(f"entry {i}: {key} {value!r} is not a plain slug")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError(f"entry {i}: prompt must be a non-empty string")
        if (group, name) in seen:
            raise ValueError(f"entry {i}: {group}/{name} appears twice")
        seen.add((group, name))
        size = element_size(item.get("size"))
        refs: Tuple[Path, ...] = ()
        if "refs" in item:
            base = Path(path).expanduser().resolve().parent
            refs = load_refs(item["refs"], base, roots, f"entry {i}")
        out.append(Element(str(group), str(name), prompt.strip(), size, refs))
    return out


def _pending_jobs(out: Path) -> Dict[Tuple[str, str], str]:
    """(group, name) -> the FLUX job id of its last failed attempt, from
    ``log.jsonl`` (dv-c237): resuming fetches a job that finished late
    instead of drawing it again. A later line for the same element wins."""
    jobs: Dict[Tuple[str, str], str] = {}
    try:
        lines = (out / "log.jsonl").read_text(encoding="utf-8").splitlines()
    except OSError:
        return jobs
    for line in lines:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict):
            continue
        key = (str(rec.get("group")), str(rec.get("name")))
        job = rec.get("job_id")
        if isinstance(job, str) and job:
            jobs[key] = job
        else:
            jobs.pop(key, None)
    return jobs


def _log(out: Path, rec: Dict[str, Any]) -> None:
    rec = {"at": datetime.now(UTC).isoformat(timespec="seconds"), **rec}
    with (out / "log.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(json.dumps(rec, ensure_ascii=False), flush=True)


async def draw_all(
    elements: Sequence[Element],
    out: Path,
    *,
    paid: bool,
    preset: str,
    key_ref: Optional[str],
) -> bool:
    """Draw the missing ones in order; False when stopped by failures."""
    failures = 0
    pending = _pending_jobs(out)
    for el in elements:
        path = out / el.group / f"{el.name}.png"
        if path.exists():
            continue
        started = time.monotonic()
        try:
            image = await mockups.draw_image(
                el.prompt,
                el.size,
                paid=paid,
                preset=preset,
                key_ref=key_ref,
                what=f"{el.group}/{el.name}",
                job_id=pending.get((el.group, el.name)),
                ref_images=el.refs,
            )
        except Exception as exc:  # one failure must not stop the run
            failures += 1
            rec: Dict[str, Any] = {
                "group": el.group,
                "name": el.name,
                "ok": False,
                "error": str(exc)[:300],
            }
            if el.refs:
                rec["refs"] = len(el.refs)
            job_id = getattr(exc, "job_id", None)
            if job_id:
                rec["job_id"] = job_id
            _log(out, rec)
            if failures >= MAX_FAILURES_IN_A_ROW:
                print(f"stopped: {failures} failures in a row", flush=True)
                return False
            continue
        failures = 0
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(image)
        path.with_suffix(".prompt.txt").write_text(el.prompt, encoding="utf-8")
        done: Dict[str, Any] = {
            "group": el.group,
            "name": el.name,
            "ok": True,
            "size": f"{el.size[0]}x{el.size[1]}",
            "seconds": round(time.monotonic() - started, 1),
        }
        if el.refs:
            done["refs"] = len(el.refs)
        _log(out, done)
    return True


def sheets(elements: Sequence[Element], out: Path, python: str) -> List[Path]:
    """One review PDF of the drawn images (a numbered grid section per
    group) and its cover PNG; [cover, pdf], or [] when nothing was drawn or
    the render failed."""
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for el in elements:
        picture = out / el.group / f"{el.name}.png"
        if picture.exists():
            groups.setdefault(el.group, []).append(review_item(picture, el.name))
    if not groups:
        return []
    spec = {
        "title": f"Design elements: {out.name}",
        "intro": "Answer with the numbers of the ones to keep, e.g. «ок: 3, 7».",
        "sections": [
            {
                "heading": group,
                "layout": "grid",
                "columns": SHEET_COLUMNS,
                "items": items,
            }
            for group, items in groups.items()
        ],
    }
    pdf = out / "elements-review.pdf"
    try:
        cover, pdf = render_with(python, spec, pdf)
    except ReviewError as e:
        print(f"no review pdf: {str(e)[:200]}", flush=True)
        return []
    return [cover, pdf]


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Draw design elements, one per entry.")
    ap.add_argument("board")
    ap.add_argument("prompts", type=Path)
    ap.add_argument(
        "--out",
        type=Path,
        help="default: <design.assets>/elements/<prompts file name>",
    )
    ap.add_argument("--groups", default="", help="comma-separated groups to draw")
    ap.add_argument("--paid", action="store_true", help="paid OpenRouter route")
    ap.add_argument("--within", type=Path, help="--out must resolve inside this")
    ap.add_argument(
        "--refs-within",
        type=Path,
        action="append",
        default=[],
        help="an entry's refs must resolve inside one of these (repeatable)",
    )
    ap.add_argument("--by", default="", help="author profile: default out prefix")
    a = ap.parse_args(argv)
    if a.by and not is_slug(a.by):
        print(f"error: --by {a.by!r} is not a plain slug", file=sys.stderr)
        return 2
    try:
        elements = load_prompts(a.prompts, roots=a.refs_within)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        print(f"error: prompts file: {exc}", file=sys.stderr)
        return 2
    if a.groups:
        wanted = [g.strip() for g in a.groups.split(",") if g.strip()]
        bad = [g for g in wanted if not is_slug(g)]
        if bad:
            print(f"error: group {bad[0]!r} is not a plain slug", file=sys.stderr)
            return 2
        elements = [e for e in elements if e.group in wanted]
        if not elements:
            print(f"error: no entry in groups {','.join(wanted)}", file=sys.stderr)
            return 2
    board = load_boards(default_boards(), guard=True).get(a.board)
    design = getattr(board, "design", None)
    if not isinstance(design, dict):
        design = {}
    python = design.get("python")
    if board is None or not isinstance(python, str) or not python:
        print(f"error: board {a.board!r} has no design block", file=sys.stderr)
        return 2
    if a.out is None:
        root = assetmap.assets_root(design, board.profile, default_root())
        out = assetmap.default_elements_out(root, a.prompts, by=a.by)
    else:
        out = a.out.expanduser().resolve()
    if a.within is not None:
        root = a.within.expanduser().resolve()
        if out != root and root not in out.parents:
            print(f"error: --out {out} is outside {root}", file=sys.stderr)
            return 2
    try:
        config = _parse_mockups(design.get("mockups") or {})
    except ValueError as exc:
        print(f"error: design.{exc} is malformed", file=sys.stderr)
        return 2
    preset = config.preset if config else "best"
    key_ref = config.key if config else None
    out.mkdir(parents=True, exist_ok=True)
    if not a.paid:
        _no_paid_fallback(out)
        if not _flux_up():
            _, reason = flux_health()
            print(f"FLUX down: {reason}: stopped, no paid fallback", flush=True)
            return 1
        with_refs = sum(1 for e in elements if e.refs)
        if with_refs and not flux_takes_images():
            print(
                f"stopped: {with_refs} entr{'y has' if with_refs == 1 else 'ies have'}"
                " refs, but the free FLUX server has no reference-image input"
                " (no ref_images in its /openapi.json): run with --paid, or wait"
                " until the server gains ref_images; nothing drawn",
                flush=True,
            )
            return 1
    try:
        return _run(a, board, elements, out, python, preset, key_ref)
    finally:
        assetmap.refresh(board, default_root())


def _run(
    a: argparse.Namespace,
    board: Any,
    elements: List[Element],
    out: Path,
    python: str,
    preset: str,
    key_ref: Optional[str],
) -> int:
    ok = asyncio.run(
        draw_all(elements, out, paid=a.paid, preset=preset, key_ref=key_ref)
    )
    made = sheets(elements, out, python)
    drawn = sum((out / e.group / f"{e.name}.png").exists() for e in elements)
    print(f"{drawn}/{len(elements)} elements in {out}", flush=True)
    for path in made:
        print(f"image: {path}", flush=True)
    return 0 if ok and drawn == len(elements) else 1
