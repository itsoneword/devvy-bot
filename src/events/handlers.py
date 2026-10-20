"""Event handlers that bridge the event bus to Claude and Telegram.

AgentHandler: translates events into ClaudeIntegration.run_command() calls.
NotificationHandler: subscribes to AgentResponseEvent and delivers to Telegram.
"""

import contextlib
import dataclasses
from datetime import UTC, datetime
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    AsyncContextManager,
    AsyncIterator,
    Dict,
    List,
    Optional,
)

import structlog

from ..claude.facade import ClaudeIntegration
from ..items.loop_guard import automated_turn
from ..model_router import get_router
from .bus import Event, EventBus
from .health_export import build_health_export_prompt, summarize_health_export
from .types import AgentResponseEvent, ScheduledEvent, WebhookEvent

if TYPE_CHECKING:
    from ..profiles.registry import ProfileRegistry
    from ..utils.turn_locks import TurnLocks

logger = structlog.get_logger()


def scheduled_report_source(event: ScheduledEvent) -> str:
    """The ``source`` a scheduled turn's reply carries (dv-e215 routing key).

    Item jobs are the owner's reminders -> ``item`` (always DM). The bgjob
    finish digest -> ``bgjob:digest``. Everything else ``schedule:<job name>``
    so the routing map can send e.g. ``schedule:crypto`` to its own topic.
    """
    if event.item_id is not None:
        return "item"
    if event.job_name.split(":", 1)[0] == "job-finish":
        return "bgjob:digest"
    return f"schedule:{event.job_name}" if event.job_name else "schedule"


class AgentHandler:
    """Translates incoming events into Claude agent executions.

    Webhook and scheduled events are converted into prompts and sent
    through the model router to ClaudeIntegration. The response is published
    back as an AgentResponseEvent for delivery.
    """

    def __init__(
        self,
        event_bus: EventBus,
        claude_integration: ClaudeIntegration,
        default_working_directory: Path,
        default_user_id: int = 0,
        profile_registry: Optional["ProfileRegistry"] = None,
        health_export_profile_slug: str = "coach",
        health_export_user_id: Optional[int] = None,
        turn_locks: Optional["TurnLocks"] = None,
        user_store: Optional[Any] = None,
        storage: Optional[Any] = None,
        window_repo: Optional[Any] = None,
        default_agent_model: Optional[str] = None,
        items: Optional[Any] = None,
    ) -> None:
        self.event_bus = event_bus
        # Optional ItemService (dv-752a), attached in main once the scheduler
        # exists. Item-linked jobs fire through it; None = plain jobs only.
        self.items: Any = items
        self.claude = claude_integration
        self.default_working_directory = default_working_directory
        self.default_user_id = default_user_id
        self.profile_registry = profile_registry
        self.health_export_profile_slug = health_export_profile_slug
        # Falls back to default_user_id (itself ALLOWED_USERS[0] by default in
        # main.py) so the feature works out of the box without extra config.
        self.health_export_user_id = health_export_user_id or default_user_id
        # Shared with MessageOrchestrator: an event-driven turn is "just
        # another queued turn" and must not run alongside a Telegram turn for
        # the same user (parallel claude process + session write race).
        self.turn_locks = turn_locks
        # Telegram Bot, wired in main after bot.initialize(); None = no DM
        # notice for background turns (tests, headless).
        self.bot: Any = None
        # Role lookup for the cost caps. AuthManager.get_user_role needs a live
        # Telegram session, which an event-driven turn (a 3am cron job) never
        # has; UserStore reads the durable bot_users role instead. Without it
        # every scheduled turn ran as "no role known" = capped, and a turn that
        # hit the cap was silently truncated.
        self.user_store = user_store
        # Storage facade: verbatim reminders persist a messages row (so the
        # agent can later see the user was reminded) and arm a pending note;
        # the agent path reads the owner's saved model pref from it.
        self.storage = storage
        self.window_repo = window_repo
        # Settings-level model default (settings.agent_model), the same
        # fallback the Telegram path uses after the per-user pref.
        self.default_agent_model = default_agent_model

    def _role_for(self, user_id: Optional[int]) -> Optional[str]:
        """Durable role for an event-driven turn, or None if unknown.

        None keeps the previous (capped) behavior, so an unresolvable user
        never accidentally gains the owner exemption.
        """
        if self.user_store is None or user_id is None:
            return None
        getter = getattr(self.user_store, "get_role_cached", None)
        if not callable(getter):
            return None
        role = getter(user_id)
        return role if isinstance(role, str) else None

    def _profile_slug_for(self, working_dir: Optional[Path]) -> Optional[str]:
        """Bare profile slug owning ``working_dir`` (the turn-lock key), the
        same slug the Telegram path derives; None without a registry."""
        if self.profile_registry is None or working_dir is None:
            return None
        from ..profiles.registry import DEFAULT_AGENT_ID

        profile, _ = self.profile_registry.resolve_working_dir(Path(working_dir))
        return profile.slug if profile is not None else DEFAULT_AGENT_ID

    def _turn_lock(
        self,
        user_id: int,
        label: str = "a background task",
        working_dir: Optional[Path] = None,
    ) -> AsyncContextManager[Any]:
        """The (user, agent) turn slot, labelled so queued chat messages can
        say what they wait behind (dv-19a6). ``working_dir`` is where the turn
        runs; its owning profile is the lock key."""
        if self.turn_locks is None:
            return contextlib.AsyncExitStack()  # no-op context
        return self.turn_locks.hold(user_id, label, self._profile_slug_for(working_dir))

    @contextlib.asynccontextmanager
    async def _busy_notice(self, chat_id: int, label: str) -> AsyncIterator[None]:
        """Show the owner, in his DM, that a background turn holds his
        conversation; removed when it ends. Best effort, never blocks the turn.
        Private chats have chat_id == user_id."""
        sent = None
        if self.bot is not None and chat_id:
            try:
                sent = await self.bot.send_message(
                    chat_id=chat_id,
                    text=f"⏳ busy: {label}… (your messages will queue)",
                    disable_notification=True,
                )
            except Exception:
                logger.debug("busy notice send failed", chat_id=chat_id)
        try:
            yield
        finally:
            if sent is not None:
                try:
                    await sent.delete()
                except Exception:
                    pass

    def register(self) -> None:
        """Subscribe to events that need agent processing."""
        self.event_bus.subscribe(WebhookEvent, self.handle_webhook)
        self.event_bus.subscribe(ScheduledEvent, self.handle_scheduled)

    async def handle_webhook(self, event: Event) -> None:
        """Process a webhook event through Claude."""
        if not isinstance(event, WebhookEvent):
            return

        if event.provider == "health":
            await self._handle_health_webhook(event)
            return

        logger.info(
            "Processing webhook event through agent",
            provider=event.provider,
            event_type=event.event_type_name,
            delivery_id=event.delivery_id,
        )

        prompt = self._build_webhook_prompt(event)

        try:
            async with self._turn_lock(
                self.default_user_id,
                f"webhook {event.provider}",
                self.default_working_directory,
            ):
                response = await get_router().run_turn(
                    self.claude,
                    purpose="webhook",
                    attended=False,
                    prompt=prompt,
                    working_directory=self.default_working_directory,
                    user_id=self.default_user_id,
                    role=self._role_for(self.default_user_id),
                )

            if response.content:
                # We don't know which chat to send to from a webhook alone.
                # The notification service needs configured target chats.
                # Publish with chat_id=0 — the NotificationService
                # will broadcast to configured notification_chat_ids.
                await self.event_bus.publish(
                    AgentResponseEvent(
                        chat_id=0,
                        text=response.content,
                        parse_mode="markdown",
                        originating_event_id=event.id,
                        source=f"webhook:{event.provider}",
                    )
                )
        except Exception:
            logger.exception(
                "Agent execution failed for webhook event",
                provider=event.provider,
                event_id=event.id,
            )

    async def _handle_health_webhook(self, event: WebhookEvent) -> None:
        """Route an Apple Health export (Health Auto Export app) to the
        configured coach-style profile instead of the generic summarizer.

        Silent by design when nothing recognized is in the payload — the
        app can be configured to export 150+ metrics at once; only a
        handful (sleep, HRV, resting HR, blood pressure) matter for the
        recovery log, and spending an agent turn on an irrelevant export
        (a workout, ECG, mood entry, ...) would just be noise.
        """
        summary = summarize_health_export(event.payload)
        if not summary:
            logger.info(
                "Health webhook had no recognized metrics, skipping agent turn",
                delivery_id=event.delivery_id,
            )
            return

        working_directory = self.default_working_directory
        if self.profile_registry is not None:
            profile = self.profile_registry.get_by_slug(self.health_export_profile_slug)
            if profile is not None:
                # Must land in the exporting user's OWN workspace. Writing to
                # the profile root would drop one person's sleep/HRV data into
                # a directory the other family member's agent reads — exactly
                # the leak dv-10e4 closes on the Telegram path.
                from ..profiles.registry import ensure_workspace_for

                working_directory = ensure_workspace_for(
                    profile,
                    self._role_for(self.health_export_user_id) or "owner",
                    self.health_export_user_id,
                )

        prompt = build_health_export_prompt(summary)

        try:
            async with self._turn_lock(
                self.health_export_user_id, "health export", working_directory
            ):
                response = await get_router().run_turn(
                    self.claude,
                    purpose="webhook",
                    attended=False,
                    prompt=prompt,
                    working_directory=working_directory,
                    user_id=self.health_export_user_id,
                    role=self._role_for(self.health_export_user_id),
                )
            # Private Telegram chats: chat_id == user_id. Targeted reply,
            # not a broadcast -- this is personal health data, not a
            # generic event everyone with notifications configured
            # should see.
            if response.content and self.health_export_user_id:
                await self.event_bus.publish(
                    AgentResponseEvent(
                        chat_id=self.health_export_user_id,
                        text=response.content,
                        parse_mode="markdown",
                        originating_event_id=event.id,
                        source="health",
                    )
                )
        except Exception:
            logger.exception(
                "Agent execution failed for health webhook",
                event_id=event.id,
            )

    def _resolve_job_owner(self, event: ScheduledEvent) -> int:
        """The Telegram user id a scheduled/verbatim job actually belongs to.

        Order: an explicit ``created_by`` on the event; else the sole entry
        in ``target_chat_ids`` (private chats have chat_id == user_id); else
        no owner information at all — fall back to ``default_user_id``,
        today's behavior (dv-3e91 open question 2; every currently-live job
        has a resolvable single owner, so this branch is defensive only).

        Deliberately returns only the id, not a role: the caller passes
        ``self._role_for(owner_id)`` straight to ``run_command``'s ``role=``
        (never a guessed "owner" — that gate is a cost-cap security
        boundary, see ``TestEventTurnsCarryTheirRole``) and separately
        decides, in :meth:`_per_user_workspace_for`, whether an unresolved
        role may fall back to "owner" for *workspace* purposes only.
        """
        if event.run_as:
            # Item jobs (dv-752a): the handler set the ASSIGNEE explicitly.
            # created_by on those rows is the creator, and running as them
            # would fire the reminder in the creator's workspace and memory.
            return event.run_as
        if event.item_id and len(event.target_chat_ids) == 1:
            # Item job fired without an item service wired: its single
            # target chat is the assignee, never the creator in created_by.
            return event.target_chat_ids[0]
        if event.created_by:
            return event.created_by
        if len(event.target_chat_ids) == 1:
            return event.target_chat_ids[0]
        return self.default_user_id

    def _per_user_workspace_for(
        self, working_dir: Path, owner_id: int, owner_role: Optional[str]
    ) -> Path:
        """Wrap a stored profile-ROOT dir into the job owner's own workspace.

        Scheduled jobs persist a raw ``working_directory``. On a per-user
        profile the root is nobody's workspace — the memory scope resolves
        per-user via the registry fallback, but *files* written by the job
        would land at the root, invisible from every user's cwd (observed:
        ``daily-recovery-log`` writing the coach root, dv-0fbb plan §3).
        Mirrors the ``_handle_health_webhook`` wrap: on shared profiles,
        per-user subdirs, and unknown dirs this is a no-op. ``owner_id`` /
        ``owner_role`` come from :meth:`_resolve_job_owner` /
        ``self._role_for`` — a job's real audience, not necessarily
        ``default_user_id`` (dv-3e91).

        An unresolved role (``owner_role is None``) only falls back to
        "owner" when ``owner_id`` IS ``default_user_id`` — dv-0fbb's
        original guess, kept for the common case where the store simply
        has no cached role yet for the bot's own owner. For an owner
        resolved from ``created_by``/``target_chat_ids`` (open question 1),
        the same guess could silently split a non-owner's files into
        ``guests/<id>``, so this skips the wrap and logs instead — worse to
        stay wrong-but-safe at the profile root than to guess.
        """
        if self.profile_registry is None or not owner_id:
            return working_dir
        profile, ws_user_id = self.profile_registry.resolve_working_dir(
            Path(str(working_dir))
        )
        if profile is None or ws_user_id is not None:
            return working_dir
        role = owner_role
        if role is None:
            if owner_id == self.default_user_id:
                role = "owner"
            else:
                logger.warning(
                    "scheduled job owner has no resolvable role, "
                    "leaving working dir at profile root",
                    owner_id=owner_id,
                    working_dir=str(working_dir),
                )
                return working_dir
        from ..profiles.registry import ensure_workspace_for

        return ensure_workspace_for(profile, role, owner_id)

    async def _owner_model_pref(self, user_id: int) -> Optional[str]:
        """Mirror the Telegram path's model resolution for event-driven turns:
        the owner's saved tier pref, else the configured agent_model, else
        None (SDK falls back to CLAUDE_MODEL). Without this, every scheduled
        turn ran on the config default — a different model than the
        conversation it landed in, which also cold-starts the prompt cache
        twice (caches are per-model)."""
        prefs = None
        if self.storage is not None:
            try:
                prefs = await self.storage.preferences.get(user_id)
            except Exception as e:
                logger.warning("model pref lookup failed", error=str(e))
        configured = (prefs or {}).get("agent_model") or self.default_agent_model
        return configured if configured in ("sonnet", "opus") else None

    async def handle_scheduled(self, event: Event) -> None:
        """Process a scheduled event through Claude."""
        if not isinstance(event, ScheduledEvent):
            return

        if event.item_id and self.items is not None:
            await self._handle_item_event(event)
            return

        if event.delivery == "message":
            await self._deliver_verbatim(event)
            return

        await self._run_agent_job(event)

    async def _handle_item_event(self, event: ScheduledEvent) -> None:
        """Fire a family item (dv-752a): live text, run as the assignee.

        A job whose item is no longer open (done/cancelled from either side)
        is dropped instead of delivered. The fire is recorded on the item so
        both sides' next turn sees it (turn-start injection).
        """
        items = self.items
        item = await items.repo.get(event.item_id)
        if item is None or item.status != "open":
            logger.info(
                "item job skipped: item not open",
                item_id=event.item_id,
                status=getattr(item, "status", None),
            )
            if items.scheduler is not None:
                await items.scheduler.remove_jobs_for_item(event.item_id)
            return
        cross = item.creator_id != item.assignee_id
        sender = None
        if cross:
            names = await items.names(item.assignee_id)
            sender = names.get(item.creator_id) or f"id {item.creator_id}"
        fired = dataclasses.replace(
            event,
            run_as=item.assignee_id,
            prompt=item.text,
            target_chat_ids=[item.assignee_id],
        )
        logger.info(
            "Firing family item",
            item_id=item.id,
            assignee_id=item.assignee_id,
            cross_member=cross,
            deliver=item.deliver,
        )
        if item.deliver == "agent":
            origin = f"set by {sender}" if sender else "the user set for themselves"
            fired = dataclasses.replace(
                fired,
                prompt=(
                    f"[Reminder #{item.id} ({origin}) is due now. Deliver it to "
                    f"the user in your own words, briefly.]\n{item.text}"
                ),
            )
            await self._run_agent_job(fired)
        else:
            await self._deliver_verbatim(
                fired, header=f"⏰ от {sender}" if sender else None, sender=sender
            )
        await items.mark_fired(item)

    async def _run_agent_job(self, event: ScheduledEvent) -> None:
        """Run a scheduled job as an agent turn for its owner."""
        logger.info(
            "Processing scheduled event through agent",
            job_id=event.job_id,
            job_name=event.job_name,
        )

        prompt = event.prompt
        if event.skill_name:
            prompt = (
                f"/{event.skill_name}\n\n{prompt}" if prompt else f"/{event.skill_name}"
            )

        owner_id = self._resolve_job_owner(event)
        owner_role = self._role_for(owner_id)
        working_dir = event.working_directory or self.default_working_directory
        working_dir = self._per_user_workspace_for(working_dir, owner_id, owner_role)

        # Carry the job's audience into the agent run so in-process agent
        # tools (notify / schedule_add) fired during this turn keep targeting
        # the chat that owns the job (e.g. a guest's recurring reminder).
        requester_chat_id = event.target_chat_ids[0] if event.target_chat_ids else None
        report_source = scheduled_report_source(event)
        # A job-finish digest is ledgered with the job so a reply to it maps
        # back to the job (dv-c41e).
        job_ref: Optional[str] = None
        job_profile_slug: Optional[str] = None
        if report_source == "bgjob:digest" and ":" in event.job_name:
            from ..bot.orchestration.job_context import job_profile

            jid = event.job_name.split(":", 1)[1]
            job_ref = f"job:{jid}"
            job_profile_slug = job_profile(jid)

        try:
            # Loop guard (dv-752a): while this scheduler-driven turn runs, the
            # item tools refuse to create items for other family members.
            label = f"job {event.job_name}" if event.job_name else "scheduled job"
            async with (
                self._turn_lock(owner_id, label, working_dir),
                self._busy_notice(owner_id, label),
            ):
                with automated_turn(owner_id):
                    response = await get_router().run_turn(
                        self.claude,
                        purpose="scheduled",
                        attended=False,
                        prompt=prompt,
                        working_directory=working_dir,
                        user_id=owner_id,
                        requester_chat_id=requester_chat_id,
                        role=owner_role,
                        model=await self._owner_model_pref(owner_id),
                    )

            if response.content:
                for chat_id in event.target_chat_ids:
                    await self.event_bus.publish(
                        AgentResponseEvent(
                            chat_id=chat_id,
                            text=response.content,
                            parse_mode="markdown",
                            originating_event_id=event.id,
                            source=report_source,
                            ledger_ref=job_ref,
                            ledger_profile_slug=job_profile_slug,
                        )
                    )

                # Also broadcast to default chats if no targets specified
                if not event.target_chat_ids:
                    await self.event_bus.publish(
                        AgentResponseEvent(
                            chat_id=0,
                            text=response.content,
                            parse_mode="markdown",
                            originating_event_id=event.id,
                            source=report_source,
                            ledger_ref=job_ref,
                            ledger_profile_slug=job_profile_slug,
                        )
                    )
        except Exception:
            logger.exception(
                "Agent execution failed for scheduled event",
                job_id=event.job_id,
                event_id=event.id,
            )

    async def _deliver_verbatim(
        self,
        event: ScheduledEvent,
        header: Optional[str] = None,
        sender: Optional[str] = None,
    ) -> None:
        """Deliver a scheduled reminder's text straight to Telegram — no LLM
        turn. Three side channels keep the agent able to see it later:
        a messages row in the profile's current session (recall + window
        reseed read from there), a sent_messages ledger entry (a Telegram
        reply to the reminder resolves and reroutes through it), and a
        pending note armed for the next turn in the same workspace (a bare
        non-reply answer still gets the referent)."""
        logger.info(
            "Delivering scheduled reminder verbatim",
            job_id=event.job_id,
            job_name=event.job_name,
        )
        owner_id = self._resolve_job_owner(event)
        owner_role = self._role_for(owner_id)
        working_dir = event.working_directory or self.default_working_directory
        working_dir = self._per_user_workspace_for(working_dir, owner_id, owner_role)
        user_id = owner_id

        profile_slug = None
        agent_id = None
        if self.profile_registry is not None:
            from ..profiles.registry import agent_id_for_working_dir

            profile, _ = self.profile_registry.resolve_working_dir(working_dir)
            profile_slug = profile.slug if profile else None
            agent_id = agent_id_for_working_dir(
                self.profile_registry, working_dir, user_id
            )

        text = event.prompt
        if header is None:
            header = f"⏰ {profile_slug}" if profile_slug else "⏰ reminder"
        origin = f" from {sender}" if sender else ""
        delivered_at = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")

        saved_message_id: Optional[int] = None
        if self.storage is not None:
            try:
                sessions = await self.storage.sessions.get_sessions_by_project(
                    str(working_dir)
                )
                session = next((s for s in sessions if s.user_id == user_id), None)
                if session is not None:
                    window_id = None
                    if self.window_repo is not None:
                        state = await self.window_repo.get_or_create(
                            user_id, working_dir
                        )
                        window_id = state.window_id
                    from ..storage.models import MessageModel

                    saved_message_id = await self.storage.messages.save_message(
                        MessageModel(
                            session_id=session.session_id,
                            user_id=user_id,
                            timestamp=datetime.now(UTC),
                            prompt=f"[scheduled reminder "
                            f"'{event.job_name}' delivered {delivered_at}]",
                            response=text,
                            window_id=window_id,
                        )
                    )
            except Exception as e:
                logger.warning("reminder messages-row persist failed", error=str(e))
            try:
                await self.storage.pending_notes.add(
                    user_id,
                    str(working_dir),
                    f"[a scheduled reminder{origin} was delivered to the user at "
                    f"{delivered_at}, without an agent turn — if their "
                    f"message reads like an answer, this is the question: "
                    f'"{text}"]',
                )
            except Exception as e:
                logger.warning("reminder pending note failed", error=str(e))

        for chat_id in event.target_chat_ids or [0]:
            await self.event_bus.publish(
                AgentResponseEvent(
                    chat_id=chat_id,
                    text=f"{header}\n\n{text}",
                    parse_mode=None,
                    originating_event_id=event.id,
                    source="reminder",
                    ledger_kind="scheduled_reminder",
                    ledger_profile_slug=agent_id,
                    ledger_messages_message_id=saved_message_id,
                )
            )

    def _build_webhook_prompt(self, event: WebhookEvent) -> str:
        """Build a Claude prompt from a webhook event."""
        payload_summary = self._summarize_payload(event.payload)

        return (
            f"A {event.provider} webhook event occurred.\n"
            f"Event type: {event.event_type_name}\n"
            f"Payload summary:\n{payload_summary}\n\n"
            f"Analyze this event and provide a concise summary. "
            f"Highlight anything that needs my attention."
        )

    def _summarize_payload(self, payload: Dict[str, Any], max_depth: int = 2) -> str:
        """Create a readable summary of a webhook payload."""
        lines: List[str] = []
        self._flatten_dict(payload, lines, max_depth=max_depth)
        # Cap at 2000 chars to keep prompt reasonable
        summary = "\n".join(lines)
        if len(summary) > 2000:
            summary = summary[:2000] + "\n... (truncated)"
        return summary

    def _flatten_dict(
        self,
        data: Any,
        lines: list,
        prefix: str = "",
        depth: int = 0,
        max_depth: int = 2,
    ) -> None:
        """Flatten a nested dict into key: value lines."""
        if depth >= max_depth:
            lines.append(f"{prefix}: ...")
            return

        if isinstance(data, dict):
            for key, value in data.items():
                full_key = f"{prefix}.{key}" if prefix else key
                if isinstance(value, (dict, list)):
                    self._flatten_dict(value, lines, full_key, depth + 1, max_depth)
                else:
                    val_str = str(value)
                    if len(val_str) > 200:
                        val_str = val_str[:200] + "..."
                    lines.append(f"{full_key}: {val_str}")
        elif isinstance(data, list):
            lines.append(f"{prefix}: [{len(data)} items]")
            for i, item in enumerate(data[:3]):  # Show first 3 items
                self._flatten_dict(item, lines, f"{prefix}[{i}]", depth + 1, max_depth)
        else:
            lines.append(f"{prefix}: {data}")
