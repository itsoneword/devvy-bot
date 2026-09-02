"""Facade injection of <today_state> (dv-df45).

The invariants that matter: only flagged profiles get the block, an
unflagged profile's prompt stays byte-identical, and no digest failure can
ever cost the user a turn.
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.claude.facade import ClaudeIntegration
from src.daystate.source import TodayState

COACH_DIR = Path("/tmp/ws/coach")
DEVOPS_DIR = Path("/tmp/ws/devops")


def _registry() -> MagicMock:
    coach = MagicMock(slug="coach", today_digest=True)
    devops = MagicMock(slug="devops", today_digest=False)

    registry = MagicMock()
    registry.get_by_working_dir.side_effect = lambda d: {
        COACH_DIR: coach,
        DEVOPS_DIR: devops,
    }.get(Path(d))
    return registry


def _provider(state=None, error: Exception = None) -> MagicMock:
    provider = MagicMock()

    async def get_today_state(user_id: int, today: str):
        if error:
            raise error
        return state

    provider.get_today_state = get_today_state
    return provider


def _integration(provider, config) -> ClaudeIntegration:
    return ClaudeIntegration(
        config=config,
        sdk_manager=MagicMock(),
        profile_registry=_registry(),
        digest_provider=provider,
    )


@pytest.fixture
def config() -> MagicMock:
    return MagicMock(user_timezone="Europe/Lisbon")


class TestTodayStateInjection:
    async def test_flagged_profile_gets_the_block_before_the_prompt(
        self, config
    ) -> None:
        state = TodayState(date="2026-08-14", food_meals=2, food_kcal=900.0)
        integration = _integration(_provider(state), config)

        result = await integration._prepend_today_state("привет", COACH_DIR, 42)

        assert result.startswith("<today_state")
        assert result.endswith("привет")
        assert "900 ккал" in result

    async def test_unflagged_profile_prompt_is_byte_identical(self, config) -> None:
        state = TodayState(date="2026-08-14", food_meals=2)
        integration = _integration(_provider(state), config)

        result = await integration._prepend_today_state("привет", DEVOPS_DIR, 42)

        assert result == "привет"

    async def test_unknown_working_dir_is_left_alone(self, config) -> None:
        integration = _integration(_provider(TodayState(date="2026-08-14")), config)

        result = await integration._prepend_today_state("hi", Path("/tmp/other"), 42)

        assert result == "hi"

    async def test_provider_failure_never_costs_the_turn(self, config) -> None:
        integration = _integration(_provider(error=RuntimeError("db down")), config)

        result = await integration._prepend_today_state("привет", COACH_DIR, 42)

        assert result == "привет"

    async def test_bad_timezone_degrades_to_plain_prompt(self) -> None:
        config = MagicMock(user_timezone="Not/AZone")
        integration = _integration(_provider(TodayState(date="2026-08-14")), config)

        result = await integration._prepend_today_state("привет", COACH_DIR, 42)

        assert result == "привет"

    async def test_no_provider_configured_is_a_no_op(self, config) -> None:
        integration = ClaudeIntegration(
            config=config,
            sdk_manager=MagicMock(),
            profile_registry=_registry(),
            digest_provider=None,
        )

        result = await integration._prepend_today_state("привет", COACH_DIR, 42)

        assert result == "привет"

    async def test_empty_state_injects_nothing(self, config) -> None:
        integration = _integration(_provider(None), config)

        result = await integration._prepend_today_state("привет", COACH_DIR, 42)

        assert result == "привет"

    async def test_user_id_is_passed_through_to_the_provider(self, config) -> None:
        seen = {}

        provider = MagicMock()

        async def get_today_state(user_id: int, today: str):
            seen["user_id"] = user_id
            seen["today"] = today
            return TodayState(date=today)

        provider.get_today_state = get_today_state
        integration = _integration(provider, config)

        await integration._prepend_today_state("привет", COACH_DIR, 222222222)

        # Cross-user leakage would be a health-data breach; the id must be
        # the caller's, never a default or a cached one.
        assert seen["user_id"] == 222222222
        assert seen["today"].count("-") == 2
