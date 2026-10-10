"""MCP server exposing Telegram-specific tools to Claude.

Runs as a stdio transport server. The ``send_image_to_user`` tool validates
file existence and extension, then returns a success string. Actual Telegram
delivery is handled by the bot's stream callback which intercepts the tool
call.
"""

import os
from pathlib import Path

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


if __name__ == "__main__":
    mcp.run(transport="stdio")
