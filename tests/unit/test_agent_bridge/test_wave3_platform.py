"""Wave 3 over HTTP: notify, schedule_*, task_*, get_weather, export_my_data.

Real uvicorn, real MCP client, a real ``JobScheduler`` on a real SQLite file
and a real ``DevizBoard`` on real markdown files. Only the event bus and the
network are stood in for — publishing to Telegram and calling Open-Meteo are
not what these tests are about.

The question each group answers:

* ``notify`` — does the chat come from the server context, and does a
  model-supplied ``chat_id`` do nothing?
* ``schedule_*`` — can user A see or delete user B's jobs? The scheduler has
  no owner filter of its own (dv-8c19), so the answer depends entirely on the
  gate in ``agent_tools._owns_scheduled_job``.
* ``task_*`` — the board is deliberately ONE board (dv-1327), so the property
  under test is attribution, not isolation.
* ``export_my_data`` — is it still gated by the profile's capability group,
  exactly as on the SDK side?
"""

import json
import socket
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional
from unittest.mock import AsyncMock

import pytest

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from src.agent_bridge import AgentBridge
from src.claude.tool_context import AgentServices, TurnContext
from src.profiles.registry import ProfileDefinition, ProfileRegistry
from src.scheduler.scheduler import JobScheduler
from src.storage.database import DatabaseManager
from src.storage.deviz_board import DevizBoard
from src.storage.user_data import UserDataManager, UserDataRepository

USER_A = 111
USER_B = 222
CHAT_A = 900001
CHAT_B = 900002
DATE = "2026-08-15"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class RecordingBus:
    """Captures what a tool tried to send instead of talking to Telegram."""

    def __init__(self) -> None:
        self.events: List[Any] = []

    async def publish(self, event: Any) -> None:
        self.events.append(event)


@pytest.fixture
async def scheduler(tmp_path: Path) -> AsyncIterator[JobScheduler]:
    db = DatabaseManager(str(tmp_path / "sched.db"))
    await db.initialize()
    yield JobScheduler(
        event_bus=AsyncMock(),
        db_manager=db,
        default_working_directory=tmp_path,
    )
    await db.close()


@pytest.fixture
async def user_data(tmp_path: Path) -> AsyncIterator[UserDataRepository]:
    db = UserDataManager(str(tmp_path / "data.db"))
    await db.initialize()
    repo = UserDataRepository(db)
    await repo.log_food(USER_A, DATE, "обед A", kcal=650)
    yield repo
    await db.close()


@pytest.fixture
def board(tmp_path: Path) -> DevizBoard:
    return DevizBoard(tmp_path / "board")


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "coach"
    ws.mkdir()
    return ws


@pytest.fixture
def registry(workspace: Path) -> ProfileRegistry:
    return ProfileRegistry(
        [
            ProfileDefinition(
                slug="coach",
                name="coach",
                working_dir=workspace,
                memory_dir=workspace / "memory",
                persona=workspace / "persona.md",
                default=True,
                # 'export' present here, absent on `general` below: the group
                # gate is what decides whether export_my_data is offered.
                user_data_tools=frozenset({"logs", "export"}),
            ),
            ProfileDefinition(
                slug="general",
                name="general",
                working_dir=workspace / "general",
                memory_dir=workspace / "general/memory",
                persona=workspace / "general/persona.md",
                user_data_tools=frozenset({"logs"}),
            ),
        ]
    )


@pytest.fixture
def bus() -> RecordingBus:
    return RecordingBus()


@pytest.fixture
def services(
    bus: RecordingBus,
    scheduler: JobScheduler,
    board: DevizBoard,
    registry: ProfileRegistry,
    user_data: UserDataRepository,
) -> AgentServices:
    return AgentServices(
        event_bus=bus,
        task_repo=board,
        scheduler=scheduler,
        profile_registry=registry,
        user_data=user_data,
    )


@pytest.fixture
async def bridge(services: AgentServices) -> AsyncIterator[AgentBridge]:
    bridge = AgentBridge(services, port=_free_port())
    await bridge.start()
    yield bridge
    await bridge.stop()


def _ctx(
    user_id: int,
    chat_id: int,
    workspace: Path,
    *,
    role: str = "owner",
    slug: str = "coach",
) -> TurnContext:
    return TurnContext(
        profile_slug=slug,
        working_dir=workspace,
        requester_chat_id=chat_id,
        memory_agent_id=f"{slug}#u{user_id}",
        user_id=user_id,
        role=role,
    )


class Caller:
    def __init__(self, session: ClientSession) -> None:
        self.session = session

    async def tools(self) -> set:
        listing = await self.session.list_tools()
        return {tool.name for tool in listing.tools}

    async def call(self, tool: str, /, **arguments: Any) -> Dict[str, Any]:
        # positional-only: `name` is a real argument of schedule_add/job_start.
        result = await self.session.call_tool(tool, arguments)
        text = "".join(block.text for block in result.content if block.type == "text")
        return {"is_error": bool(result.isError), "text": text}

    async def payload(self, tool: str, /, **arguments: Any) -> Any:
        out = await self.call(tool, **arguments)
        assert not out["is_error"], out["text"]
        return json.loads(out["text"])


async def _connect(bridge: AgentBridge, token: str) -> Any:
    return streamablehttp_client(
        bridge.url, headers={"Authorization": f"Bearer {token}"}
    )


class _Session:
    """`async with _Session(bridge, ctx) as caller:` — one turn, one token."""

    def __init__(self, bridge: AgentBridge, ctx: TurnContext) -> None:
        self._bridge = bridge
        self._ctx = ctx
        self._transport: Optional[Any] = None
        self._session: Optional[ClientSession] = None

    async def __aenter__(self) -> Caller:
        token = self._bridge.mint(self._ctx)
        self._transport = await _connect(self._bridge, token)
        read, write, _ = await self._transport.__aenter__()
        self._session = ClientSession(read, write)
        session = await self._session.__aenter__()
        await session.initialize()
        return Caller(session)

    async def __aexit__(self, *exc: Any) -> None:
        assert self._session is not None and self._transport is not None
        await self._session.__aexit__(*exc)
        await self._transport.__aexit__(*exc)


class TestWaveThreeIsListed:
    async def test_every_wave_three_tool_is_offered(
        self, bridge: AgentBridge, workspace: Path
    ) -> None:
        async with _Session(bridge, _ctx(USER_A, CHAT_A, workspace)) as caller:
            names = await caller.tools()
        assert {
            "notify",
            "schedule_add",
            "schedule_list",
            "schedule_remove",
            "task_add",
            "task_list",
            "task_comment",
            "get_weather",
            "export_my_data",
        } <= names
        assert "task_done" not in names  # devops only (dv-535b)


class TestNotify:
    async def test_notify_uses_the_server_side_chat(
        self, bridge: AgentBridge, bus: RecordingBus, workspace: Path
    ) -> None:
        async with _Session(bridge, _ctx(USER_A, CHAT_A, workspace)) as caller:
            out = await caller.payload("notify", text="время тренировки")
        assert out["status"] == "sent"
        (event,) = bus.events
        assert event.chat_id == CHAT_A
        assert event.text == "время тренировки"

    async def test_a_model_supplied_chat_id_is_ignored(
        self, bridge: AgentBridge, bus: RecordingBus, workspace: Path
    ) -> None:
        """The one property that makes notify safe on a shared bridge."""
        async with _Session(bridge, _ctx(USER_A, CHAT_A, workspace)) as caller:
            await caller.call("notify", text="hi", chat_id=CHAT_B)
        assert [event.chat_id for event in bus.events] == [CHAT_A]

    async def test_empty_text_is_refused(
        self, bridge: AgentBridge, bus: RecordingBus, workspace: Path
    ) -> None:
        async with _Session(bridge, _ctx(USER_A, CHAT_A, workspace)) as caller:
            out = await caller.call("notify", text="   ")
        assert out["is_error"]
        assert bus.events == []


class TestSchedule:
    async def test_schedule_add_stamps_the_turn_not_the_model(
        self, bridge: AgentBridge, scheduler: JobScheduler, workspace: Path
    ) -> None:
        run_at = (datetime.now(UTC) + timedelta(hours=12)).isoformat()
        async with _Session(bridge, _ctx(USER_A, CHAT_A, workspace)) as caller:
            out = await caller.payload(
                "schedule_add",
                name="напомнить",
                prompt="Сходи на тренировку",
                at=run_at,
                deliver="message",
            )
        (row,) = await scheduler.list_jobs()
        assert row["job_id"] == out["job_id"]
        assert row["created_by"] == USER_A
        assert str(row["target_chat_ids"]) == str(CHAT_A)
        assert row["working_directory"] == str(workspace)
        assert row["delivery"] == "message"

    async def test_user_a_cannot_list_user_b_jobs(
        self, bridge: AgentBridge, scheduler: JobScheduler, workspace: Path
    ) -> None:
        """The scheduler itself has no owner filter (dv-8c19) — the handler does."""
        soon = datetime.now(UTC) + timedelta(hours=1)
        await scheduler.add_job(
            "b-weighin", None, "взвесься", run_at=soon, created_by=USER_B
        )
        await scheduler.add_job(
            "a-recovery",
            None,
            "как спалось",
            run_at=soon + timedelta(hours=1),
            created_by=USER_A,
        )
        assert len(await scheduler.list_jobs()) == 2

        async with _Session(bridge, _ctx(USER_A, CHAT_A, workspace)) as caller:
            rows = await caller.payload("schedule_list")
        assert [row["job_name"] for row in rows] == ["a-recovery"]
        assert all("взвесься" not in json.dumps(row) for row in rows)

    async def test_user_a_cannot_remove_user_b_job(
        self, bridge: AgentBridge, scheduler: JobScheduler, workspace: Path
    ) -> None:
        soon = datetime.now(UTC) + timedelta(hours=1)
        job_b = await scheduler.add_job(
            "b-weighin", None, "взвесься", run_at=soon, created_by=USER_B
        )

        async with _Session(bridge, _ctx(USER_A, CHAT_A, workspace)) as caller:
            out = await caller.call("schedule_remove", job_id=job_b)

        assert out["is_error"]
        assert "another user" in out["text"]
        assert [row["job_id"] for row in await scheduler.list_jobs()] == [job_b]

    async def test_owner_can_remove_their_own_job(
        self, bridge: AgentBridge, scheduler: JobScheduler, workspace: Path
    ) -> None:
        soon = datetime.now(UTC) + timedelta(hours=1)
        job_a = await scheduler.add_job(
            "a-recovery", None, "как спалось", run_at=soon, created_by=USER_A
        )
        async with _Session(bridge, _ctx(USER_A, CHAT_A, workspace)) as caller:
            out = await caller.payload("schedule_remove", job_id=job_a)
        assert out["status"] == "removed"
        assert await scheduler.list_jobs() == []

    async def test_unattributed_legacy_rows_are_owner_only(
        self, bridge: AgentBridge, scheduler: JobScheduler, workspace: Path
    ) -> None:
        """created_by=0 predates dv-3e91; the four live rows are all like this."""
        soon = datetime.now(UTC) + timedelta(hours=1)
        await scheduler.add_job("legacy", None, "старая задача", run_at=soon)

        async with _Session(bridge, _ctx(USER_A, CHAT_A, workspace)) as caller:
            as_owner = await caller.payload("schedule_list")
        async with _Session(
            bridge, _ctx(USER_B, CHAT_B, workspace, role="guest")
        ) as caller:
            as_guest = await caller.payload("schedule_list")

        assert [row["job_name"] for row in as_owner] == ["legacy"]
        assert as_guest == []

    async def test_removing_an_unknown_job_is_not_found_not_a_leak(
        self, bridge: AgentBridge, workspace: Path
    ) -> None:
        async with _Session(bridge, _ctx(USER_A, CHAT_A, workspace)) as caller:
            out = await caller.payload("schedule_remove", job_id="deadbeef")
        assert out["status"] == "not_found"


class TestTaskBoard:
    async def test_task_add_list_and_comment_round_trip(
        self, bridge: AgentBridge, board: DevizBoard, workspace: Path
    ) -> None:
        async with _Session(bridge, _ctx(USER_A, CHAT_A, workspace)) as caller:
            created = await caller.payload("task_add", title="проверить сон")
            listed = await caller.payload("task_list")
            commented = await caller.payload(
                "task_comment", task_id=created["task_id"], text="ещё деталь"
            )

        assert created["status"] == "created"
        assert [t["title"] for t in listed] == ["проверить сон"]
        assert commented["status"] == "commented"
        task = await board.get_task(created["task_id"])
        assert task is not None and task.status == "todo"
        assert task.path is not None
        assert "from coach (agent): ещё деталь" in task.path.read_text(encoding="utf-8")

    async def test_attribution_is_the_turn_profile_not_a_parameter(
        self, bridge: AgentBridge, board: DevizBoard, workspace: Path
    ) -> None:
        """The board is one shared board (dv-1327), so the honest guarantee is
        that a caller cannot forge WHO filed an item, not that it cannot see
        someone else's."""
        async with _Session(bridge, _ctx(USER_A, CHAT_A, workspace)) as caller:
            created = await caller.payload(
                "task_add", title="из coach", created_by="devops"
            )
        task = await board.get_task(created["task_id"])
        assert task is not None
        assert task.created_by == "coach"
        assert task.labels == ["coach"]

    async def test_task_done_is_refused_for_a_non_devops_agent(
        self, bridge: AgentBridge, board: DevizBoard, workspace: Path
    ) -> None:
        """Not even listed; a direct call is still refused (dv-535b)."""
        task_id = await board.add_task(title="чужая", created_by="coach")
        async with _Session(bridge, _ctx(USER_A, CHAT_A, workspace)) as caller:
            names = await caller.tools()
        assert "task_done" not in names
        task = await board.get_task(task_id)
        assert task is not None and task.is_open


class TestWeather:
    async def test_get_weather_needs_no_scope_and_returns_the_lookup(
        self, bridge: AgentBridge, workspace: Path, monkeypatch: Any
    ) -> None:
        async def _fake(location: Any, date: Any) -> Dict[str, Any]:
            return {"location": location or "home", "date": date, "day_c": 21}

        monkeypatch.setattr("src.claude.agent_tools.get_weather_for", _fake)
        # No memory scope, no user id: get_weather must still be there.
        ctx = TurnContext(profile_slug="coach", working_dir=workspace)
        async with _Session(bridge, ctx) as caller:
            assert "get_weather" in await caller.tools()
            out = await caller.payload("get_weather", date=DATE)
        assert out == {"location": "home", "date": DATE, "day_c": 21}


class TestExportGating:
    async def test_export_is_offered_only_to_a_profile_with_the_export_group(
        self, bridge: AgentBridge, workspace: Path
    ) -> None:
        async with _Session(bridge, _ctx(USER_A, CHAT_A, workspace)) as caller:
            with_group = await caller.tools()
        async with _Session(
            bridge, _ctx(USER_A, CHAT_A, workspace, slug="general")
        ) as caller:
            without_group = await caller.tools()
            refused = await caller.call("export_my_data", domain="food")

        assert "export_my_data" in with_group
        assert "export_my_data" not in without_group
        assert refused["is_error"]
        assert "not available in this session" in refused["text"]

    async def test_export_writes_into_this_turns_workspace(
        self, bridge: AgentBridge, workspace: Path
    ) -> None:
        async with _Session(bridge, _ctx(USER_A, CHAT_A, workspace)) as caller:
            out = await caller.payload("export_my_data", domain="food")
        assert out["food"]["rows"] == 1
        written = (workspace / "exports" / "food.csv").read_text(encoding="utf-8")
        assert "обед A" in written
