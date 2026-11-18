"""MCP server exposing Telegram-specific tools to Claude.

Runs as a stdio transport server. The ``send_image_to_user`` tool validates
file existence and extension, then returns a success string. Actual Telegram
delivery is handled by the bot's stream callback which intercepts the tool
call.
"""

import asyncio
import importlib.util
import os
import sys
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}

# os.pathsep-joined roots an image may be sent from, injected by the bot when
# it mounts this server. Unset = no boundary enforced here (legacy behaviour).
IMAGE_ROOTS_ENV = "TELEGRAM_IMAGE_ROOTS"

mcp = FastMCP("telegram")


def _within(resolved: Path, root: Path) -> bool:
    try:
        resolved.relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


@mcp.tool()
async def send_image_to_user(file_path: str, caption: str = "") -> str:
    """Send an image file to the Telegram user.

    Args:
        file_path: Absolute path to the image file.
        caption: Optional caption to display with the image.

    Returns:
        Confirmation string when the image is queued for delivery.
    """
    path = Path(file_path)

    if not path.is_absolute():
        return f"Error: path must be absolute, got '{file_path}'"

    if path.suffix.lower() not in IMAGE_EXTENSIONS:
        return (
            f"Error: unsupported image extension '{path.suffix}'. "
            f"Supported: {', '.join(sorted(IMAGE_EXTENSIONS))}"
        )

    if not path.is_file():
        return f"Error: file not found: {file_path}"

    # Boundary check MUST happen here, not only in the bot's stream-callback
    # intercept. The intercept drops an out-of-bounds image silently, and the
    # model has already been told "queued for delivery" -- so it reports a
    # successful send that never happened, and the user is left waiting for an
    # image while the agent insists it was sent. Return a real error instead,
    # naming the allowed roots so the model can rewrite the path and retry.
    roots = [r for r in os.environ.get(IMAGE_ROOTS_ENV, "").split(os.pathsep) if r]
    if roots:
        resolved = path.resolve()
        if not any(_within(resolved, Path(r)) for r in roots):
            allowed = ", ".join(roots)
            return (
                f"Error: '{file_path}' is outside the deliverable roots and "
                f"was NOT sent. Write the image under one of these and call "
                f"this tool again: {allowed}"
            )

    return f"Image queued for delivery: {path.name}"


MAX_FILE_BYTES = 50 * 1024 * 1024  # Telegram bot API upload cap


@mcp.tool()
async def send_file_to_user(file_path: str, caption: str = "") -> str:
    """Send any file (PDF, docx, csv, zip, ...) to the Telegram user as a document.

    Args:
        file_path: Absolute path to the file.
        caption: Optional caption.

    Returns:
        Confirmation string when the file is queued for delivery.
    """
    path = Path(file_path)

    if not path.is_absolute():
        return f"Error: path must be absolute, got '{file_path}'"

    if not path.is_file():
        return f"Error: file not found: {file_path}"

    if path.stat().st_size > MAX_FILE_BYTES:
        return "Error: file is larger than Telegram's 50 MB upload limit"

    # Same reason as send_image_to_user: refuse here, don't let the bot drop it
    # silently after the model was told "queued".
    roots = [r for r in os.environ.get(IMAGE_ROOTS_ENV, "").split(os.pathsep) if r]
    if roots:
        resolved = path.resolve()
        if not any(_within(resolved, Path(r)) for r in roots):
            allowed = ", ".join(roots)
            return (
                f"Error: '{file_path}' is outside the deliverable roots and "
                f"was NOT sent. Copy the file under one of these and call "
                f"this tool again: {allowed}"
            )

    return f"File queued for delivery: {path.name}"


REVIEW_SCRIPT = Path(__file__).resolve().parent.parent / "designloop" / "review_pdf.py"


def _load_review_pdf() -> Any:
    """The renderer, loaded by path: this file runs as a script, with no
    ``src`` package on its path."""
    spec = importlib.util.spec_from_file_location("review_pdf", REVIEW_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["review_pdf"] = module
    spec.loader.exec_module(module)
    return module


def _outside_roots(path: Path) -> str:
    """The error text when ``path`` is outside the deliverable roots, else ""."""
    roots = [r for r in os.environ.get(IMAGE_ROOTS_ENV, "").split(os.pathsep) if r]
    if roots and not any(_within(path.resolve(), Path(r)) for r in roots):
        return (
            f"Error: '{path}' is outside the deliverable roots and nothing was "
            f"sent. Use a path under one of these: {', '.join(roots)}"
        )
    return ""


def _protected(path: Path) -> str:
    """The error text when ``path`` is in a protected data dir (the bot's
    denylist, as at delivery: a picture of secrets/ must not enter a PDF),
    else "". Fails closed when the denylist cannot be loaded."""
    try:
        repo = str(Path(__file__).resolve().parent.parent.parent)
        if repo not in sys.path:
            sys.path.insert(0, repo)
        from src.security.denylist import protected_dirs

        resolved = path.resolve()
        if any(_within(resolved, d) for d in protected_dirs().everyone):
            return f"Error: '{path}' is in a protected folder and was not used"
    except Exception as e:  # noqa: BLE001 - fail closed
        return f"Error: cannot check '{path}' against the protected folders: {e}"
    return ""


@mcp.tool()
async def send_review(
    title: str,
    sections: list[dict[str, Any]],
    file_path: str,
    intro: str = "",
) -> str:
    """Send 2+ pictures for the user to review as ONE structured PDF (a cover
    photo plus the PDF). Use it for mockups, element sheets, v1 vs v2,
    screenshots; one picture alone goes through send_image_to_user.

    Args:
        title: Document title (cover page).
        sections: List of {heading, layout, note?, columns?, items}. layout is
            "table" (row per item: thumbnail, caption, label, score; columns:
            optional [caption header, score header]), "grid" (columns tiles
            per row, default 3, numbered tiles), "compare" (items in pairs:
            left, right; label above each side) or "single" (one picture per
            page). items: list of {image: absolute path, caption, score?,
            label?}. Items are numbered 1, 2, 3 ... through the whole
            document, so the user can answer "ok: 3, 7"; mention the numbers
            in your message.
        file_path: Absolute path of the PDF to write (a .pdf inside your
            workspace); the cover goes to <name>-sheet.png next to it.
        intro: Optional text under the title on the cover.

    Returns:
        Confirmation with the page count, or an error (nothing sent).
    """
    path = Path(file_path)
    if not path.is_absolute() or path.suffix.lower() != ".pdf":
        return f"Error: file_path must be an absolute .pdf path, got '{file_path}'"
    bad = _outside_roots(path) or _protected(path)
    if bad:
        return bad
    review = _load_review_pdf()
    spec = {"title": title, "intro": intro, "sections": sections}
    try:
        spec = review.validate(spec)
    except review.ReviewError as e:
        return f"Error: {e}"
    for sec in spec["sections"]:
        for it in sec["items"]:
            raw = it.get("image")
            if not raw:
                continue
            img = Path(str(raw))
            if not img.is_absolute():
                return f"Error: image path must be absolute, got '{raw}'"
            if not img.is_file():
                return f"Error: image not found: {raw}"
            bad = _outside_roots(img) or _protected(img)
            if bad:
                return bad
    try:
        pages = await asyncio.to_thread(
            review.render, spec, path, review.cover_path(path)
        )
    except (OSError, ValueError) as e:
        return f"Error: render failed, nothing sent: {e}"
    size = path.stat().st_size
    if size > MAX_FILE_BYTES:
        return "Error: the PDF is larger than Telegram's 50 MB limit; split it"
    return f"Review queued for delivery: {path.name}, {pages} pages"


if __name__ == "__main__":
    mcp.run(transport="stdio")
