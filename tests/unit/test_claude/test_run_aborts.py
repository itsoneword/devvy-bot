"""A run that stops before producing a result must say so.

Regression tests for the silent-truncation bug: a --max-budget-usd stop
returns subtype=error_max_budget_usd with result=None, so the partial
narration the model had already streamed became the "answer" and the turn
looked like it trailed off mid-sentence. Nothing logged it, nothing labelled
it. Verified against CLI 2.1.220 before writing these.
"""

from pathlib import Path
from unittest.mock import patch

import pytest

from src.claude.sdk_integration import ClaudeSDKManager
from src.config.settings import Settings
from src.profiles.registry import ProfileDefinition, ProfileRegistry

# _patch_parse_message is an autouse fixture: importing it here is what applies
# it to this module, so it is used despite looking unused.
from .test_sdk_integration import (  # noqa: F401
    _make_assistant_message,
    _make_result_message,
    _mock_client_factory,
    _patch_parse_message,
)


@pytest.fixture
def config(tmp_path):
    return Settings(
        telegram_bot_token="test:token",
        telegram_bot_username="testbot",
        approved_directory=tmp_path,
        claude_timeout_seconds=2,
        claude_max_cost_per_request=5.0,
        enable_mcp=False,
    )


@pytest.fixture
def sdk_manager(config):
    return ClaudeSDKManager(config)


def _profile(tmp_path, **kwargs):
    return ProfileDefinition(
        slug="coach",
        name="Coach",
        working_dir=tmp_path / "coach",
        memory_dir=tmp_path / "coach" / "memory",
        persona=tmp_path / "coach" / "persona.md",
        default=True,
        **kwargs,
    )


class TestAbortIsSurfaced:
    async def test_budget_stop_is_labelled_not_passed_off_as_an_answer(
        self, sdk_manager
    ):
        """The exact production symptom: narration, then silence."""
        mock_factory = _mock_client_factory(
            _make_assistant_message("All right, now I am going to do this:"),
            _make_result_message(
                subtype="error_max_budget_usd", is_error=True, result=None
            ),
        )

        with patch(
            "src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_factory
        ):
            response = await sdk_manager.execute_command(
                prompt="do an expensive thing",
                working_directory=Path("/test"),
            )

        assert response.error_type == "max_budget"
        # The narration is still returned — it has value — but the caller can
        # now tell it apart from a finished answer.
        assert "All right" in response.content

    async def test_max_turns_stop_still_labelled(self, sdk_manager):
        mock_factory = _mock_client_factory(
            _make_assistant_message("Working on step 4..."),
            _make_result_message(subtype="error_max_turns", is_error=True, result=None),
        )

        with patch(
            "src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_factory
        ):
            response = await sdk_manager.execute_command(
                prompt="long task",
                working_directory=Path("/test"),
            )

        assert response.error_type == "max_turns"

    async def test_unknown_error_subtype_is_not_silent(self, sdk_manager):
        """A subtype we've never seen still beats presenting narration as an
        answer — this is the class of bug, not just the one instance."""
        mock_factory = _mock_client_factory(
            _make_assistant_message("Partial work"),
            _make_result_message(
                subtype="error_something_new", is_error=True, result=None
            ),
        )

        with patch(
            "src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_factory
        ):
            response = await sdk_manager.execute_command(
                prompt="task",
                working_directory=Path("/test"),
            )

        assert response.error_type == "agent_error"

    async def test_successful_run_has_no_error_type(self, sdk_manager):
        mock_factory = _mock_client_factory(
            _make_assistant_message("Done"),
            _make_result_message(result="Done"),
        )

        with patch(
            "src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_factory
        ):
            response = await sdk_manager.execute_command(
                prompt="task",
                working_directory=Path("/test"),
            )

        assert response.error_type is None

    async def test_empty_abort_does_not_trigger_the_retry(self, sdk_manager):
        """The empty-result backstop must not re-run a turn that deliberately
        stopped: it would hit the same cap and bill for the work twice."""
        calls = []

        def factory(options):
            calls.append(options)
            return _mock_client_factory(
                _make_result_message(
                    subtype="error_max_budget_usd", is_error=True, result=None
                )
            )(options)

        with patch("src.claude.sdk_integration.ClaudeSDKClient", side_effect=factory):
            response = await sdk_manager.execute_command(
                prompt="task",
                working_directory=Path("/test"),
            )

        assert len(calls) == 1, "aborted run was retried"
        assert response.error_type == "max_budget"


class TestBudgetResolution:
    def test_owner_is_uncapped_by_default(self, sdk_manager, tmp_path):
        assert sdk_manager._max_budget_for(tmp_path, "owner") is None

    def test_everyone_else_keeps_the_global_cap(self, sdk_manager, tmp_path):
        assert sdk_manager._max_budget_for(tmp_path, "guest") == 5.0
        # role=None (webhook/scheduler callers with no resolved human) too.
        assert sdk_manager._max_budget_for(tmp_path, None) == 5.0

    def test_profile_cap_applies_to_owners_too(self, config, tmp_path):
        """A per-profile cap is a deliberate statement about that agent, so it
        outranks the owner exemption — a cheap agent stuck in a loop should be
        bounded regardless of who started it."""
        registry = ProfileRegistry([_profile(tmp_path, max_cost_per_request=0.5)])
        manager = ClaudeSDKManager(config, profile_registry=registry)

        assert manager._max_budget_for(tmp_path / "coach", "owner") == 0.5
        assert manager._max_budget_for(tmp_path / "coach", "guest") == 0.5

    def test_profile_cap_of_zero_means_uncapped(self, config, tmp_path):
        registry = ProfileRegistry([_profile(tmp_path, max_cost_per_request=0)])
        manager = ClaudeSDKManager(config, profile_registry=registry)

        assert manager._max_budget_for(tmp_path / "coach", "guest") is None

    def test_profile_without_a_cap_inherits(self, config, tmp_path):
        registry = ProfileRegistry([_profile(tmp_path)])
        manager = ClaudeSDKManager(config, profile_registry=registry)

        assert manager._max_budget_for(tmp_path / "coach", "owner") is None
        assert manager._max_budget_for(tmp_path / "coach", "guest") == 5.0

    async def test_cap_reaches_the_sdk_options(self, config, tmp_path):
        """End of the wire: the resolved number is what the CLI is told."""
        registry = ProfileRegistry([_profile(tmp_path, max_cost_per_request=2.5)])
        manager = ClaudeSDKManager(config, profile_registry=registry)
        captured = []
        mock_factory = _mock_client_factory(
            _make_assistant_message("hi"),
            _make_result_message(result="hi"),
            capture_options=captured,
        )

        with patch(
            "src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_factory
        ):
            await manager.execute_command(
                prompt="task",
                working_directory=tmp_path / "coach",
                role="owner",
            )

        assert captured[0].max_budget_usd == 2.5
