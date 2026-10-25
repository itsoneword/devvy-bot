"""Tests for the per-turn verification budget and confirm/escalate ladder
(dv-9f41).

Replay fixtures are rebuilt from the evidence quoted in the ticket (bot.db is
not reachable from a pump worktree): turn 1817's tool sequence, the dv-7a3c
leg programme written with zero data reads, and the dv-5d5f shakshuka.
"""

import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.bot.orchestration.recipe_retry import needs_search_retry
from src.bot.orchestrator import MessageOrchestrator
from src.claude.verification_budget import (
    CONFIRM_LABEL,
    CONTRADICTION_MARKER,
    ESCALATE_LABEL,
    PHASE_CONFIRM,
    PHASE_DRAFT,
    PHASE_ESCALATE,
    VerificationBudget,
    confirm_prompt,
    contradicted_claim,
    current_phase,
    make_budget,
    needs_confirm_check,
    verification_phase,
)
from src.llm_analytics.quality import (
    compute_quality_daily,
    is_grounded,
    looks_like_training_program,
    verification_calls,
)
from tests.unit.test_recipe_retry import (
    _event_types,
    _request,
    _response,
    _settings,
    _state,
    _storage,
)

# --- replay fixtures ---------------------------------------------------------

# Turn 1817 (coach, 2026-09-22): "чест на объём, проверим то, что найдём".
CHEST_SESSION = (
    "Грудь, объём:\n"
    "1. Жим штанги лёжа — 4×8-10, 80 кг, RPE 8\n"
    "2. Жим гантелей на наклонной — 3×10-12, RPE 8\n"
    "3. Разводка на кроссовере — 3×12-15\n"
    "4. Отжимания на брусьях — 3 до RIR 2"
)
TURN_1817_TOOLS = (
    ["mcp__user_data__log_activity", "mcp__user_data__lift_history"]
    + ["mcp__user_data__lift_history", "Agent"]
    + ["WebSearch"] * 17
    + ["WebFetch"] * 2
    + ["Write"]
)
# dv-7a3c: a full leg programme with zero data reads, zero searches.
LEG_PROGRAMME = (
    "День ног:\n"
    "- Присед 5×5, 100 кг\n"
    "- Румынская тяга 4×8, 80 кг\n"
    "- Выпады 3×12 на каждую ногу"
)
# dv-5d5f: the shakshuka (3 tbsp paste + 100 ml water as the sauce base).
SHAKSHUKA = (
    "Шакшука:\n"
    "1. 3 ст. л. томатной пасты\n"
    "2. 100 мл воды\n"
    "3. 4 яйца, тушить 8 минут"
)
PHYSIOLOGY_PROSE = (
    "Гипертрофия от растяжения реальна: в работе Pelland 2024 объём 10-20 "
    "подходов в неделю давал лучший прирост, но разница невелика."
)


def test_assert_fixture_shape() -> None:
    assert TURN_1817_TOOLS.count("WebSearch") == 17
    assert verification_calls(TURN_1817_TOOLS) == 20  # 17 + 2 + Agent


# --- classifier --------------------------------------------------------------


def test_training_program_detected() -> None:
    assert looks_like_training_program(CHEST_SESSION)
    assert looks_like_training_program(LEG_PROGRAMME)
    assert looks_like_training_program("1. Жим 3x10\n2. Тяга 3x10")
    assert looks_like_training_program("- Присед @ 75% 1RM\n- Тяга")


def test_physiology_prose_is_not_load_bearing() -> None:
    assert not looks_like_training_program(PHYSIOLOGY_PROSE)


def test_numbers_without_list_or_list_without_numbers_is_not_a_program() -> None:
    assert not looks_like_training_program("Сделай жим 4×8 с 80 кг сегодня.")
    assert not looks_like_training_program("1. Жим лёжа\n2. Разводка\n3. Брусья")


def test_recipe_is_not_a_training_program() -> None:
    # Grams/ml must not read as weights: chef traffic goes through recipe_retry.
    assert not looks_like_training_program(SHAKSHUKA)


def test_grounding() -> None:
    assert is_grounded(["mcp__user_data__lift_history"])
    assert is_grounded(["day_summary"])
    assert is_grounded(["WebSearch"])
    assert is_grounded(["Agent"])
    assert not is_grounded(["mcp__user_data__log_train", "Bash", "Read"])
    assert not is_grounded([])


# --- replay on both poles -----------------------------------------------------


def test_replay_7a3c_leg_programme_triggers_confirm() -> None:
    assert needs_confirm_check(LEG_PROGRAMME, []) is True
    assert needs_confirm_check(LEG_PROGRAMME, ["mcp__user_data__log_train"]) is True


def test_replay_5d5f_shakshuka_still_triggers_recipe_check() -> None:
    assert needs_search_retry(SHAKSHUKA, []) is True


def test_replay_1817_grounded_in_history_needs_no_confirm() -> None:
    # It read lift_history: the load-bearing numbers had local ground, so the
    # ladder never needs a follow-up turn.
    assert needs_confirm_check(CHEST_SESSION, TURN_1817_TOOLS) is False


def _replay(tools: list[str], budget: VerificationBudget) -> list[str]:
    """Tools that actually run under ``budget`` (denied calls are dropped)."""
    return [name for name in tools if budget.charge(name) is None]


def test_replay_1817_enforced_resolves_within_three_calls() -> None:
    budget = make_budget(PHASE_DRAFT, draft_max=1, confirm_max=3, enforce=True)
    ran = _replay(TURN_1817_TOOLS, budget)
    assert verification_calls(ran) <= 3
    assert verification_calls(ran) == 1  # Agent (weight 2) denied, 1 search
    assert "Agent" not in ran
    # Local data reads are never budgeted.
    assert ran.count("mcp__user_data__lift_history") == 2


def test_replay_1817_count_only_lets_everything_run_and_flags_over() -> None:
    budget = make_budget(PHASE_DRAFT, draft_max=1, confirm_max=3, enforce=False)
    ran = _replay(TURN_1817_TOOLS, budget)
    assert ran == TURN_1817_TOOLS
    assert budget.over
    assert budget.used == 21  # 17 + 2 + Agent weighted 2


# --- budget mechanics --------------------------------------------------------


def test_draft_budget_denies_past_limit_with_ladder_message() -> None:
    budget = make_budget(PHASE_DRAFT, draft_max=1, confirm_max=3, enforce=True)
    assert budget.charge("WebSearch") is None
    reason = budget.charge("WebSearch")
    assert reason is not None and "budget" in reason
    assert budget.used == 1  # a denied call is not charged
    assert not budget.over


def test_non_search_tools_are_free() -> None:
    budget = make_budget(PHASE_DRAFT, draft_max=0, confirm_max=3, enforce=True)
    for name in ("Read", "Bash", "mcp__user_data__lift_history", "Write"):
        assert budget.charge(name) is None
    assert budget.used == 0


def test_subagent_weighs_two() -> None:
    budget = make_budget(PHASE_DRAFT, draft_max=2, confirm_max=3, enforce=True)
    assert budget.charge("Task") is None
    assert budget.used == 2
    assert budget.charge("WebSearch") is not None


def test_confirm_phase_is_websearch_only_capped_at_confirm_max() -> None:
    budget = make_budget(PHASE_CONFIRM, draft_max=1, confirm_max=3, enforce=True)
    assert budget.charge("WebFetch") is not None
    assert budget.charge("Agent") is not None
    assert [budget.charge("WebSearch") for _ in range(3)] == [None] * 3
    assert budget.charge("WebSearch") is not None


def test_escalate_phase_is_uncapped() -> None:
    budget = make_budget(PHASE_ESCALATE, draft_max=1, confirm_max=3, enforce=True)
    assert all(budget.charge("WebFetch") is None for _ in range(10))
    assert not budget.over


def test_phase_contextvar_scoped_and_reset() -> None:
    assert current_phase() == PHASE_DRAFT
    with verification_phase(PHASE_CONFIRM):
        assert current_phase() == PHASE_CONFIRM
    assert current_phase() == PHASE_DRAFT


def test_contradiction_marker_parsing() -> None:
    assert contradicted_claim("Всё сходится, сверил с lift_history.") is None
    reply = f"Проверил.\n{CONTRADICTION_MARKER} 80 кг на 4×8 — твой рабочий 70 кг"
    assert contradicted_claim(reply) == "80 кг на 4×8 — твой рабочий 70 кг"
    assert contradicted_claim(f"- **{CONTRADICTION_MARKER}** RPE 10") == "** RPE 10"


def test_confirm_prompt_names_budget_and_marker() -> None:
    prompt = confirm_prompt(3)
    assert "3" in prompt and CONTRADICTION_MARKER in prompt and "WebFetch" in prompt


# --- PreToolUse hook wiring ---------------------------------------------------


def _hook_denied(decision: dict) -> bool:  # type: ignore[type-arg]
    specific = decision.get("hookSpecificOutput", {}) if decision else {}
    return specific.get("permissionDecision") == "deny"


async def test_pre_tool_use_hook_enforces_budget(tmp_path: Path) -> None:
    from src.claude.sdk_integration import _make_pre_tool_use_hook

    budget = make_budget(PHASE_DRAFT, draft_max=1, confirm_max=3, enforce=True)
    hook = _make_pre_tool_use_hook(
        working_directory=tmp_path, full_access=True, verification_budget=budget
    )
    call = {"tool_name": "WebSearch", "tool_input": {"query": "x"}}
    assert not _hook_denied(await hook(call, None, None))
    assert _hook_denied(await hook(call, None, None))
    assert not _hook_denied(
        await hook({"tool_name": "Read", "tool_input": {}}, None, None)
    )


async def test_pre_tool_use_hook_count_only_never_denies(tmp_path: Path) -> None:
    from src.claude.sdk_integration import _make_pre_tool_use_hook

    budget = make_budget(PHASE_DRAFT, draft_max=1, confirm_max=3, enforce=False)
    hook = _make_pre_tool_use_hook(
        working_directory=tmp_path, full_access=True, verification_budget=budget
    )
    call = {"tool_name": "WebSearch", "tool_input": {"query": "x"}}
    for _ in range(5):
        assert not _hook_denied(await hook(call, None, None))
    assert budget.used == 5 and budget.exceeded_logged


def _manager(tmp_path: Path, **overrides):  # type: ignore[no-untyped-def]
    from src.claude.sdk_integration import ClaudeSDKManager
    from src.profiles.registry import ProfileDefinition, ProfileRegistry

    profiles = []
    for slug in ("devops", "coach"):
        root = tmp_path / slug
        root.mkdir()
        profiles.append(
            ProfileDefinition(
                slug=slug,
                name=slug,
                working_dir=root,
                memory_dir=root / "memory",
                persona=root / "persona.md",
                default=(slug == "devops"),
                allowed_roles=frozenset({"owner"}),
            )
        )
    return ClaudeSDKManager(
        _settings(**overrides), profile_registry=ProfileRegistry(profiles)
    )


def test_budget_for_profile_is_fresh_per_turn_and_scoped(tmp_path: Path) -> None:
    mgr = _manager(tmp_path)
    first = mgr._verification_budget_for(tmp_path / "coach")
    second = mgr._verification_budget_for(tmp_path / "coach")
    assert first is not None and second is not None and first is not second
    assert (first.phase, first.limit, first.enforce) == (PHASE_DRAFT, 1, False)
    assert mgr._verification_budget_for(tmp_path / "devops") is None


def test_budget_for_follows_phase_and_config(tmp_path: Path) -> None:
    mgr = _manager(
        tmp_path, verification_budget_enforce=True, verification_budget_confirm_max=2
    )
    with verification_phase(PHASE_CONFIRM):
        budget = mgr._verification_budget_for(tmp_path / "coach")
    assert budget is not None
    assert (budget.phase, budget.limit, budget.enforce) == (PHASE_CONFIRM, 2, True)


# --- follow-up hook: _run_verification_check ----------------------------------


def _orchestrator(monkeypatch, **overrides) -> MessageOrchestrator:  # type: ignore[no-untyped-def]
    settings = dict(verification_check_enabled=True)
    settings.update(overrides)
    o = MessageOrchestrator(_settings(**settings), {})
    monkeypatch.setattr(o, "_agent_id_for", lambda *a, **kw: "coach#u123")
    return o


async def test_check_noop_when_disabled(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    o = _orchestrator(monkeypatch, verification_check_enabled=False)
    storage, backend = _storage(), MagicMock(run_command=AsyncMock())
    state = _state(_request(), {"storage": storage, "claude_integration": backend})
    await o._run_verification_check(state, _response(LEG_PROGRAMME))
    backend.run_command.assert_not_awaited()


async def test_check_noop_when_grounded(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    o = _orchestrator(monkeypatch)
    storage, backend = _storage(), MagicMock(run_command=AsyncMock())
    state = _state(_request(), {"storage": storage, "claude_integration": backend})
    await o._run_verification_check(
        state, _response(CHEST_SESSION, tools_used=TURN_1817_TOOLS)
    )
    backend.run_command.assert_not_awaited()
    storage.log_bot_event.assert_not_awaited()


async def test_check_noop_for_other_profile(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    o = _orchestrator(monkeypatch)
    monkeypatch.setattr(o, "_agent_id_for", lambda *a, **kw: "chef")
    storage, backend = _storage(), MagicMock(run_command=AsyncMock())
    state = _state(_request(), {"storage": storage, "claude_integration": backend})
    await o._run_verification_check(state, _response(LEG_PROGRAMME))
    backend.run_command.assert_not_awaited()


async def test_confirm_agrees_ships_without_escalation(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    o = _orchestrator(monkeypatch)
    storage = _storage()
    phases: list[str] = []

    async def run_command(**kwargs):  # type: ignore[no-untyped-def]
        phases.append(current_phase())
        return _response(
            "Сверил с lift_history — цифры в норме.",
            tools_used=("mcp__user_data__lift_history",),
        )

    backend = MagicMock(run_command=AsyncMock(side_effect=run_command))
    bus = MagicMock(publish=AsyncMock())
    state = _state(
        _request(),
        {"storage": storage, "claude_integration": backend, "event_bus": bus},
    )
    await o._run_verification_check(state, _response(LEG_PROGRAMME))

    assert phases == [PHASE_CONFIRM]
    assert current_phase() == PHASE_DRAFT
    assert backend.run_command.await_args.kwargs["prompt"] == confirm_prompt(3)
    assert _event_types(storage) == [
        "verification_confirm_fired",
        "verification_confirm_done",
    ]
    done = storage.log_bot_event.call_args_list[1].args[2]
    assert done["contradiction"] is None and done["escalate_tools_used"] == []
    bus.publish.assert_awaited_once()
    assert bus.publish.await_args.args[0].text.startswith(CONFIRM_LABEL)


async def test_contradiction_escalates_exactly_once(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    o = _orchestrator(monkeypatch)
    storage = _storage()
    phases: list[str] = []
    replies = iter(
        [
            _response(
                f"{CONTRADICTION_MARKER} присед 100 кг — в истории рабочий 80 кг",
                tools_used=("mcp__user_data__lift_history",),
            ),
            # Even if the escalate reply also flags something, no 4th turn.
            _response(f"{CONTRADICTION_MARKER} ещё что-то", tools_used=("WebFetch",)),
        ]
    )

    async def run_command(**kwargs):  # type: ignore[no-untyped-def]
        phases.append(current_phase())
        return next(replies)

    backend = MagicMock(run_command=AsyncMock(side_effect=run_command))
    bus = MagicMock(publish=AsyncMock())
    state = _state(
        _request(),
        {"storage": storage, "claude_integration": backend, "event_bus": bus},
    )
    await o._run_verification_check(state, _response(LEG_PROGRAMME))

    assert phases == [PHASE_CONFIRM, PHASE_ESCALATE]
    assert "присед 100 кг" in backend.run_command.await_args.kwargs["prompt"]
    done = storage.log_bot_event.call_args_list[-1].args[2]
    assert done["contradiction"].startswith("присед 100 кг")
    assert done["escalate_tools_used"] == ["WebFetch"]
    texts = [c.args[0].text for c in bus.publish.await_args_list]
    assert texts[0].startswith(CONFIRM_LABEL) and texts[1].startswith(ESCALATE_LABEL)


# --- quality_daily counters: both tails ----------------------------------------


@pytest.fixture
def coach_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE sessions (session_id TEXT, user_id INTEGER,
            project_path TEXT);
        CREATE TABLE messages (message_id INTEGER, session_id TEXT,
            user_id INTEGER, timestamp TEXT, prompt TEXT, response TEXT);
        CREATE TABLE tool_calls (id INTEGER, session_id TEXT,
            message_id INTEGER, tool_name TEXT, started_at TEXT,
            parent_tool_use_id TEXT);
        """
    )
    conn.execute("INSERT INTO sessions VALUES ('s', 7, '/w/workspaces/coach')")
    ts = "2026-09-22T10:00:00+00:00"
    conn.executemany(
        "INSERT INTO messages VALUES (?,?,?,?,?,?)",
        [
            (1817, "s", 7, ts, "грудь", CHEST_SESSION),
            (1700, "s", 7, ts, "ноги", LEG_PROGRAMME),
            (1701, "s", 7, ts, "привет", "Привет!"),
            (1702, "s", 7, ts, "сколько белка", "Около 1.6 г/кг."),
        ],
    )
    rows = [(1817, name) for name in TURN_1817_TOOLS] + [(1702, "WebSearch")]
    conn.executemany(
        "INSERT INTO tool_calls (id, session_id, message_id, tool_name, started_at)"
        " VALUES (?,?,?,?,?)",
        [(i, "s", mid, name, ts) for i, (mid, name) in enumerate(rows)],
    )
    return conn


def test_quality_counts_both_tails(coach_db: sqlite3.Connection) -> None:
    (row,) = compute_quality_daily(coach_db)
    names = (
        "verification_calls",
        "verified_turns",
        "deep_research_turns",
        "program_turns",
        "program_turns_unchecked",
    )
    counters = dict(zip(names, row[-5:]))
    assert counters == {
        "verification_calls": 21,  # 20 (turn 1817) + 1 (cheap check, 1702)
        "verified_turns": 2,
        "deep_research_turns": 1,  # only 1817
        "program_turns": 2,
        "program_turns_unchecked": 1,  # only the dv-7a3c leg programme
    }
