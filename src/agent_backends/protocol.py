"""The seam between Telegram orchestration and an agent backend (dv-5368).

Two registries, one id space (``"claude"``, ``"openai_codex"``):

* :class:`AgentBackend` — the *runtime* interface. One live instance per
  backend, registered in ``bot_data["backends"]`` by ``main.py``. This is
  everything the orchestrator and the ``/status`` / ``/sessions`` / ``cd``
  commands are allowed to call; nothing outside ``main.py`` may reach for
  ``sdk_manager``, ``session_manager`` or ``store`` any more.
* :class:`BackendPolicy` — the *static* knowledge about a backend id that
  must work with no live instance at all: which ``user_data`` key holds its
  resumable session, how a stored ``agent_model`` preference is validated,
  what label ``/sessions`` prints. Registered in :data:`BACKEND_POLICIES`.
  Static because ``/model``, prefs seeding and unit tests need it before or
  without a backend object, and because orchestration tests hand in mocks
  for the instances.

A third backend registers once in each: an instance in
``bot_data["backends"]`` (``main.py``) and a policy in
:data:`BACKEND_POLICIES` (here). ``settings.agent_backend``'s ``Literal``
is config validation, not dispatch, and widens separately.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Dict,
    List,
    Optional,
    Protocol,
    runtime_checkable,
)

if TYPE_CHECKING:
    from src.claude.sdk_integration import ClaudeResponse, StreamUpdate
    from src.config.settings import Settings

CLAUDE_BACKEND = "claude"
CODEX_BACKEND = "openai_codex"

_CODEX_DEFAULT_MODEL = "openai-codex/gpt-5.4-mini"
_CODEX_HARD_MODEL = "openai-codex/gpt-5.5"
# Kept as the floor of the accepted set, not as the whole set: the real list
# comes from `codex debug models` via CODEX_CATALOG (dv-0c01). These two stay
# selectable even when the CLI cannot be read, so a stored preference or a
# config default never gets silently rewritten because the catalog is down.
_CODEX_PINNED_MODELS = (
    _CODEX_HARD_MODEL,
    _CODEX_DEFAULT_MODEL,
)

_CLAUDE_TIERS = ("sonnet", "opus")


def codex_supported_models() -> frozenset:
    """Model refs the bot accepts for the Codex backend, live catalog first."""
    from .codex_catalog import CODEX_CATALOG, supported_refs

    return supported_refs(CODEX_CATALOG.cached(), always=_CODEX_PINNED_MODELS)


@dataclass(frozen=True)
class BackendSession:
    """One resumable conversation as a backend reports it (``/sessions``)."""

    session_id: str
    working_dir: Path
    last_used: datetime
    cumulative_tokens: int
    backend_id: str


@runtime_checkable
class AgentBackend(Protocol):
    """What orchestration and the info commands need from a live backend.

    Kept to what is called today; ``main.py`` (the composition root) may
    still know the concrete classes for wiring that is genuinely
    backend-specific (Claude's confirmation gate, the evaluator's
    conversation reader).
    """

    backend_id: str

    def default_model(self) -> str:
        """Model a turn runs on when ``run_command`` gets ``model=None``."""
        ...

    async def run_command(
        self,
        prompt: str,
        working_directory: Path,
        user_id: int,
        session_id: Optional[str] = None,
        on_stream: Optional[Callable[["StreamUpdate"], Any]] = None,
        force_new: bool = False,
        images: Optional[List[Path]] = None,
        model: Optional[str] = None,
        requester_chat_id: Optional[int] = None,
        family_id: Optional[int] = None,
        role: Optional[str] = None,
    ) -> "ClaudeResponse":
        """Run one agent turn and return the normalised response."""
        ...

    async def resumable_session_for(
        self, user_id: int, working_directory: Path
    ) -> Optional[str]:
        """Session id this backend would resume for ``user_id`` in a dir."""
        ...

    async def sessions_for_user(self, user_id: int) -> List[BackendSession]:
        """Every session this backend knows for ``user_id``."""
        ...

    def attach_tools(self, services: Any, bridge: Any = None) -> None:
        """Wire the bot's agent tools, post-startup (they need the scheduler).

        ``services`` is the in-process ``AgentServices`` bundle; ``bridge`` is
        the loopback HTTP MCP server that exposes the same tools to
        out-of-process backends, or None when the bridge is disabled. Each
        backend takes what its transport can use and ignores the rest.
        """
        ...


class BackendPolicy(Protocol):
    """Static per-backend-id knowledge (no instance needed)."""

    backend_id: str
    label: str
    session_key: str

    def resolve_model(self, configured: Optional[str]) -> Optional[str]:
        """The ``model=`` kwarg for a turn given the stored preference.

        None means "let the backend apply its own default".
        """
        ...

    def display_model(self, configured: Optional[str], settings: "Settings") -> str:
        """What ``/status`` and ``/model`` print for the stored preference."""
        ...


class ClaudePolicy:
    backend_id = CLAUDE_BACKEND
    label = "claude"
    session_key = "claude_session_id"

    def resolve_model(self, configured: Optional[str]) -> Optional[str]:
        return str(configured) if configured in _CLAUDE_TIERS else None

    def display_model(self, configured: Optional[str], settings: "Settings") -> str:
        resolved = self.resolve_model(configured)
        if resolved is not None:
            return resolved
        return str(settings.claude_model or "CLI default")


class CodexPolicy:
    backend_id = CODEX_BACKEND
    label = "codex"
    session_key = "codex_session_id"

    def resolve_model(self, configured: Optional[str]) -> Optional[str]:
        model = str(configured or _CODEX_DEFAULT_MODEL)
        if model not in codex_supported_models():
            return _CODEX_DEFAULT_MODEL
        return model

    def display_model(self, configured: Optional[str], settings: "Settings") -> str:
        return str(self.resolve_model(configured))


BACKEND_POLICIES: Dict[str, BackendPolicy] = {
    CLAUDE_BACKEND: ClaudePolicy(),
    CODEX_BACKEND: CodexPolicy(),
    # A third backend (e.g. an OpenAI-compatible proxy) adds its policy here
    # and its instance to bot_data["backends"] in main.py.
}


def policy_for(backend_id: str) -> BackendPolicy:
    """Policy for ``backend_id``; unknown ids fall back to Claude's.

    The fallback matches the pre-registry behaviour, where every
    ``backend != "openai_codex"`` comparison took the Claude branch.
    """
    return BACKEND_POLICIES.get(backend_id, BACKEND_POLICIES[CLAUDE_BACKEND])


__all__ = [
    "AgentBackend",
    "BACKEND_POLICIES",
    "BackendPolicy",
    "BackendSession",
    "CLAUDE_BACKEND",
    "CODEX_BACKEND",
    "ClaudePolicy",
    "CodexPolicy",
    "codex_supported_models",
    "policy_for",
]
