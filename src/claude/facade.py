"""High-level Claude Code integration facade.

Provides simple interface for bot handlers.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Tuple

import structlog

from ..bot.window_seed import (
    COLD_START_TAIL_DAYS,
    COLD_START_TAIL_TURNS,
    SEED_TAIL_TURNS,
    SeedTail,
    build_seed_preamble,
    select_history_tail,
    select_tail,
)
from ..config.settings import Settings
from ..storage.repositories import MessageRepository
from .exceptions import (
    ClaudeMCPError,
    ClaudeParsingError,
    ClaudeStoppedError,
    ClaudeTimeoutError,
)

if TYPE_CHECKING:
    from ..activity.logger import ActivityLogger
    from ..agent_backends.protocol import BackendSession
    from ..daystate.source import TodayStateSource
    from ..evaluator.refresh import ContextRefreshService
    from ..evaluator.service import EvaluatorService
    from ..observability.lifecycle import LifecycleLogger
    from ..profiles.registry import ProfileRegistry
    from ..token.tracker import TokenTracker

    from .session import ClaudeSession
from .sdk_integration import (
    ClaudeResponse,
    ClaudeSDKManager,
    StreamUpdate,
    _project_jsonl_dir,
)
from .session import SessionManager

# Resume-turn failures that leave the backend transcript intact. The session
# mapping must survive these (see _classify_resume_failure).
_TRANSIENT_RESUME_ERRORS: Tuple[type, ...] = (
    ClaudeTimeoutError,
    ClaudeMCPError,
    ClaudeParsingError,
)

logger = structlog.get_logger()


def augment_prompt_with_images(prompt: str, images: Any) -> str:
    """Reference attached image files so Claude's Read tool can view them."""
    if not images:
        return prompt
    paths = "\n".join(f"- {p}" for p in images)
    return (
        f"{prompt}\n\n"
        "Attached image file(s) — use the Read tool to view them:\n"
        f"{paths}"
    )


class ClaudeIntegration:
    """Main integration point for Claude Code.

    Satisfies ``src.agent_backends.protocol.AgentBackend`` (dv-5368): the
    orchestrator and the info commands only call that surface.
    """

    backend_id = "claude"

    def __init__(
        self,
        config: Settings,
        sdk_manager: Optional[ClaudeSDKManager] = None,
        session_manager: Optional[SessionManager] = None,
        activity_logger: Optional["ActivityLogger"] = None,
        token_tracker: Optional["TokenTracker"] = None,
        evaluator_service: Optional["EvaluatorService"] = None,
        refresh_service: Optional["ContextRefreshService"] = None,
        profile_registry: Optional["ProfileRegistry"] = None,
        lifecycle_logger: Optional["LifecycleLogger"] = None,
        digest_provider: Optional["TodayStateSource"] = None,
    ):
        """Initialize Claude integration facade."""
        self.config = config
        self.sdk_manager = sdk_manager or ClaudeSDKManager(config)
        self.session_manager = session_manager
        self.activity_logger = activity_logger
        self.token_tracker = token_tracker
        self.evaluator_service = evaluator_service
        self.refresh_service = refresh_service
        self.profile_registry = profile_registry
        self.lifecycle_logger = lifecycle_logger
        self.digest_provider = digest_provider
        # Optional ItemService (dv-752a), attached in main: per-turn
        # <family_reminders> injection. None = no injection.
        self.items_service: Any = None
        # Every retirement inside the session manager (directory-cap eviction
        # included) becomes a lifecycle event, not just a log line (dv-2a5a).
        if hasattr(self.session_manager, "on_retire"):
            self.session_manager.on_retire = self._on_session_retired

    async def _emit_lifecycle(
        self, working_directory: Path, event: str, detail: Dict[str, Any]
    ) -> None:
        """Record a lifecycle event for the profile owning working_directory."""
        if not self.lifecycle_logger:
            return
        slug = "default"
        if self.profile_registry:
            p = self.profile_registry.get_by_working_dir(working_directory)
            if p:
                slug = p.slug
        await self.lifecycle_logger.record(profile=slug, event=event, detail=detail)

    async def _on_session_retired(
        self, session_id: str, reason: str, project_path: Optional[Path]
    ) -> None:
        """SessionManager.on_retire hook: durable trace of a lost mapping."""
        await self._emit_lifecycle(
            project_path if project_path is not None else Path("."),
            "session_retired",
            {"session": session_id, "reason": reason},
        )

    def _classify_resume_failure(
        self, error: BaseException, session_id: Optional[str], working_directory: Path
    ) -> Tuple[bool, str]:
        """Decide whether a failed resume turn means the session is DEAD.

        Returns ``(dead, kind)``. Only a dead session justifies deleting the
        resume mapping and retrying fresh; a transient failure must keep the
        mapping so the next message resumes the same conversation.

        Before dv-2a5a every exception was treated as dead. Observed
        2026-09-10 10:46 UTC: a 600s SDK timeout on a 60-turn devops session
        deleted the mapping, and the retry -- seeded with a 5-turn tail --
        was all the user got back. dv-fbf1 had exempted only /stop.

        * Timeout, MCP and stream-parsing errors are transient: the backend
          transcript is intact, the turn simply did not complete. Re-raise.
        * "No conversation found" from the CLI, or no transcript file on
          disk for this id, is definitive: the session is gone.
        * Anything else (generic process error with the transcript present)
          keeps the historical behaviour -- treat as dead -- so a poisoned
          transcript cannot wedge a profile into failing every turn. The kind
          is recorded so the choice is auditable.
        """
        if isinstance(error, _TRANSIENT_RESUME_ERRORS):
            return False, type(error).__name__
        if "no conversation found" in str(error).lower():
            return True, "transcript_missing"
        if session_id:
            try:
                transcript = (
                    _project_jsonl_dir(working_directory) / f"{session_id}.jsonl"
                )
                if not transcript.exists():
                    return True, "transcript_missing"
            except OSError:
                pass
        return True, "process_error"

    def _prepend_current_time(self, prompt: str) -> str:
        """Prefix the per-turn ``<current_time>`` line (dv-ca94).

        Unconditional (every profile, every turn) — unlike the today-state
        digest below, this has no ``profile.today_digest`` gate: any agent
        that reasons about "today"/"now" for scheduling needs the right
        zone, not just coach-style trackers.
        """
        from .turn_setup import current_time_context

        return f"{current_time_context(self.config.user_timezone)}\n\n{prompt}"

    async def _prepend_today_state(
        self, prompt: str, working_directory: Path, user_id: int
    ) -> str:
        """Prefix the per-turn <today_state> digest for flagged profiles.

        Injected here, at the facade, because every coach turn — Telegram
        text, media, scheduled job, webhook — flows through ``run_command``,
        while ``storage_prompt`` is assembled separately in the orchestrator.
        The digest therefore never reaches the messages table, the evaluator
        or a window reseed: it is turn-local by construction.

        Never injected into the system prompt: the window bundle is frozen
        byte-identical (dv-5e2a) and sits at position 0 of the cached prefix,
        so a per-turn change there would invalidate the whole context cache.

        Any failure degrades to the unmodified prompt — a digest must never
        cost the user a turn.
        """
        if not self.digest_provider or not self.profile_registry:
            return prompt
        try:
            profile = self.profile_registry.get_by_working_dir(working_directory)
            if profile is None or not profile.today_digest:
                return prompt

            from datetime import datetime
            from zoneinfo import ZoneInfo

            from ..daystate import render_digest

            now_local = datetime.now(ZoneInfo(self.config.user_timezone))
            state = await self.digest_provider.get_today_state(
                user_id, now_local.date().isoformat()
            )
            block = render_digest(state, now_local)
            if not block:
                return prompt
            logger.info(
                "today_state injected",
                user_id=user_id,
                profile=profile.slug,
                chars=len(block),
            )
            return f"{block}\n\n{prompt}"
        except Exception as exc:  # noqa: BLE001 — never fail a turn for a digest
            logger.warning(
                "today_state digest skipped", user_id=user_id, error=str(exc)
            )
            return prompt

    async def run_command(
        self,
        prompt: str,
        working_directory: Path,
        user_id: int,
        session_id: Optional[str] = None,
        on_stream: Optional[Callable[[StreamUpdate], None]] = None,
        force_new: bool = False,
        images: Optional[List["Path"]] = None,
        model: Optional[str] = None,
        requester_chat_id: Optional[int] = None,
        family_id: Optional[int] = None,
        role: Optional[str] = None,
    ) -> ClaudeResponse:
        """Run Claude Code command with full integration.

        ``requester_chat_id`` is the Telegram chat that initiated this turn;
        the in-process agent tools (notify/schedule_add) use it to target
        replies. When None (e.g. webhook-triggered runs), agent notifications
        fall back to the configured broadcast list.

        ``family_id`` is the requesting user's family (if any); it injects the
        shared family fact scope into prompt composition. None = no family.

        ``role`` gates the per-request SDK budget cap — "owner" removes it,
        anything else (including the default) keeps it. See
        ``ClaudeSDKManager.execute_command`` for the full rationale.
        """
        # Killswitch pre-flight (dv-fbf1): refuse before any session
        # bookkeeping. Without this gate the refusal raised deep inside
        # _execute was misclassified by the resume fallback below as a dead
        # session, which then DELETED the session mapping. `is True` because
        # tests hand in MagicMock SDK managers whose is_stopped is truthy.
        if self.sdk_manager.is_stopped is True:
            raise ClaudeStoppedError(
                "Bot is stopped via /stop. Send /resume to continue."
            )

        prompt = augment_prompt_with_images(prompt, images)
        prompt = self._prepend_current_time(prompt)
        prompt = await self._prepend_today_state(prompt, working_directory, user_id)
        from .turn_setup import prepend_items_context

        prompt = await prepend_items_context(
            self.items_service,
            self.profile_registry,
            prompt,
            working_directory,
            user_id,
        )
        # The retry-after-failed-resume path must seed from the raw prompt:
        # _heal_lost_prose may wrap `prompt` below, and seeding a wrapped
        # prompt would double-wrap <recent_conversation>.
        original_prompt = prompt
        logger.info(
            "Running Claude command",
            user_id=user_id,
            working_directory=str(working_directory),
            session_id=session_id,
            prompt_length=len(prompt),
            force_new=force_new,
        )

        # If no session_id provided, try to find an existing session for this
        # user+directory combination (auto-resume).
        # Skip auto-resume when force_new is set (e.g. after /new command).
        if not session_id and not force_new:
            existing_session = await self._find_resumable_session(
                user_id, working_directory
            )
            if existing_session:
                session_id = existing_session.session_id
                logger.info(
                    "Auto-resuming existing session for project",
                    session_id=session_id,
                    project_path=str(working_directory),
                    user_id=user_id,
                )
                await self._emit_lifecycle(
                    working_directory, "resume", {"session": session_id}
                )

        # Get or create session
        session = await self.session_manager.get_or_create_session(
            user_id, working_directory, session_id
        )

        # Execute command
        try:
            # Continue session if we have an existing session with a real ID
            is_new = getattr(session, "is_new_session", False)
            should_continue = not is_new and bool(session.session_id)

            # For new sessions, don't pass session_id to Claude Code
            claude_session_id = session.session_id if should_continue else None

            # Why THIS turn opened a fresh backend session, for the footer.
            # None on a resumed turn (dv-2a5a).
            fresh_reason: Optional[str] = None

            # Heal context if the resumed transcript dropped a prose-only turn.
            if should_continue and claude_session_id:
                prompt = await self._heal_lost_prose(
                    claude_session_id, working_directory, prompt
                )
            elif not force_new:
                # Cold start: nothing to resume for this workspace. Seed the
                # fresh session from the workspace's stored history so a new
                # session is never a blank slate the user cannot see (dv-300e).
                # `force_new` (/new) is excluded: that is an explicit reset.
                prompt, fresh_reason = await self._seed_cold_start(
                    user_id, working_directory, prompt
                )
            else:
                fresh_reason = "reset by /new"

            try:
                response = await self._execute(
                    prompt=prompt,
                    working_directory=working_directory,
                    session_id=claude_session_id,
                    continue_session=should_continue,
                    stream_callback=on_stream,
                    model=model,
                    requester_chat_id=requester_chat_id,
                    family_id=family_id,
                    user_id=user_id,
                    role=role,
                )
                if fresh_reason:
                    response.session_fresh_reason = fresh_reason
            except ClaudeStoppedError:
                # The /stop killswitch is a deliberate pre-flight refusal, not
                # evidence the session is dead — never let it fall into the
                # session-deleting fallback below (dv-fbf1: it did exactly
                # that and wiped a healthy session's mapping).
                raise
            except Exception as resume_error:
                # A failed resume turn is retried on a fresh session ONLY when
                # the session is actually gone. The CLI returns a generic
                # exit-code-1 for a missing transcript, so the classifier
                # checks the error text and the transcript file rather than
                # the exception type alone (dv-2a5a).
                if should_continue:
                    dead, failure_kind = self._classify_resume_failure(
                        resume_error, claude_session_id, working_directory
                    )
                    if not dead:
                        logger.warning(
                            "Resume turn failed transiently; keeping session",
                            session_id=claude_session_id,
                            failure_kind=failure_kind,
                            error=str(resume_error),
                        )
                        raise
                    logger.warning(
                        "Session resume failed, starting fresh session",
                        failed_session_id=claude_session_id,
                        failure_kind=failure_kind,
                        error=str(resume_error),
                    )
                    # Seed the retry with the failed session's stored tail
                    # (dv-b41f) — fetched BEFORE remove_session so the rows
                    # are still reachable whatever cascade rules apply. A
                    # storage hiccup here must never turn a recoverable retry
                    # into a hard failure: fall back to the raw prompt.
                    retry_prompt = original_prompt
                    seed_tail: Optional[SeedTail] = None
                    try:
                        if claude_session_id:
                            pairs = await self._recent_pairs(
                                claude_session_id, SEED_TAIL_TURNS
                            )
                            total = await self._count_session_messages(
                                claude_session_id
                            )
                            seed_tail = select_tail(pairs, total_turns=total)
                            retry_prompt = build_seed_preamble(
                                seed_tail, original_prompt, reason="resume_failed"
                            )
                    except Exception as seed_error:
                        logger.warning(
                            "Resume-failed seed build failed; retrying unseeded",
                            failed_session_id=claude_session_id,
                            error=str(seed_error),
                        )

                    # Clean up the stale session
                    await self.session_manager.remove_session(
                        session.session_id,
                        reason=f"resume_failed:{failure_kind}",
                        project_path=working_directory,
                    )
                    shown = seed_tail.shown_turns if seed_tail and seed_tail.text else 0
                    await self._emit_lifecycle(
                        working_directory,
                        "fresh",
                        {
                            "reason": "resume_failed",
                            "failure_kind": failure_kind,
                            "failed_session": claude_session_id,
                            "seeded": shown > 0,
                            "tail_messages": shown,
                        },
                    )

                    # Create a fresh session and retry
                    session = await self.session_manager.get_or_create_session(
                        user_id, working_directory
                    )
                    response = await self._execute(
                        prompt=retry_prompt,
                        working_directory=working_directory,
                        session_id=None,
                        continue_session=False,
                        stream_callback=on_stream,
                        model=model,
                        requester_chat_id=requester_chat_id,
                        family_id=family_id,
                        user_id=user_id,
                        role=role,
                    )
                    response.session_restarted = True
                    response.session_fresh_reason = (
                        f"resume failed ({failure_kind}), "
                        + (f"{shown} turns seeded" if shown else "unseeded")
                    )
                else:
                    raise

            # Update session (assigns real session_id for new sessions)
            await self.session_manager.update_session(session, response)

            # Record how much memory was injected this turn. With the frozen
            # window bundle (dv-5e2a) the truth is the bundle's metadata, not
            # a live re-read of the persona file -- after freezing, the live
            # file size can differ from what the prompt actually contains.
            if self.lifecycle_logger:
                provider = getattr(self.sdk_manager, "bundle_provider", None)
                meta = (
                    provider.last_meta(user_id, Path(working_directory))
                    if provider is not None
                    else None
                )
                if meta is not None:
                    await self._emit_lifecycle(
                        working_directory,
                        "memory_loaded",
                        {
                            "chars": meta.total_chars,
                            "sections": {"bundle": meta.total_chars},
                            "bundle": {
                                "window_id": meta.window_id,
                                "persona_chars": meta.persona_chars,
                                "facts_count": meta.facts_count,
                                "pointers_count": meta.pointers_count,
                                "family_count": meta.family_count,
                                "reused": meta.reused,
                            },
                        },
                    )
                else:
                    composer = self.sdk_manager._composer_for(Path(working_directory))
                    sizes = composer.get_section_sizes() if composer else {}
                    await self._emit_lifecycle(
                        working_directory,
                        "memory_loaded",
                        {"chars": sum(sizes.values()), "sections": sizes},
                    )

            # Record token usage
            if self.token_tracker and response.input_tokens > 0:
                try:
                    await self.token_tracker.record_turn(
                        session_id=session.session_id,
                        user_id=user_id,
                        input_tokens=response.input_tokens,
                        output_tokens=response.output_tokens,
                        cumulative_tokens=session.cumulative_tokens,
                        cost=response.cost_usd,
                    )
                except Exception as e:
                    logger.warning("Failed to record token usage", error=str(e))

            # Evaluator + compaction triggers now live in the orchestrator
            # (backend-agnostic window accounting). See BPC Plan 1/2.

            # Ensure response has the session's final ID
            response.session_id = session.session_id

            if not response.session_id:
                logger.warning(
                    "No session_id after execution; session cannot be resumed",
                    user_id=user_id,
                )

            logger.info(
                "Claude command completed",
                session_id=response.session_id,
                cost=response.cost,
                duration_ms=response.duration_ms,
                num_turns=response.num_turns,
                is_error=response.is_error,
            )

            if self.activity_logger:
                try:
                    await self.activity_logger.log_activity(
                        prompt=prompt,
                        response_content=response.content,
                        working_directory=working_directory,
                        num_turns=response.num_turns,
                    )
                except Exception:
                    logger.warning(
                        "Activity logging failed in run_command", exc_info=True
                    )

            return response

        except Exception as e:
            logger.error(
                "Claude command failed",
                error=str(e),
                user_id=user_id,
                session_id=session.session_id,
            )
            raise

    async def _execute(
        self,
        prompt: str,
        working_directory: Path,
        session_id: Optional[str] = None,
        continue_session: bool = False,
        stream_callback: Optional[Callable] = None,
        model: Optional[str] = None,
        requester_chat_id: Optional[int] = None,
        family_id: Optional[int] = None,
        user_id: Optional[int] = None,
        role: Optional[str] = None,
    ) -> ClaudeResponse:
        """Execute command via SDK."""
        return await self.sdk_manager.execute_command(
            prompt=prompt,
            working_directory=working_directory,
            session_id=session_id,
            continue_session=continue_session,
            stream_callback=stream_callback,
            model=model,
            requester_chat_id=requester_chat_id,
            family_id=family_id,
            user_id=user_id,
            role=role,
        )

    async def _find_resumable_session(
        self,
        user_id: int,
        working_directory: Path,
    ) -> Optional["ClaudeSession"]:  # noqa: F821
        """Find the most recent resumable session for a user in a directory.

        Returns the session if one exists with a real session ID from Claude.
        Returns None otherwise.

        **No wall-clock filter.** Conversation lifetime is owned by the window
        mechanism and measured in context size, not elapsed time (see
        WindowRepository.should_refresh / docs/DECISIONS.md 2026-09-10). This
        used to also require ``not s.is_expired(session_timeout_hours)``,
        which silently started a brand-new, unseeded session whenever a
        profile went unused longer than the timeout (12h under
        ProductionConfig). Observed 2026-09-10 on the `chef` profile: a 51h
        gap, session bfea649d dropped, the model answered from a blank
        context while /status still showed a healthy window. The two other
        fresh-session paths (window roll, resume failure) both carry a
        continuity seed; this one carried nothing, so it was pure loss.

        The outer bound is now the backend's own transcript retention: when
        the CLI has garbage-collected a transcript the resume raises, and
        run_command's resume-failure path retries fresh *with* a seeded tail.
        """

        sessions = await self.session_manager._get_user_sessions(user_id)

        matching_sessions = [
            s
            for s in sessions
            if s.project_path == working_directory and bool(s.session_id)
        ]

        if not matching_sessions:
            return None

        return max(matching_sessions, key=lambda s: s.last_used)

    # --- AgentBackend surface (src/agent_backends/protocol.py) ---

    def default_model(self) -> str:
        """Model a turn runs on with ``model=None``: config, else the CLI's."""
        return str(self.config.claude_model or "CLI default")

    async def resumable_session_for(
        self, user_id: int, working_directory: Path
    ) -> Optional[str]:
        """Public form of :meth:`_find_resumable_session`: the id only."""
        existing = await self._find_resumable_session(user_id, working_directory)
        return existing.session_id if existing else None

    async def sessions_for_user(self, user_id: int) -> List["BackendSession"]:
        """Every session row for ``user_id`` as backend-neutral records."""
        from ..agent_backends.protocol import BackendSession

        if self.session_manager is None:
            return []
        sessions = await self.session_manager._get_user_sessions(user_id)
        return [
            BackendSession(
                session_id=s.session_id,
                working_dir=Path(str(s.project_path)),
                last_used=s.last_used,
                cumulative_tokens=s.cumulative_tokens,
                backend_id=self.backend_id,
            )
            for s in sessions
        ]

    def attach_tools(self, services: Any, bridge: Any = None) -> None:
        """In-process MCP tools; the HTTP bridge is for out-of-process backends."""
        self.sdk_manager.attach_agent_services(services)

    # Cap for the heal recap prepended on a lossy resume (~few k tokens).
    _HEAL_MAX_CHARS = 24_000

    async def _recent_pairs(self, session_id: str, limit: int) -> List[Tuple[str, str]]:
        """Last `limit` stored (prompt, response) pairs for a session,
        oldest-first. SQLite keeps these even when the backend transcript is
        gone — both heal and the resume-failed seed read from here."""
        messages = MessageRepository(self.session_manager.storage.db_manager)
        return await messages.get_recent_pairs(session_id, limit)

    async def _recent_pairs_for_dir(
        self, user_id: int, working_directory: Path, limit: int, days: int
    ) -> List[Tuple[str, str]]:
        """Last `limit` stored (prompt, response) pairs for a WORKSPACE within
        the last `days`, oldest-first, across every session that workspace had.

        Session-scoped history (`_recent_pairs`) is the wrong unit for a cold
        start: what the user calls "the conversation" is the workspace, and
        before dv-300e a profile's week could be spread over half a dozen
        session rows. Timestamps are stored as ISO-8601 UTC strings, so the
        cutoff compares lexicographically.
        """
        cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        messages = MessageRepository(self.session_manager.storage.db_manager)
        return await messages.get_recent_pairs_for_dir(
            user_id, str(working_directory), cutoff, limit
        )

    async def _count_dir_messages(
        self, user_id: int, working_directory: Path, days: int
    ) -> Optional[int]:
        """Total stored turns for a workspace inside the cold-start window,
        for the seed's honest 'last N of TOTAL' label. None on any error."""
        cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        try:
            messages = MessageRepository(self.session_manager.storage.db_manager)
            return await messages.count_dir_messages(
                user_id, str(working_directory), cutoff
            )
        except Exception:
            return None

    async def _seed_cold_start(
        self, user_id: int, working_directory: Path, prompt: str
    ) -> Tuple[str, str]:
        """Wrap `prompt` with the workspace's recent stored history.

        Returns ``(prompt, reason)`` where ``reason`` is the footer text for
        this fresh session. The prompt comes back unchanged when there is no
        history, when something already wrapped it, or on any storage error
        — a seed is an improvement, never a reason to fail the turn.
        """
        if "<recent_conversation>" in prompt:
            return prompt, "cold start, pre-seeded"
        try:
            pairs = await self._recent_pairs_for_dir(
                user_id, working_directory, COLD_START_TAIL_TURNS, COLD_START_TAIL_DAYS
            )
            if not pairs:
                return prompt, "cold start, no history"
            total = await self._count_dir_messages(
                user_id, working_directory, COLD_START_TAIL_DAYS
            )
            tail = select_history_tail(pairs, total_turns=total)
            if not tail.text:
                return prompt, "cold start, no history"
            seeded = build_seed_preamble(tail, prompt, reason="cold_start")
        except Exception as seed_error:
            logger.warning(
                "Cold-start seed build failed; starting unseeded",
                working_directory=str(working_directory),
                error=str(seed_error),
            )
            return prompt, "cold start, seed failed"

        logger.info(
            "Cold-start seed built",
            working_directory=str(working_directory),
            shown_turns=tail.shown_turns,
            total_turns=tail.total_turns,
            seed_chars=len(tail.text),
        )
        await self._emit_lifecycle(
            working_directory,
            "fresh",
            {
                "reason": "cold_start",
                "seeded": True,
                "tail_messages": tail.shown_turns,
                "window_turns": tail.total_turns,
            },
        )
        return seeded, f"cold start, {tail.shown_turns} turns seeded"

    async def _count_session_messages(self, session_id: str) -> Optional[int]:
        """Total stored turns for a session, for the seed's honest
        'last N of TOTAL' label. None on any error — the label degrades."""
        try:
            messages = MessageRepository(self.session_manager.storage.db_manager)
            return await messages.count_session_messages(session_id)
        except Exception:
            return None

    async def _heal_lost_prose(
        self,
        session_id: str,
        working_directory: Path,
        prompt: str,
    ) -> str:
        """If the resumed transcript dropped a pure-prose assistant turn, prepend
        a recap of recent stored responses (which SQLite kept correctly) so the
        model regains continuity. Resume stays on; tool-driven turns don't trip
        the detector, so tool-heavy personas are unaffected. Returns the prompt
        unchanged when there's nothing to heal."""
        if not getattr(self.config, "heal_lost_context", True):
            return prompt
        # A transition seed already wrapped recent context — don't double-wrap.
        if "<recent_conversation>" in prompt:
            return prompt

        from src.claude.sdk_integration import transcript_lost_prose_turns

        lost = transcript_lost_prose_turns(session_id, working_directory)
        if not lost:
            return prompt

        pairs = await self._recent_pairs(session_id, 8)
        if not pairs:
            return prompt

        blocks: List[str] = []
        for p, resp in pairs:
            block = f"User: {p}"
            if resp:
                block += f"\nAssistant: {resp}"
            blocks.append(block)
        while blocks and len("\n\n".join(blocks)) > self._HEAL_MAX_CHARS:
            blocks.pop(0)
        if not blocks:
            return prompt
        tail = "\n\n".join(blocks)

        logger.info(
            "Healing lost prose context from storage",
            session_id=session_id,
            lost_turns=lost,
            turns_seeded=len(blocks),
        )
        return (
            "<recent_conversation>\n"
            f"{tail}\n"
            "</recent_conversation>\n\n"
            "Continue the conversation above. The user now says:\n\n"
            f"{prompt}"
        )

    async def _get_conversation_for_eval(
        self, session: "ClaudeSession"
    ) -> str:  # noqa: F821
        """Get conversation text for evaluator extraction."""
        # dv-5e4f: this used to reach `self.session_manager.storage._db`,
        # an attribute that does not exist on SQLiteSessionStorage (only
        # `.db_manager` does) -- a live AttributeError bug, caught while
        # moving this SQL into the repository layer.
        messages = MessageRepository(self.session_manager.storage.db_manager)
        pairs = await messages.get_conversation_chronological(session.session_id)

        parts: List[str] = []
        for prompt, response in pairs:
            if prompt:
                parts.append(f"User: {prompt}")
            if response:
                parts.append(f"Assistant: {response}")
        return "\n\n".join(parts)

    async def continue_session(
        self,
        user_id: int,
        working_directory: Path,
        prompt: Optional[str] = None,
        on_stream: Optional[Callable[[StreamUpdate], None]] = None,
    ) -> Optional[ClaudeResponse]:
        """Continue the most recent session."""
        logger.info(
            "Continuing session",
            user_id=user_id,
            working_directory=str(working_directory),
            has_prompt=bool(prompt),
        )

        # Get user's sessions
        sessions = await self.session_manager._get_user_sessions(user_id)

        # Find most recent session in this directory (exclude sessions without IDs)
        matching_sessions = [
            s
            for s in sessions
            if s.project_path == working_directory and bool(s.session_id)
        ]

        if not matching_sessions:
            logger.info("No matching sessions found", user_id=user_id)
            return None

        # Get most recent
        latest_session = max(matching_sessions, key=lambda s: s.last_used)

        # Continue session with default prompt if none provided
        # Claude CLI requires a prompt, so we use a placeholder
        return await self.run_command(
            prompt=prompt or "Please continue where we left off",
            working_directory=working_directory,
            user_id=user_id,
            session_id=latest_session.session_id,
            on_stream=on_stream,
        )

    async def get_session_info(
        self, session_id: str, user_id: int
    ) -> Optional[Dict[str, Any]]:
        """Get session information (scoped to requesting user)."""
        return await self.session_manager.get_session_info(session_id, user_id)

    async def get_user_sessions(self, user_id: int) -> List[Dict[str, Any]]:
        """Get all sessions for a user."""
        sessions = await self.session_manager._get_user_sessions(user_id)
        return [
            {
                "session_id": s.session_id,
                "project_path": str(s.project_path),
                "created_at": s.created_at.isoformat(),
                "last_used": s.last_used.isoformat(),
                "total_cost": s.total_cost,
                "message_count": s.message_count,
                "tools_used": s.tools_used,
            }
            for s in sessions
        ]

    async def get_user_summary(self, user_id: int) -> Dict[str, Any]:
        """Get comprehensive user summary."""
        session_summary = await self.session_manager.get_user_session_summary(user_id)

        return {
            "user_id": user_id,
            **session_summary,
        }

    async def shutdown(self) -> None:
        """Shutdown integration and cleanup resources."""
        logger.info("Shutting down Claude integration")

        # Deliberately no session cleanup here: shutdown must not destroy
        # resume mappings, or every restart costs the user context (see
        # session.py, cleanup_expired_sessions removal note).

        logger.info("Claude integration shutdown complete")
