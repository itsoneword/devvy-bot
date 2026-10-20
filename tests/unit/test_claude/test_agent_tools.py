"""Tests for the in-process agent MCP tools (requester chat threading)."""

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Dict
from unittest.mock import AsyncMock

import pytest

from src.claude import agent_tools
from src.claude.agent_tools import AgentServices, build_agent_mcp_server
from src.evaluator.writer import MemoryWriter
from src.events.types import AgentResponseEvent
from src.storage.database import DatabaseManager
from src.storage.deviz_board import DevizBoard
from src.storage.memory_repository import MemoryRepository
from src.storage.models import MessageModel, SessionModel, UserModel
from src.storage.repositories import (
    MessageRepository,
    SessionRepository,
    UserRepository,
)


class FakeEventBus:
    """Records published events."""

    def __init__(self) -> None:
        self.published: list = []

    async def publish(self, event: Any) -> None:
        self.published.append(event)


@pytest.fixture
def event_bus() -> FakeEventBus:
    return FakeEventBus()


@pytest.fixture
def scheduler() -> AsyncMock:
    mock = AsyncMock()
    mock.add_job = AsyncMock(return_value="job-1")
    return mock


@pytest.fixture
def services(event_bus: FakeEventBus, scheduler: AsyncMock) -> AgentServices:
    return AgentServices(
        event_bus=event_bus,
        task_repo=AsyncMock(),
        scheduler=scheduler,
    )


@pytest.fixture
def captured_tools(monkeypatch: pytest.MonkeyPatch) -> Dict[str, Any]:
    """Capture the tools passed to create_sdk_mcp_server, keyed by name."""
    captured: Dict[str, Any] = {}

    def fake_create(name: str, version: str, tools: list) -> Dict[str, str]:
        captured.update({t.name: t for t in tools})
        return {"name": name}

    monkeypatch.setattr(agent_tools, "create_sdk_mcp_server", fake_create)
    return captured


def _build(
    services: AgentServices,
    captured: Dict[str, Any],
    requester_chat_id: int | None = None,
    user_id: int | None = None,
    role: str | None = None,
) -> Dict[str, Any]:
    build_agent_mcp_server(
        services,
        "general",
        Path("/tmp/ws"),
        requester_chat_id=requester_chat_id,
        user_id=user_id,
        role=role,
    )
    return captured


class TestNotify:
    async def test_owner_can_route_report_source(
        self,
        services: AgentServices,
        captured_tools: Dict[str, Any],
        event_bus: FakeEventBus,
    ) -> None:
        tools = _build(services, captured_tools, requester_chat_id=555, role="owner")

        await tools["notify"].handler({"text": "check", "source": "crypto"})

        assert event_bus.published[0].chat_id == 555
        assert event_bus.published[0].source == "crypto"

    async def test_guest_cannot_route_report_source(
        self,
        services: AgentServices,
        captured_tools: Dict[str, Any],
        event_bus: FakeEventBus,
    ) -> None:
        tools = _build(services, captured_tools, requester_chat_id=555, role="guest")

        result = await tools["notify"].handler({"text": "check", "source": "crypto"})

        assert "error" in result["content"][0]["text"]
        assert event_bus.published == []

    async def test_notify_stamps_requester_chat_id(
        self,
        services: AgentServices,
        captured_tools: Dict[str, Any],
        event_bus: FakeEventBus,
    ) -> None:
        tools = _build(services, captured_tools, requester_chat_id=555)

        result = await tools["notify"].handler({"text": "hello"})

        assert "error" not in result["content"][0]["text"]
        assert len(event_bus.published) == 1
        event = event_bus.published[0]
        assert isinstance(event, AgentResponseEvent)
        assert event.chat_id == 555
        assert event.text == "hello"

    async def test_notify_ignores_model_supplied_chat_id(
        self,
        services: AgentServices,
        captured_tools: Dict[str, Any],
        event_bus: FakeEventBus,
    ) -> None:
        """The model must not be able to address arbitrary chats."""
        tools = _build(services, captured_tools, requester_chat_id=555)

        await tools["notify"].handler({"text": "hi", "chat_id": 999})

        assert event_bus.published[0].chat_id == 555

    async def test_notify_falls_back_to_broadcast_without_requester(
        self,
        services: AgentServices,
        captured_tools: Dict[str, Any],
        event_bus: FakeEventBus,
    ) -> None:
        """No requester context -> chat_id 0 -> NOTIFICATION_CHAT_IDS."""
        tools = _build(services, captured_tools, requester_chat_id=None)

        await tools["notify"].handler({"text": "hi", "chat_id": 999})

        assert event_bus.published[0].chat_id == 0


class TestScheduleAdd:
    async def test_schedule_add_persists_requester_chat(
        self,
        services: AgentServices,
        captured_tools: Dict[str, Any],
        scheduler: AsyncMock,
    ) -> None:
        tools = _build(services, captured_tools, requester_chat_id=555)
        at = (datetime.now(UTC) + timedelta(hours=1)).isoformat()

        result = await tools["schedule_add"].handler(
            {"name": "remind", "prompt": "ping me", "at": at}
        )

        assert "scheduled" in result["content"][0]["text"]
        assert scheduler.add_job.call_args.kwargs["target_chat_ids"] == [555]

    async def test_schedule_add_no_target_without_requester(
        self,
        services: AgentServices,
        captured_tools: Dict[str, Any],
        scheduler: AsyncMock,
    ) -> None:
        tools = _build(services, captured_tools, requester_chat_id=None)

        await tools["schedule_add"].handler(
            {"name": "daily", "prompt": "report", "cron": "0 9 * * *"}
        )

        assert scheduler.add_job.call_args.kwargs["target_chat_ids"] is None

    async def test_schedule_add_persists_created_by(
        self,
        services: AgentServices,
        captured_tools: Dict[str, Any],
        scheduler: AsyncMock,
    ) -> None:
        """dv-3e91: schedule_add never passed created_by, so every stored
        job's owner was unrecoverable at fire time."""
        tools = _build(services, captured_tools, requester_chat_id=555, user_id=555)
        at = (datetime.now(UTC) + timedelta(hours=1)).isoformat()

        await tools["schedule_add"].handler(
            {"name": "remind", "prompt": "ping me", "at": at}
        )

        assert scheduler.add_job.call_args.kwargs["created_by"] == 555

    async def test_schedule_add_created_by_zero_without_user_id(
        self,
        services: AgentServices,
        captured_tools: Dict[str, Any],
        scheduler: AsyncMock,
    ) -> None:
        tools = _build(services, captured_tools, user_id=None)

        await tools["schedule_add"].handler(
            {"name": "daily", "prompt": "report", "cron": "0 9 * * *"}
        )

        assert scheduler.add_job.call_args.kwargs["created_by"] == 0


class TestScheduleAddTimezone:
    """dv-ca94: a naive 'at' (no UTC offset) is the USER's local time, not
    UTC -- "today at 17:00" for a Lisbon user must fire at 16:00Z in
    September (DST) and 17:00Z in January (standard time)."""

    @pytest.fixture
    def lisbon_services(
        self, event_bus: FakeEventBus, scheduler: AsyncMock
    ) -> AgentServices:
        return AgentServices(
            event_bus=event_bus,
            task_repo=AsyncMock(),
            scheduler=scheduler,
            user_timezone="Europe/Lisbon",
        )

    async def test_naive_at_interpreted_in_dst_offset(
        self,
        lisbon_services: AgentServices,
        captured_tools: Dict[str, Any],
        scheduler: AsyncMock,
    ) -> None:
        tools = _build(lisbon_services, captured_tools, requester_chat_id=1)

        result = await tools["schedule_add"].handler(
            {"name": "remind", "prompt": "ping", "at": "2027-09-27T17:00:00"}
        )

        run_at = scheduler.add_job.call_args.kwargs["run_at"]
        assert run_at == datetime(2027, 9, 27, 16, 0, tzinfo=UTC)
        payload = json.loads(result["content"][0]["text"])
        assert payload["at_utc"] == "2027-09-27T16:00:00+00:00"
        assert "17:00:00+01:00" in payload["at_local"]

    async def test_naive_at_interpreted_in_standard_offset(
        self,
        lisbon_services: AgentServices,
        captured_tools: Dict[str, Any],
        scheduler: AsyncMock,
    ) -> None:
        """Same wall-clock hour, no DST: Lisbon is UTC+0 in January."""
        tools = _build(lisbon_services, captured_tools, requester_chat_id=1)

        await tools["schedule_add"].handler(
            {"name": "remind", "prompt": "ping", "at": "2027-01-15T17:00:00"}
        )

        run_at = scheduler.add_job.call_args.kwargs["run_at"]
        assert run_at == datetime(2027, 1, 15, 17, 0, tzinfo=UTC)

    async def test_explicit_offset_is_left_alone(
        self,
        lisbon_services: AgentServices,
        captured_tools: Dict[str, Any],
        scheduler: AsyncMock,
    ) -> None:
        """A model that deliberately means UTC (or another zone) is trusted
        -- only a BARE time defaults to the local zone."""
        tools = _build(lisbon_services, captured_tools, requester_chat_id=1)

        await tools["schedule_add"].handler(
            {"name": "remind", "prompt": "ping", "at": "2027-09-27T17:00:00+00:00"}
        )

        run_at = scheduler.add_job.call_args.kwargs["run_at"]
        assert run_at == datetime(2027, 9, 27, 17, 0, tzinfo=UTC)

    async def test_bad_configured_timezone_falls_back_to_utc(
        self,
        event_bus: FakeEventBus,
        scheduler: AsyncMock,
        captured_tools: Dict[str, Any],
    ) -> None:
        services = AgentServices(
            event_bus=event_bus,
            task_repo=AsyncMock(),
            scheduler=scheduler,
            user_timezone="Not/AZone",
        )
        tools = _build(services, captured_tools, requester_chat_id=1)

        await tools["schedule_add"].handler(
            {"name": "remind", "prompt": "ping", "at": "2027-09-27T17:00:00"}
        )

        run_at = scheduler.add_job.call_args.kwargs["run_at"]
        assert run_at == datetime(2027, 9, 27, 17, 0, tzinfo=UTC)


class TestScheduleAuthorization:
    """dv-8c19: a user may only see/manage jobs they created or that target
    them; legacy unattributed (created_by=0) rows stay owner-only."""

    def _rows(self) -> list:
        return [
            {"job_id": "mine", "created_by": 111, "target_chat_ids": "111"},
            {"job_id": "theirs", "created_by": 222, "target_chat_ids": "222"},
            {
                "job_id": "for-me",
                "created_by": 222,
                "target_chat_ids": "111,222",
            },
            {"job_id": "legacy", "created_by": 0, "target_chat_ids": ""},
        ]

    async def test_list_scopes_to_own_and_targeted_jobs(
        self,
        services: AgentServices,
        captured_tools: Dict[str, Any],
        scheduler: AsyncMock,
    ) -> None:
        scheduler.list_jobs = AsyncMock(return_value=self._rows())
        tools = _build(services, captured_tools, requester_chat_id=111, user_id=111)
        result = await tools["schedule_list"].handler({})
        ids = {row["job_id"] for row in json.loads(result["content"][0]["text"])}
        assert ids == {"mine", "for-me"}

    async def test_list_owner_role_sees_legacy_rows_not_others_jobs(
        self,
        services: AgentServices,
        captured_tools: Dict[str, Any],
        scheduler: AsyncMock,
    ) -> None:
        scheduler.list_jobs = AsyncMock(return_value=self._rows())
        build_agent_mcp_server(
            services,
            "general",
            Path("/tmp/ws"),
            requester_chat_id=111,
            user_id=111,
            role="owner",
        )
        result = await captured_tools["schedule_list"].handler({})
        ids = {row["job_id"] for row in json.loads(result["content"][0]["text"])}
        assert ids == {"mine", "for-me", "legacy"}
        assert "theirs" not in ids

    async def test_remove_refuses_another_users_job(
        self,
        services: AgentServices,
        captured_tools: Dict[str, Any],
        scheduler: AsyncMock,
    ) -> None:
        scheduler.list_jobs = AsyncMock(return_value=self._rows())
        scheduler.remove_job = AsyncMock(return_value=True)
        tools = _build(services, captured_tools, requester_chat_id=111, user_id=111)

        result = await tools["schedule_remove"].handler({"job_id": "theirs"})

        assert result.get("isError") is True
        assert "belongs to another user" in result["content"][0]["text"]
        scheduler.remove_job.assert_not_called()

    async def test_remove_allows_job_targeting_the_caller(
        self,
        services: AgentServices,
        captured_tools: Dict[str, Any],
        scheduler: AsyncMock,
    ) -> None:
        scheduler.list_jobs = AsyncMock(return_value=self._rows())
        scheduler.remove_job = AsyncMock(return_value=True)
        tools = _build(services, captured_tools, requester_chat_id=111, user_id=111)

        result = await tools["schedule_remove"].handler({"job_id": "for-me"})

        assert "removed" in result["content"][0]["text"]
        scheduler.remove_job.assert_called_once_with("for-me")

    async def test_remove_allows_own_job(
        self,
        services: AgentServices,
        captured_tools: Dict[str, Any],
        scheduler: AsyncMock,
    ) -> None:
        scheduler.list_jobs = AsyncMock(return_value=self._rows())
        scheduler.remove_job = AsyncMock(return_value=True)
        tools = _build(services, captured_tools, requester_chat_id=111, user_id=111)

        result = await tools["schedule_remove"].handler({"job_id": "mine"})

        assert "removed" in result["content"][0]["text"]
        scheduler.remove_job.assert_called_once_with("mine")


class TestGetWeather:
    async def test_get_weather_returns_data(
        self,
        services: AgentServices,
        captured_tools: Dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        fake_result = {
            "location": "Costa de Caparica, Portugal",
            "date": "2026-07-27",
            "day_max_c": 26.0,
            "night_min_c": 18.0,
            "hourly_c": {"03:00": 18.5, "21:00": 21.0},
        }
        mock_get_weather_for = AsyncMock(return_value=fake_result)
        monkeypatch.setattr(agent_tools, "get_weather_for", mock_get_weather_for)
        tools = _build(services, captured_tools)

        result = await tools["get_weather"].handler(
            {"location": "", "date": "2026-07-27"}
        )

        mock_get_weather_for.assert_awaited_once_with("", "2026-07-27")
        assert "error" not in result
        assert "Costa de Caparica" in result["content"][0]["text"]
        assert "18.0" in result["content"][0]["text"]

    async def test_get_weather_errors_are_reported_not_raised(
        self,
        services: AgentServices,
        captured_tools: Dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            agent_tools,
            "get_weather_for",
            AsyncMock(side_effect=ValueError("no location found for 'nowhere'")),
        )
        tools = _build(services, captured_tools)

        result = await tools["get_weather"].handler({"location": "nowhere"})

        assert result.get("isError") is True
        assert "no location found" in result["content"][0]["text"]


class TestRemember:
    """The in-session memory tool (dv-9c24).

    The load-bearing property is the scope one: every write must land in the
    scope the SERVER resolved for this session, whatever the model passes.
    """

    @pytest.fixture
    async def memory_repo(self, tmp_path: Path):
        db = DatabaseManager(str(tmp_path / "mem.db"))
        await db.initialize()
        yield MemoryRepository(db)
        await db.close()

    @pytest.fixture
    def memory_writer(self, memory_repo: MemoryRepository) -> MemoryWriter:
        return MemoryWriter(memory_repo, source="agent_tool")

    @pytest.fixture
    def mem_services(
        self,
        event_bus: FakeEventBus,
        scheduler: AsyncMock,
        memory_writer: MemoryWriter,
    ) -> AgentServices:
        return AgentServices(
            event_bus=event_bus,
            task_repo=AsyncMock(),
            scheduler=scheduler,
            memory_writer=memory_writer,
        )

    @staticmethod
    def _build_mem(
        services: AgentServices,
        captured: Dict[str, Any],
        memory_agent_id: str | None = "coach#u111",
        memory_family_agent_id: str | None = None,
    ) -> Dict[str, Any]:
        build_agent_mcp_server(
            services,
            "coach",
            Path("/tmp/ws"),
            requester_chat_id=555,
            memory_agent_id=memory_agent_id,
            memory_family_agent_id=memory_family_agent_id,
        )
        return captured

    async def test_fact_lands_in_the_session_scope(
        self,
        mem_services: AgentServices,
        captured_tools: Dict[str, Any],
        memory_repo: MemoryRepository,
    ) -> None:
        """Same store the evaluator writes to, under the session's own scope."""
        tools = self._build_mem(mem_services, captured_tools)

        result = await tools["remember"].handler({"fact": "allergic to walnuts"})

        assert result.get("isError") is not True
        assert "saved" in result["content"][0]["text"]
        facts = await memory_repo.get_facts("coach#u111")
        assert [(f.content, f.tier, f.source) for f in facts] == [
            ("allergic to walnuts", "general", "agent_tool")
        ]

    async def test_scope_argument_cannot_reach_another_users_store(
        self,
        mem_services: AgentServices,
        captured_tools: Dict[str, Any],
        memory_repo: MemoryRepository,
    ) -> None:
        """dv-6dbc boundary: a model-supplied scope is not a scope identifier.

        Every one of these is an attempt to name someone else's store. All of
        them must land in this session's own scope and nowhere else.
        """
        tools = self._build_mem(
            mem_services, captured_tools, memory_family_agent_id="f7:coach"
        )

        hostile_scopes = [
            "coach#u999",
            "devops",
            "f9:coach",
            {"agent_id": "coach#u999"},  # not even a string
            "PRIVATE",
        ]
        # Deliberately dissimilar so the 0.92 dedupe cannot swallow one and
        # make a leak look like a pass.
        facts = [
            "sleeps badly on Sundays",
            "drives a red hatchback",
            "owns three cats",
            "prefers metric units",
            "was born in Riga",
        ]
        for hostile, fact in zip(hostile_scopes, facts):
            await tools["remember"].handler(
                {
                    "fact": fact,
                    "scope": hostile,
                    # These are not parameters of the tool at all; passing them
                    # must be inert, not an alternative route in.
                    "agent_id": "coach#u999",
                    "user_id": 999,
                }
            )

        assert len(await memory_repo.get_facts("coach#u111")) == 5
        for other in ("coach#u999", "devops", "f9:coach", "f7:coach", "coach"):
            assert await memory_repo.get_facts(other) == []

    async def test_schema_exposes_no_scope_identifier(
        self, mem_services: AgentServices, captured_tools: Dict[str, Any]
    ) -> None:
        """Nothing in the input schema lets the model name a store."""
        tools = self._build_mem(mem_services, captured_tools)

        schema = tools["remember"].input_schema
        # dv-6f2a: an explicit JSON Schema now, so the params are one level
        # down under "properties" — the boundary being asserted is unchanged.
        assert set(schema["properties"]) == {"fact", "tier", "scope", "label"}

    async def test_duplicate_of_an_existing_fact_collapses(
        self,
        mem_services: AgentServices,
        captured_tools: Dict[str, Any],
        memory_repo: MemoryRepository,
    ) -> None:
        """The evaluator's structural dedupe applies to the tool's writes too.

        This is the mechanism behind "a fact saved by the tool is not
        duplicated by the next extraction": whichever writer goes second sees
        the first one's row and drops its own.
        """
        await memory_repo.add_fact(
            "coach#u111", "general", "likes spicy food", source="evaluator"
        )
        tools = self._build_mem(mem_services, captured_tools)

        result = await tools["remember"].handler({"fact": "Likes spicy foods!"})

        assert result.get("isError") is not True
        assert "duplicate" in result["content"][0]["text"]
        assert len(await memory_repo.get_facts("coach#u111")) == 1

    async def test_evaluator_reemission_of_a_tool_fact_collapses(
        self,
        mem_services: AgentServices,
        captured_tools: Dict[str, Any],
        memory_repo: MemoryRepository,
    ) -> None:
        """The other direction: tool writes first, batch evaluator re-emits."""
        tools = self._build_mem(mem_services, captured_tools)
        await tools["remember"].handler({"fact": "trains on Tuesdays"})

        evaluator_writer = MemoryWriter(memory_repo)
        applied = await evaluator_writer.apply_ops(
            "coach#u111",
            [{"op": "add", "tier": "general", "content": "Trains on Tuesdays."}],
        )

        assert applied.applied == 0
        assert evaluator_writer.duplicates_rejected == 1
        assert len(await memory_repo.get_facts("coach#u111")) == 1

    async def test_family_scope_selector_uses_the_resolved_family_id(
        self,
        mem_services: AgentServices,
        captured_tools: Dict[str, Any],
        memory_repo: MemoryRepository,
    ) -> None:
        tools = self._build_mem(
            mem_services, captured_tools, memory_family_agent_id="f7:coach"
        )

        await tools["remember"].handler(
            {"fact": "the family eats dinner at 19:00", "scope": "family"}
        )

        assert len(await memory_repo.get_facts("f7:coach")) == 1
        assert await memory_repo.get_facts("coach#u111") == []

    async def test_family_scope_downgrades_to_private_without_a_family(
        self,
        mem_services: AgentServices,
        captured_tools: Dict[str, Any],
        memory_repo: MemoryRepository,
    ) -> None:
        """A user with no family can never write into one."""
        tools = self._build_mem(mem_services, captured_tools)

        result = await tools["remember"].handler(
            {"fact": "we eat at 19:00", "scope": "family"}
        )

        assert json.loads(result["content"][0]["text"])["scope"] == "private"
        assert len(await memory_repo.get_facts("coach#u111")) == 1

    async def test_pinned_tier_is_honored(
        self,
        mem_services: AgentServices,
        captured_tools: Dict[str, Any],
        memory_repo: MemoryRepository,
    ) -> None:
        tools = self._build_mem(mem_services, captured_tools)

        await tools["remember"].handler({"fact": "name is Anna", "tier": "pinned"})

        assert (await memory_repo.get_facts("coach#u111"))[0].tier == "pinned"

    async def test_bad_input_is_rejected_not_silently_dropped(
        self,
        mem_services: AgentServices,
        captured_tools: Dict[str, Any],
        memory_repo: MemoryRepository,
    ) -> None:
        """An unwritable fact must return isError, never a success string.

        The failure mode this guards is the one in docs/FINDINGS.md: the model
        telling the user something was remembered when it was not.
        """
        tools = self._build_mem(mem_services, captured_tools)

        for args in (
            {"fact": "   "},
            {"fact": "x" * (agent_tools.MAX_FACT_CHARS + 1)},
            {"fact": "ok", "tier": "critical"},
        ):
            result = await tools["remember"].handler(args)
            assert result.get("isError") is True, args

        assert await memory_repo.get_facts("coach#u111") == []

    async def test_not_mounted_without_a_resolved_scope(
        self, mem_services: AgentServices, captured_tools: Dict[str, Any]
    ) -> None:
        """No scope resolved -> no tool, rather than a tool that always fails."""
        self._build_mem(mem_services, captured_tools, memory_agent_id=None)

        assert "remember" not in captured_tools

    async def test_not_mounted_without_a_writer(
        self,
        services: AgentServices,
        captured_tools: Dict[str, Any],
    ) -> None:
        self._build_mem(services, captured_tools)

        assert "remember" not in captured_tools


class TestRecall:
    """recall: explicit query-driven search over structured facts (dv-r3c1)."""

    @pytest.fixture
    async def memory_repo(self, tmp_path: Path):
        db = DatabaseManager(str(tmp_path / "mem.db"))
        await db.initialize()
        yield MemoryRepository(db)
        await db.close()

    @pytest.fixture
    def recall_services(
        self,
        event_bus: FakeEventBus,
        scheduler: AsyncMock,
        memory_repo: MemoryRepository,
    ) -> AgentServices:
        return AgentServices(
            event_bus=event_bus,
            task_repo=AsyncMock(),
            scheduler=scheduler,
            memory_repo=memory_repo,
        )

    @staticmethod
    def _build_recall(
        services: AgentServices,
        captured: Dict[str, Any],
        memory_agent_id: str | None = "coach#u111",
        memory_family_agent_id: str | None = None,
    ) -> Dict[str, Any]:
        build_agent_mcp_server(
            services,
            "coach",
            Path("/tmp/ws"),
            requester_chat_id=555,
            memory_agent_id=memory_agent_id,
            memory_family_agent_id=memory_family_agent_id,
        )
        return captured

    async def test_finds_fact_by_query(
        self,
        recall_services: AgentServices,
        captured_tools: Dict[str, Any],
        memory_repo: MemoryRepository,
    ) -> None:
        await memory_repo.add_fact("coach#u111", "general", "allergic to walnuts")
        tools = self._build_recall(recall_services, captured_tools)

        result = await tools["recall"].handler({"query": "walnuts"})

        assert result.get("isError") is not True
        assert "walnuts" in result["content"][0]["text"]

    async def test_returns_empty_list_on_no_match(
        self,
        recall_services: AgentServices,
        captured_tools: Dict[str, Any],
        memory_repo: MemoryRepository,
    ) -> None:
        await memory_repo.add_fact("coach#u111", "general", "loves spicy food")
        tools = self._build_recall(recall_services, captured_tools)

        result = await tools["recall"].handler(
            {"query": "something totally unrelated xyz"}
        )

        assert result["content"][0]["text"].strip() == "[]"

    async def test_query_is_required(
        self, recall_services: AgentServices, captured_tools: Dict[str, Any]
    ) -> None:
        tools = self._build_recall(recall_services, captured_tools)

        result = await tools["recall"].handler({"query": "   "})

        assert result.get("isError") is True

    async def test_limit_is_capped(
        self,
        recall_services: AgentServices,
        captured_tools: Dict[str, Any],
        memory_repo: MemoryRepository,
    ) -> None:
        for i in range(agent_tools.RECALL_MAX_LIMIT + 10):
            await memory_repo.add_fact(
                "coach#u111", "general", f"fact number {i} keyword"
            )
        tools = self._build_recall(recall_services, captured_tools)

        result = await tools["recall"].handler({"query": "keyword", "limit": 9999})

        payload = json.loads(result["content"][0]["text"])
        assert len(payload) == agent_tools.RECALL_MAX_LIMIT

    async def test_searches_family_scope_when_present(
        self,
        recall_services: AgentServices,
        captured_tools: Dict[str, Any],
        memory_repo: MemoryRepository,
    ) -> None:
        await memory_repo.add_fact("coach#u111", "general", "private allergy note")
        await memory_repo.add_fact("f7:coach", "general", "family dinner is at 19:00")
        tools = self._build_recall(
            recall_services, captured_tools, memory_family_agent_id="f7:coach"
        )

        result = await tools["recall"].handler({"query": "dinner"})

        payload = json.loads(result["content"][0]["text"])
        assert any(item["scope"] == "family" for item in payload)

    async def test_does_not_leak_other_agents_facts(
        self,
        recall_services: AgentServices,
        captured_tools: Dict[str, Any],
        memory_repo: MemoryRepository,
    ) -> None:
        await memory_repo.add_fact(
            "devops#u999", "general", "server root password is secret123"
        )
        tools = self._build_recall(recall_services, captured_tools)

        result = await tools["recall"].handler({"query": "password secret123"})

        assert result["content"][0]["text"].strip() == "[]"

    async def test_not_mounted_without_a_resolved_scope(
        self, recall_services: AgentServices, captured_tools: Dict[str, Any]
    ) -> None:
        self._build_recall(recall_services, captured_tools, memory_agent_id=None)

        assert "recall" not in captured_tools

    async def test_not_mounted_without_a_repo(
        self, services: AgentServices, captured_tools: Dict[str, Any]
    ) -> None:
        self._build_recall(services, captured_tools)

        assert "recall" not in captured_tools


class TestRecallConversation:
    """recall_conversation: FTS5 search over raw past messages (dv-r3c1).

    The load-bearing property is the same as MessageRepository.search_fts's
    own tests: scoping. Repeated here at the tool boundary because the tool
    is what decides whether to mount at all when user_id is missing.
    """

    @pytest.fixture
    async def conv_db(self, tmp_path: Path):
        db = DatabaseManager(str(tmp_path / "conv.db"))
        await db.initialize()
        yield db
        await db.close()

    @pytest.fixture
    def message_repo(self, conv_db: DatabaseManager) -> MessageRepository:
        return MessageRepository(conv_db)

    async def _seed_message(
        self,
        conv_db: DatabaseManager,
        message_repo: MessageRepository,
        user_id: int,
        session_id: str,
        working_dir: str,
        prompt: str,
        response: str = "",
    ) -> None:
        users = UserRepository(conv_db)
        sessions = SessionRepository(conv_db)
        # A test may seed the same user across sessions/working dirs; guard
        # the duplicate-insert IntegrityError rather than tracking state.
        try:
            await users.create_user(
                UserModel(
                    user_id=user_id,
                    telegram_username=f"user{user_id}",
                    first_seen=datetime.now(UTC),
                    last_active=datetime.now(UTC),
                    is_allowed=True,
                )
            )
        except sqlite3.IntegrityError:
            pass
        await sessions.create_session(
            SessionModel(
                session_id=session_id,
                user_id=user_id,
                project_path=working_dir,
                created_at=datetime.now(UTC),
                last_used=datetime.now(UTC),
            )
        )
        await message_repo.save_message(
            MessageModel(
                session_id=session_id,
                user_id=user_id,
                timestamp=datetime.now(UTC),
                prompt=prompt,
                response=response,
            )
        )

    @pytest.fixture
    def conv_services(
        self,
        event_bus: FakeEventBus,
        scheduler: AsyncMock,
        message_repo: MessageRepository,
    ) -> AgentServices:
        return AgentServices(
            event_bus=event_bus,
            task_repo=AsyncMock(),
            scheduler=scheduler,
            message_repo=message_repo,
        )

    @staticmethod
    def _build_conv(
        services: AgentServices,
        captured: Dict[str, Any],
        user_id: int | None = 111,
        working_directory: Path = Path("/tmp/ws"),
    ) -> Dict[str, Any]:
        build_agent_mcp_server(
            services,
            "devops",
            working_directory,
            requester_chat_id=555,
            user_id=user_id,
        )
        return captured

    async def test_finds_message_by_query(
        self,
        conv_db: DatabaseManager,
        message_repo: MessageRepository,
        conv_services: AgentServices,
        captured_tools: Dict[str, Any],
    ) -> None:
        await self._seed_message(
            conv_db,
            message_repo,
            111,
            "s1",
            "/tmp/ws",
            "we discussed the deploy window yesterday",
            "agreed on Tuesdays",
        )
        tools = self._build_conv(conv_services, captured_tools)

        result = await tools["recall_conversation"].handler({"query": "deploy window"})

        assert result.get("isError") is not True
        assert "deploy window" in result["content"][0]["text"]

    async def test_query_is_required(
        self, conv_services: AgentServices, captured_tools: Dict[str, Any]
    ) -> None:
        tools = self._build_conv(conv_services, captured_tools)

        result = await tools["recall_conversation"].handler({"query": ""})

        assert result.get("isError") is True

    async def test_scoped_to_calling_user(
        self,
        conv_db: DatabaseManager,
        message_repo: MessageRepository,
        conv_services: AgentServices,
        captured_tools: Dict[str, Any],
    ) -> None:
        await self._seed_message(
            conv_db, message_repo, 111, "s-a", "/tmp/ws", "user A secret plan"
        )
        await self._seed_message(
            conv_db, message_repo, 222, "s-b", "/tmp/ws", "unrelated message"
        )

        tools = self._build_conv(conv_services, captured_tools, user_id=222)
        result = await tools["recall_conversation"].handler({"query": "secret plan"})

        assert result["content"][0]["text"].strip() == "[]"

    async def test_scoped_to_calling_working_dir(
        self,
        conv_db: DatabaseManager,
        message_repo: MessageRepository,
        conv_services: AgentServices,
        captured_tools: Dict[str, Any],
    ) -> None:
        await self._seed_message(
            conv_db,
            message_repo,
            111,
            "s-devops",
            "/tmp/devops",
            "the on-call vault secret rotates weekly",
        )
        await self._seed_message(
            conv_db, message_repo, 111, "s-coach", "/tmp/coach", "unrelated chat"
        )

        tools = self._build_conv(
            conv_services, captured_tools, working_directory=Path("/tmp/coach")
        )
        result = await tools["recall_conversation"].handler({"query": "vault secret"})

        assert result["content"][0]["text"].strip() == "[]"

    async def test_limit_is_capped(
        self,
        conv_db: DatabaseManager,
        message_repo: MessageRepository,
        conv_services: AgentServices,
        captured_tools: Dict[str, Any],
    ) -> None:
        for i in range(agent_tools.RECALL_CONVERSATION_MAX_LIMIT + 10):
            await self._seed_message(
                conv_db,
                message_repo,
                111,
                f"s-{i}",
                "/tmp/ws",
                f"capped keyword message {i}",
            )
        tools = self._build_conv(conv_services, captured_tools)

        result = await tools["recall_conversation"].handler(
            {"query": "capped keyword", "limit": 9999}
        )

        payload = json.loads(result["content"][0]["text"])
        assert len(payload) == agent_tools.RECALL_CONVERSATION_MAX_LIMIT

    async def test_not_mounted_without_user_id(
        self, conv_services: AgentServices, captured_tools: Dict[str, Any]
    ) -> None:
        self._build_conv(conv_services, captured_tools, user_id=None)

        assert "recall_conversation" not in captured_tools

    async def test_not_mounted_without_a_repo(
        self, services: AgentServices, captured_tools: Dict[str, Any]
    ) -> None:
        self._build_conv(services, captured_tools)

        assert "recall_conversation" not in captured_tools


class TestMemoryAudit:
    """dv-a9e1 audit tools: list / forget / correct.

    Load-bearing properties: every op resolves inside the session's own
    scopes (a foreign fact id is indistinguishable from a nonexistent one),
    forget CLOSES and never deletes, and correct is the same
    invalidate-and-insert the evaluator uses — no text merging.
    """

    @pytest.fixture
    async def memory_repo(self, tmp_path: Path):
        db = DatabaseManager(str(tmp_path / "mem.db"))
        await db.initialize()
        yield MemoryRepository(db)
        await db.close()

    @pytest.fixture
    def audit_writer(self, memory_repo: MemoryRepository) -> MemoryWriter:
        return MemoryWriter(memory_repo, source="user_request")

    @pytest.fixture
    def audit_services(
        self,
        event_bus: FakeEventBus,
        scheduler: AsyncMock,
        memory_repo: MemoryRepository,
        audit_writer: MemoryWriter,
    ) -> AgentServices:
        return AgentServices(
            event_bus=event_bus,
            task_repo=AsyncMock(),
            scheduler=scheduler,
            memory_repo=memory_repo,
            audit_memory_writer=audit_writer,
        )

    @staticmethod
    def _build_audit(
        services: AgentServices,
        captured: Dict[str, Any],
        memory_agent_id: str | None = "coach#u111",
        memory_family_agent_id: str | None = None,
    ) -> Dict[str, Any]:
        build_agent_mcp_server(
            services,
            "coach",
            Path("/tmp/ws"),
            requester_chat_id=555,
            memory_agent_id=memory_agent_id,
            memory_family_agent_id=memory_family_agent_id,
        )
        return captured

    # ---- memory_list ----

    async def test_list_matches_active_rows_in_db(
        self,
        audit_services: AgentServices,
        captured_tools: Dict[str, Any],
        memory_repo: MemoryRepository,
    ) -> None:
        """Criterion 1: the list is the DB's active set, nothing else."""
        own_a = await memory_repo.add_fact("coach#u111", "general", "owns three cats")
        own_b = await memory_repo.add_fact("coach#u111", "pinned", "no walnuts")
        closed = await memory_repo.add_fact("coach#u111", "general", "old fact")
        await memory_repo.invalidate_fact(closed, "coach#u111")
        await memory_repo.add_fact("coach#u999", "general", "someone else's fact")
        tools = self._build_audit(audit_services, captured_tools)

        result = await tools["memory_list"].handler({})

        payload = json.loads(result["content"][0]["text"])
        assert {f["id"] for f in payload} == {own_a, own_b}
        assert all(f["scope"] == "private" for f in payload)

    async def test_list_includes_family_scope(
        self,
        audit_services: AgentServices,
        captured_tools: Dict[str, Any],
        memory_repo: MemoryRepository,
    ) -> None:
        await memory_repo.add_fact("coach#u111", "general", "private fact")
        await memory_repo.add_fact("f7:coach", "general", "family fact")
        tools = self._build_audit(
            audit_services, captured_tools, memory_family_agent_id="f7:coach"
        )

        result = await tools["memory_list"].handler({})

        payload = json.loads(result["content"][0]["text"])
        assert {(f["content"], f["scope"]) for f in payload} == {
            ("private fact", "private"),
            ("family fact", "family"),
        }

    async def test_list_history_shows_supersede_chain(
        self,
        audit_services: AgentServices,
        captured_tools: Dict[str, Any],
        memory_repo: MemoryRepository,
    ) -> None:
        old_id = await memory_repo.add_fact("coach#u111", "general", "height 180")
        new_id = await memory_repo.supersede_fact(
            old_id, "coach#u111", "general", "height 185"
        )
        tools = self._build_audit(audit_services, captured_tools)

        # The chain is reachable from either end.
        for probe in (old_id, new_id):
            result = await tools["memory_list"].handler({"fact_id": probe})
            payload = json.loads(result["content"][0]["text"])
            assert [f["id"] for f in payload] == [old_id, new_id]
            assert payload[0]["invalid_at"] is not None
            assert payload[0]["superseded_by"] == new_id
            assert payload[1]["invalid_at"] is None

    async def test_list_history_rejects_foreign_id(
        self,
        audit_services: AgentServices,
        captured_tools: Dict[str, Any],
        memory_repo: MemoryRepository,
    ) -> None:
        foreign = await memory_repo.add_fact("coach#u999", "general", "not yours")
        tools = self._build_audit(audit_services, captured_tools)

        result = await tools["memory_list"].handler({"fact_id": foreign})

        assert result.get("isError") is True

    # ---- memory_forget ----

    async def test_forget_closes_but_never_deletes(
        self,
        audit_services: AgentServices,
        captured_tools: Dict[str, Any],
        memory_repo: MemoryRepository,
    ) -> None:
        """Criterion 2: invalid_at set, provenance user_request, row kept."""
        fact_id = await memory_repo.add_fact("coach#u111", "general", "drinks oolong")
        tools = self._build_audit(audit_services, captured_tools)

        result = await tools["memory_forget"].handler(
            {"fact_id": fact_id, "reason": "user asked"}
        )

        assert result.get("isError") is not True
        assert "forgotten" in result["content"][0]["text"]
        assert await memory_repo.get_facts("coach#u111") == []  # gone from active
        row = await memory_repo.get_fact(fact_id, "coach#u111")  # but still there
        assert row is not None
        assert row.invalid_at is not None
        assert row.invalidated_by == "user_request"
        assert row.content == "drinks oolong"

    async def test_forget_rejects_foreign_id_and_leaves_row_untouched(
        self,
        audit_services: AgentServices,
        captured_tools: Dict[str, Any],
        memory_repo: MemoryRepository,
    ) -> None:
        foreign = await memory_repo.add_fact("coach#u999", "general", "not yours")
        tools = self._build_audit(audit_services, captured_tools)

        result = await tools["memory_forget"].handler({"fact_id": foreign})

        assert result.get("isError") is True
        row = await memory_repo.get_fact(foreign, "coach#u999")
        assert row is not None and row.invalid_at is None

    async def test_forget_twice_reports_already_closed(
        self,
        audit_services: AgentServices,
        captured_tools: Dict[str, Any],
        memory_repo: MemoryRepository,
    ) -> None:
        fact_id = await memory_repo.add_fact("coach#u111", "general", "some fact")
        tools = self._build_audit(audit_services, captured_tools)

        await tools["memory_forget"].handler({"fact_id": fact_id})
        result = await tools["memory_forget"].handler({"fact_id": fact_id})

        assert result.get("isError") is not True
        assert "already_closed" in result["content"][0]["text"]

    async def test_forget_family_fact_via_family_scope(
        self,
        audit_services: AgentServices,
        captured_tools: Dict[str, Any],
        memory_repo: MemoryRepository,
    ) -> None:
        fact_id = await memory_repo.add_fact("f7:coach", "general", "family fact")
        tools = self._build_audit(
            audit_services, captured_tools, memory_family_agent_id="f7:coach"
        )

        result = await tools["memory_forget"].handler({"fact_id": fact_id})

        assert result.get("isError") is not True
        row = await memory_repo.get_fact(fact_id, "f7:coach")
        assert row is not None and row.invalid_at is not None

    # ---- memory_correct ----

    async def test_correct_is_invalidate_and_insert_no_merge(
        self,
        audit_services: AgentServices,
        captured_tools: Dict[str, Any],
        memory_repo: MemoryRepository,
    ) -> None:
        """Criterion 3: old row closed + linked; new row byte-identical."""
        old_id = await memory_repo.add_fact(
            "coach#u111", "pinned", "allergic to walnuts", label="allergies"
        )
        tools = self._build_audit(audit_services, captured_tools)

        result = await tools["memory_correct"].handler(
            {"fact_id": old_id, "new_fact": "allergic to hazelnuts"}
        )

        assert result.get("isError") is not True
        assert "corrected" in result["content"][0]["text"]
        old = await memory_repo.get_fact(old_id, "coach#u111")
        assert old is not None
        assert old.invalid_at is not None
        assert old.superseded_by is not None
        assert old.content == "allergic to walnuts"  # untouched
        new = await memory_repo.get_fact(old.superseded_by, "coach#u111")
        assert new is not None
        assert new.content == "allergic to hazelnuts"  # verbatim
        assert new.source == "user_request"
        assert new.tier == "pinned"  # carried over

    async def test_correct_noop_when_text_is_the_same(
        self,
        audit_services: AgentServices,
        captured_tools: Dict[str, Any],
        memory_repo: MemoryRepository,
    ) -> None:
        old_id = await memory_repo.add_fact("coach#u111", "general", "likes spicy food")
        tools = self._build_audit(audit_services, captured_tools)

        result = await tools["memory_correct"].handler(
            {"fact_id": old_id, "new_fact": "Likes spicy food!"}
        )

        assert result.get("isError") is not True
        assert "unchanged" in result["content"][0]["text"]
        row = await memory_repo.get_fact(old_id, "coach#u111")
        assert row is not None and row.invalid_at is None

    async def test_correct_rejects_foreign_id(
        self,
        audit_services: AgentServices,
        captured_tools: Dict[str, Any],
        memory_repo: MemoryRepository,
    ) -> None:
        foreign = await memory_repo.add_fact("coach#u999", "general", "not yours")
        tools = self._build_audit(audit_services, captured_tools)

        result = await tools["memory_correct"].handler(
            {"fact_id": foreign, "new_fact": "hijacked"}
        )

        assert result.get("isError") is True
        assert await memory_repo.get_facts("coach#u999") != []

    # ---- mounting & schemas ----

    async def test_write_tools_not_mounted_without_audit_writer(
        self,
        event_bus: FakeEventBus,
        scheduler: AsyncMock,
        memory_repo: MemoryRepository,
        captured_tools: Dict[str, Any],
    ) -> None:
        services = AgentServices(
            event_bus=event_bus,
            task_repo=AsyncMock(),
            scheduler=scheduler,
            memory_repo=memory_repo,
        )
        self._build_audit(services, captured_tools)

        assert "memory_list" in captured_tools  # read path needs only the repo
        assert "memory_forget" not in captured_tools
        assert "memory_correct" not in captured_tools

    async def test_nothing_mounted_without_memory_scope(
        self, audit_services: AgentServices, captured_tools: Dict[str, Any]
    ) -> None:
        self._build_audit(audit_services, captured_tools, memory_agent_id=None)

        for name in ("memory_list", "memory_forget", "memory_correct"):
            assert name not in captured_tools

    async def test_schemas_expose_no_scope_identifier(
        self, audit_services: AgentServices, captured_tools: Dict[str, Any]
    ) -> None:
        tools = self._build_audit(audit_services, captured_tools)

        # dv-6f2a: params live under "properties" now (explicit schema).
        assert set(tools["memory_list"].input_schema["properties"]) == {"fact_id"}
        assert set(tools["memory_forget"].input_schema["properties"]) == {
            "fact_id",
            "reason",
        }
        assert set(tools["memory_correct"].input_schema["properties"]) == {
            "fact_id",
            "new_fact",
        }


class TestJobToolsRoleGate:
    """dv-a900: background-job tools are owner-only and fail closed.

    ``scripts/bgjob`` runs the raw CLI with ``bypassPermissions``, outside
    every per-turn SDK protection — so the tools must not exist for anyone
    but an exactly-resolved owner, and must refuse at invocation time as a
    second, independent gate.
    """

    JOB_TOOLS = ("job_start", "job_list", "job_output", "jobs_refresh_pin")

    @staticmethod
    def _build_role(
        services: AgentServices,
        captured: Dict[str, Any],
        role: str | None,
    ) -> Dict[str, Any]:
        build_agent_mcp_server(
            services,
            "general",
            Path("/tmp/ws"),
            requester_chat_id=555,
            role=role,
        )
        return captured

    async def test_owner_gets_job_tools(
        self, services: AgentServices, captured_tools: Dict[str, Any]
    ) -> None:
        tools = self._build_role(services, captured_tools, role="owner")

        for name in self.JOB_TOOLS:
            assert name in tools

    async def test_guest_gets_no_job_tools(
        self, services: AgentServices, captured_tools: Dict[str, Any]
    ) -> None:
        tools = self._build_role(services, captured_tools, role="guest")

        for name in self.JOB_TOOLS:
            assert name not in tools
        # The rest of the agent server is unaffected by the gate.
        assert "notify" in tools
        assert "get_weather" in tools

    async def test_unresolved_role_fails_closed(
        self, services: AgentServices, captured_tools: Dict[str, Any]
    ) -> None:
        """None (webhook/scheduler callers with no human role) mounts nothing."""
        tools = self._build_role(services, captured_tools, role=None)

        for name in self.JOB_TOOLS:
            assert name not in tools

    async def test_role_defaults_to_no_job_tools(
        self, services: AgentServices, captured_tools: Dict[str, Any]
    ) -> None:
        """A caller that never resolves a role must also fail closed."""
        tools = _build(services, captured_tools, requester_chat_id=555)

        for name in self.JOB_TOOLS:
            assert name not in tools

    async def test_invocation_refuses_non_owner_even_if_mounted(
        self,
        services: AgentServices,
        captured_tools: Dict[str, Any],
        event_bus: FakeEventBus,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The invocation gate is independent of the mount-time filter.

        Simulate a mis-wire that mounts the tools in a guest session; every
        call must still be refused before bgjob is ever spawned.
        """
        monkeypatch.setattr(agent_tools, "_job_tools_allowed", lambda role: True)
        tools = self._build_role(services, captured_tools, role="guest")

        for name, args in (
            ("job_start", {"prompt": "deep research", "name": "probe"}),
            ("job_list", {}),
            ("job_output", {"id": "123"}),
        ):
            result = await tools[name].handler(args)
            assert result.get("isError") is True, name
            assert "not available" in result["content"][0]["text"], name
        # jobs_refresh_pin swallows the bgjob error by design; the property
        # that matters is that it neither spawns bgjob nor publishes a pin.
        await tools["jobs_refresh_pin"].handler({})
        assert event_bus.published == []

    async def test_owner_invocation_reaches_bgjob(
        self,
        services: AgentServices,
        captured_tools: Dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Owner behavior is unchanged: the call passes the gate to bgjob."""

        class FakeProc:
            returncode = 0

            async def communicate(self) -> tuple[bytes, bytes]:
                return (b"started job 123", b"")

        spawned: list = []

        async def fake_exec(*argv: str, **kwargs: Any) -> FakeProc:
            spawned.append(argv)
            return FakeProc()

        monkeypatch.setattr(agent_tools.asyncio, "create_subprocess_exec", fake_exec)
        tools = self._build_role(services, captured_tools, role="owner")

        result = await tools["job_start"].handler(
            {"prompt": "deep research", "name": "probe"}
        )

        assert result.get("isError") is not True
        assert "started job 123" in result["content"][0]["text"]
        assert len(spawned) == 1
        assert "--profile" in spawned[0] and "general" in spawned[0]


class TestJobStartCommand:
    async def _tools(self, services, captured, role="owner"):
        build_agent_mcp_server(
            services, "general", Path("/tmp/ws"), requester_chat_id=555, role=role
        )
        return captured

    async def test_prompt_and_command_are_exclusive(self, services, captured_tools):
        tools = await self._tools(services, captured_tools)
        for a in ({}, {"prompt": "x", "command": "ls"}):
            r = await tools["job_start"].handler(a)
            assert r.get("isError") is True

    async def test_denied_command_never_spawns(
        self, services, captured_tools, monkeypatch
    ):
        spawned: list = []

        async def fake_exec(*argv: str, **kw: Any) -> Any:
            spawned.append(argv)

        monkeypatch.setattr(agent_tools.asyncio, "create_subprocess_exec", fake_exec)
        tools = await self._tools(services, captured_tools)
        r = await tools["job_start"].handler({"command": "cat /etc/shadow"})
        assert r.get("isError") is True
        assert spawned == []

    async def test_allowed_command_reaches_bgjob_with_cmd_flag(
        self, services, captured_tools, monkeypatch
    ):
        class FakeProc:
            returncode = 0

            async def communicate(self) -> tuple[bytes, bytes]:
                return (b"started x", b"")

        spawned: list = []

        async def fake_exec(*argv: str, **kw: Any) -> FakeProc:
            spawned.append(argv)
            return FakeProc()

        monkeypatch.setattr(agent_tools.asyncio, "create_subprocess_exec", fake_exec)
        monkeypatch.setattr(
            agent_tools, "_command_job_argv", lambda s, c, cmd: (cmd, "")
        )
        tools = await self._tools(services, captured_tools)
        r = await tools["job_start"].handler({"command": "make test", "name": "t"})
        assert r.get("isError") is not True
        assert "--cmd" in spawned[0] and "make test" in spawned[0]


def _build_devops(services: AgentServices, captured: Dict[str, Any]) -> Dict[str, Any]:
    """task_done is mounted for devops only (dv-535b)."""
    build_agent_mcp_server(services, "devops", Path("/tmp/ws"))
    return captured


class TestTaskBoard:
    """dv-1327: the task tools write the deviz board, not agent_tasks."""

    @pytest.fixture
    def board(self, tmp_path: Path) -> DevizBoard:
        return DevizBoard(tmp_path / "tasks")

    @pytest.fixture
    def services(self, event_bus: FakeEventBus, board: DevizBoard) -> AgentServices:
        return AgentServices(event_bus=event_bus, task_repo=board)

    async def test_task_add_creates_a_dv_task_labelled_with_the_profile(
        self,
        services: AgentServices,
        captured_tools: Dict[str, Any],
        board: DevizBoard,
    ) -> None:
        tools = _build(services, captured_tools)

        result = await tools["task_add"].handler(
            {
                "title": "Разбор недели",
                "body": "details",
                "assignee": "user",
                "due": "2026-08-18T18:00:00+01:00",
            }
        )

        payload = json.loads(result["content"][0]["text"])
        assert payload["status"] == "created"
        task_id = payload["task_id"]
        assert task_id.startswith("dv-")

        task = await board.get_task(task_id)
        assert task is not None
        assert task.title == "Разбор недели"
        assert task.created_by == "general"  # attribution, not the model's word
        assert task.labels == ["general"]
        assert task.due_at is not None

    async def test_task_list_then_done_round_trip(
        self,
        services: AgentServices,
        captured_tools: Dict[str, Any],
        board: DevizBoard,
    ) -> None:
        tools = _build_devops(services, captured_tools)
        task_id = await board.add_task(title="existing", created_by="devops")

        listed = json.loads(
            (await tools["task_list"].handler({}))["content"][0]["text"]
        )
        assert [t["id"] for t in listed] == [task_id]

        done = json.loads(
            (await tools["task_done"].handler({"task_id": task_id}))["content"][0][
                "text"
            ]
        )
        assert done == {"status": "updated", "task_id": task_id}
        task = await board.get_task(task_id)
        assert task is not None and task.status == "done"

    async def test_task_done_on_an_unknown_id_reports_not_found(
        self, services: AgentServices, captured_tools: Dict[str, Any]
    ) -> None:
        tools = _build_devops(services, captured_tools)

        result = await tools["task_done"].handler({"task_id": "dv-0000"})

        assert json.loads(result["content"][0]["text"])["status"] == "not_found"
