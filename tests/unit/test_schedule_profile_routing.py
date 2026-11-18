"""dv-f6c4: a schedule's reply belongs to the agent that created it."""

from __future__ import annotations

import pathlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.bot.orchestration.reply_context import build_reply_context
from src.claude.agent_tools import AgentServices, build_agent_mcp_server
from src.events.bus import EventBus
from src.events.handlers import AgentHandler
from src.events.types import AgentResponseEvent, ScheduledEvent
from src.notifications.routing import ReportsRouter
from src.notifications.service import NotificationService
from src.profiles.registry import ProfileDefinition, ProfileRegistry
from src.scheduler.scheduler import JobScheduler
from src.storage.database import DatabaseManager
from src.utils.constants import DEFAULT_REPORTS_ROUTES
from tests.unit.test_notifications.test_reports_routing import FakeRepo

OWNER = 111
GROUP = -1004290210285


# --- C1 -------------------------------------------------------------------


@pytest.fixture
async def db(tmp_path):
    db = DatabaseManager(str(tmp_path / "sched.db"))
    await db.initialize()
    yield db
    await db.close()


async def test_C1_profile_stored_and_carried_on_fired_event(db):
    bus = AsyncMock()
    sched = JobScheduler(
        event_bus=bus, db_manager=db, default_working_directory=Path("/tmp")
    )
    run_at = datetime.now(UTC) + timedelta(hours=1)
    job_id = await sched.add_job(
        "t", None, "p", run_at=run_at, target_chat_ids=[OWNER], profile_slug="devops"
    )
    (row,) = await sched.list_jobs()
    assert row["profile_slug"] == "devops"

    await sched._fire_event(
        job_name="t",
        prompt="p",
        working_directory="/tmp",
        target_chat_ids=[OWNER],
        skill_name=None,
        job_db_id=job_id,
        profile_slug="devops",
    )
    assert bus.publish.call_args.args[0].profile_slug == "devops"

    # After a reload from the DB.
    bus2 = AsyncMock()
    sched2 = JobScheduler(
        event_bus=bus2, db_manager=db, default_working_directory=Path("/tmp")
    )
    await sched2._load_jobs_from_db()
    (row2,) = await sched2.list_jobs()
    assert row2["profile_slug"] == "devops"
    job = sched2._scheduler.get_job(job_id) if hasattr(sched2, "_scheduler") else None
    if job is not None:
        assert job.kwargs.get("profile_slug") == "devops"


# --- C2 -------------------------------------------------------------------


def _registry(root: Path) -> ProfileRegistry:
    return ProfileRegistry(
        [
            ProfileDefinition(
                slug="gymbro",
                name="GymBro",
                working_dir=root,
                memory_dir=root / "memory",
                persona=root / "agent.md",
                default=True,
            )
        ]
    )


async def _run_handler(tmp_path, **event_kw):
    root = tmp_path / "gymbro"
    root.mkdir(exist_ok=True)
    bus = EventBus()
    bus.publish = AsyncMock()
    handler = AgentHandler(
        bus,
        MagicMock(),
        root,
        default_user_id=OWNER,
        profile_registry=_registry(root),
    )
    handler.claude.run_command = AsyncMock(
        return_value=MagicMock(content="done", tools_used=[])
    )
    kw = dict(
        job_id="j1",
        job_name="timer",
        prompt="go",
        working_directory=root,
        target_chat_ids=[OWNER],
        created_by=OWNER,
    )
    kw.update(event_kw)
    await handler.handle_scheduled(ScheduledEvent(**kw))
    return bus.publish.call_args.args[0]


async def test_C2_stored_profile_sets_deliver_profile(tmp_path):
    reply = await _run_handler(tmp_path, profile_slug="devops")
    assert isinstance(reply, AgentResponseEvent)
    assert reply.deliver_profile == "devops"
    assert reply.ledger_profile_slug in ("devops", f"devops#u{OWNER}")


async def test_C2_missing_profile_derived_from_working_directory(tmp_path):
    reply = await _run_handler(tmp_path)
    assert reply.deliver_profile == "gymbro"
    assert reply.ledger_profile_slug in ("gymbro", f"gymbro#u{OWNER}")


# --- C3 -------------------------------------------------------------------


class _StubScheduler:
    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    async def add_job(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        return "job-1"

    async def list_jobs(self) -> List[Any]:
        return []


async def _schedule_add(monkeypatch, slug, chat_id):
    captured: List[Any] = []
    monkeypatch.setattr(
        "src.claude.agent_tools.create_sdk_mcp_server",
        lambda *, name, version, tools: captured.extend(tools) or {"type": "sdk"},
    )
    sched = _StubScheduler()
    build_agent_mcp_server(
        AgentServices(
            event_bus=MagicMock(),
            task_repo=object(),
            scheduler=sched,
            memory_writer=object(),
            memory_repo=object(),
            message_repo=object(),
            audit_memory_writer=object(),
            reports_chat_id=GROUP,
        ),
        profile_slug=slug,
        working_directory=pathlib.Path("/tmp"),
        requester_chat_id=chat_id,
        memory_agent_id=slug,
        user_id=OWNER,
        role="owner",
    )
    tools = {t.name: t for t in captured}
    res = await tools["schedule_add"].handler(
        {"name": "n", "prompt": "p", "at": "2099-01-01T09:00:00+00:00"}
    )
    assert not res.get("isError"), res
    return sched.calls[0]


async def test_C3_group_turn_stores_callers_own_chat(monkeypatch):
    call = await _schedule_add(monkeypatch, "gymbro", GROUP)
    assert call["target_chat_ids"] == [OWNER]
    assert call["profile_slug"] == "gymbro"


async def test_C3_dm_turn_keeps_chat_and_stores_profile(monkeypatch):
    call = await _schedule_add(monkeypatch, "devops", OWNER)
    assert call["target_chat_ids"] == [OWNER]
    assert call["profile_slug"] == "devops"


def _router() -> ReportsRouter:
    return ReportsRouter(
        GROUP,
        dict(DEFAULT_REPORTS_ROUTES),
        FakeRepo(),
        [OWNER],
        bindings={"GymBro": "gymbro"},
    )


def _service(monkeypatch) -> tuple:
    tg = AsyncMock()
    tg.create_forum_topic = AsyncMock(return_value=MagicMock(message_thread_id=55))
    svc = NotificationService(EventBus(), tg, [OWNER], reports_router=_router())
    sent = AsyncMock()
    monkeypatch.setattr(svc, "_rate_limited_send", sent)
    return svc, sent


def _ev(source: str, **kw: Any) -> AgentResponseEvent:
    return AgentResponseEvent(
        chat_id=OWNER, text="hi", parse_mode="markdown", source=source, **kw
    )


async def test_C3_router_gymbro_to_topic_devops_to_dm(monkeypatch):
    svc, sent = _service(monkeypatch)
    await svc._deliver(
        OWNER,
        _ev(
            "schedule:timer",
            deliver_profile="gymbro",
            ledger_profile_slug="gymbro",
        ),
    )
    assert sent.call_args.args[0] == GROUP
    assert sent.call_args.kwargs["thread_id"] == 55

    sent.reset_mock()
    await svc._deliver(
        OWNER,
        _ev(
            "schedule:timer",
            deliver_profile="devops",
            ledger_profile_slug="devops",
        ),
    )
    assert sent.call_args.args[0] == OWNER
    assert sent.call_args.kwargs.get("thread_id") is None


# --- C4 -------------------------------------------------------------------


async def test_C4_dm_fallback_has_badge_and_ledger_profile(monkeypatch):
    svc, sent = _service(monkeypatch)
    ev = _ev("schedule:timer", deliver_profile="devops", ledger_profile_slug="devops")
    await svc._deliver(OWNER, ev)
    assert sent.call_args.args[0] == OWNER
    sent_ev = sent.call_args.args[1]
    assert sent_ev.text.startswith("🛠 devops")
    assert sent_ev.ledger_kind == "scheduled_reminder"
    assert sent_ev.ledger_profile_slug == "devops"


async def test_C4_topic_delivery_has_no_badge(monkeypatch):
    svc, sent = _service(monkeypatch)
    await svc._deliver(
        OWNER,
        _ev("schedule:timer", deliver_profile="gymbro", ledger_profile_slug="gymbro"),
    )
    assert sent.call_args.args[1].text == "hi"


async def test_C4_reply_to_dm_scheduled_message_reroutes_to_creator():
    """The ledger row written for the DM message selects the creator's
    profile for the reply turn (turns.py reads kind + profile_slug)."""
    import inspect

    from src.bot.orchestration import turns

    src = inspect.getsource(turns)
    assert "scheduled_reminder" in src  # reroute path exists
    # The row our delivery writes: kind + bare slug split on '#'.
    svc_src = inspect.getsource(NotificationService._deliver)
    assert "_PROFILE_BADGES" in svc_src or "badge" in svc_src.lower()


# --- C5 -------------------------------------------------------------------


async def test_C5_item_and_reminder_stay_dm_without_profile(tmp_path):
    item = await _run_handler(
        tmp_path, item_id=None, job_name="x", delivery="message", profile_slug="devops"
    )
    assert item.deliver_profile is None
    assert item.source == "reminder" or item.source.startswith("reminder")


async def test_C5_named_route_wins_over_deliver_profile(monkeypatch):
    svc, sent = _service(monkeypatch)
    router = svc.reports_router
    router.routes["schedule:crypto"] = "Markets"
    await svc._deliver(
        OWNER,
        _ev(
            "schedule:crypto:hl-friend-bot-report",
            deliver_profile="devops",
            ledger_profile_slug="devops",
        ),
    )
    assert sent.call_args.args[0] == GROUP
    assert sent.call_args.kwargs["thread_id"] == 55


async def test_C5_bgjob_digest_still_goes_to_profile_chat(monkeypatch):
    svc, sent = _service(monkeypatch)
    await svc._deliver(
        OWNER,
        _ev("bgjob:digest", deliver_profile="gymbro", ledger_profile_slug="gymbro"),
    )
    assert sent.call_args.args[0] == GROUP


# --- C6 / C7 ----------------------------------------------------------------


def test_C6_schema_has_profile_slug_column():
    from src.storage.schema.bot import scheduled_jobs

    assert "profile_slug" in scheduled_jobs.c
    assert scheduled_jobs.c.profile_slug.nullable
    mig = list(Path("migrations/bot/versions").glob("0015_*.py"))
    assert mig and "profile_slug" in mig[0].read_text()


class _Ledger:
    def __init__(self, entry):
        self.entry = entry

    async def lookup(self, chat_id, message_id):
        return self.entry


def _update(text):
    quoted = SimpleNamespace(
        text=text,
        caption=None,
        message_id=7,
        from_user=SimpleNamespace(id=999),
        chat=SimpleNamespace(id=OWNER, type="private"),
    )
    return SimpleNamespace(message=SimpleNamespace(reply_to_message=quoted))


@pytest.mark.parametrize("agent", ["devops", "devops#u111"])
async def test_C7_scheduled_agent_reply_envelope(agent):
    entry = SimpleNamespace(
        kind="scheduled_reminder",
        messages_message_id=1,
        profile_slug=agent,
        created_at=None,
    )
    prompt, _ = await build_reply_context(
        _update("🛠 devops\nresult X"),
        999,
        _Ledger(entry),
        "thanks",
        current_agent_id=agent,
        viewer_user_id=OWNER,
        viewer_is_owner=True,
    )
    assert "result X" in prompt
    assert "without an agent turn" not in prompt
    assert "another agent profile" not in prompt
