"""Tests for the recipe search-retry detector and hook (dv-300d / dv-5d5f
follow-up).

The detector (``needs_search_retry``) must stay pure and I/O-free so an
offline measurement script can call the exact same code as the live hook.
The hook tests mirror the harness in ``test_run_turn_helpers.py``
(``_run_save_honesty`` coverage) since ``_run_recipe_search_retry`` is the
same kind of post-turn stage on ``MessageOrchestrator``.
"""

from unittest.mock import AsyncMock, MagicMock

from telegram.error import BadRequest

from src.bot.orchestration.recipe_retry import (
    RECIPE_RETRY_PROMPT,
    RETRY_ATTEMPT_LABEL,
    needs_search_retry,
    parse_retry_verdict,
)
from src.bot.orchestration.turns import TurnRequest, TurnState
from src.bot.orchestrator import MessageOrchestrator
from src.bot.utils.formatting import FormattedMessage
from src.config.settings import Settings
from src.storage.sent_messages import SentMessage

RECIPE_TEXT = "1. 200 г муки\n2. 100 мл молока\n3. Смешать и выпекать 20 минут."
PROSE_TEXT = "Конечно, шакшука — это просто и вкусно, приятного аппетита!"


# --- needs_search_retry (pure detector) --------------------------------------


def test_recipe_with_no_tools_needs_retry():
    assert needs_search_retry(RECIPE_TEXT, []) is True


def test_recipe_with_websearch_does_not_need_retry():
    assert needs_search_retry(RECIPE_TEXT, ["WebSearch"]) is False


def test_recipe_with_webfetch_and_other_tools_does_not_need_retry():
    assert needs_search_retry(RECIPE_TEXT, ["WebFetch", "Bash"]) is False


def test_prose_without_list_or_quantities_never_needs_retry():
    assert needs_search_retry(PROSE_TEXT, []) is False


# --- parse_retry_verdict (pure parser) ---------------------------------------


def test_parse_ok_verdict():
    verdict, summary, body = parse_retry_verdict(
        "[OK]\nПовар.ру подтвердил пропорции."
    )
    assert verdict == "ok"
    assert summary == "Повар.ру подтвердил пропорции."
    assert body == ""


def test_parse_corrected_verdict():
    verdict, summary, body = parse_retry_verdict(
        "[CORRECTED]\nСоли в 2 раза меньше.\n\n1. 100 г муки\n2. 50 мл молока"
    )
    assert verdict == "corrected"
    assert summary == "Соли в 2 раза меньше."
    assert body == "1. 100 г муки\n2. 50 мл молока"


def test_parse_bold_tag():
    verdict, summary, _ = parse_retry_verdict("**[OK]**\nИсточник: Повар.ру")
    assert verdict == "ok"
    assert summary == "Источник: Повар.ру"


def test_parse_missing_tag_returns_none():
    verdict, summary, body = parse_retry_verdict(
        "Просто обычный ответ без какого-либо тега."
    )
    assert (verdict, summary, body) == (None, None, None)


def test_parse_tag_not_on_first_line_returns_none():
    verdict, summary, body = parse_retry_verdict(
        "Какой-то текст перед тегом.\n[OK]\nИсточник: Повар.ру"
    )
    assert (verdict, summary, body) == (None, None, None)


def test_parse_trailing_text_on_tag_line_returns_none():
    verdict, summary, body = parse_retry_verdict(
        "[OK] Источник: Повар.ру\nещё текст"
    )
    assert (verdict, summary, body) == (None, None, None)


def test_parse_empty_text_returns_none():
    assert parse_retry_verdict("") == (None, None, None)


# --- hook: _run_recipe_search_retry ------------------------------------------


def _settings(**overrides) -> Settings:
    base = dict(
        telegram_bot_token="test:token",
        telegram_bot_username="testbot",
        approved_directory=None,
        enable_mcp=False,
        mcp_config_path=None,
    )
    base.update(overrides)
    return Settings(**base)


async def _no_route():
    return None


def _request(**kw) -> TurnRequest:
    fields = dict(
        prompt="hi",
        storage_prompt="hi",
        progress_msg=MagicMock(),
        chat=MagicMock(id=999),
        user_id=123,
        role="owner",
        resolve_route=_no_route,
    )
    fields.update(kw)
    return TurnRequest(**fields)


def _state(request: TurnRequest, bot_data: dict) -> TurnState:
    context = MagicMock()
    context.user_data = {}
    context.bot_data = dict(bot_data)
    context.bot = MagicMock()
    context.bot.edit_message_text = AsyncMock(return_value=MagicMock())
    state = TurnState(
        request=request,
        context=context,
        user_id=request.user_id,
        current_dir="/w",
        storage_prompt=request.storage_prompt,
        selected_backend="claude",
        verbose_level=1,
        family_id=None,
    )
    state.saved_message_id = 77
    return state


def _response(content=RECIPE_TEXT, tools_used=()) -> MagicMock:
    r = MagicMock()
    r.content = content
    r.tools_used = [{"name": t} for t in tools_used]
    return r


def _storage(sent_rows=None) -> MagicMock:
    s = MagicMock()
    s.log_bot_event = AsyncMock()
    s.sent_messages.list_for_message = AsyncMock(return_value=sent_rows or [])
    return s


def _sent_row(chat_id: int, message_id: int) -> SentMessage:
    return SentMessage(
        id=message_id,
        telegram_chat_id=chat_id,
        telegram_message_id=message_id,
        kind="turn_reply",
        messages_message_id=77,
        profile_slug="chef",
        created_at=None,
    )


def _backend(response=None) -> MagicMock:
    backend = MagicMock()
    backend.run_command = AsyncMock(
        return_value=response
        or _response("Проверил на сайтах — рецепт верный.", tools_used=["WebSearch"])
    )
    return backend


def _event_types(storage: MagicMock) -> list:
    return [c.args[1] for c in storage.log_bot_event.call_args_list]


async def test_noop_when_flag_disabled():
    o = MessageOrchestrator(_settings(recipe_retry_enabled=False), {})
    storage = _storage()
    backend = _backend()
    state = _state(_request(), {"storage": storage, "claude_integration": backend})

    await o._run_recipe_search_retry(
        state, _response(RECIPE_TEXT, tools_used=()), [FormattedMessage(RECIPE_TEXT)]
    )

    backend.run_command.assert_not_awaited()
    storage.log_bot_event.assert_not_awaited()


async def test_noop_for_non_chef_profile_when_flag_enabled(monkeypatch):
    o = MessageOrchestrator(
        _settings(recipe_retry_enabled=True, recipe_retry_profiles="chef"), {}
    )
    monkeypatch.setattr(o, "_agent_id_for", lambda *a, **kw: "devops")
    storage = _storage()
    backend = _backend()
    state = _state(_request(), {"storage": storage, "claude_integration": backend})

    await o._run_recipe_search_retry(
        state, _response(RECIPE_TEXT, tools_used=()), [FormattedMessage(RECIPE_TEXT)]
    )

    backend.run_command.assert_not_awaited()
    storage.log_bot_event.assert_not_awaited()


async def test_fires_once_when_trigger_matches(monkeypatch):
    o = MessageOrchestrator(
        _settings(recipe_retry_enabled=True, recipe_retry_profiles="chef"), {}
    )
    monkeypatch.setattr(o, "_agent_id_for", lambda *a, **kw: "chef#u123")
    storage = _storage()
    retry_reply = _response(
        "Проверил на 2 сайтах — рецепт верный.", tools_used=("WebSearch",)
    )
    backend = MagicMock()
    backend.run_command = AsyncMock(return_value=retry_reply)
    bus = MagicMock()
    bus.publish = AsyncMock()
    state = _state(
        _request(),
        {"storage": storage, "claude_integration": backend, "event_bus": bus},
    )

    await o._run_recipe_search_retry(
        state, _response(RECIPE_TEXT, tools_used=()), [FormattedMessage(RECIPE_TEXT)]
    )

    backend.run_command.assert_awaited_once()
    kwargs = backend.run_command.await_args.kwargs
    assert kwargs["prompt"] == RECIPE_RETRY_PROMPT
    assert kwargs["working_directory"] == "/w"
    assert kwargs["user_id"] == 123
    assert kwargs["requester_chat_id"] == 999

    assert _event_types(storage) == ["recipe_retry_fired", "recipe_retry_done"]
    fired_payload = storage.log_bot_event.call_args_list[0].args[2]
    assert fired_payload["message_id"] == 77
    assert fired_payload["profile"] == "chef"
    done_payload = storage.log_bot_event.call_args_list[1].args[2]
    assert done_payload["searched"] is True
    assert done_payload["verdict"] is None
    assert done_payload["delivery"] == "separate"

    bus.publish.assert_awaited_once()
    event = bus.publish.await_args.args[0]
    assert event.chat_id == 999
    assert event.text.startswith(RETRY_ATTEMPT_LABEL)
    assert event.parse_mode is None


async def test_fires_and_edits_in_place_on_ok_verdict(monkeypatch):
    """End-to-end: [OK] verdict -> edit in place, no second message."""
    o = MessageOrchestrator(
        _settings(recipe_retry_enabled=True, recipe_retry_profiles="chef"), {}
    )
    monkeypatch.setattr(o, "_agent_id_for", lambda *a, **kw: "chef#u123")
    retry_reply = _response(
        "[OK]\nПовар.ру подтвердил пропорции.", tools_used=("WebSearch",)
    )
    backend = MagicMock()
    backend.run_command = AsyncMock(return_value=retry_reply)
    bus = MagicMock()
    bus.publish = AsyncMock()
    storage = _storage(sent_rows=[_sent_row(999, 501)])
    state = _state(
        _request(),
        {"storage": storage, "claude_integration": backend, "event_bus": bus},
    )
    delivered = [FormattedMessage(RECIPE_TEXT)]

    await o._run_recipe_search_retry(
        state, _response(RECIPE_TEXT, tools_used=()), delivered
    )

    bus.publish.assert_not_awaited()
    state.context.bot.edit_message_text.assert_awaited_once()
    kwargs = state.context.bot.edit_message_text.await_args.kwargs
    assert kwargs["chat_id"] == 999
    assert kwargs["message_id"] == 501
    assert kwargs["text"] == (
        f"{RECIPE_TEXT}\n\n✅ Проверено: Повар.ру подтвердил пропорции."
    )
    done_payload = storage.log_bot_event.call_args_list[-1].args[2]
    assert done_payload["verdict"] == "ok"
    assert done_payload["delivery"] == "appended"


# --- _deliver_recipe_retry_verdict (delivery decisions) ----------------------


async def test_delivery_ok_edits_last_part_and_appends_line():
    o = MessageOrchestrator(_settings(recipe_retry_enabled=True), {})
    storage = _storage(sent_rows=[_sent_row(999, 501), _sent_row(999, 502)])
    state = _state(_request(), {"storage": storage})
    delivered = [FormattedMessage("Часть 1"), FormattedMessage("Рецепт готов.")]

    delivery = await o._deliver_recipe_retry_verdict(
        state, "ok", "Повар.ру подтвердил.", "", delivered
    )

    assert delivery == "appended"
    state.context.bot.edit_message_text.assert_awaited_once()
    kwargs = state.context.bot.edit_message_text.await_args.kwargs
    assert kwargs["chat_id"] == 999
    assert kwargs["message_id"] == 502  # the LAST part of a multi-part original
    assert kwargs["text"] == "Рецепт готов.\n\n✅ Проверено: Повар.ру подтвердил."
    assert kwargs["parse_mode"] == "HTML"


async def test_delivery_corrected_short_single_message_edits_replace():
    o = MessageOrchestrator(_settings(recipe_retry_enabled=True), {})
    storage = _storage(sent_rows=[_sent_row(999, 501)])
    state = _state(_request(), {"storage": storage})
    delivered = [FormattedMessage(RECIPE_TEXT)]

    delivery = await o._deliver_recipe_retry_verdict(
        state, "corrected", "Соли в 2 раза меньше.", "1. 100 г муки", delivered
    )

    assert delivery == "edited"
    state.context.bot.edit_message_text.assert_awaited_once()
    kwargs = state.context.bot.edit_message_text.await_args.kwargs
    assert kwargs["chat_id"] == 999
    assert kwargs["message_id"] == 501
    assert "Соли в 2 раза меньше." in kwargs["text"]
    assert "100 г муки" in kwargs["text"]


async def test_delivery_corrected_too_long_falls_back_separate():
    o = MessageOrchestrator(_settings(recipe_retry_enabled=True), {})
    storage = _storage(sent_rows=[_sent_row(999, 501)])
    state = _state(_request(), {"storage": storage})
    delivered = [FormattedMessage(RECIPE_TEXT)]

    delivery = await o._deliver_recipe_retry_verdict(
        state, "corrected", "Соли меньше.", "x" * 5000, delivered
    )

    assert delivery == "separate"
    state.context.bot.edit_message_text.assert_not_awaited()


async def test_delivery_corrected_multi_part_original_falls_back_separate():
    o = MessageOrchestrator(_settings(recipe_retry_enabled=True), {})
    storage = _storage(sent_rows=[_sent_row(999, 501), _sent_row(999, 502)])
    state = _state(_request(), {"storage": storage})
    delivered = [FormattedMessage("Часть 1"), FormattedMessage("Часть 2")]

    delivery = await o._deliver_recipe_retry_verdict(
        state, "corrected", "Соли меньше.", "1. 100 г муки", delivered
    )

    assert delivery == "separate"
    state.context.bot.edit_message_text.assert_not_awaited()


async def test_delivery_missing_tag_falls_back_separate():
    o = MessageOrchestrator(_settings(recipe_retry_enabled=True), {})
    storage = _storage(sent_rows=[_sent_row(999, 501)])
    state = _state(_request(), {"storage": storage})
    delivered = [FormattedMessage(RECIPE_TEXT)]

    delivery = await o._deliver_recipe_retry_verdict(state, None, None, None, delivered)

    assert delivery == "separate"
    state.context.bot.edit_message_text.assert_not_awaited()


async def test_delivery_edit_failure_falls_back_separate():
    o = MessageOrchestrator(_settings(recipe_retry_enabled=True), {})
    storage = _storage(sent_rows=[_sent_row(999, 501)])
    state = _state(_request(), {"storage": storage})
    state.context.bot.edit_message_text = AsyncMock(
        side_effect=BadRequest("message to edit not found")
    )
    delivered = [FormattedMessage(RECIPE_TEXT)]

    delivery = await o._deliver_recipe_retry_verdict(
        state, "ok", "Повар.ру подтвердил.", "", delivered
    )

    assert delivery == "separate"
