"""A stopped run must be labelled for the user, not read as a finished answer.

Companion to tests/unit/test_claude/test_run_aborts.py: that one checks the
SDK layer sets error_type, this one checks the orchestrator turns it into
something the person in Telegram actually sees.
"""

from types import SimpleNamespace

import pytest

from src.bot.orchestrator import MessageOrchestrator


@pytest.fixture
def orchestrator():
    settings = SimpleNamespace(
        agentic_mode=True, agent_backend="claude", agent_model="claude"
    )
    return MessageOrchestrator(settings, {})


def test_budget_stop_tells_the_user_which_cap_and_how_to_resume(orchestrator):
    notice = orchestrator._stop_notice("max_budget")
    assert notice is not None
    assert "cost cap" in notice
    # Must name the knob — the whole failure was nobody knowing a cap existed.
    assert "CLAUDE_MAX_COST_PER_REQUEST" in notice
    assert "continue" in notice


def test_turn_limit_stop_still_explained(orchestrator):
    notice = orchestrator._stop_notice("max_turns")
    assert notice is not None
    assert "turn limit" in notice


def test_unknown_agent_error_is_explained(orchestrator):
    assert orchestrator._stop_notice("agent_error") is not None


def test_successful_run_adds_no_notice(orchestrator):
    assert orchestrator._stop_notice(None) is None
    assert orchestrator._stop_notice("something_else") is None
