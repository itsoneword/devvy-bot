"""Tests for the dv-5f3a per-user data store: schema, isolation, aggregates,
provenance guard, and the parameterized-SQL-only rule (owner decision 7)."""

import ast
import inspect
import re
from pathlib import Path

import pytest

import src.storage.user_data as user_data_module
from src.storage.schema.legacy import build_legacy
from src.storage.user_data import UserDataManager, UserDataRepository

USER_A = 111111111
USER_B = 222222222


@pytest.fixture
async def repo(tmp_path: Path):
    db = UserDataManager(str(tmp_path / "data.db"))
    await db.initialize()
    yield UserDataRepository(db)
    await db.close()


class TestNoDynamicSql:
    """Owner decision 7: all data.db access is parameterized — no f-string,
    concat, %, or .format() SQL anywhere in the module. Walk the AST and fail
    on any string-building expression that contains an SQL keyword."""

    SQL_KEYWORD = re.compile(
        r"\b(SELECT|INSERT|UPDATE|DELETE|CREATE|DROP|ALTER)\b", re.IGNORECASE
    )

    def _contains_sql(self, node: ast.AST) -> bool:
        for sub in ast.walk(node):
            if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                if self.SQL_KEYWORD.search(sub.value):
                    return True
        return False

    def test_no_string_built_sql(self) -> None:
        tree = ast.parse(inspect.getsource(user_data_module))
        offenders = []
        for node in ast.walk(tree):
            # f-strings containing SQL keywords
            if isinstance(node, ast.JoinedStr) and self._contains_sql(node):
                offenders.append((node.lineno, "f-string SQL"))
            # "..." % (...) and "...".format(...) with SQL keywords
            if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
                if self._contains_sql(node.left):
                    offenders.append((node.lineno, "percent-format SQL"))
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "format"
                and self._contains_sql(node.func.value)
            ):
                offenders.append((node.lineno, ".format() SQL"))
            # "SELECT ..." + variable  (either side non-constant)
            if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
                sides = [node.left, node.right]
                if any(self._contains_sql(s) for s in sides) and not all(
                    isinstance(s, (ast.Constant, ast.BinOp)) for s in sides
                ):
                    offenders.append((node.lineno, "concatenated SQL"))
        assert offenders == [], f"string-built SQL found: {offenders}"


class TestIsolation:
    """Every read for user A returns zero user B rows — the predicate the
    whole design exists to make testable."""

    async def test_no_cross_user_rows_anywhere(self, repo) -> None:
        await repo.log_food(USER_A, "2026-08-15", "обед", kcal=650, protein=40)
        await repo.log_food(USER_B, "2026-08-15", "ужин", kcal=400)
        await repo.log_sleep(USER_A, "2026-08-15", sleep_min=420, score=80)
        await repo.log_sleep(USER_B, "2026-08-15", sleep_min=380)
        await repo.log_train(USER_A, "2026-08-15", "bench", weight_kg="70", reps="8")
        await repo.log_train(USER_B, "2026-08-15", "bench", weight_kg="30", reps="12")
        await repo.log_activity(
            USER_A, "2026-08-15", "padel", "user_reported", duration_min=60
        )
        await repo.save_portion(USER_A, "нормальная пицца", 350)
        await repo.save_portion(USER_B, "нормальная пицца", 150)
        await repo.issue_open(USER_A, "right shoulder", symptom="top-of-raise")
        await repo.doc_save(USER_A, "note", "August notes", period="2026-08")

        b_day = await repo.day_summary(USER_B, "2026-08-15")
        assert [f["meal"] for f in b_day["food"]] == ["ужин"]
        assert b_day["sleep"]["sleep_min"] == 380
        assert all(t["weight_kg"] == "30" for t in b_day["train"])
        assert b_day["activity"] == []

        assert await repo.issue_list(USER_B) == []
        assert await repo.doc_list(USER_B) == []
        b_portions = await repo.portion_find(USER_B, "пицца")
        assert [p["grams"] for p in b_portions] == [150]
        assert [r["user_id"] for r in await repo.sleep_last(USER_B, 10)] == [USER_B]
        assert (await repo.lift_history(USER_B, "bench"))[0]["weight_kg"] == "30"
        totals = await repo.day_food_totals(USER_B, "2026-08-15")
        assert totals["kcal"] == 400 and totals["meals"] == 1

    async def test_issue_update_wrong_user_is_not_found(self, repo) -> None:
        issue = await repo.issue_open(USER_A, "right shoulder")
        assert await repo.issue_update(USER_B, issue["id"], status="RESOLVED") is None
        assert (await repo.issue_list(USER_A))[0]["status"] == "ACTIVE"

    async def test_doc_get_wrong_user_is_not_found(self, repo) -> None:
        doc = await repo.doc_save(USER_A, "program", "8-week chest/shoulders")
        assert await repo.doc_get(USER_B, doc["id"]) is None


class TestAggregates:
    async def test_day_totals_hand_computed(self, repo) -> None:
        await repo.log_food(USER_A, "2026-08-15", "завтрак", kcal=520, protein=30.5)
        await repo.log_food(USER_A, "2026-08-15", "обед", kcal=780, protein=45)
        # empty kcal stays NULL, never 0 — and is ignored by SUM
        result = await repo.log_food(USER_A, "2026-08-15", "перекус")
        assert result["day"]["meals"] == 3
        assert result["day"]["kcal"] == 1300
        assert result["day"]["protein"] == 75.5

    async def test_sleep_upsert_merges_provided_fields_only(self, repo) -> None:
        await repo.log_sleep(USER_A, "2026-08-15", weight_kg=84.3)
        result = await repo.log_sleep(
            USER_A, "2026-08-15", bed="00:30", wake="08:00", sleep_min=430
        )
        row = result["row"]
        assert row["weight_kg"] == 84.3  # morning value survived evening write
        assert row["bed"] == "00:30" and row["sleep_min"] == 430
        rows = await repo.sleep_last(USER_A, 10)
        assert len(rows) == 1  # upsert, not a second row

    async def test_sleep_7day_average(self, repo) -> None:
        for day, minutes in (("2026-08-13", 400), ("2026-08-14", 440)):
            await repo.log_sleep(USER_A, day, sleep_min=minutes)
        result = await repo.log_sleep(USER_A, "2026-08-15", sleep_min=420)
        assert result["avg_7d"]["sleep_min"] == 420  # (400+440+420)/3

    async def test_log_train_returns_previous_session(self, repo) -> None:
        await repo.log_train(USER_A, "2026-08-08", "bench", weight_kg="70", reps="8")
        await repo.log_train(USER_A, "2026-08-08", "bench", weight_kg="70", reps="7")
        result = await repo.log_train(
            USER_A, "2026-08-15", "bench", weight_kg="72.5", reps="8"
        )
        assert len(result["today"]) == 1
        assert result["previous_session"]["date"] == "2026-08-08"
        assert [s["reps"] for s in result["previous_session"]["sets"]] == ["8", "7"]

    async def test_reps_range_strings_survive(self, repo) -> None:
        """Real data has "10-12" — TEXT column, never fabricated numbers."""
        await repo.log_train(USER_A, "2026-08-15", "lat raise", reps="10-12")
        history = await repo.lift_history(USER_A, "lat raise")
        assert history[0]["reps"] == "10-12"


class TestActivityWatchFields:
    """Migration 3 (dv-63df): Apple Watch ring columns on logs_activity."""

    async def test_log_activity_round_trips_ring_fields(self, repo) -> None:
        result = await repo.log_activity(
            USER_A,
            "2026-08-15",
            "walk",
            "measured",
            kcal_active=393,
            kcal_total=2400,
            exercise_min=24,
            stand_h=8,
            steps=8266,
            distance_km=5.4,
        )
        row = result["day"][0]
        assert row["kcal_active"] == 393
        assert row["kcal_total"] == 2400
        assert row["exercise_min"] == 24
        assert row["stand_h"] == 8
        assert row["steps"] == 8266
        assert row["distance_km"] == 5.4

    async def test_log_activity_ring_fields_default_null(self, repo) -> None:
        result = await repo.log_activity(
            USER_A,
            "2026-08-15",
            "gym",
            "user_reported",
        )
        row = result["day"][0]
        assert row["kcal_active"] is None
        assert row["steps"] is None

    async def test_edit_activity_fills_ring_fields_in_place(self, repo) -> None:
        insert = await repo.log_activity(
            USER_A, "2026-08-15", "walk", "user_reported", duration_min=45
        )
        entry_id = insert["id"]

        result = await repo.edit_activity(
            USER_A, entry_id, kcal_active=393, exercise_min=24
        )

        row = result["row"]
        assert row["kcal_active"] == 393
        assert row["exercise_min"] == 24
        assert row["duration_min"] == 45  # untouched field survives

    async def test_edit_activity_clear_blanks_a_ring_field(self, repo) -> None:
        insert = await repo.log_activity(
            USER_A, "2026-08-15", "walk", "measured", kcal_active=393, steps=8266
        )
        entry_id = insert["id"]

        result = await repo.edit_activity(USER_A, entry_id, clear=["kcal_active"])

        row = result["row"]
        assert row["kcal_active"] is None
        assert row["steps"] == 8266  # not cleared

    async def test_week_summary_sums_kcal_active(self, repo) -> None:
        await repo.log_activity(
            USER_A, "2026-08-14", "walk", "measured", kcal_active=300
        )
        await repo.log_activity(
            USER_A, "2026-08-15", "walk", "measured", kcal_active=250
        )
        await repo.log_activity(
            USER_A, "2026-08-15", "padel", "measured", kcal_active=150
        )
        await repo.log_food(USER_A, "2026-08-15", "обед")  # anchors end_date

        result = await repo.week_summary(USER_A, end_date="2026-08-15")

        by_date = {d["date"]: d["kcal_active_total"] for d in result["activity"]}
        assert by_date["2026-08-14"] == 300
        assert by_date["2026-08-15"] == 400


class TestProducts:
    async def test_provenance_guard_estimate_never_overwrites(self, repo) -> None:
        await repo.save_product(
            "творог 2%", "product", "label_photo", brand="Milbona", kcal_100g=98
        )
        result = await repo.save_product(
            "творог 2%", "product", "estimate", brand="Milbona", kcal_100g=120
        )
        assert result["saved"] is False
        assert result["row"]["kcal_100g"] == 98
        assert result["row"]["provenance"] == "label_photo"

    async def test_label_photo_may_correct_estimate(self, repo) -> None:
        await repo.save_product("пицца дом.", "dish", "estimate", kcal_100g=250)
        result = await repo.save_product(
            "пицца дом.", "dish", "label_photo", kcal_100g=230
        )
        assert result["saved"] is True
        assert result["row"]["kcal_100g"] == 230
        assert result["row"]["provenance"] == "label_photo"

    async def test_products_shared_and_provenance_in_payload(self, repo) -> None:
        await repo.save_product(
            "творог 2%", "product", "label_photo", brand="Milbona", kcal_100g=98
        )
        found = await repo.product_find("творог")
        assert len(found) == 1
        assert found[0]["provenance"] == "label_photo"


class TestCyrillicCaseFolding:
    """SQLite folds case for ASCII only — ``lower()``, ``upper()`` and
    COLLATE NOCASE all leave Cyrillic untouched, and this build has no ICU.
    Every name in this store is Russian, so a case-sensitive lookup reads as
    "not in the base yet" and the agent re-estimates a product whose label it
    already photographed (2026-08-24: "Зефир есть в базе, моцарелла тоже").
    """

    async def test_find_ignores_case_in_cyrillic(self, repo) -> None:
        await repo.save_product("Зефир", "product", "label_photo", kcal_100g=318)
        for query in ("зефир", "Зефир", "ЗЕФИР", "ефи"):
            assert len(await repo.product_find(query)) == 1, query

    async def test_find_ignores_case_in_brand(self, repo) -> None:
        await repo.save_product(
            "рикотта", "product", "label_photo", brand="Galbani", kcal_100g=146
        )
        assert len(await repo.product_find("galbani")) == 1

    async def test_case_variant_updates_the_row_it_found(self, repo) -> None:
        """The write side of the same bug: ON CONFLICT(name, brand) is bound
        to a case-sensitive UNIQUE index, so a lowercase re-save used to
        insert a second row the provenance guard could not see past."""
        await repo.save_product("Моцарелла", "product", "label_photo", kcal_100g=280)
        result = await repo.save_product(
            "моцарелла", "product", "label_photo", kcal_100g=300
        )
        assert result["saved"] is True
        assert result["row"]["name"] == "Моцарелла"  # stored name wins
        assert result["row"]["kcal_100g"] == 300
        assert len(await repo.product_find("моцарелла")) == 1

    async def test_provenance_guard_holds_across_case(self, repo) -> None:
        await repo.save_product("Ветчина", "product", "label_photo", kcal_100g=88)
        result = await repo.save_product(
            "ветчина", "product", "estimate", kcal_100g=150
        )
        assert result["saved"] is False
        assert result["row"]["kcal_100g"] == 88

    async def test_portions_fold_case_too(self, repo) -> None:
        await repo.save_portion(USER_A, "Нормальная пицца", 300)
        row = await repo.save_portion(USER_A, "нормальная пицца", 350)
        assert row["grams"] == 350
        assert row["phrase"] == "Нормальная пицца"
        assert len(await repo.portion_find(USER_A, "ПИЦЦА")) == 1


class TestPortions:
    async def test_upsert_on_user_phrase(self, repo) -> None:
        await repo.save_portion(USER_A, "нормальная пицца", 300)
        row = await repo.save_portion(USER_A, "нормальная пицца", 350)
        assert row["grams"] == 350
        assert len(await repo.portion_find(USER_A, "пицца")) == 1


class TestActivitySource:
    """dv-c388: source is a required, enum-checked column on logs_activity —
    mirrors the products.provenance guard. week_summary must exclude
    'estimated' rows from the sum it hands the model, and say what it
    excluded, so an estimate can never silently harden into a TDEE number."""

    async def test_source_argument_is_mandatory(self, repo) -> None:
        with pytest.raises(TypeError):
            await repo.log_activity(USER_A, "2026-08-15", "walk")  # type: ignore[call-arg]

    async def test_null_source_is_rejected_by_the_column(self, repo) -> None:
        with pytest.raises(Exception):
            await repo.log_activity(USER_A, "2026-08-15", "walk", None)  # type: ignore[arg-type]

    async def test_invalid_source_is_rejected_by_the_check(self, repo) -> None:
        with pytest.raises(Exception):
            await repo.log_activity(USER_A, "2026-08-15", "walk", "guessed")

    async def test_week_summary_excludes_estimated_and_reports_it(self, repo) -> None:
        await repo.log_food(USER_A, "2026-08-15", "обед", kcal=1)
        await repo.log_activity(
            USER_A, "2026-08-14", "padel", "measured", duration_min=60
        )
        await repo.log_activity(
            USER_A, "2026-08-15", "walk", "estimated", duration_min=45
        )

        summary = await repo.week_summary(USER_A, end_date="2026-08-15")

        by_date = {d["date"]: d["duration_min"] for d in summary["activity"]}
        assert by_date == {"2026-08-14": 60}  # the estimated day never appears
        excluded = {d["date"]: d for d in summary["activity_estimated_excluded"]}
        assert excluded["2026-08-15"]["count"] == 1
        assert excluded["2026-08-15"]["duration_min"] == 45


class TestActivitySourceMigration:
    """Migration 3 (dv-c388): existing rows backfill to 'user_reported' (the
    honest claim, never excluded by the aggregate filter) and the rebuilt
    table enforces NOT NULL + CHECK for every write from here on."""

    async def test_pre_existing_row_backfills_and_check_is_enforced(
        self, tmp_path: Path
    ) -> None:
        db_path = str(tmp_path / "data.db")

        # A pre-Alembic data.db at legacy v2, then the current runner
        # bridges it (legacy 3..4 replayed, verified, stamped).
        build_legacy(db_path, "data", upto=2)
        legacy_db = UserDataManager(db_path)  # pool only, no initialize
        async with legacy_db.get_connection() as conn:
            await conn.execute(
                "INSERT INTO logs_activity (user_id, date, type, duration_min, "
                "intensity, imported_from, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (USER_A, "2026-08-01", "padel", 60.0, "high", None, "x", "x"),
            )
            await conn.commit()
        await legacy_db.close()

        upgraded_db = UserDataManager(db_path)
        await upgraded_db.initialize()
        repo = UserDataRepository(upgraded_db)
        try:
            day = await repo.day_summary(USER_A, "2026-08-01")
            assert day["activity"][0]["source"] == "user_reported"

            with pytest.raises(Exception):
                await repo.log_activity(USER_A, "2026-08-02", "walk", "guessed")
        finally:
            await upgraded_db.close()


class TestMigrationSupport:
    async def test_delete_imported_spares_live_rows(self, repo) -> None:
        await repo.log_food(
            USER_A, "2026-08-01", "обед", kcal=600, imported_from="data/food.csv"
        )
        await repo.log_food(USER_A, "2026-08-15", "обед", kcal=650)  # live row
        counts = await repo.count_imported(USER_A, "data/food.csv")
        assert counts["logs_food"] == 1
        deleted = await repo.delete_imported(USER_A, "data/food.csv")
        assert deleted == 1
        remaining = await repo.day_food_totals(USER_A, "2026-08-15")
        assert remaining["meals"] == 1  # live row untouched

    async def test_delete_imported_is_user_scoped(self, repo) -> None:
        await repo.log_food(
            USER_B, "2026-08-01", "ужин", kcal=500, imported_from="data/food.csv"
        )
        deleted = await repo.delete_imported(USER_A, "data/food.csv")
        assert deleted == 0
        assert (await repo.day_food_totals(USER_B, "2026-08-01"))["meals"] == 1


class TestDayStateReads:
    """The reads dv-df45's digest needs, including the isolation rule that
    matters most here: one user's digest must never show another's health data."""

    async def test_latest_weight_returns_most_recent_of_any_age(self, repo) -> None:
        await repo.log_sleep(USER_A, "2026-07-01", weight_kg=84.0)
        await repo.log_sleep(USER_A, "2026-08-14", weight_kg=82.4)
        await repo.log_sleep(USER_A, "2026-08-15")  # no weigh-in that day

        weight = await repo.latest_weight(USER_A)

        assert weight["weight_kg"] == 82.4
        assert weight["date"] == "2026-08-14"

    async def test_latest_weight_is_none_without_any_weigh_in(self, repo) -> None:
        await repo.log_sleep(USER_A, "2026-08-15", sleep_min=420)
        assert await repo.latest_weight(USER_A) is None

    async def test_latest_weight_never_crosses_users(self, repo) -> None:
        await repo.log_sleep(USER_B, "2026-08-15", weight_kg=66.1)
        assert await repo.latest_weight(USER_A) is None

    async def test_last_train_before_excludes_today(self, repo) -> None:
        await repo.log_train(USER_A, "2026-08-12", "жим", session="Грудь", set_n="1")
        await repo.log_train(USER_A, "2026-08-15", "присед", session="Ноги", set_n="1")

        previous = await repo.last_train_before(USER_A, "2026-08-15")

        assert previous["date"] == "2026-08-12"
        assert previous["session"] == "Грудь"

    async def test_last_train_before_collapses_a_day_to_one_row(self, repo) -> None:
        for set_n in ("1", "2", "3"):
            await repo.log_train(
                USER_A, "2026-08-12", "жим", session="Грудь", set_n=set_n
            )

        previous = await repo.last_train_before(USER_A, "2026-08-15")

        assert previous["sets"] == 3

    async def test_last_train_before_is_none_without_history(self, repo) -> None:
        assert await repo.last_train_before(USER_A, "2026-08-15") is None

    async def test_has_entries_on_detects_each_log_independently(self, repo) -> None:
        assert await repo.has_entries_on(USER_A, "2026-08-15") is False

        await repo.log_activity(
            USER_A, "2026-08-15", "прогулка", "user_reported", duration_min=30
        )

        # Activity alone counts: the gap-ping must not nag someone who logged
        # only a walk.
        assert await repo.has_entries_on(USER_A, "2026-08-15") is True

    async def test_has_entries_on_never_crosses_users(self, repo) -> None:
        await repo.log_food(USER_B, "2026-08-15", "ужин", kcal=500)
        assert await repo.has_entries_on(USER_A, "2026-08-15") is False
