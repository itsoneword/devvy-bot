"""Tests for ContextRefreshService."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.evaluator.refresh import ContextRefreshService


def _make_session(
    cumulative_tokens: int = 155_000, session_id: str = "sess-1"
) -> MagicMock:
    session = MagicMock()
    session.session_id = session_id
    session.cumulative_tokens = cumulative_tokens
    return session


def _make_service(
    evaluator: AsyncMock | None = None,
    session_manager: AsyncMock | None = None,
    get_conversation: AsyncMock | None = None,
    notify: AsyncMock | None = None,
) -> ContextRefreshService:
    return ContextRefreshService(
        evaluator_service=evaluator or AsyncMock(),
        session_manager=session_manager or AsyncMock(),
        get_conversation=get_conversation
        or AsyncMock(return_value="User: hi\nAssistant: hello"),
        notify_callback=notify,
    )


@pytest.mark.asyncio
async def test_successful_refresh() -> None:
    """Evaluator succeeds, session removed, notification sent."""
    evaluator = AsyncMock()
    evaluator.run = AsyncMock(return_value=True)
    session_mgr = AsyncMock()
    notify = AsyncMock()

    svc = _make_service(evaluator=evaluator, session_manager=session_mgr, notify=notify)
    session = _make_session()

    result = await svc.refresh(session, user_id=42)

    assert result is True
    evaluator.run.assert_awaited_once()
    session_mgr.remove_session.assert_awaited_once_with(
        "sess-1", reason="context_refresh"
    )
    notify.assert_awaited_once_with(
        42,
        "Session refreshed at 150k tokens. Memory preserved -- starting fresh session.",
    )


@pytest.mark.asyncio
async def test_refresh_continues_when_evaluator_fails() -> None:
    """Session still removed and notification sent even if evaluator raises."""
    evaluator = AsyncMock()
    evaluator.run = AsyncMock(side_effect=RuntimeError("CLI crash"))
    session_mgr = AsyncMock()
    notify = AsyncMock()

    svc = _make_service(evaluator=evaluator, session_manager=session_mgr, notify=notify)
    session = _make_session()

    result = await svc.refresh(session, user_id=42)

    assert result is True
    session_mgr.remove_session.assert_awaited_once_with(
        "sess-1", reason="context_refresh"
    )
    notify.assert_awaited_once()


@pytest.mark.asyncio
async def test_refresh_fails_when_session_removal_fails() -> None:
    """Returns False if session_manager.remove_session raises."""
    session_mgr = AsyncMock()
    session_mgr.remove_session = AsyncMock(side_effect=RuntimeError("DB error"))

    svc = _make_service(session_manager=session_mgr)
    session = _make_session()

    result = await svc.refresh(session, user_id=42)

    assert result is False


@pytest.mark.asyncio
async def test_notification_failure_doesnt_break_refresh() -> None:
    """Refresh returns True even when notification callback raises."""
    notify = AsyncMock(side_effect=RuntimeError("Telegram timeout"))
    session_mgr = AsyncMock()

    svc = _make_service(session_manager=session_mgr, notify=notify)
    session = _make_session()

    result = await svc.refresh(session, user_id=42)

    assert result is True
    session_mgr.remove_session.assert_awaited_once()


@pytest.mark.asyncio
async def test_evaluator_called_with_session_context() -> None:
    """Evaluator.run is called with the session's token + id context."""
    evaluator = AsyncMock()
    evaluator.run = AsyncMock(return_value=True)

    svc = _make_service(evaluator=evaluator)
    session = _make_session(cumulative_tokens=160_000)

    await svc.refresh(session, user_id=42)

    call_kwargs = evaluator.run.call_args.kwargs
    assert "include_decay" not in call_kwargs
    assert call_kwargs["cumulative_tokens"] == 160_000
    assert call_kwargs["session_id"] == "sess-1"


@pytest.mark.asyncio
async def test_refresh_without_notify_callback() -> None:
    """Refresh works when no notify_callback is provided."""
    session_mgr = AsyncMock()

    svc = _make_service(session_manager=session_mgr, notify=None)
    session = _make_session()

    result = await svc.refresh(session, user_id=42)

    assert result is True
    session_mgr.remove_session.assert_awaited_once()


def test_should_refresh_at_150k() -> None:
    """ClaudeSession.should_refresh() returns True at 150k+."""
    from datetime import UTC, datetime

    from src.claude.session import ClaudeSession

    session = ClaudeSession(
        session_id="s1",
        user_id=1,
        project_path="/tmp",
        created_at=datetime.now(UTC),
        last_used=datetime.now(UTC),
        cumulative_tokens=155_000,
    )
    assert session.should_refresh() is True


def test_should_not_refresh_below_150k() -> None:
    """ClaudeSession.should_refresh() returns False below 150k."""
    from datetime import UTC, datetime

    from src.claude.session import ClaudeSession

    session = ClaudeSession(
        session_id="s1",
        user_id=1,
        project_path="/tmp",
        created_at=datetime.now(UTC),
        last_used=datetime.now(UTC),
        cumulative_tokens=120_000,
    )
    assert session.should_refresh() is False
