"""Detector tests for the save-honesty check (dv-7783)."""

import pytest

from src.bot.orchestration.save_honesty import (
    WRITE_TOOLS,
    check_turn,
    detect_write_claim,
    satisfied_write_tool,
)


class TestDetectWriteClaim:
    @pytest.mark.parametrize(
        "text",
        [
            "Записал: обед — гречка с курицей.",
            "Все записано в дневник.",
            "Сохранил твои предпочтения.",
            "Запомнил! Рем важнее.",
            "Запомню на будущее.",
            "Зафиксировала вес: 82кг.",
            "Добавил в лог тренировку.",
            "Отметил в дневнике.",
            "I'll remember that you prefer morning workouts.",
            "I've saved your preference.",
            "I've logged the meal.",
            "Saved it to your food log.",
            "Your weight is recorded to your log — updated your memory.",
        ],
    )
    def test_save_claims_match(self, text):
        claim = detect_write_claim(text)
        assert claim is not None
        assert claim.inform is True

    @pytest.mark.parametrize(
        "text",
        [
            "Помню, ты говорил что не любишь орехи.",
            "Я помню твой график.",
            "I remember you said you dislike nuts.",
            "Do you remember when we discussed this?",
            "Remember to stretch before running!",
            "Насколько я помню, у тебя аллергия.",
        ],
    )
    def test_recall_statements_do_not_match(self, text):
        assert detect_write_claim(text) is None

    def test_msg_766_second_person_guard(self):
        """The real msg-766 shape: user quotes the bot and says the bot
        remembered wrong — «запомнил» about YOU (the counterpart), not a
        first-person claim."""
        assert detect_write_claim("…а ты запомнил обратное") is None
        assert detect_write_claim("вы записали неправильно") is None
        assert detect_write_claim("трекер записал 5км") is None

    def test_future_tense_is_measure_only(self):
        claim = detect_write_claim("Запишу вечером в дневник.")
        assert claim is not None
        assert claim.family == "future_ru"
        assert claim.inform is False

    def test_zapomnyu_is_a_hard_claim(self):
        """D13 exception: «запомню»/"I'll remember" counts as a real claim."""
        claim = detect_write_claim("Запомню!")
        assert claim is not None
        assert claim.inform is True

    def test_forget_claims_match(self):
        for text in [
            "Убрал из памяти этот факт.",
            "Удалил из памяти.",
            "I've forgotten that preference.",
            "Removed the old note from my memory.",
        ]:
            claim = detect_write_claim(text)
            assert claim is not None, text
            assert claim.family == "forget"

    def test_correction_claims_match(self):
        for text in [
            "Обновил запись о твоём весе.",
            "Updated your memory with the new address.",
        ]:
            assert detect_write_claim(text) is not None, text

    def test_empty_and_none(self):
        assert detect_write_claim(None) is None
        assert detect_write_claim("") is None

    def test_excerpt_bounded(self):
        text = "x" * 500 + " записал " + "y" * 500
        claim = detect_write_claim(text)
        assert claim is not None
        assert len(claim.excerpt) <= 210


class TestSatisfiedWriteTool:
    def test_prefers_specific_tool_over_bash(self):
        assert (
            satisfied_write_tool(["Bash", "mcp__agent__remember"])
            == "mcp__agent__remember"
        )

    def test_bash_alone_satisfies_distinctly(self):
        assert satisfied_write_tool(["Read", "Bash"]) == "Bash"

    def test_read_only_tools_do_not_satisfy(self):
        assert satisfied_write_tool(["Read", "Grep", "mcp__agent__recall"]) is None

    def test_audit_tools_are_write_ish_from_day_one(self):
        assert "mcp__agent__memory_forget" in WRITE_TOOLS
        assert "mcp__agent__memory_correct" in WRITE_TOOLS


class TestCheckTurn:
    def test_claim_with_write_is_honest(self):
        res = check_turn("Записал!", ["mcp__agent__remember"])
        assert res.mismatch_kind is None
        assert res.satisfied_by == "mcp__agent__remember"

    def test_claim_without_write_is_mismatch(self):
        res = check_turn("Записал: обед — суп.", [])
        assert res.mismatch_kind == "claim_no_write"
        assert res.satisfied_by is None

    def test_trigger_without_write_or_eval_is_mismatch(self):
        res = check_turn("Хорошо!", [], trigger_fired=True)
        assert res.mismatch_kind == "trigger_no_write"

    def test_trigger_with_forced_eval_is_satisfied(self):
        res = check_turn("Хорошо!", [], trigger_fired=True, force_eval_scheduled=True)
        assert res.mismatch_kind is None
        assert res.satisfied_by == "force_eval"

    def test_claim_plus_trigger_with_forced_eval_is_satisfied(self):
        res = check_turn("Записал!", [], trigger_fired=True, force_eval_scheduled=True)
        assert res.mismatch_kind is None
        assert res.satisfied_by == "force_eval"

    def test_measure_only_claim_never_mismatches(self):
        res = check_turn("Запишу вечером.", [])
        assert res.claim is not None
        assert res.mismatch_kind is None

    def test_no_claim_no_trigger_is_clean(self):
        res = check_turn("Вот рецепт супа.", [])
        assert res.claim is None
        assert res.mismatch_kind is None
        assert res.satisfied_by is None


class TestCoachDataToolsCoverage:
    """Regression for the 2026-08-19 drift: coach's dv-3668 write tools were
    absent from WRITE_TOOLS, so 16 of 27 recorded mismatches were honest
    turns whose write went through an MCP tool the detector didn't know."""

    def test_real_food_log_turn_is_satisfied(self):
        res = check_turn(
            "Записала под 18.08 (ужин), моцареллу сохранила в базу по этикетке.",
            ["Read", "mcp__agent__product_save", "mcp__agent__log_food"],
        )
        assert res.mismatch_kind is None
        assert res.satisfied_by == "mcp__agent__product_save"

    def test_every_user_data_write_tool_is_in_write_tools(self):
        """Any new lane-1 write tool must be added to WRITE_TOOLS, or every
        honest confirmation it backs becomes a claim_no_write alert."""
        from src.claude.user_data_tools import build_user_data_write_tools

        built = build_user_data_write_tools(
            object(), 1, frozenset({"logs", "products", "portions"})
        )
        missing = [t.name for t in built if f"mcp__agent__{t.name}" not in WRITE_TOOLS]
        assert not missing, f"write tools missing from WRITE_TOOLS: {missing}"
