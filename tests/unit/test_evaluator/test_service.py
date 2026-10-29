"""Tests for EvaluatorService (op-based, DB-backed memory)."""

import json
import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.evaluator.service import EvaluatorService
from src.evaluator.writer import MemoryWriter
from src.storage.database import DatabaseManager
from src.storage.memory_repository import MemoryRepository


@pytest.fixture
async def db(tmp_path):
    mgr = DatabaseManager(str(tmp_path / "mem.db"))
    await mgr.initialize()
    # memory_extractions has a user_id FK; seed the user the tests record under.
    async with mgr.get_connection() as conn:
        await conn.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (123,))
        await conn.commit()
    yield mgr
    await mgr.close()


@pytest.fixture
def repo(db):
    return MemoryRepository(db)


@pytest.fixture
def writer(repo):
    return MemoryWriter(repo)


@pytest.fixture
def mock_tracker():
    return MagicMock()


@pytest.fixture
def notify_callback():
    return AsyncMock()


@pytest.fixture
def service(repo, db, writer, mock_tracker, notify_callback):
    svc = EvaluatorService(
        memory_repo=repo,
        writer=writer,
        token_tracker=mock_tracker,
        db=db,
        notify_callback=notify_callback,
    )
    # Default: reconciler returns no explicit verdicts -> every candidate
    # defaults to "new" (fact-preserving degradation) -> plain adds, which is
    # what the pre-pipeline tests assumed. Pipeline-specific tests override.
    svc._invoke_reconcile_cli = AsyncMock(return_value="[]")
    return svc


async def _extraction_status(db) -> str:
    async with db.get_connection() as conn:
        cur = await conn.execute(
            "SELECT validation_status FROM memory_extractions ORDER BY id DESC LIMIT 1"
        )
        row = await cur.fetchone()
        return row[0] if row else ""


class TestEvaluatorServiceRun:
    """Test EvaluatorService.run() method."""

    async def test_successful_extraction(self, service, repo):
        """CLI returns valid ops -> writer applies -> facts present, success recorded."""
        ops_json = '[{"op":"add","tier":"general","content":"likes spicy food"}]'
        with patch.object(service, "_invoke_cli", new_callable=AsyncMock) as mock_cli:
            mock_cli.return_value = ops_json
            result = await service.run(
                session_id="sess-1",
                user_id=123,
                conversation_text="User: I love spicy food",
                cumulative_tokens=55000,
                agent_id="chef",
            )

        assert result is True
        facts = await repo.get_facts("chef")
        assert [f.content for f in facts] == ["likes spicy food"]

    async def test_facts_scoped_by_agent(self, service, repo):
        """Ops apply under the passed agent_id, not a global default."""
        ops_json = '[{"op":"add","tier":"pinned","content":"no peanuts"}]'
        with patch.object(service, "_invoke_cli", new_callable=AsyncMock) as mock_cli:
            mock_cli.return_value = ops_json
            await service.run(
                session_id="s",
                user_id=1,
                conversation_text="...",
                cumulative_tokens=55000,
                agent_id="chef",
            )
        assert await repo.get_facts("devops") == []
        assert len(await repo.get_facts("chef")) == 1

    async def test_cli_failure_records_failed_and_notifies(
        self, service, db, notify_callback
    ):
        with patch.object(service, "_invoke_cli", new_callable=AsyncMock) as mock_cli:
            mock_cli.side_effect = RuntimeError("Claude CLI failed (exit 1): error")
            result = await service.run(
                session_id="sess-1",
                user_id=123,
                conversation_text="User: test",
                cumulative_tokens=55000,
                agent_id="chef",
            )

        assert result is False
        assert await _extraction_status(db) == "failed"
        notify_callback.assert_called_once()
        assert "failed" in notify_callback.call_args[0][1].lower()

    async def test_validation_failure_records_failed(self, service, db):
        with patch.object(service, "_invoke_cli", new_callable=AsyncMock) as mock_cli:
            mock_cli.return_value = "not valid json at all"
            result = await service.run(
                session_id="sess-1",
                user_id=123,
                conversation_text="User: test",
                cumulative_tokens=55000,
                agent_id="chef",
            )

        assert result is False
        assert await _extraction_status(db) == "failed"

    async def test_empty_extraction_succeeds(self, service, repo):
        with patch.object(service, "_invoke_cli", new_callable=AsyncMock) as mock_cli:
            mock_cli.return_value = "[]"
            result = await service.run(
                session_id="sess-1",
                user_id=123,
                conversation_text="User: test",
                cumulative_tokens=55000,
                agent_id="chef",
            )

        assert result is True
        assert await repo.get_facts("chef") == []

    async def test_successful_extraction_notifies_user(self, service, notify_callback):
        ops_json = '[{"op":"add","tier":"general","content":"insight"}]'
        with patch.object(service, "_invoke_cli", new_callable=AsyncMock) as mock_cli:
            mock_cli.return_value = ops_json
            await service.run(
                session_id="sess-1",
                user_id=123,
                conversation_text="User: hello",
                cumulative_tokens=55000,
                agent_id="chef",
            )

        notify_callback.assert_called_once()
        assert "Memory: 1 found" in notify_callback.call_args[0][1]
        # agent_id rides along so main.py can persist the notification into
        # the profile's session (dv-c5d9 step 5).
        assert notify_callback.call_args[0][2] == "chef"

    async def test_db_record_on_success(self, service, db):
        ops_json = '[{"op":"add","tier":"general","content":"insight"}]'
        with patch.object(service, "_invoke_cli", new_callable=AsyncMock) as mock_cli:
            mock_cli.return_value = ops_json
            await service.run(
                session_id="sess-1",
                user_id=123,
                conversation_text="User: hello",
                cumulative_tokens=55000,
                agent_id="chef",
            )
        assert await _extraction_status(db) == "success"


class TestExtractionIdempotence:
    """dv-6dbc-adjacent: identical prompt must not be extracted twice.

    The prompt embeds the conversation delta AND the agent's current facts, so
    an identical hash means identical input and identical ops -- the CLI call is
    pure cost. 14 of 66 extractions on 2026-08-04 were such duplicates.
    """

    async def test_identical_prompt_skips_second_cli_call(self, service, repo):
        """The checkpoint/rotation pair: same delta, same facts, same prompt.

        Run #1 extracts nothing, so the fact list is unchanged and run #2 builds
        a byte-identical prompt -- exactly the observed duplicate.
        """
        kwargs = dict(
            user_id=123,
            conversation_text="User: nothing memorable here",
            cumulative_tokens=55000,
            agent_id="chef",
        )
        with patch.object(service, "_invoke_cli", new_callable=AsyncMock) as mock_cli:
            mock_cli.return_value = "[]"
            first = await service.run(session_id="sess-1", **kwargs)
            assert mock_cli.await_count == 1

            second = await service.run(session_id="sess-1:refresh", **kwargs)
            assert mock_cli.await_count == 1, "second identical prompt must not hit CLI"

        assert first is True
        assert second is True, "skip reports success so the checkpoint advances"

    async def test_changed_facts_produce_a_new_prompt_and_do_run(self, service, repo):
        """The guard keys on the prompt, not the text -- new facts must re-run."""
        kwargs = dict(
            user_id=77,
            conversation_text="User: I love spicy food",
            cumulative_tokens=55000,
            agent_id="chef",
        )
        with patch.object(service, "_invoke_cli", new_callable=AsyncMock) as mock_cli:
            mock_cli.return_value = (
                '[{"op":"add","tier":"general","content":"likes spicy food"}]'
            )
            await service.run(session_id="s1", **kwargs)
            # run #1 wrote a fact -> current_facts differs -> prompt differs
            await service.run(session_id="s2", **kwargs)
            assert mock_cli.await_count == 2

    async def test_failed_run_is_retried(self, service):
        """Only `success` rows suppress a re-run; a failure must retry."""
        kwargs = dict(
            user_id=5,
            conversation_text="User: hi",
            cumulative_tokens=55000,
            agent_id="chef",
        )
        with patch.object(service, "_invoke_cli", new_callable=AsyncMock) as mock_cli:
            mock_cli.return_value = "not json at all"
            await service.run(session_id="s1", **kwargs)
            await service.run(session_id="s2", **kwargs)
            assert mock_cli.await_count == 2


class TestExtractionInvocation:
    """The delta the extractor is allowed to send."""

    def test_delta_under_the_cap_is_untouched(self, service):
        text = "User: hi\n\nAssistant: hello"
        assert service._bound_delta(text, "chef") == text

    def test_oversized_delta_keeps_the_recent_tail(
        self, repo, db, writer, mock_tracker
    ):
        service = EvaluatorService(
            memory_repo=repo,
            writer=writer,
            token_tracker=mock_tracker,
            db=db,
            max_delta_chars=4_000,
        )
        turns = [f"User: message {i}\n\nAssistant: {'x' * 200}" for i in range(60)]
        bounded = service._bound_delta("\n\n".join(turns), "chef")

        assert len(bounded) <= 4_000 + 100  # cap plus the omission marker
        assert "earlier conversation omitted" in bounded
        assert "message 59" in bounded  # newest kept
        assert "message 0\n" not in bounded  # oldest dropped
        assert bounded.split("\n\n", 1)[1].startswith("User: ")


async def _op_stats(db) -> dict:
    import json

    async with db.get_connection() as conn:
        cur = await conn.execute(
            "SELECT op_stats FROM memory_extractions ORDER BY id DESC LIMIT 1"
        )
        row = await cur.fetchone()
        return json.loads(row[0]) if row and row[0] else {}


class TestReconcilePipeline:
    """dv-c5d9: extract -> reconcile -> map -> apply -> honest report."""

    ADD_OP = '[{"op":"add","tier":"general","content":"drinks tea now"}]'

    async def test_duplicate_verdict_drops_candidate_and_reports_already_saved(
        self, service, repo, db, notify_callback
    ):
        fid = await repo.add_fact("chef", "general", "drinks tea now")
        service._invoke_reconcile_cli = AsyncMock(
            return_value=('[{"index":0,"verdict":"duplicate","existing_id":%d}]' % fid)
        )
        with patch.object(service, "_invoke_cli", new_callable=AsyncMock) as cli:
            cli.return_value = self.ADD_OP
            assert await service.run(
                session_id="s",
                user_id=123,
                conversation_text="User: tea please",
                cumulative_tokens=55000,
                agent_id="chef",
            )
        assert len(await repo.get_facts("chef")) == 1  # nothing added
        stats = await _op_stats(db)
        assert stats["found"] == 1
        assert stats["already_saved"] == 1
        assert stats["added"] == 0
        # D9: a run where everything was already known STILL reports.
        notify_callback.assert_called_once()
        msg = notify_callback.call_args[0][1]
        assert "1 found" in msg and "1 already saved" in msg and "0 new" in msg

    async def test_contradiction_verdict_supersedes(
        self, service, repo, db, notify_callback
    ):
        old_id = await repo.add_fact("chef", "general", "drinks coffee")
        service._invoke_reconcile_cli = AsyncMock(
            return_value=(
                '[{"index":0,"verdict":"contradicts","existing_id":%d}]' % old_id
            )
        )
        with patch.object(service, "_invoke_cli", new_callable=AsyncMock) as cli:
            cli.return_value = self.ADD_OP
            await service.run(
                session_id="s",
                user_id=123,
                conversation_text="User: switched to tea",
                cumulative_tokens=55000,
                agent_id="chef",
            )
        active = await repo.get_facts("chef")
        assert [f.content for f in active] == ["drinks tea now"]
        closed = await repo.get_facts("chef", status="superseded")
        assert closed[0].id == old_id
        assert closed[0].superseded_by == active[0].id
        stats = await _op_stats(db)
        assert stats["superseded"] == 1 and stats["added"] == 0
        assert "1 superseded" in notify_callback.call_args[0][1]

    async def test_safety_contradiction_keeps_both_and_flags_conflict(
        self, service, repo, db, notify_callback
    ):
        old_id = await repo.add_fact("chef", "pinned", "allergic to peanuts")
        service._invoke_reconcile_cli = AsyncMock(
            return_value=(
                '[{"index":0,"verdict":"contradicts","existing_id":%d,'
                '"safety":true}]' % old_id
            )
        )
        with patch.object(service, "_invoke_cli", new_callable=AsyncMock) as cli:
            cli.return_value = (
                '[{"op":"add","tier":"pinned","content":"peanuts are fine now"}]'
            )
            await service.run(
                session_id="s",
                user_id=123,
                conversation_text="User: peanuts are fine now",
                cumulative_tokens=55000,
                agent_id="chef",
            )
        active = await repo.get_facts("chef")
        assert len(active) == 2  # both stay active — never pick a winner
        groups = {f.conflict_group for f in active}
        assert groups == {str(min(f.id for f in active))}
        stats = await _op_stats(db)
        assert stats["conflicts_flagged"] == 1
        assert "safety conflict" in notify_callback.call_args[0][1]

    async def test_extraction_supersede_is_demoted_to_hint(self, service, repo):
        """R5: extraction's supersede is a hint; verdict 'new' overrides it."""
        old_id = await repo.add_fact("chef", "general", "runs on Mondays")
        service._invoke_reconcile_cli = AsyncMock(
            return_value='[{"index":0,"verdict":"new"}]'
        )
        ops = (
            '[{"op":"supersede","supersedes_id":%d,"tier":"general",'
            '"content":"also swims on Fridays"}]' % old_id
        )
        with patch.object(service, "_invoke_cli", new_callable=AsyncMock) as cli:
            cli.return_value = ops
            await service.run(
                session_id="s",
                user_id=123,
                conversation_text="User: I also swim",
                cumulative_tokens=55000,
                agent_id="chef",
            )
        active = await repo.get_facts("chef")
        assert {f.content for f in active} == {
            "runs on Mondays",
            "also swims on Fridays",
        }  # old fact NOT superseded

    async def test_reconciler_failure_falls_back_to_hints(self, service, repo, db):
        """A reconciler outage must not drop facts: hints are trusted (v1)."""
        old_id = await repo.add_fact("chef", "general", "runs on Mondays")
        service._invoke_reconcile_cli = AsyncMock(
            side_effect=RuntimeError("timeout after 120s (claude cli)")
        )
        ops = (
            '[{"op":"supersede","supersedes_id":%d,"tier":"general",'
            '"content":"runs on Tuesdays now"}]' % old_id
        )
        with patch.object(service, "_invoke_cli", new_callable=AsyncMock) as cli:
            cli.return_value = ops
            assert await service.run(
                session_id="s",
                user_id=123,
                conversation_text="User: tuesdays now",
                cumulative_tokens=55000,
                agent_id="chef",
            )
        active = await repo.get_facts("chef")
        assert [f.content for f in active] == ["runs on Tuesdays now"]
        stats = await _op_stats(db)
        assert stats["reconcile"].startswith("failed:")

    async def test_slice_saved_remember_fact_counts_as_already_saved(
        self, service, repo, db
    ):
        """Step 1: a fact remember() wrote during the slice shows up as
        'already saved', distinguished in op_stats."""
        fid = await repo.add_fact(
            "chef", "general", "drinks tea now", source="agent_tool"
        )
        service._invoke_reconcile_cli = AsyncMock(
            return_value=('[{"index":0,"verdict":"duplicate","existing_id":%d}]' % fid)
        )
        with patch.object(service, "_invoke_cli", new_callable=AsyncMock) as cli:
            cli.return_value = self.ADD_OP
            await service.run(
                session_id="s",
                user_id=123,
                conversation_text="User: tea",
                cumulative_tokens=55000,
                agent_id="chef",
            )
        stats = await _op_stats(db)
        assert stats["already_saved"] == 1
        assert stats["slice_saved_matches"] == 1

    async def test_empty_extraction_skips_reconciler_and_notify(
        self, service, notify_callback
    ):
        reconcile = AsyncMock(return_value="[]")
        service._invoke_reconcile_cli = reconcile
        with patch.object(service, "_invoke_cli", new_callable=AsyncMock) as cli:
            cli.return_value = "[]"
            await service.run(
                session_id="s",
                user_id=123,
                conversation_text="User: nothing here",
                cumulative_tokens=55000,
                agent_id="chef",
            )
        reconcile.assert_not_called()
        notify_callback.assert_not_called()


class TestEpisodicGuardWiring:
    """dv-7517 step 3: episodic candidates rejected before reconciliation,
    counted in op_stats.rejected_episodic, visible in the report."""

    MEAL_OP = (
        '[{"op":"add","tier":"general","content":'
        '"08.08 breakfast: 2 eggs + bread 60g — ~542 kcal"},'
        '{"op":"add","tier":"general","content":"allergic to walnuts"}]'
    )

    async def test_episodic_candidate_rejected_counted_and_reported(
        self, service, repo, db, notify_callback
    ):
        reconcile = AsyncMock(return_value="[]")
        service._invoke_reconcile_cli = reconcile
        with patch.object(service, "_invoke_cli", new_callable=AsyncMock) as cli:
            cli.return_value = self.MEAL_OP
            assert await service.run(
                session_id="s",
                user_id=123,
                conversation_text="User: logged breakfast",
                cumulative_tokens=55000,
                agent_id="coach",
            )
        # Only the durable fact was written; the meal line never reached
        # reconciliation or the writer.
        assert [f.content for f in await repo.get_facts("coach")] == [
            "allergic to walnuts"
        ]
        reconcile_prompt = reconcile.call_args[0][0]
        assert "542 kcal" not in reconcile_prompt
        stats = await _op_stats(db)
        assert stats["found"] == 2
        assert stats["rejected_episodic"] == 1
        assert stats["rejected_episodic_detail"][0]["reason"] == "single meal log"
        assert "542 kcal" in stats["rejected_episodic_detail"][0]["content"]
        msg = notify_callback.call_args[0][1]
        assert "1 episodic rejected" in msg

    async def test_run_with_only_episodic_candidates_still_reports(
        self, service, repo, db, notify_callback
    ):
        op = (
            '[{"op":"add","tier":"general","content":'
            '"Day 10.08 nutrition tracking closed: total ~1703 kcal"}]'
        )
        with patch.object(service, "_invoke_cli", new_callable=AsyncMock) as cli:
            cli.return_value = op
            assert await service.run(
                session_id="s",
                user_id=123,
                conversation_text="User: day summary",
                cumulative_tokens=55000,
                agent_id="coach",
            )
        assert await repo.get_facts("coach") == []
        notify_callback.assert_called_once()
        assert "1 episodic rejected" in notify_callback.call_args[0][1]

    async def test_kill_switch_passes_episodic_through(
        self, repo, db, writer, mock_tracker
    ):
        svc = EvaluatorService(
            memory_repo=repo,
            writer=writer,
            token_tracker=mock_tracker,
            db=db,
            episodic_guard_enabled=False,
        )
        svc._invoke_reconcile_cli = AsyncMock(return_value="[]")
        with patch.object(svc, "_invoke_cli", new_callable=AsyncMock) as cli:
            cli.return_value = self.MEAL_OP
            assert await svc.run(
                session_id="s",
                user_id=123,
                conversation_text="User: logged breakfast",
                cumulative_tokens=55000,
                agent_id="coach",
            )
        contents = {f.content for f in await repo.get_facts("coach")}
        assert "08.08 breakfast: 2 eggs + bread 60g — ~542 kcal" in contents
        stats = await _op_stats(db)
        assert stats["rejected_episodic"] == 0

    async def test_pinned_candidate_never_rejected(
        self, service, repo, db, notify_callback
    ):
        op = (
            '[{"op":"add","tier":"pinned","content":'
            '"12.08 dinner reaction: allergic swelling after peanuts ~200 kcal"}]'
        )
        with patch.object(service, "_invoke_cli", new_callable=AsyncMock) as cli:
            cli.return_value = op
            await service.run(
                session_id="s",
                user_id=123,
                conversation_text="User: reaction",
                cumulative_tokens=55000,
                agent_id="coach",
            )
        assert len(await repo.get_facts("coach")) == 1
        stats = await _op_stats(db)
        assert stats["rejected_episodic"] == 0


class TestEvaluatorTrigger:
    """Test evaluator trigger conditions via ClaudeSession."""

    def test_should_run_at_50k(self):
        """Session at 55k tokens with checkpoint 0 should trigger evaluator."""
        from datetime import UTC, datetime

        from src.claude.session import ClaudeSession

        session = ClaudeSession(
            session_id="s1",
            user_id=1,
            project_path=Path("/tmp"),
            created_at=datetime.now(UTC),
            last_used=datetime.now(UTC),
            cumulative_tokens=55000,
            last_eval_checkpoint=0,
        )
        assert session.should_run_evaluator() is True

    def test_should_not_run_below_threshold(self):
        """Session at 30k tokens should not trigger evaluator."""
        from datetime import UTC, datetime

        from src.claude.session import ClaudeSession

        session = ClaudeSession(
            session_id="s1",
            user_id=1,
            project_path=Path("/tmp"),
            created_at=datetime.now(UTC),
            last_used=datetime.now(UTC),
            cumulative_tokens=30000,
            last_eval_checkpoint=0,
        )
        assert session.should_run_evaluator() is False

    def test_should_not_run_after_mark(self):
        """Session already marked at checkpoint 1 should not re-trigger."""
        from datetime import UTC, datetime

        from src.claude.session import ClaudeSession

        session = ClaudeSession(
            session_id="s1",
            user_id=1,
            project_path=Path("/tmp"),
            created_at=datetime.now(UTC),
            last_used=datetime.now(UTC),
            cumulative_tokens=55000,
            last_eval_checkpoint=1,
        )
        assert session.should_run_evaluator() is False


class TestTaskStatusIsNotAFact:
    """dv-cf07 phase 1: the extraction prompt tells the model to skip pure
    task status; decisions, findings, lessons and owner rules still land."""

    async def _run_capturing_prompt(self, service, ops_json: str) -> str:
        with patch.object(service, "_invoke_cli", new_callable=AsyncMock) as mock_cli:
            mock_cli.return_value = ops_json
            ok = await service.run(
                session_id="s-status",
                user_id=123,
                conversation_text="User: please keep notes on the pump work",
                cumulative_tokens=55000,
                agent_id="devops",
            )
        assert ok is True
        return str(mock_cli.await_args.args[0])

    async def test_extraction_prompt_skips_merged_started_raised(self, service):
        prompt = await self._run_capturing_prompt(service, "[]")
        # Only the instructions count, never the conversation being scanned.
        lower = prompt.lower().split("## conversation")[0]
        # One rule bullet names the status shapes the evaluator kept writing.
        bullets = re.split(r"\n\s*- ", lower)
        status_rule = [b for b in bullets if "merged" in b]
        assert status_rule, "no rule mentions 'merged'"
        rule = status_rule[0]
        for word in ("started", "raised", "status"):
            assert word in rule, word
        # The general tier no longer reads as permission to store status.
        assert "history, status" not in lower

    async def test_decisions_findings_lessons_owner_rules_still_extracted(
        self, service, repo
    ):
        ops = [
            {
                "op": "add",
                "kind": "conversation_result",
                "tier": "general",
                "content": "Decision: the stale-fact sweep lives in ConsolidationService",
            },
            {
                "op": "add",
                "kind": "conversation_result",
                "tier": "general",
                "content": "Finding: the remember tool bypasses the reconciler",
            },
            {
                "op": "add",
                "kind": "user_fact",
                "tier": "general",
                "content": "Lesson: run make test-core before every restart",
            },
            {
                "op": "add",
                "kind": "user_fact",
                "tier": "pinned",
                "content": "Never run git stash in the shared working tree",
            },
        ]
        prompt = await self._run_capturing_prompt(service, json.dumps(ops))
        # The prompt still invites decisions as facts.
        assert "decision" in prompt.lower()

        facts = await repo.get_facts("devops")
        stored = {(f.content, f.kind, f.tier) for f in facts}
        for op in ops:
            assert (op["content"], op["kind"], op["tier"]) in stored, op["content"]
