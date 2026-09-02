"""Editing an already-logged meal in place (2026-08-17).

Before this existed the agent could only append a negative "КОРРЕКЦИЯ" row,
which kept day totals right but destroyed the log as a record of meals.
"""

from pathlib import Path

import pytest

from src.storage.user_data import UserDataManager, UserDataRepository

USER_A = 111111111
USER_B = 222222222
DAY = "2026-08-17"


@pytest.fixture
async def repo(tmp_path: Path):
    db = UserDataManager(str(tmp_path / "data.db"))
    await db.initialize()
    yield UserDataRepository(db)
    await db.close()


class TestEditFood:
    async def test_corrects_the_row_and_recomputes_the_day(self, repo) -> None:
        logged = await repo.log_food(USER_A, DAY, "блинчики", kcal=550, protein=20)
        await repo.log_food(USER_A, DAY, "обед", kcal=750)

        result = await repo.edit_food(USER_A, logged["id"], kcal=250)

        assert result["edited"] is True
        assert result["row"]["kcal"] == 250
        # The whole point: the day total reflects the correction with no
        # second row in the log.
        assert result["day"]["kcal"] == 1000
        assert result["day"]["meals"] == 2

    async def test_omitted_fields_are_kept_not_erased(self, repo) -> None:
        logged = await repo.log_food(
            USER_A, DAY, "блинчики", kcal=550, protein=20, source="text"
        )

        result = await repo.edit_food(USER_A, logged["id"], kcal=250)

        assert result["row"]["protein"] == 20
        assert result["row"]["source"] == "text"
        assert result["row"]["meal"] == "блинчики"

    async def test_can_rename_the_meal(self, repo) -> None:
        logged = await repo.log_food(USER_A, DAY, "перекус", kcal=100)

        result = await repo.edit_food(
            USER_A, logged["id"], meal="перекус (орехи 30г)", kcal=180
        )

        assert result["row"]["meal"] == "перекус (орехи 30г)"
        assert result["row"]["kcal"] == 180

    async def test_another_users_entry_is_untouchable(self, repo) -> None:
        theirs = await repo.log_food(USER_B, DAY, "ужин", kcal=800)

        result = await repo.edit_food(USER_A, theirs["id"], kcal=1)

        # Structural isolation: the id exists, but not for this caller.
        assert result["edited"] is False
        summary = await repo.day_summary(USER_B, DAY)
        assert summary["food"][0]["kcal"] == 800

    async def test_unknown_id_is_a_no_op(self, repo) -> None:
        result = await repo.edit_food(USER_A, 999999, kcal=100)
        assert result["edited"] is False
        assert "no such entry" in result["reason"]

    async def test_edit_does_not_touch_other_rows_of_the_same_day(self, repo) -> None:
        first = await repo.log_food(USER_A, DAY, "завтрак", kcal=400)
        await repo.log_food(USER_A, DAY, "обед", kcal=600)

        await repo.edit_food(USER_A, first["id"], kcal=300)

        summary = await repo.day_summary(USER_A, DAY)
        assert [r["kcal"] for r in summary["food"]] == [300, 600]

    async def test_updated_at_moves_but_the_row_keeps_its_id_and_date(
        self, repo
    ) -> None:
        logged = await repo.log_food(USER_A, DAY, "ужин", kcal=500)

        result = await repo.edit_food(USER_A, logged["id"], kcal=450)

        assert result["row"]["id"] == logged["id"]
        assert result["row"]["date"] == DAY
        assert result["row"]["updated_at"] >= result["row"]["created_at"]


class TestClearFields:
    async def test_clear_blanks_a_value_coalesce_cannot(self, repo) -> None:
        logged = await repo.log_food(USER_A, DAY, "обед", kcal=500, protein=30)

        # COALESCE keeps old values, so clearing needs its own path — this is
        # how the agent takes back a number it invented.
        result = await repo.edit_food(USER_A, logged["id"], clear=["protein"])

        assert result["row"]["protein"] is None
        assert result["row"]["kcal"] == 500

    async def test_clear_and_set_in_one_call(self, repo) -> None:
        logged = await repo.log_food(USER_A, DAY, "обед", kcal=500, fat=20, carb=40)

        result = await repo.edit_food(
            USER_A, logged["id"], kcal=450, clear=["fat", "carb"]
        )

        assert result["row"]["kcal"] == 450
        assert result["row"]["fat"] is None
        assert result["row"]["carb"] is None

    async def test_unknown_field_name_clears_nothing(self, repo) -> None:
        logged = await repo.log_food(USER_A, DAY, "обед", kcal=500, protein=30)

        result = await repo.edit_food(
            USER_A, logged["id"], clear=["kcalories", "date", "user_id"]
        )

        # A model typo must never widen into clearing a different column.
        assert result["row"]["kcal"] == 500
        assert result["row"]["protein"] == 30
        assert result["row"]["date"] == DAY


class TestDeleteLogEntry:
    async def test_deletes_own_row_and_recomputes_the_day(self, repo) -> None:
        keep = await repo.log_food(USER_A, DAY, "завтрак", kcal=400)
        drop = await repo.log_food(USER_A, DAY, "дубль", kcal=400)

        result = await repo.delete_log_entry(USER_A, "food", drop["id"])

        assert result["deleted"] is True
        assert result["row"]["meal"] == "дубль"
        assert result["day"]["kcal"] == 400
        summary = await repo.day_summary(USER_A, DAY)
        assert [r["id"] for r in summary["food"]] == [keep["id"]]

    async def test_cannot_delete_another_users_row(self, repo) -> None:
        theirs = await repo.log_food(USER_B, DAY, "ужин", kcal=800)

        result = await repo.delete_log_entry(USER_A, "food", theirs["id"])

        assert result["deleted"] is False
        summary = await repo.day_summary(USER_B, DAY)
        assert len(summary["food"]) == 1

    async def test_deletes_across_every_domain(self, repo) -> None:
        await repo.log_sleep(USER_A, DAY, sleep_min=420)
        await repo.log_train(USER_A, DAY, "присед", session="Ноги", set_n="1")
        await repo.log_activity(
            USER_A, DAY, "прогулка", "user_reported", duration_min=30
        )
        summary = await repo.day_summary(USER_A, DAY)

        for domain, key in (("train", "train"), ("activity", "activity")):
            row_id = summary[key][0]["id"]
            assert (await repo.delete_log_entry(USER_A, domain, row_id))["deleted"]
        sleep_id = summary["sleep"]["id"]
        assert (await repo.delete_log_entry(USER_A, "sleep", sleep_id))["deleted"]

        after = await repo.day_summary(USER_A, DAY)
        assert after["train"] == [] and after["activity"] == []
        assert after["sleep"] is None

    async def test_unknown_domain_is_refused(self, repo) -> None:
        result = await repo.delete_log_entry(USER_A, "products", 1)
        assert result["deleted"] is False
        assert "unknown domain" in result["reason"]

    async def test_unknown_id_is_a_no_op(self, repo) -> None:
        result = await repo.delete_log_entry(USER_A, "food", 999999)
        assert result["deleted"] is False


class TestEditTrainAndActivity:
    async def test_edit_train_corrects_the_set(self, repo) -> None:
        await repo.log_train(
            USER_A, DAY, "присед", session="Ноги", set_n="1", weight_kg="80", reps="5"
        )
        summary = await repo.day_summary(USER_A, DAY)
        entry_id = summary["train"][0]["id"]

        result = await repo.edit_train(USER_A, entry_id, weight_kg="85", reps="8-10")

        assert result["row"]["weight_kg"] == "85"
        assert result["row"]["reps"] == "8-10"
        assert result["row"]["exercise"] == "присед"

    async def test_edit_activity_corrects_duration(self, repo) -> None:
        await repo.log_activity(
            USER_A, DAY, "прогулка", "user_reported", duration_min=30
        )
        summary = await repo.day_summary(USER_A, DAY)
        entry_id = summary["activity"][0]["id"]

        result = await repo.edit_activity(USER_A, entry_id, duration_min=75)

        assert result["row"]["duration_min"] == 75
        assert result["row"]["type"] == "прогулка"

    async def test_edit_train_of_another_user_is_refused(self, repo) -> None:
        await repo.log_train(
            USER_B, DAY, "жим", session="Грудь", set_n="1", weight_kg="60"
        )
        theirs = await repo.day_summary(USER_B, DAY)

        result = await repo.edit_train(USER_A, theirs["train"][0]["id"], weight_kg="1")

        assert result["edited"] is False


class TestDeleteProduct:
    async def test_removes_a_shared_product(self, repo) -> None:
        saved = await repo.save_product("тестовый творог", "product", "estimate")
        product_id = saved["row"]["id"]

        result = await repo.delete_product(product_id)

        assert result["deleted"] is True
        assert await repo.product_find("тестовый творог") == []

    async def test_unknown_product_is_a_no_op(self, repo) -> None:
        result = await repo.delete_product(999999)
        assert result["deleted"] is False
