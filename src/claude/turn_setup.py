"""Per-turn setup shared by the Claude SDK and Codex CLI backends (dv-a342).

Three things both backends used to compute separately, each copy carrying a
comment promising to stay in sync with the other:

* :func:`resolve_turn_context` — WHO this turn is for: profile slug, memory
  scope, family scope. The SDK path bakes it into the in-process ``agent``
  MCP server; the Codex path mints a bridge token from it. They must resolve
  identically, or a tool called from one backend reads a scope the other
  backend wrote (dv-6dbc).
* :func:`compose_persona` — WHAT text the model gets as its persona: the
  frozen per-window bundle (dv-5e2a) when there is one, else a live compose
  over the facts the retriever selects for this prompt. The backends differ
  only in *where* that text goes: Claude passes it as ``system_prompt``;
  Codex prepends it to the user prompt (its cwd ``AGENTS.md`` is
  human-authored and must not be clobbered). Both differences live in the
  callers, not here.
* :func:`image_roots_env` — the ``TELEGRAM_IMAGE_ROOTS`` value handed to the
  telegram MCP server so ``send_image_to_user`` refuses an out-of-bounds path
  itself instead of reporting "queued" for an image the bot's intercept then
  drops.

CLAUDE.md is deliberately not a layer of the composed text: the Claude CLI
loads it from the ``project`` setting source, and injecting it here put the
file in context twice (docs/FINDINGS.md 2026-08-04).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from .prompt_composer import PromptComposer
from .tool_context import TurnContext

if TYPE_CHECKING:
    from ..profiles.registry import ProfileRegistry
    from ..storage.memory_repository import MemoryRetriever
    from .window_bundle import WindowBundleProvider


def composer_for(
    registry: Optional["ProfileRegistry"],
    default: Optional[PromptComposer],
    cache: Dict[str, PromptComposer],
    working_directory: Path,
) -> Optional[PromptComposer]:
    """Pick the per-profile composer for a working dir, else ``default``.

    ``cache`` is the caller's per-profile-slug memo; a composer is built once
    per profile and reused so guest subdirectories share the root's instance.
    """
    if not registry:
        return default
    profile = registry.get_by_working_dir(working_directory)
    if not profile:
        return default
    cached = cache.get(profile.slug)
    if cached is None:
        cached = PromptComposer(
            memory_path=profile.memory_dir,
            agent_md_path=profile.persona,
        )
        cache[profile.slug] = cached
    return cached


def resolve_turn_context(
    registry: Optional["ProfileRegistry"],
    working_directory: Path,
    *,
    user_id: Optional[int] = None,
    requester_chat_id: Optional[int] = None,
    family_id: Optional[int] = None,
    role: Optional[str] = None,
) -> TurnContext:
    """Resolve this turn's identity server-side; the model never supplies it.

    ``profile_slug`` is the owner-tier scope of the profile at this working
    dir; ``memory_agent_id`` applies the per-user split (guest workspaces,
    ``personal_memory`` profiles) via ``agent_id_for_working_dir``, the same
    resolver the read path uses; ``family_agent_id`` is set only when the
    turn has a family. ``requester_chat_id`` / ``user_id`` / ``role`` pass
    through untouched.
    """
    from ..profiles.registry import agent_id_for_working_dir, family_agent_id_for_agent

    memory_agent_id = agent_id_for_working_dir(registry, working_directory, user_id)
    return TurnContext(
        profile_slug=agent_id_for_working_dir(registry, working_directory),
        working_dir=working_directory,
        requester_chat_id=requester_chat_id,
        memory_agent_id=memory_agent_id,
        family_agent_id=(
            family_agent_id_for_agent(memory_agent_id, family_id)
            if family_id is not None
            else None
        ),
        user_id=user_id,
        role=role,
    )


async def compose_persona(
    ctx: TurnContext,
    *,
    prompt: str,
    composer: Optional[PromptComposer],
    memory_retriever: Optional["MemoryRetriever"],
    bundle_provider: Optional["WindowBundleProvider"],
    family_id: Optional[int] = None,
    full_access: bool = False,
) -> Optional[str]:
    """Return the persona text for this turn, or None when nothing composes.

    Order of preference:

    1. The frozen per-window bundle (dv-5e2a) — one composition per window,
       byte-identical on every turn and on every backend until the next roll
       or ``/new``. Requires a ``user_id`` (the window key) and a provider.
    2. A live compose: the retriever selects facts for ``prompt`` under
       ``ctx.memory_agent_id`` (and ``ctx.family_agent_id`` when set), and the
       composer renders them. This is the only path for callers with no
       user_id and for deployments without the provider.
    3. None — no composer configured. The caller decides what that means
       (the SDK path substitutes its bare cwd constraint; Codex sends the
       prompt as-is).

    ``OSError`` from the composer (unreadable persona file) propagates; the
    caller owns the recovery, which differs per backend.
    """
    working_directory = ctx.working_dir
    if working_directory is None:
        raise ValueError("TurnContext.working_dir is required to compose a persona")
    if bundle_provider is not None and ctx.user_id is not None:
        bundle = await bundle_provider.get_or_materialize(
            working_directory=working_directory,
            user_id=ctx.user_id,
            family_id=family_id,
            full_access=full_access,
        )
        if bundle is not None:
            return bundle.text
    if not composer:
        return None
    if ctx.memory_agent_id is None:
        raise ValueError("TurnContext.memory_agent_id is required to compose a persona")
    pinned_facts: List[str] = []
    general_facts: List[str] = []
    family_facts: List[str] = []
    result_pointers: List[str] = []
    if memory_retriever:
        (
            pinned_facts,
            general_facts,
            result_pointers,
        ) = await memory_retriever.select(ctx.memory_agent_id, query=prompt)
        if ctx.family_agent_id is not None:
            family_facts = await memory_retriever.select_family(
                ctx.family_agent_id, query=prompt
            )
    return composer.compose(
        working_directory=working_directory,
        pinned_facts=pinned_facts,
        general_facts=general_facts,
        family_facts=family_facts or None,
        result_pointers=result_pointers or None,
        full_access=full_access,
    )


def current_time_context(tz_name: str) -> str:
    """One line stating 'now' in the configured local zone, for both backends.

    dv-ca94: an agent that only ever sees UTC (the Codex ``clock`` tool, the
    host's own ``Etc/UTC`` TZ) writes "17:00 today" as ``17:00Z`` and the
    reminder fires an hour late for a Lisbon user. Prepended to the PROMPT
    (never the persona/system prompt) so it is fresh every turn without
    invalidating the frozen per-window bundle (dv-5e2a) — the same reason
    ``_prepend_today_state`` lives on the prompt path, not here as a
    composed section.

    Falls back to UTC for an unknown/invalid zone name rather than raising —
    a bad ``USER_TIMEZONE`` must not break every turn.
    """
    from datetime import UTC, datetime
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        tz = ZoneInfo(tz_name or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        tz_name = "UTC"
        tz = ZoneInfo("UTC")
    now_local = datetime.now(tz)
    now_utc = datetime.now(UTC)
    offset = now_local.strftime("%z") or "+0000"
    offset_fmt = f"{offset[:3]}:{offset[3:]}"
    return (
        f"<current_time>Now: {now_local.strftime('%Y-%m-%d %H:%M')} local "
        f"({tz_name}, UTC{offset_fmt}); UTC: {now_utc.strftime('%Y-%m-%dT%H:%M')}Z. "
        "A bare date/time you give a scheduling tool (no UTC offset) is "
        "interpreted in this local zone, not UTC.</current_time>"
    )


async def prepend_items_context(
    items: Any,
    registry: Optional["ProfileRegistry"],
    prompt: str,
    working_directory: Path,
    user_id: Optional[int],
) -> str:
    """Prefix the ``<family_reminders>`` block (dv-752a), both backends.

    The family roster plus reminder activity since this user's previous turn
    in this agent: a reminder delivered here (so a bare reply to it is
    understood) and what happened to reminders this user set for others (so
    the creator's agent knows). Prompt, never the system prompt, for the
    same frozen-bundle reason as ``<current_time>``. Nothing is written into
    anyone's memory. Any failure degrades to the unchanged prompt.
    """
    if items is None or not user_id:
        return prompt
    try:
        from ..profiles.registry import agent_id_for_working_dir

        slug = agent_id_for_working_dir(registry, working_directory)
        block = await items.turn_context(int(user_id), slug)
    except Exception as exc:  # noqa: BLE001 — never fail a turn for context
        import structlog

        structlog.get_logger().warning(
            "family_reminders context skipped", user_id=user_id, error=str(exc)
        )
        return prompt
    return f"{block}\n\n{prompt}" if block else prompt


def image_roots_env(approved_directory: Any, working_directory: Path) -> str:
    """``TELEGRAM_IMAGE_ROOTS`` for this turn: deliverable roots, pathsep-joined.

    The roots are the deployment's approved directory (falling back to the
    turn's working dir) plus the session scratch dirs — sandboxed profiles
    write throwaway charts to /tmp, which is legitimate and must stay
    deliverable. See ``allowed_image_roots``.
    """
    # Lazy: src.bot imports src.claude.
    from ..bot.utils.image_extractor import allowed_image_roots

    return os.pathsep.join(
        str(root)
        for root in allowed_image_roots(approved_directory or working_directory)
    )
