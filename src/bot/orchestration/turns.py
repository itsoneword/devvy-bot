"""Conversation-turn handlers for the orchestrator.

Text/document/photo/voice turn pipelines, media turn helpers and the
window-conversation queries they use. Code is moved verbatim from
src/bot/orchestrator.py.
"""

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    Awaitable,
    Callable,
    Dict,
    List,
    Optional,
    Tuple,
    cast,
)

import structlog
from telegram import Update
from telegram.ext import ContextTypes

from ...profiles.registry import user_media_root
from ..utils.draft_streamer import DraftStreamer, generate_draft_id
from ..utils.formatting import FormattedMessage
from ..utils.image_extractor import (
    ImageAttachment,
    allowed_image_roots,
)
from .base import OrchestratorBase
from .formatting import image_save_extension
from .routing import (
    ProfileAccessDenied,
    apply_route,
)

# Thresholds are read off the live WindowRepository instance (settings-driven),
# not imported as constants — see dv-9f31.
from .windowing import (
    _context_tokens_of,
    account_turn_and_maybe_eval,
    consume_pending_seed,
    mentions_remember_trigger,
    refresh_window,
    window_conversation_text,
)

if TYPE_CHECKING:
    from ...claude.sdk_integration import StreamUpdate

logger = structlog.get_logger()

# Uploads that are NOT UTF-8 text: never inline these into the prompt, save
# them to disk and give the agent the path instead.
BINARY_DOCUMENT_EXTENSIONS = {
    ".pdf",
    ".docx",
    ".xlsx",
    ".pptx",
    ".zip",
}


@dataclass
class TurnRoute:
    """Resolved routing for one turn.

    Produced by a ``TurnRequest.resolve_route`` callable inside
    ``_run_turn`` — after the backend-availability early return, so route
    resolution (which may mutate user_data, e.g. a sticky profile switch)
    never happens for a turn that is about to bail out.
    """

    working_dir: Any
    prompt: str
    badge: str = ""
    restore_dir: Optional[Path] = None  # one-shot @slug: dir to restore after
    # When set, overrides TurnRequest.storage_prompt: the text path builds
    # the reply envelope during route resolution (dv-f2a8), and what
    # save_claude_interaction sees must include it — a bare "да" stored
    # without its quoted context is useless to the evaluator.
    storage_prompt: Optional[str] = None


@dataclass
class TurnRequest:
    """One conversation turn through the shared ``_run_turn`` pipeline.

    The text and media paths intentionally differ in several places; every
    difference is carried here as an explicit knob (set by the caller), so
    the shared runner never has to guess which path it is on. Defaults are
    the media path's behavior; ``_agentic_text_turn`` switches them on.
    """

    # What to run and where to report progress.
    prompt: str  # run prompt before route rewrite / window seeding
    storage_prompt: str  # what save_claude_interaction / audit / footer see
    progress_msg: Any
    chat: Any
    user_id: int
    role: str
    # Resolves the working dir (and text-path extras). Returns None when the
    # turn was refused and the user already messaged (ProfileAccessDenied).
    resolve_route: Callable[[], Awaitable[Optional[TurnRoute]]]
    # Media path: images saved for the backend. ``pass_images_kwarg`` keeps
    # the run_command call shape exact — media always passes images= (even
    # None for doc/voice), text never passes the kwarg at all.
    images: Optional[List[Path]] = None
    pass_images_kwarg: bool = False
    # The user's raw text BEFORE any reply envelope was prepended. The
    # «запомни» force-eval trigger matches on this, so quoted text can
    # physically never trip it (msg-766 class, dv-7783). None (media path)
    # falls back to storage_prompt, which never carries an envelope there.
    trigger_text: Optional[str] = None
    # Text path only (drift preserved: media never did any of these).
    use_window_transitions: bool = False  # BPC seed + row force_new + backstop
    enable_stream_drafts: bool = False
    catch_run_errors: bool = False  # wide try/except + error msg as response
    show_footer: bool = False
    resilient_send: bool = False  # HTML send -> plain retry -> failure notice
    audit_command: Optional[str] = None  # audit_logger command name, if any
    # Byte-identical log strings per path.
    interaction_log_error: str = "Failed to log media interaction"
    accounting_log_error: str = "Media window accounting failed"


class TurnsMixin(OrchestratorBase):
    """Text/document/photo/voice turn pipelines and their helpers."""

    if TYPE_CHECKING:
        # Typing-only declarations of cross-mixin helpers. At runtime these
        # are provided by StreamingMixin / InfoCommandsMixin on the concrete
        # MessageOrchestrator; declaring them here (never executed) lets this
        # mixin type-check standalone without importing sibling mixins.
        @staticmethod
        def _start_typing_heartbeat(
            chat: Any,
            interval: float = 2.0,
        ) -> "asyncio.Task[None]":
            raise NotImplementedError

        def _make_stream_callback(
            self,
            verbose_level: int,
            progress_msg: Any,
            tool_log: List[Dict[str, Any]],
            start_time: float,
            mcp_images: Optional[List[ImageAttachment]] = None,
            approved_directory: Optional[Path] = None,
            draft_streamer: Optional[DraftStreamer] = None,
        ) -> Optional[Callable[["StreamUpdate"], Any]]:
            raise NotImplementedError

        async def _send_images(
            self,
            update: Update,
            images: List[ImageAttachment],
            reply_to_message_id: Optional[int] = None,
            caption: Optional[str] = None,
            caption_parse_mode: Optional[str] = None,
        ) -> bool:
            raise NotImplementedError

        def _stop_notice(self, error_type: Optional[str]) -> Optional[str]:
            raise NotImplementedError

    def _save_uploaded_document(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        user_id: int,
        file_name: Optional[str],
        data: bytes,
    ) -> Optional[Path]:
        """Persist a binary upload into the active profile's working dir.

        Returns the saved path, or None if it could not be written. The name is
        sanitised to a bare filename so a crafted upload can't traverse out of
        the profile directory.
        """
        try:
            base = self._media_upload_dir(context, user_id)
            base.mkdir(parents=True, exist_ok=True)
            safe = Path(file_name or "upload").name or "upload"
            target = base / safe
            stem, suffix = target.stem, target.suffix
            n = 1
            while target.exists():
                target = base / f"{stem}_{n}{suffix}"
                n += 1
            target.write_bytes(data)
            logger.info(
                "Saved binary upload",
                path=str(target),
                size=len(data),
                user_id=user_id,
            )
            return target
        except Exception as e:
            logger.error("Failed to save uploaded document", error=str(e))
            return None

    async def _max_message_id(
        self, context: ContextTypes.DEFAULT_TYPE, user_id: int, working_dir: Any
    ) -> int:
        """Highest message_id stored for THIS (user, working_dir).

        Scoped, not a global MAX(message_id): this value becomes the memory
        evaluator's checkpoint cursor, and profiles interleave in one messages
        table. A global max lets a message written by another profile advance
        this profile's cursor past its own unprocessed turns, which silently
        drops them from the next evaluator run — i.e. facts the user was told
        were remembered are never actually extracted.
        """
        storage = context.bot_data.get("storage")
        if not storage:
            return 0
        wd = str(Path(working_dir))
        async with storage.db_manager.get_connection() as conn:
            cur = await conn.execute(
                "SELECT COALESCE(MAX(m.message_id), 0) FROM messages m "
                "JOIN sessions s ON m.session_id = s.session_id "
                "WHERE m.user_id=? AND s.project_path=?",
                (user_id, wd),
            )
            row = await cur.fetchone()
            return int(row[0]) if row else 0

    def _media_working_dir(
        self, context: ContextTypes.DEFAULT_TYPE, user_id: int
    ) -> Path:
        """Working dir for a media turn (document/photo/voice).

        Every role resolves through the same profile router as text turns.
        The old owner short-circuit read ``current_directory``, which is not
        a persisted pref — after a restart the first media turn fell back to
        the home dir and ran in the default profile (dv-b3d7).
        ``active_profile`` IS re-seeded from the DB on every handler, so
        routing on it makes media survive restarts exactly like text.

        Raises ProfileAccessDenied when a guest has no allowed profile.
        """
        return self._routed_working_dir(context, user_id)

    def _media_upload_dir(
        self, context: ContextTypes.DEFAULT_TYPE, user_id: int
    ) -> Path:
        """Directory inbound photos and documents are written to.

        Always a per-user ``uploads/`` folder, never the profile root: on a
        shared profile (chef, pm) the root is readable by every guest who can
        select that profile, so anything dropped there leaks (dv-9ebe). Photos
        used to be written to the working dir itself, which is how 28 loose
        ``tg_image_*`` files accumulated in the repo root and 8 in the chef
        workspace.

        Falls back to ``<working dir>/uploads`` when no profile resolves
        (single-profile installs), which is the old behaviour minus the litter.
        """
        working_dir = self._media_working_dir(context, user_id)
        registry = context.bot_data.get("profile_registry")
        if registry is not None:
            profile = registry.get_by_working_dir(working_dir)
            if profile is not None:
                role = self._resolve_role(user_id, context)
                return user_media_root(profile, role, user_id) / "uploads"
        return working_dir / "uploads"

    async def _window_conversation_text(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        user_id: int,
        working_dir: Any,
        state: Any,
    ) -> str:
        """Concatenate prompts/responses of the current window since the last
        evaluator checkpoint. Thin wrapper around the shared
        ``windowing.window_conversation_text`` (also used by /new)."""
        return await window_conversation_text(
            context.bot_data.get("storage"), user_id, working_dir, state
        )

    async def agentic_text(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        override_text: Optional[str] = None,
    ) -> None:
        """Serialize turns per user, then run the actual text turn.

        A message that arrives mid-turn is acked immediately ("queued") and
        runs when the current turn finishes — against the session state that
        turn produced, like the CLI's own input queueing.
        """
        # A reply carrying a /auth login code is an ordinary text message, so
        # it must be intercepted before the turn lock -- otherwise an in-flight
        # turn holds the code until it finishes and the login has expired.
        from .commands.auth import consume_login_code

        if await consume_login_code(update, context):
            return

        user_id = update.effective_user.id
        lock = self._turn_lock(user_id)
        queued_ack = None
        if lock.locked():
            try:
                queued_ack = await update.message.reply_text(
                    "⏳ Queued — will run when the current turn finishes."
                )
            except Exception:
                pass
        async with lock:
            if queued_ack is not None:
                try:
                    await queued_ack.delete()
                except Exception:
                    pass
            await self._agentic_text_turn(update, context, override_text)

    async def _agentic_text_turn(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        override_text: Optional[str] = None,
    ) -> None:
        """Direct Claude passthrough. Simple progress. No suggestions.

        ``override_text`` lets a switch command re-dispatch inline text without
        mutating the (immutable) Telegram Message.
        """
        user_id = update.effective_user.id
        message_text = (
            override_text if override_text is not None else update.message.text
        )

        logger.info(
            "Agentic text message",
            user_id=user_id,
            message_length=len(message_text),
        )

        role = self._resolve_role(user_id, context)

        # Rate limit check (role picks the owner vs guest cost cap)
        rate_limiter = context.bot_data.get("rate_limiter")
        if rate_limiter:
            allowed, limit_message = await rate_limiter.check_rate_limit(
                user_id, 0.001, role=role
            )
            if not allowed:
                await update.message.reply_text(f"⏱️ {limit_message}")
                return

        chat = update.message.chat
        await chat.send_action("typing")

        progress_msg = await update.message.reply_text("Working...")

        # message_text is Optional per the Telegram stubs but is always a str
        # on this path (len() above would have raised); cast once at the
        # TurnRequest boundary (runtime no-op).
        text_prompt = cast(str, message_text)

        async def resolve_route() -> Optional[TurnRoute]:
            # Profile routing: resolve target profile, working dir, and badge.
            current_dir = context.user_data.get(
                "current_directory", self.settings.approved_directory or Path.home()
            )
            registry = context.bot_data.get("profile_registry")
            badge = ""
            restore_dir: Optional[Path] = None
            prompt_text = text_prompt
            if registry:
                try:
                    decision = apply_route(
                        text_prompt,
                        context.user_data,
                        registry,
                        role=role,
                        user_id=user_id,
                    )
                except ProfileAccessDenied as exc:
                    await progress_msg.edit_text(
                        f"🚫 The '{exc.slug}' profile is owner-only."
                    )
                    storage = context.bot_data.get("storage")
                    if storage:
                        await storage.log_bot_event(
                            user_id,
                            "profile_access_denied",
                            {"profile": exc.slug, "role": exc.role, "via": "route"},
                            success=False,
                        )
                    return None
                current_dir = decision.working_dir
                badge = decision.badge
                restore_dir = decision.restore_dir
                prompt_text = decision.prompt or text_prompt

            # Replying to a verbatim scheduled reminder routes this ONE turn
            # to the profile that scheduled it: the reply gesture states who
            # the answer is for, and without this the answer lands in
            # whatever profile happens to be sticky-active. An explicit
            # /slug or @slug directive in the text still wins (it already
            # rewrote prompt/dirs above); sticky selection is untouched.
            if (
                registry
                and update.message.reply_to_message
                and restore_dir is None
                and prompt_text == text_prompt
            ):
                _ledger_storage = context.bot_data.get("storage")
                _ledger = getattr(_ledger_storage, "sent_messages", None)
                quoted_msg = update.message.reply_to_message
                entry = None
                if _ledger is not None:
                    try:
                        entry = await _ledger.lookup(
                            quoted_msg.chat.id, quoted_msg.message_id
                        )
                    except Exception:
                        entry = None
                if (
                    entry is not None
                    and entry.kind == "scheduled_reminder"
                    and entry.profile_slug
                ):
                    # Ledger stores the agent id ("coach#u123" or "coach");
                    # routing needs the bare profile slug.
                    target_slug = entry.profile_slug.split("#", 1)[0]
                    profile = registry.get_by_slug(target_slug)
                    if profile is not None and profile.allows_role(role):
                        from ...profiles.registry import ensure_workspace_for
                        from .routing import _PROFILE_BADGES

                        reminder_dir = ensure_workspace_for(profile, role, user_id)
                        if reminder_dir != current_dir:
                            restore_dir = current_dir
                            current_dir = reminder_dir
                            badge = _PROFILE_BADGES.get(target_slug, target_slug)
                            logger.info(
                                "Reply to scheduled reminder rerouted",
                                user_id=user_id,
                                target_profile=target_slug,
                            )

            # If the user is replying to a previous message, prepend a
            # labelled reply envelope so the agent knows what they're
            # referring to. Built AFTER route resolution — deliberately, and
            # load-bearing twice over (dv-f2a8): the origin label must
            # compare the quoted message's ledger slug against THIS turn's
            # agent id, and apply_route must parse a /slug or @slug
            # directive from the user's raw text, not an envelope-prefixed
            # one. The raw text is kept separately (trigger_text on the
            # TurnRequest) — the «запомни» trigger must never see quoted
            # text (dv-7783, msg-766).
            storage_prompt: Optional[str] = None
            if update.message.reply_to_message:
                from .reply_context import build_reply_context

                try:
                    bot_id = context.bot.id
                except Exception:
                    bot_id = None
                storage_for_ledger = context.bot_data.get("storage")
                prompt_text, _ = await build_reply_context(
                    update,
                    bot_id,
                    getattr(storage_for_ledger, "sent_messages", None),
                    prompt_text,
                    current_agent_id=self._agent_id_for(context, current_dir, user_id),
                )
                storage_prompt = prompt_text
            return TurnRoute(
                working_dir=current_dir,
                prompt=prompt_text,
                badge=badge,
                restore_dir=restore_dir,
                storage_prompt=storage_prompt,
            )

        await self._run_turn(
            TurnRequest(
                prompt=text_prompt,
                storage_prompt=text_prompt,
                progress_msg=progress_msg,
                chat=chat,
                user_id=user_id,
                role=role,
                resolve_route=resolve_route,
                trigger_text=text_prompt,
                use_window_transitions=True,
                enable_stream_drafts=True,
                catch_run_errors=True,
                show_footer=True,
                resilient_send=True,
                audit_command="text_message",
                interaction_log_error="Failed to log interaction",
                accounting_log_error="Window accounting failed",
            ),
            update,
            context,
        )

    async def _run_turn(
        self,
        request: TurnRequest,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """Shared turn runner for the text and media pipelines.

        Implements the common backend/session/run/accounting/delivery
        pipeline exactly once; every deliberate difference between the two
        paths arrives as an explicit ``TurnRequest`` knob or inside the
        caller-supplied ``resolve_route``. Behavior-preserving refactor of
        the previously duplicated ``_agentic_text_turn`` /
        ``_run_agentic_media_turn`` bodies.

        With ``catch_run_errors`` unset (media path), exceptions from the
        run and post-run bookkeeping propagate to the caller after the
        typing heartbeat is cancelled — the media entry points surface them
        by editing the progress message.
        """
        progress_msg = request.progress_msg
        chat = request.chat
        user_id = request.user_id
        role = request.role

        selected_backend = self._selected_backend(context)
        claude_integration = context.bot_data.get("claude_integration")
        codex_integration = context.bot_data.get("codex_integration")
        active_integration = (
            codex_integration
            if selected_backend == "openai_codex"
            else claude_integration
        )
        if not active_integration:
            await progress_msg.edit_text(
                f"{selected_backend} integration not available. Check configuration."
            )
            return

        # Route resolution runs AFTER the availability early-return above, so
        # it can never mutate user_data (sticky profile switch) for a turn
        # that is about to bail out. None = refused, user already messaged.
        route = await request.resolve_route()
        if route is None:
            return
        current_dir = route.working_dir
        badge = route.badge
        restore_dir = route.restore_dir
        prompt_text = route.prompt
        storage_prompt = (
            route.storage_prompt
            if route.storage_prompt is not None
            else request.storage_prompt
        )

        session_key = self._session_key(selected_backend)
        session_id = context.user_data.get(session_key)
        if context.bot_data.get("profile_registry"):
            # Per-profile isolation: let the facade auto-resume by working_dir
            # (_find_resumable_session) rather than the single global session id.
            session_id = None

        # Check if /new was used — skip auto-resume for this first message.
        # Flag is only cleared after a successful run so retries keep the intent.
        force_new = bool(context.user_data.get("force_new_session"))

        # BPC: if a transition (backend switch / 150k compaction) armed a seed,
        # prepend the previous window's recent tail so the fresh backend
        # session continues the conversation. Both paths since dv-2f19; the
        # media path used to skip this, which meant an armed rotation was
        # ignored until the user next sent text.
        window_repo = (
            context.bot_data.get("window_repo")
            if request.use_window_transitions
            else None
        )
        if window_repo:
            # The transition also set force_new_session=True. Read it from the
            # window row, not just user_data, so a restart between the roll
            # and this turn cannot cancel the rotation (dv-9f31).
            _wstate = await window_repo.get_or_create(user_id, current_dir)
            force_new = (
                force_new
                or _wstate.force_new_session
                or bool(context.user_data.get("force_new_session"))
            )
            if not force_new and window_repo.should_refresh(_wstate):
                # Backstop: the window is already over the roll threshold and
                # nothing armed a transition (older build, lost flag, threshold
                # lowered under a running window, manual DB edit). Run the SAME
                # transition a normal turn-end crossing would — final
                # extraction over the outgoing slice, window roll (so the
                # bundle re-materializes for the new window_id), armed seed —
                # then let THIS turn consume it via consume_pending_seed
                # below, so the user's message still goes out with continuity.
                # The previous bare force_new dropped ~164k tokens of context
                # with no seed, no extraction, and a stale frozen bundle
                # (dv-d9e2, live incident 2026-08-08 10:21:08Z).
                logger.warning(
                    "Window over refresh threshold with no armed transition; "
                    "forcing a fresh session",
                    user_id=user_id,
                    working_dir=str(current_dir),
                    context_tokens=_wstate.last_context_tokens,
                )
                await refresh_window(
                    window_repo,
                    context.bot_data.get("evaluator_service"),
                    context.user_data,
                    user_id=user_id,
                    working_dir=current_dir,
                    backend=selected_backend,
                    conversation_text=await self._window_conversation_text(
                        context, user_id, current_dir, _wstate
                    ),
                    agent_id=self._agent_id_for(context, current_dir, user_id),
                    family_id=self._family_id_for(context, user_id, current_dir),
                )
                force_new = True
            _storage = context.bot_data.get("storage")
            prompt_text = await consume_pending_seed(
                window_repo,
                context.user_data,
                user_id=user_id,
                working_dir=current_dir,
                user_message=prompt_text,
                memory_repo=getattr(_storage, "memory_repository", None),
                agent_id=self._agent_id_for(context, current_dir, user_id),
            )

        # One-shot pending notes for this workspace (e.g. a verbatim
        # scheduled reminder delivered since the last turn): prepend so the
        # agent has the referent even when the user answers without
        # replying-to. Appended after the (possibly seeded) prompt prefix
        # is built, consumed exactly once. A reply-to that quotes the same
        # reminder just repeats ~1 line — harmless, and rarer than the bare
        # answer this exists for.
        _notes_storage = context.bot_data.get("storage")
        _notes_repo = getattr(_notes_storage, "pending_notes", None)
        if _notes_repo is not None:
            notes = await _notes_repo.consume(user_id, str(current_dir))
            if notes:
                prompt_text = "\n".join(notes) + "\n\n" + prompt_text

        # --- Verbose progress tracking via stream callback ---
        verbose_level = self._get_verbose_level(context)
        tool_log: List[Dict[str, Any]] = []
        start_time = time.time()
        mcp_images: List[ImageAttachment] = []

        # Stream drafts (private chats only; text path only)
        draft_streamer: Optional[DraftStreamer] = None
        if (
            request.enable_stream_drafts
            and self.settings.enable_stream_drafts
            and chat.type == "private"
        ):
            draft_streamer = DraftStreamer(
                bot=context.bot,
                chat_id=chat.id,
                draft_id=generate_draft_id(),
                message_thread_id=update.message.message_thread_id,
                throttle_interval=self.settings.stream_draft_interval,
            )

        on_stream = self._make_stream_callback(
            verbose_level,
            progress_msg,
            tool_log,
            start_time,
            mcp_images=mcp_images,
            # Denylist mode (native service) has no global approved_directory;
            # fall back to the current request's working dir (profile-aware)
            # so send_image_to_user still has a boundary to validate against.
            approved_directory=allowed_image_roots(
                self.settings.approved_directory or current_dir
            ),
            draft_streamer=draft_streamer,
        )

        # Independent typing heartbeat — stays alive even with no stream events
        heartbeat = self._start_typing_heartbeat(chat)

        # Family memory scope for this turn (None = no family, no change).
        family_id = self._family_id_for(context, user_id, current_dir)

        success = True
        saved_message_id: Optional[int] = None
        try:
            if selected_backend == "openai_codex" and codex_integration:
                codex_integration.set_user_model(user_id, self._selected_model(context))
            # Media always passes images= (even None for doc/voice); text
            # never passes the kwarg at all. The splat keeps each path's
            # call shape byte-identical to the pre-refactor code.
            extra_kwargs: Dict[str, Any] = (
                {"images": request.images} if request.pass_images_kwarg else {}
            )
            claude_response = await active_integration.run_command(
                prompt=prompt_text,
                working_directory=current_dir,
                user_id=user_id,
                session_id=session_id,
                on_stream=on_stream,
                force_new=force_new,
                **extra_kwargs,
                model=self._claude_model_override(context),
                requester_chat_id=chat.id,
                family_id=family_id,
                role=role,
            )

            # Media path cancels the heartbeat right after the run; the text
            # path keeps it alive through post-run bookkeeping (cancelled by
            # the finally below — cancel() is idempotent).
            if not request.catch_run_errors:
                heartbeat.cancel()

            # New session created successfully — clear the one-shot flag in
            # both places it can live.
            if force_new:
                context.user_data["force_new_session"] = False
                if window_repo:
                    await window_repo.clear_pending_transition(user_id, current_dir)

            context.user_data[session_key] = claude_response.session_id

            # True-up the budget with the REAL turn cost — the pre-check only
            # consumed a ~$0.001 estimate, which is why the per-user cap never
            # actually triggered on spend.
            rate_limiter = context.bot_data.get("rate_limiter")
            if rate_limiter:
                if claude_response.cost > 0:
                    await rate_limiter.record_cost(user_id, claude_response.cost)

                # Session watchdog (dv-8bb5): soft hourly thresholds, checked
                # against the same real cost plus this turn's SDK turn count.
                # Never blocks — a crossed threshold just alerts the admin.
                watchdog_manager = context.bot_data.get("watchdog_manager")
                if watchdog_manager:
                    for alert in rate_limiter.record_watchdog_turn(
                        user_id, claude_response.cost, claude_response.num_turns
                    ):
                        await watchdog_manager.trigger(
                            alert.user_id, alert.metric, alert.value, alert.threshold
                        )

            # NOTE: no prose-based cwd tracking here (dv-b4f1). Classic mode
            # regex-scanned the reply for `cd <path>` and moved the user's
            # working directory to match; in denylist mode that ended in
            # `Path.is_relative_to(None)` -> TypeError, which the turn's broad
            # handler turned into an error reply after the tokens were paid
            # for. Profiles + /repo are the supported way to move.

            # One-shot (@slug): restore the active profile's working directory.
            if restore_dir is not None:
                context.user_data["current_directory"] = restore_dir

            # Store interaction (stamped with the active conversation window)
            storage = context.bot_data.get("storage")
            window_repo = context.bot_data.get("window_repo")
            state_before = None
            if window_repo:
                state_before = await window_repo.get_or_create(user_id, current_dir)
            if storage:
                try:
                    saved_message_id = await storage.save_claude_interaction(
                        user_id=user_id,
                        session_id=claude_response.session_id,
                        prompt=storage_prompt,
                        response=claude_response,
                        ip_address=None,
                        window_id=state_before.window_id if state_before else None,
                    )
                except Exception as e:
                    logger.warning(request.interaction_log_error, error=str(e))

            # Save-honesty check (dv-7783): did the reply claim a durable
            # write that no tool call backs? Telemetry only — log + owner
            # inform, no retry/annotation. Sits at the one choke point both
            # text and media turns share, joinable to `messages` via the id
            # save_claude_interaction just returned.
            if storage and self.settings.save_claim_check_enabled:
                try:
                    from ...events.types import AgentResponseEvent
                    from .save_honesty import check_turn

                    trigger_fired = mentions_remember_trigger(
                        request.trigger_text
                        if request.trigger_text is not None
                        else storage_prompt
                    )
                    honesty = check_turn(
                        claude_response.content,
                        [t["name"] for t in claude_response.tools_used],
                        trigger_fired=trigger_fired,
                        force_eval_scheduled=(
                            trigger_fired
                            and context.bot_data.get("evaluator_service") is not None
                        ),
                    )
                    profile_slug = self._agent_id_for(context, current_dir, user_id)
                    if honesty.claim is not None or trigger_fired:
                        # Every detected claim is logged, not only mismatches:
                        # the FP/miss rates are recomputable only with a
                        # denominator (plan decision 3).
                        await storage.log_bot_event(
                            user_id,
                            "save_claim_detected",
                            {
                                "message_id": saved_message_id,
                                "profile": profile_slug,
                                "matched_pattern": (
                                    honesty.claim.family if honesty.claim else None
                                ),
                                "satisfied_by": honesty.satisfied_by,
                                "trigger_fired": trigger_fired,
                            },
                        )
                    if honesty.mismatch_kind:
                        excerpt = honesty.claim.excerpt if honesty.claim else ""
                        await storage.log_bot_event(
                            user_id,
                            "save_claim_mismatch",
                            {
                                "message_id": saved_message_id,
                                "profile": profile_slug,
                                "mismatch_kind": honesty.mismatch_kind,
                                "matched_pattern": (
                                    honesty.claim.family if honesty.claim else None
                                ),
                                "claim_excerpt": excerpt,
                                "tools_used": [
                                    t["name"] for t in claude_response.tools_used
                                ],
                                "trigger_fired": trigger_fired,
                            },
                            success=False,
                        )
                        event_bus = context.bot_data.get("event_bus")
                        if event_bus:
                            claim_line = (
                                f"«{' '.join(excerpt.split())[:160]}»"
                                if excerpt
                                else "(no claim text — trigger without write)"
                            )
                            await event_bus.publish(
                                AgentResponseEvent(
                                    chat_id=0,  # broadcast to owner chats
                                    text=(
                                        "⚠️ save-claim mismatch "
                                        f"[{honesty.mismatch_kind}] "
                                        f"{profile_slug} msg={saved_message_id}: "
                                        f"{claim_line}"
                                    ),
                                    parse_mode=None,
                                    source="save_honesty",
                                )
                            )
                except Exception as e:
                    logger.warning("Save-honesty check failed", error=str(e))

            # Backend-agnostic token accounting + 50k evaluator checkpoint.
            window_state_for_footer = None
            window_rotated_for_footer = False
            if window_repo:
                try:
                    evaluator = context.bot_data.get("evaluator_service")
                    # Resolve the active profile slug so facts are scoped to
                    # *this* agent's memory, not the global default.
                    agent_id = self._agent_id_for(context, current_dir, user_id)
                    state = await window_repo.get_or_create(user_id, current_dir)
                    convo = await self._window_conversation_text(
                        context, user_id, current_dir, state
                    )
                    await account_turn_and_maybe_eval(
                        window_repo=window_repo,
                        evaluator=evaluator,
                        user_id=user_id,
                        working_dir=current_dir,
                        backend=selected_backend,
                        added_tokens=(
                            claude_response.input_tokens + claude_response.output_tokens
                        ),
                        context_tokens=_context_tokens_of(claude_response),
                        conversation_text=convo,
                        max_message_id=await self._max_message_id(
                            context, user_id, current_dir
                        ),
                        agent_id=agent_id,
                        family_id=family_id,
                        # Trigger runs on the user's raw text only — a quoted
                        # «запомнил» in the reply envelope cannot force an
                        # eval (dv-7783, msg-766).
                        force_eval=mentions_remember_trigger(
                            request.trigger_text
                            if request.trigger_text is not None
                            else storage_prompt
                        ),
                    )
                    # 150k compaction: roll the window on the SAME backend and
                    # arm the seed so the next turn continues fresh.
                    state_after = await window_repo.get_or_create(user_id, current_dir)
                    window_state_for_footer = state_after
                    if window_repo.should_refresh(state_after):
                        window_rotated_for_footer = True
                        await refresh_window(
                            window_repo,
                            evaluator,
                            context.user_data,
                            user_id=user_id,
                            working_dir=current_dir,
                            backend=selected_backend,
                            conversation_text=await self._window_conversation_text(
                                context, user_id, current_dir, state_after
                            ),
                            agent_id=agent_id,
                            family_id=family_id,
                        )
                except Exception as e:
                    logger.warning(request.accounting_log_error, error=str(e))

            # Format response (no reply_markup — strip keyboards)
            from ..utils.formatting import ResponseFormatter

            formatter = ResponseFormatter(self.settings)
            formatted_messages = formatter.format_claude_response(
                claude_response.content
            )

            # Footer: compact window diagnostic at verbose >= 1 (so rotation
            # is visible by default without cranking verbosity), full
            # input/output token + cost breakdown layered on at verbose >= 2.
            # Rendered on both the text and media paths (dv-2f19).
            if request.show_footer and verbose_level >= 1:
                footer_parts = []
                if verbose_level >= 2:
                    input_t = claude_response.input_tokens
                    output_t = claude_response.output_tokens
                    # Fall back to estimation if SDK didn't provide real counts
                    if input_t == 0 and output_t == 0:
                        input_t = len(storage_prompt) // 4
                        output_t = len(claude_response.content) // 4
                        prefix = "~"
                    else:
                        prefix = ""
                    total_t = input_t + output_t
                    cost_str = (
                        f"${claude_response.cost:.4f}"
                        if claude_response.cost > 0
                        else ""
                    )
                    footer_parts += [
                        f"{prefix}{input_t} in",
                        f"{prefix}{output_t} out",
                        f"{prefix}{total_t} total",
                    ]
                    if cost_str:
                        footer_parts.append(cost_str)
                if getattr(claude_response, "session_restarted", False):
                    footer_parts.append("↻ session restarted (resume failed)")
                if window_rotated_for_footer:
                    footer_parts.append("🔄 window rotated")
                elif window_state_for_footer is not None:
                    # Real context vs the roll threshold. Was cumulative_tokens
                    # (a flow sum measured on uncached input only), which read
                    # ~2.5x low and made the gauge agree with the bug rather
                    # than with reality (dv-9f31).
                    footer_parts.append(
                        f"win {window_state_for_footer.last_context_tokens // 1000}k/"
                        f"{window_repo.refresh_threshold_tokens // 1000}k"
                    )

                if footer_parts:
                    footer = "\n\n---\n" + " | ".join(footer_parts)

                    # Append to last message, checking Telegram limit
                    last_msg = formatted_messages[-1]
                    if len(last_msg.text) + len(footer) <= 4000:
                        formatted_messages[-1] = FormattedMessage(
                            text=last_msg.text + footer,
                            parse_mode=last_msg.parse_mode,
                        )
                    else:
                        # Footer as separate message if it would exceed limit
                        formatted_messages.append(
                            FormattedMessage(text=footer.strip(), parse_mode=None)
                        )

            # Aborted run: the "response" above is just mid-task narration (no
            # final result exists). Tell the user instead of passing it off as
            # a finished answer. Shown at every verbosity level.
            notice = self._stop_notice(getattr(claude_response, "error_type", None))
            if notice:
                formatted_messages.append(
                    FormattedMessage(text=notice, parse_mode=None)
                )

        except Exception as e:
            if not request.catch_run_errors:
                # Media path: propagate to the entry point (which edits the
                # progress message); the finally still cancels the heartbeat.
                raise
            success = False
            logger.error(
                "Agent backend failed",
                backend=selected_backend,
                error=str(e),
                user_id=user_id,
            )
            from .formatting import _format_error_message

            formatted_messages = [
                FormattedMessage(_format_error_message(e), parse_mode="HTML")
            ]
        finally:
            heartbeat.cancel()
            if draft_streamer:
                try:
                    await draft_streamer.flush()
                except Exception:
                    logger.debug("Draft flush failed in finally block", user_id=user_id)

        try:
            await progress_msg.delete()
        except Exception:
            logger.debug("Failed to delete progress message, ignoring")

        # Prefix the profile badge onto the first outgoing message (profile mode).
        if badge and formatted_messages and formatted_messages[0].text:
            first = formatted_messages[0]
            formatted_messages[0] = FormattedMessage(
                text=f"{badge}\n\n{first.text}",
                parse_mode=first.parse_mode,
            )

        # Use MCP-collected images (from send_image_to_user tool calls)
        images: List[ImageAttachment] = mcp_images

        # Try to combine text + images in one message when possible
        caption_sent = False
        if images and len(formatted_messages) == 1:
            msg = formatted_messages[0]
            if msg.text and len(msg.text) <= 1024:
                try:
                    caption_sent = await self._send_images(
                        update,
                        images,
                        reply_to_message_id=update.message.message_id,
                        caption=msg.text,
                        caption_parse_mode=msg.parse_mode,
                    )
                except Exception as img_err:
                    logger.warning("Image+caption send failed", error=str(img_err))

        # Ledger every outgoing reply (dv-7783): the reply-context builder
        # resolves user replies to our messages here. Never raises; a failed
        # ledger write only degrades the NEXT reply's origin attribution.
        _ledger_storage = context.bot_data.get("storage")

        async def _ledger_sent(sent: Any) -> None:
            if _ledger_storage is None or sent is None:
                return
            try:
                await _ledger_storage.sent_messages.record(
                    telegram_chat_id=sent.chat.id,
                    telegram_message_id=sent.message_id,
                    kind="turn_reply",
                    messages_message_id=saved_message_id,
                    profile_slug=self._agent_id_for(context, current_dir, user_id),
                )
            except Exception as ledger_err:
                logger.warning("sent_messages ledger failed", error=str(ledger_err))

        # Send text messages (skip if caption was already embedded in photos)
        if not caption_sent:
            for i, message in enumerate(formatted_messages):
                if not message.text or not message.text.strip():
                    continue
                if not request.resilient_send:
                    # Media path: no HTML→plain fallback (preserved drift) —
                    # a send error propagates to the media entry point.
                    sent_msg = await update.message.reply_text(
                        message.text,
                        parse_mode=message.parse_mode,
                        reply_markup=None,
                        reply_to_message_id=(
                            update.message.message_id if i == 0 else None
                        ),
                    )
                    await _ledger_sent(sent_msg)
                    if i < len(formatted_messages) - 1:
                        await asyncio.sleep(0.5)
                    continue
                try:
                    sent_msg = await update.message.reply_text(
                        message.text,
                        parse_mode=message.parse_mode,
                        reply_markup=None,  # No keyboards in agentic mode
                        reply_to_message_id=(
                            update.message.message_id if i == 0 else None
                        ),
                    )
                    await _ledger_sent(sent_msg)
                    if i < len(formatted_messages) - 1:
                        await asyncio.sleep(0.5)
                except Exception as send_err:
                    logger.warning(
                        "Failed to send HTML response, retrying as plain text",
                        error=str(send_err),
                        message_index=i,
                    )
                    try:
                        sent_msg = await update.message.reply_text(
                            message.text,
                            reply_markup=None,
                            reply_to_message_id=(
                                update.message.message_id if i == 0 else None
                            ),
                        )
                        await _ledger_sent(sent_msg)
                    except Exception as plain_err:
                        await update.message.reply_text(
                            f"Failed to deliver response "
                            f"(Telegram error: {str(plain_err)[:150]}). "
                            f"Please try again.",
                            reply_to_message_id=(
                                update.message.message_id if i == 0 else None
                            ),
                        )

            # Send images separately if caption wasn't used
            if images:
                try:
                    await self._send_images(
                        update,
                        images,
                        reply_to_message_id=update.message.message_id,
                    )
                except Exception as img_err:
                    logger.warning("Image send failed", error=str(img_err))

        # Audit log (text path only — media turns never logged one).
        if request.audit_command is not None:
            audit_logger = context.bot_data.get("audit_logger")
            if audit_logger:
                await audit_logger.log_command(
                    user_id=user_id,
                    command=request.audit_command,
                    args=[storage_prompt[:100]],
                    success=success,
                )

    async def agentic_document(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Process file upload -> Claude, minimal chrome."""
        user_id = update.effective_user.id
        document = update.message.document

        logger.info(
            "Agentic document upload",
            user_id=user_id,
            filename=document.file_name,
        )

        # Security validation
        security_validator = context.bot_data.get("security_validator")
        if security_validator:
            valid, error = security_validator.validate_filename(document.file_name)
            if not valid:
                await update.message.reply_text(f"File rejected: {error}")
                return

        # Size check. 20MB matches Telegram Bot API's own hard cap on file
        # downloads for bots (cloud API, no local Bot API server) -- and the
        # same limit used by the security middleware's earlier check.
        max_size = 20 * 1024 * 1024
        if document.file_size > max_size:
            await update.message.reply_text(
                f"File too large ({document.file_size / 1024 / 1024:.1f}MB). Max: 20MB."
            )
            return

        chat = update.message.chat
        lock, progress_msg, was_queued = await self._begin_media_turn(
            update, user_id, "Working..."
        )
        async with lock:
            if was_queued:
                try:
                    await progress_msg.edit_text("Working...")
                except Exception:
                    pass

            # Try enhanced file handler, fall back to basic
            features = context.bot_data.get("features")
            file_handler = features.get_file_handler() if features else None
            prompt: Optional[str] = None

            # Binary documents (pdf/office/archives) must never be inlined into
            # the prompt as text -- they aren't UTF-8 and the old fallback just
            # bailed with "Must be text-based". Save them to the profile's
            # working dir and hand the agent the PATH so it can open them with
            # the pdf/docx/xlsx skills (or Read) like any other file on disk.
            ext = Path(document.file_name or "").suffix.lower()
            if ext in BINARY_DOCUMENT_EXTENSIONS:
                file_handler = None

            if file_handler:
                try:
                    processed_file = await file_handler.handle_document_upload(
                        document,
                        user_id,
                        update.message.caption or "Please review this file:",
                    )
                    prompt = processed_file.prompt
                except Exception:
                    file_handler = None

            if not file_handler:
                file = await document.get_file()
                file_bytes = await file.download_as_bytearray()
                caption = update.message.caption or "Please review this file:"
                content: Optional[str] = None
                if ext not in BINARY_DOCUMENT_EXTENSIONS:
                    try:
                        content = bytes(file_bytes).decode("utf-8")
                    except UnicodeDecodeError:
                        content = None

                if content is not None:
                    if len(content) > 50000:
                        content = content[:50000] + "\n... (truncated)"
                    prompt = (
                        f"{caption}\n\n**File:** `{document.file_name}`\n\n"
                        f"```\n{content}\n```"
                    )
                else:
                    saved = self._save_uploaded_document(
                        context, user_id, document.file_name, bytes(file_bytes)
                    )
                    if saved is None:
                        await progress_msg.edit_text(
                            "Could not save the uploaded file."
                        )
                        return
                    prompt = (
                        f"{caption}\n\n**File saved to:** `{saved}`\n\n"
                        f"It is a binary document ({ext or 'no extension'}), not "
                        f"text. Open it from that path with the appropriate "
                        f"tool/skill rather than expecting its contents inline."
                    )

            if not prompt:
                await progress_msg.edit_text("Could not read the file.")
                return

            # Process through the ACTIVE backend (same path as photo/voice).
            try:
                await self._run_agentic_media_turn(
                    update=update,
                    context=context,
                    prompt=prompt,
                    progress_msg=progress_msg,
                    user_id=user_id,
                    chat=chat,
                )
            except Exception as e:
                from .formatting import _format_error_message

                await progress_msg.edit_text(
                    _format_error_message(e), parse_mode="HTML"
                )
                logger.error(
                    "Document processing failed", error=str(e), user_id=user_id
                )

    async def agentic_photo(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Process photo -> Claude, minimal chrome."""
        user_id = update.effective_user.id

        features = context.bot_data.get("features")
        image_handler = features.get_image_handler() if features else None

        if not image_handler:
            await update.message.reply_text("Photo processing is not available.")
            return

        chat = update.message.chat
        lock, progress_msg, was_queued = await self._begin_media_turn(
            update, user_id, "Working..."
        )
        async with lock:
            if was_queued:
                try:
                    await progress_msg.edit_text("Working...")
                except Exception:
                    pass
            try:
                photo = update.message.photo[-1]
                processed_image = await image_handler.process_image(
                    photo, update.message.caption
                )

                # Save the image into the working dir so the active backend can
                # view it (Codex via -i; Claude via the Read tool path).
                from ..features.image_handler import save_image_bytes

                upload_dir = self._media_upload_dir(context, user_id)
                tg_file = await photo.get_file()
                data = await tg_file.download_as_bytearray()
                img_path = save_image_bytes(
                    bytes(data), image_save_extension(processed_image), upload_dir
                )

                await self._run_agentic_media_turn(
                    update=update,
                    context=context,
                    prompt=processed_image.prompt,
                    progress_msg=progress_msg,
                    user_id=user_id,
                    chat=chat,
                    images=[img_path],
                )

            except Exception as e:
                from .formatting import _format_error_message

                await progress_msg.edit_text(
                    _format_error_message(e), parse_mode="HTML"
                )
                logger.error(
                    "Claude photo processing failed", error=str(e), user_id=user_id
                )

    async def agentic_voice(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Transcribe voice message -> Claude, minimal chrome."""
        user_id = update.effective_user.id

        features = context.bot_data.get("features")
        voice_handler = features.get_voice_handler() if features else None

        if not voice_handler:
            await update.message.reply_text(self._voice_unavailable_message())
            return

        chat = update.message.chat
        lock, progress_msg, was_queued = await self._begin_media_turn(
            update, user_id, "Transcribing..."
        )
        async with lock:
            if was_queued:
                try:
                    await progress_msg.edit_text("Transcribing...")
                except Exception:
                    pass
            try:
                voice = update.message.voice
                processed_voice = await voice_handler.process_voice_message(
                    voice, update.message.caption
                )

                await progress_msg.edit_text("Working...")
                await self._run_agentic_media_turn(
                    update=update,
                    context=context,
                    prompt=processed_voice.prompt,
                    progress_msg=progress_msg,
                    user_id=user_id,
                    chat=chat,
                )

            except Exception as e:
                from .formatting import _format_error_message

                await progress_msg.edit_text(
                    _format_error_message(e), parse_mode="HTML"
                )
                logger.error(
                    "Claude voice processing failed", error=str(e), user_id=user_id
                )

    async def _begin_media_turn(
        self, update: Update, user_id: int, initial_text: str
    ) -> Tuple[asyncio.Lock, Any, bool]:
        """Claim the user's turn slot AT ARRIVAL — before any download or
        transcription — so execution order matches send order (a voice still
        transcribing must not be overtaken by a text sent a second later).
        Returns (lock, progress_msg, was_queued); the caller enters
        ``async with lock`` and resets the progress text once inside."""
        lock = self._turn_lock(user_id)
        was_queued = lock.locked()
        chat = update.message.chat
        await chat.send_action("typing")
        progress_msg = await update.message.reply_text(
            "⏳ Queued — will run when the current turn finishes."
            if was_queued
            else initial_text
        )
        return lock, progress_msg, was_queued

    async def _run_agentic_media_turn(
        self,
        *,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        prompt: str,
        progress_msg: Any,
        user_id: int,
        chat: Any,
        images: Optional[List[Path]] = None,
    ) -> None:
        """Run a media-derived prompt through the ACTIVE backend.

        Media-specific prep (working dir via ``_media_working_dir``,
        ProfileAccessDenied refusal) feeds the shared ``_run_turn`` pipeline;
        run/send errors propagate to the document/photo/voice entry points,
        which surface them by editing the progress message.
        """

        async def resolve_route() -> Optional[TurnRoute]:
            try:
                current_dir = self._media_working_dir(context, user_id)
            except ProfileAccessDenied as exc:
                await progress_msg.edit_text(
                    f"🚫 The '{exc.slug}' profile is owner-only."
                )
                return None
            # Same profile badge the text path prefixes (dv-2f19): a photo
            # answer must say which agent replied, exactly like a text one.
            badge = ""
            if context.bot_data.get("profile_registry"):
                from .routing import _PROFILE_BADGES

                slug = self._agent_id_for(context, current_dir, user_id).split("#", 1)[
                    0
                ]
                badge = _PROFILE_BADGES.get(slug, slug)
            return TurnRoute(working_dir=current_dir, prompt=prompt, badge=badge)

        await self._run_turn(
            TurnRequest(
                prompt=prompt,
                storage_prompt=prompt,
                progress_msg=progress_msg,
                chat=chat,
                user_id=user_id,
                role=self._resolve_role(user_id, context),
                resolve_route=resolve_route,
                images=images,
                pass_images_kwarg=True,
                # Media turns used to skip window accounting entirely, so a
                # photo's tokens were never counted, the 150k rotation never
                # fired on them, and no footer was rendered (dv-2f19). They
                # now run the same window pipeline as text.
                use_window_transitions=True,
                show_footer=True,
            ),
            update,
            context,
        )

    def _voice_unavailable_message(self) -> str:
        """Return provider-aware guidance when voice feature is unavailable."""
        return (
            "Voice processing is not available. "
            f"Set {self.settings.voice_provider_api_key_env} "
            f"for {self.settings.voice_provider_display_name} and install "
            "voice extras with: poetry install --extras voice"
        )
