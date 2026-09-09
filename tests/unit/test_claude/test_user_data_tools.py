"""Tests for the data.db read-lane MCP tools (dv-5f3a step 4): capability
mounting, injected user_id, and isolation via server context."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Dict
from unittest.mock import AsyncMock

import pytest

from src.claude import agent_tools
from src.claude.agent_tools import AgentServices, build_agent_mcp_server
from src.claude.user_data_tools import (
    build_user_data_read_tools,
    build_user_data_write_tools,
)
from src.events.types import AgentResponseEvent
from src.profiles.registry import ProfileDefinition, ProfileRegistry
from src.storage.user_data import UserDataManager, UserDataRepository

USER_A = 111111111
USER_B = 222222222

DATA_TOOLS = {
    "day_summary",
    "sleep_last",
    "lift_history",
    "week_summary",
    "product_find",
    "portion_find",
}


@pytest.fixture
async def repo(tmp_path: Path):
    db = UserDataManager(str(tmp_path / "data.db"))
    await db.initialize()
    repo = UserDataRepository(db)
    await repo.log_food(USER_A, "2026-08-15", "обед", kcal=650)
    await repo.log_food(USER_B, "2026-08-15", "ужин", kcal=400)
    await repo.save_portion(USER_A, "нормальная пицца", 350)
    await repo.save_portion(USER_B, "нормальная пицца", 150)
    await repo.save_product(
        "творог 2%", "product", "label_photo", brand="Milbona", kcal_100g=98
    )
    yield repo
    await db.close()


def _profile(slug: str, groups: frozenset) -> ProfileDefinition:
    return ProfileDefinition(
        slug=slug,
        name=slug,
        working_dir=Path(f"/tmp/{slug}"),
        memory_dir=Path(f"/tmp/{slug}/memory"),
        persona=Path(f"/tmp/{slug}/persona.md"),
        default=(slug == "devops"),
        user_data_tools=groups,
    )


@pytest.fixture
def registry() -> ProfileRegistry:
    return ProfileRegistry(
        [
            _profile("devops", frozenset()),
            _profile("coach", frozenset({"logs", "products", "portions"})),
            _profile("chef", frozenset({"products"})),
        ]
    )


@pytest.fixture
def captured_tools(monkeypatch: pytest.MonkeyPatch) -> Dict[str, Any]:
    captured: Dict[str, Any] = {}

    def fake_create(name: str, version: str, tools: list) -> Dict[str, str]:
        captured.update({t.name: t for t in tools})
        return {"name": name}

    monkeypatch.setattr(agent_tools, "create_sdk_mcp_server", fake_create)
    return captured


def _services(repo, registry) -> AgentServices:
    return AgentServices(
        event_bus=AsyncMock(),
        task_repo=AsyncMock(),
        profile_registry=registry,
        user_data=repo,
    )


def _build(services, captured, slug: str, user_id=None) -> Dict[str, Any]:
    captured.clear()
    build_agent_mcp_server(services, slug, Path("/tmp/ws"), user_id=user_id)
    return captured


def _payload(result: Dict[str, Any]) -> Any:
    return json.loads(result["content"][0]["text"])


class TestMounting:
    async def test_mounted_by_capability_config_not_slug(
        self, repo, registry, captured_tools
    ) -> None:
        services = _services(repo, registry)
        coach = _build(services, captured_tools, "coach", user_id=USER_A)
        assert DATA_TOOLS <= set(coach)
        chef = _build(services, captured_tools, "chef", user_id=USER_A)
        assert "product_find" in chef
        assert "day_summary" not in chef and "portion_find" not in chef
        devops = _build(services, captured_tools, "devops", user_id=USER_A)
        assert not (DATA_TOOLS & set(devops))

    async def test_per_user_slug_resolves_base_profile(
        self, repo, registry, captured_tools
    ) -> None:
        services = _services(repo, registry)
        tools = _build(services, captured_tools, f"coach#u{USER_A}", user_id=USER_A)
        assert DATA_TOOLS <= set(tools)

    async def test_not_mounted_without_user_id(
        self, repo, registry, captured_tools
    ) -> None:
        services = _services(repo, registry)
        tools = _build(services, captured_tools, "coach", user_id=None)
        assert not (DATA_TOOLS & set(tools))

    async def test_not_mounted_without_repo(
        self, repo, registry, captured_tools
    ) -> None:
        services = _services(None, registry)
        tools = _build(services, captured_tools, "coach", user_id=USER_A)
        assert not (DATA_TOOLS & set(tools))

    async def test_no_tool_takes_a_user_or_scope_parameter(
        self, repo, registry, captured_tools
    ) -> None:
        services = _services(repo, registry)
        tools = _build(services, captured_tools, "coach", user_id=USER_A)
        for name in DATA_TOOLS:
            schema_keys = set(tools[name].input_schema)
            assert not schema_keys & {"user_id", "user", "scope", "agent_id"}, name


class TestIsolation:
    async def test_injected_user_id_wins_over_crafted_args(self, repo) -> None:
        """A model-supplied user_id argument is ignored, not honored."""
        (day_summary, *_rest) = build_user_data_read_tools(
            repo, USER_A, frozenset({"logs"})
        )
        result = await day_summary.handler({"date": "2026-08-15", "user_id": USER_B})
        payload = _payload(result)
        assert [f["meal"] for f in payload["food"]] == ["обед"]

    async def test_portions_are_per_user(self, repo) -> None:
        tools = build_user_data_read_tools(repo, USER_B, frozenset({"portions"}))
        result = await tools[0].handler({"phrase": "пицца"})
        payload = _payload(result)
        assert [p["grams"] for p in payload] == [150]

    async def test_products_shared_with_provenance(self, repo) -> None:
        tools = build_user_data_read_tools(repo, USER_A, frozenset({"products"}))
        result = await tools[0].handler({"query": "творог"})
        payload = _payload(result)
        assert payload[0]["provenance"] == "label_photo"


class TestExport:
    """Lane 2 — the universal read path: exports only the caller's rows as
    files under the working directory."""

    @pytest.fixture
    async def loaded_repo(self, repo):
        await repo.log_food(USER_A, "2026-08-01", "завтрак", kcal=500)
        await repo.log_sleep(USER_A, "2026-08-15", sleep_min=420)
        await repo.log_sleep(USER_B, "2026-08-15", sleep_min=380)
        await repo.issue_open(USER_A, "плечо")
        await repo.doc_save(USER_A, "note", "август", period="2026-08")
        return repo

    def _export_tool(self, repo, workdir: Path):
        tools = build_user_data_read_tools(
            repo, USER_A, frozenset({"export"}), working_directory=workdir
        )
        assert [t.name for t in tools] == ["export_my_data"]
        return tools[0]

    async def test_exports_only_callers_rows(self, loaded_repo, tmp_path) -> None:
        workdir = tmp_path / "ws"
        export = self._export_tool(loaded_repo, workdir)
        result = await export.handler({"domain": "all", "user_id": USER_B})
        report = _payload(result)
        # food: A has 2 rows total; B's ужин must not leak
        assert report["food"]["rows"] == 2
        food_csv = (workdir / "exports" / "food.csv").read_text(encoding="utf-8")
        assert "обед" in food_csv and "завтрак" in food_csv
        assert "ужин" not in food_csv
        assert "user_id" not in food_csv.splitlines()[0]
        # sleep: only A's row
        assert report["sleep"]["rows"] == 1
        # products are shared by design
        assert report["products"]["rows"] == 1
        # docs land as markdown files
        doc_path = Path(report["docs"]["paths"][0])
        assert doc_path.read_text(encoding="utf-8") == "август"

    async def test_date_window_bounds_dated_domains(
        self, loaded_repo, tmp_path
    ) -> None:
        export = self._export_tool(loaded_repo, tmp_path / "ws")
        result = await export.handler({"domain": "food", "date_from": "2026-08-10"})
        assert _payload(result)["food"]["rows"] == 1  # only the 15th

    async def test_unknown_domain_is_an_error(self, loaded_repo, tmp_path) -> None:
        export = self._export_tool(loaded_repo, tmp_path / "ws")
        result = await export.handler({"domain": "memory_facts"})
        assert result.get("is_error") is True

    async def test_not_mounted_without_working_directory(self, repo) -> None:
        tools = build_user_data_read_tools(
            repo, USER_A, frozenset({"export"}), working_directory=None
        )
        assert tools == []

    async def test_empty_domain_writes_header_only(self, repo, tmp_path) -> None:
        workdir = tmp_path / "ws"
        export = self._export_tool(repo, workdir)
        result = await export.handler({"domain": "train"})
        assert _payload(result)["train"]["rows"] == 0


class TestWriteTools:
    """Lane 1 (dv-5f3a step 5): user-scoped writes returning aggregates."""

    def _tools(self, repo, groups={"logs", "products", "portions"}):
        built = build_user_data_write_tools(repo, USER_A, frozenset(groups))
        return {t.name: t for t in built}

    async def test_write_tools_mount_by_group(self, repo) -> None:
        assert set(self._tools(repo)) == {
            "log_food",
            "food_edit",
            "log_sleep",
            "sleep_edit",
            "log_train",
            "log_activity",
            "train_edit",
            "activity_edit",
            "log_delete",
            "product_save",
            "product_delete",
            "portion_save",
            "portion_delete",
        }
        assert set(self._tools(repo, {"products"})) == {
            "product_save",
            "product_delete",
        }
        assert set(self._tools(repo, {"portions"})) == {
            "portion_save",
            "portion_delete",
        }

    async def test_log_food_returns_day_totals(self, repo) -> None:
        tools = self._tools(repo)
        result = await tools["log_food"].handler(
            {
                "date": "2026-08-15",
                "items": [{"meal": "ужин", "kcal": 350}],
                "user_id": USER_B,
            }
        )
        payload = _payload(result)
        # crafted user_id ignored: totals include USER_A's existing обед (650)
        assert payload["day"]["meals"] == 2
        assert payload["day"]["kcal"] == 1000

    async def test_log_food_empty_number_stays_null(self, repo) -> None:
        tools = self._tools(repo)
        result = await tools["log_food"].handler(
            {"date": "2026-08-16", "items": [{"meal": "перекус", "kcal": ""}]}
        )
        assert _payload(result)["day"]["kcal"] is None

    async def test_log_sleep_upserts_provided_fields(self, repo) -> None:
        tools = self._tools(repo)
        await tools["log_sleep"].handler(
            {"nights": [{"date": "2026-08-15", "weight_kg": 84.3}]}
        )
        result = await tools["log_sleep"].handler(
            {"nights": [{"date": "2026-08-15", "sleep_min": 420}]}
        )
        row = _payload(result)["nights"][0]["row"]
        assert row["weight_kg"] == 84.3 and row["sleep_min"] == 420

    async def test_log_train_returns_previous_session(self, repo) -> None:
        tools = self._tools(repo)
        await tools["log_train"].handler(
            {
                "date": "2026-08-08",
                "sets": [{"exercise": "bench", "weight_kg": "70", "reps": "8"}],
            }
        )
        result = await tools["log_train"].handler(
            {
                "date": "2026-08-15",
                "sets": [{"exercise": "bench", "weight_kg": "72.5", "reps": "10-12"}],
            }
        )
        payload = _payload(result)
        assert payload["previous_session"]["bench"]["date"] == "2026-08-08"
        assert payload["today"]["bench"][0]["reps"] == "10-12"

    async def test_product_save_provenance_required_and_guarded(self, repo) -> None:
        tools = self._tools(repo)
        missing = await tools["product_save"].handler(
            {"products": [{"name": "сыр", "kind": "product"}]}
        )
        assert missing.get("is_error") is True
        refused = await tools["product_save"].handler(
            {
                "products": [
                    {
                        "name": "творог 2%",
                        "brand": "Milbona",
                        "kind": "product",
                        "provenance": "estimate",
                        "kcal_100g": 120,
                    }
                ]
            }
        )
        payload = _payload(refused)["results"][0]
        assert payload["saved"] is False
        assert payload["row"]["kcal_100g"] == 98

    async def test_portion_save_is_user_scoped(self, repo) -> None:
        tools = self._tools(repo, {"portions"})
        await tools["portion_save"].handler(
            {
                "portions": [{"phrase": "тарелка супа", "grams": 300}],
                "user_id": USER_B,
            }
        )
        mine = await repo.portion_find(USER_A, "супа")
        theirs = await repo.portion_find(USER_B, "супа")
        assert [p["grams"] for p in mine] == [300]
        assert theirs == []

    async def test_no_write_tool_takes_user_param(self, repo) -> None:
        for name, t in self._tools(repo).items():
            assert not set(t.input_schema) & {"user_id", "user", "scope"}, name

    async def test_log_activity_accepts_ring_fields(self, repo) -> None:
        result = await self._tools(repo)["log_activity"].handler(
            {
                # today: the retroactive backfill_reason gate is dv-c388's
                # test, not this one's — keep the two orthogonal.
                "date": datetime.now(UTC).date().isoformat(),
                "activities": [
                    {
                        "type": "walk",
                        "source": "measured",
                        "kcal_active": 393,
                        "exercise_min": 24,
                        "stand_h": 8,
                        "steps": 8266,
                        "distance_km": 5.4,
                        "kcal_total": 2400,
                    }
                ],
            }
        )
        row = _payload(result)["day"][0]
        assert row["kcal_active"] == 393
        assert row["steps"] == 8266
        assert row["distance_km"] == 5.4
        assert row["kcal_total"] == 2400

    async def test_activity_edit_fills_and_clears_ring_fields(self, repo) -> None:
        tools = self._tools(repo)
        logged = await tools["log_activity"].handler(
            {
                "date": datetime.now(UTC).date().isoformat(),
                "activities": [
                    {"type": "walk", "source": "measured", "kcal_active": 393}
                ],
            }
        )
        entry_id = _payload(logged)["ids"][0]

        edited = await tools["activity_edit"].handler(
            {"edits": [{"entry_id": entry_id, "steps": 8266}]}
        )
        assert _payload(edited)["rows"][0]["steps"] == 8266

        cleared = await tools["activity_edit"].handler(
            {"edits": [{"entry_id": entry_id, "clear": "kcal_active"}]}
        )
        assert _payload(cleared)["rows"][0]["kcal_active"] is None


class TestValidation:
    async def test_day_summary_rejects_bad_date(self, repo) -> None:
        (day_summary, *_rest) = build_user_data_read_tools(
            repo, USER_A, frozenset({"logs"})
        )
        result = await day_summary.handler({"date": "today"})
        assert result.get("is_error") is True

    async def test_lift_history_requires_exercise(self, repo) -> None:
        tools = build_user_data_read_tools(repo, USER_A, frozenset({"logs"}))
        lift = next(t for t in tools if t.name == "lift_history")
        result = await lift.handler({})
        assert result.get("is_error") is True


class TestOptionalParams:
    """dv-9679: the SDK's dict-shorthand schema marks EVERY key required
    (``claude_agent_sdk/__init__.py``: ``"required": list(properties)``), so
    a partial write was impossible — the model had to fail the call or
    invent the fields it wasn't told. Every schema here is explicit instead.
    """

    def _all_tools(self, repo) -> Dict[str, Any]:
        built = build_user_data_read_tools(
            repo, USER_A, frozenset({"logs", "products", "portions"})
        ) + build_user_data_write_tools(
            repo, USER_A, frozenset({"logs", "products", "portions"})
        )
        return {t.name: t for t in built}

    def test_schemas_are_explicit_not_shorthand(self, repo) -> None:
        """Shorthand dicts are the bug; the SDK only honours `required` when
        the schema carries a string `type` and `properties`."""
        for name, t in self._all_tools(repo).items():
            schema = t.input_schema
            assert isinstance(schema.get("type"), str), name
            assert "properties" in schema, name
            assert "required" in schema, name

    def test_required_is_a_subset_of_properties(self, repo) -> None:
        for name, t in self._all_tools(repo).items():
            schema = t.input_schema
            extra = set(schema["required"]) - set(schema["properties"])
            assert not extra, f"{name} requires undeclared params: {extra}"

    def test_batch_item_schemas_are_explicit_too(self, repo) -> None:
        """dv-b042: the dv-9679 rule has to hold one level down.

        Batching made the row an object inside an array, and a row schema
        that marks every field required is the same trap at batch scale: the
        model must then supply an `rpe`/`hrv`/`kcal` for EVERY row or fail
        the call, and what it supplies is a 0. A 0 is not "unknown" — it
        enters AVG() and corrupts the averages, which is exactly the
        empty-stays-empty rule (dv-3668) being broken by the schema rather
        than by the handler.
        """
        # log_delete's row is pure addressing — {domain, entry_id}, both
        # looked up from day_summary, neither a value that could be invented.
        # Every other row carries measurements and must be partially writable.
        addressing_only = {"log_delete"}
        for name, t in self._all_tools(repo).items():
            for key, prop in t.input_schema["properties"].items():
                if prop.get("type") != "array":
                    continue
                items = prop.get("items", {})
                assert items.get("type") == "object", f"{name}.{key}"
                assert "properties" in items, f"{name}.{key}"
                assert "required" in items, f"{name}.{key}"
                extra = set(items["required"]) - set(items["properties"])
                assert not extra, f"{name}.{key} requires undeclared: {extra}"
                if name in addressing_only:
                    continue
                optional = set(items["properties"]) - set(items["required"])
                assert optional, (
                    f"{name}.{key} marks every row field required — the model "
                    "would have to invent the ones it wasn't told"
                )
                # No measurement is ever required: numbers are the fields a
                # model fabricates, and a fabricated 0 poisons the averages.
                numeric = {
                    f
                    for f, spec in items["properties"].items()
                    if spec.get("type") == "number"
                }
                forced = numeric & set(items["required"])
                # portion_save's grams is the row's whole point and is asked
                # for, never inferred; nothing else may force a number.
                assert forced <= {"grams"}, f"{name}.{key} forces numbers: {forced}"

    def test_log_sleep_requires_only_date_per_night(self, repo) -> None:
        """The reported case: alcohol/THC alone must be writable."""
        schema = self._all_tools(repo)["log_sleep"].input_schema
        assert schema["required"] == ["nights"]
        assert schema["properties"]["nights"]["items"]["required"] == ["date"]

    async def test_log_sleep_accepts_a_two_field_write(self, repo) -> None:
        tools = self._all_tools(repo)
        result = await tools["log_sleep"].handler(
            {"nights": [{"date": "2026-08-18", "alcohol_u": 1, "thc": "1"}]}
        )
        assert result.get("is_error") is not True
        row = _payload(result)["nights"][0]["row"]
        assert row["alcohol_u"] == 1
        assert row["thc"] == "1"
        # untouched fields stay empty rather than becoming 0 (dv-adea)
        assert row["sleep_min"] is None
        assert row["hrv"] is None

    def test_optional_write_params_are_not_required(self, repo) -> None:
        """A plain walk must be loggable without inventing ring numbers."""
        schema = self._all_tools(repo)["log_activity"].input_schema
        assert set(schema["required"]) == {"date", "activities"}
        item = schema["properties"]["activities"]["items"]
        assert set(item["required"]) == {"type", "source"}
        for ring in ("steps", "distance_km", "kcal_active"):
            assert ring not in item["required"]
        assert "backfill_reason" not in schema["required"]


class TestFoodEditTool:
    """Correcting a logged meal in place (2026-08-17). The agent used to be
    forced into negative 'КОРРЕКЦИЯ' rows because no edit path existed."""

    def _tools(self, repo, groups=("logs",)):
        built = build_user_data_write_tools(repo, USER_A, frozenset(groups))
        return {t.name: t for t in built}

    async def test_edits_own_entry_and_returns_new_day_totals(self, repo) -> None:
        summary = await repo.day_summary(USER_A, "2026-08-15")
        entry_id = summary["food"][0]["id"]

        result = await self._tools(repo)["food_edit"].handler(
            {"edits": [{"entry_id": entry_id, "kcal": 400}]}
        )
        payload = _payload(result)

        assert payload["rows"][0]["kcal"] == 400
        assert payload["days"]["2026-08-15"]["kcal"] == 400

    async def test_cannot_edit_another_users_entry(self, repo) -> None:
        theirs = await repo.day_summary(USER_B, "2026-08-15")
        entry_id = theirs["food"][0]["id"]

        result = await self._tools(repo)["food_edit"].handler(
            {"edits": [{"entry_id": entry_id, "kcal": 1}], "user_id": USER_B}
        )

        # user_id in args is ignored (server-context rule) and the id belongs
        # to someone else, so this must fail rather than mutate their log.
        assert result.get("is_error")
        after = await repo.day_summary(USER_B, "2026-08-15")
        assert after["food"][0]["kcal"] == 400

    async def test_missing_entry_id_is_rejected(self, repo) -> None:
        result = await self._tools(repo)["food_edit"].handler(
            {"edits": [{"kcal": 100}]}
        )
        assert result.get("is_error")


class TestSleepEditTool:
    """dv-4e81: log_sleep's COALESCE upsert cannot write NULL, so before this
    tool a wrong sleep number could be replaced but never taken back — and
    the domain most likely to hold invented zeros was the one with no way to
    remove them."""

    def _tools(self, repo, groups=("logs",)):
        built = build_user_data_write_tools(repo, USER_A, frozenset(groups))
        return {t.name: t for t in built}

    async def test_clear_blanks_a_field_log_sleep_cannot(self, repo) -> None:
        tools = self._tools(repo)
        await tools["log_sleep"].handler(
            {"nights": [{"date": "2026-08-15", "weight_kg": 82.4, "hrv": 0}]}
        )

        # log_sleep alone can never blank it — the omitted field is kept.
        kept = await tools["log_sleep"].handler(
            {"nights": [{"date": "2026-08-15", "weight_kg": 82.4}]}
        )
        assert _payload(kept)["nights"][0]["row"]["hrv"] == 0

        result = await tools["sleep_edit"].handler(
            {"edits": [{"date": "2026-08-15", "clear": "hrv"}]}
        )
        row = _payload(result)["nights"][0]["row"]
        assert row["hrv"] is None
        assert row["weight_kg"] == 82.4

    async def test_addresses_by_date_and_needs_no_entry_id(self, repo) -> None:
        schema = self._tools(repo)["sleep_edit"].input_schema
        item = schema["properties"]["edits"]["items"]
        assert item["required"] == ["date"]
        assert "entry_id" not in item["properties"]

    async def test_a_date_with_no_night_is_rejected(self, repo) -> None:
        result = await self._tools(repo)["sleep_edit"].handler(
            {"edits": [{"date": "2026-08-15", "score": 80}]}
        )
        assert result.get("is_error")
        assert await repo.sleep_last(USER_A) == []

    async def test_cannot_edit_another_users_night(self, repo) -> None:
        await repo.log_sleep(USER_B, "2026-08-15", hrv=44)

        result = await self._tools(repo)["sleep_edit"].handler(
            {"edits": [{"date": "2026-08-15", "clear": "hrv"}], "user_id": USER_B}
        )

        assert result.get("is_error")
        assert (await repo.sleep_last(USER_B))[0]["hrv"] == 44

    async def test_bad_date_rejects_the_whole_batch(self, repo) -> None:
        await repo.log_sleep(USER_A, "2026-08-15", hrv=0)

        result = await self._tools(repo)["sleep_edit"].handler(
            {
                "edits": [
                    {"date": "2026-08-15", "clear": "hrv"},
                    {"date": "2026-08-16", "score": 80},
                ]
            }
        )

        assert result.get("is_error")
        assert "NOTHING WAS WRITTEN" in result["content"][0]["text"]
        assert (await repo.sleep_last(USER_A))[0]["hrv"] == 0

    async def test_malformed_date_is_rejected(self, repo) -> None:
        result = await self._tools(repo)["sleep_edit"].handler(
            {"edits": [{"date": "вчера", "clear": "hrv"}]}
        )
        assert result.get("is_error")


class TestPortionDeleteTool:
    """dv-4e81: a phrase saved by mistake used to be permanent."""

    def _tools(self, repo):
        built = build_user_data_write_tools(repo, USER_A, frozenset({"portions"}))
        return {t.name: t for t in built}

    async def test_deletes_own_phrase(self, repo) -> None:
        mine = (await repo.portion_find(USER_A, "пицца"))[0]

        result = await self._tools(repo)["portion_delete"].handler(
            {"portion_id": mine["id"]}
        )

        assert _payload(result)["deleted"] is True
        assert await repo.portion_find(USER_A, "пицца") == []

    async def test_cannot_delete_another_users_phrase(self, repo) -> None:
        theirs = (await repo.portion_find(USER_B, "пицца"))[0]

        result = await self._tools(repo)["portion_delete"].handler(
            {"portion_id": theirs["id"], "user_id": USER_B}
        )

        assert result.get("is_error")
        assert len(await repo.portion_find(USER_B, "пицца")) == 1

    async def test_missing_id_is_rejected(self, repo) -> None:
        result = await self._tools(repo)["portion_delete"].handler({})
        assert result.get("is_error")


class TestLogActivityGuardrails:
    """dv-c388: coach fabricated 5 activity rows and wrote 5 more when the
    user pushed back. This replays the guardrail acceptance scenario — the
    tool must demand a reason for a retroactive write, week_summary must
    exclude the estimate, and the owner must be informed automatically."""

    def _tools(self, repo, event_bus=None, profile_slug: str = "coach"):
        built = build_user_data_write_tools(
            repo,
            USER_A,
            frozenset({"logs"}),
            event_bus=event_bus,
            profile_slug=profile_slug,
        )
        return {t.name: t for t in built}

    def _today(self) -> str:
        return datetime.now(UTC).date().isoformat()

    def _days_ago(self, n: int) -> str:
        return (datetime.now(UTC).date() - timedelta(days=n)).isoformat()

    async def test_source_omitted_is_rejected(self, repo) -> None:
        result = await self._tools(repo)["log_activity"].handler(
            {
                "date": self._today(),
                "activities": [{"type": "walk", "duration_min": 30}],
            }
        )
        assert result.get("is_error") is True

    async def test_invalid_source_is_rejected(self, repo) -> None:
        result = await self._tools(repo)["log_activity"].handler(
            {
                "date": self._today(),
                "activities": [{"type": "walk", "source": "guessed"}],
            }
        )
        assert result.get("is_error") is True

    async def test_unlabelled_row_rejects_whole_batch(self, repo) -> None:
        """dv-b042 + dv-c388: batching must not let a row without a source
        ride in behind a labelled one. The good row is rejected too, and
        NOTHING reaches the table — a partially-written batch would leave the
        model believing all three landed."""
        today = self._today()
        result = await self._tools(repo)["log_activity"].handler(
            {
                "date": today,
                "activities": [
                    {"type": "walk", "source": "measured", "steps": 8000},
                    {"type": "padel", "duration_min": 60},  # no source
                    {"type": "climb", "source": "user_reported"},
                ],
            }
        )
        assert result.get("is_error") is True
        assert "activities[1]" in result["content"][0]["text"]
        assert "NOTHING WAS WRITTEN" in result["content"][0]["text"]
        summary = await repo.day_summary(USER_A, today)
        assert summary["activity"] == []

    async def test_retroactive_write_without_reason_is_refused(self, repo) -> None:
        target_date = self._days_ago(3)
        result = await self._tools(repo)["log_activity"].handler(
            {
                "date": target_date,
                "activities": [
                    {"type": "walk", "source": "estimated", "duration_min": 60}
                ],
            }
        )
        assert result.get("is_error") is True
        summary = await repo.day_summary(USER_A, target_date)
        assert summary["activity"] == []  # nothing written

    async def test_retroactive_write_with_reason_succeeds(self, repo) -> None:
        target_date = self._days_ago(3)
        result = await self._tools(repo)["log_activity"].handler(
            {
                "date": target_date,
                "activities": [
                    {"type": "walk", "source": "estimated", "duration_min": 60}
                ],
                "backfill_reason": "user asked for a rough estimate of a gap day",
            }
        )
        payload = _payload(result)
        assert payload["day"][0]["source"] == "estimated"

    async def test_yesterday_is_not_retroactive(self, repo) -> None:
        result = await self._tools(repo)["log_activity"].handler(
            {
                "date": self._days_ago(1),
                "activities": [
                    {"type": "walk", "source": "estimated", "duration_min": 20}
                ],
            }
        )
        assert result.get("is_error") is not True

    async def test_retroactive_estimate_informs_owner(self, repo) -> None:
        event_bus = AsyncMock()
        target_date = self._days_ago(5)
        await self._tools(repo, event_bus=event_bus)["log_activity"].handler(
            {
                "date": target_date,
                "activities": [
                    {"type": "walk", "source": "estimated", "duration_min": 60}
                ],
                "backfill_reason": "no data for that day, user requested a guess",
            }
        )
        event_bus.publish.assert_awaited_once()
        published = event_bus.publish.await_args.args[0]
        assert isinstance(published, AgentResponseEvent)
        assert published.chat_id == 0
        assert "estimated backfill" in published.text
        assert target_date in published.text

    async def test_one_estimated_row_in_a_batch_still_pages_the_owner(
        self, repo
    ) -> None:
        """dv-b042: batching must not dilute the dv-c388 alarm. A batch that
        is mostly measured but carries one estimate still pages the owner,
        and the message names the estimated activity, not the batch."""
        event_bus = AsyncMock()
        target_date = self._days_ago(5)
        await self._tools(repo, event_bus=event_bus)["log_activity"].handler(
            {
                "date": target_date,
                "activities": [
                    {"type": "run", "source": "measured", "duration_min": 40},
                    {"type": "walk", "source": "estimated", "duration_min": 60},
                ],
                "backfill_reason": "watch export plus one guessed gap",
            }
        )
        event_bus.publish.assert_awaited_once()
        published = event_bus.publish.await_args.args[0]
        assert "estimated backfill" in published.text
        assert "walk" in published.text
        assert "run" not in published.text

    async def test_retroactive_measured_does_not_inform_owner(self, repo) -> None:
        event_bus = AsyncMock()
        target_date = self._days_ago(5)
        result = await self._tools(repo, event_bus=event_bus)["log_activity"].handler(
            {
                "date": target_date,
                "activities": [
                    {"type": "run", "source": "measured", "duration_min": 40}
                ],
                "backfill_reason": "importing an old Watch export",
            }
        )
        assert result.get("is_error") is not True
        event_bus.publish.assert_not_awaited()

    async def test_week_summary_excludes_estimated_and_reports_it(self, repo) -> None:
        await repo.log_food(USER_A, "2026-08-15", "обед", kcal=1)
        await repo.log_activity(
            USER_A, "2026-08-14", "padel", "measured", duration_min=60
        )
        await repo.log_activity(
            USER_A, "2026-08-15", "walk", "estimated", duration_min=45
        )

        tools = build_user_data_read_tools(repo, USER_A, frozenset({"logs"}))
        week_summary = next(t for t in tools if t.name == "week_summary")
        payload = _payload(await week_summary.handler({}))

        by_date = {d["date"]: d["duration_min"] for d in payload["activity"]}
        assert "2026-08-15" not in by_date
        assert by_date["2026-08-14"] == 60
        excluded = {d["date"]: d for d in payload["activity_estimated_excluded"]}
        assert excluded["2026-08-15"]["count"] == 1


class TestBatchWrites:
    """dv-b042: every write tool takes an array, so a whole workout or a
    multi-part meal is ONE round-trip instead of eleven, and the grounding
    payload is returned once per exercise instead of once per set."""

    def _tools(self, repo, groups=("logs", "products", "portions")):
        built = build_user_data_write_tools(repo, USER_A, frozenset(groups))
        return {t.name: t for t in built}

    # The real 2026-08-21 `pull_chest` session from the ticket: 16 sets over
    # 5 exercises, which cost 16 sequential tool calls before this change.
    PULL_CHEST = (
        [{"exercise": "Жим штанги на наклонной 45", "set_n": str(n)} for n in range(4)]
        + [
            {"exercise": "Разводка гантелей на наклонной 30", "set_n": str(n)}
            for n in range(3)
        ]
        + [{"exercise": "Тяга в тренажёре лёжа", "set_n": str(n)} for n in range(3)]
        + [{"exercise": "Фейс-пулл", "set_n": "1"}]
        + [{"exercise": "Пулловер", "set_n": str(n)} for n in range(5)]
    )

    async def test_whole_session_is_one_call(self, repo) -> None:
        result = await self._tools(repo)["log_train"].handler(
            {"date": "2026-08-21", "session": "pull_chest", "sets": self.PULL_CHEST}
        )
        payload = _payload(result)
        assert payload["written"] == 16
        assert len(payload["ids"]) == 16
        summary = await repo.day_summary(USER_A, "2026-08-21")
        assert len(summary["train"]) == 16
        # session comes from the batch level, so every row carries it
        assert {r["session"] for r in summary["train"]} == {"pull_chest"}

    async def test_previous_session_appears_once_per_exercise(self, repo) -> None:
        """The acceptance criterion. Before: call 4 for an exercise
        re-returned sets 1-3 AND the previous session again — quadratic
        within an exercise. Now each exercise's grounding block appears
        exactly once no matter how many sets it has."""
        await self._tools(repo)["log_train"].handler(
            {
                "date": "2026-08-14",
                "session": "pull_chest",
                "sets": [{"exercise": "Фейс-пулл", "weight_kg": "20"}],
            }
        )
        payload = _payload(
            await self._tools(repo)["log_train"].handler(
                {"date": "2026-08-21", "session": "pull_chest", "sets": self.PULL_CHEST}
            )
        )

        # 16 sets, 5 distinct exercises -> 5 blocks, not 16.
        assert len(payload["previous_session"]) == 5
        assert len(payload["today"]) == 5
        assert payload["previous_session"]["Фейс-пулл"]["date"] == "2026-08-14"
        assert payload["previous_session"]["Фейс-пулл"]["sets"][0]["weight_kg"] == "20"
        # An exercise with no history is present but empty — never invented.
        assert payload["previous_session"]["Пулловер"] == {"date": None, "sets": []}
        # ...and the 4-set exercise still lists all 4 under one key.
        assert len(payload["today"]["Жим штанги на наклонной 45"]) == 4

    async def test_bad_row_rejects_whole_batch_with_its_index(self, repo) -> None:
        """Atomicity: a bad row names its index and nothing is written, so
        the model resends the batch rather than guessing which half landed."""
        result = await self._tools(repo)["log_train"].handler(
            {
                "date": "2026-08-21",
                "sets": [
                    {"exercise": "bench", "reps": "8"},
                    {"reps": "10"},  # no exercise
                    {"exercise": "row", "reps": "12"},
                ],
            }
        )
        assert result.get("is_error") is True
        text = result["content"][0]["text"]
        assert "sets[1]" in text
        assert "NOTHING WAS WRITTEN" in text
        summary = await repo.day_summary(USER_A, "2026-08-21")
        assert summary["train"] == []

    async def test_food_batch_returns_day_totals_once(self, repo) -> None:
        result = await self._tools(repo)["log_food"].handler(
            {
                "date": "2026-08-19",
                "items": [
                    {"meal": "курица", "kcal": 300, "protein": 50},
                    {"meal": "рис", "kcal": 200, "carb": 45},
                    {"meal": "салат", "kcal": 100},
                ],
            }
        )
        payload = _payload(result)
        assert payload["written"] == 3
        # One settled total, not three running subtotals.
        assert payload["day"]["kcal"] == 600
        assert payload["day"]["meals"] == 3

    async def test_omitted_numbers_stay_null_across_a_batch(self, repo) -> None:
        """The owner's constraint: a field nobody reported must be NULL for
        that row, not 0 — a 0 enters AVG() and corrupts every later average,
        while NULL is skipped. Batching must not quietly fill the gaps."""
        await self._tools(repo)["log_sleep"].handler(
            {
                "nights": [
                    {"date": "2026-08-19", "sleep_min": 400, "hrv": 60},
                    {"date": "2026-08-20", "sleep_min": 380},  # no hrv measured
                    {"date": "2026-08-21", "sleep_min": 420, "hrv": 64},
                ]
            }
        )
        rows = {r["date"]: r for r in await repo.sleep_last(USER_A, 7)}
        assert rows["2026-08-20"]["hrv"] is None
        assert rows["2026-08-20"]["score"] is None
        # The average is over the two real readings, not dragged down by a 0.
        assert (rows["2026-08-19"]["hrv"] + rows["2026-08-21"]["hrv"]) / 2 == 62

    async def test_batch_delete_is_all_or_nothing(self, repo) -> None:
        tools = self._tools(repo)
        logged = _payload(
            await tools["log_train"].handler(
                {
                    "date": "2026-08-21",
                    "sets": [
                        {"exercise": "bench", "reps": "8"},
                        {"exercise": "bench", "reps": "8"},
                    ],
                }
            )
        )
        ids = logged["ids"]

        refused = await tools["log_delete"].handler(
            {
                "entries": [
                    {"domain": "train", "entry_id": ids[0]},
                    {"domain": "train", "entry_id": 999999},  # not this user's
                ]
            }
        )
        assert refused.get("is_error") is True
        summary = await repo.day_summary(USER_A, "2026-08-21")
        assert len(summary["train"]) == 2  # the good id survived the refusal

        ok = await tools["log_delete"].handler(
            {"entries": [{"domain": "train", "entry_id": i} for i in ids]}
        )
        assert _payload(ok)["deleted"] == 2
        summary = await repo.day_summary(USER_A, "2026-08-21")
        assert summary["train"] == []

    async def test_empty_array_is_an_error_not_a_silent_success(self, repo) -> None:
        """A write tool reporting success having written nothing is how
        «записал» gets said about an empty log."""
        for name, args in (
            ("log_train", {"date": "2026-08-21", "sets": []}),
            ("log_food", {"date": "2026-08-21", "items": []}),
            ("log_sleep", {"nights": []}),
            ("log_activity", {"date": "2026-08-21", "activities": []}),
        ):
            result = await self._tools(repo)[name].handler(args)
            assert result.get("is_error") is True, name

    async def test_products_batch_saves_the_good_rows_when_one_is_refused(
        self, repo
    ) -> None:
        """Unlike the log tools, products are NOT all-or-nothing: the
        provenance guard refusing one estimate must not throw away the other
        products read off the same label photo."""
        result = await self._tools(repo)["product_save"].handler(
            {
                "products": [
                    {
                        "name": "хлеб",
                        "kind": "product",
                        "provenance": "label_photo",
                        "kcal_100g": 250,
                    },
                    {
                        # dv-5f3a guard: estimate can't overwrite label data
                        "name": "творог 2%",
                        "brand": "Milbona",
                        "kind": "product",
                        "provenance": "estimate",
                        "kcal_100g": 120,
                    },
                ]
            }
        )
        payload = _payload(result)
        assert payload["written"] == 1
        assert payload["refused"] == 1
        assert [p["kcal_100g"] for p in await repo.product_find("хлеб")] == [250]
        # the authoritative row is untouched and handed back
        assert payload["results"][1]["row"]["kcal_100g"] == 98
