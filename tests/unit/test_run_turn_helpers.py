"""dv-04c8: direct coverage for the ``_run_turn`` stages that had none.

``_render_footer`` and ``_run_save_honesty`` used to be inline in
``_run_turn`` and reachable only through a full turn (the footer's
rotation / restart / estimate / overflow branches and the whole
save-honesty telemetry path had no test). The other extracted stages —
budget true-up, interaction store, window accounting + roll, delivery —
are guarded by the existing end-to-end turn tests.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.bot.orchestration.turns import TurnRequest, TurnState
from src.bot.orchestrator import MessageOrchestrator
from src.bot.utils.formatting import FormattedMessage
from src.config.settings import Settings


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
        chat=MagicMock(),
        user_id=123,
        role="owner",
        resolve_route=_no_route,
    )
    fields.update(kw)
    return TurnRequest(**fields)


def _state(
    orch: MessageOrchestrator,
    settings: Settings,
    *,
    request: TurnRequest,
    bot_data: dict,
    verbose_level: int = 1,
) -> TurnState:
    context = MagicMock()
    context.user_data = {}
    context.bot_data = {"settings": settings, **bot_data}
    return TurnState(
        request=request,
        context=context,
        user_id=request.user_id,
        current_dir="/w",
        storage_prompt=request.storage_prompt,
        selected_backend="claude",
        verbose_level=verbose_level,
        family_id=None,
    )


def _response(
    content="the answer",
    *,
    input_tokens=100,
    output_tokens=200,
    cost=0.0042,
    tools_used=(),
    session_restarted=False,
) -> MagicMock:
    r = MagicMock()
    r.session_id = "sess-1"
    r.content = content
    r.tools_used = [{"name": t} for t in tools_used]
    r.input_tokens = input_tokens
    r.output_tokens = output_tokens
    r.cost = cost
    r.num_turns = 1
    r.session_restarted = session_restarted
    return r


def _window(last_context_tokens=42_000, threshold=150_000):
    repo = MagicMock()
    repo.refresh_threshold_tokens = threshold
    wstate = MagicMock()
    wstate.last_context_tokens = last_context_tokens
    return repo, wstate


@pytest.fixture
def orch(monkeypatch):
    settings = _settings()
    o = MessageOrchestrator(settings, {})
    # The model label is what /status prints; pin it so the test reads the
    # footer, not the model-resolution chain (covered elsewhere).
    monkeypatch.setattr(o, "_selected_model", lambda ctx: "openai-codex/gpt-5")
    return o, settings


# --- _render_footer -----------------------------------------------------------


def _footer_state(orch, settings, *, verbose_level, show_footer=True, **turn):
    request = _request(
        show_footer=show_footer, storage_prompt=turn.pop("storage_prompt", "hi")
    )
    state = _state(
        orch, settings, request=request, bot_data={}, verbose_level=verbose_level
    )
    for k, v in turn.items():
        setattr(state, k, v)
    return state


@pytest.mark.parametrize("verbose_level,show_footer", [(0, True), (1, False)])
def test_footer_hidden_below_verbose_1_or_when_path_opts_out(
    orch, verbose_level, show_footer
):
    o, settings = orch
    state = _footer_state(
        o, settings, verbose_level=verbose_level, show_footer=show_footer
    )
    msgs = [FormattedMessage(text="reply", parse_mode="HTML")]

    o._render_footer(state, _response(), msgs)

    assert msgs == [FormattedMessage(text="reply", parse_mode="HTML")]


def test_verbose_1_footer_is_model_tail_and_window_gauge_only(orch):
    o, settings = orch
    repo, wstate = _window(last_context_tokens=42_000, threshold=150_000)
    state = _footer_state(
        o, settings, verbose_level=1, window_repo=repo, window_state_for_footer=wstate
    )
    msgs = [FormattedMessage(text="reply", parse_mode="HTML")]

    o._render_footer(state, _response(), msgs)

    assert len(msgs) == 1
    assert msgs[0].parse_mode == "HTML"
    assert msgs[0].text == "reply\n\n---\ngpt-5 | win 42k/150k"


def test_rotation_replaces_the_gauge(orch):
    o, settings = orch
    repo, wstate = _window()
    state = _footer_state(
        o,
        settings,
        verbose_level=1,
        window_repo=repo,
        window_state_for_footer=wstate,
        window_rotated_for_footer=True,
    )
    msgs = [FormattedMessage(text="reply", parse_mode="HTML")]

    o._render_footer(state, _response(), msgs)

    assert msgs[0].text.endswith("gpt-5 | 🔄 window rotated")
    assert "win " not in msgs[0].text


def test_no_window_repo_means_no_gauge(orch):
    o, settings = orch
    state = _footer_state(o, settings, verbose_level=1)
    msgs = [FormattedMessage(text="reply", parse_mode=None)]

    o._render_footer(state, _response(), msgs)

    assert msgs[0].text == "reply\n\n---\ngpt-5"


def test_session_restart_flag_is_surfaced(orch):
    o, settings = orch
    state = _footer_state(o, settings, verbose_level=1)
    msgs = [FormattedMessage(text="reply", parse_mode=None)]

    o._render_footer(state, _response(session_restarted=True), msgs)

    assert msgs[0].text.endswith("gpt-5 | ↻ session restarted (resume failed)")


def test_verbose_2_adds_real_tokens_and_price(orch):
    o, settings = orch
    state = _footer_state(o, settings, verbose_level=2)
    msgs = [FormattedMessage(text="reply", parse_mode=None)]

    o._render_footer(
        state, _response(input_tokens=100, output_tokens=200, cost=0.0042), msgs
    )

    assert (
        msgs[0].text == "reply\n\n---\ngpt-5 | 100 in | 200 out | 300 total | $0.0042"
    )


def test_verbose_2_estimates_when_backend_reports_no_counts(orch):
    o, settings = orch
    state = _footer_state(o, settings, verbose_level=2, storage_prompt="x" * 40)
    msgs = [FormattedMessage(text="reply", parse_mode=None)]

    o._render_footer(
        state,
        _response("y" * 80, input_tokens=0, output_tokens=0, cost=None),
        msgs,
    )

    assert msgs[0].text == "reply\n\n---\ngpt-5 | ~10 in | ~20 out | ~30 total"


@pytest.mark.parametrize("cost", [None, 0.0])
def test_unpriced_and_free_turns_show_no_dollar_figure(orch, cost):
    o, settings = orch
    state = _footer_state(o, settings, verbose_level=2)
    msgs = [FormattedMessage(text="reply", parse_mode=None)]

    o._render_footer(state, _response(cost=cost), msgs)

    assert "$" not in msgs[0].text
    assert "300 total" in msgs[0].text


def test_footer_overflowing_telegram_limit_goes_out_as_its_own_message(orch):
    o, settings = orch
    state = _footer_state(o, settings, verbose_level=1)
    long_text = "x" * 3995
    msgs = [FormattedMessage(text=long_text, parse_mode="HTML")]

    o._render_footer(state, _response(), msgs)

    assert msgs[0] == FormattedMessage(text=long_text, parse_mode="HTML")
    assert msgs[1] == FormattedMessage(text="---\ngpt-5", parse_mode=None)


# --- _run_save_honesty --------------------------------------------------------


def _honesty_state(orch, settings, *, storage, event_bus=None, evaluator=None, **req):
    request = _request(**req)
    bot_data = {"storage": storage, "event_bus": event_bus}
    if evaluator is not None:
        bot_data["evaluator_service"] = evaluator
    state = _state(orch, settings, request=request, bot_data=bot_data)
    state.saved_message_id = 77
    return state


def _storage():
    s = MagicMock()
    s.log_bot_event = AsyncMock()
    return s


def _events(storage):
    return [
        (c.args[1], c.args[2], c.kwargs) for c in storage.log_bot_event.call_args_list
    ]


async def test_disabled_check_logs_nothing():
    # The switch is read off the orchestrator's own settings, not bot_data.
    settings = _settings(save_claim_check_enabled=False)
    o = MessageOrchestrator(settings, {})
    storage = _storage()
    state = _honesty_state(o, settings, storage=storage)

    await o._run_save_honesty(state, _response("Записал: обед — суп."))

    storage.log_bot_event.assert_not_awaited()


async def test_no_storage_is_a_noop(orch):
    o, settings = orch
    state = _honesty_state(o, settings, storage=None)

    await o._run_save_honesty(state, _response("Записал: обед — суп."))


async def test_honest_claim_is_counted_but_not_a_mismatch(orch):
    o, settings = orch
    storage = _storage()
    bus = MagicMock()
    bus.publish = AsyncMock()
    state = _honesty_state(o, settings, storage=storage, event_bus=bus)

    await o._run_save_honesty(
        state, _response("Записал: обед — суп.", tools_used=["Read", "Write"])
    )

    events = _events(storage)
    assert [e[0] for e in events] == ["save_claim_detected"]
    payload = events[0][1]
    assert payload["message_id"] == 77
    assert payload["profile"] == "devops"
    assert payload["satisfied_by"] == "Write"
    assert payload["trigger_fired"] is False
    bus.publish.assert_not_awaited()


async def test_claim_without_write_logs_mismatch_and_informs_owner(orch):
    o, settings = orch
    storage = _storage()
    bus = MagicMock()
    bus.publish = AsyncMock()
    state = _honesty_state(o, settings, storage=storage, event_bus=bus)

    await o._run_save_honesty(state, _response("Записал: обед — суп."))

    kinds = [(e[0], e[2].get("success", True)) for e in _events(storage)]
    assert kinds == [("save_claim_detected", True), ("save_claim_mismatch", False)]
    mismatch = _events(storage)[1][1]
    assert mismatch["mismatch_kind"] == "claim_no_write"
    assert mismatch["message_id"] == 77
    assert mismatch["tools_used"] == []
    assert "Записал" in mismatch["claim_excerpt"]

    bus.publish.assert_awaited_once()
    event = bus.publish.await_args.args[0]
    assert event.chat_id == 0
    assert event.source == "save_honesty"
    assert event.parse_mode is None
    assert "[claim_no_write] devops msg=77:" in event.text
    assert "Записал" in event.text


async def test_trigger_matches_raw_text_not_the_reply_envelope(orch):
    o, settings = orch
    storage = _storage()
    # The quoted envelope says «запомни»; the user's own text does not.
    state = _honesty_state(
        o,
        settings,
        storage=storage,
        storage_prompt="> запомни это\n\nда",
        trigger_text="да",
    )
    assert state.trigger_text == "да"

    await o._run_save_honesty(state, _response("Хорошо!"))

    storage.log_bot_event.assert_not_awaited()


async def test_trigger_without_write_or_eval_is_trigger_no_write(orch):
    o, settings = orch
    storage = _storage()
    state = _honesty_state(
        o, settings, storage=storage, storage_prompt="запомни: я вегетарианец"
    )

    await o._run_save_honesty(state, _response("Хорошо!"))

    events = _events(storage)
    assert [e[0] for e in events] == ["save_claim_detected", "save_claim_mismatch"]
    assert events[0][1]["trigger_fired"] is True
    assert events[0][1]["matched_pattern"] is None
    assert events[1][1]["mismatch_kind"] == "trigger_no_write"


async def test_trigger_with_evaluator_scheduled_is_satisfied(orch):
    o, settings = orch
    storage = _storage()
    state = _honesty_state(
        o,
        settings,
        storage=storage,
        evaluator=MagicMock(),
        storage_prompt="запомни: я вегетарианец",
    )

    await o._run_save_honesty(state, _response("Хорошо!"))

    events = _events(storage)
    assert [e[0] for e in events] == ["save_claim_detected"]
    assert events[0][1]["satisfied_by"] == "force_eval"


async def test_telemetry_failure_never_escapes(orch):
    o, settings = orch
    storage = _storage()
    storage.log_bot_event = AsyncMock(side_effect=RuntimeError("db down"))
    state = _honesty_state(o, settings, storage=storage)

    await o._run_save_honesty(state, _response("Записал: обед — суп."))


# --- _render_footer: part order and real model resolution (dv-e748) ---------
# Replaces tests/unit/test_token_footer.py, which re-implemented the footer
# inline and never called orchestrator code.


def test_verbose_2_footer_keeps_the_full_part_order(orch):
    o, settings = orch
    repo, wstate = _window()
    state = _footer_state(
        o,
        settings,
        verbose_level=2,
        window_repo=repo,
        window_state_for_footer=wstate,
        window_rotated_for_footer=True,
    )
    msgs = [FormattedMessage(text="reply", parse_mode=None)]

    o._render_footer(state, _response(session_restarted=True), msgs)

    assert msgs[0].text == (
        "reply\n\n---\ngpt-5 | 100 in | 200 out | 300 total | $0.0042 | "
        "↻ session restarted (resume failed) | 🔄 window rotated"
    )


@pytest.mark.parametrize(
    "user_data,label",
    [
        ({"agent_backend": "claude", "agent_model": "opus"}, "opus"),
        # No tier picked: the configured Claude model, never the "claude"
        # sentinel the callback stores for "config default".
        ({"agent_backend": "claude"}, "{claude_model}"),
        (
            {"agent_backend": "openai_codex", "agent_model": "openai-codex/gpt-5.5"},
            "gpt-5.5",
        ),
    ],
)
def test_footer_model_label_is_the_users_pick_without_the_provider_prefix(
    user_data, label
):
    settings = _settings()
    o = MessageOrchestrator(settings, {})  # real _selected_model, not pinned
    state = _footer_state(o, settings, verbose_level=1)
    state.context.user_data = user_data
    msgs = [FormattedMessage(text="reply", parse_mode=None)]

    o._render_footer(state, _response(), msgs)

    # Unset CLAUDE_MODEL (a fresh clone, no .env) renders as "CLI default".
    expected = label.format(claude_model=settings.claude_model or "CLI default")
    assert msgs[0].text == f"reply\n\n---\n{expected}"
