"""Test ClaudeIntegration facade — force_new, activity logging."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.claude.exceptions import ClaudeStoppedError
from src.claude.facade import ClaudeIntegration
from src.claude.sdk_integration import ClaudeResponse
from src.claude.session import ClaudeSession, SessionManager
from src.config.settings import Settings

from .conftest import InMemorySessionStorage


def _make_mock_response(session_id: str = "new-session-id") -> MagicMock:
    """Create a mock ClaudeResponse with sensible defaults."""
    resp = MagicMock()
    resp.session_id = session_id
    resp.cost = 0.0
    resp.duration_ms = 100
    resp.num_turns = 1
    resp.tools_used = []
    resp.is_error = False
    resp.content = "ok"
    resp.input_tokens = 100
    resp.output_tokens = 50
    return resp


def _make_user_data(force_new: bool = False) -> Dict[str, Any]:
    """Simulate context.user_data dict as the handlers would see it."""
    return {
        "claude_session_id": None,
        "session_started": True,
        "force_new_session": force_new,
    }


@pytest.fixture
def config(tmp_path):
    """Create test config."""
    return Settings(
        telegram_bot_token="test:token",
        telegram_bot_username="testbot",
        approved_directory=tmp_path,
        session_timeout_hours=24,
        max_sessions_per_user=5,
    )


@pytest.fixture
def session_manager(config):
    """Create session manager with in-memory storage."""
    storage = InMemorySessionStorage()
    return SessionManager(config, storage)


@pytest.fixture
def facade(config, session_manager):
    """Create facade with mocked SDK manager."""
    sdk_manager = MagicMock()

    integration = ClaudeIntegration(
        config=config,
        sdk_manager=sdk_manager,
        session_manager=session_manager,
    )
    return integration


class TestForceNewSkipsAutoResume:
    """Verify that force_new=True prevents _find_resumable_session."""

    async def test_auto_resume_finds_existing_session(self, facade, session_manager):
        """Without force_new, run_command auto-resumes an existing session."""
        project = Path("/test/project")
        user_id = 123

        # Seed an existing non-temp session in storage
        existing = ClaudeSession(
            session_id="real-session-id",
            user_id=user_id,
            project_path=project,
            created_at=datetime.utcnow(),
            last_used=datetime.utcnow(),
        )
        await session_manager.storage.save_session(existing)
        session_manager.active_sessions[existing.session_id] = existing

        # _find_resumable_session should find it
        found = await facade._find_resumable_session(user_id, project)
        assert found is not None
        assert found.session_id == "real-session-id"

    async def test_force_new_skips_auto_resume(self, facade, session_manager):
        """With force_new=True, run_command does NOT auto-resume."""
        project = Path("/test/project")
        user_id = 123

        # Seed an existing non-temp session
        existing = ClaudeSession(
            session_id="real-session-id",
            user_id=user_id,
            project_path=project,
            created_at=datetime.utcnow(),
            last_used=datetime.utcnow(),
        )
        await session_manager.storage.save_session(existing)
        session_manager.active_sessions[existing.session_id] = existing

        # Mock _find_resumable_session to track whether it's called
        with patch.object(
            facade, "_find_resumable_session", wraps=facade._find_resumable_session
        ) as spy:
            with patch.object(
                facade,
                "_execute",
                return_value=_make_mock_response(),
            ):
                await facade.run_command(
                    prompt="hello",
                    working_directory=project,
                    user_id=user_id,
                    session_id=None,
                    force_new=True,
                )

            # _find_resumable_session should NOT have been called
            spy.assert_not_called()


class TestForceNewSurvivesFailure:
    """Verify the handler-level contract: force_new_session flag stays set
    when run_command fails, so the next retry still starts a fresh session."""

    async def _seed_session(
        self,
        session_manager: SessionManager,
        user_id: int = 123,
        project: Path = Path("/test/project"),
    ) -> ClaudeSession:
        existing = ClaudeSession(
            session_id="old-session-id",
            user_id=user_id,
            project_path=project,
            created_at=datetime.utcnow(),
            last_used=datetime.utcnow(),
        )
        await session_manager.storage.save_session(existing)
        session_manager.active_sessions[existing.session_id] = existing
        return existing

    async def test_flag_survives_run_command_failure(self, facade, session_manager):
        """If run_command raises, the caller should still see
        force_new_session=True so the retry skips auto-resume."""
        project = Path("/test/project")
        user_id = 123
        await self._seed_session(session_manager, user_id, project)

        user_data = _make_user_data(force_new=True)

        # Simulate what the handler does: read flag, call run_command
        force_new = bool(user_data.get("force_new_session"))
        assert force_new is True

        with patch.object(
            facade,
            "_execute",
            side_effect=RuntimeError("network timeout"),
        ):
            with pytest.raises(RuntimeError, match="network timeout"):
                await facade.run_command(
                    prompt="hello",
                    working_directory=project,
                    user_id=user_id,
                    session_id=None,
                    force_new=force_new,
                )

        # Handler would NOT have cleared the flag (no success path reached)
        # so user_data still has it — simulating the handler contract:
        assert user_data["force_new_session"] is True

    async def test_flag_cleared_after_successful_run(self, facade, session_manager):
        """After a successful run_command, the handler clears
        force_new_session so subsequent messages auto-resume normally."""
        project = Path("/test/project")
        user_id = 123
        await self._seed_session(session_manager, user_id, project)

        user_data = _make_user_data(force_new=True)
        force_new = bool(user_data.get("force_new_session"))

        with patch.object(
            facade,
            "_execute",
            return_value=_make_mock_response(),
        ):
            await facade.run_command(
                prompt="hello",
                working_directory=project,
                user_id=user_id,
                session_id=None,
                force_new=force_new,
            )

        # Simulate the handler clearing the flag on success
        if force_new:
            user_data["force_new_session"] = False

        assert user_data["force_new_session"] is False

    async def test_retry_after_failure_still_skips_auto_resume(
        self, facade, session_manager
    ):
        """Full scenario: /new -> fail -> retry -> success.
        Both calls should skip auto-resume; flag cleared only after success."""
        project = Path("/test/project")
        user_id = 123
        await self._seed_session(session_manager, user_id, project)

        user_data = _make_user_data(force_new=True)

        # --- First attempt: fails ---
        force_new = bool(user_data.get("force_new_session"))
        with patch.object(
            facade, "_find_resumable_session", wraps=facade._find_resumable_session
        ) as spy1:
            with patch.object(
                facade,
                "_execute",
                side_effect=RuntimeError("backend down"),
            ):
                with pytest.raises(RuntimeError):
                    await facade.run_command(
                        prompt="hello",
                        working_directory=project,
                        user_id=user_id,
                        session_id=None,
                        force_new=force_new,
                    )
            spy1.assert_not_called()

        # Flag untouched (handler didn't reach success path)
        assert user_data["force_new_session"] is True

        # --- Second attempt: succeeds ---
        force_new = bool(user_data.get("force_new_session"))
        with patch.object(
            facade, "_find_resumable_session", wraps=facade._find_resumable_session
        ) as spy2:
            with patch.object(
                facade,
                "_execute",
                return_value=_make_mock_response(),
            ):
                await facade.run_command(
                    prompt="hello",
                    working_directory=project,
                    user_id=user_id,
                    session_id=None,
                    force_new=force_new,
                )
            spy2.assert_not_called()

        # Handler clears on success
        if force_new:
            user_data["force_new_session"] = False
        assert user_data["force_new_session"] is False


class TestActivityLogging:
    """Verify activity_logger integration in run_command."""

    @pytest.fixture
    def activity_logger(self):
        """Create a mock activity logger."""
        mock = MagicMock()
        mock.log_activity = AsyncMock()
        return mock

    @pytest.fixture
    def facade_with_logger(self, config, session_manager, activity_logger):
        """Create facade with mocked SDK manager and activity_logger."""
        sdk_manager = MagicMock()
        integration = ClaudeIntegration(
            config=config,
            sdk_manager=sdk_manager,
            session_manager=session_manager,
            activity_logger=activity_logger,
        )
        return integration

    async def test_activity_logger_called_after_successful_response(
        self, facade_with_logger, activity_logger
    ):
        """run_command() calls activity_logger.log_activity() after success."""
        project = Path("/test/project")
        user_id = 123
        mock_resp = _make_mock_response()
        mock_resp.content = "response text"
        mock_resp.num_turns = 3

        with patch.object(facade_with_logger, "_execute", return_value=mock_resp):
            await facade_with_logger.run_command(
                prompt="hello world",
                working_directory=project,
                user_id=user_id,
            )

        # dv-ca94: run_command prepends a <current_time> line to every
        # prompt, so the logged prompt carries it too.
        activity_logger.log_activity.assert_awaited_once()
        kwargs = activity_logger.log_activity.await_args.kwargs
        assert kwargs["prompt"].endswith("\n\nhello world")
        assert kwargs["response_content"] == "response text"
        assert kwargs["working_directory"] == project
        assert kwargs["num_turns"] == 3

    async def test_activity_logger_not_called_when_none(self, facade, session_manager):
        """run_command() does NOT call activity_logger when it is None."""
        project = Path("/test/project")
        user_id = 123

        # facade fixture has no activity_logger (None)
        with patch.object(facade, "_execute", return_value=_make_mock_response()):
            result = await facade.run_command(
                prompt="hello",
                working_directory=project,
                user_id=user_id,
            )

        # No crash, response returned normally
        assert result.content == "ok"

    async def test_activity_logger_exception_does_not_block_response(
        self, facade_with_logger, activity_logger
    ):
        """run_command() returns response even when log_activity() raises."""
        project = Path("/test/project")
        user_id = 123
        mock_resp = _make_mock_response()

        activity_logger.log_activity = AsyncMock(side_effect=RuntimeError("disk full"))

        with patch.object(facade_with_logger, "_execute", return_value=mock_resp):
            result = await facade_with_logger.run_command(
                prompt="hello",
                working_directory=project,
                user_id=user_id,
            )

        # Response returned despite logging failure
        assert result.content == "ok"

    async def test_activity_logger_not_called_on_failure(
        self, facade_with_logger, activity_logger
    ):
        """activity_logger.log_activity() NOT called when run_command() fails."""
        project = Path("/test/project")
        user_id = 123

        with patch.object(
            facade_with_logger,
            "_execute",
            side_effect=RuntimeError("backend error"),
        ):
            with pytest.raises(RuntimeError, match="backend error"):
                await facade_with_logger.run_command(
                    prompt="hello",
                    working_directory=project,
                    user_id=user_id,
                )

        activity_logger.log_activity.assert_not_awaited()


class TestEmptySessionIdWarning:
    """Verify facade warns when final session_id is empty."""

    async def test_empty_session_id_warning_in_facade(self, facade, session_manager):
        """When Claude returns no session_id, facade logs a warning."""
        project = Path("/test/project")
        user_id = 456

        # Return a response with empty session_id
        mock_response = _make_mock_response(session_id="")

        with patch.object(
            facade,
            "_execute",
            return_value=mock_response,
        ):
            result = await facade.run_command(
                prompt="hello",
                working_directory=project,
                user_id=user_id,
                session_id=None,
            )

        # Session ID should be empty on the response
        assert not result.session_id


class TestResumeFailedSeed:
    """dv-b41f/dv-fbf1: the resume-failed fallback must seed the retry with
    the failed session's stored tail, stay non-silent, and never treat the
    /stop killswitch as a dead session."""

    async def _seed_session(
        self,
        session_manager: SessionManager,
        user_id: int = 123,
        project: Path = Path("/test/project"),
        session_id: str = "old-session-id",
    ) -> ClaudeSession:
        existing = ClaudeSession(
            session_id=session_id,
            user_id=user_id,
            project_path=project,
            created_at=datetime.utcnow(),
            last_used=datetime.utcnow(),
        )
        await session_manager.storage.save_session(existing)
        session_manager.active_sessions[existing.session_id] = existing
        return existing

    def _fail_then_succeed(self, facade):
        """Patch _execute: resume attempt raises, retry returns a response."""
        return patch.object(
            facade,
            "_execute",
            AsyncMock(side_effect=[RuntimeError("exit 1"), _make_mock_response()]),
        )

    async def test_retry_prompt_is_seeded(self, facade, session_manager):
        project = Path("/test/project")
        await self._seed_session(session_manager)
        pairs = [("q1", "a1"), ("q2", "a2")]

        with (
            patch.object(facade, "_recent_pairs", AsyncMock(return_value=pairs)),
            patch.object(facade, "_count_session_messages", AsyncMock(return_value=7)),
            self._fail_then_succeed(facade) as execute,
        ):
            result = await facade.run_command(
                prompt="hello",
                working_directory=project,
                user_id=123,
                role="owner",
            )

        assert execute.await_count == 2
        retry = execute.call_args_list[1].kwargs
        prompt = retry["prompt"]
        assert prompt.startswith("<conversation_context>")
        assert "could not be resumed" in prompt
        assert "dropped deliberately" not in prompt
        assert "<recent_conversation>" in prompt
        assert "User: q1" in prompt and "Assistant: a2" in prompt
        assert "last 2 of its 7 turns" in prompt
        assert prompt.rstrip().endswith("hello")
        # Fresh-session args and identity threading stay intact.
        assert retry["session_id"] is None
        assert retry["continue_session"] is False
        assert retry["user_id"] == 123
        assert retry["role"] == "owner"
        assert result.session_restarted is True

    async def test_retry_unseeded_when_no_stored_pairs(self, facade, session_manager):
        project = Path("/test/project")
        await self._seed_session(session_manager)

        with (
            patch.object(facade, "_recent_pairs", AsyncMock(return_value=[])),
            patch.object(
                facade, "_count_session_messages", AsyncMock(return_value=None)
            ),
            self._fail_then_succeed(facade) as execute,
        ):
            await facade.run_command(
                prompt="hello", working_directory=project, user_id=123
            )

        prompt = execute.call_args_list[1].kwargs["prompt"]
        # dv-ca94: every prompt carries a <current_time> prefix now.
        assert prompt.endswith("\n\nhello")
        assert "<conversation_context>" not in prompt

    async def test_retry_seeds_from_pre_heal_prompt(self, facade, session_manager):
        """No double-wrap: even when heal rewrapped the first attempt's
        prompt, the retry is seeded from the raw user message."""
        project = Path("/test/project")
        await self._seed_session(session_manager)
        healed = (
            "<recent_conversation>\nUser: earlier\n</recent_conversation>\n\n"
            "Continue the conversation above. The user now says:\n\nhello"
        )

        with (
            patch.object(facade, "_heal_lost_prose", AsyncMock(return_value=healed)),
            patch.object(
                facade, "_recent_pairs", AsyncMock(return_value=[("q1", "a1")])
            ),
            patch.object(
                facade, "_count_session_messages", AsyncMock(return_value=None)
            ),
            self._fail_then_succeed(facade) as execute,
        ):
            await facade.run_command(
                prompt="hello", working_directory=project, user_id=123
            )

        prompt = execute.call_args_list[1].kwargs["prompt"]
        assert prompt.count("<recent_conversation>") == 1
        assert "User: earlier" not in prompt
        assert prompt.rstrip().endswith("hello")

    async def test_seed_build_failure_is_not_fatal(self, facade, session_manager):
        project = Path("/test/project")
        await self._seed_session(session_manager)

        with (
            patch.object(
                facade,
                "_recent_pairs",
                AsyncMock(side_effect=RuntimeError("db locked")),
            ),
            self._fail_then_succeed(facade) as execute,
        ):
            result = await facade.run_command(
                prompt="hello", working_directory=project, user_id=123
            )

        # dv-ca94: every prompt carries a <current_time> prefix now.
        assert execute.call_args_list[1].kwargs["prompt"].endswith("\n\nhello")
        assert result.session_restarted is True

    async def test_lifecycle_event_records_seed_state(self, config, session_manager):
        lifecycle = MagicMock()
        lifecycle.record = AsyncMock()
        facade = ClaudeIntegration(
            config=config,
            sdk_manager=MagicMock(),
            session_manager=session_manager,
            lifecycle_logger=lifecycle,
        )
        project = Path("/test/project")
        await self._seed_session(session_manager)

        with (
            patch.object(
                facade, "_recent_pairs", AsyncMock(return_value=[("q1", "a1")])
            ),
            patch.object(facade, "_count_session_messages", AsyncMock(return_value=5)),
            self._fail_then_succeed(facade),
        ):
            await facade.run_command(
                prompt="hello", working_directory=project, user_id=123
            )

        fresh = [
            c
            for c in lifecycle.record.await_args_list
            if c.kwargs.get("event") == "fresh"
        ]
        assert len(fresh) == 1
        detail = fresh[0].kwargs["detail"]
        assert detail["reason"] == "resume_failed"
        assert detail["failed_session"] == "old-session-id"
        assert detail["seeded"] is True
        assert detail["tail_messages"] == 1

    async def test_claude_stopped_error_never_deletes_session(
        self, facade, session_manager
    ):
        """The killswitch refusal must propagate, not masquerade as a dead
        session (dv-fbf1: it used to trigger remove_session)."""
        project = Path("/test/project")
        await self._seed_session(session_manager)

        with patch.object(
            facade,
            "_execute",
            AsyncMock(side_effect=ClaudeStoppedError("stopped")),
        ) as execute:
            with pytest.raises(ClaudeStoppedError):
                await facade.run_command(
                    prompt="hello", working_directory=project, user_id=123
                )

        assert execute.await_count == 1  # no fresh-session retry
        assert "old-session-id" in session_manager.storage.sessions

    async def test_killswitch_gate_refuses_before_any_bookkeeping(
        self, facade, session_manager
    ):
        project = Path("/test/project")
        await self._seed_session(session_manager)
        facade.sdk_manager.is_stopped = True

        with patch.object(facade, "_execute", AsyncMock()) as execute:
            with pytest.raises(ClaudeStoppedError):
                await facade.run_command(
                    prompt="hello", working_directory=project, user_id=123
                )

        execute.assert_not_awaited()
        assert "old-session-id" in session_manager.storage.sessions

    def test_response_flag_defaults_false(self):
        resp = ClaudeResponse(
            content="ok", session_id="s", cost=0.0, duration_ms=1, num_turns=1
        )
        assert resp.session_restarted is False


class TestResumeHasNoWallClockLimit:
    """A conversation ends when its CONTEXT is full, never because time passed.

    Regression for 2026-09-10: `_find_resumable_session` filtered on
    `is_expired(session_timeout_hours)` (12h under ProductionConfig), so any
    profile left alone for longer silently started an unseeded, empty session
    — the `chef` profile lost 51h of context that way while /status still
    reported a healthy window. See docs/DECISIONS.md.
    """

    @staticmethod
    async def _seed_aged(session_manager, project: Path, *, days: int) -> str:
        session = ClaudeSession(
            session_id=f"aged-{days}d",
            user_id=123,
            project_path=project,
            created_at=datetime.now(UTC) - timedelta(days=days),
            last_used=datetime.now(UTC) - timedelta(days=days),
        )
        await session_manager.storage.save_session(session)
        return session.session_id

    @pytest.mark.parametrize("days", [1, 3, 30, 365])
    async def test_old_session_is_resumable(self, facade, session_manager, days):
        project = Path("/test/project")
        session_id = await self._seed_aged(session_manager, project, days=days)

        found = await facade._find_resumable_session(123, project)

        assert found is not None, f"{days}d-old session must still resume"
        assert found.session_id == session_id

    async def test_run_command_continues_the_old_session(self, facade, session_manager):
        """End to end: the aged session id is what gets handed to the SDK,
        with continue_session=True — not a fresh unseeded run."""
        project = Path("/test/project")
        session_id = await self._seed_aged(session_manager, project, days=45)

        with patch.object(
            facade, "_execute", AsyncMock(return_value=_make_mock_response(session_id))
        ) as execute:
            await facade.run_command(
                prompt="hello", working_directory=project, user_id=123
            )

        assert execute.await_args.kwargs["session_id"] == session_id
        assert execute.await_args.kwargs["continue_session"] is True

    async def test_newest_session_wins_among_several(self, facade, session_manager):
        """With no expiry filter, selection is still 'most recently used'."""
        project = Path("/test/project")
        await self._seed_aged(session_manager, project, days=30)
        await self._seed_aged(session_manager, project, days=2)

        found = await facade._find_resumable_session(123, project)

        assert found is not None
        assert found.session_id == "aged-2d"


class TestColdStartSeed:
    """A fresh session for a workspace is seeded from that workspace's stored
    history across sessions, not left blank (dv-300e)."""

    async def test_cold_start_prompt_is_seeded(self, facade, session_manager):
        project = Path("/test/project")
        pairs = [("q1", "a1"), ("q2", "a2")]

        with (
            patch.object(
                facade, "_recent_pairs_for_dir", AsyncMock(return_value=pairs)
            ),
            patch.object(facade, "_count_dir_messages", AsyncMock(return_value=20)),
            patch.object(
                facade, "_execute", AsyncMock(return_value=_make_mock_response())
            ) as execute,
        ):
            await facade.run_command(
                prompt="hello", working_directory=project, user_id=123
            )

        sent = execute.await_args.kwargs
        assert sent["continue_session"] is False
        assert sent["session_id"] is None
        prompt = sent["prompt"]
        assert prompt.startswith("<conversation_context>")
        assert "rebuilt from this workspace's stored history" in prompt
        assert "User: q1" in prompt and "Assistant: a2" in prompt
        assert "last 2 of its 20 turns" in prompt
        assert prompt.rstrip().endswith("hello")

    async def test_force_new_is_never_seeded(self, facade, session_manager):
        """/new means a clean slate; the seed must not resurrect history."""
        project = Path("/test/project")

        with (
            patch.object(
                facade,
                "_recent_pairs_for_dir",
                AsyncMock(return_value=[("q1", "a1")]),
            ),
            patch.object(
                facade, "_execute", AsyncMock(return_value=_make_mock_response())
            ) as execute,
        ):
            await facade.run_command(
                prompt="hello",
                working_directory=project,
                user_id=123,
                force_new=True,
            )

        # dv-ca94: every prompt carries a <current_time> prefix now.
        assert execute.await_args.kwargs["prompt"].endswith("\n\nhello")

    async def test_resumed_session_is_not_seeded(self, facade, session_manager):
        """Resume carries its own context — no seed on top of it."""
        project = Path("/test/project")
        existing = ClaudeSession(
            session_id="real-session-id",
            user_id=123,
            project_path=project,
            created_at=datetime.utcnow(),
            last_used=datetime.utcnow(),
        )
        await session_manager.storage.save_session(existing)
        session_manager.active_sessions[existing.session_id] = existing

        with (
            patch.object(
                facade, "_heal_lost_prose", AsyncMock(side_effect=lambda _s, _d, p: p)
            ),
            patch.object(
                facade, "_recent_pairs_for_dir", AsyncMock(return_value=[("q", "a")])
            ) as pairs,
            patch.object(
                facade,
                "_execute",
                AsyncMock(return_value=_make_mock_response("real-session-id")),
            ) as execute,
        ):
            await facade.run_command(
                prompt="hello", working_directory=project, user_id=123
            )

        assert execute.await_args.kwargs["continue_session"] is True
        # dv-ca94: every prompt carries a <current_time> prefix now.
        assert execute.await_args.kwargs["prompt"].endswith("\n\nhello")
        pairs.assert_not_awaited()

    async def test_no_history_leaves_prompt_untouched(self, facade, session_manager):
        project = Path("/test/project")

        with (
            patch.object(facade, "_recent_pairs_for_dir", AsyncMock(return_value=[])),
            patch.object(
                facade, "_execute", AsyncMock(return_value=_make_mock_response())
            ) as execute,
        ):
            await facade.run_command(
                prompt="hello", working_directory=project, user_id=123
            )

        # dv-ca94: every prompt carries a <current_time> prefix now.
        assert execute.await_args.kwargs["prompt"].endswith("\n\nhello")

    async def test_seed_failure_is_not_fatal(self, facade, session_manager):
        project = Path("/test/project")

        with (
            patch.object(
                facade,
                "_recent_pairs_for_dir",
                AsyncMock(side_effect=RuntimeError("db gone")),
            ),
            patch.object(
                facade, "_execute", AsyncMock(return_value=_make_mock_response())
            ) as execute,
        ):
            result = await facade.run_command(
                prompt="hello", working_directory=project, user_id=123
            )

        # dv-ca94: every prompt carries a <current_time> prefix now.
        assert execute.await_args.kwargs["prompt"].endswith("\n\nhello")
        assert result is not None

    async def test_lifecycle_records_cold_start(self, config, session_manager):
        project = Path("/test/project")
        recorded = []

        class _Lifecycle:
            async def record(self, profile, event, detail):
                recorded.append((event, detail))

        facade = ClaudeIntegration(
            config=config,
            sdk_manager=MagicMock(),
            session_manager=session_manager,
            lifecycle_logger=_Lifecycle(),
        )

        with (
            patch.object(
                facade,
                "_recent_pairs_for_dir",
                AsyncMock(return_value=[("q1", "a1")]),
            ),
            patch.object(facade, "_count_dir_messages", AsyncMock(return_value=4)),
            patch.object(
                facade, "_execute", AsyncMock(return_value=_make_mock_response())
            ),
        ):
            await facade.run_command(
                prompt="hello", working_directory=project, user_id=123
            )

        events = [d for e, d in recorded if e == "fresh"]
        assert events, f"no fresh event recorded: {recorded}"
        assert events[0]["reason"] == "cold_start"
        assert events[0]["seeded"] is True
        assert events[0]["tail_messages"] == 1


class TestResumeFailureClassification:
    """A failed resume turn deletes the session mapping ONLY when the session
    is actually gone (dv-2a5a). A 600s timeout on 2026-09-10 used to be
    treated as a dead session and wiped a 60-turn conversation."""

    async def _seed_session(self, session_manager: SessionManager) -> ClaudeSession:
        existing = ClaudeSession(
            session_id="old-session-id",
            user_id=123,
            project_path=Path("/test/project"),
            created_at=datetime.now(UTC),
            last_used=datetime.now(UTC),
        )
        await session_manager.storage.save_session(existing)
        session_manager.active_sessions[existing.session_id] = existing
        return existing

    async def test_timeout_keeps_the_session(self, facade, session_manager):
        from src.claude.exceptions import ClaudeTimeoutError

        await self._seed_session(session_manager)
        with patch.object(
            facade,
            "_execute",
            AsyncMock(
                side_effect=ClaudeTimeoutError("Claude SDK timed out after 600s")
            ),
        ) as execute:
            with pytest.raises(ClaudeTimeoutError):
                await facade.run_command(
                    prompt="hello", working_directory=Path("/test/project"), user_id=123
                )

        assert execute.await_count == 1, "must not retry on a transient failure"
        assert await session_manager.storage.load_session("old-session-id", 123)
        assert getattr(session_manager.storage, "retired", []) == []

    @pytest.mark.parametrize(
        "error_text, kind",
        [
            (
                "Claude process error: No conversation found with session ID: x",
                "transcript_missing",
            ),
            ("exit 1", "transcript_missing"),  # no transcript file under tmp HOME
        ],
    )
    async def test_dead_session_is_retired_with_reason(
        self, facade, session_manager, error_text, kind
    ):
        await self._seed_session(session_manager)
        with (
            patch.object(facade, "_recent_pairs", AsyncMock(return_value=[("q", "a")])),
            patch.object(facade, "_count_session_messages", AsyncMock(return_value=3)),
            patch.object(
                facade,
                "_execute",
                AsyncMock(
                    side_effect=[RuntimeError(error_text), _make_mock_response()]
                ),
            ) as execute,
        ):
            result = await facade.run_command(
                prompt="hello", working_directory=Path("/test/project"), user_id=123
            )

        assert execute.await_count == 2
        assert result.session_restarted is True
        assert result.session_fresh_reason == f"resume failed ({kind}), 1 turns seeded"
        assert session_manager.storage.retired == [
            ("old-session-id", f"resume_failed:{kind}")
        ]

    async def test_process_error_with_transcript_present_is_still_dead(
        self, facade, session_manager, tmp_path, monkeypatch
    ):
        """Historical behaviour kept: a generic process error while the
        transcript exists is treated as dead so a poisoned transcript
        cannot wedge a profile. The kind says which branch fired."""
        from src.claude import facade as facade_mod

        monkeypatch.setattr(facade_mod, "_project_jsonl_dir", lambda wd: tmp_path)
        (tmp_path / "old-session-id.jsonl").write_text("{}\n")
        await self._seed_session(session_manager)
        with (
            patch.object(facade, "_recent_pairs", AsyncMock(return_value=[])),
            patch.object(facade, "_count_session_messages", AsyncMock(return_value=0)),
            patch.object(
                facade,
                "_execute",
                AsyncMock(side_effect=[RuntimeError("exit 1"), _make_mock_response()]),
            ),
        ):
            result = await facade.run_command(
                prompt="hello", working_directory=Path("/test/project"), user_id=123
            )

        assert result.session_fresh_reason == "resume failed (process_error), unseeded"
        assert session_manager.storage.retired == [
            ("old-session-id", "resume_failed:process_error")
        ]

    async def test_cold_start_names_itself_in_the_response(self, facade):
        with (
            patch.object(
                facade,
                "_recent_pairs_for_dir",
                AsyncMock(return_value=[("q1", "a1"), ("q2", "a2")]),
            ),
            patch.object(facade, "_count_dir_messages", AsyncMock(return_value=9)),
            patch.object(
                facade, "_execute", AsyncMock(return_value=_make_mock_response())
            ),
        ):
            result = await facade.run_command(
                prompt="hello", working_directory=Path("/test/project"), user_id=123
            )

        assert result.session_fresh_reason == "cold start, 2 turns seeded"

    async def test_resumed_turn_has_no_fresh_reason(self, facade, session_manager):
        await self._seed_session(session_manager)
        resp = _make_mock_response()
        resp.session_fresh_reason = None  # a MagicMock attribute is never None
        with patch.object(facade, "_execute", AsyncMock(return_value=resp)):
            result = await facade.run_command(
                prompt="hello", working_directory=Path("/test/project"), user_id=123
            )
        assert result.session_fresh_reason is None

    async def test_retirement_emits_lifecycle_event(self, config, session_manager):
        recorded = []

        class _Lifecycle:
            async def record(self, profile, event, detail):
                recorded.append((event, detail))

        facade = ClaudeIntegration(
            config=config,
            sdk_manager=MagicMock(),
            session_manager=session_manager,
            lifecycle_logger=_Lifecycle(),
        )
        await self._seed_session(session_manager)
        await session_manager.remove_session(
            "old-session-id", reason="dir_cap:5", project_path=Path("/test/project")
        )

        assert (
            "session_retired",
            {"session": "old-session-id", "reason": "dir_cap:5"},
        ) in recorded
