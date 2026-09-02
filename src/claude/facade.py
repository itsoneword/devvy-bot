"""High-level Claude Code integration facade.

Provides simple interface for bot handlers.
"""

from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Tuple

import aiosqlite
import structlog

from ..bot.window_seed import (
    SEED_TAIL_TURNS,
    SeedTail,
    build_seed_preamble,
    select_tail,
)
from ..config.settings import Settings
from .exceptions import ClaudeStoppedError

if TYPE_CHECKING:
    from ..activity.logger import ActivityLogger
    from ..daystate.source import TodayStateSource
    from ..evaluator.refresh import ContextRefreshService
    from ..evaluator.service import EvaluatorService
    from ..observability.lifecycle import LifecycleLogger
    from ..profiles.registry import ProfileRegistry
    from ..token.tracker import TokenTracker

    from .session import ClaudeSession
from .sdk_integration import ClaudeResponse, ClaudeSDKManager, StreamUpdate
from .session import SessionManager

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
    """Main integration point for Claude Code."""

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
        prompt = await self._prepend_today_state(prompt, working_directory, user_id)
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

            # Heal context if the resumed transcript dropped a prose-only turn.
            if should_continue and claude_session_id:
                prompt = await self._heal_lost_prose(
                    claude_session_id, working_directory, prompt
                )

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
            except ClaudeStoppedError:
                # The /stop killswitch is a deliberate pre-flight refusal, not
                # evidence the session is dead — never let it fall into the
                # session-deleting fallback below (dv-fbf1: it did exactly
                # that and wiped a healthy session's mapping).
                raise
            except Exception as resume_error:
                # If resume failed (e.g., session expired/missing on Claude's side),
                # retry as a fresh session.  The CLI returns a generic exit-code-1
                # when the session is gone, so we catch *any* error during resume.
                if should_continue:
                    logger.warning(
                        "Session resume failed, starting fresh session",
                        failed_session_id=claude_session_id,
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
                    await self.session_manager.remove_session(session.session_id)
                    await self._emit_lifecycle(
                        working_directory,
                        "fresh",
                        {
                            "reason": "resume_failed",
                            "failed_session": claude_session_id,
                            "seeded": bool(seed_tail.text) if seed_tail else False,
                            "tail_messages": (
                                seed_tail.shown_turns if seed_tail else 0
                            ),
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
                        cost=response.cost,
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

        Returns the session if one exists that is non-expired and has a real
        (non-temporary) session ID from Claude. Returns None otherwise.
        """

        sessions = await self.session_manager._get_user_sessions(user_id)

        matching_sessions = [
            s
            for s in sessions
            if s.project_path == working_directory
            and bool(s.session_id)
            and not s.is_expired(self.config.session_timeout_hours)
        ]

        if not matching_sessions:
            return None

        return max(matching_sessions, key=lambda s: s.last_used)

    # Cap for the heal recap prepended on a lossy resume (~few k tokens).
    _HEAL_MAX_CHARS = 24_000

    async def _recent_pairs(self, session_id: str, limit: int) -> List[Tuple[str, str]]:
        """Last `limit` stored (prompt, response) pairs for a session,
        oldest-first. SQLite keeps these even when the backend transcript is
        gone — both heal and the resume-failed seed read from here."""
        async with self.session_manager.storage.db_manager.get_connection() as conn:
            conn.row_factory = aiosqlite.Row
            rows = await (
                await conn.execute(
                    "SELECT prompt, response FROM messages "
                    "WHERE session_id = ? ORDER BY timestamp DESC LIMIT ?",
                    (session_id, limit),
                )
            ).fetchall()
        return [(r["prompt"], r["response"]) for r in reversed(rows)]

    async def _count_session_messages(self, session_id: str) -> Optional[int]:
        """Total stored turns for a session, for the seed's honest
        'last N of TOTAL' label. None on any error — the label degrades."""
        try:
            async with self.session_manager.storage.db_manager.get_connection() as conn:
                row = await (
                    await conn.execute(
                        "SELECT COUNT(*) FROM messages WHERE session_id = ?",
                        (session_id,),
                    )
                ).fetchone()
            return int(row[0]) if row else None
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
        async with self.session_manager.storage._db.get_connection() as conn:
            conn.row_factory = aiosqlite.Row
            rows = await (
                await conn.execute(
                    "SELECT prompt, response FROM messages "
                    "WHERE session_id = ? ORDER BY timestamp ASC",
                    (session.session_id,),
                )
            ).fetchall()

        parts: List[str] = []
        for row in rows:
            if row["prompt"]:
                parts.append(f"User: {row['prompt']}")
            if row["response"]:
                parts.append(f"Assistant: {row['response']}")
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
                "expired": s.is_expired(self.config.session_timeout_hours),
            }
            for s in sessions
        ]

    async def cleanup_expired_sessions(self) -> int:
        """Clean up expired sessions."""
        return await self.session_manager.cleanup_expired_sessions()

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

        await self.cleanup_expired_sessions()

        logger.info("Claude integration shutdown complete")
