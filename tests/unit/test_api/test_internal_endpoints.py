"""Tests for the agent-facing /internal/* endpoints."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from src.api.server import create_api_app
from src.events.bus import EventBus
from src.events.types import AgentResponseEvent
from src.storage.database import DatabaseManager

SECRET = "internal-secret"
AUTH = {"Authorization": f"Bearer {SECRET}"}


def make_settings(**overrides):  # type: ignore[no-untyped-def]
    settings = MagicMock()
    settings.development_mode = True
    settings.github_webhook_secret = "gh-secret"
    settings.webhook_api_secret = overrides.get("webhook_api_secret", SECRET)
    settings.api_server_port = 8080
    settings.debug = False
    return settings


@pytest.fixture
async def db(tmp_path):
    db = DatabaseManager(str(tmp_path / "api.db"))
    await db.initialize()
    yield db
    await db.close()


class TestInternalAuth:
    def test_missing_token_rejected(self) -> None:
        app = create_api_app(EventBus(), make_settings())
        client = TestClient(app)
        assert client.post("/internal/notify", json={"text": "hi"}).status_code == 401

    def test_unconfigured_secret_returns_503(self) -> None:
        app = create_api_app(EventBus(), make_settings(webhook_api_secret=None))
        client = TestClient(app)
        response = client.post("/internal/notify", json={"text": "hi"}, headers=AUTH)
        assert response.status_code == 503


class TestInternalNotify:
    def test_notify_publishes_agent_response_event(self) -> None:
        bus = EventBus()
        bus.publish = AsyncMock()  # type: ignore[method-assign]
        app = create_api_app(bus, make_settings())
        client = TestClient(app)

        response = client.post(
            "/internal/notify", json={"text": "workout time"}, headers=AUTH
        )
        assert response.status_code == 200
        event = bus.publish.call_args[0][0]
        assert isinstance(event, AgentResponseEvent)
        assert event.text == "workout time"
        assert event.chat_id == 0  # broadcast to default notification chats
        assert event.parse_mode is None  # default stays plain

    def test_notify_markdown_flag(self) -> None:
        bus = EventBus()
        bus.publish = AsyncMock()  # type: ignore[method-assign]
        client = TestClient(create_api_app(bus, make_settings()))
        client.post(
            "/internal/notify", json={"text": "**x**", "markdown": True}, headers=AUTH
        )
        assert bus.publish.call_args[0][0].parse_mode == "markdown"


class TestInternalNotifyDeliver:
    """dv-a9b0: `deliver` routes a card to the named profile's own chat
    (AgentResponseEvent.deliver_profile, the job digest's route)."""

    @staticmethod
    def _client(bus):  # type: ignore[no-untyped-def]
        registry = MagicMock()
        registry.get_by_slug.side_effect = lambda slug: (
            MagicMock(slug=slug) if slug in ("app", "keyed", "devops") else None
        )
        return TestClient(
            create_api_app(bus, make_settings(), profile_registry=registry)
        )

    def test_deliver_sets_deliver_profile_for_a_known_profile(self) -> None:
        bus = EventBus()
        bus.publish = AsyncMock()  # type: ignore[method-assign]
        r = self._client(bus).post(
            "/internal/notify",
            json={
                "text": "q?",
                "source": "taskpump:ask",
                "ref": "pump:app:dv-1",
                "profile": "app",
                "deliver": True,
            },
            headers=AUTH,
        )
        assert r.status_code == 200
        event = bus.publish.call_args[0][0]
        assert event.deliver_profile == "app"
        assert event.ledger_profile_slug == "app"
        assert event.ledger_ref == "pump:app:dv-1"

    def test_without_deliver_profile_only_tags_the_ledger(self) -> None:
        bus = EventBus()
        bus.publish = AsyncMock()  # type: ignore[method-assign]
        self._client(bus).post(
            "/internal/notify",
            json={"text": "q?", "ref": "pump:dv-1", "profile": "devops"},
            headers=AUTH,
        )
        event = bus.publish.call_args[0][0]
        assert not event.deliver_profile
        assert event.ledger_profile_slug == "devops"


class TestInternalSchedule:
    def _client_with_scheduler(self, scheduler) -> TestClient:  # type: ignore[no-untyped-def]
        app = create_api_app(
            EventBus(), make_settings(), scheduler=scheduler, profile_registry=None
        )
        return TestClient(app)

    def test_schedule_without_scheduler_returns_503(self) -> None:
        client = self._client_with_scheduler(None)
        response = client.post(
            "/internal/schedule",
            json={"name": "j", "prompt": "p", "cron": "0 7 * * *"},
            headers=AUTH,
        )
        assert response.status_code == 503

    def test_cron_job_registered(self) -> None:
        scheduler = MagicMock()
        scheduler.add_job = AsyncMock(return_value="job-1")
        client = self._client_with_scheduler(scheduler)

        response = client.post(
            "/internal/schedule",
            json={"name": "morning", "prompt": "check in", "cron": "0 7 * * *"},
            headers=AUTH,
        )
        assert response.status_code == 200
        assert response.json()["job_id"] == "job-1"
        kwargs = scheduler.add_job.call_args.kwargs
        assert kwargs["cron_expression"] == "0 7 * * *"
        assert kwargs["run_at"] is None
        # No Telegram identity on this path — 0 is honest, not a bug
        # (dv-3e91), but it must be explicit so `schedule_list` doesn't
        # misreport an unset column as "resolvable".
        assert kwargs["created_by"] == 0

    def test_both_cron_and_at_rejected(self) -> None:
        scheduler = MagicMock()
        scheduler.add_job = AsyncMock(return_value="job-1")
        client = self._client_with_scheduler(scheduler)

        at = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
        response = client.post(
            "/internal/schedule",
            json={"name": "j", "prompt": "p", "cron": "0 7 * * *", "at": at},
            headers=AUTH,
        )
        assert response.status_code == 400

    def test_past_one_shot_rejected(self) -> None:
        scheduler = MagicMock()
        scheduler.add_job = AsyncMock(return_value="job-1")
        client = self._client_with_scheduler(scheduler)

        at = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        response = client.post(
            "/internal/schedule",
            json={"name": "j", "prompt": "p", "at": at},
            headers=AUTH,
        )
        assert response.status_code == 400

    def test_unknown_profile_returns_400_without_registry(self) -> None:
        scheduler = MagicMock()
        scheduler.add_job = AsyncMock(return_value="job-1")
        client = self._client_with_scheduler(scheduler)

        response = client.post(
            "/internal/schedule",
            json={"name": "j", "prompt": "p", "cron": "0 7 * * *", "profile": "coach"},
            headers=AUTH,
        )
        assert response.status_code == 400

    def _client_with_registry(self, scheduler, tmp_path):  # type: ignore[no-untyped-def]
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
        general = ProfileDefinition(
            slug="general",
            name="General",
            working_dir=tmp_path / "general",
            memory_dir=tmp_path / "general" / "memory",
            persona=tmp_path / "general" / "persona.md",
            personal_memory=False,
        )
        registry = ProfileRegistry([coach, general])
        app = create_api_app(
            EventBus(), make_settings(), scheduler=scheduler, profile_registry=registry
        )
        return TestClient(app)

    def test_multi_target_rejected_on_personal_memory_profile(self, tmp_path) -> None:
        """dv-3e91 open question 4: one turn can't be two people's private
        session — the in-process schedule_add tool already enforces a
        single target, this webhook/agentctl path had no such check."""
        scheduler = MagicMock()
        scheduler.add_job = AsyncMock(return_value="job-1")
        client = self._client_with_registry(scheduler, tmp_path)

        response = client.post(
            "/internal/schedule",
            json={
                "name": "j",
                "prompt": "p",
                "cron": "0 7 * * *",
                "profile": "coach",
                "target_chat_ids": [111, 222],
            },
            headers=AUTH,
        )
        assert response.status_code == 400
        scheduler.add_job.assert_not_called()

    def test_multi_target_allowed_on_shared_profile(self, tmp_path) -> None:
        scheduler = MagicMock()
        scheduler.add_job = AsyncMock(return_value="job-1")
        client = self._client_with_registry(scheduler, tmp_path)

        response = client.post(
            "/internal/schedule",
            json={
                "name": "j",
                "prompt": "p",
                "cron": "0 7 * * *",
                "profile": "general",
                "target_chat_ids": [111, 222],
            },
            headers=AUTH,
        )
        assert response.status_code == 200
        assert scheduler.add_job.call_args.kwargs["target_chat_ids"] == [111, 222]

    def test_internal_delete_refused_no_caller_identity(self) -> None:
        """dv-8c19: this path has no user identity to authorize a cancel
        against -- any WEBHOOK_API_SECRET holder could cancel ANY user's
        job. Refuse outright rather than fake-authorize it."""
        scheduler = MagicMock()
        scheduler.remove_job = AsyncMock(return_value=True)
        client = self._client_with_scheduler(scheduler)

        response = client.delete("/internal/schedule/some-job-id", headers=AUTH)

        assert response.status_code == 403
        scheduler.remove_job.assert_not_called()

    def test_single_target_allowed_on_personal_memory_profile(self, tmp_path) -> None:
        scheduler = MagicMock()
        scheduler.add_job = AsyncMock(return_value="job-1")
        client = self._client_with_registry(scheduler, tmp_path)

        response = client.post(
            "/internal/schedule",
            json={
                "name": "j",
                "prompt": "p",
                "cron": "0 7 * * *",
                "profile": "coach",
                "target_chat_ids": [111],
            },
            headers=AUTH,
        )
        assert response.status_code == 200


class TestInternalTasks:
    async def test_task_lifecycle(self, db) -> None:
        app = create_api_app(EventBus(), make_settings(), db_manager=db)
        client = TestClient(app)

        created = client.post(
            "/internal/tasks",
            json={"title": "Buy bands", "created_by": "coach"},
            headers=AUTH,
        )
        assert created.status_code == 200
        task_id = created.json()["task_id"]

        listed = client.get("/internal/tasks", headers=AUTH)
        assert [t["id"] for t in listed.json()] == [task_id]
        assert listed.json()[0]["created_by"] == "coach"

        done = client.patch(
            f"/internal/tasks/{task_id}", json={"status": "done"}, headers=AUTH
        )
        assert done.status_code == 200
        assert client.get("/internal/tasks", headers=AUTH).json() == []

    async def test_update_unknown_task_404(self, db) -> None:
        app = create_api_app(EventBus(), make_settings(), db_manager=db)
        client = TestClient(app)
        response = client.patch(
            "/internal/tasks/999", json={"status": "done"}, headers=AUTH
        )
        assert response.status_code == 404


class TestInternalPrompt:
    """POST /internal/prompt runs one agent turn in a profile's conversation."""

    def _client(self, registry) -> TestClient:  # type: ignore[no-untyped-def]
        self.bus = EventBus()
        self.bus.publish = AsyncMock()  # type: ignore[method-assign]
        app = create_api_app(self.bus, make_settings(), profile_registry=registry)
        return TestClient(app)

    def test_prompt_publishes_scheduled_event(self) -> None:
        from pathlib import Path

        from src.events.types import ScheduledEvent

        registry = MagicMock()
        profile = MagicMock()
        profile.working_dir = Path("/home/user/workspaces/general")
        registry.get_by_slug.return_value = profile

        client = self._client(registry)
        response = client.post(
            "/internal/prompt",
            json={
                "prompt": "[job finished; read output and report]",
                "profile": "general",
                "chat_id": 42,
            },
            headers=AUTH,
        )
        assert response.status_code == 200
        event = self.bus.publish.call_args[0][0]
        assert isinstance(event, ScheduledEvent)
        assert event.prompt == "[job finished; read output and report]"
        assert event.working_directory == Path("/home/user/workspaces/general")
        assert event.target_chat_ids == [42]

    def test_prompt_unknown_profile_404(self) -> None:
        registry = MagicMock()
        registry.get_by_slug.return_value = None
        client = self._client(registry)
        response = client.post(
            "/internal/prompt",
            json={"prompt": "x", "profile": "nope"},
            headers=AUTH,
        )
        assert response.status_code == 404

    def test_prompt_without_registry_400(self) -> None:
        client = self._client(None)
        response = client.post(
            "/internal/prompt",
            json={"prompt": "x", "profile": "general"},
            headers=AUTH,
        )
        assert response.status_code == 400

    def test_prompt_requires_auth(self) -> None:
        client = self._client(MagicMock())
        assert (
            client.post(
                "/internal/prompt", json={"prompt": "x", "profile": "g"}
            ).status_code
            == 401
        )


class TestInternalPolicyAsk:
    """A background job's ask rule (dv-ef90): the owner's button, one hour."""

    def test_ask_goes_to_the_confirmation_manager_with_the_job_timeout(self) -> None:
        from src.model_router.job import ASK_TIMEOUT_SECONDS

        manager = MagicMock()
        manager.request = AsyncMock(return_value=True)
        app = create_api_app(EventBus(), make_settings(), confirmation_manager=manager)
        client = TestClient(app)

        response = client.post(
            "/internal/policy/ask",
            json={"reason": "rm outside tmp", "preview": "rm x", "source": "pump-1"},
            headers=AUTH,
        )

        assert response.status_code == 200 and response.json() == {"approved": True}
        manager.request.assert_awaited_once_with(
            "rm outside tmp",
            "rm x",
            timeout_seconds=ASK_TIMEOUT_SECONDS,
            source="pump-1",
            context=None,
        )

    def test_ask_without_a_manager_is_denied(self) -> None:
        app = create_api_app(EventBus(), make_settings())
        client = TestClient(app)
        response = client.post(
            "/internal/policy/ask", json={"reason": "r"}, headers=AUTH
        )
        assert response.status_code == 200 and response.json() == {"approved": False}

    def test_ask_requires_auth(self) -> None:
        app = create_api_app(EventBus(), make_settings())
        assert (
            TestClient(app)
            .post("/internal/policy/ask", json={"reason": "r"})
            .status_code
            == 401
        )


class TestInternalModelCalls:
    async def test_record_writes_a_row(self, db) -> None:
        from src.storage.model_calls import ModelCallRepository

        app = create_api_app(
            EventBus(), make_settings(), model_calls=ModelCallRepository(db)
        )
        client = TestClient(app)
        row = {
            "purpose": "background_job",
            "kind": "turn",
            "harness": "claude",
            "harness_version": "2.1.288",
            "model": "sonnet",
            "profile": "devops",
            "input_tokens": 5,
            "output_tokens": 7,
            "cost_usd": 0.5,
            "duration_ms": 1200,
            "outcome": "ok",
        }
        response = client.post("/internal/model_calls", json=row, headers=AUTH)
        assert response.status_code == 200, response.text

        async with db.get_connection() as conn:
            cur = await conn.execute("SELECT * FROM model_calls")
            (stored,) = [dict(r) for r in await cur.fetchall()]
        assert stored["purpose"] == "background_job" and stored["attended"] == 0
        assert stored["provider"] == "anthropic" and stored["model"] == "sonnet"
        assert stored["cost_usd"] == 0.5 and stored["outcome"] == "ok"

    async def test_job_name_is_stored(self, db) -> None:
        """dv-87d8: a pump stage's job name is kept for its Phoenix trace."""
        from src.storage.model_calls import ModelCallRepository

        app = create_api_app(
            EventBus(), make_settings(), model_calls=ModelCallRepository(db)
        )
        row = {
            "purpose": "background_job",
            "kind": "turn",
            "harness": "claude",
            "outcome": "ok",
            "job_name": "pump-dv-87d8-implement-p1",
        }
        response = TestClient(app).post("/internal/model_calls", json=row, headers=AUTH)
        assert response.status_code == 200, response.text
        async with db.get_connection() as conn:
            cur = await conn.execute("SELECT job_name, exported_at FROM model_calls")
            (stored,) = [dict(r) for r in await cur.fetchall()]
        assert stored["job_name"] == "pump-dv-87d8-implement-p1"
        assert stored["exported_at"] is None

    async def test_unknown_purpose_is_rejected(self, db) -> None:
        from src.storage.model_calls import ModelCallRepository

        app = create_api_app(
            EventBus(), make_settings(), model_calls=ModelCallRepository(db)
        )
        row = {"purpose": "misc", "kind": "turn", "harness": "claude", "outcome": "ok"}
        assert (
            TestClient(app)
            .post("/internal/model_calls", json=row, headers=AUTH)
            .status_code
            == 400
        )

    async def test_tools_are_written_under_the_call_row(self, db) -> None:
        """dv-2260: a job's tool rows ride along and get the new row's id."""
        from src.storage.model_calls import ModelCallRepository
        from src.storage.tool_calls import ToolCallRepository

        app = create_api_app(
            EventBus(),
            make_settings(),
            model_calls=ModelCallRepository(db),
            tool_calls=ToolCallRepository(db),
        )
        row = {
            "purpose": "background_job",
            "kind": "turn",
            "harness": "claude",
            "session_id": "job-s",
            "outcome": "ok",
            "trace_id": "tr1",
            "tools": [
                {
                    "tool_use_id": "u1",
                    "seq": 1,
                    "tool": "Bash",
                    "status": "ok",
                    "started_at": datetime.now(UTC).isoformat(),
                }
            ],
        }
        response = TestClient(app).post("/internal/model_calls", json=row, headers=AUTH)
        assert response.status_code == 200, response.text

        async with db.get_connection() as conn:
            cur = await conn.execute("SELECT id FROM model_calls")
            (call_id,) = [r[0] for r in await cur.fetchall()]
        (tool,) = await ToolCallRepository(db).for_model_call(call_id)
        assert tool["trace_id"] == "tr1" and tool["session_id"] == "job-s"
        assert tool["tool"] == "Bash" and tool["status"] == "ok"

    async def test_trace_write_failure_keeps_the_call_row(self, db) -> None:
        from src.storage.model_calls import ModelCallRepository

        broken = MagicMock()
        broken.upsert = AsyncMock(side_effect=RuntimeError("disk"))
        app = create_api_app(
            EventBus(),
            make_settings(),
            model_calls=ModelCallRepository(db),
            tool_calls=broken,
        )
        row = {
            "purpose": "background_job",
            "kind": "turn",
            "harness": "claude",
            "outcome": "ok",
            "tools": [{"tool_use_id": "u1", "seq": 1, "tool": "Read"}],
        }
        response = TestClient(app).post("/internal/model_calls", json=row, headers=AUTH)
        assert response.status_code == 200, response.text
        async with db.get_connection() as conn:
            cur = await conn.execute("SELECT COUNT(*) FROM model_calls")
            assert (await cur.fetchone())[0] == 1
