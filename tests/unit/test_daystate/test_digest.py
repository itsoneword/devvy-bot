"""Digest rendering and program parsing (dv-df45)."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from src.daystate.digest import MAX_CHARS, render_digest
from src.daystate.program import parse_weekly_split, planned_for
from src.daystate.source import TodayState

LISBON = ZoneInfo("Europe/Lisbon")
NOW = datetime(2026, 8, 14, 14, 2, tzinfo=LISBON)  # Friday

PROGRAM = """# Программа

## Недельный сплит

| День | Сессия | Заметки |
|---|---|---|
| Пн | Ноги | тяжёлый день |
| Ср | Грудь | |
| Пт | Тяга: спина / плечи / руки | лёгкая |
| Вс | Скалодром | |
| Вт, Чт, Сб | Отдых | падел |

## Жёсткие ограничения
текст после таблицы
"""


class TestProgramParsing:
    def test_maps_every_weekday_including_multi_day_rows(self) -> None:
        mapping = parse_weekly_split(PROGRAM)
        assert mapping["Пн"] == "Ноги"
        assert mapping["Ср"] == "Грудь"
        assert mapping["Вс"] == "Скалодром"
        # One row listing three days must populate all three.
        for day in ("Вт", "Чт", "Сб"):
            assert mapping[day] == "Отдых"

    def test_prose_after_the_table_is_not_parsed_as_rows(self) -> None:
        mapping = parse_weekly_split(PROGRAM)
        assert set(mapping) == {"Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"}

    def test_missing_or_malformed_input_yields_no_mapping(self) -> None:
        # A wrong value is worse than a missing line, so every failure mode
        # must degrade to "no mapping" rather than a guess.
        assert parse_weekly_split(None) == {}
        assert parse_weekly_split("") == {}
        assert parse_weekly_split("# Программа\n\nбез таблицы") == {}
        assert parse_weekly_split("## Недельный сплит\n\nтекст, не таблица") == {}

    def test_planned_for_wraps_at_week_end(self) -> None:
        mapping = parse_weekly_split(PROGRAM)
        # Sunday (index 6) + 1 must wrap to Monday, not fall off the list.
        assert planned_for(mapping, 7) == "Ноги"
        assert planned_for({}, 0) is None


class TestDigestRendering:
    def _full_state(self) -> TodayState:
        return TodayState(
            date="2026-08-14",
            food_meals=3,
            food_kcal=1240.0,
            food_protein=78.0,
            last_meal="обед",
            last_meal_time="13:10",
            sleep={"sleep_min": 420.0, "score": 78.0, "hrv": 45.0, "rhr": 52.0},
            weight_kg=82.4,
            weight_date="2026-08-13",
            train_sets_today=5,
            train_session_today="Ноги",
            last_train_date="2026-08-12",
            last_train_session="Грудь",
            activity_count_today=2,
            activity_types_today=["walk", "padel"],
            activity_kcal_today=393.0,
            planned_today="Тяга",
            planned_tomorrow="Отдых",
            active_restrictions=["Спазм шеи"],
        )

    def test_full_day_renders_every_line(self) -> None:
        block = render_digest(self._full_state(), NOW)
        assert block.startswith('<today_state as_of="2026-08-14 14:02')
        assert block.endswith("</today_state>")
        assert "3 приёма, 1240 ккал, 78 г белка" in block
        assert "(последний: обед 13:10)" in block
        assert "420 мин, score 78, HRV 45, RHR 52" in block
        assert "82.4 кг (2026-08-13)" in block
        assert "5 подходов (Ноги)" in block
        assert "последняя до сегодня: 2026-08-12 Грудь" in block
        assert "Активность сегодня: 2 (walk, padel), 393 ккал актив." in block
        assert "сегодня Тяга; завтра Отдых" in block
        assert "Спазм шеи" in block

    def test_empty_day_says_so_rather_than_omitting(self) -> None:
        # "nothing logged" must be distinguishable from "not covered".
        block = render_digest(TodayState(date="2026-08-14"), NOW)
        assert "Еда сегодня: ничего не записано" in block
        assert "Сон (ночь на сегодня): не записан" in block
        assert "Тренировка сегодня: не записана" in block
        assert "Активность сегодня: не записана" in block

    def test_partial_sleep_shows_only_recorded_fields(self) -> None:
        state = TodayState(date="2026-08-14", sleep={"sleep_min": 400.0})
        block = render_digest(state, NOW)
        assert "400 мин" in block
        assert "HRV" not in block

    def test_activity_without_kcal_omits_the_kcal_clause(self) -> None:
        state = TodayState(date="2026-08-14", activity_count_today=1)
        block = render_digest(state, NOW)
        assert "Активность сегодня: 1" in block
        assert "ккал актив." not in block

    def test_absent_optional_blocks_are_omitted(self) -> None:
        block = render_digest(TodayState(date="2026-08-14"), NOW)
        assert "Вес:" not in block
        assert "По программе:" not in block
        assert "Активные ограничения" not in block

    def test_supersedes_note_is_present(self) -> None:
        # Old digests stay in scrollback; the block must claim precedence.
        block = render_digest(self._full_state(), NOW)
        assert "Supersedes" in block

    def test_no_state_renders_nothing(self) -> None:
        assert render_digest(None, NOW) == ""

    def test_block_is_capped_and_stays_closed(self) -> None:
        state = self._full_state()
        state.active_restrictions = [f"ограничение {i} " * 12 for i in range(40)]
        block = render_digest(state, NOW)
        assert len(block) <= MAX_CHARS
        # Truncation must never drop the closing tag — it would swallow the
        # user's actual message into the block.
        assert block.endswith("</today_state>")

    def test_integral_numbers_render_without_decimal_noise(self) -> None:
        block = render_digest(self._full_state(), NOW)
        assert "1240 ккал" in block
        assert "1240.0" not in block

    @pytest.mark.parametrize(
        "meals,expected",
        [
            (1, "1 приём"),
            (2, "2 приёма"),
            (4, "4 приёма"),
            (5, "5 приёмов"),
            (11, "11 приёмов"),
            (21, "21 приём"),
            (22, "22 приёма"),
        ],
    )
    def test_russian_count_agreement(self, meals: int, expected: str) -> None:
        state = TodayState(date="2026-08-14", food_meals=meals, food_kcal=100.0)
        assert expected in render_digest(state, NOW)
