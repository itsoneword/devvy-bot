"""Reports-group routing (dv-e215)."""

from types import SimpleNamespace
from typing import Any, Dict, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram.error import BadRequest, TelegramError
from telegram.ext import ApplicationHandlerStop

from src.bot.core import ClaudeCodeBot
from src.config.settings import Settings
from src.events.bus import EventBus
from src.events.types import AgentResponseEvent
from src.notifications.routing import ReportsRouter
from src.notifications.service import NotificationService
from src.utils.constants import DEFAULT_REPORTS_ROUTES

OWNER = 100
REPORTS = -1004290210285


class FakeRepo:
    def __init__(self) -> None:
        self.rows: Dict[tuple, Any] = {}

    async def get_by_chat_project(self, chat_id: int, slug: str) -> Optional[Any]:
        return self.rows.get((chat_id, slug))

    async def upsert_mapping(
        self,
        project_slug: str,
        chat_id: int,
        message_thread_id: int,
        topic_name: str,
        is_active: bool = True,
    ) -> Any:
        row = SimpleNamespace(
            message_thread_id=message_thread_id,
            topic_name=topic_name,
            is_active=is_active,
        )
        self.rows[(chat_id, project_slug)] = row
        return row


@pytest.fixture
def bot() -> AsyncMock:
    b = AsyncMock()
    counter = iter(range(10, 1000))
    b.create_forum_topic = AsyncMock(
        side_effect=lambda **kw: SimpleNamespace(message_thread_id=next(counter))
    )
    return b


def make_service(bot: AsyncMock, repo: Optional[FakeRepo] = None, on: bool = True):
    router = (
        ReportsRouter(
            REPORTS, dict(DEFAULT_REPORTS_ROUTES), repo or FakeRepo(), [OWNER]
        )
        if on
        else None
    )
    svc = NotificationService(
        event_bus=EventBus(), bot=bot, default_chat_ids=[OWNER], reports_router=router
    )
    return svc


async def deliver(svc: NotificationService, source: str, **kw: Any) -> None:
    ev = AgentResponseEvent(chat_id=0, text="hi", parse_mode=None, source=source, **kw)
    await svc._deliver(OWNER, ev)


async def test_unset_setting_routes_nothing(bot: AsyncMock) -> None:
    svc = make_service(bot, on=False)
    await deliver(svc, "taskpump:status")
    kw = bot.send_message.call_args.kwargs
    assert kw["chat_id"] == OWNER
    assert "message_thread_id" not in kw
    bot.create_forum_topic.assert_not_called()


@pytest.mark.parametrize(
    "source,topic",
    [
        ("taskpump:status", "Pump"),
        ("bgjob", "Jobs"),
        ("bgjob:digest", "Jobs"),
        ("server-monitor:host1", "Monitoring"),
        ("schedule:daily-report", "Reports"),
        ("schedule:crypto", "Markets"),
    ],
)
async def test_mapped_source_goes_to_group_topic(
    bot: AsyncMock, source: str, topic: str
) -> None:
    repo = FakeRepo()
    svc = make_service(bot, repo)
    await deliver(svc, source)
    kw = bot.send_message.call_args.kwargs
    assert kw["chat_id"] == REPORTS
    assert (
        kw["message_thread_id"]
        == repo.rows[(REPORTS, f"reports:{topic.lower()}")].message_thread_id
    )
    assert bot.create_forum_topic.call_args.kwargs["name"] == topic


async def test_topic_created_once_and_persisted(bot: AsyncMock) -> None:
    repo = FakeRepo()
    await deliver(make_service(bot, repo), "bgjob")
    # New service instance (restart) reuses the persisted id.
    await deliver(make_service(bot, repo), "bgjob")
    assert bot.create_forum_topic.await_count == 1


@pytest.mark.parametrize(
    "source", ["reminder", "item", "family", "agent", "unknown"]
)
async def test_answer_needed_and_unmapped_stay_in_dm(
    bot: AsyncMock, source: str
) -> None:
    await deliver(make_service(bot), source)
    assert bot.send_message.call_args.kwargs["chat_id"] == OWNER
    bot.create_forum_topic.assert_not_called()


async def test_taskpump_ask_goes_to_pump_topic(bot: AsyncMock) -> None:
    await deliver(make_service(bot), "taskpump:ask")
    assert bot.send_message.call_args.kwargs["chat_id"] == REPORTS
    assert bot.create_forum_topic.call_args.kwargs["name"] == "Pump"


async def test_ledgered_reminder_and_other_chats_stay_in_dm(bot: AsyncMock) -> None:
    svc = make_service(bot)
    await deliver(svc, "bgjob", ledger_kind="scheduled_reminder")
    assert bot.send_message.call_args.kwargs["chat_id"] == OWNER
    ev = AgentResponseEvent(chat_id=555, text="x", parse_mode=None, source="bgjob")
    await svc._deliver(555, ev)  # a family member's chat
    assert bot.send_message.call_args.kwargs["chat_id"] == 555


async def test_send_failure_falls_back_to_dm(bot: AsyncMock) -> None:
    async def send(**kw: Any) -> Any:
        if kw["chat_id"] == REPORTS:
            raise TelegramError("Forbidden: bot was kicked")
        return SimpleNamespace(chat=SimpleNamespace(id=kw["chat_id"]), message_id=1)

    bot.send_message = AsyncMock(side_effect=send)
    await deliver(make_service(bot), "bgjob")
    assert bot.send_message.call_args.kwargs["chat_id"] == OWNER


async def test_topic_create_failure_falls_back_to_dm(bot: AsyncMock) -> None:
    bot.create_forum_topic = AsyncMock(side_effect=BadRequest("Chat is not a forum"))
    await deliver(make_service(bot), "bgjob")
    assert bot.send_message.call_args.kwargs["chat_id"] == OWNER


async def test_deleted_topic_recreated_once(bot: AsyncMock) -> None:
    repo = FakeRepo()
    repo.rows[(REPORTS, "reports:jobs")] = SimpleNamespace(
        message_thread_id=5, topic_name="Jobs", is_active=True
    )

    async def send(**kw: Any) -> Any:
        if kw.get("message_thread_id") == 5:
            raise BadRequest("Message thread not found")
        return SimpleNamespace(chat=SimpleNamespace(id=kw["chat_id"]), message_id=1)

    bot.send_message = AsyncMock(side_effect=send)
    await deliver(make_service(bot, repo), "bgjob")
    assert bot.create_forum_topic.await_count == 1
    last = bot.send_message.call_args.kwargs
    assert last["chat_id"] == REPORTS and last["message_thread_id"] != 5
    assert (
        repo.rows[(REPORTS, "reports:jobs")].message_thread_id
        == last["message_thread_id"]
    )


async def test_pinned_status_posts_inside_topic(bot: AsyncMock) -> None:
    bot.send_message = AsyncMock(
        return_value=SimpleNamespace(chat=SimpleNamespace(id=REPORTS), message_id=7)
    )
    await deliver(make_service(bot), "bgjob:status", pin_as="jobs")
    kw = bot.send_message.call_args.kwargs
    assert kw["chat_id"] == REPORTS and "message_thread_id" in kw
    bot.pin_chat_message.assert_awaited_once()



async def test_first_pump_pin_unpins_old_topic_pins_then_edits_in_place(
    bot: AsyncMock,
) -> None:
    """dv-3d3d: the stray 15.10 pin in the Pump topic goes when ours is made."""
    bot.send_message = AsyncMock(
        return_value=SimpleNamespace(chat=SimpleNamespace(id=REPORTS), message_id=7)
    )
    svc = make_service(bot)
    await deliver(svc, "taskpump:status", pin_as="pump")

    thread = bot.send_message.call_args.kwargs["message_thread_id"]
    bot.unpin_all_forum_topic_messages.assert_awaited_once()
    ukw = bot.unpin_all_forum_topic_messages.call_args.kwargs
    assert ukw["chat_id"] == REPORTS and ukw["message_thread_id"] == thread
    names = [c[0] for c in bot.mock_calls]
    assert names.index("unpin_all_forum_topic_messages") < names.index(
        "pin_chat_message"
    )

    # Next tick: same message edited in place, nothing re-posted or re-cleared.
    await deliver(svc, "taskpump:status", pin_as="pump")
    assert bot.send_message.await_count == 1
    bot.edit_message_text.assert_awaited_once()
    assert bot.edit_message_text.call_args.kwargs["message_id"] == 7
    bot.unpin_all_forum_topic_messages.assert_awaited_once()


async def test_transient_pin_edit_error_does_not_post_a_second_pin(
    bot: AsyncMock,
) -> None:
    """dv-3d3d: only a vanished message is re-created; a hiccup waits a tick."""
    bot.send_message = AsyncMock(
        return_value=SimpleNamespace(chat=SimpleNamespace(id=REPORTS), message_id=7)
    )
    svc = make_service(bot)
    await deliver(svc, "taskpump:status", pin_as="pump")
    bot.edit_message_text = AsyncMock(side_effect=TelegramError("Bad Gateway"))

    await deliver(svc, "taskpump:status", pin_as="pump")

    assert bot.send_message.await_count == 1
    assert bot.pin_chat_message.await_count == 1
    assert svc._pinned_messages == {(REPORTS, "pump"): 7}

    # A really deleted message is re-created, and the old id unpinned first.
    bot.edit_message_text = AsyncMock(
        side_effect=BadRequest("Message to edit not found")
    )
    bot.send_message = AsyncMock(
        return_value=SimpleNamespace(chat=SimpleNamespace(id=REPORTS), message_id=8)
    )
    await deliver(svc, "taskpump:status", pin_as="pump")
    assert bot.send_message.await_count == 1
    assert svc._pinned_messages == {(REPORTS, "pump"): 8}
    unpinned = [c.kwargs.get("message_id") for c in bot.unpin_chat_message.call_args_list]
    assert 7 in unpinned


def test_routes_setting_parses_pairs_and_json() -> None:
    parse = Settings.parse_reports_routes
    assert parse("a=A, b:c=B") == {"a": "A", "b:c": "B"}
    assert parse('{"X": "Y"}') == {"X": "Y"}


async def test_incoming_message_in_reports_chat_ignored() -> None:
    settings = SimpleNamespace(reports_chat_id=REPORTS)
    core = ClaudeCodeBot.__new__(ClaudeCodeBot)
    core.settings = settings  # type: ignore[assignment]
    core.deps = {}
    core.app = MagicMock()
    core._add_reports_chat_handlers()
    handlers = [c.args[0] for c in core.app.add_handler.call_args_list]
    msg_handler = next(h for h in handlers if hasattr(h, "filters"))
    assert msg_handler.filters.chat_ids == {REPORTS}
    with pytest.raises(ApplicationHandlerStop):
        await msg_handler.callback(MagicMock(), MagicMock())


async def test_my_chat_member_is_audited() -> None:
    audit = SimpleNamespace(log_chat_membership=AsyncMock())
    core = ClaudeCodeBot.__new__(ClaudeCodeBot)
    core.settings = SimpleNamespace(reports_chat_id=None)  # type: ignore[assignment]
    core.deps = {"audit_logger": audit}
    core.app = MagicMock()
    core._add_reports_chat_handlers()
    handlers = [c.args[0] for c in core.app.add_handler.call_args_list]
    assert len(handlers) == 1  # no ignore-handler when unset
    change = SimpleNamespace(
        chat=SimpleNamespace(id=REPORTS, type="supergroup", title="Reports"),
        from_user=SimpleNamespace(id=OWNER),
        old_chat_member=SimpleNamespace(status="left"),
        new_chat_member=SimpleNamespace(status="administrator"),
    )
    await handlers[0].callback(SimpleNamespace(my_chat_member=change), MagicMock())
    audit.log_chat_membership.assert_awaited_once()
    assert audit.log_chat_membership.call_args.kwargs["chat_id"] == REPORTS
