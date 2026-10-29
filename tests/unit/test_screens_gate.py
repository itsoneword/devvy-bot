"""dv-4cd8: every registered bot screen passes the phone-width checks.

A screen that fails today is registered with ``known_bad="<ticket id>"``
(e.g. the admin menu, dv-f6af). That is a strict expectation: once the
screen is fixed this test fails until the marker is dropped.
"""

import re

import pytest

try:
    from src.bot.screens.registry import SCREENS
except ImportError:  # phase 1 not built yet: the existence test fails
    SCREENS = {}


def test_registry_has_the_model_screens_in_both_auth_states():
    from src.bot.screens.registry import SCREENS as screens

    assert "model.claude.logged_out" in screens
    assert "model.codex.logged_in" in screens


@pytest.mark.parametrize("screen_id", sorted(SCREENS))
async def test_registered_screen_passes_or_is_known_bad(screen_id):
    from src.bot.screens.checks import check

    spec = SCREENS[screen_id]
    built = await spec.build()
    assert built, f"{screen_id} built no screen"
    violations = [v for screen in built for v in check(screen)]
    if spec.known_bad:
        assert re.fullmatch(r"[a-z]+-[0-9a-f]{4}", spec.known_bad), spec.known_bad
        assert violations, f"{screen_id} passes now: drop known_bad={spec.known_bad!r}"
    else:
        assert violations == [], [(v.code, v.where, v.detail) for v in violations]
