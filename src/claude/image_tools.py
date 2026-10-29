"""``view_image``: the one way an agent looks at an image (dv-275f).

A raw ``Read`` of a PNG returns the whole file base64-encoded: a 1.2 MB image
killed a turn at the old 1 MiB SDK buffer (``docs/FINDINGS.md`` 2026-08-04),
and a full-size phone render (1170x2532 @3x) wastes context even when it
fits. So ``src/security/policy.py`` denies ``Read`` of raster files and points
here. This tool:

* checks every path against the image roots ``send_image_to_user`` uses
  (workspace + the profile's ``extra_dirs`` + session scratch, protected
  dirs refused even inside a root: :func:`validate_image_path`), raster only;
* downscales to a long edge of at most :data:`MAX_EDGE` and encodes under
  :data:`MAX_BYTES` (shrinking further if quality alone is not enough);
* with ``paths`` (2..6) returns ONE numbered contact sheet, so comparing
  variants costs one image;
* returns the SDK's image block plus a text block with sizes and the legend.

It is a :class:`ToolSpec` in ``ALL_TOOL_SPECS``, so the Claude in-process
server and the Codex bridge serve the same handler. Pillow is imported inside
the call: a venv without it still starts the bot, and the tool says why it
cannot work.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import math
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import structlog

from ..security.policy import RASTER_IMAGE_SUFFIXES
from .tool_context import AgentServices, ToolSpec, TurnContext

logger = structlog.get_logger()

# Claude's native long-edge limit: anything bigger is downscaled by the API
# anyway, so sending more is pure waste.
MAX_EDGE = 1568
MIN_EDGE = 256
# The encoded image, before base64 (x4/3 on the stream): far below the
# 16 MiB SDK buffer, and small enough to keep a few per turn.
MAX_BYTES = 400_000
MAX_SHEET = 6


_JPEG_QUALITIES = (85, 75, 60, 45)
_SHRINK = 0.8
_LABEL_HEIGHT = 28
_GAP = 8


class ImageToolError(Exception):
    """A refusal or failure the model should read as the tool's answer."""


def _err(message: str) -> Dict[str, Any]:
    return {"content": [{"type": "text", "text": f"error: {message}"}], "isError": True}


# -- argument parsing ---------------------------------------------------------


def _paths_arg(args: Dict[str, Any]) -> List[str]:
    path = args.get("path")
    paths = args.get("paths")
    if path and paths:
        raise ImageToolError("pass either path or paths, not both")
    if isinstance(path, str) and path:
        return [path]
    if isinstance(paths, list) and paths:
        if not all(isinstance(p, str) and p for p in paths):
            raise ImageToolError("paths must be a list of absolute file paths")
        if len(paths) > MAX_SHEET:
            raise ImageToolError(
                f"at most {MAX_SHEET} images per sheet, got {len(paths)}"
            )
        return list(paths)
    raise ImageToolError("path (one image) or paths (2-6 images) is required")


def _region_arg(value: Any) -> Optional[Tuple[float, float, float, float]]:
    if value is None:
        return None
    if (
        not isinstance(value, (list, tuple))
        or len(value) != 4
        or not all(
            isinstance(v, (int, float)) and not isinstance(v, bool) for v in value
        )
    ):
        raise ImageToolError("region is [x0, y0, x1, y1], fractions 0..1")
    x0, y0, x1, y1 = (float(v) for v in value)
    if not (0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1):
        raise ImageToolError("region needs 0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1")
    return (x0, y0, x1, y1)


def _max_edge_arg(value: Any) -> int:
    if value is None:
        return MAX_EDGE
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ImageToolError("max_edge is a number of pixels")
    return max(MIN_EDGE, min(MAX_EDGE, int(value)))


def _roots(services: AgentServices, ctx: TurnContext) -> List[Path]:
    """The turn's image roots: the same ones ``send_image_to_user`` gets."""
    # Lazy, like every src.claude import of image_extractor (import cycle).
    from ..bot.utils.image_extractor import allowed_image_roots

    extra: Sequence[Path] = ()
    registry = services.profile_registry
    if registry is not None and ctx.working_dir is not None:
        profile = registry.get_by_working_dir(ctx.working_dir)
        extra = tuple(getattr(profile, "extra_dirs", ()) or ()) if profile else ()
    return allowed_image_roots(ctx.working_dir, extra)


def _checked(paths: List[str], roots: List[Path]) -> List[Path]:
    from ..bot.utils.image_extractor import validate_image_path

    out: List[Path] = []
    for raw in paths:
        attachment = validate_image_path(raw, roots)
        if attachment is None or attachment.path.suffix.lower() not in (
            RASTER_IMAGE_SUFFIXES
        ):
            # Generic on purpose: which of the reasons applies is not
            # something a refused path should learn.
            raise ImageToolError(
                f"{raw!r} is outside this agent's image roots, missing, "
                f"or not a raster image (png/jpg/gif/webp/bmp)"
            )
        out.append(attachment.path)
    return out


# -- rendering (Pillow, run in a worker thread) -------------------------------


def _load(path: Path, region: Optional[Tuple[float, float, float, float]]) -> Any:
    from PIL import Image, ImageOps

    with Image.open(path) as opened:
        opened.seek(0)  # animations: first frame only
        img = ImageOps.exif_transpose(opened)
        img.load()
    original = img.size
    if region is not None:
        w, h = img.size
        x0, y0, x1, y1 = region
        box = (round(x0 * w), round(y0 * h), round(x1 * w), round(y1 * h))
        if box[2] <= box[0] or box[3] <= box[1]:
            raise ImageToolError("region is smaller than one pixel")
        img = img.crop(box)
    return img, original


def _has_alpha(img: Any) -> bool:
    return img.mode in ("RGBA", "LA") or (
        img.mode == "P" and "transparency" in img.info
    )


def _flatten(img: Any) -> Any:
    from PIL import Image

    if not _has_alpha(img):
        return img.convert("RGB")
    rgba = img.convert("RGBA")
    canvas = Image.new("RGB", rgba.size, "white")
    canvas.paste(rgba, mask=rgba.getchannel("A"))
    return canvas


def _encode(img: Any, max_bytes: int) -> Tuple[bytes, str, Tuple[int, int]]:
    """PNG for transparency when it fits, else JPEG; shrink until under cap."""
    from PIL import Image

    while True:
        if _has_alpha(img):
            buf = io.BytesIO()
            img.save(buf, "PNG", optimize=True)
            if buf.tell() <= max_bytes:
                return buf.getvalue(), "image/png", img.size
        rgb = _flatten(img)
        for quality in _JPEG_QUALITIES:
            buf = io.BytesIO()
            rgb.save(buf, "JPEG", quality=quality, optimize=True)
            if buf.tell() <= max_bytes:
                return buf.getvalue(), "image/jpeg", img.size
        w, h = img.size
        if max(w, h) * _SHRINK < MIN_EDGE:
            raise ImageToolError(f"could not encode the image under {max_bytes} bytes")
        img = img.resize(
            (max(1, round(w * _SHRINK)), max(1, round(h * _SHRINK))),
            Image.Resampling.LANCZOS,
        )


def _sheet_layout(sizes: List[Tuple[int, int]], max_edge: int) -> Tuple[int, int, int]:
    """(cols, cell_w, cell_h) giving the biggest cells inside max_edge."""
    n = len(sizes)
    aspect = sorted(w / h for w, h in sizes)[n // 2]  # median width/height
    best: Tuple[float, int] = (0.0, 1)
    for cols in range(1, n + 1):
        rows = math.ceil(n / cols)
        # Cell height that makes the sheet's long edge exactly max_edge.
        by_w = (max_edge - _GAP * (cols + 1)) / (cols * aspect)
        by_h = (max_edge - _GAP * (rows + 1)) / rows - _LABEL_HEIGHT
        cell_h = min(by_w, by_h)
        if cell_h >= best[0]:
            best = (cell_h, cols)
    cell_h = max(16, int(best[0]))
    return best[1], max(16, int(cell_h * aspect)), cell_h


def _contact_sheet(images: List[Any], names: List[str], max_edge: int) -> Any:
    from PIL import Image, ImageDraw, ImageFont

    try:
        font: Any = ImageFont.load_default(size=_LABEL_HEIGHT - 10)
    except TypeError:  # Pillow < 10.1: fixed-size bitmap font only
        font = ImageFont.load_default()
    cols, cell_w, cell_h = _sheet_layout([im.size for im in images], max_edge)
    rows = math.ceil(len(images) / cols)
    width = cols * cell_w + _GAP * (cols + 1)
    height = rows * (cell_h + _LABEL_HEIGHT) + _GAP * (rows + 1)
    sheet = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(sheet)
    for i, (img, name) in enumerate(zip(images, names)):
        row, col = divmod(i, cols)
        left = _GAP + col * (cell_w + _GAP)
        top = _GAP + row * (cell_h + _LABEL_HEIGHT + _GAP)
        draw.text((left + 4, top + 4), f"{i + 1}  {name}", fill="black", font=font)
        cell = _flatten(img)
        cell.thumbnail((cell_w, cell_h), Image.Resampling.LANCZOS)
        sheet.paste(
            cell,
            (
                left + (cell_w - cell.width) // 2,
                top + _LABEL_HEIGHT + (cell_h - cell.height) // 2,
            ),
        )
    return sheet


def render_for_model(
    paths: List[Path],
    region: Optional[Tuple[float, float, float, float]] = None,
    max_edge: int = MAX_EDGE,
    max_bytes: int = MAX_BYTES,
) -> Tuple[bytes, str, Dict[str, Any]]:
    """(encoded bytes, mime type, meta) for one image or one contact sheet.

    Blocking (Pillow): call it through ``asyncio.to_thread``. Raises
    ``ImportError`` without Pillow and :class:`ImageToolError` on a bad image.
    """
    from PIL import Image

    loaded = []
    for path in paths:
        try:
            loaded.append(_load(path, region))
        except ImageToolError:
            raise
        except Exception as exc:  # decode errors, decompression bombs
            raise ImageToolError(
                f"could not decode {path.name}: {type(exc).__name__}"
            ) from exc
    meta: Dict[str, Any] = {}
    if len(loaded) == 1:
        img, original = loaded[0]
        img.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
        meta["file"] = paths[0].name
        meta["original"] = list(original)
    else:
        img = _contact_sheet(
            [im for im, _ in loaded], [p.name for p in paths], max_edge
        )
        meta["sheet"] = [
            {"n": i, "file": p.name, "original": list(original)}
            for i, (p, (_, original)) in enumerate(zip(paths, loaded), start=1)
        ]
    if region is not None:
        meta["region"] = list(region)
    data, mime, sent = _encode(img, max_bytes)
    meta["sent"] = list(sent)
    meta["bytes"] = len(data)
    return data, mime, meta


# -- the tool -----------------------------------------------------------------


async def _view_image(
    services: AgentServices, ctx: TurnContext, args: Dict[str, Any]
) -> Dict[str, Any]:
    return await view_image_in_roots(_roots(services, ctx), args, ctx.profile_slug)


async def view_image_in_roots(
    roots: List[Path], args: Dict[str, Any], profile: str = ""
) -> Dict[str, Any]:
    """The tool body for a caller that already knows its roots: the bot's
    turn (above) and the background jobs' stdio server (``src/mcp/job_image_server.py``).
    """
    try:
        raw_paths = _paths_arg(args)
        region = _region_arg(args.get("region"))
        max_edge = _max_edge_arg(args.get("max_edge"))
        paths = _checked(raw_paths, roots)
        data, mime, meta = await asyncio.to_thread(
            render_for_model, paths, region, max_edge
        )
    except ImageToolError as exc:
        return _err(str(exc))
    except ImportError:
        logger.warning("view_image: Pillow is not installed")
        return _err(
            "view_image needs Pillow, which is not installed in the bot's "
            "environment; ask the owner to run `poetry install`"
        )
    logger.info(
        "view_image",
        profile=profile,
        images=len(paths),
        bytes=meta["bytes"],
        sent=meta["sent"],
    )
    return {
        "content": [
            {
                "type": "image",
                "data": base64.b64encode(data).decode("ascii"),
                "mimeType": mime,
            },
            {"type": "text", "text": json.dumps(meta)},
        ]
    }


_VIEW_IMAGE_DESCRIPTION = (
    "Look at an image file (png/jpg/gif/webp/bmp). The only way to see an "
    "image: Read on an image file is refused. The image is downscaled to a "
    f"long edge of at most {MAX_EDGE} px and under {MAX_BYTES // 1000} KB, so a "
    "full-size render is safe to pass. `path`: one absolute path. `paths`: "
    f"2-{MAX_SHEET} absolute paths, returned as ONE numbered contact sheet to "
    "compare variants side by side (the legend maps numbers to files). "
    "`region`: [x0, y0, x1, y1] as fractions 0..1 to zoom into part of one "
    "image (e.g. [0, 0, 1, 0.25] = top quarter). `max_edge`: smaller long "
    f"edge in px ({MIN_EDGE}-{MAX_EDGE}) when detail is not needed. The path "
    "must be in your workspace, an extra dir, or your scratch dir."
)

_NUMBER = {"type": "number"}

VIEW_IMAGE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "Absolute path of one image"},
        "paths": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": MAX_SHEET,
            "description": "2-6 absolute paths for one contact sheet",
        },
        "region": {
            "type": "array",
            "items": _NUMBER,
            "minItems": 4,
            "maxItems": 4,
            "description": "[x0, y0, x1, y1] crop as fractions 0..1",
        },
        "max_edge": {
            "type": "integer",
            "description": f"Long edge in px, {MIN_EDGE}-{MAX_EDGE}",
        },
    },
    # One of path / paths: the handler says which is missing.
    "required": [],
}

IMAGE_SPECS: List[ToolSpec] = [
    ToolSpec(
        name="view_image",
        description=_VIEW_IMAGE_DESCRIPTION,
        schema=VIEW_IMAGE_SCHEMA,
        handler=_view_image,
    ),
]
