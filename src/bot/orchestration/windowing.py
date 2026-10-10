"""Conversation window accounting: turn recording, evaluator checkpoints,
window rotation and continuity-seed transitions.

Extracted verbatim from src/bot/orchestrator.py (behavior-preserving move).
"""

import asyncio
import re
from pathlib import Path
from typing import Any, Optional

import structlog

from ...profiles.registry import ProfileRegistry
from ...storage.repositories import MessageRepository

logger = structlog.get_logger()

_CODEX_BACKEND = "openai_codex"
_CODEX_REFRESH_WINDOW_FRACTION = 0.80


# Phrases that mean "save this durably" — forces an off-cycle evaluator run
# instead of waiting for the token checkpoint (WINDOW_EVAL_INTERVAL_TOKENS).
# False positives just cost one extra evaluator invocation; false negatives
# silently drop the fact, so this stays broad on purpose. Cyrillic \w is
# unicode-aware by default in Python 3's re module, so \b works fine on
# "запомни" etc.
_REMEMBER_TRIGGER_RE = re.compile(
    r"запомн\w*|не забудь\w*|\bremember\b|\bdon'?t forget\b|\bkeep in mind\b",
    re.IGNORECASE,
)


def mentions_remember_trigger(text: Optional[str]) -> bool:
    """True if the user's message explicitly asked to remember something."""
    return bool(text) and bool(_REMEMBER_TRIGGER_RE.search(text))


def _context_tokens_of(response: Any) -> Optional[int]:
    """Real context this turn, or None if the backend reported nothing.

    Returning None on an all-zero response matters: a failed/aborted turn
    reports no usage, and writing 0 as the level would make the window look
    empty and silently reset the rotation clock. Both backends report a real
    level (Claude from per-call usage, Codex from its rollout file); when one
    cannot this turn, None tells the repository to hold the last value.
    """
    tokens = getattr(response, "context_tokens", None)
    if tokens is None:
        # Non-ClaudeResponse backend object: best effort from the flat fields.
        tokens = getattr(response, "input_tokens", 0) or 0
    return int(tokens) if tokens else None


def effective_refresh_threshold(
    configured_threshold: int,
    *,
    backend: str,
    model_ref: Optional[str] = None,
) -> int:
    """Clamp a Codex roll threshold to the selected model's context window.

    The catalog reports 272k for current Codex models while rollouts expose
    about 258.4k usable input.  Rotating at 80% leaves room for the response
    and one unexpectedly large tool result.  Claude keeps the operator's
    configured threshold because its model metadata comes from another
    transport and is not represented in the Codex catalog.
    """
    configured = int(configured_threshold)
    if backend != _CODEX_BACKEND or not model_ref:
        return configured

    from ...agent_backends.codex_catalog import CODEX_CATALOG

    model = next(
        (item for item in CODEX_CATALOG.cached() if item.ref == model_ref), None
    )
    if model is None or model.context_window <= 0:
        return configured
    model_safe = max(20_000, int(model.context_window * _CODEX_REFRESH_WINDOW_FRACTION))
    return min(configured, model_safe)


async def account_turn_and_maybe_eval(
    *,
    window_repo: Any,
    evaluator: Any,
    user_id: int,
    working_dir: Any,
    backend: str,
    added_tokens: int,
    conversation_text: str,
    max_message_id: int,
    agent_id: str = "devops",
    family_id: Optional[int] = None,
    force_eval: bool = False,
    context_tokens: Optional[int] = None,
    refresh_threshold_tokens: Optional[int] = None,
) -> Any:
    """Record a turn against the conversation window and, at each
    eval-interval checkpoint (``WINDOW_EVAL_INTERVAL_TOKENS``), trigger the
    evaluator on the *active* backend. Returns the updated WindowState.
    ``agent_id`` is the active profile's memory scope so facts land under the
    right agent (default 'devops'). ``family_id`` (the speaking user's
    family, if any) lets the evaluator scope explicitly family-framed facts
    to the shared family memory. ``force_eval`` bypasses the token checkpoint
    (e.g. the user explicitly said "remember ...") so a short conversation
    doesn't lose a fact waiting for the next checkpoint.

    ``context_tokens`` is the turn's REAL context size (input + cache_read +
    cache_creation); it drives the eval/refresh thresholds. None means the
    backend can't report it and the repo falls back to the flow sum."""
    state = await window_repo.record_turn(
        user_id,
        working_dir,
        backend=backend,
        added_tokens=added_tokens,
        context_tokens=context_tokens,
    )
    # A turn that ALSO rolls the window must not extract twice. The caller runs
    # refresh_window() right after this, and that path fires its own extraction
    # over the identical delta (both read from last_checkpoint_message_id, which
    # only advances once an extraction completes). Observed 2026-08-04: every
    # duplicate was a `...:{window_id}` / `...:refresh` pair with the SAME
    # prompt_hash seconds apart -- 14 of 66 extractions, 21% pure waste, and two
    # "Memory updated" pings for one turn. The refresh run is the one to keep:
    # its facts feed the continuity seed the next window opens on.
    will_refresh = window_repo.should_refresh(state, refresh_threshold_tokens)
    if (
        evaluator
        and not will_refresh
        and (force_eval or window_repo.should_run_evaluator(state))
    ):

        async def _run_and_checkpoint() -> None:
            # Only advance the "already processed" cursor once the evaluator
            # actually completes successfully. Advancing it eagerly (the old
            # behavior) meant a crashed/errored run silently threw away that
            # slice of conversation forever — the next checkpoint would never
            # re-send it, so a fact mentioned only in a failed turn was lost
            # even though the user was told "remembered."
            # should_run_evaluator gates on last_context_tokens (a LEVEL), not
            # cumulative_tokens (a flow SUM, logged separately by the CLI call
            # below) -- log the level here or "why did this fire" is unanswerable.
            logger.info(
                "Evaluator gate crossed",
                window_id=state.window_id,
                last_context_tokens=state.last_context_tokens,
                eval_interval_tokens=window_repo.eval_interval_tokens,
                last_eval_checkpoint=state.last_eval_checkpoint,
                force_eval=force_eval,
            )
            ok = await evaluator.run(
                session_id=f"{user_id}:{working_dir}:{state.window_id}",
                user_id=user_id,
                conversation_text=conversation_text,
                cumulative_tokens=state.cumulative_tokens,
                backend=backend,
                agent_id=agent_id,
                family_id=family_id,
                # Slice boundary (dv-c5d9 step 1): facts the `remember` tool
                # saved after this message id count as "already saved" in the
                # run's report, not as duplicates.
                since_message_id=state.last_checkpoint_message_id,
            )
            if ok:
                await window_repo.mark_evaluator_run(
                    user_id, working_dir, max_message_id
                )

        asyncio.create_task(_run_and_checkpoint())
    return state


SEED_NOTES_FACT_LIMIT = 15


async def _session_notes_for_seed(memory_repo: Any, agent_id: str) -> str:
    """Two labeled sub-blocks for the seed's session-notes: dated user-fact
    lines and dated conversation_result pointers, rendered via the same
    helpers as the window bundle so the dv-8254 taxonomy cannot drift between
    the two surfaces (plan decision 8). The fact lines deliberately duplicate
    the bundle's snapshot — belt-and-braces at the highest-risk moment (owner
    default D8, revisit with dv-e574's measurement). conversation_result
    content never appears; pointers only.

    The roll's final evaluator run lands its facts just before the next turn
    consumes the seed, so 'freshest' is exactly 'what the outgoing window was
    about'."""
    if not memory_repo or not agent_id:
        return ""
    from src.storage.memory_repository import render_fact_line, render_pointer_line

    try:
        snapshot = await memory_repo.facts_snapshot(agent_id)
    except Exception as e:
        logger.warning("Seed session notes unavailable", error=str(e))
        return ""
    # Conflict-pair members are excluded: the bundle renders them adjacently
    # under an explicit warning; repeating one side here without that flag
    # would present an unresolved conflict as settled truth.
    conflict_ids = {fact.id for pair in snapshot.conflicts for fact in pair}
    facts = [f for f in snapshot.pinned + snapshot.general if f.id not in conflict_ids]
    fact_lines = [f"- {render_fact_line(f)}" for f in facts[:SEED_NOTES_FACT_LIMIT]]
    pointer_lines = [f"- {render_pointer_line(p)}" for p in snapshot.pointers]
    sections: list[str] = []
    if fact_lines:
        sections.append(
            "About the user (dated [DD.MM.YY] = when each became true):\n"
            + "\n".join(fact_lines)
        )
    if pointer_lines:
        sections.append(
            "Your own earlier conclusions (pointers, not content — retrieve "
            "before relying on them):\n" + "\n".join(pointer_lines)
        )
    return "\n".join(sections)


async def consume_pending_seed(
    window_repo: Any,
    user_data: dict,
    *,
    user_id: int,
    working_dir: Any,
    user_message: str,
    memory_repo: Any = None,
    agent_id: str = "",
) -> str:
    """If a transition left a pending seed, build the seed preamble from the
    old window's tail + freshest extracted facts and return
    prompt = preamble+message. Otherwise return the message unchanged.
    Clears the flag.

    The tail is the last SEED_TAIL_TURNS turns of the outgoing window, NOT
    "everything since the evaluator checkpoint". The old checkpoint-bounded
    version produced an empty seed on *every* compaction: rotation fires at
    150k and the evaluator at each 50k, and 150k is an exact multiple of 50k,
    so the two always coincide on the same turn. mark_evaluator_run then set
    the checkpoint to that final message id, leaving "messages after the
    checkpoint" empty by construction — so the fresh session started with no
    context at all and the agent answered "I don't know what you're
    referring to" to a direct follow-up.
    """
    from src.bot.window_seed import SEED_TAIL_TURNS, build_seed_preamble, select_tail

    if not window_repo:
        return user_message
    # The DB is the source of truth (survives restarts); user_data is only
    # read as a fallback for a transition armed by an older build still in
    # flight when this shipped.
    state = await window_repo.get_or_create(user_id, working_dir)
    old_window = state.pending_seed_window or user_data.pop("pending_seed_window", None)
    user_data.pop("pending_seed_window", None)
    if not old_window:
        return user_message
    if state.pending_seed_window == state.window_id:
        # Armed (in the DB) to seed from the window still in progress: a
        # backend switch (dv-168e, see arm_backend_switch). Nothing was
        # rolled, so there is no context to reclaim -- rebuild the WHOLE
        # window for the fresh session on the new backend. A roll always
        # arms the PREVIOUS window id, so the two modes cannot collide; the
        # legacy user_data fallback only ever carried roll seeds.
        return await _full_window_seed(
            window_repo,
            user_id=user_id,
            working_dir=working_dir,
            window_id=old_window,
            user_message=user_message,
        )
    rows = await window_repo.tail_messages(
        user_id, working_dir, window_id=old_window, limit=SEED_TAIL_TURNS
    )
    # Honest labelling needs the outgoing window's REAL turn count ("last 5
    # of 47"), not just the fetched tail. Guarded: an older repo (or a test
    # double) without the counter degrades to an unnumbered label, never an
    # invented total.
    total_turns = None
    counter = getattr(window_repo, "count_window_messages", None)
    if counter is not None:
        try:
            total_turns = await counter(user_id, working_dir, window_id=old_window)
        except Exception as e:
            logger.warning("Seed turn count unavailable", error=str(e))
    tail = select_tail(rows, total_turns=total_turns)
    notes = await _session_notes_for_seed(memory_repo, agent_id)
    return build_seed_preamble(
        tail=tail, user_message=user_message, session_notes=notes
    )


async def _full_window_seed(
    window_repo: Any,
    *,
    user_id: int,
    working_dir: Any,
    window_id: int,
    user_message: str,
) -> str:
    """Every stored turn of the current window (plus a tool-names line per
    turn) under a ``backend_switch`` header. The only bound is the window's own rotation threshold; the char
    backstop is sized to it (est. 4 chars/token, generous for anything but
    pathological text) and its firing is logged, never silent."""
    from src.bot.window_seed import build_seed_preamble, select_full_window

    turns = await window_repo.window_turns_with_tools(
        user_id, working_dir, window_id=window_id
    )
    max_chars = int(window_repo.refresh_threshold_tokens) * 4
    tail = select_full_window(turns, max_chars=max_chars)
    if tail.truncated:
        logger.warning(
            "Backend-switch rebuild hit the window-size backstop",
            user_id=user_id,
            working_dir=str(working_dir),
            window_id=window_id,
            total_turns=tail.total_turns,
            shown_turns=tail.shown_turns,
            max_chars=max_chars,
        )
    logger.info(
        "Backend-switch rebuild seeded",
        user_id=user_id,
        window_id=window_id,
        turns=tail.shown_turns,
        chars=len(tail.text),
    )
    return build_seed_preamble(
        tail=tail, user_message=user_message, reason="backend_switch"
    )


async def arm_backend_switch(
    window_repo: Any,
    user_data: dict,
    *,
    user_id: int,
    working_dir: Any,
) -> None:
    """Backend switch (dv-168e): NO roll, no extraction -- same window id,
    the token counter carries on. Arms the same durable transition a roll
    does, but seeded from the CURRENT window, which ``consume_pending_seed``
    reads as "rebuild this window in full". The new backend's native session
    (if any) is stale -- it lacks the other model's turns -- so force_new is
    armed too, in ``conversation_windows`` so a restart before the next
    message cannot drop it (dv-9f31/dv-a826)."""
    state = await window_repo.get_or_create(user_id, working_dir)
    await window_repo.set_pending_transition(
        user_id, working_dir, seed_window=state.window_id, force_new_session=True
    )
    user_data["pending_seed_window"] = state.window_id
    user_data["force_new_session"] = True


async def refresh_window(
    window_repo: Any,
    evaluator: Any,
    user_data: dict,
    *,
    user_id: int,
    working_dir: Any,
    backend: str,
    conversation_text: str,
    agent_id: str = "devops",
    family_id: Optional[int] = None,
) -> None:
    """Compaction transition: final eval on the active backend, roll the
    window, and arm the seed for the next turn. (A backend switch no longer
    rolls -- see ``arm_backend_switch``.)

    Runs strictly BETWEEN turns — the caller invokes it after the current
    turn's response is complete, so a long execution is never cut mid-flight
    even if it blew far past the threshold; the roll just happens once it is
    done.
    """
    # State is fetched BEFORE the evaluator task is created so the run sees
    # the outgoing window's checkpoint cursor (its slice boundary), not
    # whatever the roll below writes.
    state = await window_repo.get_or_create(user_id, working_dir)
    if evaluator:
        asyncio.create_task(
            evaluator.run(
                session_id=f"{user_id}:{working_dir}:refresh",
                user_id=user_id,
                conversation_text=conversation_text,
                cumulative_tokens=0,
                backend=backend,
                agent_id=agent_id,
                family_id=family_id,
                since_message_id=state.last_checkpoint_message_id,
            )
        )
    # Estimate what the continuity seed will occupy in the fresh window so
    # the token counter starts there instead of lying with "0/150k". Uses the
    # same select_tail as consume_pending_seed so estimate == actual seed.
    from src.bot.window_seed import SEED_TAIL_TURNS, select_tail

    rows = await window_repo.tail_messages(
        user_id, working_dir, window_id=state.window_id, limit=SEED_TAIL_TURNS
    )
    seed_estimate = len(select_tail(rows).text) // 4

    old, _new = await window_repo.roll_window(
        user_id, working_dir, new_backend=backend, initial_tokens=seed_estimate
    )
    # Persist the intent BEFORE mirroring it into user_data. If the process
    # dies between the roll and the user's next message, the DB still knows a
    # fresh session is owed; user_data alone did not, and a restart on
    # 2026-08-02 silently resumed the retired session (dv-9f31).
    await window_repo.set_pending_transition(
        user_id, working_dir, seed_window=old, force_new_session=True
    )
    user_data["pending_seed_window"] = old
    user_data["force_new_session"] = True


async def window_conversation_text(
    storage: Any, user_id: int, working_dir: Any, state: Any
) -> str:
    """Concatenate prompts/responses of the current window since the last
    evaluator checkpoint (bounded by message_id).

    Joined to sessions and filtered on project_path — window_id alone is
    only unique within a (user_id, working_dir) pair (every profile's
    counter independently starts at 1), so a bare window_id filter can
    pull another profile's messages into this window's conversation text
    (and from there into the memory evaluator / rotation seed).
    """
    if not storage:
        return ""
    wd = str(Path(working_dir))
    messages = MessageRepository(storage.db_manager)
    rows = await messages.get_window_messages(
        user_id, wd, state.window_id, state.last_checkpoint_message_id
    )
    parts: list[str] = []
    for prompt, response in rows:
        # Guarded like the facade version: persisted system notifications
        # (dv-c5d9 step 5) have an empty prompt and must not render a
        # blank "User:" line into the evaluator's conversation text.
        if prompt:
            parts.append(f"User: {prompt}")
        if response:
            parts.append(f"Assistant: {response}")
    return "\n\n".join(parts)


async def reset_window(
    window_repo: Any,
    evaluator: Any,
    user_data: dict,
    *,
    user_id: int,
    working_dir: Any,
    backend: str,
    conversation_text: str,
    agent_id: str = "devops",
    family_id: Optional[int] = None,
) -> None:
    """/new — explicit user reset. Same data care as ``refresh_window``
    (final extraction over the un-extracted tail, then a window roll so the
    next turn materializes a fresh bundle) but deliberately NO continuity
    seed: the user asked for a clean conversation break (dv-d9e2 path 3).

    The pending transition is still armed durably (seed_window=None,
    force_new_session=True): that both survives a restart before the next
    message (dv-9f31 class) and clears any previously armed *seeded*
    transition, which would otherwise leak the old window's tail into the
    session the user just asked to be clean.
    """
    state = await window_repo.get_or_create(user_id, working_dir)
    if evaluator and conversation_text.strip():
        asyncio.create_task(
            evaluator.run(
                session_id=f"{user_id}:{working_dir}:reset",
                user_id=user_id,
                conversation_text=conversation_text,
                cumulative_tokens=0,
                backend=backend,
                agent_id=agent_id,
                family_id=family_id,
                since_message_id=state.last_checkpoint_message_id,
            )
        )
    await window_repo.roll_window(user_id, working_dir, new_backend=backend)
    await window_repo.set_pending_transition(
        user_id, working_dir, seed_window=None, force_new_session=True
    )
    user_data.pop("pending_seed_window", None)
    user_data["force_new_session"] = True


def apply_window_tokens(
    registry: ProfileRegistry, last_by_dir: dict, windows: list
) -> None:
    """Overlay authoritative token counts from conversation windows.

    Session records keep cumulative_tokens at 0 for Claude sessions; the
    running counter lives in conversation_windows (WindowRepository). Mutates
    last_by_dir in place for every profile that has a window row.

    Carries both numbers: ``cumulative_tokens`` (flow — everything this window
    has processed) and ``context_tokens`` (level — how big the conversation is
    right now, which is what the roll threshold reads). /sessions shows the
    level, because that is the one a user can act on.
    """
    by_dir = {Path(w.working_dir): w for w in windows}
    for p in registry.profiles:
        window = by_dir.get(p.working_dir)
        info = last_by_dir.get(p.working_dir)
        if window is not None and info is not None:
            info["cumulative_tokens"] = window.cumulative_tokens
            info["context_tokens"] = window.last_context_tokens
