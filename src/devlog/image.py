"""The devlog picture: one ``post_image`` call through the model router
(dv-fc9d), so every picture has its ``model_calls`` row and cost.

Code, not the agent, adds the "no text" instruction; a headline is drawn
afterwards by ``overlay``. A monthly cap (profile key ``image_cap_usd``,
default 10 USD) on this month's ``post_image`` cost refuses further calls.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Optional

PURPOSE = "post_image"
DEFAULT_CAP_USD = 10.0
# A call whose cost is unknown counts at this (above the ~0.04 USD real price).
UNKNOWN_IMAGE_COST_USD = 0.05
MAX_PROMPT_CHARS = 1500
NO_TEXT = (
    "Absolutely no text, letters, numbers, captions, logos or watermarks "
    "anywhere in the picture."
)
SYSTEM = (
    "You draw one illustration for a developer's Telegram channel post. "
    "Clean, modern, editorial style; one clear subject. " + NO_TEXT
)


class ImageCapReached(RuntimeError):
    """This month's ``post_image`` spend has reached the cap."""


def build_image_prompt(prompt: str) -> str:
    """The agent's scene description with the no-text rule added by code."""
    scene = " ".join((prompt or "").split())[:MAX_PROMPT_CHARS]
    if not scene:
        raise ValueError("describe the picture")
    return f"{scene}\n\n{NO_TEXT} Leave the bottom fifth calm: a caption goes there."


def check_cap(*, month_cost_usd: float, cap_usd: float) -> None:
    if month_cost_usd >= cap_usd:
        raise ImageCapReached(
            f"image budget reached: {month_cost_usd:.2f} of {cap_usd:.2f} USD this "
            "month (profile key image_cap_usd). Use a screenshot or no picture."
        )


def month_cost(db_path: Path, now: Optional[datetime] = None) -> float:
    """This calendar month's ``post_image`` cost from ``model_calls`` (UTC)."""
    now = now or datetime.now(UTC)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    from ..storage.channel_posts import purpose_cost_since

    return purpose_cost_since(
        db_path, PURPOSE, start, unknown_cost_usd=UNKNOWN_IMAGE_COST_USD
    )


async def generate_image(
    prompt: str,
    out_dir: Path,
    *,
    headline: Optional[str] = None,
    db_path: Optional[Path] = None,
    cap_usd: float = DEFAULT_CAP_USD,
) -> Path:
    """One picture through the router, the headline drawn on it, saved as PNG
    in ``out_dir``. Raises ``ImageCapReached`` / ``ValueError`` /
    ``ModelCallError``."""
    from ..model_router.router import get_router
    from ..model_router.types import CallRequest
    from .overlay import MAX_HEADLINE_CHARS, draw_headline

    if headline and len(headline) > MAX_HEADLINE_CHARS:
        raise ValueError(f"headline over {MAX_HEADLINE_CHARS} characters")
    if db_path is not None:
        check_cap(month_cost_usd=month_cost(db_path), cap_usd=cap_usd)
    result = await get_router().complete(
        CallRequest(
            purpose=PURPOSE,
            prompt=build_image_prompt(prompt),
            system_prompt=SYSTEM,
            image_output=True,
            timeout_seconds=180.0,
        )
    )
    if not result.image:
        raise ValueError("the image model returned no picture")
    data = draw_headline(result.image, headline) if headline else _as_png(result.image)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"post-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%f')}.png"
    path.write_bytes(data)
    return path


def _as_png(data: bytes) -> bytes:
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.open(io.BytesIO(data)).convert("RGB").save(buf, format="PNG")
    return buf.getvalue()
