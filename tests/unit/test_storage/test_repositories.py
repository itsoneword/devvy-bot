"""Tests for repository implementations."""

import sqlite3
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.storage.database import DatabaseManager
from src.storage.models import (
    MessageModel,
    ProjectThreadModel,
    SessionModel,
    ToolUsageModel,
    UserModel,
)
from src.storage.repositories import (
    AnalyticsRepository,
    AuditLogRepository,
    MessageRepository,
    ProjectThreadRepository,
    SessionRepository,
    ToolUsageRepository,
    UserRepository,
)


@pytest.fixture
async def db_manager():
    """Create test database manager."""
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "test.db"
        manager = DatabaseManager(f"sqlite:///{db_path}")
        await manager.initialize()
        yield manager
        await manager.close()


@pytest.fixture
async def user_repo(db_manager):
    """Create user repository."""
    return UserRepository(db_manager)


@pytest.fixture
async def session_repo(db_manager):
    """Create session repository."""
    return SessionRepository(db_manager)


@pytest.fixture
async def message_repo(db_manager):
    """Create message repository."""
    return MessageRepository(db_manager)


@pytest.fixture
async def tool_repo(db_manager):
    """Create tool usage repository."""
    return ToolUsageRepository(db_manager)


@pytest.fixture
async def audit_repo(db_manager):
    """Create audit log repository."""
    return AuditLogRepository(db_manager)


@pytest.fixture
async def analytics_repo(db_manager):
    """Create analytics repository."""
    return AnalyticsRepository(db_manager)


@pytest.fixture
async def project_thread_repo(db_manager):
    """Create project thread repository."""
    return ProjectThreadRepository(db_manager)


class TestUserRepository:
    """Test user repository."""

    async def test_create_and_get_user(self, user_repo):
        """Test creating and retrieving user."""
        user = UserModel(
            user_id=12345,
            telegram_username="testuser",
            first_seen=datetime.now(UTC),
            last_active=datetime.now(UTC),
            is_allowed=True,
        )

        # Create user
        created_user = await user_repo.create_user(user)
        assert created_user.user_id == 12345

        # Get user
        retrieved_user = await user_repo.get_user(12345)
        assert retrieved_user is not None
        assert retrieved_user.user_id == 12345
        assert retrieved_user.telegram_username == "testuser"
        assert retrieved_user.is_allowed == 1  # SQLite stores boolean as integer

    async def test_update_user(self, user_repo):
        """Test updating user."""
        user = UserModel(
            user_id=12346,
            telegram_username="testuser2",
            first_seen=datetime.now(UTC),
            last_active=datetime.now(UTC),
            is_allowed=False,
            total_cost=10.5,
            message_count=5,
        )

        await user_repo.create_user(user)

        # Update user
        user.total_cost = 20.0
        user.message_count = 10
        await user_repo.update_user(user)

        # Verify update
        updated_user = await user_repo.get_user(12346)
        assert updated_user.total_cost == 20.0
        assert updated_user.message_count == 10

    async def test_get_allowed_users(self, user_repo):
        """Test getting allowed users."""
        # Create allowed user
        allowed_user = UserModel(
            user_id=12347,
            telegram_username="allowed",
            first_seen=datetime.now(UTC),
            last_active=datetime.now(UTC),
            is_allowed=True,
        )
        await user_repo.create_user(allowed_user)

        # Create disallowed user
        disallowed_user = UserModel(
            user_id=12348,
            telegram_username="disallowed",
            first_seen=datetime.now(UTC),
            last_active=datetime.now(UTC),
            is_allowed=False,
        )
        await user_repo.create_user(disallowed_user)

        # Get allowed users
        allowed_users = await user_repo.get_allowed_users()
        assert 12347 in allowed_users
        assert 12348 not in allowed_users


class TestSessionRepository:
    """Test session repository."""

    async def test_create_and_get_session(self, session_repo, user_repo):
        """Test creating and retrieving session."""
        # Create user first
        user = UserModel(
            user_id=12349,
            telegram_username="sessionuser",
            first_seen=datetime.now(UTC),
            last_active=datetime.now(UTC),
            is_allowed=True,
        )
        await user_repo.create_user(user)

        # Create session
        session = SessionModel(
            session_id="test-session-123",
            user_id=12349,
            project_path="/test/project",
            created_at=datetime.now(UTC),
            last_used=datetime.now(UTC),
            total_cost=5.0,
            total_turns=3,
            message_count=2,
        )

        created_session = await session_repo.create_session(session)
        assert created_session.session_id == "test-session-123"

        # Get session
        retrieved_session = await session_repo.get_session("test-session-123")
        assert retrieved_session is not None
        assert retrieved_session.user_id == 12349
        assert retrieved_session.project_path == "/test/project"

    async def test_get_user_sessions(self, session_repo, user_repo):
        """Test getting user sessions."""
        # Create user
        user = UserModel(
            user_id=12350,
            telegram_username="multisessionuser",
            first_seen=datetime.now(UTC),
            last_active=datetime.now(UTC),
            is_allowed=True,
        )
        await user_repo.create_user(user)

        # Create multiple sessions
        for i in range(3):
            session = SessionModel(
                session_id=f"test-session-{i}",
                user_id=12350,
                project_path=f"/test/project{i}",
                created_at=datetime.now(UTC),
                last_used=datetime.now(UTC),
            )
            await session_repo.create_session(session)

        # Get user sessions
        sessions = await session_repo.get_user_sessions(12350)
        assert len(sessions) == 3
        assert all(s.user_id == 12350 for s in sessions)


class TestMessageRepository:
    """Test message repository."""

    async def test_save_and_get_messages(self, message_repo, session_repo, user_repo):
        """Test saving and retrieving messages."""
        # Setup user and session
        user = UserModel(
            user_id=12352,
            telegram_username="messageuser",
            first_seen=datetime.now(UTC),
            last_active=datetime.now(UTC),
            is_allowed=True,
        )
        await user_repo.create_user(user)

        session = SessionModel(
            session_id="message-session",
            user_id=12352,
            project_path="/test/messages",
            created_at=datetime.now(UTC),
            last_used=datetime.now(UTC),
        )
        await session_repo.create_session(session)

        # Save message
        message = MessageModel(
            session_id="message-session",
            user_id=12352,
            timestamp=datetime.now(UTC),
            prompt="Test prompt",
            response="Test response",
            cost=0.05,
            duration_ms=1500,
        )

        message_id = await message_repo.save_message(message)
        assert message_id is not None

        # Get session messages
        messages = await message_repo.get_session_messages("message-session")
        assert len(messages) == 1
        assert messages[0].prompt == "Test prompt"
        assert messages[0].response == "Test response"


class TestMessageRepositorySearchFts:
    """recall_conversation (dv-r3c1): FTS5 keyword search over raw messages.

    The load-bearing property is scoping: results must never cross a
    (user, working_dir) boundary. A leak here is a security bug (dv-6dbc),
    not a UX bug.
    """

    async def _seed(
        self,
        user_repo,
        session_repo,
        message_repo,
        user_id: int,
        session_id: str,
        project_path: str,
        prompt: str,
        response: str,
    ) -> None:
        # A test may seed the same user across two working dirs (to prove
        # cross-profile scoping) -- create_user() has no upsert, so guard
        # against the duplicate-insert IntegrityError instead of requiring
        # every caller to track what it already created.
        try:
            await user_repo.create_user(
                UserModel(
                    user_id=user_id,
                    telegram_username=f"user{user_id}",
                    first_seen=datetime.now(UTC),
                    last_active=datetime.now(UTC),
                    is_allowed=True,
                )
            )
        except sqlite3.IntegrityError:
            pass
        await session_repo.create_session(
            SessionModel(
                session_id=session_id,
                user_id=user_id,
                project_path=project_path,
                created_at=datetime.now(UTC),
                last_used=datetime.now(UTC),
            )
        )
        await message_repo.save_message(
            MessageModel(
                session_id=session_id,
                user_id=user_id,
                timestamp=datetime.now(UTC),
                prompt=prompt,
                response=response,
            )
        )

    async def test_finds_keyword_match(self, message_repo, session_repo, user_repo):
        await self._seed(
            user_repo,
            session_repo,
            message_repo,
            111,
            "s1",
            "/home/u/devops",
            "what did we decide about the deploy window",
            "we agreed on Tuesdays at 09:00 UTC",
        )

        hits = await message_repo.search_fts(111, "/home/u/devops", "deploy window", 5)

        assert len(hits) == 1
        assert "deploy window" in hits[0].prompt_snippet
        assert hits[0].window_id is None

    async def test_scoped_to_calling_user_only(
        self, message_repo, session_repo, user_repo
    ):
        """Same working_dir, different user -- must not leak across users."""
        await self._seed(
            user_repo,
            session_repo,
            message_repo,
            111,
            "s-a",
            "/home/u/devops",
            "the database password rotation is scheduled for Friday",
            "confirmed",
        )
        await self._seed(
            user_repo,
            session_repo,
            message_repo,
            222,
            "s-b",
            "/home/u/devops",
            "unrelated message about lunch",
            "unrelated response",
        )

        hits = await message_repo.search_fts(
            222, "/home/u/devops", "password rotation", 5
        )

        assert hits == []

    async def test_scoped_to_calling_working_dir_only(
        self, message_repo, session_repo, user_repo
    ):
        """Same user, different profile working_dir -- must not cross-leak.

        This is the boundary dv-6dbc closed for facts, applied to raw
        conversation history: `messages` has no working_dir column, so
        scoping goes through sessions.project_path.
        """
        await self._seed(
            user_repo,
            session_repo,
            message_repo,
            111,
            "s-devops",
            "/home/u/devops",
            "the on-call rotation secret is stored in vault",
            "noted",
        )
        await self._seed(
            user_repo,
            session_repo,
            message_repo,
            111,
            "s-coach",
            "/home/u/coach",
            "unrelated coaching chat",
            "unrelated reply",
        )

        hits = await message_repo.search_fts(111, "/home/u/coach", "on-call vault", 5)

        assert hits == []
        hits_correct_profile = await message_repo.search_fts(
            111, "/home/u/devops", "on-call vault", 5
        )
        assert len(hits_correct_profile) == 1

    async def test_cyrillic_substring_matches_via_prefix(
        self, message_repo, session_repo, user_repo
    ):
        """unicode61 has no Russian stemmer -- обед/обеда/обедом are distinct
        tokens. The FTS table is built with prefix='2 3 4' and the query
        helper appends '*', so a short prefix still matches the inflected
        form actually stored."""
        await self._seed(
            user_repo,
            session_repo,
            message_repo,
            111,
            "s-ru",
            "/home/u/devops",
            "договорились на обеде обсудить релиз",
            "хорошо, увидимся на обеде",
        )

        hits = await message_repo.search_fts(111, "/home/u/devops", "обед", 5)

        assert len(hits) == 1
        assert "обеде" in hits[0].prompt_snippet

    async def test_snippet_is_truncated_hard(
        self, message_repo, session_repo, user_repo
    ):
        from src.storage.repositories import _SNIPPET_MAX_CHARS

        long_prompt = "keyword " + ("x" * (_SNIPPET_MAX_CHARS + 500))
        await self._seed(
            user_repo,
            session_repo,
            message_repo,
            111,
            "s-long",
            "/home/u/devops",
            long_prompt,
            "y" * (_SNIPPET_MAX_CHARS + 500),
        )

        hits = await message_repo.search_fts(111, "/home/u/devops", "keyword", 5)

        assert len(hits) == 1
        assert len(hits[0].prompt_snippet) <= _SNIPPET_MAX_CHARS + 1
        assert len(hits[0].response_snippet) <= _SNIPPET_MAX_CHARS + 1

    async def test_newest_first(self, message_repo, session_repo, user_repo):
        await user_repo.create_user(
            UserModel(
                user_id=111,
                telegram_username="u",
                first_seen=datetime.now(UTC),
                last_active=datetime.now(UTC),
                is_allowed=True,
            )
        )
        await session_repo.create_session(
            SessionModel(
                session_id="s-order",
                user_id=111,
                project_path="/home/u/devops",
                created_at=datetime.now(UTC),
                last_used=datetime.now(UTC),
            )
        )
        older = datetime.now(UTC) - timedelta(hours=1)
        newer = datetime.now(UTC)
        await message_repo.save_message(
            MessageModel(
                session_id="s-order",
                user_id=111,
                timestamp=older,
                prompt="marker older",
                response="",
            )
        )
        await message_repo.save_message(
            MessageModel(
                session_id="s-order",
                user_id=111,
                timestamp=newer,
                prompt="marker newer",
                response="",
            )
        )

        hits = await message_repo.search_fts(111, "/home/u/devops", "marker", 5)

        assert [h.prompt_snippet for h in hits] == ["marker newer", "marker older"]

    async def test_limit_is_respected(self, message_repo, session_repo, user_repo):
        await user_repo.create_user(
            UserModel(
                user_id=111,
                telegram_username="u",
                first_seen=datetime.now(UTC),
                last_active=datetime.now(UTC),
                is_allowed=True,
            )
        )
        await session_repo.create_session(
            SessionModel(
                session_id="s-limit",
                user_id=111,
                project_path="/home/u/devops",
                created_at=datetime.now(UTC),
                last_used=datetime.now(UTC),
            )
        )
        for i in range(5):
            await message_repo.save_message(
                MessageModel(
                    session_id="s-limit",
                    user_id=111,
                    timestamp=datetime.now(UTC),
                    prompt=f"capped message {i}",
                    response="",
                )
            )

        hits = await message_repo.search_fts(111, "/home/u/devops", "capped", 2)

        assert len(hits) == 2


class TestProjectThreadRepository:
    """Test project thread repository."""

    async def test_upsert_and_lookup(self, project_thread_repo):
        """Upsert creates mapping and lookup resolves it."""
        mapping = await project_thread_repo.upsert_mapping(
            project_slug="app1",
            chat_id=-1001234567890,
            message_thread_id=321,
            topic_name="App One",
        )

        assert isinstance(mapping, ProjectThreadModel)
        assert mapping.project_slug == "app1"
        assert mapping.message_thread_id == 321

        lookup = await project_thread_repo.get_by_chat_thread(-1001234567890, 321)
        assert lookup is not None
        assert lookup.project_slug == "app1"

    async def test_deactivate_missing_projects(self, project_thread_repo):
        """Mappings not in active set are deactivated."""
        await project_thread_repo.upsert_mapping(
            project_slug="app1",
            chat_id=-1001234567890,
            message_thread_id=111,
            topic_name="App 1",
        )
        await project_thread_repo.upsert_mapping(
            project_slug="app2",
            chat_id=-1001234567890,
            message_thread_id=222,
            topic_name="App 2",
        )

        changed = await project_thread_repo.deactivate_missing_projects(
            chat_id=-1001234567890,
            active_project_slugs=["app1"],
        )

        assert changed == 1
        inactive_mapping = await project_thread_repo.get_by_chat_project(
            -1001234567890, "app2"
        )
        assert inactive_mapping is not None
        assert inactive_mapping.is_active is False

    async def test_list_stale_active_mappings(self, project_thread_repo):
        """Returns only active mappings not in enabled project set."""
        await project_thread_repo.upsert_mapping(
            project_slug="app1",
            chat_id=-1001234567890,
            message_thread_id=111,
            topic_name="App 1",
            is_active=True,
        )
        await project_thread_repo.upsert_mapping(
            project_slug="app2",
            chat_id=-1001234567890,
            message_thread_id=222,
            topic_name="App 2",
            is_active=True,
        )
        await project_thread_repo.upsert_mapping(
            project_slug="app3",
            chat_id=-1001234567890,
            message_thread_id=333,
            topic_name="App 3",
            is_active=False,
        )

        stale = await project_thread_repo.list_stale_active_mappings(
            chat_id=-1001234567890,
            active_project_slugs=["app1"],
        )

        assert len(stale) == 1
        assert stale[0].project_slug == "app2"

    async def test_set_active_updates_flag(self, project_thread_repo):
        """set_active toggles mapping active flag."""
        await project_thread_repo.upsert_mapping(
            project_slug="app1",
            chat_id=-1001234567890,
            message_thread_id=111,
            topic_name="App 1",
            is_active=True,
        )

        changed = await project_thread_repo.set_active(
            chat_id=-1001234567890,
            project_slug="app1",
            is_active=False,
        )

        assert changed == 1
        mapping = await project_thread_repo.get_by_chat_project(-1001234567890, "app1")
        assert mapping is not None
        assert mapping.is_active is False


class TestToolUsageRepository:
    """Test tool usage repository."""

    async def test_save_and_get_tool_usage(self, tool_repo, session_repo, user_repo):
        """Test saving and retrieving tool usage."""
        # Setup user and session
        user = UserModel(
            user_id=12353,
            telegram_username="tooluser",
            first_seen=datetime.now(UTC),
            last_active=datetime.now(UTC),
            is_allowed=True,
        )
        await user_repo.create_user(user)

        session = SessionModel(
            session_id="tool-session",
            user_id=12353,
            project_path="/test/tools",
            created_at=datetime.now(UTC),
            last_used=datetime.now(UTC),
        )
        await session_repo.create_session(session)

        # Save tool usage
        tool_usage = ToolUsageModel(
            session_id="tool-session",
            tool_name="Read",
            tool_input={"file_path": "/test/file.py"},
            timestamp=datetime.now(UTC),
            success=True,
        )

        usage_id = await tool_repo.save_tool_usage(tool_usage)
        assert usage_id is not None

        # Get session tool usage
        usage_records = await tool_repo.get_session_tool_usage("tool-session")
        assert len(usage_records) == 1
        assert usage_records[0].tool_name == "Read"
        assert usage_records[0].tool_input["file_path"] == "/test/file.py"

    async def test_get_tool_stats(self, tool_repo, session_repo, user_repo):
        """Test getting tool statistics."""
        # Setup user and session
        user = UserModel(
            user_id=12354,
            telegram_username="statsuser",
            first_seen=datetime.now(UTC),
            last_active=datetime.now(UTC),
            is_allowed=True,
        )
        await user_repo.create_user(user)

        session = SessionModel(
            session_id="stats-session",
            user_id=12354,
            project_path="/test/stats",
            created_at=datetime.now(UTC),
            last_used=datetime.now(UTC),
        )
        await session_repo.create_session(session)

        # Create multiple tool usages
        tools = ["Read", "Write", "Read", "Edit", "Read"]
        for tool in tools:
            tool_usage = ToolUsageModel(
                session_id="stats-session",
                tool_name=tool,
                timestamp=datetime.now(UTC),
                success=True,
            )
            await tool_repo.save_tool_usage(tool_usage)

        # Get tool stats
        stats = await tool_repo.get_tool_stats()

        # Find Read tool stats
        read_stats = next(s for s in stats if s["tool_name"] == "Read")
        assert read_stats["usage_count"] == 3
        assert read_stats["success_count"] == 3
        assert read_stats["error_count"] == 0


class TestAuditLogRepository:
    """Test audit log repository."""

class TestAnalyticsRepository:
    """Test analytics repository."""

    async def test_get_system_stats(
        self, analytics_repo, message_repo, session_repo, user_repo
    ):
        """Test getting system statistics."""
        # Setup test data
        user = UserModel(
            user_id=12355,
            telegram_username="analyticsuser",
            first_seen=datetime.now(UTC),
            last_active=datetime.now(UTC),
            is_allowed=True,
        )
        await user_repo.create_user(user)

        session = SessionModel(
            session_id="analytics-session",
            user_id=12355,
            project_path="/test/analytics",
            created_at=datetime.now(UTC),
            last_used=datetime.now(UTC),
        )
        await session_repo.create_session(session)

        # Create messages
        for i in range(3):
            message = MessageModel(
                session_id="analytics-session",
                user_id=12355,
                timestamp=datetime.now(UTC),
                prompt=f"Test prompt {i}",
                response=f"Test response {i}",
                cost=0.1,
            )
            await message_repo.save_message(message)

        # Get system stats
        stats = await analytics_repo.get_system_stats()

        assert stats["overall"]["total_users"] >= 1
        assert stats["overall"]["total_sessions"] >= 1
        assert stats["overall"]["total_messages"] >= 3
        assert stats["overall"]["total_cost"] >= 0.3
