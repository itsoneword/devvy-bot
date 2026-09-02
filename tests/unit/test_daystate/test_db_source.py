"""DbTodayStateSource against a real (temporary) data.db (dv-df45)."""

from pathlib import Path

import pytest

from src.daystate.db_source import DbTodayStateSource
from src.storage.user_data import UserDataManager, UserDataRepository

USER_A = 111111111
USER_B = 222222222
TODAY = "2026-08-14"  # a Friday

PROGRAM = """## Недельный сплит

| День | Сессия |
|---|---|
| Пт | Тяга |
| Сб | Отдых |
"""


@pytest.fixture
async def source(tmp_path: Path):
    db = UserDataManager(str(tmp_path / "data.db"))
    await db.initialize()
    repo = UserDataRepository(db)
    yield DbTodayStateSource(repo), repo
    await db.close()


class TestDbTodayStateSource:
    async def test_assembles_todays_state_from_every_domain(self, source) -> None:
        src, repo = source
        await repo.log_food(
            USER_A, TODAY, "завтрак", kcal=500, protein=30, time="09:00"
        )
        await repo.log_food(USER_A, TODAY, "обед", kcal=740, protein=48, time="13:10")
        await repo.log_sleep(USER_A, TODAY, sleep_min=420, weight_kg=82.4)
        await repo.log_train(USER_A, TODAY, "присед", session="Ноги", set_n="1")
        await repo.log_train(USER_A, "2026-08-12", "жим", session="Грудь", set_n="1")
        await repo.log_activity(USER_A, TODAY, "walk", "measured", kcal_active=150)
        await repo.log_activity(USER_A, TODAY, "padel", "measured", kcal_active=243)
        await repo.issue_open(USER_A, "Спазм шеи")
        await repo.doc_save(USER_A, "program", PROGRAM, title="program.md")

        state = await src.get_today_state(USER_A, TODAY)

        assert state.food_meals == 2
        assert state.food_kcal == 1240
        assert state.food_protein == 78
        assert state.last_meal == "обед"
        assert state.last_meal_time == "13:10"
        assert state.sleep["sleep_min"] == 420
        assert state.weight_kg == 82.4
        assert state.train_sets_today == 1
        assert state.train_session_today == "Ноги"
        assert state.last_train_date == "2026-08-12"
        assert state.activity_count_today == 2
        assert state.activity_types_today == ["walk", "padel"]
        assert state.activity_kcal_today == 393
        assert state.planned_today == "Тяга"
        assert state.planned_tomorrow == "Отдых"
        assert state.active_restrictions == ["Спазм шеи"]

    async def test_chronic_restrictions_are_excluded(self, source) -> None:
        # Owner decision 4: chronic conditions belong in the window bundle,
        # not in every turn.
        src, repo = source
        await repo.log_food(USER_A, TODAY, "обед", kcal=500)
        await repo.issue_open(USER_A, "Спазм шеи")
        await repo.issue_open(USER_A, "Колено (артроз)", status="CHRONIC")
        await repo.issue_open(USER_A, "Правое плечо", status="RESOLVED")

        state = await src.get_today_state(USER_A, TODAY)

        assert state.active_restrictions == ["Спазм шеи"]

    async def test_empty_day_for_a_tracking_user_still_renders(self, source) -> None:
        # The absence itself is the signal — this is the "did I eat today?"
        # case the whole feature exists for.
        src, repo = source
        await repo.log_sleep(USER_A, "2026-08-10", weight_kg=82.0)

        state = await src.get_today_state(USER_A, TODAY)

        assert state is not None
        assert state.food_meals == 0
        assert state.weight_kg == 82.0

    async def test_activity_only_day_still_tracks(self, source) -> None:
        # An activity-only day (no food/sleep/train) must still surface —
        # this is exactly the dv-63df gap: rings with no other log that day.
        src, repo = source
        await repo.log_activity(USER_A, TODAY, "walk", "user_reported", duration_min=30)

        state = await src.get_today_state(USER_A, TODAY)

        assert state is not None
        assert state.activity_count_today == 1
        assert state.activity_kcal_today is None  # no kcal_active reported

    async def test_user_with_no_history_gets_no_digest(self, source) -> None:
        src, _ = source
        assert await src.get_today_state(USER_A, TODAY) is None

    async def test_never_mixes_two_users(self, source) -> None:
        src, repo = source
        await repo.log_food(USER_B, TODAY, "ужин", kcal=900, protein=50)
        await repo.issue_open(USER_B, "Спазм шеи")
        await repo.log_food(USER_A, TODAY, "обед", kcal=500, protein=20)

        state = await src.get_today_state(USER_A, TODAY)

        # Health data crossing users would be the worst failure this feature
        # could have.
        assert state.food_kcal == 500
        assert state.active_restrictions == []

    async def test_has_entries_today_reflects_any_domain(self, source) -> None:
        src, repo = source
        assert await src.has_entries_today(USER_A, TODAY) is False
        await repo.log_sleep(USER_A, TODAY, sleep_min=400)
        assert await src.has_entries_today(USER_A, TODAY) is True

    async def test_unparseable_date_drops_program_but_keeps_the_rest(
        self, source
    ) -> None:
        src, repo = source
        await repo.log_food(USER_A, "not-a-date", "обед", kcal=500)
        await repo.doc_save(USER_A, "program", PROGRAM, title="program.md")

        state = await src.get_today_state(USER_A, "not-a-date")

        assert state.food_kcal == 500
        assert state.planned_today is None
