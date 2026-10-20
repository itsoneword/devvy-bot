"""Reports-group inbound (dv-e215 v2): topic -> profile, one conversation."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram.ext import ApplicationHandlerStop

from src.bot.core import ClaudeCodeBot
from src.bot.orchestration.reply_context import build_reply_context
from src.bot.orchestrator import MessageOrchestrator
from src.config.settings import Settings
from src.notifications.routing import ReportsRouter
from src.profiles.registry import ProfileDefinition, ProfileRegistry, workspace_for
from tests.unit.test_notifications.test_reports_routing import FakeRepo, make_service

GROUP = -1004290210285
OWNER = 100
BOT_ID = 7


def _router(policy: str = "owner") -> ReportsRouter:
    repo = FakeRepo()
    for slug, tid in (("coach", 21), ("monitoring", 22)):
        repo.rows[(GROUP, "reports:" + slug)] = SimpleNamespace(
            message_thread_id=tid, is_active=True
        )
    return ReportsRouter(
        GROUP,
        {"server-monitor": "Monitoring"},
        repo,
        [OWNER],
        bindings={"Coach": "coach", "Monitoring": "devops"},
        inbound_policy=policy,
    )


async def test_profile_for_thread_bound_unbound_and_wrong_chat() -> None:
    r = _router()
    assert await r.profile_for_thread(GROUP, 21) == "coach"
    assert await r.profile_for_thread(GROUP, 22) == "devops"
    assert await r.profile_for_thread(GROUP, 99) is None  # Pump etc.: unbound
    assert await r.profile_for_thread(GROUP, None) is None  # General
    assert await r.profile_for_thread(555, 21) is None


def test_policy_owner_default_and_allowed() -> None:
    assert _router().sender_allowed("owner")
    assert not _router().sender_allowed("guest")
    assert not _router().sender_allowed(None)
    assert _router("allowed").sender_allowed("guest")
    assert _router("garbage").inbound_policy == "owner"


async def test_ensure_bound_topics_creates_missing() -> None:
    bot = AsyncMock()
    bot.create_forum_topic = AsyncMock(
        return_value=SimpleNamespace(message_thread_id=77)
    )
    r = ReportsRouter(GROUP, {}, FakeRepo(), [OWNER], bindings={"Coach": "coach"})
    await r.ensure_bound_topics(bot)
    assert await r.profile_for_thread(GROUP, 77) == "coach"


# --- the gate (handler group -4) ---------------------------------------------


def _gate(router, role="owner"):
    core = ClaudeCodeBot.__new__(ClaudeCodeBot)
    core.settings = SimpleNamespace(reports_chat_id=GROUP, allowed_users=[OWNER])  # type: ignore[assignment]
    auth = SimpleNamespace(get_user_role=lambda uid: role)
    core.deps = {"reports_router": router, "auth_manager": auth}
    core.app = MagicMock()
    core._add_reports_chat_handlers()
    handlers = [c.args[0] for c in core.app.add_handler.call_args_list]
    return next(h for h in handlers if hasattr(h, "filters")).callback


def _msg(thread, text="hi", uid=OWNER):
    m = SimpleNamespace(
        chat_id=GROUP,
        text=text,
        message_thread_id=thread,
        is_topic_message=thread is not None,
    )
    return SimpleNamespace(effective_message=m, effective_user=SimpleNamespace(id=uid))


async def test_gate_owner_in_bound_topic_passes() -> None:
    assert await _gate(_router())(_msg(21), MagicMock()) is None


async def test_gate_non_owner_ignored() -> None:
    with pytest.raises(ApplicationHandlerStop):
        await _gate(_router(), role="guest")(_msg(21, uid=5), MagicMock())


async def test_gate_unbound_topic_and_commands_ignored() -> None:
    gate = _gate(_router())
    with pytest.raises(ApplicationHandlerStop):
        await gate(_msg(99), MagicMock())
    with pytest.raises(ApplicationHandlerStop):
        await gate(_msg(21, text="/start"), MagicMock())


async def test_gate_without_router_ignores_everything() -> None:
    with pytest.raises(ApplicationHandlerStop):
        await _gate(None)(_msg(21), MagicMock())


# --- the turn: one-shot route, sticky profile untouched ----------------------


def _profile(slug, root, default=False):
    return ProfileDefinition(
        slug, slug, Path(root), Path(root) / "m", Path(root) / "p.md", default=default
    )


async def test_topic_turn_reroutes_one_turn_and_keeps_sticky(tmp_path, monkeypatch):
    monkeypatch.delenv("APPROVED_DIRECTORY", raising=False)
    dev, coach = tmp_path / "dev", tmp_path / "coach"
    dev.mkdir()
    coach.mkdir()
    registry = ProfileRegistry(
        [_profile("devops", dev, default=True), _profile("coach", coach)]
    )
    settings = Settings(
        telegram_bot_token="t:t",
        telegram_bot_username="b",
        approved_directory=None,
        enable_mcp=False,
        mcp_config_path=None,
        reports_chat_id=GROUP,
    )
    orch = MessageOrchestrator(settings, {})
    orch._run_turn = AsyncMock()  # type: ignore[method-assign]

    update = MagicMock()
    update.effective_user.id = OWNER
    update.message.text = "как плечо?"
    update.message.chat_id = GROUP
    update.message.message_thread_id = 21
    update.message.is_topic_message = True
    update.message.reply_to_message = None
    update.message.chat.send_action = AsyncMock()
    update.message.reply_text = AsyncMock(return_value=AsyncMock())
    user_data = {"active_profile": "devops", "current_directory": dev}
    context = MagicMock()
    context.user_data = user_data
    context.bot_data = {
        "settings": settings,
        "profile_registry": registry,
        "reports_router": _router(),
        "rate_limiter": None,
        "auth_manager": SimpleNamespace(get_user_role=lambda uid: "owner"),
    }

    await orch.agentic_text(update, context)
    request = orch._run_turn.call_args.args[0]
    route = await request.resolve_route()

    # same workspace the DM route would use for this (user, agent)
    assert route.working_dir == workspace_for(
        registry.get_by_slug("coach"), "owner", OWNER
    )
    assert route.restore_dir == dev
    assert user_data["active_profile"] == "devops"
    assert user_data["current_directory"] == dev
    # typing indicator and "Working..." go to the same topic
    assert update.message.chat.send_action.call_args.kwargs["message_thread_id"] == 21

    # Outside any bound topic the normal sticky route applies.
    update.message.chat_id = 5
    update.message.message_thread_id = None
    update.message.is_topic_message = False
    await orch.agentic_text(update, context)
    request = orch._run_turn.call_args.args[0]
    route = await request.resolve_route()
    assert route.working_dir == workspace_for(
        registry.get_by_slug("devops"), "owner", OWNER
    )
    assert route.restore_dir is None


# --- reply context: reports in the ledger, addressee check -------------------


def _quoted(chat_id, chat_type):
    return SimpleNamespace(
        text="BTC -3%",
        caption=None,
        from_user=SimpleNamespace(id=BOT_ID, first_name="bot"),
        chat=SimpleNamespace(id=chat_id, type=chat_type),
        message_id=700,
        forward_origin=None,
    )


class _Ledger:
    def __init__(self, entry):
        self.entry = entry

    async def lookup(self, chat_id, message_id):
        return self.entry


_ENTRY = SimpleNamespace(
    kind="report", messages_message_id=None, profile_slug=None, created_at=None
)


async def _ctx(quoted, **kw):
    update = SimpleNamespace(message=SimpleNamespace(reply_to_message=quoted))
    return (await build_reply_context(update, BOT_ID, _Ledger(_ENTRY), "why?", **kw))[0]


async def test_reply_to_report_in_topic_is_context_for_owner() -> None:
    p = await _ctx(
        _quoted(GROUP, "supergroup"),
        viewer_user_id=OWNER,
        viewer_is_owner=True,
        reports_chat_id=GROUP,
    )
    assert "system notification" in p and "BTC -3%" in p


async def test_reply_to_report_not_addressed_to_viewer() -> None:
    p = await _ctx(
        _quoted(GROUP, "supergroup"),
        viewer_user_id=5,
        viewer_is_owner=False,
        reports_chat_id=GROUP,
    )
    assert "another recipient" in p
    p = await _ctx(_quoted(OWNER, "private"), viewer_user_id=5)
    assert "another recipient" in p


async def test_service_ledgers_reports_sent_into_topics() -> None:
    from src.events.types import AgentResponseEvent

    bot = AsyncMock()
    bot.create_forum_topic = AsyncMock(
        return_value=SimpleNamespace(message_thread_id=10)
    )
    bot.send_message = AsyncMock(
        return_value=SimpleNamespace(chat=SimpleNamespace(id=GROUP), message_id=900)
    )
    svc = make_service(bot)
    svc.sent_messages = SimpleNamespace(record=AsyncMock())
    ev = AgentResponseEvent(
        text="up", chat_id=OWNER, source="server-monitor:vps", originating_event_id="e"
    )
    await svc._deliver(OWNER, ev)
    svc.sent_messages.record.assert_awaited_once()
    kw = svc.sent_messages.record.call_args.kwargs
    assert kw["kind"] == "report" and kw["telegram_chat_id"] == GROUP


# --- media in bound topics (voice/photo/document) ------------------------------


def _gate_media_msg(thread, uid=OWNER, **media):
    m = SimpleNamespace(
        chat_id=GROUP,
        text=None,
        message_thread_id=thread,
        is_topic_message=thread is not None,
        voice=None,
        photo=None,
        document=None,
    )
    for k, v in media.items():
        setattr(m, k, v)
    return SimpleNamespace(effective_message=m, effective_user=SimpleNamespace(id=uid))


@pytest.mark.parametrize("kind", ["voice", "photo", "document"])
async def test_gate_media_bound_topic_owner_passes(kind) -> None:
    upd = _gate_media_msg(21, **{kind: object() if kind != "photo" else [object()]})
    assert await _gate(_router())(upd, MagicMock()) is None


@pytest.mark.parametrize("kind", ["voice", "photo", "document"])
async def test_gate_media_unbound_or_non_owner_ignored(kind) -> None:
    media = {kind: object() if kind != "photo" else [object()]}
    with pytest.raises(ApplicationHandlerStop):
        await _gate(_router())(_gate_media_msg(99, **media), MagicMock())
    with pytest.raises(ApplicationHandlerStop):
        await _gate(_router(), role="guest")(
            _gate_media_msg(21, uid=5, **media), MagicMock()
        )


async def test_voice_in_bound_topic_reroutes_one_turn(tmp_path, monkeypatch):
    monkeypatch.delenv("APPROVED_DIRECTORY", raising=False)
    dev, coach = tmp_path / "dev", tmp_path / "coach"
    dev.mkdir()
    coach.mkdir()
    registry = ProfileRegistry(
        [_profile("devops", dev, default=True), _profile("coach", coach)]
    )
    settings = Settings(
        telegram_bot_token="t:t",
        telegram_bot_username="b",
        approved_directory=None,
        enable_mcp=False,
        mcp_config_path=None,
        reports_chat_id=GROUP,
    )
    orch = MessageOrchestrator(settings, {})
    orch._run_turn = AsyncMock()  # type: ignore[method-assign]

    progress = AsyncMock()
    update = MagicMock()
    update.effective_user.id = OWNER
    update.message.chat_id = GROUP
    update.message.message_thread_id = 21
    update.message.is_topic_message = True
    update.message.caption = None
    update.message.chat.send_action = AsyncMock()
    update.message.reply_text = AsyncMock(return_value=progress)
    voice_handler = SimpleNamespace(
        process_voice_message=AsyncMock(
            return_value=SimpleNamespace(transcription="привет", prompt="привет")
        )
    )
    user_data = {"active_profile": "devops", "current_directory": dev}
    context = MagicMock()
    context.user_data = user_data
    context.bot_data = {
        "settings": settings,
        "profile_registry": registry,
        "reports_router": _router(),
        "features": SimpleNamespace(get_voice_handler=lambda: voice_handler),
        "auth_manager": SimpleNamespace(get_user_role=lambda uid: "owner"),
    }

    await orch.agentic_voice(update, context)
    # "Transcribing..." notice + typing go into the topic
    assert update.message.chat.send_action.call_args.kwargs["message_thread_id"] == 21
    assert update.message.reply_text.await_args.args == ("Transcribing...",)
    voice_handler.process_voice_message.assert_awaited_once()

    request = orch._run_turn.call_args.args[0]
    route = await request.resolve_route()
    assert route.working_dir == workspace_for(
        registry.get_by_slug("coach"), "owner", OWNER
    )
    assert route.restore_dir == dev
    assert user_data["active_profile"] == "devops"
    assert user_data["current_directory"] == dev

    # Unbound location: ordinary sticky route, nothing restored.
    update.message.chat_id = 5
    update.message.message_thread_id = None
    update.message.is_topic_message = False
    await orch.agentic_voice(update, context)
    route = await orch._run_turn.call_args.args[0].resolve_route()
    assert route.working_dir == workspace_for(
        registry.get_by_slug("devops"), "owner", OWNER
    )
    assert route.restore_dir is None
