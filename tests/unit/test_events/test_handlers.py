"""Tests for event handlers."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.events.bus import EventBus
from src.events.handlers import AgentHandler
from src.events.types import AgentResponseEvent, ScheduledEvent, WebhookEvent


@pytest.fixture
def event_bus() -> EventBus:
    return EventBus()


@pytest.fixture
def mock_claude() -> AsyncMock:
    mock = AsyncMock()
    mock.run_command = AsyncMock()
    return mock


@pytest.fixture
def agent_handler(event_bus: EventBus, mock_claude: AsyncMock) -> AgentHandler:
    handler = AgentHandler(
        event_bus=event_bus,
        claude_integration=mock_claude,
        default_working_directory=Path("/tmp/test"),
        default_user_id=42,
    )
    handler.register()
    return handler


class TestAgentHandler:
    """Tests for AgentHandler."""

    async def test_webhook_event_triggers_claude(
        self, event_bus: EventBus, mock_claude: AsyncMock, agent_handler: AgentHandler
    ) -> None:
        """Webhook events are processed through Claude."""
        mock_response = MagicMock()
        mock_response.content = "Analysis complete"
        mock_claude.run_command.return_value = mock_response

        published: list = []
        original_publish = event_bus.publish

        async def capture_publish(event):  # type: ignore[no-untyped-def]
            published.append(event)
            await original_publish(event)

        event_bus.publish = capture_publish  # type: ignore[assignment]

        event = WebhookEvent(
            provider="github",
            event_type_name="push",
            payload={"ref": "refs/heads/main"},
            delivery_id="del-1",
        )

        await agent_handler.handle_webhook(event)

        mock_claude.run_command.assert_called_once()
        call_kwargs = mock_claude.run_command.call_args
        assert "github" in call_kwargs.kwargs["prompt"].lower()

        # Should publish an AgentResponseEvent
        response_events = [e for e in published if isinstance(e, AgentResponseEvent)]
        assert len(response_events) == 1
        assert response_events[0].text == "Analysis complete"

    async def test_scheduled_event_triggers_claude(
        self, event_bus: EventBus, mock_claude: AsyncMock, agent_handler: AgentHandler
    ) -> None:
        """Scheduled events invoke Claude with the job's prompt."""
        mock_response = MagicMock()
        mock_response.content = "Standup summary"
        mock_claude.run_command.return_value = mock_response

        published: list = []
        original_publish = event_bus.publish

        async def capture_publish(event):  # type: ignore[no-untyped-def]
            published.append(event)
            await original_publish(event)

        event_bus.publish = capture_publish  # type: ignore[assignment]

        event = ScheduledEvent(
            job_name="standup",
            prompt="Generate daily standup",
            target_chat_ids=[100],
        )

        await agent_handler.handle_scheduled(event)

        mock_claude.run_command.assert_called_once()
        assert "standup" in mock_claude.run_command.call_args.kwargs["prompt"].lower()

        response_events = [e for e in published if isinstance(e, AgentResponseEvent)]
        assert len(response_events) == 1
        assert response_events[0].chat_id == 100

    async def test_scheduled_event_threads_requester_chat_id(
        self, event_bus: EventBus, mock_claude: AsyncMock, agent_handler: AgentHandler
    ) -> None:
        """The job's target chat is passed to run_command as requester context."""
        mock_response = MagicMock()
        mock_response.content = "Done"
        mock_claude.run_command.return_value = mock_response

        event = ScheduledEvent(
            job_name="reminder",
            prompt="water the plants",
            target_chat_ids=[777],
        )

        await agent_handler.handle_scheduled(event)

        kwargs = mock_claude.run_command.call_args.kwargs
        assert kwargs["requester_chat_id"] == 777

    async def test_scheduled_event_without_targets_has_no_requester(
        self, event_bus: EventBus, mock_claude: AsyncMock, agent_handler: AgentHandler
    ) -> None:
        """No target chats -> requester_chat_id None (broadcast fallback)."""
        mock_response = MagicMock()
        mock_response.content = "Done"
        mock_claude.run_command.return_value = mock_response

        event = ScheduledEvent(job_name="sys", prompt="health check")

        await agent_handler.handle_scheduled(event)

        kwargs = mock_claude.run_command.call_args.kwargs
        assert kwargs["requester_chat_id"] is None

    async def test_webhook_event_has_no_requester_chat(
        self, event_bus: EventBus, mock_claude: AsyncMock, agent_handler: AgentHandler
    ) -> None:
        """Webhook runs carry no requester context (broadcast fallback)."""
        mock_response = MagicMock()
        mock_response.content = "Summary"
        mock_claude.run_command.return_value = mock_response

        event = WebhookEvent(provider="github", event_type_name="push", payload={})

        await agent_handler.handle_webhook(event)

        kwargs = mock_claude.run_command.call_args.kwargs
        assert kwargs.get("requester_chat_id") is None

    async def test_scheduled_event_with_skill(
        self, event_bus: EventBus, mock_claude: AsyncMock, agent_handler: AgentHandler
    ) -> None:
        """Scheduled events with skill_name prepend the skill invocation."""
        mock_response = MagicMock()
        mock_response.content = "Done"
        mock_claude.run_command.return_value = mock_response

        event = ScheduledEvent(
            job_name="standup",
            prompt="morning report",
            skill_name="daily-standup",
            target_chat_ids=[100],
        )

        await agent_handler.handle_scheduled(event)

        prompt = mock_claude.run_command.call_args.kwargs["prompt"]
        assert prompt.startswith("/daily-standup")
        assert "morning report" in prompt

    async def test_claude_error_does_not_propagate(
        self, event_bus: EventBus, mock_claude: AsyncMock, agent_handler: AgentHandler
    ) -> None:
        """Agent errors are logged but don't crash the handler."""
        mock_claude.run_command.side_effect = RuntimeError("SDK error")

        event = WebhookEvent(
            provider="github",
            event_type_name="push",
            payload={},
        )

        # Should not raise
        await agent_handler.handle_webhook(event)

    def test_build_webhook_prompt(self, agent_handler: AgentHandler) -> None:
        """Webhook prompt includes provider and event info."""
        event = WebhookEvent(
            provider="github",
            event_type_name="pull_request",
            payload={"action": "opened", "number": 42},
        )

        prompt = agent_handler._build_webhook_prompt(event)
        assert "github" in prompt.lower()
        assert "pull_request" in prompt
        assert "action: opened" in prompt

    def test_payload_summary_truncation(self, agent_handler: AgentHandler) -> None:
        """Large payloads are truncated in the summary."""
        big_payload = {"key": "x" * 3000}
        summary = agent_handler._summarize_payload(big_payload)
        assert len(summary) <= 2100  # 2000 + truncation message


class TestAgentHandlerTurnLock:
    """Event-driven turns share the per-user turn lock with the orchestrator."""

    async def test_scheduled_turn_waits_for_held_lock(
        self, event_bus: EventBus, mock_claude: AsyncMock
    ) -> None:
        import asyncio

        from src.utils.turn_locks import TurnLocks

        locks = TurnLocks()
        handler = AgentHandler(
            event_bus=event_bus,
            claude_integration=mock_claude,
            default_working_directory=Path("/tmp/test"),
            default_user_id=42,
            turn_locks=locks,
        )
        mock_response = MagicMock()
        mock_response.content = "done"
        mock_claude.run_command.return_value = mock_response

        event = ScheduledEvent(job_id="j1", job_name="n", prompt="p")

        # Simulate a Telegram turn in flight for the same user.
        await locks.get(42).acquire()
        task = asyncio.create_task(handler.handle_scheduled(event))
        await asyncio.sleep(0.05)
        assert not mock_claude.run_command.called  # still queued behind the turn

        locks.get(42).release()
        await task
        assert mock_claude.run_command.called

    async def test_no_turn_locks_still_runs(
        self, event_bus: EventBus, mock_claude: AsyncMock, agent_handler: AgentHandler
    ) -> None:
        mock_response = MagicMock()
        mock_response.content = "done"
        mock_claude.run_command.return_value = mock_response
        await agent_handler.handle_scheduled(
            ScheduledEvent(job_id="j1", job_name="n", prompt="p")
        )
        assert mock_claude.run_command.called


class TestEventTurnsCarryTheirRole:
    """Scheduled/webhook turns must resolve the owner's role.

    They never have a live Telegram session, so AuthManager.get_user_role
    returns None for them and every event-driven turn ran as "no role known"
    = capped at CLAUDE_MAX_COST_PER_REQUEST. A turn that hit that cap was
    truncated with no error surfaced, so a 3am scheduled job could quietly
    return half an answer.
    """

    @pytest.fixture
    def user_store(self) -> MagicMock:
        store = MagicMock()
        store.get_role_cached = MagicMock(
            side_effect=lambda uid: {42: "owner", 7: "guest"}.get(uid)
        )
        return store

    def _handler(self, event_bus, mock_claude, user_store, user_id=42):
        return AgentHandler(
            event_bus=event_bus,
            claude_integration=mock_claude,
            default_working_directory=Path("/tmp/test"),
            default_user_id=user_id,
            user_store=user_store,
        )

    async def test_scheduled_turn_passes_owner_role(
        self, event_bus, mock_claude, user_store
    ) -> None:
        handler = self._handler(event_bus, mock_claude, user_store)
        mock_claude.run_command.return_value = MagicMock(content="done")

        await handler.handle_scheduled(
            ScheduledEvent(job_id="j1", job_name="n", prompt="p")
        )

        assert mock_claude.run_command.call_args.kwargs["role"] == "owner"

    async def test_webhook_turn_passes_role(
        self, event_bus, mock_claude, user_store
    ) -> None:
        handler = self._handler(event_bus, mock_claude, user_store)
        mock_claude.run_command.return_value = MagicMock(content="done")

        await handler.handle_webhook(
            WebhookEvent(
                provider="github", event_type_name="push", payload={"ref": "main"}
            )
        )

        assert mock_claude.run_command.call_args.kwargs["role"] == "owner"

    async def test_guest_role_is_not_upgraded(
        self, event_bus, mock_claude, user_store
    ) -> None:
        handler = self._handler(event_bus, mock_claude, user_store, user_id=7)
        mock_claude.run_command.return_value = MagicMock(content="done")

        await handler.handle_scheduled(
            ScheduledEvent(job_id="j1", job_name="n", prompt="p")
        )

        assert mock_claude.run_command.call_args.kwargs["role"] == "guest"

    async def test_unknown_user_stays_capped(
        self, event_bus, mock_claude, user_store
    ) -> None:
        """No resolvable role must never mean "assume owner" — that would hand
        the uncapped path to anyone the store doesn't know."""
        handler = self._handler(event_bus, mock_claude, user_store, user_id=999)
        mock_claude.run_command.return_value = MagicMock(content="done")

        await handler.handle_scheduled(
            ScheduledEvent(job_id="j1", job_name="n", prompt="p")
        )

        assert mock_claude.run_command.call_args.kwargs["role"] is None

    async def test_no_user_store_keeps_previous_behavior(
        self, event_bus, mock_claude
    ) -> None:
        handler = AgentHandler(
            event_bus=event_bus,
            claude_integration=mock_claude,
            default_working_directory=Path("/tmp/test"),
            default_user_id=42,
        )
        mock_claude.run_command.return_value = MagicMock(content="done")

        await handler.handle_scheduled(
            ScheduledEvent(job_id="j1", job_name="n", prompt="p")
        )

        assert mock_claude.run_command.call_args.kwargs["role"] is None


class TestHealthExportLandsInTheUsersOwnWorkspace:
    """An Apple Health export must not be written to the shared profile root.

    coach is personal_memory:true — dropping one person's sleep/HRV data into
    the profile root puts it in the directory the other family member's agent
    reads, re-creating the leak dv-10e4 closes on the Telegram path.
    """

    async def test_export_writes_to_the_per_user_dir(
        self, event_bus, mock_claude, tmp_path
    ) -> None:
        from src.profiles.registry import ProfileDefinition, ProfileRegistry

        coach = ProfileDefinition(
            slug="coach",
            name="Coach",
            working_dir=tmp_path / "coach",
            memory_dir=tmp_path / "coach" / "memory",
            persona=tmp_path / "coach" / "persona.md",
            default=True,
            personal_memory=True,
        )
        store = MagicMock()
        store.get_role_cached = MagicMock(return_value="owner")
        handler = AgentHandler(
            event_bus=event_bus,
            claude_integration=mock_claude,
            default_working_directory=tmp_path,
            default_user_id=111111111,
            profile_registry=ProfileRegistry([coach]),
            health_export_user_id=111111111,
            user_store=store,
        )
        mock_claude.run_command.return_value = MagicMock(content="logged")

        await handler.handle_webhook(
            WebhookEvent(
                provider="health",
                event_type_name="export",
                payload={
                    "data": {
                        "metrics": [
                            {
                                "name": "heart rate variability",
                                "units": "ms",
                                "data": [
                                    {"date": "2026-08-03 08:00:00 +0000", "qty": 55}
                                ],
                            }
                        ]
                    }
                },
            )
        )

        assert mock_claude.run_command.called, "health export produced no turn"
        used = mock_claude.run_command.call_args.kwargs["working_directory"]
        assert used == tmp_path / "coach" / "users" / "111111111"


class TestScheduledJobLandsInTheUsersOwnWorkspace:
    """A scheduled job whose stored working_directory is a per-user profile
    ROOT must run in the default user's own workspace — otherwise its files
    land at the root, which no user's cwd can reach (dv-0fbb plan §3;
    observed with daily-recovery-log writing the coach root)."""

    def _handler(self, event_bus, mock_claude, tmp_path, *, personal_memory):
        from src.profiles.registry import ProfileDefinition, ProfileRegistry

        profile = ProfileDefinition(
            slug="general",
            name="General",
            working_dir=tmp_path / "general",
            memory_dir=tmp_path / "general" / "memory",
            persona=tmp_path / "general" / "persona.md",
            default=True,
            personal_memory=personal_memory,
        )
        store = MagicMock()
        store.get_role_cached = MagicMock(return_value="owner")
        return AgentHandler(
            event_bus=event_bus,
            claude_integration=mock_claude,
            default_working_directory=tmp_path,
            default_user_id=111111111,
            profile_registry=ProfileRegistry([profile]),
            user_store=store,
        )

    async def test_per_user_profile_root_is_wrapped(
        self, event_bus, mock_claude, tmp_path
    ) -> None:
        handler = self._handler(event_bus, mock_claude, tmp_path, personal_memory=True)
        mock_claude.run_command.return_value = MagicMock(content="done")

        await handler.handle_scheduled(
            ScheduledEvent(
                job_id="j1",
                job_name="career-status-checkin",
                prompt="check in",
                working_directory=tmp_path / "general",
            )
        )

        used = mock_claude.run_command.call_args.kwargs["working_directory"]
        assert used == tmp_path / "general" / "users" / "111111111"

    async def test_shared_profile_root_is_untouched(
        self, event_bus, mock_claude, tmp_path
    ) -> None:
        """`shared: true` profiles keep running at the root."""
        handler = self._handler(event_bus, mock_claude, tmp_path, personal_memory=False)
        mock_claude.run_command.return_value = MagicMock(content="done")

        await handler.handle_scheduled(
            ScheduledEvent(
                job_id="j2",
                job_name="shared-job",
                prompt="p",
                working_directory=tmp_path / "general",
            )
        )

        used = mock_claude.run_command.call_args.kwargs["working_directory"]
        assert used == tmp_path / "general"

    async def test_unknown_dir_is_untouched(
        self, event_bus, mock_claude, tmp_path
    ) -> None:
        """Dirs outside the registry (e.g. the repo itself) pass through."""
        handler = self._handler(event_bus, mock_claude, tmp_path, personal_memory=True)
        mock_claude.run_command.return_value = MagicMock(content="done")

        await handler.handle_scheduled(
            ScheduledEvent(
                job_id="j3",
                job_name="repo-job",
                prompt="p",
                working_directory=tmp_path / "elsewhere",
            )
        )

        used = mock_claude.run_command.call_args.kwargs["working_directory"]
        assert used == tmp_path / "elsewhere"


class TestScheduledJobRunsAsRealOwner:
    """dv-3e91: a job's stored working_directory/target_chat_ids may belong
    to someone other than the bot's default owner — handle_scheduled must
    run (and land) as THAT person, not silently as the default owner."""

    def _handler(self, event_bus, mock_claude, tmp_path, *, roles):
        from src.profiles.registry import ProfileDefinition, ProfileRegistry

        profile = ProfileDefinition(
            slug="coach",
            name="Coach",
            working_dir=tmp_path / "coach",
            memory_dir=tmp_path / "coach" / "memory",
            persona=tmp_path / "coach" / "persona.md",
            default=True,
            personal_memory=True,
        )
        store = MagicMock()
        store.get_role_cached = MagicMock(side_effect=lambda uid: roles.get(uid))
        return AgentHandler(
            event_bus=event_bus,
            claude_integration=mock_claude,
            default_working_directory=tmp_path,
            default_user_id=111111111,
            profile_registry=ProfileRegistry([profile]),
            user_store=store,
        )

    async def test_scheduled_run_does_not_read_owners_facts(
        self, event_bus, mock_claude, tmp_path
    ) -> None:
        """A job addressed to the wife (222222222) must run as her, in her
        own workspace — not in the bot owner's coach session, where her
        reminder turn could see (and leak) his health facts (dv-3e91
        evidence table: friday-weighin-reminder)."""
        handler = self._handler(
            event_bus,
            mock_claude,
            tmp_path,
            roles={111111111: "owner", 222222222: "owner"},
        )
        mock_claude.run_command.return_value = MagicMock(content="done")

        await handler.handle_scheduled(
            ScheduledEvent(
                job_id="friday-weighin",
                job_name="friday-weighin-reminder",
                prompt="check in",
                working_directory=tmp_path / "coach",
                target_chat_ids=[222222222],
            )
        )

        call = mock_claude.run_command.call_args.kwargs
        assert call["user_id"] == 222222222
        assert call["working_directory"] == tmp_path / "coach" / "users" / "222222222"

    async def test_created_by_takes_precedence_over_target_chat_ids(
        self, event_bus, mock_claude, tmp_path
    ) -> None:
        handler = self._handler(
            event_bus,
            mock_claude,
            tmp_path,
            roles={111111111: "owner", 222222222: "owner"},
        )
        mock_claude.run_command.return_value = MagicMock(content="done")

        await handler.handle_scheduled(
            ScheduledEvent(
                job_id="j",
                job_name="n",
                prompt="p",
                working_directory=tmp_path / "coach",
                target_chat_ids=[222222222],
                created_by=111111111,
            )
        )

        call = mock_claude.run_command.call_args.kwargs
        assert call["user_id"] == 111111111

    async def test_unresolvable_role_for_non_default_owner_skips_wrap(
        self, event_bus, mock_claude, tmp_path
    ) -> None:
        """Open question 1: guessing "owner" for someone the role store
        doesn't know could split their files into the wrong tree — safer to
        leave the run at the stored root (and still run as the real owner,
        just capped, per TestEventTurnsCarryTheirRole)."""
        handler = self._handler(
            event_bus, mock_claude, tmp_path, roles={111111111: "owner"}
        )
        mock_claude.run_command.return_value = MagicMock(content="done")

        await handler.handle_scheduled(
            ScheduledEvent(
                job_id="j",
                job_name="n",
                prompt="p",
                working_directory=tmp_path / "coach",
                target_chat_ids=[999],
            )
        )

        call = mock_claude.run_command.call_args.kwargs
        assert call["user_id"] == 999
        assert call["role"] is None
        assert call["working_directory"] == tmp_path / "coach"

    async def test_no_resolvable_owner_falls_back_to_default_user(
        self, event_bus, mock_claude, tmp_path
    ) -> None:
        """No created_by, no single target chat (dv-3e91 open question 2) —
        every currently-live job has a resolvable single owner, so this is
        defensive: keep dv-0fbb's existing default-user behavior."""
        handler = self._handler(
            event_bus, mock_claude, tmp_path, roles={111111111: "owner"}
        )
        mock_claude.run_command.return_value = MagicMock(content="done")

        await handler.handle_scheduled(
            ScheduledEvent(
                job_id="j",
                job_name="n",
                prompt="p",
                working_directory=tmp_path / "coach",
                target_chat_ids=[111, 222],
            )
        )

        call = mock_claude.run_command.call_args.kwargs
        assert call["user_id"] == 111111111
        assert call["working_directory"] == tmp_path / "coach" / "users" / "111111111"


class TestVerbatimDelivery:
    """delivery='message' scheduled events skip the LLM entirely."""

    def _storage(self, session=None, saved_id=901):
        storage = AsyncMock()
        storage.sessions.get_sessions_by_project = AsyncMock(
            return_value=[session] if session else []
        )
        storage.messages.save_message = AsyncMock(return_value=saved_id)
        storage.pending_notes.add = AsyncMock()
        storage.preferences.get = AsyncMock(return_value=None)
        return storage

    async def _capture(self, event_bus):
        published: list = []
        original_publish = event_bus.publish

        async def capture_publish(event):  # type: ignore[no-untyped-def]
            published.append(event)
            await original_publish(event)

        event_bus.publish = capture_publish  # type: ignore[assignment]
        return published

    async def test_verbatim_skips_llm_and_publishes_ledgered(
        self, event_bus: EventBus, mock_claude: AsyncMock
    ) -> None:
        """The job's owner is resolved from target_chat_ids (555), NOT
        default_user_id (42) — this is the dv-3e91 wrong-user bug: a
        reminder addressed to someone else must persist under their own
        session/notes, not the bot's default owner's."""
        from types import SimpleNamespace

        session = SimpleNamespace(user_id=555, session_id="sess-1")
        storage = self._storage(session=session)
        handler = AgentHandler(
            event_bus=event_bus,
            claude_integration=mock_claude,
            default_working_directory=Path("/tmp/test"),
            default_user_id=42,
            storage=storage,
        )
        published = await self._capture(event_bus)

        await handler.handle_scheduled(
            ScheduledEvent(
                job_id="j1",
                job_name="shoulder-check",
                prompt="Как плечо?",
                working_directory=Path("/tmp/test"),
                target_chat_ids=[555],
                delivery="message",
            )
        )

        mock_claude.run_command.assert_not_called()
        responses = [e for e in published if isinstance(e, AgentResponseEvent)]
        assert len(responses) == 1
        assert responses[0].chat_id == 555
        assert "Как плечо?" in responses[0].text
        assert responses[0].parse_mode is None
        assert responses[0].ledger_kind == "scheduled_reminder"
        assert responses[0].ledger_messages_message_id == 901

        # Persisted as a messages row in the workspace's session…
        saved = storage.messages.save_message.await_args.args[0]
        assert saved.session_id == "sess-1"
        assert saved.user_id == 555
        assert saved.response == "Как плечо?"
        assert "shoulder-check" in saved.prompt
        # …and armed as a pending note for the next turn there — for the
        # job's real owner (555), not the bot's default owner (42).
        note_args = storage.pending_notes.add.await_args.args
        assert note_args[0] == 555
        assert "Как плечо?" in note_args[2]

    async def test_verbatim_without_session_still_delivers(
        self, event_bus: EventBus, mock_claude: AsyncMock
    ) -> None:
        storage = self._storage(session=None)
        handler = AgentHandler(
            event_bus=event_bus,
            claude_integration=mock_claude,
            default_working_directory=Path("/tmp/test"),
            default_user_id=42,
            storage=storage,
        )
        published = await self._capture(event_bus)

        await handler.handle_scheduled(
            ScheduledEvent(
                job_name="r",
                prompt="ping",
                working_directory=Path("/tmp/test"),
                target_chat_ids=[555],
                delivery="message",
            )
        )

        storage.messages.save_message.assert_not_awaited()
        responses = [e for e in published if isinstance(e, AgentResponseEvent)]
        assert len(responses) == 1
        assert responses[0].ledger_messages_message_id is None


class TestScheduledModelPref:
    """Agent-path scheduled turns mirror the Telegram model resolution."""

    def _handler(self, event_bus, mock_claude, prefs, default_model=None):
        storage = AsyncMock()
        storage.preferences.get = AsyncMock(return_value=prefs)
        return AgentHandler(
            event_bus=event_bus,
            claude_integration=mock_claude,
            default_working_directory=Path("/tmp/test"),
            default_user_id=42,
            storage=storage,
            default_agent_model=default_model,
        )

    async def test_owner_pref_passed_to_run_command(
        self, event_bus: EventBus, mock_claude: AsyncMock
    ) -> None:
        mock_claude.run_command.return_value = MagicMock(content="ok")
        handler = self._handler(event_bus, mock_claude, {"agent_model": "opus"})

        await handler.handle_scheduled(
            ScheduledEvent(job_name="r", prompt="p", target_chat_ids=[1])
        )

        assert mock_claude.run_command.call_args.kwargs["model"] == "opus"

    async def test_config_default_used_when_no_pref(
        self, event_bus: EventBus, mock_claude: AsyncMock
    ) -> None:
        mock_claude.run_command.return_value = MagicMock(content="ok")
        handler = self._handler(event_bus, mock_claude, None, default_model="sonnet")

        await handler.handle_scheduled(
            ScheduledEvent(job_name="r", prompt="p", target_chat_ids=[1])
        )

        assert mock_claude.run_command.call_args.kwargs["model"] == "sonnet"

    async def test_non_tier_pref_falls_back_to_none(
        self, event_bus: EventBus, mock_claude: AsyncMock
    ) -> None:
        mock_claude.run_command.return_value = MagicMock(content="ok")
        handler = self._handler(
            event_bus, mock_claude, {"agent_model": "gpt-5.2-codex"}
        )

        await handler.handle_scheduled(
            ScheduledEvent(job_name="r", prompt="p", target_chat_ids=[1])
        )

        assert mock_claude.run_command.call_args.kwargs["model"] is None

    async def test_fable_pref_passed_to_run_command(
        self, event_bus: EventBus, mock_claude: AsyncMock
    ) -> None:
        """dv-d63d: fable is a Claude tier for scheduled turns too."""
        mock_claude.run_command.return_value = MagicMock(content="ok")
        handler = self._handler(event_bus, mock_claude, {"agent_model": "fable"})

        await handler.handle_scheduled(
            ScheduledEvent(job_name="r", prompt="p", target_chat_ids=[1])
        )

        assert mock_claude.run_command.call_args.kwargs["model"] == "fable"

    async def test_fable_config_default_used_when_no_pref(
        self, event_bus: EventBus, mock_claude: AsyncMock
    ) -> None:
        mock_claude.run_command.return_value = MagicMock(content="ok")
        handler = self._handler(event_bus, mock_claude, None, default_model="fable")

        await handler.handle_scheduled(
            ScheduledEvent(job_name="r", prompt="p", target_chat_ids=[1])
        )

        assert mock_claude.run_command.call_args.kwargs["model"] == "fable"

    def test_handlers_has_no_tier_list_of_its_own(self) -> None:
        """The tier gate is one list (protocol.py); handlers.py must not keep
        a second literal copy that drifts when a tier is added."""
        import src.events.handlers as handlers_mod

        source = Path(handlers_mod.__file__).read_text()
        assert '("sonnet", "opus")' not in source
        assert "('sonnet', 'opus')" not in source


class TestScheduledTurnDeliversSentImages:
    """dv-ff39: send_image_to_user / send_file_to_user calls made in a
    scheduled (job-finish) or webhook turn reach the user.

    The chat path collects them through the stream intercept; the event
    path must read ``response.tools_used`` after the turn and carry the
    validated files on ``AgentResponseEvent.image_paths``.
    """

    @staticmethod
    def _png(p: Path) -> Path:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 200)
        return p

    @staticmethod
    def _call(name: str, path: Path, **extra) -> dict:  # type: ignore[no-untyped-def]
        entry = {"name": name, "input": {"file_path": str(path), "caption": ""}}
        entry.update(extra)
        return entry

    def _gymbro(self, event_bus, mock_claude, tmp_path):  # type: ignore[no-untyped-def]
        from src.profiles.registry import ProfileDefinition, ProfileRegistry

        profile = ProfileDefinition(
            slug="gymbro",
            name="GymBro",
            working_dir=tmp_path / "gymbro",
            memory_dir=tmp_path / "gymbro" / "memory",
            persona=tmp_path / "gymbro" / "persona.md",
            personal_memory=False,
        )
        (tmp_path / "gymbro").mkdir(parents=True, exist_ok=True)
        store = MagicMock()
        store.get_role_cached = MagicMock(return_value="owner")
        return AgentHandler(
            event_bus=event_bus,
            claude_integration=mock_claude,
            default_working_directory=tmp_path / "gymbro",
            default_user_id=111111111,
            profile_registry=ProfileRegistry([profile]),
            user_store=store,
        )

    @staticmethod
    def _capture(event_bus: EventBus) -> list:
        published: list = []

        async def capture_publish(event):  # type: ignore[no-untyped-def]
            published.append(event)

        event_bus.publish = capture_publish  # type: ignore[assignment]
        return published

    @staticmethod
    def _job_finish(tmp_path: Path) -> ScheduledEvent:
        return ScheduledEvent(
            job_id="jf1",
            job_name="job-finish:3ffad5b69afc",
            prompt="job finished",
            working_directory=tmp_path / "gymbro",
            target_chat_ids=[111111111],
        )

    @staticmethod
    def _resolved(paths) -> list:  # type: ignore[no-untyped-def]
        return [str(Path(p).resolve()) for p in paths]

    async def test_job_finish_turn_delivers_eight_pngs_to_gymbro_topic(
        self, event_bus, mock_claude, tmp_path
    ) -> None:
        handler = self._gymbro(event_bus, mock_claude, tmp_path)
        design = tmp_path / "gymbro" / "design" / "2026-10-26"
        pngs = [self._png(design / f"a{i}_dark.png") for i in range(8)]
        mock_claude.run_command.return_value = MagicMock(
            content="Here are the 8 designs",
            tools_used=[
                self._call("mcp__telegram__send_image_to_user", p) for p in pngs
            ],
        )
        published = self._capture(event_bus)

        await handler.handle_scheduled(self._job_finish(tmp_path))

        events = [e for e in published if isinstance(e, AgentResponseEvent)]
        assert len(events) == 1
        ev = events[0]
        assert ev.text == "Here are the 8 designs"
        assert ev.source == "bgjob:digest"
        assert ev.deliver_profile == "gymbro"
        assert self._resolved(ev.image_paths) == self._resolved(pngs)

        # ...and the notification service sends them as one album into the
        # GymBro topic (message_thread_id).
        from src.notifications.service import NotificationService

        bot = MagicMock()
        bot.send_message = AsyncMock(return_value=MagicMock(chat=MagicMock(id=-100)))
        bot.send_photo = AsyncMock()
        bot.send_media_group = AsyncMock()
        bot.send_document = AsyncMock()
        svc = NotificationService(EventBus(), bot, default_chat_ids=[-100])
        await svc._rate_limited_send(-100, ev, thread_id=77)
        bot.send_media_group.assert_awaited_once()
        kw = bot.send_media_group.call_args.kwargs
        assert kw["message_thread_id"] == 77
        assert len(kw["media"]) == 8

    async def test_invalid_send_image_calls_dropped_valid_kept(
        self, event_bus, mock_claude, tmp_path
    ) -> None:
        handler = self._gymbro(event_bus, mock_claude, tmp_path)
        good1 = self._png(tmp_path / "gymbro" / "design" / "good1.png")
        good2 = self._png(tmp_path / "gymbro" / "design" / "good2.png")
        outside = self._png(tmp_path / "elsewhere" / "outside.png")
        missing = tmp_path / "gymbro" / "design" / "missing.png"
        errored = self._png(tmp_path / "gymbro" / "design" / "errored.png")
        mock_claude.run_command.return_value = MagicMock(
            content="sent",
            tools_used=[
                self._call("mcp__telegram__send_image_to_user", good1),
                self._call("mcp__telegram__send_image_to_user", outside),
                self._call("mcp__telegram__send_image_to_user", missing),
                self._call(
                    "mcp__telegram__send_image_to_user",
                    errored,
                    error="path outside allowed roots",
                ),
                self._call("send_image_to_user", good2),
                {"name": "Read", "input": {"file_path": str(good1)}},
            ],
        )
        published = self._capture(event_bus)

        await handler.handle_scheduled(self._job_finish(tmp_path))

        events = [e for e in published if isinstance(e, AgentResponseEvent)]
        assert len(events) == 1
        assert self._resolved(events[0].image_paths) == self._resolved([good1, good2])

    async def test_image_only_turn_still_delivers_images(
        self, event_bus, mock_claude, tmp_path
    ) -> None:
        handler = self._gymbro(event_bus, mock_claude, tmp_path)
        img = self._png(tmp_path / "gymbro" / "design" / "only.png")
        mock_claude.run_command.return_value = MagicMock(
            content="",
            tools_used=[self._call("mcp__telegram__send_image_to_user", img)],
        )
        published = self._capture(event_bus)

        await handler.handle_scheduled(self._job_finish(tmp_path))

        events = [e for e in published if isinstance(e, AgentResponseEvent)]
        assert len(events) == 1
        assert self._resolved(events[0].image_paths) == self._resolved([img])

        # Delivery: Telegram rejects an empty text message; the photo must
        # still be sent.
        from telegram.error import BadRequest

        from src.notifications.service import NotificationService

        async def send_message(**kw):  # type: ignore[no-untyped-def]
            if not str(kw.get("text", "")).strip():
                raise BadRequest("Message text is empty")
            return MagicMock(chat=MagicMock(id=-100))

        bot = MagicMock()
        bot.send_message = AsyncMock(side_effect=send_message)
        bot.send_photo = AsyncMock()
        bot.send_media_group = AsyncMock()
        bot.send_document = AsyncMock()
        svc = NotificationService(EventBus(), bot, default_chat_ids=[-100])
        await svc._rate_limited_send(-100, events[0], thread_id=77)
        bot.send_photo.assert_awaited_once()
        assert bot.send_photo.call_args.kwargs["message_thread_id"] == 77

    async def test_send_file_pdf_in_scheduled_turn_delivered_as_document(
        self, event_bus, mock_claude, tmp_path
    ) -> None:
        handler = self._gymbro(event_bus, mock_claude, tmp_path)
        pdf = tmp_path / "gymbro" / "plan.pdf"
        pdf.write_bytes(b"%PDF-1.4\n" + b"0" * 100)
        mock_claude.run_command.return_value = MagicMock(
            content="plan attached",
            tools_used=[self._call("mcp__telegram__send_file_to_user", pdf)],
        )
        published = self._capture(event_bus)

        await handler.handle_scheduled(self._job_finish(tmp_path))

        events = [e for e in published if isinstance(e, AgentResponseEvent)]
        assert len(events) == 1
        assert self._resolved(events[0].image_paths) == self._resolved([pdf])

        from src.notifications.service import NotificationService

        bot = MagicMock()
        bot.send_message = AsyncMock(return_value=MagicMock(chat=MagicMock(id=-100)))
        bot.send_photo = AsyncMock()
        bot.send_media_group = AsyncMock()
        bot.send_document = AsyncMock()
        svc = NotificationService(EventBus(), bot, default_chat_ids=[-100])
        await svc._rate_limited_send(-100, events[0], thread_id=77)
        bot.send_document.assert_awaited_once()
        assert bot.send_document.call_args.kwargs["filename"] == "plan.pdf"
        bot.send_photo.assert_not_awaited()

    async def test_webhook_turn_with_send_image_delivers_image(
        self, event_bus, mock_claude, tmp_path
    ) -> None:
        wd = tmp_path / "wd"
        img = self._png(wd / "chart.png")
        handler = AgentHandler(
            event_bus=event_bus,
            claude_integration=mock_claude,
            default_working_directory=wd,
            default_user_id=42,
        )
        mock_claude.run_command.return_value = MagicMock(
            content="Build chart",
            tools_used=[self._call("mcp__telegram__send_image_to_user", img)],
        )
        published = self._capture(event_bus)

        await handler.handle_webhook(
            WebhookEvent(
                provider="github",
                event_type_name="push",
                payload={"ref": "refs/heads/main"},
                delivery_id="del-ff39",
            )
        )

        events = [e for e in published if isinstance(e, AgentResponseEvent)]
        assert len(events) == 1
        assert events[0].text == "Build chart"
        assert self._resolved(events[0].image_paths) == self._resolved([img])

    # --- added ---------------------------------------------------------

    async def test_turn_without_text_or_images_publishes_nothing(
        self, event_bus, mock_claude, tmp_path
    ) -> None:
        """Empty case: no text, no sends -> no empty notification."""
        handler = self._gymbro(event_bus, mock_claude, tmp_path)
        mock_claude.run_command.return_value = MagicMock(content="", tools_used=[])
        published = self._capture(event_bus)

        await handler.handle_scheduled(self._job_finish(tmp_path))

        assert not [e for e in published if isinstance(e, AgentResponseEvent)]

    async def test_same_image_sent_twice_delivered_once(
        self, event_bus, mock_claude, tmp_path
    ) -> None:
        """Duplicate: the agent re-sends the same file -> one photo."""
        handler = self._gymbro(event_bus, mock_claude, tmp_path)
        img = self._png(tmp_path / "gymbro" / "design" / "dup.png")
        mock_claude.run_command.return_value = MagicMock(
            content="sent",
            tools_used=[
                self._call("mcp__telegram__send_image_to_user", img),
                self._call("mcp__telegram__send_image_to_user", img),
            ],
        )
        published = self._capture(event_bus)

        await handler.handle_scheduled(self._job_finish(tmp_path))

        events = [e for e in published if isinstance(e, AgentResponseEvent)]
        assert len(events) == 1
        assert self._resolved(events[0].image_paths) == self._resolved([img])

    async def test_more_than_cap_images_truncated(
        self, event_bus, mock_claude, tmp_path
    ) -> None:
        """Regression guard: at most MAX_IMAGES_PER_RESPONSE files per reply."""
        from src.bot.utils.image_extractor import MAX_IMAGES_PER_RESPONSE

        handler = self._gymbro(event_bus, mock_claude, tmp_path)
        pngs = [
            self._png(tmp_path / "gymbro" / "many" / f"{i:02d}.png")
            for i in range(MAX_IMAGES_PER_RESPONSE + 3)
        ]
        mock_claude.run_command.return_value = MagicMock(
            content="lots",
            tools_used=[
                self._call("mcp__telegram__send_image_to_user", p) for p in pngs
            ],
        )
        published = self._capture(event_bus)

        await handler.handle_scheduled(self._job_finish(tmp_path))

        events = [e for e in published if isinstance(e, AgentResponseEvent)]
        assert len(events) == 1
        assert self._resolved(events[0].image_paths) == self._resolved(
            pngs[:MAX_IMAGES_PER_RESPONSE]
        )
