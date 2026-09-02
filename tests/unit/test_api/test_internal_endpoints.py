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
