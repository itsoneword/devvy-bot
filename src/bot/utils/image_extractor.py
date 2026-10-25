"""Validate image file paths and prepare them for Telegram delivery.

Used by the MCP ``send_image_to_user`` tool intercept — the stream callback
validates each path via :func:`validate_image_path` and collects
:class:`ImageAttachment` objects for later Telegram delivery.
"""

import mimetypes
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence, Union

import structlog

logger = structlog.get_logger()

# Env var carrying the os.pathsep-joined roots an image may be sent from.
# Set by the bot when it mounts the telegram MCP server, so the tool itself
# can reject an out-of-bounds path instead of the bot silently dropping it
# after the tool already told the model "queued for delivery".
IMAGE_ROOTS_ENV = "TELEGRAM_IMAGE_ROOTS"


def session_scratch_roots() -> List[Path]:
    """Scratch dirs an agent may legitimately write a chart to.

    Sandboxed profiles (coach, chef, pm, ...) get a per-session tmp dir and
    naturally write throwaway plots there rather than into the workspace. That
    is not an escape attempt, so those paths must be deliverable.
    """
    tmp = Path(tempfile.gettempdir())
    roots = [tmp / f"claude-{os.getuid()}"]
    return [r for r in roots if r.exists()]


def allowed_image_roots(
    working_directory: Union[str, Path, None],
    extra_dirs: Sequence[Union[str, Path]] = (),
) -> List[Path]:
    """Roots a ``send_image_to_user`` path may live under for this turn.

    ``extra_dirs`` are the profile's owner-granted extra roots (dv-f90c); the
    denylist check in :func:`validate_image_path` still applies to them.
    """
    roots: List[Path] = []
    if working_directory:
        roots.append(Path(working_directory))
    roots.extend(Path(d) for d in extra_dirs)
    roots.extend(session_scratch_roots())
    return roots


# Supported image extensions -> MIME types
IMAGE_EXTENSIONS = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".svg": "image/svg+xml",
}

# Raster formats that can be sent via reply_photo() (Telegram supports these natively)
TELEGRAM_PHOTO_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}

# Safety caps
MAX_IMAGES_PER_RESPONSE = 10
MAX_FILE_SIZE_BYTES = 50 * 1024 * 1024  # 50 MB
PHOTO_SIZE_LIMIT = 10 * 1024 * 1024  # 10 MB — Telegram photo API limit


def _is_within(resolved: Path, root: Path) -> bool:
    """True if ``resolved`` sits inside ``root`` (both fully resolved)."""
    try:
        resolved.relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


@dataclass
class ImageAttachment:
    """An image file to attach to a Telegram response."""

    path: Path
    mime_type: str
    original_reference: str


def validate_image_path(
    file_path: str,
    approved_directory: Union[Path, Sequence[Path]],
    caption: str = "",
    any_file: bool = False,
) -> Optional[ImageAttachment]:
    """Validate a single image path from an MCP ``send_image_to_user`` call.

    With ``any_file=True`` (``send_file_to_user``) any extension is accepted;
    non-image files are delivered as Telegram documents.

    ``approved_directory`` accepts a single root or a sequence of roots; the
    path must resolve inside at least one of them.

    Returns an :class:`ImageAttachment` if the path is a valid, existing image
    inside *approved_directory*, or ``None`` otherwise.
    """
    try:
        path = Path(file_path)
        if not path.is_absolute():
            return None

        resolved = path.resolve()

        # Security: must be within one of the approved roots
        roots = (
            [approved_directory]
            if isinstance(approved_directory, (str, Path))
            else list(approved_directory)
        )
        if not any(_is_within(resolved, Path(r)) for r in roots):
            logger.debug(
                "MCP image path outside approved roots",
                path=str(resolved),
                approved=[str(r) for r in roots],
            )
            return None

        # Data-root areas no agent may read (dv-2c1c): an approved root that
        # is an ancestor (e.g. APPROVED_DIRECTORY=~/Devvybot) must not make
        # secrets/ or archive/ deliverable. Lazy: keeps this module light.
        from src.security.denylist import protected_dirs

        if any(_is_within(resolved, d) for d in protected_dirs().everyone):
            logger.warning("MCP image path in a protected dir", path=str(resolved))
            return None

        if not resolved.is_file():
            return None

        file_size = resolved.stat().st_size
        if file_size > MAX_FILE_SIZE_BYTES:
            logger.debug("MCP image file too large", path=str(resolved), size=file_size)
            return None

        ext = resolved.suffix.lower()
        mime_type = IMAGE_EXTENSIONS.get(ext)
        if not mime_type and any_file:
            mime_type = (
                mimetypes.guess_type(resolved.name)[0] or "application/octet-stream"
            )
        if not mime_type:
            return None

        return ImageAttachment(
            path=resolved,
            mime_type=mime_type,
            original_reference=caption or file_path,
        )
    except (OSError, ValueError) as e:
        logger.debug("MCP image path validation failed", path=file_path, error=str(e))
        return None


def should_send_as_photo(path: Path) -> bool:
    """Return True if the image should be sent via reply_photo().

    Raster images ≤ 10 MB are sent as photos (inline preview).
    SVGs and large files are sent as documents.
    """
    ext = path.suffix.lower()
    if ext not in TELEGRAM_PHOTO_EXTENSIONS:
        return False

    try:
        return path.stat().st_size <= PHOTO_SIZE_LIMIT
    except OSError:
        return False
