"""Family items & reminders (dv-752a phase A): service, fire path, injection."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, List
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.events.bus import EventBus
from src.events.handlers import AgentHandler
from src.events.types import AgentResponseEvent, ScheduledEvent
from src.items.loop_guard import automated_turn, is_automated
from src.items.repository import ItemRepository
from src.items.service import ItemError, ItemService
from src.profiles.registry import ProfileDefinition, ProfileRegistry
from src.scheduler.scheduler import JobScheduler, build_cron_trigger
from src.storage.database import DatabaseManager

HUSBAND = 710001
WIFE = 710002
STRANGER = 777


@pytest.fixture
async def db(tmp_path: Path):
    db = DatabaseManager(str(tmp_path / "bot.db"))
    await db.initialize()
    async with db.get_connection() as conn:
        await conn.execute(
            "INSERT INTO families (id, created_by) VALUES (1, ?)", (HUSBAND,)
        )
        await conn.executemany(
            "INSERT INTO bot_users (telegram_id, role, family_id, source, "
            "display_name, relation, is_admin) VALUES (?, 'owner', ?, 'env', ?, ?, ?)",
            [
                (HUSBAND, 1, "Иван", "муж", 1),
                (WIFE, 1, "Аня", "жена", 0),
                (STRANGER, None, "Чужой", None, 0),
            ],
        )
        await conn.commit()
    yield db
    await db.close()


@pytest.fixture
def registry(tmp_path: Path) -> ProfileRegistry:
    def profile(slug: str, **kw: Any) -> ProfileDefinition:
        wd = tmp_path / "ws" / slug
        wd.mkdir(parents=True)
        return ProfileDefinition(
            slug=slug,
            name=slug,
            working_dir=wd,
            memory_dir=wd / "memory",
            persona=wd / "persona.md",
            **kw,
        )

    return ProfileRegistry(
        [profile("general", default=True), profile("chef"), profile("coach")]
    )


@pytest.fixture
def scheduler(db: DatabaseManager) -> JobScheduler:
    return JobScheduler(
        event_bus=AsyncMock(), db_manager=db, default_working_directory=Path("/tmp")
    )


@pytest.fixture
def service(db, scheduler, registry) -> ItemService:
    return ItemService(
        ItemRepository(db),
        scheduler=scheduler,
        profile_registry=registry,
        default_tz="Europe/Lisbon",
        role_lookup=lambda uid: "owner",
    )


def _future(hours: int = 2) -> str:
    local = datetime.now(UTC) + timedelta(hours=hours)
    return local.replace(tzinfo=None, microsecond=0).isoformat()


async def _jobs(db: DatabaseManager) -> List[dict]:
    async with db.get_connection() as conn:
        cur = await conn.execute("SELECT * FROM scheduled_jobs WHERE is_active = 1")
        return [dict(r) for r in await cur.fetchall()]


class TestMigration:
    async def test_admin_is_family_creator_only(self, tmp_path: Path) -> None:
        db = DatabaseManager(str(tmp_path / "m.db"))
        await db.initialize()
        async with db.get_connection() as conn:
            cols = {
                r[1]
                for r in await (
                    await conn.execute("PRAGMA table_info(items)")
                ).fetchall()
            }
            job_cols = {
                r[1]
                for r in await (
                    await conn.execute("PRAGMA table_info(scheduled_jobs)")
                ).fetchall()
            }
        await db.close()
        assert {"creator_id", "assignee_id", "recurrence", "tz", "profile_slug"} <= cols
        assert {"item_id", "timezone", "ends_at"} <= job_cols
        assert "deadline" in cols

    async def test_item_notes_table_exists(self, db) -> None:
        async with db.get_connection() as conn:
            cols = {
                r[1]
                for r in await (
                    await conn.execute("PRAGMA table_info(item_notes)")
                ).fetchall()
            }
        assert {
            "item_id",
            "author_id",
            "kind",
            "body",
            "path",
            "source",
            "created_at",
        } <= cols


class TestDeadlineAndNotes:
    async def test_date_only_deadline_is_end_of_local_day(self, service) -> None:
        out = await service.add(HUSBAND, "tax", deadline="2030-07-15")
        assert out["deadline"] == "2030-07-15 23:59 Europe/Lisbon"
        item = await service.repo.get(out["id"])
        # Lisbon is UTC+1 in July
        assert item.deadline == "2030-07-15T22:59:00+00:00"

    async def test_datetime_deadline_and_clear(self, service) -> None:
        out = await service.add(HUSBAND, "x", deadline="2030-01-10T09:30:00")
        assert out["deadline"] == "2030-01-10 09:30 Europe/Lisbon"
        upd = await service.update(HUSBAND, out["id"], deadline="2030-02-01")
        assert upd["deadline"].startswith("2030-02-01 23:59")
        cleared = await service.update(HUSBAND, out["id"], deadline="none")
        assert "deadline" not in cleared

    async def test_bad_deadline_refused(self, service) -> None:
        with pytest.raises(ItemError, match="deadline"):
            await service.add(HUSBAND, "x", deadline="friday")

    async def test_deadline_independent_of_reminder(self, service) -> None:
        out = await service.add(
            HUSBAND, "x", at=_future(), deadline="2031-01-01", agent="general"
        )
        assert out["deadline"] and out["next_local"]

    async def test_note_auth_and_describe(self, service) -> None:
        out = await service.add(HUSBAND, "buy", who=str(HUSBAND))
        iid = out["id"]
        with pytest.raises(ItemError):
            await service.add_note(WIFE, iid, body="hi")
        with pytest.raises(ItemError):
            await service.add_note(STRANGER, iid, body="hi")
        await service.add_note(HUSBAND, iid, body="a" * 400, path="/tmp/r.md")
        await service.add_note(HUSBAND, iid, "research", "found it")
        with pytest.raises(ItemError, match="kind"):
            await service.add_note(HUSBAND, iid, "bogus", "x")
        listed = (await service.list(HUSBAND))[0]
        assert listed["notes"] == 2 and "notes_list" not in listed
        one = await service.get(HUSBAND, iid)
        assert one["notes"] == 2
        first, second = one["notes_list"]
        assert first["author"] == "you" and first["path"] == "/tmp/r.md"
        assert len(first["body"]) == 301 and first["body"].endswith("…")
        assert second["kind"] == "research"
        assert len(await service.list_notes(HUSBAND, iid)) == 2

    async def test_assignee_can_note(self, service) -> None:
        out = await service.add(HUSBAND, "cream", who=str(WIFE))
        await service.add_note(WIFE, out["id"], body="ok")
        one = await service.get(HUSBAND, out["id"])
        assert one["notes_list"][0]["author"] == "Аня"

    async def test_body_cap(self, service) -> None:
        out = await service.add(HUSBAND, "x")
        with pytest.raises(ItemError, match="path"):
            await service.add_note(HUSBAND, out["id"], body="a" * 4001)
        await service.add_note(HUSBAND, out["id"], body="a" * 4000)

    async def test_close_writes_status_change_note(self, service) -> None:
        out = await service.add(HUSBAND, "x", who=str(WIFE))
        await service.close(WIFE, out["id"], "done")
        notes = await service.repo.list_notes(out["id"])
        assert [(n.kind, n.body, n.author_id) for n in notes] == [
            ("status_change", "done", WIFE)
        ]
        await service.close(WIFE, out["id"], "done")  # already closed: no dup
        assert len(await service.repo.list_notes(out["id"])) == 1

    async def test_tools(self, service, scheduler) -> None:
        import json

        from src.claude.agent_tools import ALL_TOOL_SPECS
        from src.claude.tool_context import AgentServices, TurnContext

        spec = {s.name: s for s in ALL_TOOL_SPECS}
        services = AgentServices(
            event_bus=AsyncMock(), task_repo=None, scheduler=scheduler, items=service
        )
        ctx = TurnContext(profile_slug="general", user_id=HUSBAND, role="owner")
        added = await spec["item_add"].handler(
            services, ctx, {"text": "t", "deadline": "2030-03-03"}
        )
        iid = json.loads(added["content"][0]["text"])["id"]
        ok = await spec["item_note"].handler(
            services, ctx, {"id": iid, "text": "r", "kind": "research"}
        )
        assert json.loads(ok["content"][0]["text"])["status"] == "noted"
        got = await spec["item_list"].handler(services, ctx, {"id": iid})
        assert json.loads(got["content"][0]["text"])["notes_list"][0]["body"] == "r"
        bad = await spec["item_note"].handler(
            services, ctx, {"id": iid, "text": "a" * 5000}
        )
        assert bad.get("isError")


class TestAdd:
    async def test_self_one_shot_registers_item_job(self, service, db) -> None:
        out = await service.add(HUSBAND, "stretch", at=_future(), source_profile="chef")
        assert out["for"] == "you" and out["agent"] == "chef"
        jobs = await _jobs(db)
        assert len(jobs) == 1
        assert jobs[0]["item_id"] == out["id"]
        assert jobs[0]["target_chat_ids"] == str(HUSBAND)
        assert jobs[0]["delivery"] == "message"

    async def test_cross_member_defaults_to_general_and_her_chat(
        self, service, db
    ) -> None:
        out = await service.add(HUSBAND, "купить сливки", who=str(WIFE), at=_future())
        assert out["for"] == "Аня" and out["agent"] == "general"
        job = (await _jobs(db))[0]
        assert job["target_chat_ids"] == str(WIFE)
        assert job["created_by"] == HUSBAND

    async def test_name_and_relation_resolve(self, service) -> None:
        a = await service.add(HUSBAND, "x", who="жена", at=_future(), agent="chef")
        b = await service.add(WIFE, "y", who="Иван", at=_future())
        assert a["for"] == "Аня" and a["agent"] == "chef"
        assert b["for"] == "Иван"

    async def test_non_family_refused_with_roster(self, service) -> None:
        with pytest.raises(ItemError) as exc:
            await service.add(HUSBAND, "x", who=str(STRANGER), at=_future())
        assert "Аня" in str(exc.value) and str(WIFE) in str(exc.value)
        with pytest.raises(ItemError):
            await service.add(HUSBAND, "x", who="соседка", at=_future())

    async def test_cross_member_agent_delivery_refused(self, service) -> None:
        with pytest.raises(ItemError, match="deliver=message"):
            await service.add(
                HUSBAND, "x", who=str(WIFE), at=_future(), deliver="agent"
            )

    async def test_unknown_agent_lists_choices(self, service) -> None:
        with pytest.raises(ItemError, match="chef"):
            await service.add(HUSBAND, "x", at=_future(), agent="nope")

    async def test_plain_todo_has_no_job(self, service, db) -> None:
        out = await service.add(HUSBAND, "buy bulbs")
        assert out["deliver"] == "none"
        assert await _jobs(db) == []

    async def test_past_at_refused(self, service) -> None:
        with pytest.raises(ItemError, match="past"):
            await service.add(HUSBAND, "x", at="2020-01-01T10:00:00")


class TestTimezone:
    """dv-ca94/752a: a bare time is the RECIPIENT's local time."""

    async def test_lisbon_17_is_16z_in_summer_17z_in_winter(self, service) -> None:
        year = datetime.now(UTC).year + 1
        summer = await service.add(HUSBAND, "s", at=f"{year}-07-15T17:00:00")
        winter = await service.add(HUSBAND, "w", at=f"{year}-01-15T17:00:00")
        assert summer["next_utc"].endswith("T16:00+00:00")
        assert winter["next_utc"].endswith("T17:00+00:00")

    async def test_recurrence_stored_local_with_tz(self, service, db) -> None:
        out = await service.add(HUSBAND, "check-in", cron="0 17 * * 2,5")
        job = (await _jobs(db))[0]
        assert job["cron_expression"] == "0 17 * * 2,5"
        assert job["timezone"] == "Europe/Lisbon"
        assert out["recurrence"] == "0 17 * * 2,5 (Europe/Lisbon)"

    def test_cron_trigger_follows_dst(self) -> None:
        trig = build_cron_trigger("0 17 * * *", timezone="Europe/Lisbon")
        sep = trig.get_next_fire_time(None, datetime(2026, 9, 28, tzinfo=UTC))
        nov = trig.get_next_fire_time(None, datetime(2026, 11, 2, tzinfo=UTC))
        assert sep.astimezone(UTC).hour == 16
        assert nov.astimezone(UTC).hour == 17

    async def test_bad_cron_refused(self, service) -> None:
        with pytest.raises(ItemError):
            await service.add(HUSBAND, "x", cron="0 25 * * *")


class TestAuthorization:
    async def test_creator_and_assignee_may_act_third_party_not(
        self, service, db
    ) -> None:
        out = await service.add(HUSBAND, "x", who=str(WIFE), at=_future())
        with pytest.raises(ItemError):
            await service.update(STRANGER, out["id"], text="hacked")
        with pytest.raises(ItemError):
            await service.close(STRANGER, out["id"], "cancelled")
        await service.update(WIFE, out["id"], text="купить 2 сливок")
        res = await service.close(WIFE, out["id"], "cancelled")
        assert res["status"] == "cancelled"
        assert await _jobs(db) == []

    async def test_both_sides_list_it(self, service) -> None:
        out = await service.add(HUSBAND, "x", who=str(WIFE), at=_future())
        by_me = await service.list(HUSBAND, scope="by_me")
        for_me = await service.list(WIFE, scope="for_me")
        assert [r["id"] for r in by_me] == [out["id"]] == [r["id"] for r in for_me]
        assert for_me[0]["from"] == "Иван"
        assert await service.list(WIFE, scope="by_me") == []

    async def test_cancel_all_mine_only_touches_items_for_me(self, service, db) -> None:
        await service.add(HUSBAND, "for her", who=str(WIFE), at=_future())
        await service.add(WIFE, "for herself", at=_future())
        mine = await service.add(WIFE, "for him", who=str(HUSBAND), at=_future())
        res = await service.cancel_all_mine(WIFE)
        assert res["count"] == 2
        left = await _jobs(db)
        assert [j["item_id"] for j in left] == [mine["id"]]

    async def test_reschedule_replaces_job(self, service, db) -> None:
        out = await service.add(HUSBAND, "x", at=_future(2))
        await service.update(HUSBAND, out["id"], at=_future(5))
        jobs = await _jobs(db)
        assert len(jobs) == 1 and jobs[0]["item_id"] == out["id"]


class TestLoopGuard:
    async def test_automated_turn_cannot_create_for_others(self, service) -> None:
        with automated_turn(HUSBAND):
            assert is_automated(HUSBAND)
            with pytest.raises(ItemError, match="loop guard"):
                await service.add(HUSBAND, "x", who=str(WIFE), at=_future())
            await service.add(HUSBAND, "self is fine", at=_future())
        assert not is_automated(HUSBAND)

    async def test_hourly_cap(self, service) -> None:
        service.cross_member_hourly_cap = 2
        for _ in range(2):
            await service.add(HUSBAND, "x", who=str(WIFE))
        with pytest.raises(ItemError, match="at most 2"):
            await service.add(HUSBAND, "x", who=str(WIFE))


class TestFamilyMembers:
    async def test_set_member_within_family_only(self, service) -> None:
        m = await service.set_member(HUSBAND, str(WIFE), "Анна", None)
        assert m.display_name == "Анна" and m.relation == "жена"
        with pytest.raises(ItemError):
            await service.set_member(HUSBAND, str(STRANGER), "x", None)

    async def test_fill_display_name_only_when_missing(self, db) -> None:
        repo = ItemRepository(db)
        assert not await repo.fill_display_name(WIFE, "Anna")
        async with db.get_connection() as conn:
            await conn.execute(
                "UPDATE bot_users SET display_name = NULL WHERE telegram_id = ?",
                (WIFE,),
            )
            await conn.commit()
        assert await repo.fill_display_name(WIFE, "Anna")
        assert (await repo.get_member(WIFE)).display_name == "Anna"


class TestTurnContext:
    async def test_roster_and_delivery_on_both_sides_once(self, service) -> None:
        # Establish both cursors first so creation below is "new".
        await service.turn_context(WIFE, "chef")
        await service.turn_context(HUSBAND, "general")
        out = await service.add(
            HUSBAND, "купить сливки", who=str(WIFE), at=_future(), agent="chef",
            source_profile="general",
        )  # fmt: skip
        item = await service.repo.get(out["id"])
        await service.mark_fired(item)

        hers = await service.turn_context(WIFE, "chef")
        assert "Family members:" in hers and "Иван" in hers
        assert "delivered to this user here" in hers and "купить сливки" in hers
        assert "from Иван" in hers
        # Injected once: the cursor moved.
        assert "купить сливки" not in await service.turn_context(WIFE, "chef")
        # Another of her agents is not told about a chef delivery.
        assert "купить сливки" not in await service.turn_context(WIFE, "coach")

        his = await service.turn_context(HUSBAND, "general")
        assert "reminder for Аня was delivered" in his

    async def test_no_family_no_activity_is_empty(self, service) -> None:
        assert await service.turn_context(STRANGER, "general") == ""


class TestFirePath:
    def _storage(self) -> AsyncMock:
        storage = AsyncMock()
        storage.sessions.get_sessions_by_project = AsyncMock(return_value=[])
        storage.pending_notes.add = AsyncMock()
        storage.preferences.get = AsyncMock(return_value=None)
        return storage

    async def _handler(self, service, registry, claude=None):
        bus = EventBus()
        published: list = []

        async def capture(event):  # type: ignore[no-untyped-def]
            published.append(event)

        bus.publish = capture  # type: ignore[assignment]
        user_store = MagicMock()
        user_store.get_role_cached = lambda uid: "owner"
        handler = AgentHandler(
            event_bus=bus,
            claude_integration=claude or AsyncMock(),
            default_working_directory=Path("/tmp"),
            default_user_id=HUSBAND,
            profile_registry=registry,
            storage=self._storage(),
            user_store=user_store,
            items=service,
        )
        return handler, published

    async def test_cross_member_fires_in_her_chat_with_sender_header(
        self, service, registry
    ) -> None:
        out = await service.add(
            HUSBAND, "купить сливки", who=str(WIFE), at=_future(), agent="chef"
        )
        await service.update(HUSBAND, out["id"], text="купить сливки 33%")
        handler, published = await self._handler(service, registry)
        await handler.handle_scheduled(
            ScheduledEvent(
                job_id="j",
                job_name="item",
                prompt="stale snapshot",
                working_directory=registry.get_by_slug("chef").working_dir,
                target_chat_ids=[WIFE],
                delivery="message",
                created_by=HUSBAND,
                item_id=out["id"],
            )
        )
        msgs = [e for e in published if isinstance(e, AgentResponseEvent)]
        assert len(msgs) == 1
        assert msgs[0].chat_id == WIFE
        assert msgs[0].text.startswith("⏰ от Иван")
        assert "купить сливки 33%" in msgs[0].text  # live text, not snapshot
        # Pending note lands for HER, not the creator.
        note = handler.storage.pending_notes.add.await_args.args
        assert note[0] == WIFE and "from Иван" in note[2]
        item = await service.repo.get(out["id"])
        assert item.status == "fired" and item.last_fired_at

    async def test_cancelled_item_is_not_delivered(self, service, registry, db) -> None:
        out = await service.add(HUSBAND, "x", who=str(WIFE), at=_future())
        await service.close(WIFE, out["id"], "cancelled")
        handler, published = await self._handler(service, registry)
        await handler.handle_scheduled(
            ScheduledEvent(
                job_id="j",
                prompt="x",
                target_chat_ids=[WIFE],
                delivery="message",
                created_by=HUSBAND,
                item_id=out["id"],
            )
        )
        assert published == []

    async def test_agent_item_runs_as_assignee_under_loop_guard(
        self, service, registry
    ) -> None:
        out = await service.add(WIFE, "взвеситься", cron="0 19 * * 4", deliver="agent")
        seen: dict = {}

        async def run_command(**kw: Any) -> Any:
            seen.update(kw)
            seen["automated"] = is_automated(kw["user_id"])
            return MagicMock(content="Напоминаю: взвеситься")

        claude = AsyncMock()
        claude.run_command = run_command
        handler, published = await self._handler(service, registry, claude)
        await handler.handle_scheduled(
            ScheduledEvent(
                job_id="j",
                prompt="x",
                working_directory=registry.get_by_slug("general").working_dir,
                target_chat_ids=[WIFE],
                delivery="agent",
                created_by=HUSBAND,  # even a wrong created_by cannot win
                item_id=out["id"],
            )
        )
        assert seen["user_id"] == WIFE
        assert seen["automated"] is True
        assert "взвеситься" in seen["prompt"]
        assert (await service.repo.get(out["id"])).status == "open"  # recurring


class TestSchedulerLoad:
    async def test_reload_uses_timezone_and_drops_ended(self, scheduler, db) -> None:
        past = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        async with db.get_connection() as conn:
            await conn.executemany(
                "INSERT INTO scheduled_jobs (job_id, job_name, cron_expression, "
                "prompt, working_directory, is_active, item_id, timezone, ends_at) "
                "VALUES (?, ?, ?, 'p', '/tmp', 1, ?, ?, ?)",
                [
                    ("live", "l", "0 17 * * *", 5, "Europe/Lisbon", None),
                    ("ended", "e", "0 17 * * *", 6, "Europe/Lisbon", past),
                ],
            )
            await conn.commit()
        await scheduler._load_jobs_from_db()
        job = scheduler._scheduler.get_job("live")
        assert str(job.trigger.timezone) == "Europe/Lisbon"
        assert job.kwargs["item_id"] == 5
        assert scheduler._scheduler.get_job("ended") is None
        assert [j["job_id"] for j in await _jobs(db)] == ["live"]


class TestTools:
    def _ctx(self, user_id: int = HUSBAND, slug: str = "general") -> Any:
        from src.claude.tool_context import TurnContext

        return TurnContext(profile_slug=slug, user_id=user_id, role="owner")

    def _spec(self, name: str) -> Any:
        from src.claude.agent_tools import ALL_TOOL_SPECS

        return next(s for s in ALL_TOOL_SPECS if s.name == name)

    def _services(self, service, scheduler) -> Any:
        from src.claude.tool_context import AgentServices

        return AgentServices(
            event_bus=AsyncMock(), task_repo=None, scheduler=scheduler, items=service
        )

    async def test_item_tools_mount_only_with_service_and_user(
        self, service, scheduler
    ) -> None:
        from src.claude.agent_tools import available_tool_specs
        from src.claude.tool_context import AgentServices, TurnContext

        with_items = self._services(service, scheduler)
        names = {s.name for s in available_tool_specs(with_items, self._ctx())}
        assert {"item_add", "item_cancel_all_mine", "family_member_set"} <= names
        no_items = AgentServices(event_bus=AsyncMock(), task_repo=None)
        assert "item_add" not in {
            s.name for s in available_tool_specs(no_items, self._ctx())
        }
        anon = TurnContext(profile_slug="general")
        assert "item_add" not in {
            s.name for s in available_tool_specs(with_items, anon)
        }

    async def test_item_add_and_refusal_via_tool(self, service, scheduler) -> None:
        import json

        services = self._services(service, scheduler)
        add = self._spec("item_add").handler
        ok = await add(
            services,
            self._ctx(),
            {"text": "сливки", "for": str(WIFE), "at": _future(), "agent": "chef"},
        )
        body = json.loads(ok["content"][0]["text"])
        assert body["status"] == "created" and body["for"] == "Аня"
        bad = await add(services, self._ctx(), {"text": "x", "for": str(STRANGER)})
        assert bad.get("isError") and "not a member" in bad["content"][0]["text"]
        done = await self._spec("item_done").handler(
            services, self._ctx(WIFE), {"id": body["id"]}
        )
        assert json.loads(done["content"][0]["text"])["status"] == "done"

    async def test_schedule_tools_leave_item_jobs_alone(
        self, service, scheduler
    ) -> None:
        import json

        services = self._services(service, scheduler)
        await service.add(HUSBAND, "x", at=_future())
        listed = await self._spec("schedule_list").handler(services, self._ctx(), {})
        assert json.loads(listed["content"][0]["text"]) == []
        job_id = (await scheduler.list_jobs())[0]["job_id"]
        res = await self._spec("schedule_remove").handler(
            services, self._ctx(), {"job_id": job_id}
        )
        assert res.get("isError") and "item_cancel" in res["content"][0]["text"]


class TestInjectionSeam:
    async def test_prepends_block_and_degrades_on_error(
        self, service, registry
    ) -> None:
        from src.claude.turn_setup import prepend_items_context

        wd = registry.get_by_slug("chef").working_dir
        out = await prepend_items_context(service, registry, "hi", wd, WIFE)
        assert out.startswith("<family_reminders>") and out.endswith("\n\nhi")
        broken = MagicMock()
        broken.turn_context = AsyncMock(side_effect=RuntimeError("db gone"))
        assert await prepend_items_context(broken, registry, "hi", wd, WIFE) == "hi"
        assert await prepend_items_context(None, registry, "hi", wd, WIFE) == "hi"


class TestRemindersCommand:
    async def test_render_sections_and_cancel_callback(self, service) -> None:
        from src.bot.orchestration.commands.reminders import (
            handle_reminders_callback,
            render,
        )

        a = await service.add(HUSBAND, "для неё", who=str(WIFE), at=_future())
        await service.add(WIFE, "себе", at=_future())
        text, markup = await render(service, WIFE)
        assert "For myself" in text and "Set for me by family" in text
        assert "← Иван" in text
        callbacks = [b.callback_data for row in markup.inline_keyboard for b in row]
        assert f"rem:x:{a['id']}" in callbacks and "rem:all" in callbacks

        query = MagicMock()
        query.from_user.id = WIFE
        query.edit_message_text = AsyncMock()
        context = MagicMock()
        context.bot_data = {"item_service": service}
        await handle_reminders_callback(query, context, f"rem:x:{a['id']}")
        assert (await service.repo.get(a["id"])).status == "cancelled"
        query.from_user.id = STRANGER
        await handle_reminders_callback(query, context, "rem:all:yes")
        assert "Cancelled 0" in query.edit_message_text.await_args.args[0]


_MIGRATE_REMINDERS = (
    Path(__file__).resolve().parents[3] / "scripts/migrate_reminders_to_items.py"
)


# One-off host migration; withheld from the public export (EXCLUDE_SCRIPTS).
@pytest.mark.skipif(
    not _MIGRATE_REMINDERS.exists(), reason="migrate_reminders_to_items.py not shipped"
)
class TestMigrationScript:
    def _plan(self, job: dict, **kw: Any) -> Any:
        import importlib.util
        from zoneinfo import ZoneInfo

        path = (
            Path(__file__).resolve().parents[3]
            / "scripts/migrate_reminders_to_items.py"
        )
        spec = importlib.util.spec_from_file_location("mig", path)
        mod = importlib.util.module_from_spec(spec)
        import sys

        sys.modules["mig"] = mod
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        base = {
            "job_id": "j" * 32,
            "target_chat_ids": str(WIFE),
            "created_by": 0,
            "working_directory": "/x/workspaces/coach",
            "delivery": "agent",
        }
        base.update(job)
        now = kw.pop("now", datetime(2026, 9, 28, 18, tzinfo=UTC))
        return mod.plan_job(
            base, ZoneInfo("Europe/Lisbon"), now, {WIFE: HUSBAND}, False, None
        )

    def test_known_routine_keeps_hour_as_local(self) -> None:
        p = self._plan(
            {"job_name": "friday-weighin-reminder", "cron_expression": "0 19 * * 4"}
        )
        assert p.action == "CONVERT" and p.local_cron == "0 19 * * 4"
        assert p.creator_id == HUSBAND and p.assignee_id == WIFE
        assert p.profile_slug == "coach"

    def test_name_stated_local_times(self) -> None:
        p = self._plan(
            {
                "job_name": "Daily todo reminder 11:00/17:00 Lisbon (26–30.09)",
                "cron_expression": "0 10,16 26-30 9 *",
            }
        )
        assert p.action == "CONVERT" and p.local_cron == "0 11,17 26-30 9 *"
        assert p.ends_at is not None and p.ends_at.day == 30

    def test_unclear_and_oneshot_skipped_window_over_deactivated(self) -> None:
        assert (
            self._plan({"job_name": "x", "cron_expression": "0 9 * * *"}).action
            == "SKIP"
        )
        assert (
            self._plan(
                {"job_name": "x", "cron_expression": "@at:2030-01-01T00:00:00+00:00"}
            ).action
            == "SKIP"
        )
        over = self._plan(
            {"job_name": "Todo 11:00/17:00", "cron_expression": "0 10,16 1-2 9 *"}
        )
        assert over.action == "DEACTIVATE"


async def test_item_job_without_service_still_runs_as_assignee() -> None:
    """The created_by-runs-it risk: an item job never runs as its creator."""
    storage = AsyncMock()
    storage.sessions.get_sessions_by_project = AsyncMock(return_value=[])
    storage.pending_notes.add = AsyncMock()
    handler = AgentHandler(
        event_bus=AsyncMock(),
        claude_integration=AsyncMock(),
        default_working_directory=Path("/tmp"),
        default_user_id=HUSBAND,
        storage=storage,
    )
    await handler.handle_scheduled(
        ScheduledEvent(
            job_id="j",
            prompt="x",
            target_chat_ids=[WIFE],
            delivery="message",
            created_by=HUSBAND,
            item_id=9,
        )
    )
    assert storage.pending_notes.add.await_args.args[0] == WIFE
