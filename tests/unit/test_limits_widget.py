"""/limits as a Bot API 10.4 widget in the owner's DM (dv-4665).

Chain: sendWidget in a private chat, widget_query received through
``update.api_kwargs`` (PTB 22.6 has no 10.4), answerWidgetQuery. All raw
calls live in ``src/bot/telegram_widgets.py``.
"""

import asyncio
import json
import re
import time
import zlib
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram import Update
from telegram.ext import TypeHandler

from src.bot.core import ClaudeCodeBot
from src.bot.orchestrator import MessageOrchestrator
from src.config import create_test_config
from tests.unit.test_limits_panel import (
    USER_ID,
    _backends,
    _command_update,
    _context,
    _settings,
)

ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / "src" / "bot" / "telegram_widgets.py"


def _bot_mock() -> MagicMock:
    bot = MagicMock()
    bot.do_api_request = AsyncMock(return_value={"message_id": 5})
    return bot


def _calls(bot: MagicMock, endpoint: str) -> list:
    return [c for c in bot.do_api_request.await_args_list if c.args[0] == endpoint]


def _all_text(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


# --- C1 ---------------------------------------------------------------------


async def test_C1_private_chat_sends_widget_and_no_text(tmp_path):
    settings = _settings(tmp_path)
    orch = MessageOrchestrator(settings, {})
    context = _context(settings, {"backends": _backends()})
    context.bot = _bot_mock()
    update = _command_update()

    await orch.agentic_limits(update, context)

    sends = _calls(context.bot, "sendWidget")
    assert len(sends) == 1
    update.message.reply_text.assert_not_called()
    blob = _all_text(sends[0])
    assert "<" in blob and "caption" in blob


async def test_C1_group_chat_answers_text(tmp_path):
    settings = _settings(tmp_path)
    orch = MessageOrchestrator(settings, {})
    context = _context(settings, {"backends": _backends()})
    context.bot = _bot_mock()
    update = _command_update()
    update.message.chat.type = "supergroup"
    update.effective_chat.type = "supergroup"

    await orch.agentic_limits(update, context)

    assert not _calls(context.bot, "sendWidget")
    update.message.reply_text.assert_awaited()


async def test_C1_kill_switch_off_answers_text(tmp_path):
    settings = create_test_config(
        approved_directory=str(tmp_path),
        agentic_mode=True,
        limits_widget_enabled=False,
    )
    orch = MessageOrchestrator(settings, {})
    context = _context(settings, {"backends": _backends()})
    context.bot = _bot_mock()
    update = _command_update()

    await orch.agentic_limits(update, context)

    assert not _calls(context.bot, "sendWidget")
    update.message.reply_text.assert_awaited()


async def test_C1_send_failure_falls_back_to_text(tmp_path):
    settings = _settings(tmp_path)
    orch = MessageOrchestrator(settings, {})
    context = _context(settings, {"backends": _backends()})
    context.bot = _bot_mock()
    context.bot.do_api_request = AsyncMock(side_effect=RuntimeError("Bad Request"))
    update = _command_update()

    await orch.agentic_limits(update, context)

    update.message.reply_text.assert_awaited()


# --- C2 ---------------------------------------------------------------------


def _widget_update(user_id: int) -> Update:
    return Update.de_json(
        {
            "update_id": 9,
            "widget_query": {
                "id": "wq1",
                "from": {"id": user_id, "is_bot": False, "first_name": "x"},
                "data": "r",
            },
        },
        None,
    )


async def _init(bot: ClaudeCodeBot) -> None:
    from telegram.ext import Application

    with (
        patch.object(Application, "initialize", AsyncMock()),
        patch.object(ClaudeCodeBot, "_set_bot_commands", AsyncMock()),
    ):
        await bot.initialize()


async def _widget_handler(tmp_path, backends):
    settings = create_test_config(
        approved_directory=str(tmp_path), agentic_mode=True, allowed_users=[USER_ID]
    )
    bot = ClaudeCodeBot(settings, {"backends": backends})
    await _init(bot)
    handlers = [h for h in bot.app.handlers[-4] if isinstance(h, TypeHandler)]
    assert handlers, "no TypeHandler(Update) in group -4"
    return bot, handlers


async def _run_query(tmp_path, backends, user_id):
    bot, handlers = await _widget_handler(tmp_path, backends)
    api = AsyncMock(return_value=True)
    context = MagicMock()
    context.bot_data = {}
    context.bot = bot.app.bot
    update = _widget_update(user_id)
    with patch.object(type(bot.app.bot), "do_api_request", api):
        for h in handlers:
            await h.callback(update, context)
    return api, context


def _answer_payload(api) -> str:
    answers = [c for c in api.await_args_list if c.args[0] == "answerWidgetQuery"]
    assert len(answers) == 1
    return _all_text(answers[0])


async def test_C2_owner_query_answered_once_within_1024_bytes(tmp_path):
    api, _ = await _run_query(tmp_path, _backends(), USER_ID)
    payload = _answer_payload(api)
    assert "wq1" in payload
    answer = [c for c in api.await_args_list if c.args[0] == "answerWidgetQuery"][0]
    data = answer.kwargs.get("api_kwargs") or (
        answer.args[1] if len(answer.args) > 1 else {}
    )
    # the data field that carries the numbers must fit Telegram's 1024 bytes
    for value in data.values():
        if isinstance(value, str):
            assert len(value.encode("utf-8")) <= 1024
        elif isinstance(value, (dict, list)):
            assert len(json.dumps(value, ensure_ascii=False).encode()) <= 1024


async def test_C2_slow_codex_read_still_answers_before_deadline(tmp_path):
    backends = _backends()

    async def hang(*a, **k):
        await asyncio.sleep(60)

    backends["openai_codex"].read_account_limits = AsyncMock(side_effect=hang)
    start = time.monotonic()
    api, _ = await _run_query(tmp_path, backends, USER_ID)
    assert time.monotonic() - start < 14
    _answer_payload(api)


async def test_C2_failing_codex_read_still_answers(tmp_path):
    backends = _backends(codex_limits=RuntimeError("boom"))
    api, _ = await _run_query(tmp_path, backends, USER_ID)
    _answer_payload(api)


async def test_C2_non_owner_gets_answer_without_limits(tmp_path):
    backends = _backends()
    api, _ = await _run_query(tmp_path, backends, 4242)
    _answer_payload(api)
    backends["openai_codex"].read_account_limits.assert_not_called()


async def test_C2_update_without_widget_query_is_ignored(tmp_path):
    bot, handlers = await _widget_handler(tmp_path, _backends())
    api = AsyncMock()
    update = Update.de_json({"update_id": 3}, None)
    context = MagicMock()
    context.bot_data = {}
    with patch.object(type(bot.app.bot), "do_api_request", api):
        for h in handlers:
            await h.callback(update, context)
    api.assert_not_called()


# --- C3 ---------------------------------------------------------------------


async def _started_allowed(webhook: bool, tmp_path):
    settings = create_test_config(
        approved_directory=str(tmp_path),
        agentic_mode=True,
        webhook_url="https://example.org/hook" if webhook else None,
    )
    bot = ClaudeCodeBot(settings, {})
    await _init(bot)
    bot.app.initialize = AsyncMock()
    bot.app.start = AsyncMock()
    bot.app.run_webhook = AsyncMock()
    bot.app.updater = MagicMock()

    async def _stop(*a, **k):
        bot.is_running = False

    bot.app.updater.start_polling = AsyncMock(side_effect=_stop)
    with patch("src.bot.core.monitor_loop_lag", AsyncMock()):
        await bot.start()
    call = (
        bot.app.run_webhook.await_args
        if webhook
        else bot.app.updater.start_polling.await_args
    )
    return call.kwargs["allowed_updates"]


@pytest.mark.parametrize("webhook", [False, True])
async def test_C3_allowed_updates_include_widget_types(tmp_path, webhook):
    allowed = await _started_allowed(webhook, tmp_path)
    for t in Update.ALL_TYPES:
        assert t in allowed
    assert "widget_query" in allowed
    assert "shared_widget_query" in allowed


# --- C4 ---------------------------------------------------------------------


def test_C4_raw_calls_live_only_in_widget_module():
    assert MODULE.exists()
    # dv-1c98: rich_message.py is the second raw Bot API caller (sendRichMessage)
    rich = ROOT / "src" / "bot" / "rich_message.py"
    for path in (ROOT / "src").rglob("*.py"):
        if path == MODULE:
            continue
        text = path.read_text()
        needles = ("sendWidget", "answerWidgetQuery")
        if path != rich:
            needles = ("do_api_request",) + needles
        for needle in needles:
            assert needle not in text, f"{needle} in {path}"
        assert (
            not re.search(r"""["']widget_query["']""", text) or "allowed" in text
        ), path


def test_C4_module_is_typed_named_and_documented():
    import inspect

    from src.bot import telegram_widgets as tw

    head = MODULE.read_text()[:1500].lower()
    assert "10.4" in head and ("remove" in head or "delete" in head or "goes" in head)
    for name in ("send_widget", "answer_widget_query", "widget_query_from_update"):
        fn = getattr(tw, name)
        assert inspect.iscoroutinefunction(fn) or name.endswith("_update")
        sig = inspect.signature(fn)
        assert sig.return_annotation is not inspect.Signature.empty
        for p in sig.parameters.values():
            assert p.annotation is not inspect.Parameter.empty, (name, p.name)


# --- C6 ---------------------------------------------------------------------


async def test_C6_widget_html_size_and_no_external_refs(tmp_path):
    settings = _settings(tmp_path)
    orch = MessageOrchestrator(settings, {})
    context = _context(settings, {"backends": _backends()})
    context.bot = _bot_mock()
    await orch.agentic_limits(_command_update(), context)
    sends = _calls(context.bot, "sendWidget")
    assert sends
    html = None
    for v in list((sends[0].kwargs.get("api_kwargs") or {}).values()):
        if isinstance(v, str) and "<" in v and "caption" != v:
            html = v if html is None or len(v) > len(html) else html
    assert html
    raw = html.encode("utf-8")
    assert len(raw) <= 32768
    assert len(zlib.compress(raw, 9)) <= 4096
    low = html.lower()
    assert not re.search(r"https?://|fetch\(|xmlhttprequest|<script[^>]+src", low)
