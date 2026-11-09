"""Monitoring's bound topic has its own profile and persisted conversation."""

import asyncio
import base64
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram.ext import ApplicationHandlerStop

from src.bot.orchestration.reply_context import build_reply_context
from src.bot.orchestrator import MessageOrchestrator
from src.claude.prompt_composer import PromptComposer
from src.claude.window_bundle import WindowBundleProvider
from src.config.settings import Settings
from src.events.types import AgentResponseEvent
from src.profiles import load_profile_registry
from src.profiles.registry import ProfileDefinition, ProfileRegistry, workspace_for
from src.storage.database import DatabaseManager
from src.storage.memory_repository import MemoryRepository
from src.storage.repositories import MessageRepository, SessionRepository
from src.storage.window_bundle_repository import WindowBundleRepository
from src.storage.window_repository import WindowRepository
from tests.unit.test_install_profiles import TEMPLATES, _write_config, install_profiles
from tests.unit.test_notifications.test_reports_routing import FakeRepo, make_service
from tests.unit.test_reports_inbound import BOT_ID, GROUP, OWNER, _gate, _msg, _router


def _profile(slug: str, root: Path, *, default: bool = False) -> ProfileDefinition:
    return ProfileDefinition(
        slug,
        slug,
        root,
        root / "memory",
        root / "persona.md",
        default=default,
        personal_memory=False,
    )


def _turn(tmp_path: Path):
    dev, monitoring = tmp_path / "devops", tmp_path / "monitoring"
    dev.mkdir(exist_ok=True)
    monitoring.mkdir(exist_ok=True)
    registry = ProfileRegistry(
        [_profile("devops", dev, default=True), _profile("monitoring", monitoring)]
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
    context = MagicMock()
    context.user_data = {"active_profile": "devops", "current_directory": dev}
    context.bot_data = {
        "settings": settings,
        "profile_registry": registry,
        "reports_router": _router(),
        "rate_limiter": None,
        "auth_manager": SimpleNamespace(get_user_role=lambda uid: "owner"),
    }
    return orch, context, registry


def _update(thread: int | None, text: str = "status"):
    update = MagicMock()
    update.effective_user.id = OWNER
    update.message.text = text
    update.message.chat_id = GROUP if thread else OWNER
    update.message.message_thread_id = thread
    update.message.is_topic_message = thread is not None
    update.message.reply_to_message = None
    update.message.caption = None
    update.message.chat.send_action = AsyncMock()
    update.message.reply_text = AsyncMock(return_value=AsyncMock())
    update.effective_message = update.message
    return update


@pytest.mark.skipif(  # the template is withheld from the public export
    not (TEMPLATES / "monitoring").is_dir(), reason="monitoring template not shipped"
)
async def test_monitoring_text_uses_shipped_owner_only_profile_without_switching_devops(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("DEVVY_ROOT", str(tmp_path))
    monkeypatch.delenv("APPROVED_DIRECTORY", raising=False)
    assert "monitoring" in install_profiles.available_templates(TEMPLATES)
    cfg = _write_config(
        tmp_path, install_profiles.build_config(["devops", "monitoring"], TEMPLATES)
    )
    registry = load_profile_registry(cfg)
    profile = registry.get_by_slug("monitoring")
    assert profile.allowed_roles == frozenset({"owner"})
    assert profile.working_dir != registry.get_by_slug("devops").working_dir
    assert profile.memory_dir != registry.get_by_slug("devops").memory_dir
    assert profile.persona != registry.get_by_slug("devops").persona
    assert profile.sandbox is False and profile.unrestricted is True
    orch, context, _ = _turn(tmp_path)
    context.bot_data["profile_registry"] = registry
    orch._run_turn = AsyncMock()
    update = _update(22)
    await orch.agentic_text(update, context)
    route = await orch._run_turn.call_args.args[0].resolve_route()
    assert route.working_dir == workspace_for(profile, "owner", OWNER)
    assert context.user_data["active_profile"] == "devops"
    assert context.user_data["current_directory"] == tmp_path / "devops"
    assert update.message.chat.send_action.call_args.kwargs["message_thread_id"] == 22
    with pytest.raises(ApplicationHandlerStop):
        await _gate(_router(), role="guest")(_msg(22, uid=5), MagicMock())


async def test_devops_and_monitoring_turns_overlap_but_monitoring_serializes(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("APPROVED_DIRECTORY", raising=False)
    orch, context, _ = _turn(tmp_path)
    started = []
    release = asyncio.Event()

    async def run(request, *args):
        route = await request.resolve_route()
        started.append(route.working_dir)
        await release.wait()

    orch._run_turn = run
    first = asyncio.create_task(orch.agentic_text(_update(22, "one"), context))
    devops = None
    second = None
    try:
        for _ in range(100):
            if len(started) == 1:
                break
            await asyncio.sleep(0.01)
        assert started == [tmp_path / "monitoring"]
        devops = asyncio.create_task(orch.agentic_text(_update(None, "dev"), context))
        second = asyncio.create_task(orch.agentic_text(_update(22, "two"), context))
        await asyncio.sleep(0.05)
        assert started.count(tmp_path / "monitoring") == 1
        assert tmp_path / "devops" in started
        assert not devops.done() and not second.done()
    finally:
        release.set()
        await asyncio.gather(*(task for task in (first, devops, second) if task))
    assert started.count(tmp_path / "monitoring") == 2


@pytest.mark.parametrize("kind", ["voice", "photo", "document"])
async def test_monitoring_media_routes_and_replies_in_topic(
    kind, tmp_path, monkeypatch
):
    monkeypatch.delenv("APPROVED_DIRECTORY", raising=False)
    orch, context, registry = _turn(tmp_path)
    routes = []

    async def run(request, *args):
        routes.append(await request.resolve_route())

    orch._run_turn = run
    update = _update(22)
    if kind == "voice":
        update.message.voice = object()
        context.bot_data["features"] = SimpleNamespace(
            get_voice_handler=lambda: SimpleNamespace(
                process_voice_message=AsyncMock(
                    return_value=SimpleNamespace(transcription="check", prompt="check")
                )
            )
        )
    elif kind == "photo":
        update.message.photo = [object()]
        image = SimpleNamespace(
            base64_data=base64.b64encode(b"image").decode(),
            metadata={"format": "png"},
        )
        context.bot_data["features"] = SimpleNamespace(
            get_image_handler=lambda: SimpleNamespace(
                process_image=AsyncMock(return_value=image),
                create_photo_prompt=lambda caption: "inspect photo",
            )
        )
    else:
        update.message.document = SimpleNamespace(
            file_name="report.txt",
            file_size=6,
            get_file=AsyncMock(
                return_value=SimpleNamespace(
                    download_as_bytearray=AsyncMock(return_value=bytearray(b"report"))
                )
            ),
        )
    await getattr(orch, f"agentic_{kind}")(update, context)
    assert len(routes) == 1
    assert routes[0].working_dir == workspace_for(
        registry.get_by_slug("monitoring"), "owner", OWNER
    )
    assert context.user_data["active_profile"] == "devops"
    assert update.message.chat.send_action.call_args.kwargs["message_thread_id"] == 22
    assert update.message.reply_text.await_count >= 1


async def test_monitoring_sessions_windows_bundles_and_raw_recall_are_separate(
    tmp_path,
):
    db = DatabaseManager(str(tmp_path / "isolation.db"))
    await db.initialize()
    try:
        dev, mon = str(tmp_path / "devops"), str(tmp_path / "monitoring")
        windows = WindowRepository(db)
        dev_window = await windows.get_or_create(OWNER, dev)
        mon_window = await windows.get_or_create(OWNER, mon)
        assert dev_window.window_id == mon_window.window_id == 1
        assert dev_window.working_dir != mon_window.working_dir
        async with db.get_connection() as conn:
            await conn.execute("INSERT INTO users (user_id) VALUES (?)", (OWNER,))
            for sid, path, prompt in (
                ("dev-session", dev, "deploy alpha"),
                ("mon-session", mon, "monitor alpha"),
            ):
                await conn.execute(
                    "INSERT INTO sessions (session_id,user_id,project_path) VALUES (?,?,?)",
                    (sid, OWNER, path),
                )
                await conn.execute(
                    "INSERT INTO messages (session_id,user_id,prompt,response,window_id) "
                    "VALUES (?,?,?,?,?)",
                    (sid, OWNER, prompt, "done", 1),
                )
            await conn.commit()
        sessions = SessionRepository(db)
        assert [s.session_id for s in await sessions.get_sessions_by_project(dev)] == [
            "dev-session"
        ]
        assert [s.session_id for s in await sessions.get_sessions_by_project(mon)] == [
            "mon-session"
        ]
        messages = MessageRepository(db)
        for path, own, other in (
            (dev, "deploy", "monitor"),
            (mon, "monitor", "deploy"),
        ):
            tail = await messages.get_window_messages(OWNER, path, 1, 0)
            assert len(tail) == 1 and own in tail[0][0]
            rolled_tail = await windows.tail_messages(OWNER, path, 1, 10)
            assert len(rolled_tail) == 1 and own in rolled_tail[0][0]
            cold_start = await messages.get_recent_pairs_for_dir(
                OWNER, path, "1970-01-01", 10
            )
            assert len(cold_start) == 1 and own in cold_start[0][0]
            hits = await messages.search_fts(OWNER, path, "alpha", 10)
            assert len(hits) == 1 and own in hits[0].prompt_snippet
            assert other not in hits[0].prompt_snippet
        persona = tmp_path / "persona.md"
        persona.write_text("Monitoring isolated persona")
        provider = WindowBundleProvider(
            windows,
            WindowBundleRepository(db),
            MemoryRepository(db),
            default_composer=PromptComposer(
                memory_path=tmp_path, agent_md_path=persona
            ),
        )
        first = await provider.get_or_materialize(Path(dev), OWNER)
        second = await provider.get_or_materialize(Path(mon), OWNER)
        assert first is not None and second is not None
        assert first.window_id == second.window_id == 1
        bundles = WindowBundleRepository(db)
        assert (await bundles.get(OWNER, dev, 1)).working_dir == dev
        assert (await bundles.get(OWNER, mon, 1)).working_dir == mon
        assert await bundles.get(OWNER, dev, 2) is None
    finally:
        await db.close()


async def test_server_report_arrives_in_monitoring_and_reply_has_report_context():
    bot = AsyncMock()
    bot.create_forum_topic = AsyncMock(
        return_value=SimpleNamespace(message_thread_id=22)
    )
    bot.send_message = AsyncMock(
        return_value=SimpleNamespace(chat=SimpleNamespace(id=GROUP), message_id=700)
    )
    repo = FakeRepo()
    service = make_service(bot, repo)
    service.reports_router = _router()
    service.sent_messages = SimpleNamespace(record=AsyncMock())
    await service._deliver(
        OWNER,
        AgentResponseEvent(
            text="CPU high",
            chat_id=OWNER,
            source="server-monitor:vps",
            originating_event_id="e",
        ),
    )
    assert bot.send_message.call_args.kwargs["message_thread_id"] == 22
    quoted = SimpleNamespace(
        text="CPU high",
        caption=None,
        from_user=SimpleNamespace(id=BOT_ID, first_name="bot"),
        chat=SimpleNamespace(id=GROUP, type="supergroup"),
        message_id=700,
        forward_origin=None,
    )
    reply = SimpleNamespace(message=SimpleNamespace(reply_to_message=quoted))
    ledger = SimpleNamespace(
        lookup=AsyncMock(
            return_value=SimpleNamespace(
                kind="report",
                messages_message_id=None,
                profile_slug=None,
                created_at=None,
            )
        )
    )
    prompt, _ = await build_reply_context(
        reply,
        BOT_ID,
        ledger,
        "why?",
        viewer_user_id=OWNER,
        viewer_is_owner=True,
        reports_chat_id=GROUP,
    )
    assert "CPU high" in prompt and "why?" in prompt
    assert await service.reports_router.profile_for_thread(GROUP, 22) == "monitoring"
