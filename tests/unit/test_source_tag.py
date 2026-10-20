"""Source envelope for non-DM turns (dv-1dda)."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from src.bot.orchestration.reply_context import build_source_tag
from src.bot.orchestrator import MessageOrchestrator
from src.config.settings import Settings
from src.profiles.registry import ProfileDefinition, ProfileRegistry
from tests.unit.test_reports_inbound import GROUP, OWNER, _router


def _update(chat_type, title=None, thread=None):
    return SimpleNamespace(
        message=SimpleNamespace(
            chat=SimpleNamespace(type=chat_type, title=title),
            message_thread_id=thread,
            is_topic_message=thread is not None,
        )
    )


def test_tag_topic_dm_and_fallbacks() -> None:
    assert (
        build_source_tag(_update("supergroup", "Devi Ops", 22), "Markets")
        == '[via: group "Devi Ops" › topic "Markets"]'
    )
    assert build_source_tag(_update("private")) == ""
    assert build_source_tag(_update("supergroup", "Devi Ops", 9)).endswith(
        'topic "thread 9"]'
    )
    assert build_source_tag(_update("supergroup", "G")) == '[via: group "G"]'


async def test_text_turn_prompt_and_storage_carry_tag(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("APPROVED_DIRECTORY", raising=False)
    root = tmp_path / "dev"
    root.mkdir()
    prof = ProfileDefinition(
        "devops", "devops", root, root / "m", root / "p.md", default=True
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
    update.message.text = "что там"
    update.message.chat_id = GROUP
    update.message.chat.type = "supergroup"
    update.message.chat.title = "Devi Ops"
    update.message.message_thread_id = 22
    update.message.is_topic_message = True
    update.message.reply_to_message = None
    update.message.chat.send_action = AsyncMock()
    update.message.reply_text = AsyncMock(return_value=AsyncMock())
    context = MagicMock()
    context.user_data = {"active_profile": "devops", "current_directory": Path(root)}
    context.bot_data = {
        "settings": settings,
        "profile_registry": ProfileRegistry([prof]),
        "reports_router": _router(),
        "rate_limiter": None,
        "auth_manager": SimpleNamespace(get_user_role=lambda uid: "owner"),
    }

    await orch.agentic_text(update, context)
    request = orch._run_turn.call_args.args[0]
    route = await request.resolve_route()
    expected = '[via: group "Devi Ops" › topic "Monitoring"]\nчто там'
    assert route.prompt == expected
    assert route.storage_prompt == expected
    assert request.trigger_text == "что там"  # trigger never sees the tag

    # Reply envelope + source tag coexist: tag first, then replied-to, then text.
    update.message.reply_to_message = SimpleNamespace(
        text="old",
        caption=None,
        from_user=SimpleNamespace(id=5, first_name="Bob"),
        chat=SimpleNamespace(id=GROUP, type="supergroup"),
        message_id=3,
        forward_origin=None,
    )
    await orch.agentic_text(update, context)
    route = await orch._run_turn.call_args.args[0].resolve_route()
    assert route.prompt.startswith(
        '[via: group "Devi Ops" › topic "Monitoring"]\n[replied-to'
    )
    assert route.prompt.endswith("\n\nчто там")

    # DM: untouched.
    update.message.reply_to_message = None
    update.message.chat.type = "private"
    update.message.is_topic_message = False
    update.message.message_thread_id = None
    update.message.chat_id = 5
    await orch.agentic_text(update, context)
    route = await orch._run_turn.call_args.args[0].resolve_route()
    assert route.prompt == "что там"
    assert route.storage_prompt is None


async def test_media_turn_carries_tag(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("APPROVED_DIRECTORY", raising=False)
    root = tmp_path / "dev"
    root.mkdir()
    prof = ProfileDefinition(
        "devops", "devops", root, root / "m", root / "p.md", default=True
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
    update.message.chat_id = GROUP
    update.message.chat.type = "supergroup"
    update.message.chat.title = "Devi Ops"
    update.message.message_thread_id = 22
    update.message.is_topic_message = True
    update.message.reply_to_message = None
    context = MagicMock()
    context.user_data = {"active_profile": "devops", "current_directory": Path(root)}
    context.bot_data = {
        "settings": settings,
        "profile_registry": ProfileRegistry([prof]),
        "reports_router": _router(),
        "auth_manager": SimpleNamespace(get_user_role=lambda uid: "owner"),
    }
    await orch._run_agentic_media_turn(
        update=update,
        context=context,
        prompt="голос",
        progress_msg=AsyncMock(),
        user_id=OWNER,
        chat=update.message.chat,
    )
    route = await orch._run_turn.call_args.args[0].resolve_route()
    assert route.prompt == '[via: group "Devi Ops" › topic "Monitoring"]\nголос'
    assert route.storage_prompt == route.prompt
