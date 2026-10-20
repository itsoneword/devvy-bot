"""Tests for the MessageOrchestrator."""

import asyncio
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.bot.orchestration.formatting import _redact_secrets
from src.bot.orchestrator import (
    COMMAND_TABLE,
    OWNER_ONLY_COMMANDS,
    MessageOrchestrator,
)
from src.config import create_test_config


@pytest.fixture
def tmp_dir():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


@pytest.fixture
def agentic_settings(tmp_dir):
    return create_test_config(approved_directory=str(tmp_dir), agentic_mode=True)


@pytest.fixture
def deps():
    return {
        "claude_integration": MagicMock(),
        "storage": MagicMock(),
        "security_validator": MagicMock(),
        "rate_limiter": MagicMock(),
        "audit_logger": MagicMock(),
    }


def _registered_command_names(orchestrator: MessageOrchestrator) -> list[str]:
    """Register handlers on a mock app and return all command names."""
    from telegram.ext import CommandHandler

    app = MagicMock()
    app.add_handler = MagicMock()
    orchestrator.register_handlers(app)

    names: list[str] = []
    for call in app.add_handler.call_args_list:
        handler = call[0][0]
        if isinstance(handler, CommandHandler):
            names.extend(handler.commands)
    return names


# Commands that must always be REGISTERED. Since dv-a1c8 most of them are not
# in the Telegram menu — /help is what makes them discoverable — so this list
# is about "still works", not "still listed". Additions do not need to touch
# it; only removals should fail.
AGENTIC_REQUIRED_COMMANDS = {
    "start",
    "new",
    "help",
    "status",
    "verbose",
    "memory",
    "repo",
    "model",
    "restart",
    "servercheck",
    "tasks",
    "schedules",
    "stop",
    "resume",
}


async def test_agentic_registers_commands(agentic_settings, deps):
    """Every required command is registered, exactly once."""
    orchestrator = MessageOrchestrator(agentic_settings, deps)
    registered = _registered_command_names(orchestrator)

    # No duplicate registrations
    assert len(registered) == len(set(registered))
    assert AGENTIC_REQUIRED_COMMANDS <= set(registered)
    # The menu is a subset of what is registered — never the other way round.
    advertised = {c.command for c in await orchestrator.get_bot_commands()}
    assert advertised <= set(registered)


def test_command_table_and_handler_map_agree(agentic_settings, deps):
    """A spec without a handler (or the reverse) must fail here, not at runtime."""
    orchestrator = MessageOrchestrator(agentic_settings, deps)
    assert {spec.name for spec in COMMAND_TABLE} == set(
        orchestrator._command_handlers()
    )


async def test_owner_menu_is_the_settled_short_list_in_order(tmp_dir, deps):
    """D5: exact owner menu, exact order — setMyCommands preserves send order."""
    settings = create_test_config(approved_directory=str(tmp_dir))
    orchestrator = MessageOrchestrator(settings, {**deps, "profile_registry": None})
    names = [c.command for c in await orchestrator.get_bot_commands()]

    # /agents needs a profile registry; without one the rest must still hold.
    assert names == [
        "start",
        "new",
        "stop",
        "resume",
        "restart",
        "auth",
        "help",
        "status",
        "usage",
        "limits",
        "model",
        "tasks",
    ]


async def test_guest_menu_is_the_settled_short_list(tmp_dir, deps):
    """D5: guests see six entries and no owner-only command."""
    settings = create_test_config(approved_directory=str(tmp_dir))
    orchestrator = MessageOrchestrator(settings, {**deps, "profile_registry": None})
    names = [c.command for c in await orchestrator.get_bot_commands(role="guest")]

    assert names == ["start", "new", "help", "usage", "family"]
    assert not OWNER_ONLY_COMMANDS & set(names)


def test_callback_pattern_covers_every_prefix(agentic_settings, deps):
    """A dispatch branch whose prefix is not in a registered pattern is dead.

    This is a real bug that shipped: /auth's buttons were wired into
    _agentic_callback, but the CallbackQueryHandler pattern was still
    ^(cd:|model:|agent:), so pressing them did precisely nothing — no error,
    no log line, no reply. Registration tests passed because the COMMAND was
    registered; only the callback route was missing.
    """
    from telegram.ext import CallbackQueryHandler

    orchestrator = MessageOrchestrator(agentic_settings, deps)
    app = MagicMock()
    app.add_handler = MagicMock()
    orchestrator.register_handlers(app)

    patterns = [
        call[0][0].pattern
        for call in app.add_handler.call_args_list
        if isinstance(call[0][0], CallbackQueryHandler)
    ]

    # One real callback_data per prefix the bot actually emits.
    samples = [
        "cd:project_a",
        "model:claude",
        "model:codex_login",
        "agent:coach",
        "agents:manage",
        "agents:toggle:pm",
        "agents:new",
        "auth:refresh",
        "auth:login:codex",
        "auth:login:claude",
        "vset:whisper",
        "vset:off",
        "confirm:yes",
        "watchdog:hold",
        "qi:ab12cd34",
    ]
    for data in samples:
        assert any(p.match(data) for p in patterns), f"no handler routes {data!r}"


async def test_unlisted_commands_are_still_registered(agentic_settings, deps):
    """The whole premise of the menu cut: unlisted != removed."""
    orchestrator = MessageOrchestrator(agentic_settings, deps)
    registered = set(_registered_command_names(orchestrator))
    listed = {c.command for c in await orchestrator.get_bot_commands()}

    unlisted = {"verbose", "memory", "repo", "servercheck", "schedules"}
    assert unlisted <= registered
    assert not unlisted & listed


async def test_register_handlers_always_registers_the_agentic_set(tmp_dir, deps):
    """There is only one handler set now — no mode switch can select another."""
    settings = create_test_config(approved_directory=str(tmp_dir))
    orchestrator = MessageOrchestrator(settings, deps)
    registered = set(_registered_command_names(orchestrator))

    assert {"start", "new", "restart", "stop", "resume"} <= registered
    # Classic-only commands must not come back.
    assert not registered & {"cd", "ls", "pwd", "projects", "export", "actions", "git"}


def test_agentic_registers_text_document_photo_handlers(agentic_settings, deps):
    """Agentic mode registers text, document, photo, and voice message handlers."""
    orchestrator = MessageOrchestrator(agentic_settings, deps)
    app = MagicMock()
    app.add_handler = MagicMock()

    orchestrator.register_handlers(app)

    from telegram.ext import CallbackQueryHandler, MessageHandler

    msg_handlers = [
        call
        for call in app.add_handler.call_args_list
        if isinstance(call[0][0], MessageHandler)
    ]
    cb_handlers = [
        call
        for call in app.add_handler.call_args_list
        if isinstance(call[0][0], CallbackQueryHandler)
    ]

    # 4 message handlers (text, document, photo, voice)
    assert len(msg_handlers) == 4
    # 3 callback handlers (cd:/model:/agent: selection + confirm: gate +
    # watchdog: hold/dismiss gate)
    assert len(cb_handlers) == 3


def test_agentic_message_handlers_reject_edited_messages(agentic_settings, deps):
    """dv-b7e2: edited_message updates must never reach the group=10 handlers.

    Handlers read update.message directly, which is None for edits (PTB falls
    back to update.edited_message for filter matching but not for the handler
    itself) -> AttributeError. Regression-tested at the filter level with a
    real telegram.Update, not a MagicMock, since MagicMock would satisfy the
    filters regardless of whether EDITED_MESSAGE is actually excluded.
    """
    import datetime

    from telegram import Chat, Document, Message, PhotoSize, Update, User, Voice
    from telegram.ext import MessageHandler

    orchestrator = MessageOrchestrator(agentic_settings, deps)
    app = MagicMock()
    app.add_handler = MagicMock()
    orchestrator.register_handlers(app)

    msg_handlers = [
        call[0][0]
        for call in app.add_handler.call_args_list
        if isinstance(call[0][0], MessageHandler)
    ]
    assert len(msg_handlers) == 4

    chat = Chat(id=1, type="private")
    user = User(id=1, first_name="u", is_bot=False)
    now = datetime.datetime.now(datetime.timezone.utc)

    edited_text = Update(
        update_id=1,
        edited_message=Message(
            message_id=1, date=now, chat=chat, from_user=user, text="edited text"
        ),
    )
    edited_document = Update(
        update_id=2,
        edited_message=Message(
            message_id=2,
            date=now,
            chat=chat,
            from_user=user,
            document=Document(file_id="f", file_unique_id="fu", file_name="a.txt"),
        ),
    )
    edited_photo = Update(
        update_id=3,
        edited_message=Message(
            message_id=3,
            date=now,
            chat=chat,
            from_user=user,
            photo=(PhotoSize(file_id="p", file_unique_id="pu", width=1, height=1),),
            caption="edited caption",
        ),
    )
    edited_voice = Update(
        update_id=4,
        edited_message=Message(
            message_id=4,
            date=now,
            chat=chat,
            from_user=user,
            voice=Voice(file_id="v", file_unique_id="vu", duration=1),
        ),
    )

    for handler, edited_update in zip(
        msg_handlers, (edited_text, edited_document, edited_photo, edited_voice)
    ):
        assert edited_update.message is None
        assert not handler.check_update(edited_update)

    # Sanity: the same filters still accept the equivalent non-edited update.
    fresh_text = Update(
        update_id=5,
        message=Message(
            message_id=5, date=now, chat=chat, from_user=user, text="hello"
        ),
    )
    assert msg_handlers[0].check_update(fresh_text)


async def test_agentic_bot_commands(agentic_settings, deps):
    """Menu is well-formed: unique, described, and short (dv-a1c8)."""
    orchestrator = MessageOrchestrator(agentic_settings, deps)
    commands = await orchestrator.get_bot_commands()

    cmd_names = [c.command for c in commands]
    assert len(cmd_names) == len(set(cmd_names))
    assert all(c.description.strip() for c in commands)
    assert len(cmd_names) <= 12
    assert {"start", "new", "help", "restart", "stop", "resume"} <= set(cmd_names)


def test_selected_codex_hard_model_is_supported(agentic_settings, deps):
    orchestrator = MessageOrchestrator(agentic_settings, deps)
    context = MagicMock()
    context.user_data = {
        "agent_backend": "openai_codex",
        "agent_model": "openai-codex/gpt-5.5",
    }

    assert orchestrator._selected_model(context) == "openai-codex/gpt-5.5"


def test_selected_codex_model_falls_back_from_unsupported(agentic_settings, deps):
    orchestrator = MessageOrchestrator(agentic_settings, deps)
    context = MagicMock()
    context.user_data = {
        "agent_backend": "openai_codex",
        "agent_model": "openai-codex/gpt-5.4-pro",
    }

    assert orchestrator._selected_model(context) == "openai-codex/gpt-5.6-sol"
    assert context.user_data["agent_model"] == "openai-codex/gpt-5.6-sol"


async def test_restart_command_sends_sigterm(deps):
    """restart_command sends SIGTERM to the current process."""
    from unittest.mock import patch

    from src.bot.orchestration.commands.system import restart_command

    update = MagicMock()
    update.effective_user.id = 123
    update.message.reply_text = AsyncMock()

    context = MagicMock()
    context.bot_data = {"audit_logger": None}

    with patch("src.bot.orchestration.commands.system.os.kill") as mock_kill:
        await restart_command(update, context)

    import os
    import signal

    mock_kill.assert_called_once_with(os.getpid(), signal.SIGTERM)
    # Verify confirmation message was sent
    update.message.reply_text.assert_called_once()
    msg = update.message.reply_text.call_args[0][0]
    assert "Restarting" in msg


async def test_agentic_start_no_keyboard(agentic_settings, deps):
    """Agentic /start sends brief message without inline keyboard."""
    orchestrator = MessageOrchestrator(agentic_settings, deps)

    update = MagicMock()
    update.effective_user.first_name = "Alice"
    update.message.reply_text = AsyncMock()

    context = MagicMock()
    context.user_data = {}
    context.bot_data = {"settings": agentic_settings}
    for k, v in deps.items():
        context.bot_data[k] = v

    await orchestrator.agentic_start(update, context)

    update.message.reply_text.assert_called_once()
    call_kwargs = update.message.reply_text.call_args
    # No reply_markup argument (no keyboard)
    assert (
        "reply_markup" not in call_kwargs.kwargs
        or call_kwargs.kwargs.get("reply_markup") is None
    )
    # Contains user name
    assert "Alice" in call_kwargs.args[0]


async def test_agentic_new_resets_session(agentic_settings, deps):
    """Agentic /new clears session and sends brief confirmation."""
    orchestrator = MessageOrchestrator(agentic_settings, deps)

    update = MagicMock()
    update.message.reply_text = AsyncMock()

    context = MagicMock()
    context.user_data = {
        "claude_session_id": "old-session-123",
        "codex_session_id": "old-codex-session-123",
    }
    context.bot_data = {}  # no window_repo wired in this unit test

    await orchestrator.agentic_new(update, context)

    assert context.user_data["claude_session_id"] is None
    assert context.user_data["codex_session_id"] is None
    update.message.reply_text.assert_called_once_with("Session reset. What's next?")


async def test_agentic_new_rolls_routed_profile_window(
    agentic_settings, deps, tmp_path
):
    """dv-5f3a regression: /new must roll the window of the workspace the
    user is TALKING in (profile-routed, like text turns), not the legacy
    current_directory/home fallback. Live failure 2026-08-15: /new in the
    coach chat rolled /home/testuser's window; the coach bundle never
    refreshed."""
    from src.profiles.registry import ProfileDefinition, ProfileRegistry

    coach_root = tmp_path / "coach"
    coach_root.mkdir()
    registry = ProfileRegistry(
        [
            ProfileDefinition(
                slug="coach",
                name="Coach",
                working_dir=coach_root,
                memory_dir=coach_root / "memory",
                persona=coach_root / "persona.md",
                default=True,
                personal_memory=True,
            )
        ]
    )
    window_repo = MagicMock()
    state = MagicMock()
    state.last_checkpoint_message_id = 0
    window_repo.get_or_create = AsyncMock(return_value=state)
    window_repo.roll_window = AsyncMock()
    window_repo.set_pending_transition = AsyncMock()

    orchestrator = MessageOrchestrator(agentic_settings, deps)
    update = MagicMock()
    update.effective_user.id = 111111111
    update.message.reply_text = AsyncMock()
    context = MagicMock()
    context.user_data = {"active_profile": "coach", "role": "owner"}
    context.bot_data = {
        "window_repo": window_repo,
        "profile_registry": registry,
        "storage": None,
        "evaluator_service": None,
    }

    await orchestrator.agentic_new(update, context)

    rolled_dir = window_repo.roll_window.call_args[0][1]
    assert str(rolled_dir) == str(coach_root / "users" / "111111111")
    armed_dir = window_repo.set_pending_transition.call_args[0][1]
    assert str(armed_dir) == str(rolled_dir)


async def test_agentic_status_compact(agentic_settings, deps):
    """Agentic /status returns compact one-line status."""
    orchestrator = MessageOrchestrator(agentic_settings, deps)

    update = MagicMock()
    update.effective_user.id = 123
    update.message.reply_text = AsyncMock()

    context = MagicMock()
    context.user_data = {}
    context.bot_data = {"rate_limiter": None}

    await orchestrator.agentic_status(update, context)

    call_args = update.message.reply_text.call_args
    text = call_args.args[0]
    assert "Session: none" in text


async def test_agentic_usage_reports_costs_and_caps(agentic_settings, deps):
    """/usage shows today's spend, 30-day total, budget window, and caps."""
    from datetime import UTC, datetime

    orchestrator = MessageOrchestrator(agentic_settings, deps)

    today = datetime.now(UTC).strftime("%Y-%m-%d")
    storage = MagicMock()
    storage.costs.get_user_daily_costs = AsyncMock(
        return_value=[
            SimpleNamespace(date=today, daily_cost=1.25, request_count=4),
            SimpleNamespace(date="2026-07-01", daily_cost=2.00, request_count=10),
        ]
    )
    storage.log_bot_event = AsyncMock()

    rate_limiter = MagicMock()
    rate_limiter.get_user_status.return_value = {
        "cost_usage": {"current": 1.25, "limit": 10.0, "remaining": 8.75},
        "token_usage": {"current": 0, "limit": 2_500_000, "remaining": 2_500_000},
    }
    # /usage asks the limiter what it will actually enforce; None = uncapped.
    rate_limiter.effective_daily_cap.return_value = None
    rate_limiter.effective_daily_token_cap.return_value = None

    update = MagicMock()
    update.effective_user.id = 123
    update.message.reply_text = AsyncMock()

    context = MagicMock()
    # Role must be stamped explicitly: since dv-09d8 an unresolvable role
    # fails closed to guest instead of promoting to owner.
    context.user_data = {"role": "owner"}
    context.bot_data = {
        "storage": storage,
        "rate_limiter": rate_limiter,
        "auth_manager": None,
    }

    await orchestrator.agentic_usage(update, context)

    text = update.message.reply_text.call_args.args[0]
    assert "Today: $1.25 (4 requests)" in text
    assert "Last 30 days: $3.25 (14 requests)" in text
    # Owners are exempt from both caps, so /usage shows "no cap" rather than
    # a number that would misleadingly imply enforcement.
    assert "Budget window (24h): $1.25 (no cap)" in text
    assert "Per-request cap: none" in text
    # No unmetered tokens spent and no cap: the token line stays out of the way.
    assert "Unmetered tokens" not in text


async def test_agentic_usage_shows_caps_for_guest_role(agentic_settings, deps):
    """Guests still see (and are subject to) the numeric caps."""
    from datetime import UTC, datetime

    orchestrator = MessageOrchestrator(agentic_settings, deps)

    today = datetime.now(UTC).strftime("%Y-%m-%d")
    storage = MagicMock()
    storage.costs.get_user_daily_costs = AsyncMock(
        return_value=[SimpleNamespace(date=today, daily_cost=1.25, request_count=4)]
    )
    storage.log_bot_event = AsyncMock()

    rate_limiter = MagicMock()
    rate_limiter.get_user_status.return_value = {
        "cost_usage": {"current": 1.25, "limit": 10.0, "remaining": 8.75},
        "token_usage": {"current": 0, "limit": 500_000, "remaining": 500_000},
    }
    rate_limiter.effective_daily_cap.return_value = 10.0
    rate_limiter.effective_daily_token_cap.return_value = 500_000

    update = MagicMock()
    update.effective_user.id = 456
    update.message.reply_text = AsyncMock()

    context = MagicMock()
    context.user_data = {"role": "guest"}
    context.bot_data = {
        "storage": storage,
        "rate_limiter": rate_limiter,
        "auth_manager": None,
    }

    await orchestrator.agentic_usage(update, context)

    text = update.message.reply_text.call_args.args[0]
    assert "$1.25 / $10.00" in text
    # A capped guest sees the token allowance even before spending any.
    assert "Unmetered tokens (24h): 0 / 500,000 — 500,000 left" in text
    assert "Per-request cap: $" in text


async def test_agentic_usage_does_not_show_a_codex_day_as_free(agentic_settings, deps):
    """dv-0722: a day spent on an unpriced backend reads as tokens, not $0."""
    from datetime import UTC, datetime

    orchestrator = MessageOrchestrator(agentic_settings, deps)

    today = datetime.now(UTC).strftime("%Y-%m-%d")
    storage = MagicMock()
    # The SQLite dollar ledger is honestly $0.00 -- nothing was billed.
    storage.costs.get_user_daily_costs = AsyncMock(
        return_value=[SimpleNamespace(date=today, daily_cost=0.0, request_count=9)]
    )
    storage.log_bot_event = AsyncMock()

    rate_limiter = MagicMock()
    rate_limiter.get_user_status.return_value = {
        "cost_usage": {"current": 0.0, "limit": 10.0, "remaining": 10.0},
        "token_usage": {"current": 480_000, "limit": 2_500_000},
    }
    rate_limiter.effective_daily_cap.return_value = None
    rate_limiter.effective_daily_token_cap.return_value = None

    update = MagicMock()
    update.effective_user.id = 123
    update.message.reply_text = AsyncMock()

    context = MagicMock()
    context.user_data = {"role": "owner"}
    context.bot_data = {
        "storage": storage,
        "rate_limiter": rate_limiter,
        "auth_manager": None,
    }

    await orchestrator.agentic_usage(update, context)

    text = update.message.reply_text.call_args.args[0]
    assert "Today: $0.00 (9 requests)" in text
    assert "Budget window (24h): $0.00 (no cap)" in text
    assert "Unmetered tokens (24h): 480,000 (no cap)" in text
    rate_limiter.effective_daily_token_cap.assert_called_once_with(123, "owner")


async def test_agentic_usage_survives_missing_deps(agentic_settings, deps):
    """/usage still answers when storage and rate limiter are absent."""
    orchestrator = MessageOrchestrator(agentic_settings, deps)

    update = MagicMock()
    update.effective_user.id = 123
    update.message.reply_text = AsyncMock()

    context = MagicMock()
    context.user_data = {}
    context.bot_data = {"auth_manager": None}

    await orchestrator.agentic_usage(update, context)

    text = update.message.reply_text.call_args.args[0]
    assert "Per-request cap" in text


class TestPerUserTurnLock:
    """Concurrent messages must serialize per user, not spawn parallel turns."""

    def _orchestrator_with_probe(self, agentic_settings, deps, active):
        """Orchestrator whose turn body records concurrent entries."""
        orchestrator = MessageOrchestrator(agentic_settings, deps)
        peak = {"max": 0}

        async def fake_turn(update, context, override_text=None):
            active["n"] += 1
            peak["max"] = max(peak["max"], active["n"])
            await asyncio.sleep(0.05)
            active["n"] -= 1

        orchestrator._agentic_text_turn = fake_turn
        return orchestrator, peak

    def _update_for(self, user_id):
        update = MagicMock()
        update.effective_user.id = user_id
        update.message.reply_text = AsyncMock(
            return_value=MagicMock(delete=AsyncMock())
        )
        return update

    async def test_same_user_runs_sequentially(self, agentic_settings, deps):
        active = {"n": 0}
        orchestrator, peak = self._orchestrator_with_probe(
            agentic_settings, deps, active
        )
        context = MagicMock()
        u1, u2 = self._update_for(123), self._update_for(123)
        await asyncio.gather(
            orchestrator.agentic_text(u1, context),
            orchestrator.agentic_text(u2, context),
        )
        assert peak["max"] == 1

    async def test_second_message_gets_queued_ack(self, agentic_settings, deps):
        active = {"n": 0}
        orchestrator, _ = self._orchestrator_with_probe(agentic_settings, deps, active)
        context = MagicMock()
        u1, u2 = self._update_for(123), self._update_for(123)
        t1 = asyncio.create_task(orchestrator.agentic_text(u1, context))
        await asyncio.sleep(0.01)  # let the first turn take the lock
        t2 = asyncio.create_task(orchestrator.agentic_text(u2, context))
        await asyncio.gather(t1, t2)
        u1.message.reply_text.assert_not_called()
        ack_text = u2.message.reply_text.call_args.args[0]
        assert "Queued" in ack_text

    async def test_different_users_run_in_parallel(self, agentic_settings, deps):
        active = {"n": 0}
        orchestrator, peak = self._orchestrator_with_probe(
            agentic_settings, deps, active
        )
        context = MagicMock()
        u1, u2 = self._update_for(123), self._update_for(456)
        await asyncio.gather(
            orchestrator.agentic_text(u1, context),
            orchestrator.agentic_text(u2, context),
        )
        assert peak["max"] == 2

    async def test_voice_claims_slot_at_arrival_before_transcription(
        self, agentic_settings, deps
    ):
        """Sent = locked: a voice message queues on the turn lock BEFORE
        transcription, so a text sent later cannot overtake it, and the
        media turn itself runs under the same lock as text turns."""
        orchestrator = MessageOrchestrator(agentic_settings, deps)
        orchestrator._run_agentic_media_turn = AsyncMock()

        voice_handler = MagicMock()
        voice_handler.process_voice_message = AsyncMock(
            return_value=MagicMock(prompt="transcribed text")
        )
        features = MagicMock()
        features.get_voice_handler.return_value = voice_handler

        update = self._update_for(123)
        update.message.chat.send_action = AsyncMock()
        update.message.reply_text = AsyncMock(
            return_value=MagicMock(edit_text=AsyncMock(), delete=AsyncMock())
        )
        context = MagicMock()
        context.bot_data = {"features": features}

        # Another turn is running: the voice must not even START transcribing.
        await orchestrator._turn_lock(123).acquire()
        task = asyncio.create_task(orchestrator.agentic_voice(update, context))
        await asyncio.sleep(0.05)
        voice_handler.process_voice_message.assert_not_called()
        orchestrator._run_agentic_media_turn.assert_not_called()

        orchestrator._turn_lock(123).release()
        await task
        voice_handler.process_voice_message.assert_awaited_once()
        orchestrator._run_agentic_media_turn.assert_awaited_once()
        # The user saw an immediate "queued" ack while waiting.
        ack_text = update.message.reply_text.call_args.args[0]
        assert "Queued" in ack_text

    async def test_callback_query_does_not_take_the_lock(self, agentic_settings, deps):
        """Confirmation buttons must stay clickable while a turn is running:
        _agentic_callback has no dependency on the per-user turn lock."""
        import inspect

        from src.bot.orchestrator import MessageOrchestrator as MO

        source = inspect.getsource(MO._agentic_callback)
        assert "_turn_lock" not in source


async def test_agentic_text_calls_claude(agentic_settings, deps):
    """Agentic text handler calls Claude and returns response without keyboard."""
    orchestrator = MessageOrchestrator(agentic_settings, deps)

    # Mock Claude response
    mock_response = MagicMock()
    mock_response.session_id = "session-abc"
    mock_response.content = "Hello, I can help with that!"
    mock_response.tools_used = []

    claude_integration = AsyncMock()
    claude_integration.run_command = AsyncMock(return_value=mock_response)

    update = MagicMock()
    update.effective_user.id = 123
    update.message.text = "Help me with this code"
    update.message.message_id = 1
    update.message.chat.send_action = AsyncMock()
    update.message.reply_text = AsyncMock()

    # Progress message mock
    progress_msg = AsyncMock()
    progress_msg.delete = AsyncMock()
    update.message.reply_text.return_value = progress_msg

    context = MagicMock()
    context.user_data = {}
    context.bot_data = {
        "settings": agentic_settings,
        "claude_integration": claude_integration,
        "storage": None,
        "rate_limiter": None,
        "audit_logger": None,
    }

    await orchestrator.agentic_text(update, context)

    # Claude was called
    claude_integration.run_command.assert_called_once()

    # Session ID updated
    assert context.user_data["claude_session_id"] == "session-abc"

    # Progress message deleted
    progress_msg.delete.assert_called_once()

    # Response sent without keyboard (reply_markup=None)
    response_calls = [
        c
        for c in update.message.reply_text.call_args_list
        if c != update.message.reply_text.call_args_list[0]
    ]
    for call in response_calls:
        assert call.kwargs.get("reply_markup") is None


async def test_agentic_callback_scoped_to_cd_pattern(agentic_settings, deps):
    """Agentic callback handler is registered with cd: pattern filter."""
    orchestrator = MessageOrchestrator(agentic_settings, deps)
    app = MagicMock()
    app.add_handler = MagicMock()

    orchestrator.register_handlers(app)

    from telegram.ext import CallbackQueryHandler

    cb_handlers = [
        call[0][0]
        for call in app.add_handler.call_args_list
        if isinstance(call[0][0], CallbackQueryHandler)
    ]

    # Every callback handler must be pattern-scoped (no catch-all handlers)
    assert cb_handlers
    assert all(h.pattern is not None for h in cb_handlers)
    # One handler must match cd: project-selection data
    assert any(h.pattern.match("cd:my_project") for h in cb_handlers)
    # One handler must match confirm: destructive-op gate data
    assert any(h.pattern.match("confirm:abc:yes") for h in cb_handlers)
    # Arbitrary callback data must not be handled
    assert not any(h.pattern.match("random:data") for h in cb_handlers)


async def test_agentic_document_rejects_large_files(agentic_settings, deps):
    """Agentic document handler rejects files over 20MB (Telegram's own cap)."""
    orchestrator = MessageOrchestrator(agentic_settings, deps)

    update = MagicMock()
    update.effective_user.id = 123
    update.message.document.file_name = "big.bin"
    update.message.document.file_size = 25 * 1024 * 1024  # 25MB
    update.message.reply_text = AsyncMock()

    context = MagicMock()
    context.bot_data = {"security_validator": None}

    await orchestrator.agentic_document(update, context)

    call_args = update.message.reply_text.call_args
    assert "too large" in call_args.args[0].lower()


async def test_agentic_voice_calls_claude(agentic_settings, deps):
    """Agentic voice handler transcribes and routes prompt to Claude."""
    orchestrator = MessageOrchestrator(agentic_settings, deps)

    mock_response = MagicMock()
    mock_response.session_id = "voice-session-123"
    mock_response.content = "Voice response from Claude"
    mock_response.tools_used = []

    claude_integration = AsyncMock()
    claude_integration.run_command = AsyncMock(return_value=mock_response)

    processed_voice = MagicMock()
    processed_voice.prompt = "Voice prompt text"

    voice_handler = MagicMock()
    voice_handler.process_voice_message = AsyncMock(return_value=processed_voice)

    features = MagicMock()
    features.get_voice_handler.return_value = voice_handler

    update = MagicMock()
    update.effective_user.id = 123
    update.message.voice = MagicMock()
    update.message.caption = "please summarize"
    update.message.message_id = 1
    update.message.chat.send_action = AsyncMock()
    update.message.reply_text = AsyncMock()

    progress_msg = AsyncMock()
    progress_msg.edit_text = AsyncMock()
    progress_msg.delete = AsyncMock()
    update.message.reply_text.return_value = progress_msg

    context = MagicMock()
    context.user_data = {}
    context.bot_data = {
        "settings": agentic_settings,
        "features": features,
        "claude_integration": claude_integration,
    }

    await orchestrator.agentic_voice(update, context)

    voice_handler.process_voice_message.assert_awaited_once_with(
        update.message.voice, "please summarize"
    )
    claude_integration.run_command.assert_awaited_once()
    assert context.user_data["claude_session_id"] == "voice-session-123"


async def test_agentic_voice_missing_handler_is_provider_aware(tmp_path, deps):
    """Missing voice handler guidance references the configured provider key."""
    settings = create_test_config(
        approved_directory=str(tmp_path),
        agentic_mode=True,
        voice_provider="openai",
    )
    orchestrator = MessageOrchestrator(settings, deps)

    features = MagicMock()
    features.get_voice_handler.return_value = None

    update = MagicMock()
    update.effective_user.id = 123
    update.message.reply_text = AsyncMock()

    context = MagicMock()
    context.bot_data = {"features": features}
    context.user_data = {}

    await orchestrator.agentic_voice(update, context)

    call_args = update.message.reply_text.call_args
    assert "OPENAI_API_KEY" in call_args.args[0]


async def test_agentic_voice_transcription_failure_surfaces_user_error(
    agentic_settings, deps
):
    """Transcription failures are shown to users and do not call Claude."""
    orchestrator = MessageOrchestrator(agentic_settings, deps)

    voice_handler = MagicMock()
    voice_handler.process_voice_message = AsyncMock(
        side_effect=RuntimeError("Mistral transcription request failed: boom")
    )

    features = MagicMock()
    features.get_voice_handler.return_value = voice_handler

    claude_integration = AsyncMock()
    claude_integration.run_command = AsyncMock()

    update = MagicMock()
    update.effective_user.id = 123
    update.message.voice = MagicMock()
    update.message.caption = None
    update.message.chat.send_action = AsyncMock()
    update.message.reply_text = AsyncMock()

    progress_msg = AsyncMock()
    progress_msg.edit_text = AsyncMock()
    update.message.reply_text.return_value = progress_msg

    context = MagicMock()
    context.user_data = {}
    context.bot_data = {
        "settings": agentic_settings,
        "features": features,
        "claude_integration": claude_integration,
    }

    await orchestrator.agentic_voice(update, context)

    progress_msg.edit_text.assert_awaited_once()
    error_text = progress_msg.edit_text.call_args.args[0]
    assert "Mistral transcription request failed" in error_text
    assert progress_msg.edit_text.call_args.kwargs["parse_mode"] == "HTML"
    claude_integration.run_command.assert_not_awaited()


async def test_agentic_start_escapes_html_in_name(agentic_settings, deps):
    """Names with HTML-special characters are escaped safely."""
    orchestrator = MessageOrchestrator(agentic_settings, deps)

    update = MagicMock()
    update.effective_user.first_name = "A<B>&C"
    update.message.reply_text = AsyncMock()

    context = MagicMock()
    context.user_data = {}

    await orchestrator.agentic_start(update, context)

    call_kwargs = update.message.reply_text.call_args
    text = call_kwargs.args[0]
    # HTML-special characters should be escaped
    assert "A&lt;B&gt;&amp;C" in text
    # parse_mode is HTML
    assert call_kwargs.kwargs.get("parse_mode") == "HTML"


async def test_agentic_text_logs_failure_on_error(agentic_settings, deps):
    """Failed Claude runs are logged with success=False."""
    orchestrator = MessageOrchestrator(agentic_settings, deps)

    claude_integration = AsyncMock()
    claude_integration.run_command = AsyncMock(side_effect=Exception("Claude broke"))

    audit_logger = AsyncMock()
    audit_logger.log_command = AsyncMock()

    update = MagicMock()
    update.effective_user.id = 123
    update.message.text = "do something"
    update.message.message_id = 1
    update.message.chat.send_action = AsyncMock()
    update.message.reply_text = AsyncMock()

    progress_msg = AsyncMock()
    progress_msg.delete = AsyncMock()
    update.message.reply_text.return_value = progress_msg

    context = MagicMock()
    context.user_data = {}
    context.bot_data = {
        "settings": agentic_settings,
        "claude_integration": claude_integration,
        "storage": None,
        "rate_limiter": None,
        "audit_logger": audit_logger,
    }

    await orchestrator.agentic_text(update, context)

    # Audit logged with success=False
    audit_logger.log_command.assert_called_once()
    call_kwargs = audit_logger.log_command.call_args
    assert call_kwargs.kwargs["success"] is False


# --- _redact_secrets / _summarize_tool_input tests ---


class TestRedactSecrets:
    """Ensure sensitive substrings are redacted from Bash command summaries."""

    def test_safe_command_unchanged(self):
        assert (
            _redact_secrets("poetry run pytest tests/ -v")
            == "poetry run pytest tests/ -v"
        )

    def test_anthropic_api_key_redacted(self):
        key = "sk-ant-api03-abc123def456ghi789jkl012mno345"
        cmd = f"ANTHROPIC_API_KEY={key}"
        result = _redact_secrets(cmd)
        assert key not in result
        assert "***" in result

    def test_sk_key_redacted(self):
        cmd = "curl -H 'Authorization: Bearer sk-1234567890abcdefghijklmnop'"
        result = _redact_secrets(cmd)
        assert "sk-1234567890abcdefghijklmnop" not in result
        assert "***" in result

    def test_github_pat_redacted(self):
        cmd = "git clone https://ghp_abcdefghijklmnop1234@github.com/user/repo"
        result = _redact_secrets(cmd)
        assert "ghp_abcdefghijklmnop1234" not in result
        assert "***" in result

    def test_aws_key_redacted(self):
        cmd = "AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE"
        result = _redact_secrets(cmd)
        assert "AKIAIOSFODNN7EXAMPLE" not in result
        assert "***" in result

    def test_flag_token_redacted(self):
        cmd = "mycli --token=supersecretvalue123"
        result = _redact_secrets(cmd)
        assert "supersecretvalue123" not in result
        assert "--token=" in result or "--token" in result

    def test_password_env_redacted(self):
        cmd = "PASSWORD=MyS3cretP@ss! ./run.sh"
        result = _redact_secrets(cmd)
        assert "MyS3cretP@ss!" not in result
        assert "***" in result

    def test_bearer_token_redacted(self):
        cmd = "curl -H 'Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig'"
        result = _redact_secrets(cmd)
        assert "eyJhbGciOiJIUzI1NiJ9.payload.sig" not in result

    def test_connection_string_redacted(self):
        cmd = "psql postgresql://admin:secret_password@db.host:5432/mydb"
        result = _redact_secrets(cmd)
        assert "secret_password" not in result

    def test_summarize_tool_input_bash_redacts(self, agentic_settings, deps):
        """_summarize_tool_input applies redaction to Bash commands."""
        orchestrator = MessageOrchestrator(agentic_settings, deps)
        result = orchestrator._summarize_tool_input(
            "Bash",
            {"command": "curl --token=mysupersecrettoken123 https://api.example.com"},
        )
        assert "mysupersecrettoken123" not in result
        assert "***" in result

    def test_summarize_tool_input_non_bash_unchanged(self, agentic_settings, deps):
        """Non-Bash tools don't go through redaction."""
        orchestrator = MessageOrchestrator(agentic_settings, deps)
        result = orchestrator._summarize_tool_input(
            "Read", {"file_path": "/home/user/.env"}
        )
        assert result == ".env"


# --- Typing heartbeat tests ---


class TestTypingHeartbeat:
    """Verify typing indicator stays alive independently of stream events."""

    async def test_heartbeat_sends_typing_action(self, agentic_settings, deps):
        """Heartbeat sends typing actions at the configured interval."""
        chat = AsyncMock()
        chat.send_action = AsyncMock()

        orchestrator = MessageOrchestrator(agentic_settings, deps)
        heartbeat = orchestrator._start_typing_heartbeat(chat, interval=0.05)

        # Let the heartbeat fire a few times
        await asyncio.sleep(0.2)
        heartbeat.cancel()
        try:
            await heartbeat
        except asyncio.CancelledError:
            pass

        # Should have been called multiple times
        assert chat.send_action.call_count >= 2
        chat.send_action.assert_called_with("typing")

    async def test_heartbeat_cancels_cleanly(self, agentic_settings, deps):
        """Cancelling the heartbeat task does not raise."""
        chat = AsyncMock()
        orchestrator = MessageOrchestrator(agentic_settings, deps)
        heartbeat = orchestrator._start_typing_heartbeat(chat, interval=0.05)

        heartbeat.cancel()
        # Should not raise
        try:
            await heartbeat
        except asyncio.CancelledError:
            pass

        assert heartbeat.cancelled() or heartbeat.done()

    async def test_heartbeat_survives_send_action_errors(self, agentic_settings, deps):
        """Heartbeat keeps running even if send_action raises."""
        chat = AsyncMock()
        call_count = [0]

        async def flaky_send_action(action: str) -> None:
            call_count[0] += 1
            if call_count[0] <= 2:
                raise Exception("Network error")

        chat.send_action = flaky_send_action

        orchestrator = MessageOrchestrator(agentic_settings, deps)
        heartbeat = orchestrator._start_typing_heartbeat(chat, interval=0.05)

        await asyncio.sleep(0.3)
        heartbeat.cancel()
        try:
            await heartbeat
        except asyncio.CancelledError:
            pass

        # Should have called send_action more than 2 times (survived errors)
        assert call_count[0] >= 3

    async def test_stream_callback_independent_of_typing(self, agentic_settings, deps):
        """Stream callback no longer sends typing — that's the heartbeat's job."""
        orchestrator = MessageOrchestrator(agentic_settings, deps)

        progress_msg = AsyncMock()
        tool_log: list = []  # type: ignore[type-arg]
        callback = orchestrator._make_stream_callback(
            verbose_level=1,
            progress_msg=progress_msg,
            tool_log=tool_log,
            start_time=0.0,
        )
        assert callback is not None

        # Verify the callback signature doesn't accept a 'chat' parameter
        # (typing is no longer handled by the stream callback)
        import inspect

        sig = inspect.signature(orchestrator._make_stream_callback)
        assert "chat" not in sig.parameters


# --- Claude model tier selection ---


def _ctx_with(user_data):
    ctx = MagicMock()
    ctx.user_data = user_data
    return ctx


def test_model_override_default_none(agentic_settings, deps):
    """No user pick and no AGENT_MODEL -> None (config/CLI default applies)."""
    orchestrator = MessageOrchestrator(agentic_settings, deps)
    assert orchestrator._model_override(_ctx_with({})) is None


def test_model_override_user_tier(agentic_settings, deps):
    orchestrator = MessageOrchestrator(agentic_settings, deps)
    ctx = _ctx_with({"agent_model": "opus"})
    assert orchestrator._model_override(ctx) == "opus"


def test_model_override_for_codex_is_the_validated_codex_ref(agentic_settings, deps):
    """Codex gets its model through the same kwarg (dv-4207): a Claude tier
    left in user_data is not a Codex model, so the catalog default wins."""
    from src.bot.orchestration.base import _CODEX_DEFAULT_MODEL

    orchestrator = MessageOrchestrator(agentic_settings, deps)
    ctx = _ctx_with({"agent_backend": "openai_codex", "agent_model": "sonnet"})
    assert orchestrator._model_override(ctx) == _CODEX_DEFAULT_MODEL
    ctx = _ctx_with(
        {"agent_backend": "openai_codex", "agent_model": "openai-codex/gpt-5.5"}
    )
    assert orchestrator._model_override(ctx) == "openai-codex/gpt-5.5"


def test_model_override_ignores_placeholder(agentic_settings, deps):
    """Legacy 'claude' placeholder in user_data must not become the model."""
    orchestrator = MessageOrchestrator(agentic_settings, deps)
    ctx = _ctx_with({"agent_model": "claude"})
    assert orchestrator._model_override(ctx) is None


class TestThinkingProgressRendering:
    """Reasoning text renders with its own icon; no dataclass repr survives (dv-4b71)."""

    @staticmethod
    def _update(update_type: str, content, tool_calls=None):
        return SimpleNamespace(type=update_type, content=content, tool_calls=tool_calls)

    async def _run(self, orchestrator, verbose_level, updates, draft_streamer=None):
        tool_log: list = []
        progress_msg = AsyncMock()
        callback = orchestrator._make_stream_callback(
            verbose_level=verbose_level,
            progress_msg=progress_msg,
            tool_log=tool_log,
            start_time=0.0,
            draft_streamer=draft_streamer,
        )
        if callback is None:
            return tool_log, None
        for upd in updates:
            await callback(upd)
        return tool_log, progress_msg

    async def test_thinking_logged_at_level_1(self, agentic_settings, deps):
        """Level 1 records the thought as its own activity kind."""
        orchestrator = MessageOrchestrator(agentic_settings, deps)
        tool_log, _ = await self._run(
            orchestrator,
            1,
            [self._update("thinking", "The service is masked.\nUnmask it.")],
        )

        assert tool_log == [
            {"kind": "thinking", "detail": "The service is masked. Unmask it."}
        ]

    async def test_thinking_ignored_at_level_0(self, agentic_settings, deps):
        """Verbose level 0 shows nothing at all."""
        orchestrator = MessageOrchestrator(agentic_settings, deps)
        tool_log, progress_msg = await self._run(
            orchestrator, 0, [self._update("thinking", "Silent thought.")]
        )

        assert tool_log == []
        assert progress_msg is None  # callback not even created

        # And even if an entry somehow existed, level 0 renders none of it.
        rendered = orchestrator._format_verbose_progress(
            [{"kind": "thinking", "detail": "Silent thought."}], 0, time.time()
        )
        assert "Silent thought." not in rendered
        assert "\U0001f4ad" not in rendered

    async def test_thinking_not_streamed_to_draft_at_level_0(
        self, agentic_settings, deps
    ):
        """Draft streaming keeps the callback alive at level 0; thinking still hidden."""
        orchestrator = MessageOrchestrator(agentic_settings, deps)
        draft_streamer = AsyncMock()
        tool_log, _ = await self._run(
            orchestrator,
            0,
            [self._update("thinking", "Silent thought.")],
            draft_streamer=draft_streamer,
        )

        assert tool_log == []
        draft_streamer.append_tool.assert_not_called()

    async def test_thinking_streamed_to_draft_at_level_1(self, agentic_settings, deps):
        """With a draft streamer, thinking appears with the thought balloon."""
        orchestrator = MessageOrchestrator(agentic_settings, deps)
        draft_streamer = AsyncMock()
        await self._run(
            orchestrator,
            1,
            [self._update("thinking", "Weighing two options here.")],
            draft_streamer=draft_streamer,
        )

        (line,), _ = draft_streamer.append_tool.call_args
        assert line.startswith("\U0001f4ad ")
        assert "Weighing two options here." in line

    async def test_empty_thinking_content_logs_nothing(self, agentic_settings, deps):
        """A redacted block that somehow reaches the callback is still dropped."""
        orchestrator = MessageOrchestrator(agentic_settings, deps)
        tool_log, _ = await self._run(
            orchestrator,
            2,
            [self._update("thinking", ""), self._update("thinking", "   \n ")],
        )

        assert tool_log == []

    async def test_thinking_and_text_use_different_icons(self, agentic_settings, deps):
        """Thought balloon for reasoning, speech balloon for commentary."""
        orchestrator = MessageOrchestrator(agentic_settings, deps)
        tool_log, _ = await self._run(
            orchestrator,
            2,
            [
                self._update("thinking", "Reasoning here."),
                self._update("assistant", "Commentary here."),
            ],
        )

        rendered = orchestrator._format_verbose_progress(tool_log, 2, time.time())
        assert "\U0001f4ad Reasoning here." in rendered
        assert "\U0001f4ac Commentary here." in rendered

    def test_level_1_truncates_harder_than_level_2(self, agentic_settings, deps):
        """Level 1 gets a short slice; level 2 a longer one; both are clipped."""
        orchestrator = MessageOrchestrator(agentic_settings, deps)
        long_thought = "word " * 100
        tool_log = [{"kind": "thinking", "detail": long_thought.strip()}]

        level1 = orchestrator._format_verbose_progress(tool_log, 1, time.time())
        level2 = orchestrator._format_verbose_progress(tool_log, 2, time.time())

        line1 = [ln for ln in level1.splitlines() if ln.startswith("\U0001f4ad")][0]
        line2 = [ln for ln in level2.splitlines() if ln.startswith("\U0001f4ad")][0]
        assert len(line1) < len(line2)
        assert line1.endswith("…")
        assert line2.endswith("…")
        assert len(line2) <= 210

    def test_short_thought_is_not_ellipsized(self, agentic_settings, deps):
        """Text that fits is shown verbatim, with no trailing ellipsis."""
        orchestrator = MessageOrchestrator(agentic_settings, deps)
        tool_log = [{"kind": "thinking", "detail": "Short thought."}]

        rendered = orchestrator._format_verbose_progress(tool_log, 1, time.time())

        assert "\U0001f4ad Short thought." in rendered
        assert "…" not in rendered

    async def test_no_dataclass_repr_can_reach_the_progress_message(
        self, agentic_settings, deps
    ):
        """End-to-end guard: real SDK blocks in, no repr out."""
        from claude_agent_sdk import AssistantMessage, TextBlock, ThinkingBlock

        from src.claude.sdk_integration import ClaudeSDKManager
        from src.config.settings import Settings

        sdk_manager = ClaudeSDKManager(
            Settings(
                telegram_bot_token="test:token",
                telegram_bot_username="testbot",
                approved_directory=agentic_settings.approved_directory,
                claude_timeout_seconds=2,
                enable_mcp=False,
            )
        )
        orchestrator = MessageOrchestrator(agentic_settings, deps)
        tool_log: list = []
        callback = orchestrator._make_stream_callback(
            verbose_level=2,
            progress_msg=AsyncMock(),
            tool_log=tool_log,
            start_time=0.0,
        )

        messages = [
            AssistantMessage(
                content=[
                    ThinkingBlock(
                        thinking="",
                        signature="CAIS1wQKhwEIEBgCKkC53BJ2rTY1bGcWeduwBJ3OM",
                    )
                ],
                model="m",
            ),
            AssistantMessage(
                content=[ThinkingBlock(thinking="Real thought.", signature="sig")],
                model="m",
            ),
            AssistantMessage(
                content=[
                    ThinkingBlock(thinking="Mixed thought.", signature="sig"),
                    TextBlock(text="Answer text."),
                ],
                model="m",
            ),
        ]
        for msg in messages:
            await sdk_manager._handle_stream_message(msg, callback)

        rendered = orchestrator._format_verbose_progress(tool_log, 2, time.time())
        assert "ThinkingBlock(" not in rendered
        assert "TextBlock(" not in rendered
        assert "signature=" not in rendered
        assert "Real thought." in rendered
        assert "Mixed thought." in rendered
        assert "Answer text." in rendered
        # The redacted block contributed nothing.
        assert len(tool_log) == 3
