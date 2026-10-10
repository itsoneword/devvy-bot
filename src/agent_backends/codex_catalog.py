"""Live Codex model catalog, read from `codex debug models`.

The `/model` keyboard used to hardcode two slugs. That is wrong twice over:
the entitled set depends on the subscription (this box lists four), and OpenAI
renames/retires slugs on its own schedule, so a constant silently becomes a
menu of models the CLI will reject.

`codex debug models` prints one JSON object with a `models` array; each entry
carries `slug`, `display_name`, `visibility`, `context_window` and `priority`.
Only `visibility == "list"` entries are offered to the user — `hide` covers
internal models (`gpt-reserve`, `codex-auto-review`) that exist but are not
user-selectable. `priority` is the vendor's own ordering, ascending.

The payload is large (~250KB on 0.152.0, mostly per-model instruction
templates), so it is fetched at most once per TTL per process and never
logged.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

import structlog

logger = structlog.get_logger()

# Model refs are stored/selected with this prefix ("openai-codex/gpt-5.5");
# _strip_provider_prefix in codex_cli.py removes it before `-m`.
CODEX_MODEL_PREFIX = "openai-codex/"

DEFAULT_TTL_SECONDS = 3600.0


@dataclass(frozen=True)
class CodexModel:
    """One user-selectable Codex model."""

    slug: str
    display_name: str
    context_window: int = 0
    priority: int = 0

    @property
    def ref(self) -> str:
        """The bot-internal model reference ("openai-codex/<slug>")."""
        return f"{CODEX_MODEL_PREFIX}{self.slug}"


# Last-resort catalog: stable, currently entitled slugs. Used
# when `codex debug models` is unavailable (CLI missing, logged out, network
# down) so /model never renders an empty backend section.
FALLBACK_MODELS: Tuple[CodexModel, ...] = (
    CodexModel(slug="gpt-5.6-sol", display_name="gpt-5.6-sol", context_window=272_000),
    CodexModel(slug="gpt-5.5", display_name="gpt-5.5", context_window=272_000),
)


def parse_codex_models(payload: Any) -> List[CodexModel]:
    """Parse `codex debug models` output into the listable models.

    Accepts the raw JSON text or an already-decoded object. Returns models
    with ``visibility == "list"`` sorted by ascending ``priority`` (the
    vendor's own "best first" order), then by slug for a stable tie-break.
    Raises ValueError when the payload has no recognisable model list — the
    caller falls back rather than showing a truncated menu.
    """
    if isinstance(payload, (str, bytes)):
        payload = json.loads(payload)
    if not isinstance(payload, dict):
        raise ValueError("codex debug models: expected a JSON object")
    raw_models = payload.get("models")
    if not isinstance(raw_models, list):
        raise ValueError("codex debug models: no 'models' array")

    models: List[CodexModel] = []
    for entry in raw_models:
        if not isinstance(entry, dict):
            continue
        if entry.get("visibility") != "list":
            continue
        slug = entry.get("slug")
        if not isinstance(slug, str) or not slug:
            continue
        display = entry.get("display_name")
        try:
            ctx = int(entry.get("context_window") or 0)
        except (TypeError, ValueError):
            ctx = 0
        try:
            priority = int(entry.get("priority") or 0)
        except (TypeError, ValueError):
            priority = 0
        models.append(
            CodexModel(
                slug=slug,
                display_name=str(display) if display else slug,
                context_window=ctx,
                priority=priority,
            )
        )
    if not models:
        raise ValueError("codex debug models: no listable models")
    models.sort(key=lambda m: (m.priority, m.slug))
    return models


class CodexModelCatalog:
    """Process-wide cache of the live Codex model list.

    ``get()`` is the async accessor (fetches when stale); ``cached()`` is the
    synchronous one for call sites that cannot await, such as the model-ref
    validation in ``OrchestratorBase._selected_model``. Both fall back to
    :data:`FALLBACK_MODELS`, so the two historical constants keep working
    whatever the CLI does.
    """

    def __init__(
        self,
        codex_bin: str = "codex",
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        timeout: float = 30.0,
    ) -> None:
        self._codex_bin = codex_bin
        self._ttl = ttl_seconds
        self._timeout = timeout
        self._models: Optional[List[CodexModel]] = None
        self._fetched_at = 0.0
        self._lock = asyncio.Lock()

    def invalidate(self) -> None:
        """Drop the cache — call after a successful `/auth` Codex login, since
        the entitled model set is a property of the account that just changed.
        """
        self._models = None
        self._fetched_at = 0.0

    def cached(self) -> List[CodexModel]:
        """Last known catalog without touching the CLI (fallback if never fetched)."""
        return list(self._models) if self._models else list(FALLBACK_MODELS)

    async def get(self, force: bool = False) -> List[CodexModel]:
        """Return the catalog, refreshing it when older than the TTL."""
        now = time.monotonic()
        if not force and self._models and (now - self._fetched_at) < self._ttl:
            return list(self._models)
        async with self._lock:
            # Another waiter may have refreshed while we queued.
            now = time.monotonic()
            if not force and self._models and (now - self._fetched_at) < self._ttl:
                return list(self._models)
            models = await self._fetch()
            if models:
                self._models = models
                self._fetched_at = time.monotonic()
        return self.cached()

    async def _fetch(self) -> Optional[List[CodexModel]]:
        try:
            proc = await asyncio.create_subprocess_exec(
                self._codex_bin,
                "debug",
                "models",
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except (OSError, ValueError) as exc:
            logger.warning("codex debug models unavailable", error=str(exc))
            return None
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=self._timeout
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            logger.warning("codex debug models timed out", timeout=self._timeout)
            return None
        if proc.returncode != 0:
            # stderr can carry a ~250KB parse dump on a stale CLI — one line.
            tail = stderr.decode("utf-8", errors="replace").strip()[-300:]
            logger.warning(
                "codex debug models failed", exit_code=proc.returncode, stderr=tail
            )
            return None
        try:
            models = parse_codex_models(stdout.decode("utf-8", errors="replace"))
        except (ValueError, json.JSONDecodeError) as exc:
            logger.warning("codex debug models unparsable", error=str(exc))
            return None
        logger.info("Codex model catalog refreshed", models=[m.slug for m in models])
        return models


def supported_refs(
    models: Sequence[CodexModel], *, always: Sequence[str] = ()
) -> frozenset:
    """Model refs the bot will accept: the live catalog plus ``always``.

    ``always`` keeps ``_CODEX_DEFAULT_MODEL``/``_CODEX_HARD_MODEL`` selectable
    even when the catalog cannot be read or has dropped them.
    """
    refs: Dict[str, None] = {m.ref: None for m in models}
    for ref in always:
        refs[ref] = None
    return frozenset(refs)


#: The one catalog the bot uses. Module-level so the TTL is per process.
CODEX_CATALOG = CodexModelCatalog()
