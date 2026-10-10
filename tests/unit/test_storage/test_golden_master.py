"""dv-5e4f Phase 0.1, item 2: synthetic golden-master behavior snapshot.

Builds every DB this repo owns from synthetic (never-real) data via the
CURRENT init code, drives the storage-layer public surface (facade.py,
items/repository.py, channel_intel/store.py, llm_analytics/store.py, plus
the specific classes Phase 0 is about to route through repositories --
TokenTracker, JobScheduler, LifecycleLogger), and compares a normalized
JSON dump of every result -- and, for writes, a dump of the affected
tables -- against a committed golden file.

This is the oracle for "behavior did not change" through Phase 0 (SQL moves
into repositories), Phase 2 (pool swap) and Phase 1 (Alembic): if this test
is unchanged after a phase, the owner never has to re-check the bot in the
UI by hand for that phase. A genuine, reviewed behavior change updates the
golden with ``DEVVY_UPDATE_GOLDEN=1`` or ``pytest --update-golden`` -- never
silently.

Coverage (see the end-of-module docstring note and the task report for the
full breakdown): this file does NOT attempt to enumerate every method on
every repository class under src/storage/ -- see the handback report for
exactly what's covered vs. not and why.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from src.channel_intel.store import Store as ChannelIntelStore
from src.events.bus import EventBus
from src.items.repository import ItemRepository
from src.llm_analytics.store import Store as AnalyticsStore
from src.observability.lifecycle import LifecycleLogger
from src.scheduler.scheduler import JobScheduler
from src.storage.database import DatabaseManager
from src.storage.facade import Storage
from src.token.tracker import TokenTracker
from tests.helpers.golden import (
    compare_or_update,
    has_failures,
    jsonable,
    mask,
    sort_rows,
)
from tests.unit.test_llm_analytics.test_server import PROJ as LLM_PROJ
from tests.unit.test_llm_analytics.test_server import S1 as LLM_S1
from tests.unit.test_llm_analytics.test_server import S2 as LLM_S2
from tests.unit.test_llm_analytics.test_server import USER as LLM_USER
from tests.unit.test_llm_analytics.test_server import (
    _build_fixture as _build_llm_fixture,
)

HUSBAND = 710001
WIFE = 710002


async def _dump_table(db: DatabaseManager, table: str, order_key: str = "id") -> list:
    async with db.get_connection() as conn:
        cur = await conn.execute(f"SELECT * FROM {table}")
        rows = [dict(r) for r in await cur.fetchall()]
    return sort_rows(jsonable(rows), key=order_key)


async def _seed_bot_db_families(db: DatabaseManager) -> None:
    async with db.get_connection() as conn:
        await conn.execute(
            "INSERT INTO families (id, created_by) VALUES (1, ?)", (HUSBAND,)
        )
        await conn.executemany(
            "INSERT INTO bot_users (telegram_id, role, family_id, source, "
            "display_name, relation, is_admin) VALUES (?, 'owner', ?, 'env', ?, ?, ?)",
            [
                (HUSBAND, 1, "Ivan", "husband", 1),
                (WIFE, 1, "Ann", "wife", 0),
            ],
        )
        await conn.commit()


async def _facade_section(tmp_path: Path) -> Dict[str, Any]:
    storage = Storage(f"sqlite:///{tmp_path / 'facade_bot.db'}")
    await storage.initialize()
    out: Dict[str, Any] = {}
    try:
        user = await storage.get_or_create_user(1001, username="alice")
        out["get_or_create_user"] = jsonable(user)

        session = await storage.create_session(1001, "/work/proj", "sess-1")
        out["create_session"] = jsonable(session)

        class _Resp:
            content = "hello"
            cost = 0.01
            cost_usd = 0.01
            duration_ms = 42
            error_type = None
            is_error = False
            num_turns = 1
            tools_used = [{"name": "Bash", "input": {"command": "ls"}}]

        message_id = await storage.save_claude_interaction(
            1001, "sess-1", "hi there", _Resp()
        )
        out["save_claude_interaction_message_id"] = "<id>"
        assert message_id > 0

        await storage.log_security_event(1001, "login", {"ip": "127.0.0.1"})
        await storage.log_bot_event(1001, "restart", {"reason": "deploy"})

        out["is_user_allowed"] = await storage.is_user_allowed(1001)
        out["get_user_session_summary"] = jsonable(
            await storage.get_user_session_summary(1001)
        )
        out["get_session_history"] = jsonable(
            await storage.get_session_history("sess-1")
        )
        out["get_user_dashboard"] = jsonable(await storage.get_user_dashboard(1001))
        out["get_admin_dashboard"] = jsonable(await storage.get_admin_dashboard())
        out["health_check"] = await storage.health_check()

        # Representative coverage of sub-repositories not fully reached via
        # the high-level methods above.
        await storage.preferences.set(1001, active_profile="devops")
        out["preferences.get"] = jsonable(await storage.preferences.get(1001))

        fact_id = await storage.memory_repository.add_fact(
            "devops", "general", "likes dark roast coffee"
        )
        out["memory.get_facts"] = jsonable(
            await storage.memory_repository.get_facts("devops")
        )
        out["memory.facts_snapshot"] = jsonable(
            await storage.memory_repository.facts_snapshot("devops")
        )
        assert fact_id > 0

        await storage.sent_messages.record(555, 777, "turn_reply", message_id)
        out["sent_messages.lookup"] = jsonable(
            await storage.sent_messages.lookup(555, 777)
        )

        await storage.pending_notes.add(1001, "/work/proj", "a scheduled note")
        out["pending_notes.consume"] = jsonable(
            await storage.pending_notes.consume(1001, "/work/proj")
        )

        from datetime import UTC, datetime, timedelta

        await storage.holds.upsert_hold(
            1001, datetime.now(UTC) + timedelta(hours=1), "test hold", 0
        )
        out["holds.get_hold.reason"] = (
            await storage.holds.get_hold(1001)
        ).reason  # avoid raw datetime in golden

        await storage.pinned_messages.set(555, "status", 999)
        out["pinned_messages.load_all"] = {
            f"{k[0]}:{k[1]}": v
            for k, v in (await storage.pinned_messages.load_all()).items()
        }

        legacy_task_id = await storage.legacy_tasks.add_task("legacy todo")
        out["legacy_tasks.get_task"] = jsonable(
            await storage.legacy_tasks.get_task(legacy_task_id)
        )

        family = await storage.bot_users.create_family(created_by=1001)
        await storage.bot_users.add_user(1002, role="guest", family_id=family.id)
        out["bot_users.list_family_members"] = jsonable(
            await storage.bot_users.list_family_members(family.id)
        )
        out["bot_users.count_users"] = await storage.bot_users.count_users()

        # Table dumps proving every write above actually landed (Phase 0
        # proof requirement: "every write method followed by a dump of
        # affected tables").
        for table in (
            "users",
            "sessions",
            "messages",
            "tool_usage",
            "cost_tracking",
            "audit_log",
            "user_preferences",
            "memory_facts",
            "sent_messages",
            "user_holds",
            "pinned_messages",
            "agent_tasks",
            "families",
            "bot_users",
        ):
            out[f"dump.{table}"] = mask(await _dump_table(storage.db_manager, table))

        return out
    finally:
        await storage.close()


async def _items_section(tmp_path: Path) -> Dict[str, Any]:
    db = DatabaseManager(str(tmp_path / "items_bot.db"))
    await db.initialize()
    out: Dict[str, Any] = {}
    try:
        await _seed_bot_db_families(db)
        repo = ItemRepository(db)

        item_id = await repo.insert(
            family_id=1,
            creator_id=HUSBAND,
            assignee_id=WIFE,
            text="buy milk",
            status="open",
            recurrence=None,
            tz="UTC",
            ends_at=None,
            deliver="message",
            profile_slug=None,
            source_profile=None,
            last_fired_at=None,
            done_at=None,
            done_by=None,
            due_at=None,
            deadline=None,
        )
        out["insert_and_get"] = jsonable(await repo.get(item_id))

        await repo.update(item_id, status="done", done_by=WIFE)
        out["after_update"] = jsonable(await repo.get(item_id))

        out["list_for_all"] = jsonable(
            await repo.list_for(WIFE, scope="all", statuses=("open", "done"))
        )
        out["count_cross_created_since"] = await repo.count_cross_created_since(
            HUSBAND, "2000-01-01T00:00:00"
        )
        out["activity_since"] = jsonable(
            await repo.activity_since(WIFE, "2000-01-01T00:00:00")
        )

        note_id = await repo.add_note(item_id, HUSBAND, "comment", "thanks!")
        out["list_notes"] = jsonable(await repo.list_notes(item_id))
        out["count_notes"] = await repo.count_notes(item_id)
        assert note_id > 0

        await repo.set_seen(WIFE, "devops", "2026-01-01T00:00:00")
        out["get_seen"] = await repo.get_seen(WIFE, "devops")

        out["get_member"] = jsonable(await repo.get_member(HUSBAND))
        out["family_members"] = jsonable(await repo.family_members(1))

        await repo.set_member_fields(WIFE, relation="spouse")
        out["fill_display_name_noop"] = await repo.fill_display_name(WIFE, "Ann W")
        out["get_member_after_fields"] = jsonable(await repo.get_member(WIFE))

        for table in ("items", "item_notes", "item_seen", "bot_users"):
            out[f"dump.{table}"] = mask(await _dump_table(db, table))

        return out
    finally:
        await db.close()


async def _channel_intel_section(tmp_path: Path) -> Dict[str, Any]:
    store = await ChannelIntelStore.open(tmp_path / "kb.db")
    out: Dict[str, Any] = {}
    try:
        place_result = await store.upsert_place(
            name="Sunny Cafe",
            type="cafe",
            location="Lisbon",
            price="$$",
            features=["wifi", "quiet"],
            status="want_to_visit",
        )
        place_id = place_result.place.id
        out["upsert_place"] = jsonable(place_result)
        out["get_place"] = jsonable(await store.get_place(place_id))
        out["find_places"] = jsonable(await store.find_places(features=["wifi"]))
        out["search"] = jsonable(await store.search("Sunny"))

        images = await store.attach_images(
            place_id=place_id,
            items=[
                {
                    "file_path": "images/img1.jpg",
                    "caption": "front door",
                    "tags": ["exterior"],
                }
            ],
        )
        out["attach_images"] = jsonable(images)
        out["find_images_for"] = jsonable(
            await store.find_images_for(place_id=place_id)
        )
        out["set_confirmed"] = jsonable(
            await store.set_confirmed(images[0].id, place_id=place_id)
        )

        ref = await store.save_reference(
            source_channel="friends_chat", text="try Sunny Cafe", tags=["food"]
        )
        await store.link_reference_to_place(reference_id=ref.id, place_id=place_id)
        out["save_reference"] = jsonable(ref)
        out["places_for_reference"] = await store.places_for_reference(ref.id)

        visit = await store.record_visit(
            place_id=place_id, date="2026-01-01", narrative="great coffee"
        )
        out["record_visit"] = jsonable(visit)

        post = await store.create_post(place_ids=[place_id], body="Loved it!")
        out["create_post"] = jsonable(post)
        out["get_post"] = jsonable(await store.get_post(post.id))

        await store.update_preferences(budget_range="$$")
        out["get_preferences"] = jsonable(await store.get_preferences())

        return out
    finally:
        await store.close()


async def _llm_analytics_section(tmp_path: Path) -> Dict[str, Any]:
    config = _build_llm_fixture(tmp_path / "llm")
    store = await AnalyticsStore.open(config.db_path)
    out: Dict[str, Any] = {}
    try:
        out["health"] = jsonable(await store.health())
        out["filters"] = jsonable(await store.filters())
        out["overview"] = jsonable(await store.overview())
        out["quality"] = jsonable(await store.quality())
        out["sessions_page"] = jsonable(await store.sessions_page(limit=10))
        out["session_detail"] = jsonable(await store.session_detail(LLM_S1))
        out["session_timeline"] = jsonable(await store.session_timeline(LLM_S1))
        out["get_call"] = jsonable(await store.get_call("req_s1_1"))
        out["call_location"] = jsonable(await store.call_location("req_s1_1"))
        out["reconciliation"] = jsonable(await store.reconciliation())
        out["chains"] = jsonable(await store.chains())
        out["chain_detail"] = jsonable(
            await store.chain_detail(user_id=LLM_USER, working_dir=LLM_PROJ)
        )
        # second session, exercises a different code path in session_detail
        out["session_detail_s2"] = jsonable(await store.session_detail(LLM_S2))
        return out
    finally:
        await store.close()


async def _token_and_scheduler_section(tmp_path: Path) -> Dict[str, Any]:
    db = DatabaseManager(str(tmp_path / "token_sched_bot.db"))
    await db.initialize()
    out: Dict[str, Any] = {}
    try:
        async with db.get_connection() as conn:
            await conn.execute("INSERT INTO users (user_id) VALUES (42)")
            await conn.execute(
                "INSERT INTO sessions (session_id, user_id, project_path) "
                "VALUES ('sess-tok', 42, '/work/proj')"
            )
            await conn.commit()

        tracker = TokenTracker(db)
        await tracker.record_turn("sess-tok", 42, 100, 50, 150, 0.002)
        await tracker.record_turn("sess-tok", 42, 10, 5, 165, 0.0005)
        out["token.get_session_totals"] = jsonable(
            await tracker.get_session_totals("sess-tok")
        )
        out["token.get_cumulative_for_session"] = (
            await tracker.get_cumulative_for_session("sess-tok")
        )
        out["token.get_usage_by_project"] = jsonable(
            await tracker.get_usage_by_project(42)
        )

        async with db.get_connection() as conn:
            await conn.execute(
                "INSERT INTO memory_extractions "
                "(session_id, user_id, prompt_hash, validation_status) "
                "VALUES ('sess-tok', 42, 'hash1', 'success')"
            )
            await conn.commit()
        out["token.get_last_extraction"] = jsonable(
            await tracker.get_last_extraction("sess-tok")
        )

        bus = EventBus()
        scheduler = JobScheduler(bus, db, tmp_path)
        job_id = await scheduler.add_job(
            job_name="daily-digest",
            cron_expression="0 9 * * *",
            prompt="send the digest",
            created_by=42,
        )
        await scheduler.start()
        out["scheduler.list_jobs"] = mask(jsonable(await scheduler.list_jobs()))
        removed = await scheduler.remove_jobs_for_item(0)  # no job targets item 0
        out["scheduler.remove_jobs_for_item_none"] = removed
        await scheduler.stop()
        out["scheduler.job_id_present"] = bool(job_id)

        lifecycle = LifecycleLogger(db)
        await lifecycle.ensure_schema()
        await lifecycle.record("devops", "started", {"pid": 123})
        out["lifecycle.recent"] = mask(jsonable(await lifecycle.recent("devops")))

        for table in ("session_tracking", "memory_extractions", "scheduled_jobs"):
            out[f"dump.{table}"] = mask(await _dump_table(db, table))

        return out
    finally:
        await db.close()


async def _window_repository_section(tmp_path: Path) -> Dict[str, Any]:
    """dv-5e4f Phase 0.1 gap-fill: WindowRepository (src/storage/
    window_repository.py) had no golden coverage at all -- every public
    method below."""
    from src.storage.window_repository import WindowRepository

    db = DatabaseManager(str(tmp_path / "window_bot.db"))
    await db.initialize()
    out: Dict[str, Any] = {}
    try:
        async with db.get_connection() as conn:
            await conn.execute("INSERT INTO users (user_id) VALUES (901)")
            await conn.execute(
                "INSERT INTO sessions (session_id, user_id, project_path) "
                "VALUES ('sess-win', 901, '/work/winproj')"
            )
            await conn.commit()

        repo = WindowRepository(db, eval_interval_tokens=1000)
        wd = "/work/winproj"

        out["get_or_create.initial"] = jsonable(await repo.get_or_create(901, wd))

        st = await repo.record_turn(901, wd, "claude", 100, context_tokens=1500)
        out["record_turn.claude_with_context"] = jsonable(st)
        st = await repo.record_turn(901, wd, "claude", 50, context_tokens=None)
        out["record_turn.claude_no_context_holds_level"] = jsonable(st)
        st = await repo.record_turn(901, wd, "openai_codex", 20, context_tokens=None)
        out["record_turn.non_level_backend_uses_flow"] = jsonable(st)

        out["should_run_evaluator.after_turns"] = repo.should_run_evaluator(st)
        await repo.mark_evaluator_run(901, wd, max_message_id=10)
        st = await repo.get_or_create(901, wd)
        out["after_mark_evaluator_run"] = jsonable(st)

        out["should_refresh.default_threshold"] = repo.should_refresh(st)
        out["should_refresh.custom_threshold"] = repo.should_refresh(
            st, threshold_tokens=1000
        )

        await repo.set_pending_transition(
            901, wd, seed_window=3, force_new_session=True
        )
        out["after_set_pending_transition"] = jsonable(
            await repo.get_or_create(901, wd)
        )
        await repo.clear_pending_transition(901, wd)
        out["after_clear_pending_transition"] = jsonable(
            await repo.get_or_create(901, wd)
        )

        old, new = await repo.roll_window(901, wd, "openai_codex", initial_tokens=200)
        out["roll_window"] = {"old": old, "new": new}
        out["after_roll"] = jsonable(await repo.get_or_create(901, wd))

        async with db.get_connection() as conn:
            await conn.execute(
                "INSERT INTO messages (session_id, user_id, prompt, response, "
                "window_id) VALUES ('sess-win', 901, 'hi', 'hello', ?)",
                (new,),
            )
            await conn.execute(
                "INSERT INTO messages (session_id, user_id, prompt, response, "
                "window_id) VALUES ('sess-win', 901, 'bye', 'later', ?)",
                (new,),
            )
            await conn.commit()
        out["tail_messages"] = jsonable(await repo.tail_messages(901, wd, new, limit=5))
        out["count_window_messages"] = await repo.count_window_messages(901, wd, new)

        out["list_for_user"] = jsonable(await repo.list_for_user(901))
        out["dump.conversation_windows"] = mask(
            await _dump_table(db, "conversation_windows", order_key="user_id")
        )
        return out
    finally:
        await db.close()


async def _codex_sessions_section(tmp_path: Path) -> Dict[str, Any]:
    """src/storage/codex_sessions.py: CodexSessionStore,
    InMemoryCodexSessionStore and import_codex_sessions_json -- zero prior
    golden coverage."""
    import json

    from src.storage.codex_sessions import (
        CodexSessionStore,
        InMemoryCodexSessionStore,
        import_codex_sessions_json,
    )

    db = DatabaseManager(str(tmp_path / "codex_bot.db"))
    await db.initialize()
    out: Dict[str, Any] = {}
    try:
        async with db.get_connection() as conn:
            await conn.executemany(
                "INSERT INTO users (user_id) VALUES (?)", [(902,), (903,)]
            )
            await conn.commit()

        store = CodexSessionStore(db)
        wd = Path("/work/codexproj")

        out["thread_for.none"] = await store.thread_for(902, wd)

        await store.record(
            902, wd, "thread-a", 100, 50, thread_total_input=100, thread_total_output=50
        )
        out["thread_for.after_record"] = await store.thread_for(902, wd)
        out["info_for"] = jsonable(await store.info_for(902, wd))
        out["thread_totals"] = list(await store.thread_totals(902, wd, "thread-a"))

        await store.record(
            902, wd, "thread-b", 10, 5, thread_total_input=10, thread_total_output=5
        )
        out["thread_for.after_second_thread_moves_head"] = await store.thread_for(
            902, wd
        )
        out["sessions_for_user"] = jsonable(await store.sessions_for_user(902))

        await store.clear(902, wd)
        out["thread_for.after_clear"] = await store.thread_for(902, wd)

        out["dump.sessions_codex_rows"] = mask(
            [
                r
                for r in await _dump_table(db, "sessions", order_key="session_id")
                if r.get("backend") == "openai_codex"
            ]
        )

        # import_codex_sessions_json: legacy JSON entry for a user/dir that
        # has no `sessions` row yet (the "inserted" path), read-only on disk.
        wd2 = Path("/work/legacyproj")
        json_path = tmp_path / "codex_sessions.json"
        json_path.write_text(
            json.dumps(
                {
                    f"903::{wd2}": {
                        "session_id": "thread-legacy",
                        "user_id": 903,
                        "working_dir": str(wd2),
                        "cumulative_tokens": 300,
                        "thread_total_input": 200,
                        "thread_total_output": 100,
                        "updated_at": "2026-01-01T00:00:00+00:00",
                    }
                }
            )
        )
        counts = await import_codex_sessions_json(db, json_path)
        out["import_codex_sessions_json.counts"] = counts
        out["import_codex_sessions_json.thread_for"] = await store.thread_for(903, wd2)
        # Idempotent rerun: already tagged, must not double count.
        out["import_codex_sessions_json.rerun_counts"] = (
            await import_codex_sessions_json(db, json_path)
        )
        out["import_codex_sessions_json.missing_file"] = (
            await import_codex_sessions_json(db, tmp_path / "does_not_exist.json")
        )

        mem = InMemoryCodexSessionStore()
        out["in_memory.thread_for_none"] = await mem.thread_for(1, Path("/a"))
        await mem.record(
            1, Path("/a"), "t1", 10, 5, thread_total_input=10, thread_total_output=5
        )
        out["in_memory.thread_for"] = await mem.thread_for(1, Path("/a"))
        out["in_memory.info_for"] = jsonable(await mem.info_for(1, Path("/a")))
        out["in_memory.thread_totals"] = list(
            await mem.thread_totals(1, Path("/a"), "t1")
        )
        out["in_memory.sessions_for_user"] = jsonable(await mem.sessions_for_user(1))
        await mem.clear(1, Path("/a"))
        out["in_memory.thread_for_after_clear"] = await mem.thread_for(1, Path("/a"))

        return out
    finally:
        await db.close()


async def _model_calls_section(tmp_path: Path) -> Dict[str, Any]:
    """src/storage/model_calls.py: ModelCallRepository.record -- the only
    public method, append-only, zero prior golden coverage."""
    from src.storage.model_calls import ModelCall, ModelCallRepository

    db = DatabaseManager(str(tmp_path / "modelcalls_bot.db"))
    await db.initialize()
    out: Dict[str, Any] = {}
    try:
        repo = ModelCallRepository(db)
        await repo.record(
            ModelCall(
                purpose="turn",
                kind="turn",
                harness="claude",
                harness_version="1.2.3",
                provider="anthropic",
                model="claude-sonnet-5",
                attended=True,
                profile="devops",
                user_id=904,
                session_id="sess-mc",
                input_tokens=120,
                output_tokens=40,
                cache_read_tokens=10,
                cache_creation_tokens=5,
                cost_usd=0.01,
                duration_ms=250,
                outcome="ok",
                error=None,
            )
        )
        await repo.record(
            ModelCall(
                purpose="plain",
                kind="plain",
                harness="openai_codex",
                harness_version=None,
                provider="openai",
                model=None,
                attended=False,
                profile=None,
                user_id=None,
                session_id=None,
                input_tokens=0,
                output_tokens=0,
                cache_read_tokens=0,
                cache_creation_tokens=0,
                cost_usd=None,
                duration_ms=5,
                outcome="error",
                error="boom",
            )
        )
        out["dump.model_calls"] = mask(await _dump_table(db, "model_calls"))
        return out
    finally:
        await db.close()


async def _deviz_board_section(tmp_path: Path) -> tuple:
    """src/storage/deviz_board.py: DevizBoard -- zero prior golden coverage.

    Task ids are server-generated (``secrets.token_hex``), so unlike every
    other section this one returns a ``(data, redact)`` pair: the caller
    merges ``redact`` into the top-level literal-substring redaction map
    (same mechanism as the ``tmp_path`` redaction) so the random ids never
    make the golden file non-reproducible.
    """
    from src.storage.deviz_board import DevizBoard

    board = DevizBoard(tmp_path / "tasks")
    out: Dict[str, Any] = {}

    from datetime import UTC, datetime

    task_id = await board.add_task(
        "  Fix   the   thing  ",
        body="full description",
        created_by="devops",
        assignee="devops",
        priority="high",
        labels=["bug", "db"],
        # Fixed, distinct due_at: list_tasks sorts due_at-present before
        # due_at-absent, so this task's position is deterministic even if
        # both tasks' created_at lands in the same second (a tie there would
        # otherwise fall back to filesystem glob order, which is random --
        # task ids are secrets.token_hex).
        due_at=datetime(2030, 1, 1, tzinfo=UTC),
    )
    task2_id = await board.add_task(
        "Second task", created_by="user", status="in_progress"
    )

    out["add_task.id_present"] = bool(task_id) and bool(task2_id)
    out["get_task"] = jsonable(await board.get_task(task_id))
    out["list_tasks.open"] = jsonable(await board.list_tasks(status="open"))

    out["set_status.ok"] = await board.set_status(task_id, "done")
    out["get_task.after_done"] = jsonable(await board.get_task(task_id))
    out["list_tasks.done"] = jsonable(await board.list_tasks(status="done"))
    out["list_tasks.all"] = jsonable(await board.list_tasks(status="all"))

    out["append_comment.ok"] = await board.append_comment(task2_id, "progress note")
    out["get_task.after_comment"] = jsonable(await board.get_task(task2_id))

    out["find_open_by_title.hit"] = jsonable(
        await board.find_open_by_title("Second task")
    )
    out["find_open_by_title.miss"] = jsonable(await board.find_open_by_title("nope"))

    out["get_task.missing"] = jsonable(await board.get_task("dv-0000"))
    out["set_status.missing"] = await board.set_status("dv-0000", "done")
    out["append_comment.missing"] = await board.append_comment("dv-0000", "x")

    redact = {task_id: "<DV_ID_1>", task2_id: "<DV_ID_2>"}
    return out, redact


async def _session_storage_section(tmp_path: Path) -> Dict[str, Any]:
    """src/storage/session_storage.py: SQLiteSessionStorage -- zero prior
    golden coverage (the facade/items sections exercise sessions through
    SessionRepository, a different class over the same table)."""
    from datetime import UTC, datetime

    from src.claude.session import ClaudeSession
    from src.storage.session_storage import SQLiteSessionStorage

    db = DatabaseManager(str(tmp_path / "session_storage_bot.db"))
    await db.initialize()
    out: Dict[str, Any] = {}
    try:
        store = SQLiteSessionStorage(db)
        now = datetime(2026, 1, 10, 12, 0, tzinfo=UTC)

        session = ClaudeSession(
            session_id="sqlss-1",
            user_id=905,
            project_path=Path("/work/sqlssproj"),
            created_at=now,
            last_used=now,
            total_cost=0.5,
            total_turns=2,
            message_count=4,
        )
        await store.save_session(session)  # exercises _ensure_user_exists + INSERT
        out["load_session.after_insert"] = jsonable(
            await store.load_session("sqlss-1", 905)
        )

        session.total_turns = 3
        session.message_count = 5
        await store.save_session(session)  # UPDATE path (rowcount > 0)
        out["load_session.after_update"] = jsonable(
            await store.load_session("sqlss-1", 905)
        )
        out["load_session.wrong_user"] = jsonable(
            await store.load_session("sqlss-1", 999999)
        )
        out["load_session.missing"] = jsonable(await store.load_session("nope", 905))

        session2 = ClaudeSession(
            session_id="sqlss-2",
            user_id=905,
            project_path=Path("/work/sqlssproj2"),
            created_at=now,
            last_used=now,
        )
        await store.save_session(session2)

        out["get_user_sessions"] = jsonable(await store.get_user_sessions(905))
        out["get_all_sessions"] = jsonable(await store.get_all_sessions())

        await store.delete_session("sqlss-2", reason="test_teardown")
        out["get_user_sessions.after_delete"] = jsonable(
            await store.get_user_sessions(905)
        )
        out["dump.sessions_sqlss"] = mask(
            [
                r
                for r in await _dump_table(db, "sessions", order_key="session_id")
                if str(r.get("session_id", "")).startswith("sqlss-")
            ]
        )
        return out
    finally:
        await db.close()


async def _webhook_and_window_bundle_section(tmp_path: Path) -> Dict[str, Any]:
    """src/storage/webhook_repository.py (WebhookEventRepository) and
    src/storage/window_bundle_repository.py (WindowBundleRepository) --
    zero prior golden coverage."""
    from src.storage.webhook_repository import WebhookEventRepository
    from src.storage.window_bundle_repository import (
        WindowBundle,
        WindowBundleRepository,
    )

    db = DatabaseManager(str(tmp_path / "webhook_bundle_bot.db"))
    await db.initialize()
    out: Dict[str, Any] = {}
    try:
        webhooks = WebhookEventRepository(db)
        out["record_if_new.first"] = await webhooks.record_if_new(
            "evt-1", "github", "push", "delivery-1", {"ref": "main"}
        )
        out["record_if_new.duplicate"] = await webhooks.record_if_new(
            "evt-1", "github", "push", "delivery-1", {"ref": "main"}
        )
        out["dump.webhook_events"] = mask(await _dump_table(db, "webhook_events"))

        bundles = WindowBundleRepository(db)
        bundle = WindowBundle(
            user_id=906,
            working_dir="/work/bundleproj",
            window_id=1,
            agent_id="devops",
            bundle_text="<system prompt text>",
            bundle_sha256="a" * 64,
            persona_chars=100,
            facts_count=3,
            pointers_count=1,
            family_count=2,
            total_chars=120,
        )
        out["put.first"] = jsonable(await bundles.put(bundle))
        # Second put with the SAME key but different content: first write
        # wins (INSERT OR IGNORE), so this must return the ORIGINAL row.
        conflicting = WindowBundle(
            user_id=906,
            working_dir="/work/bundleproj",
            window_id=1,
            agent_id="devops",
            bundle_text="<different text>",
            bundle_sha256="b" * 64,
            persona_chars=200,
            facts_count=5,
            pointers_count=2,
            family_count=2,
            total_chars=220,
        )
        out["put.conflicting_keeps_first"] = jsonable(await bundles.put(conflicting))
        out["get.present"] = jsonable(await bundles.get(906, "/work/bundleproj", 1))
        out["get.missing_window"] = jsonable(
            await bundles.get(906, "/work/bundleproj", 2)
        )
        return out
    finally:
        await db.close()


async def _repo_gaps_section(tmp_path: Path) -> Dict[str, Any]:
    """Public methods on already-partly-covered repository modules that the
    existing sections' call patterns never reach:
    SchedulerRepository.deactivate_job / active_job_ids_for_item
    (src/storage/scheduler_repository.py -- list_active_jobs/save_job are
    covered via JobScheduler in _token_and_scheduler_section), and
    MemoryExtractionRepository.already_extracted / record_extraction
    (src/storage/extraction_repository.py -- get_last_extraction is covered
    via TokenTracker in the same section)."""
    from src.storage.extraction_repository import MemoryExtractionRepository
    from src.storage.scheduler_repository import SchedulerRepository

    db = DatabaseManager(str(tmp_path / "repo_gaps_bot.db"))
    await db.initialize()
    out: Dict[str, Any] = {}
    try:
        async with db.get_connection() as conn:
            await conn.execute("INSERT INTO users (user_id) VALUES (907)")
            await conn.execute(
                "INSERT INTO items (id, creator_id, assignee_id, text, "
                "created_at, updated_at) VALUES (1, 907, 907, 'water plants', "
                "'2026-01-01T00:00:00', '2026-01-01T00:00:00')"
            )
            await conn.commit()

        sched = SchedulerRepository(db)
        await sched.save_job(
            job_id="job-gap-1",
            job_name="water-plants",
            cron_expression="0 8 * * *",
            prompt="water the plants",
            target_chat_ids_csv="",
            working_directory="/work",
            skill_name=None,
            created_by=907,
            delivery="message",
            item_id=1,
            timezone="UTC",
            ends_at_iso=None,
        )
        out["active_job_ids_for_item.before_deactivate"] = (
            await sched.active_job_ids_for_item(1)
        )
        await sched.deactivate_job("job-gap-1")
        out["active_job_ids_for_item.after_deactivate"] = (
            await sched.active_job_ids_for_item(1)
        )
        out["dump.scheduled_jobs_gap"] = mask(
            [
                r
                for r in await _dump_table(db, "scheduled_jobs", order_key="job_id")
                if r.get("job_id") == "job-gap-1"
            ]
        )

        extractions = MemoryExtractionRepository(db)
        out["already_extracted.before"] = await extractions.already_extracted(
            907, "hash-abc"
        )
        await extractions.record_extraction(
            session_id="sess-gap",
            user_id=907,
            prompt_hash="hash-abc",
            raw_response="raw",
            parsed_insights_json='{"facts": []}',
            validation_status="success",
            target_files_json="[]",
            error_message=None,
            cumulative_tokens_at_run=100,
            op_stats_json='{"found": 0}',
        )
        out["already_extracted.after_success"] = await extractions.already_extracted(
            907, "hash-abc"
        )
        await extractions.record_extraction(
            session_id="sess-gap",
            user_id=907,
            prompt_hash="hash-failed",
            raw_response=None,
            parsed_insights_json=None,
            validation_status="failed",
            target_files_json=None,
            error_message="parse error",
            cumulative_tokens_at_run=50,
            op_stats_json=None,
        )
        out["already_extracted.failed_status_excluded"] = (
            await extractions.already_extracted(907, "hash-failed")
        )
        out["dump.memory_extractions_gap"] = mask(
            await _dump_table(db, "memory_extractions")
        )
        return out
    finally:
        await db.close()


async def _user_data_section(tmp_path: Path) -> Dict[str, Any]:
    """src/storage/user_data.py: UserDataManager + UserDataRepository
    (data.db) -- zero prior golden coverage. Every public method on
    UserDataRepository, plus the py_casefold Unicode-casefold matching paths
    in save_product/product_find/save_portion/portion_find."""
    from src.storage.user_data import UserDataManager, UserDataRepository

    db = UserDataManager(f"sqlite:///{tmp_path / 'data.db'}")
    await db.initialize()
    repo = UserDataRepository(db)
    out: Dict[str, Any] = {}
    U = 1001
    D0, D1, D2 = "2026-01-05", "2026-01-10", "2026-01-11"
    try:
        # -- food --
        a = await repo.log_food(
            U, D1, "breakfast", kcal=400, protein=20, fat=10, carb=50
        )
        out["log_food"] = jsonable(a)
        items_res = await repo.log_food_items(
            U,
            D1,
            [
                {"meal": "lunch", "kcal": 600, "protein": 30},
                {"meal": "snack", "kcal": 150},
            ],
        )
        out["log_food_items"] = jsonable(items_res)
        dinner = await repo.log_food(
            U, D1, "dinner", kcal=500, imported_from="test.csv"
        )
        out["edit_food"] = jsonable(
            await repo.edit_food(U, a["id"], kcal=420, clear=["confidence"])
        )
        out["missing_entry_index.food"] = await repo.missing_entry_index(
            U, "food", [a["id"], 999999]
        )
        out["edit_food_many"] = jsonable(
            await repo.edit_food_many(
                U, [{"entry_id": items_res["ids"][0], "kcal": 610}]
            )
        )
        out["day_food_totals"] = jsonable(await repo.day_food_totals(U, D1))
        out["delete_log_entry.food"] = jsonable(
            await repo.delete_log_entry(U, "food", items_res["ids"][1])
        )
        out["delete_log_entries.food"] = jsonable(
            await repo.delete_log_entries(
                U, [{"domain": "food", "entry_id": dinner["id"]}]
            )
        )
        out["count_imported.before_delete"] = await repo.count_imported(U, "test.csv")

        # -- training --
        t_early = await repo.log_train(
            U, D0, exercise="squat", set_n="1", weight_kg="90", reps="5"
        )
        t_res = await repo.log_train_sets(
            U,
            D1,
            [
                {"exercise": "squat", "set_n": "1", "weight_kg": "100", "reps": "5"},
                {"exercise": "bench", "set_n": "1", "weight_kg": "60", "reps": "8"},
            ],
            session="A",
        )
        out["log_train"] = jsonable(t_early)
        out["log_train_sets"] = jsonable(t_res)
        out["edit_train"] = jsonable(
            await repo.edit_train(U, t_res["ids"][0], weight_kg="105")
        )
        out["edit_train_many"] = jsonable(
            await repo.edit_train_many(U, [{"entry_id": t_res["ids"][1], "reps": "9"}])
        )
        out["missing_entry_index.train"] = await repo.missing_entry_index(
            U, "train", [t_res["ids"][0], 999999]
        )
        out["lift_history"] = jsonable(await repo.lift_history(U, "squat", limit=5))

        # -- activity --
        act = await repo.log_activity(
            U, D1, type="run", source="measured", duration_min=30, kcal_active=200
        )
        out["log_activity"] = jsonable(act)
        acts_res = await repo.log_activities(
            U,
            D1,
            [
                {"type": "walk", "source": "user_reported", "duration_min": 15},
                {"type": "swim", "source": "estimated", "duration_min": 20},
            ],
        )
        out["log_activities"] = jsonable(acts_res)
        out["edit_activity"] = jsonable(
            await repo.edit_activity(U, act["id"], duration_min=35)
        )
        out["edit_activity_many"] = jsonable(
            await repo.edit_activity_many(
                U, [{"entry_id": acts_res["ids"][0], "intensity": "moderate"}]
            )
        )
        out["missing_entry_index.activity"] = await repo.missing_entry_index(
            U, "activity", [act["id"], 999999]
        )
        out["delete_log_entries.mixed"] = jsonable(
            await repo.delete_log_entries(
                U, [{"domain": "activity", "entry_id": acts_res["ids"][1]}]
            )
        )

        # -- sleep --
        out["log_sleep"] = jsonable(
            await repo.log_sleep(
                U,
                D1,
                bed="23:00",
                wake="07:00",
                sleep_min=480,
                score=85,
                hrv=60,
                rhr=55,
            )
        )
        out["log_sleep_nights"] = jsonable(
            await repo.log_sleep_nights(
                U,
                [
                    {"date": D2, "bed": "23:30", "wake": "07:15", "sleep_min": 465},
                    {"date": D0, "bed": "22:45", "wake": "06:30", "sleep_min": 450},
                ],
            )
        )
        out["edit_sleep_day"] = jsonable(
            await repo.edit_sleep_day(U, D1, score=90, clear=["hrv"])
        )
        out["missing_sleep_date_index"] = await repo.missing_sleep_date_index(
            U, [D1, "2099-01-01"]
        )
        out["edit_sleep_days"] = jsonable(
            await repo.edit_sleep_days(U, [{"date": D2, "wellbeing": "good"}])
        )
        out["sleep_last"] = jsonable(await repo.sleep_last(U, n=5))

        # -- summaries / day-state reads --
        out["day_summary"] = jsonable(await repo.day_summary(U, D1))
        out["week_summary"] = jsonable(await repo.week_summary(U, end_date=D1))
        out["latest_weight"] = jsonable(await repo.latest_weight(U))
        out["last_train_before"] = jsonable(await repo.last_train_before(U, D1))
        out["has_entries_on.true"] = await repo.has_entries_on(U, D1)
        out["has_entries_on.false"] = await repo.has_entries_on(U, "1999-01-01")

        # -- products (shared; py_casefold paths) --
        p1 = await repo.save_product(
            name="Зефир",
            kind="product",
            brand="ChocoCo",
            provenance="label_photo",
            kcal_100g=300,
        )
        out["save_product.authoritative"] = jsonable(p1)
        out["save_product.estimate_refused_via_casefold"] = jsonable(
            await repo.save_product(
                name="зефир",
                kind="product",
                brand="chococo",
                provenance="estimate",
                kcal_100g=999,
            )
        )
        out["save_products"] = jsonable(
            await repo.save_products(
                [
                    {
                        "name": "Milk",
                        "kind": "product",
                        "provenance": "official",
                        "kcal_100g": 42,
                    },
                    {
                        "name": "Bread",
                        "kind": "product",
                        "provenance": "estimate",
                        "kcal_100g": 250,
                    },
                ]
            )
        )
        out["product_find.cyrillic_casefold"] = jsonable(await repo.product_find("зеф"))
        out["product_find.ascii_uppercase"] = jsonable(await repo.product_find("MILK"))
        bread_id = (await repo.product_find("Bread"))[0]["id"]
        out["delete_product"] = jsonable(await repo.delete_product(bread_id))

        # -- portions (per-user; py_casefold paths) --
        await repo.save_portion(U, phrase="Нормальная порция", grams=250)
        out["save_portion.casefold_update"] = jsonable(
            await repo.save_portion(U, phrase="нормальная порция", grams=260)
        )
        out["save_portions"] = jsonable(
            await repo.save_portions(U, [{"phrase": "small plate", "grams": 150}])
        )
        out["portion_find"] = jsonable(await repo.portion_find(U, "норм"))
        small_plate_id = (await repo.portion_find(U, "small plate"))[0]["id"]
        out["delete_portion"] = jsonable(await repo.delete_portion(U, small_plate_id))

        # -- issues --
        issue = await repo.issue_open(
            U, title="Knee pain", symptom="ache", started_on=D0
        )
        out["issue_open"] = jsonable(issue)
        out["issue_update"] = jsonable(
            await repo.issue_update(U, issue["id"], status="RESOLVED", resolved_on=D1)
        )
        out["issue_list.all"] = jsonable(await repo.issue_list(U))
        out["issue_list.resolved"] = jsonable(
            await repo.issue_list(U, status="RESOLVED")
        )

        # -- docs --
        doc = await repo.doc_save(
            U, kind="note", content="Some notes", period="2026-01", title="Jan notes"
        )
        out["doc_save"] = jsonable(doc)
        out["doc_get"] = jsonable(await repo.doc_get(U, doc["id"]))
        out["doc_get.missing"] = jsonable(await repo.doc_get(U, 999999))
        out["doc_list"] = jsonable(await repo.doc_list(U, kind="note"))

        # -- export / migration support --
        out["export_rows.food"] = jsonable(await repo.export_rows(U, "food"))
        out["export_rows.portions"] = jsonable(await repo.export_rows(U, "portions"))
        out["export_rows.products_shared"] = jsonable(
            await repo.export_rows(U, "products")
        )
        out["count_imported.after_delete_dinner"] = await repo.count_imported(
            U, "test.csv"
        )
        out["delete_imported"] = await repo.delete_imported(U, "test.csv")
        out["count_imported.after_delete_imported"] = await repo.count_imported(
            U, "test.csv"
        )

        return out
    finally:
        await db.close()


async def test_storage_golden_master(tmp_path: Path):
    subdirs = {
        name: tmp_path / name
        for name in (
            "facade",
            "items",
            "ci",
            "la",
            "tok",
            "winrepo",
            "codex",
            "mcalls",
            "deviz",
            "sessstore",
            "webhookbundle",
            "repogaps",
            "userdata",
        )
    }
    for p in subdirs.values():
        p.mkdir(exist_ok=True)

    deviz_data, deviz_redact = await _deviz_board_section(subdirs["deviz"])

    golden: Dict[str, Any] = {
        "facade": await _facade_section(subdirs["facade"]),
        "items": await _items_section(subdirs["items"]),
        "channel_intel": await _channel_intel_section(subdirs["ci"]),
        "llm_analytics": await _llm_analytics_section(subdirs["la"]),
        "token_and_scheduler": await _token_and_scheduler_section(subdirs["tok"]),
        "window_repository": await _window_repository_section(subdirs["winrepo"]),
        "codex_sessions": await _codex_sessions_section(subdirs["codex"]),
        "model_calls": await _model_calls_section(subdirs["mcalls"]),
        "deviz_board": deviz_data,
        "session_storage": await _session_storage_section(subdirs["sessstore"]),
        "webhook_and_window_bundle": await _webhook_and_window_bundle_section(
            subdirs["webhookbundle"]
        ),
        "repo_gaps": await _repo_gaps_section(subdirs["repogaps"]),
        "user_data": await _user_data_section(subdirs["userdata"]),
    }

    redact = {str(tmp_path): "<TMP>"}
    redact.update(deviz_redact)

    messages = compare_or_update("golden_master", mask(golden), redact=redact)
    for m in messages:
        print(m)
    assert not has_failures(messages), "\n".join(messages)
